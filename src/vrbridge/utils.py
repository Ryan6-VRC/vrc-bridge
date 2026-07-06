from __future__ import annotations

import logging
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
        """Send a new value via ctx and update our mirror."""
        self.last = x
        ctx.send(self.addr, x)
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


def press_pulse(ctx, address: str, value, duration: float):
    """
    One-shot 'press': send value, sleep duration seconds, send 0.
    Keep tiny and synchronous; caller is a short-lived callback.
    """
    ctx.send(address, value)
    time.sleep(max(0.0, duration))
    ctx.send(address, 0)
