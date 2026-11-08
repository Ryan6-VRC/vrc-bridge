"""The face-tracking sender: ALVR's face datagram in, VRChat's native `/tracking/face/v1` blob out.

It takes VRCFaceTracking's place for a native-face-tracking avatar, so the chain is ALVR, then this
mapping, then the client. `face_blob` owns the datagram, the mapping, the filter and the byte layout;
this module owns when a blob is sent.

* **Input.** One UDP socket bound to `127.0.0.1:[face] port`, ALVR's VRCFaceTracking sink port by
  default, so ALVR's face-tracking sink stays on its VRCFaceTracking setting. ALVR sends from loopback,
  so this runs on the PC running the ALVR streamer. VRCFaceTracking binds the same port: whichever
  starts second fails, and this one says so and stays off.
* **Cadence.** Datagrams arrive at the headset's tracking-packet rate, mostly byte-identical repeats;
  the latest values are held and one blob is sent per tick at `[face] rate`. The filter steps on the
  tick, with the tick's real period, so its feel does not depend on the input rate.
* **Presence.** The source is active while a datagram carrying an eye or face segment arrived within
  `presence_timeout_secs`. Zero-length datagrams (ALVR with a headset that has no face tracking) and
  silence are both inactive. Nothing is sent while inactive, and the client zeroes every forwarded
  value 2 s after the last blob, which is the intended behaviour when the headset comes off.
* **Native only.** A blob goes out only while the worn avatar's `/avatar/avatarfullfacetracking` is
  true, VRCFaceTracking's own gate: on any other avatar the blob would take over eye gaze. The client
  streams that flag on its out-port when an avatar loads, which updates the gate at once; because a
  streamed row can be lost under inbound load, the flag is also read by name over OSCQuery every
  `gate_poll_secs` while the source is active, on an avatar change, and when the source comes back.
  A 404 reads as false (no avatar declares it, or one is loading); a failed read changes nothing. A
  read applies only if no streamed flag or avatar change landed while it was in flight, and it primes
  the stream's change filter, so the next streamed value is compared against what the gate holds.
* **Other mappings' keys.** `set_keys` writes values into the keys `pack` never fills
  (`WRITABLE_KEYS`), onto every blob after the filter, so a mapping can carry a channel of its own to
  every viewer outside the synced-parameter budget. Such a value travels only while this sender is
  sending; `index_puppet` is the consumer.
* **Toggles.** `FaceTrackingToggles` is `osc_vrcft` following this source instead of VRCFT's mDNS
  advertisement: `EyeTrackingActive` and `LipTrackingActive` go true while face data flows and false
  when it stops, after each avatar change and on each transition. A router registers it in place of
  `VRCFTMapping`, never beside it, since the two would contradict each other.
"""
from __future__ import annotations

import select
import socket
import threading
import time
from typing import Optional

from vrbridge import VRBridge
from vrbridge.face_blob import BLOB_KEYS, FaceFrame, FaceSmoother, _byte, decode, pack
from vrbridge.mappings.mapping_base import Mapping
from vrbridge.mappings.osc_vrcft import VRCFTMapping
from vrbridge.osc_manager import FETCH_NOT_FOUND
from vrbridge.settings import settings

FACE_ADDR = "/tracking/face/v1"
FULL_FACE_ADDR = "/avatar/avatarfullfacetracking"
AVATAR_CHANGE_ADDR = "/avatar/change"
LISTEN_HOST = "127.0.0.1"

# A tick that ran late (a stalled thread, a suspended PC) is filtered as if it took this long, so one
# long gap cannot hand the filter a near-1 alpha and a derivative of nothing.
_MAX_DT = 0.1
_JOIN_SECS = 1.0

#: The keys `face_blob.pack` never fills, so a value `set_keys` writes there overwrites no tracked
#: data. `tests/test_face_blob.py`'s `NEVER_WRITTEN` asserts the same set against `pack` itself.
WRITABLE_KEYS = frozenset({
    "EyeDilationRight", "EyeDilationLeft", "EyeConstrictRight", "EyeConstrictLeft",
    "NasalDilationRight", "NasalDilationLeft", "NasalConstrictRight", "NasalConstrictLeft",
    "JawBackward", "JawClench", "JawMandibleRaise", "LipSuckCornerRight", "LipSuckCornerLeft",
    "TongueUp", "TongueDown", "TongueRight", "TongueLeft", "TongueRoll", "TongueBendDown",
    "TongueCurlUp", "TongueSquish", "TongueFlat", "TongueTwistRight", "TongueTwistLeft",
    "SoftPalateClose", "ThroatSwallow", "NeckFlexRight", "NeckFlexLeft",
})
_KEY_INDEX = {k: BLOB_KEYS.index(k) for k in WRITABLE_KEYS}


class FaceSenderMapping(Mapping):
    """Receive, hold, smooth and send; the module docstring is the contract."""
    name = "osc_face"

    def __init__(self, bridge: VRBridge, tuning=None):
        super().__init__(bridge)
        self._tune = tuning if tuning is not None else settings().face
        self._lock = threading.Lock()
        self._sock: Optional[socket.socket] = None
        self._stop: Optional[threading.Event] = None
        self._threads: list[threading.Thread] = []
        self._gate_wake = threading.Event()
        # Owned by the worker thread: the held state, the last payload seen, the warned tags.
        self._frame = FaceFrame()
        self._smoother = (FaceSmoother(self._tune.min_cutoff, self._tune.beta, self._tune.d_cutoff)
                          if self._tune.smoothing else None)
        self._last_payload: Optional[bytes] = None
        self._last_had_data = False
        self._warned_tags: set[bytes] = set()
        # Written by the worker, read by the gate thread and the toggles: a monotonic time or None.
        self._last_data: Optional[float] = None
        # The worn avatar's full-face-tracking flag; None until something has said. Under _lock, with
        # the sequence a read checks so a streamed flag or an avatar change supersedes it.
        self._full_face: Optional[bool] = None
        self._gate_seq = 0
        # Written by other mappings' threads through set_keys, read by the worker: blob index -> byte.
        # Its own lock, so a key write never waits on a gate read.
        self._keys_lock = threading.Lock()
        self._keys: dict[int, int] = {}
        #: The bound port, once listening. Differs from `[face] port` only when that is 0.
        self.port: Optional[int] = None

    # ---- lifecycle -------------------------------------------------------

    def _attach(self) -> None:
        # Ungated: tracking the flag while switched off is harmless, and keeps it current for
        # reactivation.
        self.bridge.on_osc(FULL_FACE_ADDR, self._on_full_face)
        self.bridge.on_osc(AVATAR_CHANGE_ADDR, self._on_avatar_change)
        self.bridge.on_stop(self._on_stop)

    def activate(self) -> None:
        super().activate()
        self._start()

    def deactivate(self) -> None:
        super().deactivate()
        self._shutdown()

    def _start(self) -> None:
        with self._lock:
            if self._sock is not None:
                return
        port = self._tune.port
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((LISTEN_HOST, port))
        except OSError as e:
            sock.close()
            self.bridge.log.error(
                "osc_face: cannot listen on %s:%d (%s); the face sender is off. VRCFaceTracking "
                "listens on the same port, so close it, or set [face] port.", LISTEN_HOST, port, e)
            return
        stop = threading.Event()
        self._gate_wake.clear()              # the last shutdown's wake is not a request to read
        threads = [threading.Thread(target=self._run, args=(sock, stop), daemon=True,
                                    name="vrbridge-face-sender"),
                   threading.Thread(target=self._gate_loop, args=(stop,), daemon=True,
                                    name="vrbridge-face-gate")]
        with self._lock:
            self._sock, self._stop, self._threads = sock, stop, threads
        self.port = sock.getsockname()[1]
        for t in threads:
            t.start()
        self.bridge.log.info("osc_face: listening for ALVR face data on %s:%d", LISTEN_HOST, self.port)

    def _shutdown(self) -> None:
        with self._lock:
            sock, stop, threads = self._sock, self._stop, self._threads
            self._sock, self._stop, self._threads = None, None, []
        if stop is None:
            return
        stop.set()
        self._gate_wake.set()
        sock.close()
        for t in threads:
            t.join(timeout=_JOIN_SECS)
        self._last_data = None

    def _on_stop(self, ctx) -> None:
        self._shutdown()

    # ---- state other mappings read ----------------------------------------

    def source_active(self, now: Optional[float] = None) -> bool:
        """Whether face data from a tracking headset is arriving."""
        last = self._last_data
        if last is None:
            return False
        now = time.monotonic() if now is None else now
        return now - last <= self._tune.presence_timeout_secs

    def set_keys(self, values: dict[str, float]) -> None:
        """Write each value (0..1, quantized as `pack` quantizes) into its key on every blob from the
        next tick on, until set again; 0 is the same as never set. Thread-safe. Raises ValueError,
        writing nothing, if any name is outside `WRITABLE_KEYS`."""
        bad = set(values) - WRITABLE_KEYS
        if bad:
            raise ValueError(f"osc_face: {sorted(bad)} are filled from face data or are not blob "
                             f"keys; set_keys writes only WRITABLE_KEYS")
        packed = {_KEY_INDEX[k]: _byte(v) for k, v in values.items()}
        with self._keys_lock:
            self._keys.update(packed)

    # ---- the worker: receive and send --------------------------------------

    def _run(self, sock: socket.socket, stop: threading.Event) -> None:
        period = 1.0 / self._tune.rate
        last_tick = time.monotonic()
        next_tick = last_tick + period
        failed = False
        while not stop.is_set():
            try:
                readable, _, _ = select.select([sock], [], [], max(0.0, next_tick - time.monotonic()))
            except (OSError, ValueError):
                return  # the socket was closed under us: shutdown
            try:
                if readable:
                    try:
                        data = sock.recv(65535)
                    except OSError:
                        if stop.is_set():
                            return
                        continue
                    self._on_datagram(data, time.monotonic())
                now = time.monotonic()
                if now >= next_tick:
                    dt = min(now - last_tick, _MAX_DT)
                    last_tick = now
                    next_tick += period
                    if next_tick <= now:
                        next_tick = now + period
                    self._tick(dt, now)
                failed = False
            except Exception:
                # Keep the sender alive through one bad datagram or send, and log once per run of
                # failures rather than at the datagram rate.
                if not failed:
                    self.bridge.log.exception("osc_face: error in the face sender; it keeps running")
                failed = True

    def _on_datagram(self, data: bytes, now: float) -> None:
        if data == self._last_payload:
            if self._last_had_data:
                returning = not self.source_active(now)
                self._last_data = now
                if returning:
                    self._gate_wake.set()
            return
        self._last_payload = data
        dgram = decode(data)
        if dgram.unknown is not None and dgram.unknown not in self._warned_tags:
            self._warned_tags.add(dgram.unknown)
            self.bridge.log.warning(
                "osc_face: a datagram carries segment %r, which this sender does not read; the "
                "rest of each such datagram is skipped. Logged once per tag.", dgram.unknown)
        self._last_had_data = dgram.has_data
        if dgram.has_data:
            returning = not self.source_active(now)
            self._frame.apply(dgram)
            self._last_data = now
            if returning:
                self._gate_wake.set()

    def _tick(self, dt: float, now: float) -> None:
        if not self.source_active(now):
            return
        frame = self._smoother.step(self._frame, dt) if self._smoother else self._frame
        if self._full_face is True and self.enabled:
            blob = bytearray(pack(frame))
            with self._keys_lock:
                for i, v in self._keys.items():
                    blob[i] = v
            self.bridge.osc.send(FACE_ADDR, bytes(blob))

    # ---- the gate: is the worn avatar native? -------------------------------

    def _on_full_face(self, ctx, address: str, value) -> None:
        self._set_full_face(bool(value))

    def _on_avatar_change(self, ctx, address: str, value) -> None:
        with self._lock:
            self._gate_seq += 1
        self._gate_wake.set()

    def _gate_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            woke = self._gate_wake.wait(self._tune.gate_poll_secs)
            self._gate_wake.clear()
            if stop.is_set():
                return
            if not (woke or self.source_active()):
                continue
            with self._lock:
                seq = self._gate_seq
            res = self.bridge.osc.fetch(FULL_FACE_ADDR)
            if res.ok:
                self._set_full_face(bool(res.value), seq)
            elif res.reason == FETCH_NOT_FOUND:
                self._set_full_face(False, seq)

    def _set_full_face(self, value: bool, read_seq: Optional[int] = None) -> None:
        """Apply the flag. A streamed value (`read_seq` None) supersedes any read in flight; a read
        applies only if nothing superseded it, and primes the change filter with what it read."""
        with self._lock:
            if read_seq is None:
                self._gate_seq += 1
            elif read_seq != self._gate_seq:
                return
            else:
                self.bridge.osc.prime(FULL_FACE_ADDR, value)
            changed, self._full_face = self._full_face != value, value
        if changed:
            self.bridge.log.info(
                "osc_face: the worn avatar %s native face tracking; %s the blob.",
                "has" if value else "does not have", "sending" if value else "not sending")


class FaceTrackingToggles(VRCFTMapping):
    """`EyeTrackingActive`/`LipTrackingActive` from whether face data flows; see the module docstring."""
    name = "osc_face_toggles"
    source_label = "ALVR face data"

    def __init__(self, bridge: VRBridge, sender: FaceSenderMapping, tuning=None):
        super().__init__(bridge, tuning)
        self._sender = sender
        # The first-look rule exists for VRCFT's mDNS discovery latency, which this source does not
        # have: ALVR may already be streaming when the router starts, and a first look that only
        # recorded True would never send it. Starting from False makes that first look a transition.
        self._last_running = False

    def _source_running(self) -> bool:
        return self._sender.source_active()
