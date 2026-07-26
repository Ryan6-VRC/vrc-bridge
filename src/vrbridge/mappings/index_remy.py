# mappings/index_remy.py
"""
RemyMapping: bridge SteamVR controller inputs to Remy REST API calls.

Mapping:
- Left  THUMBSTICK_SHORT_PRESS  -> POST /respond
- Left  THUMBSTICK_LONG_PRESS   -> POST /upload_image    (uploads newest file in watched folder)
- Left  TOUCHPAD_PRESS          -> PUT  /toggles/audio_0 {"enabled": true}
- Left  TOUCHPAD_RELEASE        -> PUT  /toggles/audio_0 {"enabled": false}
- Right TOUCHPAD_PRESS          -> PUT  /toggles/audio_0 {"enabled": true}
- Right TOUCHPAD_RELEASE        -> PUT  /toggles/audio_0 {"enabled": false}
- Right THUMBSTICK_SHORT_PRESS  -> POST /stop or /start  (based on GET /state)
- OSC  /avatar/parameters/GrabSync/GameAudio_IsGrabbed -> PUT /toggles/audio_1 {"enabled": <bool>}

Notes
-----
- All network I/O is done in a dedicated worker thread via a queue so
  controller callbacks never block. Errors are *logged* in the worker and
  never raised back into the controller thread.
- Touchpad actions are gated behind Mapping.enabled to allow coexistence
  with other mappings (see Mapping._gate in mapping_base).
- Thumbstick actions are intentionally registered *without* gating so they
  remain active regardless of mapping enable state.

The API endpoints above match the FastAPI routes:
  /respond, /start, /stop, /state, and /toggles/{mode}.
"""

from __future__ import annotations

import base64
import os
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

import httpx
from PIL import Image

from vrbridge.mappings.mapping_base import Mapping
from vrbridge.settings import settings
from vrbridge import ControllerEventType, VRBridge

# ------------------------------ Config ------------------------------------

# Host, timeouts, queue depth, retries, watched folder and upload height are
# settings.RemySettings, read at construction. VRBRIDGE_REMY_URL and
# VRBRIDGE_REMY_WATCH_DIR still override the host and folder, so an existing
# environment-driven setup keeps working.
IMAGE_EXTS: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".bmp", ".webp")

# OSC parameter to mirror grab state into Audio toggles
SELFAUDIO_GRAB_ADDR: str = "/avatar/parameters/GrabSync/SelfAudio" # Contact indicating local hand nearby
GAMEAUDIO_GRAB_ADDR: str = "/avatar/parameters/GrabSync/GameAudio_IsGrabbed" # PhysBone local or remote grab

# ------------------------------ Task types --------------------------------

@dataclass
class _HTTPRequest:
    """Represents a simple HTTP JSON request the worker can execute."""
    method: Literal["GET", "POST", "PUT", "DELETE"]
    path: str
    json: Optional[dict[str, Any]] = None

@dataclass
class _ToggleStartStop:
    """Ask worker to GET /state and POST /start or /stop accordingly."""
    pass

@dataclass
class _UploadLatestImage:
    """Ask worker to locate the newest image in WATCH_DIR and POST /upload_image."""
    pass


# ------------------------------ Mapping -----------------------------------

class RemyMapping(Mapping):
    """
    Map controller inputs to ai-remy REST API calls using a background worker.

    Touchpads are gated (only active when this mapping is enabled).
    Thumbsticks are always active (un-gated) per the spec.
    """
    name = "index_remy"

    def __init__(self, bridge: VRBridge, tuning=None):
        super().__init__(bridge)
        t = self._tune = tuning if tuning is not None else settings().remy
        self._base_url = os.environ.get("VRBRIDGE_REMY_URL") or t.base_url
        env_dir = os.environ.get("VRBRIDGE_REMY_WATCH_DIR")
        self._watch_dir = Path(env_dir).expanduser() if env_dir else t.resolved_watch_dir()
        self._q: "queue.Queue[object]" = queue.Queue(maxsize=t.work_queue_maxsize)
        self._worker = threading.Thread(target=self._worker_loop, name="RemyHTTPWorker", daemon=True)
        self._worker.start()

        self._audio_mode: Literal["none","self","game"] = "none"  # current selection
        self._last_audio0: Optional[bool] = None            # last /toggles/audio_0
        self._last_audio1: Optional[bool] = None            # last /toggles/audio_1

    # ---- registration -----------------------------------------------------

    def register(self) -> None:
        """Attach controller callbacks. Thumbsticks are unconditional; touchpads are gated."""
        super().register()

        # Thumbsticks (always active)
        self.bridge.on_controller(
            ControllerEventType.THUMBSTICK_SHORT_PRESS, hand="left",
            callback=self._on_left_thumb_short,
        )
        self.bridge.on_controller(
            ControllerEventType.THUMBSTICK_LONG_PRESS, hand="left",
            callback=self._on_left_thumb_long,
        )
        self.bridge.on_controller(
            ControllerEventType.THUMBSTICK_SHORT_PRESS, hand="right",
            callback=self._on_right_thumb_short_toggle,
        )

        # Touchpads (gated to allow coexistence with other mappings)
        self.bridge.on_controller(
            ControllerEventType.TOUCHPAD_PRESS, hand="left",
            callback=self._gate(self._on_left_tpad_press),
        )
        self.bridge.on_controller(
            ControllerEventType.TOUCHPAD_RELEASE, hand="left",
            callback=self._gate(self._on_left_tpad_release),
        )
        self.bridge.on_controller(
            ControllerEventType.TOUCHPAD_PRESS, hand="right",
            callback=self._gate(self._on_right_tpad_press),
        )
        self.bridge.on_controller(
            ControllerEventType.TOUCHPAD_RELEASE, hand="right",
            callback=self._gate(self._on_right_tpad_release),
        )

        # OSC (always active):
        # - Watch both GRAB_ADDRs. Decide initial mode on GameAudio_IsGrabbed -> 1 using current SelfAudio.
        # - If we chose selfaudio but SelfAudio later falls while grab persists, switch to gameaudio.
        self.bridge.on_osc(
            GAMEAUDIO_GRAB_ADDR,
            callback=self._on_gameaudio_grabbed,
            watch=[SELFAUDIO_GRAB_ADDR],
        )
        self.bridge.on_osc(
            SELFAUDIO_GRAB_ADDR,
            callback=self._on_selfaudio_change,
            watch=[GAMEAUDIO_GRAB_ADDR],
        )

    # ---- controller callbacks (thumbsticks: NOT gated) --------------------

    def _on_left_thumb_short(self, ctx, evt):
        """Left THUMBSTICK_SHORT_PRESS → POST /respond."""
        self._enqueue(_HTTPRequest("POST", "/respond"))

    def _on_left_thumb_long(self, ctx, evt):
        """
        Left THUMBSTICK_LONG_PRESS → POST /upload_image with the newest file.

        We enqueue a sentinel so the *worker* (not the controller thread) does:
          - find newest image in WATCH_DIR
          - optionally resize to TARGET_HEIGHT if Pillow is available
          - base64 encode and POST {"image_data": "data:<mime>;base64,<...>"} to /upload_image
        """
        self._enqueue(_UploadLatestImage())

    def _on_right_thumb_short_toggle(self, ctx, evt):
        """Right THUMBSTICK_SHORT_PRESS → toggle /start ↔ /stop based on GET /state."""
        self._enqueue(_ToggleStartStop())

    # ---- controller callbacks (touchpads: GATED) --------------------------

    def _on_left_tpad_press(self, ctx, evt):
        """Left TOUCHPAD_PRESS → enable audio_0."""
        self._enqueue(_HTTPRequest("PUT", "/toggles/audio_0", {"enabled": True}))

    def _on_left_tpad_release(self, ctx, evt):
        """Left TOUCHPAD_RELEASE → disable audio_0."""
        self._enqueue(_HTTPRequest("PUT", "/toggles/audio_0", {"enabled": False}))

    def _on_right_tpad_press(self, ctx, evt):
        """Right TOUCHPAD_PRESS → enable audio_0."""
        self._enqueue(_HTTPRequest("PUT", "/toggles/audio_0", {"enabled": True}))

    def _on_right_tpad_release(self, ctx, evt):
        """Right TOUCHPAD_RELEASE → disable audio_0."""
        self._enqueue(_HTTPRequest("PUT", "/toggles/audio_0", {"enabled": False}))

    # ---- OSC callbacks (NOT gated) ----------------------------------------

    def _on_gameaudio_grabbed(self, ctx, address, value):
        """
        GameAudio_IsGrabbed changed.
        - If 0/false -> no grab: disable both audio_0 and audio_1.
        - If 1/true  -> someone grabbed: choose between self/game using current SelfAudio snapshot.
        """
        if not value:
            self._set_audio_mode("none")
        elif ctx.get(SELFAUDIO_GRAB_ADDR, 0):
            self._set_audio_mode("self")
        else:
            self._set_audio_mode("game")

    def _on_selfaudio_change(self, ctx, address, value):
        """
        SelfAudio constant contact changed.
        If we previously decided 'self' and SelfAudio goes false while the grab persists,
        switch to 'game' to correct a possible false positive.
        """
        if  ctx.get(GAMEAUDIO_GRAB_ADDR, 0) and (not value) and (self._audio_mode == "self"):
            self._set_audio_mode("game")

    # ---- worker plumbing --------------------------------------------------

    def _enqueue(self, task: object) -> None:
        """Put a task on the worker queue without blocking the controller thread."""
        try:
            self._q.put_nowait(task)
        except queue.Full:
            # Drop rather than block the input thread.
            self.bridge.log.warning("RemyMapping queue full; dropping task: %r", task)

    def _worker_loop(self) -> None:
        """
        Background worker that executes queued tasks.

        Guarantees:
          - Uses a small timeout (HTTP_TIMEOUT_SEC).
          - Never raises back into the controller thread; all errors are logged and swallowed.
          - Light retry: each failing request is retried up to MAX_RETRIES additional times.

        This isolates network variability from the input loop.  The worker is a daemon thread
        that terminates with the process.
        """
        # Create a single client to reuse connections
        with httpx.Client(base_url=self._base_url, timeout=self._tune.http_timeout_sec) as client:
            while True:
                task = self._q.get()
                try:
                    if isinstance(task, _HTTPRequest):
                        self._do_request(client, task)
                    elif isinstance(task, _ToggleStartStop):
                        self._do_toggle_start_stop(client)
                    elif isinstance(task, _UploadLatestImage):
                        self._do_upload_latest(client)
                    else:
                        self.bridge.log.debug("RemyMapping: ignoring unknown task %r", task)
                except Exception as e:
                    # Log and continue; never bubble up.
                    self.bridge.log.warning("RemyMapping worker error for %r: %s", task, e)
                finally:
                    try:
                        self._q.task_done()
                    except Exception:
                        pass

    # ---- task handlers ----------------------------------------------------

    def _do_request(self, client: httpx.Client, req: _HTTPRequest) -> None:
        """
        Execute a JSON request with light retries.
        Logs status code at INFO on success, WARNING on failure.
        """
        last_exc = None
        for attempt in range(self._tune.max_retries + 1):
            try:
                r = client.request(req.method, req.path, json=req.json)
                self.bridge.log.info("RemyMapping %s %s -> %s", req.method, req.path, r.status_code)
                return
            except Exception as e:
                last_exc = e
        self.bridge.log.warning("RemyMapping %s %s failed after %d attempts: %s",
                                req.method, req.path, self._tune.max_retries + 1, last_exc)

    def _do_toggle_start_stop(self, client: httpx.Client) -> None:
        """GET /state; if 'stopped' then POST /start else POST /stop."""
        try:
            r = client.get("/state")
            state = (r.json() or {}).get("state")
        except Exception as e:
            self.bridge.log.warning("RemyMapping /state failed: %s", e)
            return

        dest = "/start" if state == "stopped" else "/stop"
        self._do_request(client, _HTTPRequest("POST", dest))

    def _do_upload_latest(self, client: httpx.Client) -> None:
        """Find the newest image in WATCH_DIR and POST to /upload_image."""
        latest = self._find_latest_image(self._watch_dir)
        if not latest:
            self.bridge.log.info("RemyMapping: no images found under %s", self._watch_dir)
            return

        try:
            height = self._tune.target_height if self._tune.resize_on_upload else None
            payload = {"image_data": self._encode_image_dataurl(latest, height)}
        except Exception as e:
            self.bridge.log.warning("RemyMapping: failed preparing image %s: %s", latest, e)
            return

        self._do_request(client, _HTTPRequest("POST", "/upload_image", payload))

    # ---- helpers ----------------------------------------------------------

    @staticmethod
    def _find_latest_image(root: Path) -> Optional[Path]:
        """Return the most recently modified image file under `root`, else None."""
        if not root.exists():
            return None
        newest_path: Optional[Path] = None
        newest_mtime = -1.0
        # Search recursively; skip unreadable entries.
        for ext in IMAGE_EXTS:
            for p in root.rglob(f"*{ext}"):
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    continue
                if mtime > newest_mtime:
                    newest_mtime = mtime
                    newest_path = p
        return newest_path

    @staticmethod
    def _encode_image_dataurl(path: Path, target_height: int | None) -> str:
        """Read an image, optionally resize it, and return a data URL for /upload_image.

        `target_height` of None uploads at the source resolution.
        """
        with Image.open(path).convert("RGB") as img:
            if target_height:
                w, h = img.size
                new_w = max(1, int(w * (target_height / float(h)))) if h else w
                img = img.resize((new_w, target_height), Image.Resampling.LANCZOS)
            import io
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            data = buf.getvalue()
        b64 = base64.b64encode(data).decode("utf-8")
        return f"data:image/jpeg;base64,{b64}"

    def _set_audio_mode(self, mode: Literal["none","self","game"]) -> None:
        """
        Set the desired audio mode:
          - None  -> audio_0=False, audio_1=False
          - "self"-> audio_0=True,  audio_1=False
          - "game"-> audio_0=False, audio_1=True
        Only enqueue requests when a value actually changes.
        """
        if mode == self._audio_mode:
            pass

        self._audio_mode = mode
        target_audio0 = (mode == "self")
        target_audio1 = (mode == "game")

        if target_audio0 != self._last_audio0:
            self._last_audio0 = target_audio0
            self._enqueue(_HTTPRequest("PUT", "/toggles/audio_0", {"enabled": target_audio0}))

        if target_audio1 != self._last_audio1:
            self._last_audio1 = target_audio1
            self._enqueue(_HTTPRequest("PUT", "/toggles/audio_1", {"enabled": target_audio1}))
