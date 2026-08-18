"""
Index Puppet — two-axis puppet via Index touchpads (VRBridge).

Uses absolute touchpad position (evt.ax, evt.ay) provided by VRBridge
for TOUCHPAD_SCROLL_RAW samples, so the finger can start anywhere on the pad
and still register as off-center. On TOUCHPAD_LIFT, one zero sample runs through
the live filter — the eased hand-off the remote smoother continues from.

The codec and the smoothed-float/raw-bits split live in `vrbridge.quant_channel`,
of which this mapping is the first consumer; each axis address holds its own
`QuantChannel` so `single_touch_mode: "together"` mirrors one raw value into two
independent filters.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, Literal

from vrbridge.mappings.mapping_base import Mapping
from vrbridge.quant_channel import ChannelSpec, QuantChannel
from vrbridge.quant_channel import derive_bool_addrs as _derive_bool_addrs
from vrbridge.quant_channel import encode_unit as _quant_encode_unit
from vrbridge.settings import PuppetSettings, settings
from vrbridge import ControllerEventType, VRBridge

Hand = Literal["left", "right"]

# ------------------------------ Config ------------------------------------

# OSC parameters to drive
LEFT_X_ADDR  = "/avatar/parameters/IndexPuppet/Left_X"
LEFT_Y_ADDR  = "/avatar/parameters/IndexPuppet/Left_Y"
RIGHT_X_ADDR = "/avatar/parameters/IndexPuppet/Right_X"
RIGHT_Y_ADDR = "/avatar/parameters/IndexPuppet/Right_Y"

# IndexPuppet/Enable will be set to False N seconds after the last touch
TOUCH_ACTIVE_ADDR = "/avatar/parameters/IndexPuppet/Enable"

# Tuning lives in settings.PuppetSettings and is read at construction:
#   quant_level            magnitude bits for the OSCmooth-style boolean codec.
#                          0 writes only the float addresses. Above 0 also drives
#                          booleans derived from each float address name --
#                          "…/Left_X" gives "…/Left_XNegative", "…/Left_X1",
#                          "…/Left_X2", "…/Left_X4", one bit per level.
#   touch_active_idle_secs how long after the last touch Enable drops.
#   single_touch_mode      "together" mirrors a lone pad to both sides;
#                          "separate" writes only the side being touched.
#   invert_x / invert_y    -1 to flip an axis.
#   float_smooth_tau_secs  low-pass time constant for float output; <= 0 disables.
#                          Quantized booleans are always raw and immediate.

# --------------------------- Quantization ---------------------------------
# `_derive_bool_addrs` and `_quant_encode_unit` are `quant_channel`'s, imported
# under the names this module always exported: `tests/test_addresses.py` reaches
# the census through them, and the aliases keep one implementation under both.

AXIS_ADDRS = (LEFT_X_ADDR, LEFT_Y_ADDR, RIGHT_X_ADDR, RIGHT_Y_ADDR)


def quant_addr_map(n: int) -> dict:
    """Boolean address map for all four axes at quant level n."""
    return {addr: _derive_bool_addrs(addr, n) for addr in AXIS_ADDRS}


def _clamp_unit(v: float) -> float:
    return -1.0 if v < -1.0 else 1.0 if v > 1.0 else v

# ----------------------------- Mapping ------------------------------------

@dataclass
class HandState:
    active: bool = False

class IndexPuppetMapping(Mapping):
    name = "index_puppet"

    def __init__(self, bridge: VRBridge, tuning: PuppetSettings | None = None):
        super().__init__(bridge)
        self._tune = tuning if tuning is not None else settings().puppet
        self._state: Dict[Hand, HandState] = {"left": HandState(), "right": HandState()}
        self._last_contact_ts: float = 0.0
        self._touch_active: bool = False
        # One QuantChannel per address, built here from self._tune rather than at
        # def time -- a def-time default would freeze the config's values at import
        # (design.md's frozen-range failure). Per-address instances are what keep the
        # "together" mirror's two filters independent.
        self._channels: Dict[str, QuantChannel] = {
            addr: QuantChannel(ChannelSpec(
                name=addr.removeprefix("/avatar/parameters/"), address=addr,
                bits=self._tune.quant_level, signed=True,
                float_tau=self._tune.float_smooth_tau_secs))
            for addr in AXIS_ADDRS}

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
        
        x = ax * self._tune.invert_x
        y = ay * self._tune.invert_y
        return _clamp_unit(x), _clamp_unit(y)

    def _send_axis(self, ctx, base_addr: str, raw_value: float, now: float):
        """Send one axis sample: raw/immediate booleans, smoothed float -- the
        split QuantChannel.send owns."""
        self._channels[base_addr].send(ctx.send, raw_value, now)

    def _send_for_hand(self, ctx, hand: Hand, x: float, y: float, now: float):
        """Send axis values honoring single_touch_mode."""
        # Determine if we should mirror to the other hand
        other = self._other_hand(hand)
        mirror = (self._tune.single_touch_mode == "together") and \
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
        """Send zeros, mirroring when appropriate for single_touch_mode."""
        # Reuse generic logic with 0.0
        self._send_for_hand(ctx, hand, 0.0, 0.0, now)

    def _reset_all(self, ctx):
        """Reset internal state and send exact zeros (e.g., on avatar change).

        The zeros here are a hard reset -- each filter is re-seated at 0 before its
        send, so what lands on the wire is 0.0 exactly. The previous behavior eased
        the float toward 0 through live filter state, which left the incoming avatar
        holding a nonzero remnant of the outgoing avatar's last gesture; an avatar
        change is a discontinuity, not a motion to smooth. (A *lift* still eases:
        that is the same hand on the same avatar returning to rest.)
        """
        self.bridge.log.info("IndexPuppet: Resetting state due to avatar change.")

        # Reset master enable
        self._touch_active = False
        ctx.send(TOUCH_ACTIVE_ADDR, 0)

        # Reset hand states and zero outputs
        now = time.time()
        self._state = {"left": HandState(), "right": HandState()}
        for channel in self._channels.values():
            channel.reset(0.0, now)
            channel.send(ctx.send, 0.0, now)

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

    def _attach(self) -> None:
        
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
            if self._touch_active and (now - self._last_contact_ts) >= self._tune.touch_active_idle_secs:
                self.bridge.osc.send(TOUCH_ACTIVE_ADDR, 0)
                self._touch_active = False
