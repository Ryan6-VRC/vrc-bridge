from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import VRBridge


def setup_logging(name: str = "vrbridge", level: int = logging.INFO) -> logging.Logger:
    """Create a module-level logger with a simple format if one doesn't exist yet."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        h = logging.StreamHandler()
        fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s")
        h.setFormatter(fmt)
        logger.addHandler(h)
    logger.propagate = False
    logger.setLevel(level)
    return logger


def clamp(x: float, vmin: float = -1.0, vmax: float = 1.0) -> float:
    """Clamp x to [vmin, vmax]."""
    return vmin if x < vmin else vmax if x > vmax else x


def clamp01(x: float) -> float:
    """Clamp x to [0, 1]."""
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


@dataclass
class ParamState:
    """
    Track a single parameter value mirrored over OSC (numeric or bool).

    Quality-of-life:
      - Optional `bridge` self-registers an OSC listener for `addr`, so you
        don't have to manually wire `bridge.on_osc(addr, ...)` at each call site.
    """
    addr: str
    default: float = 0.0
    bridge: "VRBridge | None" = None
    last: float = field(init=False, default=0.0)

    def __post_init__(self):
        self.last = self.default
        if self.bridge is not None:
            self._register_ingest_listener(self.bridge)

    # ---------------- Public API ----------------

    def get(self, ctx=None) -> float:
        """Return the freshest known value. Returns default if current is None."""
        return self.last if self.last is not None else self.default

    def set(self, ctx, x) -> float:
        """Send a new value via ctx and update our mirror if the send was accepted.

        The mirror used to advance first and ignore the result. OSCManager.send
        returns False when no VRChat target has been discovered yet, so scrolling
        before VRChat appears walked the mirror up a ladder that was never
        transmitted -- and the first successful send then jumped the camera to
        wherever that walk had reached. Nothing said so.

        Only an explicit False counts as a refusal, so a ctx whose send() returns
        None (the common case for a test double) still advances the mirror.
        """
        if ctx.send(self.addr, x) is False:
            return self.last
        self.last = x
        return self.last

    def ingest(self, value: Any):
        """Update local mirror based on an external OSC update (no send)."""
        # If we receive None (empty OSC arg), we ignore it to preserve state,
        # or you could map it to self.default. Ignoring is usually safer for glitches.
        if value is not None:
            self.last = value

    def reset(self, ctx=None, *, send: bool = False) -> float:
        """
        Restore `last` to `default`. If send=True (and ctx is provided), also transmit.
        Returns the new `last`.
        """
        self.last = self.default
        if send and ctx is not None:
            ctx.send(self.addr, self.last)
        return self.last

    # ---------------- Internals ----------------

    def _register_ingest_listener(self, bridge: "VRBridge"):
        """Attach a single on-OSC listener that feeds into ingest()."""
        def _on_osc(ctx, address, value):
            self.ingest(value)
        bridge.on_osc(self.addr, _on_osc)


def nearest_index(values: list[float], x: float) -> int:
    """Return index of the element in values closest to x."""
    return min(range(len(values)), key=lambda i: abs(values[i] - x)) if values else 0


def step_param(ctx, state: ParamState, steps_x: list[float], delta_steps: int):
    """Move a parameter along a discretized ladder by delta_steps."""
    if not steps_x or delta_steps == 0:
        return
    cur = state.get(ctx)
    idx = nearest_index(steps_x, cur)
    idx = max(0, min(len(steps_x) - 1, idx + delta_steps))
    state.set(ctx, float(steps_x[idx]))


class SmoothScroller:
    """
    Convert high-rate raw dy samples into smooth param deltas with a 'sticky' start.

    Sticky phase: accumulate |dy| until threshold reached, then begin emitting deltas
    from subsequent samples. Reset if samples stop for a while, or when explicitly told.

    Usage:
        ss = SmoothScroller(sensitivity=..., max_delta=..., sticky_abs=..., reset_gap=...)
        delta = ss.on_sample(dy, when=time.time())
        if reset_needed: ss.reset()
    """
    def __init__(self, *, sensitivity: float, max_delta: float,
                 sticky_abs: float, reset_gap: float):
        self.sensitivity = sensitivity
        self.max_delta = max_delta
        self.sticky_abs = sticky_abs
        self.reset_gap = reset_gap
        self._unstuck = False
        self._accum_abs = 0.0
        self._last_when = 0.0

    def reset(self):
        self._unstuck = False
        self._accum_abs = 0.0
        self._last_when = 0.0

    def on_sample(self, dy: float, when: float | None = None) -> float:
        now = when or time.time()

        # Lift-like gap -> re-sticky
        if self._last_when > 0.0 and (now - self._last_when) > self.reset_gap:
            self._unstuck = False
            self._accum_abs = 0.0

        if not self._unstuck:
            self._accum_abs += abs(dy or 0.0)
            self._last_when = now
            if self._accum_abs >= self.sticky_abs:
                self._unstuck = True
            return 0.0

        # Emit bounded delta once unstuck
        delta = dy * self.sensitivity
        if delta > self.max_delta:
            delta = self.max_delta
        elif delta < -self.max_delta:
            delta = -self.max_delta
        self._last_when = now
        return delta


class _PulseWorker:
    """Runs one-shot pulses on one background thread, strictly in order.

    Two problems, one mechanism.

    *Blocking.* press_pulse used to sleep inline. The design record rules that a
    blocking sleep in an OSC callback is harmless, and it is -- OSC dispatch is
    per-datagram threaded. But every caller of press_pulse is a *controller*
    callback, and those run synchronously on the single ControllerLoop thread, so
    the sleep froze input polling. A two-step aperture scroll cost 0.2s; a
    diagonal left-pad drag fires the vertical and horizontal handlers from the
    same sample and cost 0.4s -- exactly long_press_threshold, so a tap arriving
    just after could be observed late and reclassified as a long press, firing the
    wrong VRCLens feature.

    *Coalescing.* A train of N pulses sent back to back put the trailing 0 and the
    next value in the same VRChat frame (~11ms at 90fps), so an edge-triggered
    consumer saw one transition instead of N. A gap after each pulse separates them.

    Serializing on one thread also settles the residual the design record notes
    for a chatter-prone latch: two flips inside one pulse duration used to
    interleave as 1,1,0,0 rather than 1,0,1,0.
    """

    def __init__(self):
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._log = logging.getLogger("vrbridge")

    def submit(self, ctx, address: str, value, duration: float, gap: float) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="OSCPulse", daemon=True)
                self._thread.start()
        self._q.put((ctx, address, value, duration, gap))

    def _run(self) -> None:
        while True:
            ctx, address, value, duration, gap = self._q.get()
            try:
                ctx.send(address, value)
                time.sleep(duration)
                ctx.send(address, 0)
                if gap:
                    time.sleep(gap)
            except Exception:
                # Off the caller's thread, so nothing else would ever surface this.
                self._log.exception("Pulse on %s failed", address)
            finally:
                self._q.task_done()

    def drain(self, timeout: float = 5.0) -> bool:
        """Block until every queued pulse has finished. For tests and shutdown."""
        deadline = time.monotonic() + timeout
        while not self._q.empty():
            if time.monotonic() > deadline:
                return False
            time.sleep(0.005)
        self._q.join()
        return True


_pulses = _PulseWorker()


def press_pulse(ctx, address: str, value, duration: float, *, gap: float | None = None):
    """Queue a one-shot 'press': send value, hold for duration, send 0.

    Returns immediately. The hold happens on a shared worker thread, so a
    controller callback can fire one without stalling input polling.

    `gap` is the quiet time enforced after the trailing 0 before the next pulse
    runs; it defaults to `duration`. It exists so a train of pulses on one
    address reads as N distinct transitions rather than one -- both values need
    to be longer than a VRChat frame.
    """
    duration = max(0.0, duration)
    _pulses.submit(ctx, address, value, duration, duration if gap is None else max(0.0, gap))
