from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Literal, Optional

import openvr

from . import config as cfg

HandType = Literal["left", "right"]

@dataclass
class ControllerEvent:
    """Lightweight event container surfaced to the VRBridge."""
    type: str
    hand: HandType
    steps: int | None = None
    # Per-sample raw deltas
    dx: float | None = None
    dy: float | None = None
    # Absolute touch position (normalized trackpad coordinates)
    ax: float | None = None
    ay: float | None = None
    when: float = 0.0

class ControllerManager:
    """
    Thin SteamVR input backend.
    - Initializes OpenVR as a BACKGROUND app and registers an action manifest.
    - Emits touchpad (Index trackpad) and thumbstick/joystick (Knuckles/Quest) events.
    - Tolerates SteamVR restarts/disconnects and quits gracefully on VREvent_Quit.
    """
    def __init__(self, *, logger=None, vr_background: bool = True, files_dir: str | None = None):
        self.log = logger
        self.vr_background = vr_background
        self._listener: Optional[Callable[[ControllerEvent], None]] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._quit = threading.Event()
        self._initialized = False

        # Paths for SteamVR files; write them if missing.
        files = cfg.ensure_steamvr_files(files_dir=files_dir or cfg.get_files_dir())
        self.ACTIONS = files.actions
        self.BIND_KNU = files.bindings_knuckles
        self.BIND_OCU = files.bindings_oculus
        self.VRMAN   = files.vrmanifest

    # ---------------------- Lifecycle -------------------------------------
    def set_listener(self, fn: Callable[[ControllerEvent], None]):
        """Set a single event sink (set by VRBridge)."""
        self._listener = fn

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._quit.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="ControllerLoop")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.5)
            self._thread = None

    def quit_requested(self):
        return self._quit.is_set()

    # ---------------------- OpenVR init -----------------------------------
    def _init_openvr(self):
        """Init OpenVR in background/overlay mode and set up action handles."""
        app_type = openvr.VRApplication_Background if self.vr_background else openvr.VRApplication_Overlay
        openvr.init(app_type)

        # Register/identify app so SteamVR can apply default bindings.
        apps = openvr.VRApplications()
        if not apps.isApplicationInstalled(cfg.APP_KEY):
            try:
                apps.addApplicationManifest(self.VRMAN, False)
                if self.log: self.log.info("Installed VR manifest.")
            except openvr.error_code.ApplicationError as e:
                # benign if already installed
                if "ApplicationAlreadyInstalled" not in str(e):
                    raise
        apps.identifyApplication(0, cfg.APP_KEY)
        if self.log: self.log.info("IdentifyApplication OK.")

        # Cache the system (for polling events such as VREvent_Quit)
        self._vrsys = openvr.VRSystem()

        # Input action handles
        self._vrinput = openvr.VRInput()
        self._vrinput.setActionManifestPath(os.path.abspath(self.ACTIONS))
        self._set_main = self._vrinput.getActionSetHandle("/actions/main")

        self._a_track_click = self._vrinput.getActionHandle("/actions/main/in/track_click")
        self._a_track_touch = self._vrinput.getActionHandle("/actions/main/in/track_touch")
        self._a_track_pos   = self._vrinput.getActionHandle("/actions/main/in/track_pos")
        self._a_joy_click   = self._vrinput.getActionHandle("/actions/main/in/joy_click")
        self._a_joy_pos     = self._vrinput.getActionHandle("/actions/main/in/joy_pos")

        self._h_left  = self._vrinput.getInputSourceHandle("/user/hand/left")
        self._h_right = self._vrinput.getInputSourceHandle("/user/hand/right")

        self._action_sets = (openvr.VRActiveActionSet_t * 1)()
        self._action_sets[0].ulActionSet = self._set_main

        self._state = {
            self._h_left:  self._make_hand_state("left"),
            self._h_right: self._make_hand_state("right"),
        }

    def _make_hand_state(self, label: HandType):
        return {
            "label": label,
            # trackpad state
            "tpad_last_click": False,
            "tpad_down": False,
            "tpad_down_ts": 0.0,
            # thumbstick state
            "joy_last_click": False,
            "joy_down": False,
            "joy_down_ts": 0.0,
            # scroll accumulators
            "tpad_touching": False,
            "tpad_just_touched": False,
            "last_x": 0.0, "last_y": 0.0, "acc_x": 0.0, "acc_y": 0.0,
        }

    # ---------------------- Main loop -------------------------------------
    def _run(self):
        while not self._stop.is_set():
            try:
                if not self._initialized:
                    self._init_openvr()
                    self._initialized = True
                    if self.log: self.log.info("OpenVR initialized.")

                while not self._stop.is_set():
                    # SteamVR may ask background apps to quit when VR session ends.
                    # We poll events to catch VREvent_Quit and exit cleanly.
                    self._poll_vr_events()

                    # Update action state; if it fails (e.g., SteamVR restarting), back off and re-init.
                    try:
                        self._vrinput.updateActionState(self._action_sets)
                    except Exception as e:
                        if self.log: self.log.warning("updateActionState failed: %s", e)
                        time.sleep(0.25)
                        break  # re-init

                    self._poll_once()
                    time.sleep(cfg.CONTROLLER_POLL_INTERVAL)
            except KeyboardInterrupt:
                self._stop.set()
                break
            except Exception as e:
                if self.log: self.log.exception("Controller loop error: %s", e)
            finally:
                try:
                    openvr.shutdown()
                except Exception as e:
                    if self.log: self.log.debug("openvr.shutdown failed: %s", e)
                self._initialized = False
                time.sleep(0.5)

    def _poll_vr_events(self):
        """Poll system-level events and respond to quit requests."""
        try:
            # pyopenvr API: pollNextEvent fills an event struct and returns bool
            ev = openvr.VREvent_t()
            while self._vrsys.pollNextEvent(ev):
                if ev.eventType == openvr.VREvent_Quit:
                    if self.log: self.log.info("SteamVR requested quit (VREvent_Quit).")
                    self._quit.set() # tell the outside world
                    self._stop.set() # stop our own thread promptly
                    return
        except Exception as e:
            # Non-fatal; event polling is best-effort.
            if self.log: self.log.debug("pollNextEvent error: %s", e)

    # ---------------------- Event emission --------------------------------
    def _emit(self, evt_type: str, hand_handle, steps: int | None = None, *,
              dx: float | None = None, dy: float | None = None,
              ax: float | None = None, ay: float | None = None):
        if not self._listener:
            return
        label = self._state[hand_handle]["label"]
        evt = ControllerEvent(type=evt_type, hand=label, steps=steps,
                              dx=dx, dy=dy, ax=ax, ay=ay, when=time.time())
        try:
            self._listener(evt)
        except Exception as e:
            # Do not let user callbacks crash the backend
            if self.log: self.log.debug("Controller emit error: %s", e)

    def _poll_once(self):
        s = self._state
        for hand in (self._h_left, self._h_right):
            # --- Touchpad click family ---
            try:
                d = self._vrinput.getDigitalActionData(self._a_track_click, hand)
                if d.bActive and d.bState != s[hand]["tpad_last_click"]:
                    now = time.time()
                    if d.bState:
                        s[hand]["tpad_down"] = True; s[hand]["tpad_down_ts"] = now
                        self._emit("touchpad.press", hand)
                    else:
                        self._emit("touchpad.release", hand)
                        held = now - s[hand]["tpad_down_ts"]
                        self._emit("touchpad.long_press" if held >= cfg.LONG_PRESS_THRESHOLD else "touchpad.short_press", hand)
                        s[hand]["tpad_down"] = False; s[hand]["tpad_down_ts"] = 0.0
                    s[hand]["tpad_last_click"] = d.bState
            except Exception as e:
                if self.log: self.log.debug("touchpad click read failed: %s", e)

            # --- Touch state (NEW emits 'touch' / 'lift') ---
            try:
                t = self._vrinput.getDigitalActionData(self._a_track_touch, hand)
                if t.bActive:
                    if t.bState and not s[hand]["tpad_touching"]:
                        s[hand]["tpad_touching"] = True
                        s[hand]["tpad_just_touched"] = True
                        s[hand]["acc_x"] = 0.0; s[hand]["acc_y"] = 0.0
                        # Attach absolute position if available
                        try:
                            p0 = self._vrinput.getAnalogActionData(self._a_track_pos, hand)
                            if p0.bActive:
                                self._emit("touchpad.touch", hand, ax=float(p0.x), ay=float(p0.y))
                            else:
                                self._emit("touchpad.touch", hand)
                        except Exception:
                            self._emit("touchpad.touch", hand)
                    elif not t.bState and s[hand]["tpad_touching"]:
                        s[hand]["tpad_touching"] = False
                        s[hand]["acc_x"] = 0.0; s[hand]["acc_y"] = 0.0
                        # On lift, snap absolute to center for downstream consumers
                        self._emit("touchpad.lift", hand, ax=0.0, ay=0.0)
            except Exception as e:
                if self.log: self.log.debug("touchpad touch read failed: %s", e)

            # --- Scroll from X/Y (only when touching; if clicking, raw deltas suppressed) ---
            try:
                if s[hand]["tpad_touching"]:
                    p = self._vrinput.getAnalogActionData(self._a_track_pos, hand)
                    if p.bActive:
                        if s[hand]["tpad_just_touched"]:
                            s[hand]["last_x"] = p.x
                            s[hand]["last_y"] = p.y
                            s[hand]["tpad_just_touched"] = False
                        else:
                            if s[hand]["tpad_down"]:
                                s[hand]["last_x"] = p.x
                                s[hand]["last_y"] = p.y
                            else:
                                dx = p.x - s[hand]["last_x"]
                                dy = p.y - s[hand]["last_y"]
                                s[hand]["last_x"] = p.x
                                s[hand]["last_y"] = p.y

                                # Emit high-frequency raw deltas (x & y)
                                rdx = cfg.INVERT_HSCROLL * dx
                                rdy = cfg.INVERT_VSCROLL * dy
                                if (abs(rdx) >= cfg.RAW_SCROLL_MIN_DELTA) or (abs(rdy) >= cfg.RAW_SCROLL_MIN_DELTA):
                                    self._emit("touchpad.scroll_raw", hand, dx=rdx, dy=rdy,
                                               ax=float(p.x), ay=float(p.y))

                                if abs(dy) >= cfg.TRACKPAD_DEADZONE:
                                    s[hand]["acc_y"] += cfg.INVERT_VSCROLL * dy
                                    steps_v = int(s[hand]["acc_y"] / cfg.TRACKPAD_V_SCROLL_STEP)
                                    if steps_v:
                                        steps_v = max(min(steps_v, cfg.MAX_STEPS_PER_FRAME), -cfg.MAX_STEPS_PER_FRAME)
                                        self._emit("touchpad.vscroll", hand, steps=steps_v)
                                        s[hand]["acc_y"] -= steps_v * cfg.TRACKPAD_V_SCROLL_STEP
                                if abs(dx) >= cfg.TRACKPAD_DEADZONE:
                                    s[hand]["acc_x"] += cfg.INVERT_HSCROLL * dx
                                    steps_h = int(s[hand]["acc_x"] / cfg.TRACKPAD_H_SCROLL_STEP)
                                    if steps_h:
                                        steps_h = max(min(steps_h, cfg.MAX_STEPS_PER_FRAME), -cfg.MAX_STEPS_PER_FRAME)
                                        self._emit("touchpad.hscroll", hand, steps=steps_h)
                                        s[hand]["acc_x"] -= steps_h * cfg.TRACKPAD_H_SCROLL_STEP
            except Exception as e:
                if self.log: self.log.debug("touchpad scroll read failed: %s", e)

            # --- Thumbstick/Joystick click family ---
            try:
                j = self._vrinput.getDigitalActionData(self._a_joy_click, hand)
                if j.bActive and j.bState != s[hand]["joy_last_click"]:
                    now = time.time()
                    if j.bState:
                        s[hand]["joy_down"] = True; s[hand]["joy_down_ts"] = now
                        self._emit("thumbstick.press", hand)
                    else:
                        self._emit("thumbstick.release", hand)
                        held = now - s[hand]["joy_down_ts"]
                        self._emit("thumbstick.long_press" if held >= cfg.LONG_PRESS_THRESHOLD else "thumbstick.short_press", hand)
                        s[hand]["joy_down"] = False; s[hand]["joy_down_ts"] = 0.0
                    s[hand]["joy_last_click"] = j.bState
            except Exception as e:
                if self.log: self.log.debug("thumbstick click read failed: %s", e)
