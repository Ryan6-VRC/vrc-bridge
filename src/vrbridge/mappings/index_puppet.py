"""
Index Puppet — two-axis puppet via Index touchpads (VRBridge).

Uses absolute touchpad position (evt.ax, evt.ay) provided by VRBridge
for TOUCHPAD_SCROLL_RAW samples, so the finger can start anywhere on the pad
and still register as off-center. On TOUCHPAD_LIFT, values ease back to (0, 0).

Supports OSCmooth-style boolean quantization alongside the original
float parameters.

Requires the vrbridge project to be importable.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Dict, Literal, Tuple

from vrbridge.mappings.mapping_base import Mapping
from vrbridge import ControllerEventType, VRBridge

Hand = Literal["left", "right"]

# ------------------------------ Config ------------------------------------

# OSC parameters to drive
LEFT_X_ADDR  = "/avatar/parameters/IndexPuppet/Left_X"
LEFT_Y_ADDR  = "/avatar/parameters/IndexPuppet/Left_Y"
RIGHT_X_ADDR = "/avatar/parameters/IndexPuppet/Right_X"
RIGHT_Y_ADDR = "/avatar/parameters/IndexPuppet/Right_Y"

# Quantization of floats to additional boolean addresses (OSCmooth-style).
# If QUANT_LEVEL == 0, only the original float addresses are written.
# If QUANT_LEVEL > 0, we also drive boolean parameters dynamically derived
# from the float address names:
#   e.g. "/avatar/parameters/IndexPuppet/Left_X" ->
#        "/avatar/parameters/IndexPuppet/Left_XNegative",
#        "/avatar/parameters/IndexPuppet/Left_X1",
#        "/avatar/parameters/IndexPuppet/Left_X2",
#        "/avatar/parameters/IndexPuppet/Left_X4", ...
# The number of magnitude bits equals QUANT_LEVEL.
QUANT_LEVEL: int = 3

# IndexPuppet/Enable will be set to False N seconds after the last touch
TOUCH_ACTIVE_ADDR = "/avatar/parameters/IndexPuppet/Enable"
TOUCH_ACTIVE_IDLE_SECS: float = 0.5

# Single-touch behavior:
# - "separate": (default) if only one touchpad is used, write only that side.
# - "together": when only a single touchpad is used, mirror its values to both
#               left and right addresses; when both pads are used, each writes
#               to its own addresses as normal.
SINGLE_TOUCH_MODE: Literal["together","separate"] = "together"

# Optional axis inversion
INVERT_X: int = 1     # set to -1 to invert X
INVERT_Y: int = 1     # set to -1 to invert Y

# Float-only smoothing:
# - Quantized booleans remain raw/immediate.
# - Float outputs are low-pass filtered with a time constant (seconds).
# Set <= 0.0 to disable smoothing.
FLOAT_SMOOTH_TAU_SECS: float = 0.12

# --------------------------- Quantization ---------------------------------

def _derive_bool_addrs(base_addr: str, n: int) -> dict:
    """Given a float address like '/.../Left_X', return boolean addr mapping."""
    if n <= 0:
        return {'neg': None, 'bits': []}
    root, name = base_addr.rsplit('/', 1)
    root = root + '/'
    return {
        'neg': f"{root}{name}Negative",
        'bits': [f"{root}{name}{1<<i}" for i in range(n)]
    }

def _quant_encode_unit(x: float, n: int) -> Tuple[bool, int]:
    """Encode x in [-1,1] as (negative_flag, integer code k in [0, 2^n-1])."""
    if n <= 0:
        return (False, 0)
    # Clamp to [-1,1]
    if x < -1.0: x = -1.0
    if x >  1.0: x =  1.0
    D = (1 << n) - 1
    k = int(round(abs(x) * D))
    neg = (x < 0.0) and (k > 0)  # avoid a negative zero
    return neg, k

# Precompute boolean address maps for each axis (empty if disabled)
_QUANT_ADDRS = {
    LEFT_X_ADDR:  _derive_bool_addrs(LEFT_X_ADDR,  QUANT_LEVEL),
    LEFT_Y_ADDR:  _derive_bool_addrs(LEFT_Y_ADDR,  QUANT_LEVEL),
    RIGHT_X_ADDR: _derive_bool_addrs(RIGHT_X_ADDR, QUANT_LEVEL),
    RIGHT_Y_ADDR: _derive_bool_addrs(RIGHT_Y_ADDR, QUANT_LEVEL),
}

def _send_axis_float(ctx, base_addr: str, value: float):
    """Send only the float axis value."""
    ctx.send(base_addr, value)

def _send_axis_bits(ctx, base_addr: str, value: float):
    """Send only quantized booleans derived from the provided raw value."""
    n = QUANT_LEVEL
    if n <= 0:
        return
    addrs = _QUANT_ADDRS.get(base_addr)
    if not addrs or not addrs["bits"]:
        return
    neg, k = _quant_encode_unit(value, n)
    ctx.send(addrs["neg"], 1 if neg else 0)
    for i, addr in enumerate(addrs["bits"]):
        ctx.send(addr, (k >> i) & 1)

def _clamp_unit(v: float) -> float:
    return -1.0 if v < -1.0 else 1.0 if v > 1.0 else v


@dataclass
class AxisFilterState:
    value: float = 0.0
    last_ts: float = 0.0
    initialized: bool = False


class FloatAxisSmoother:
    """Per-address first-order low-pass smoothing for float axis outputs."""

    def __init__(self, tau_secs: float):
        self.tau_secs = max(0.0, float(tau_secs))
        self._state: Dict[str, AxisFilterState] = {}

    def reset_axis(self, addr: str, value: float, now: float) -> float:
        st = self._state.setdefault(addr, AxisFilterState())
        st.value = _clamp_unit(value)
        st.last_ts = now
        st.initialized = True
        return st.value

    def filter(self, addr: str, target: float, now: float) -> float:
        target = _clamp_unit(target)
        if self.tau_secs <= 0.0:
            return self.reset_axis(addr, target, now)

        st = self._state.setdefault(addr, AxisFilterState())
        if not st.initialized:
            return self.reset_axis(addr, target, now)

        dt = now - st.last_ts
        alpha = 1.0 if dt <= 0.0 else (1.0 - math.exp(-dt / self.tau_secs))
        st.value = _clamp_unit(st.value + (target - st.value) * alpha)
        st.last_ts = now
        return st.value

# ----------------------------- Mapping ------------------------------------

@dataclass
class HandState:
    active: bool = False

class IndexPuppetMapping(Mapping):
    name = "index_puppet"

    def __init__(self, bridge: VRBridge):
        super().__init__(bridge)
        self._state: Dict[Hand, HandState] = {"left": HandState(), "right": HandState()}
        self._last_contact_ts: float = 0.0
        self._touch_active: bool = False
        self._float_smoother = FloatAxisSmoother(FLOAT_SMOOTH_TAU_SECS)

    # -- helpers --

    @staticmethod
    def _other_hand(h: Hand) -> Hand:
        return "right" if h == "left" else "left"

    @staticmethod
    def _event_ts(evt) -> float:
        when = getattr(evt, "when", None)
        return float(when) if when is not None else time.time()

    def _apply_inversion(self, ax: float, ay: float, st: HandState) -> tuple[float, float]:
        """Compute puppet X/Y given absolute pad position & inversion."""
        if not st.active:
            return 0.0, 0.0
        
        x = ax * INVERT_X
        y = ay * INVERT_Y
        return _clamp_unit(x), _clamp_unit(y)

    def _send_axis(self, ctx, base_addr: str, raw_value: float, now: float):
        """
        Send axis with split paths:
        - booleans: raw/immediate
        - float: always smoothed
        """
        _send_axis_bits(ctx, base_addr, raw_value)
        float_value = self._float_smoother.filter(base_addr, raw_value, now)
        _send_axis_float(ctx, base_addr, float_value)

    def _send_for_hand(self, ctx, hand: Hand, x: float, y: float, now: float):
        """Send axis values honoring SINGLE_TOUCH_MODE."""
        # Determine if we should mirror to the other hand
        other = self._other_hand(hand)
        mirror = (SINGLE_TOUCH_MODE == "together") and \
                 self._state[hand].active and \
                 (not self._state[other].active)

        # Define address targets
        targets = []
        if hand == "left":
            targets.append((LEFT_X_ADDR, LEFT_Y_ADDR))
            if mirror:
                targets.append((RIGHT_X_ADDR, RIGHT_Y_ADDR))
        else:
            targets.append((RIGHT_X_ADDR, RIGHT_Y_ADDR))
            if mirror:
                targets.append((LEFT_X_ADDR, LEFT_Y_ADDR))

        for addr_x, addr_y in targets:
            self._send_axis(ctx, addr_x, x, now)
            self._send_axis(ctx, addr_y, y, now)

    def _send_zero_for_hand(self, ctx, hand: Hand, now: float):
        """Send zeros, mirroring when appropriate for SINGLE_TOUCH_MODE."""
        # Reuse generic logic with 0.0
        self._send_for_hand(ctx, hand, 0.0, 0.0, now)

    def _reset_all(self, ctx):
        """Reset internal state and send zero OSC values (e.g., on avatar change)."""
        self.bridge.log.info("IndexPuppet: Resetting state due to avatar change.")
        
        # Reset master enable
        self._touch_active = False
        ctx.send(TOUCH_ACTIVE_ADDR, 0)
        
        # Reset hand states and zero outputs
        now = time.time()
        for hand in ("left", "right"):
            self._state[hand] = HandState() # Reset active flags
            # We explicitly zero specific addresses to ensure no stale values remain
            if hand == "left":
                self._send_axis(ctx, LEFT_X_ADDR, 0.0, now)
                self._send_axis(ctx, LEFT_Y_ADDR, 0.0, now)
            else:
                self._send_axis(ctx, RIGHT_X_ADDR, 0.0, now)
                self._send_axis(ctx, RIGHT_Y_ADDR, 0.0, now)

    # -- controller handlers --

    def _on_touch(self, hand: Hand):
        def inner(ctx, evt):
            st = self._state[hand]
            st.active = True

            # Touch activity bookkeeping
            self._last_contact_ts = time.time()
            if not self._touch_active:
                self._touch_active = True
                ctx.send(TOUCH_ACTIVE_ADDR, 1)

            # If absolute provided on TOUCH, send first sample immediately
            if evt.ax is not None and evt.ay is not None:
                x, y = self._apply_inversion(evt.ax, evt.ay, st)
                self._send_for_hand(ctx, hand, x, y, now=self._event_ts(evt))
        return self._gate(inner)

    def _on_raw(self, hand: Hand):
        def inner(ctx, evt):
            st = self._state[hand]
            
            # Update last contact time while still touching
            self._last_contact_ts = time.time()
            
            # We rely on VRBridge to provide floats here; if None, we let it fail (defensive 'or 0.0' removed)
            x, y = self._apply_inversion(evt.ax, evt.ay, st)
            self._send_for_hand(ctx, hand, x, y, now=self._event_ts(evt))
        return self._gate(inner)

    def _on_lift(self, hand: Hand):
        def inner(ctx, evt):
            st = self._state[hand]
            
            # Send zeros before marking state as inactive
            self._send_zero_for_hand(ctx, hand, now=self._event_ts(evt))
            st.active = False
            
            # If both hands are now lifted, update the idle timestamp
            if not (self._state["left"].active or self._state["right"].active):
                self._last_contact_ts = time.time()
        return self._gate(inner)
    
    def _on_avatar_change(self, ctx, address: str, value: str):
        """Handle avatar changes by clearing all state."""
        self._reset_all(ctx)

    # -- lifecycle --

    def register(self) -> None:
        super().register()
        
        # Listen for Avatar Changes
        self.bridge.on_osc("/avatar/change", self._on_avatar_change)

        # Controller Inputs
        self.bridge.on_controller(ControllerEventType.TOUCHPAD_TOUCH,      hand="left",  callback=self._on_touch("left"))
        self.bridge.on_controller(ControllerEventType.TOUCHPAD_SCROLL_RAW, hand="left",  callback=self._on_raw("left"))
        self.bridge.on_controller(ControllerEventType.TOUCHPAD_LIFT,       hand="left",  callback=self._on_lift("left"))

        self.bridge.on_controller(ControllerEventType.TOUCHPAD_TOUCH,      hand="right", callback=self._on_touch("right"))
        self.bridge.on_controller(ControllerEventType.TOUCHPAD_SCROLL_RAW, hand="right", callback=self._on_raw("right"))
        self.bridge.on_controller(ControllerEventType.TOUCHPAD_LIFT,       hand="right", callback=self._on_lift("right"))

    def update(self, now: float) -> None:
        # Clear the flag after N seconds of inactivity
        if not (self._state["left"].active or self._state["right"].active):
            if self._touch_active and (now - self._last_contact_ts) >= TOUCH_ACTIVE_IDLE_SECS:
                self.bridge.osc.send(TOUCH_ACTIVE_ADDR, 0)
                self._touch_active = False
