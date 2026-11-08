"""
Index Puppet — two-axis puppet via Index touchpads (VRBridge).

Uses absolute touchpad position (evt.ax, evt.ay) provided by VRBridge
for TOUCHPAD_SCROLL_RAW samples, so the finger can start anywhere on the pad
and still register as off-center. On TOUCHPAD_LIFT, one zero sample per axis
goes to the output, which eases it to rest.

**Input, then one output.** The input half turns pad events into per-axis raw
samples (after the `single_touch_mode` mirror and inversion) and an "in use" flag
that drops `touch_active_idle_secs` after the last contact. It knows no addresses
and no keys: it talks to exactly one output, built at construction, through
`sample(axis, value, now)`, `active(flag)`, `tick(now)` and `reset()`.

* **`QuantOutput`** is the quant-channel wire (README §Quant channels): one
  `QuantChannel` per `IndexPuppet/*` axis address, each sample sent at event time,
  and `IndexPuppet/Enable` as the "in use" flag. Remote smoothing is the avatar's.
* **`FaceOutput`** writes the puppet into the face sender's blob through its feed
  (`osc_face`'s `set_keys`/`clear_keys`), on the keys the `[puppet] face_*` settings
  name. It smooths each axis in the bridge, on the router's tick, and releases the
  keys once the puppet is at rest so a tracked key goes back to the tracker.

Which one is the operator's config: any `face_*` key bound selects the face feed,
every one empty selects quant. Neither rides beside the other, and the face
output never falls back to quant: with keys bound but no sender it logs once and
sends nothing, since a silent fallback would hide the misconfiguration.

**One lock.** Touch handlers run on `ControllerLoop`, the avatar change and the
router's (de)activation on OSC threads, and `update` on the router loop, which
ticks a mapping whether or not it is enabled. Each takes the puppet lock and
re-checks `enabled` inside it, and `deactivate` clears `enabled` and resets
inside it. Without that, a tick or a handler already past the gate would write
the puppet back onto the wire just after a reset cleared it.

The codec and the smoothed-float/raw-bits split live in `vrbridge.quant_channel`,
of which `QuantOutput` is the first consumer; each axis address holds its own
`QuantChannel` so `single_touch_mode: "together"` mirrors one raw value into two
independent filters.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Dict, Literal, Optional

from vrbridge.face_blob import _byte
from vrbridge.mappings.mapping_base import Mapping
from vrbridge.quant_channel import ChannelSpec, QuantChannel
from vrbridge.quant_channel import derive_bool_addrs as _derive_bool_addrs
from vrbridge.quant_channel import encode_unit as _quant_encode_unit
from vrbridge.settings import PUPPET_AXES, PuppetSettings, settings
from vrbridge import ControllerEventType, VRBridge

if TYPE_CHECKING:
    from vrbridge.mappings.osc_face import FaceSenderMapping

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
#   face_*                 the face-feed keys; any one set selects FaceOutput.

# --------------------------- Quantization ---------------------------------
# `_derive_bool_addrs` and `_quant_encode_unit` are `quant_channel`'s, imported
# under the names this module always exported: `tests/test_addresses.py` reaches
# the census through them, and the aliases keep one implementation under both.

AXIS_ADDRS = (LEFT_X_ADDR, LEFT_Y_ADDR, RIGHT_X_ADDR, RIGHT_Y_ADDR)

# The input's axis names, which are also the `face_*` settings' (left_x, ...).
AXES = PUPPET_AXES
_HAND_AXES: Dict[Hand, tuple] = {"left": ("left_x", "left_y"), "right": ("right_x", "right_y")}


def quant_addr_map(n: int) -> dict:
    """Boolean address map for all four axes at quant level n."""
    return {addr: _derive_bool_addrs(addr, n) for addr in AXIS_ADDRS}


def _clamp_unit(v: float) -> float:
    return -1.0 if v < -1.0 else 1.0 if v > 1.0 else v

# ----------------------------- Outputs ------------------------------------
# Called only under the mapping's lock, so neither output locks for itself.

class QuantOutput:
    """The quant wire, as this mapping has always sent it: each sample at event time
    through its axis address's `QuantChannel`, and `IndexPuppet/Enable` as the flag.
    `tests/test_index_puppet.py` holds it to a wire recorded before the split."""

    _ADDR = dict(zip(AXES, AXIS_ADDRS))

    def __init__(self, tune: PuppetSettings, send: Callable[[str, object], object]):
        self._send = send
        # One QuantChannel per address, built here from tune rather than at def
        # time -- a def-time default would freeze the config's values at import
        # (design.md's frozen-range failure). Per-address instances are what keep the
        # "together" mirror's two filters independent.
        self._channels: Dict[str, QuantChannel] = {
            addr: QuantChannel(ChannelSpec(
                name=addr.removeprefix("/avatar/parameters/"), address=addr,
                bits=tune.quant_level, signed=True,
                float_tau=tune.float_smooth_tau_secs))
            for addr in AXIS_ADDRS}

    def sample(self, axis: str, value: float, now: float) -> None:
        """Raw/immediate booleans, smoothed float -- the split QuantChannel.send owns."""
        self._channels[self._ADDR[axis]].send(self._send, value, now)

    def active(self, flag: bool) -> None:
        self._send(TOUCH_ACTIVE_ADDR, 1 if flag else 0)

    def tick(self, now: float) -> None:
        return

    def reset(self) -> None:
        """Drop Enable and send exact zeros.

        The zeros here are a hard reset -- each filter is re-seated at 0 before its
        send, so what lands on the wire is 0.0 exactly. Easing the float toward 0
        through live filter state left the incoming avatar holding a nonzero remnant
        of the outgoing avatar's last gesture; an avatar change is a discontinuity,
        not a motion to smooth. (A *lift* still eases: that is the same hand on the
        same avatar returning to rest.)
        """
        self._send(TOUCH_ACTIVE_ADDR, 0)
        now = time.time()
        for channel in self._channels.values():
            channel.reset(0.0, now)
            channel.send(self._send, 0.0, now)


class FaceOutput:
    """The puppet on the face feed. `sample` and `active` only set targets; `tick`
    steps one low-pass per axis and writes the bound keys.

    The filter is `QuantChannel._filter`'s exponential at `float_smooth_tau_secs`,
    without the bit encoder, stepped on the router's clock. A signed axis splits onto
    its two keys, positive into `_pos` and the magnitude below zero into `_neg`, the
    other at 0, so silence and the client's 2 s timeout both read as rest. After a
    lift the axis eases to rest over the following ticks with no special case at the
    bottom: `_byte` truncates, so the filter reaches byte 0 on its own.

    Once nothing is in use and every key it writes packs to byte 0 it clears the
    keys, once, and stops writing until the next touch. Holding them at 0 would hold
    a tracked key at 0 for as long as the bridge runs.
    """

    def __init__(self, tune: PuppetSettings, face: "FaceSenderMapping"):
        self._face = face
        self._tau = tune.float_smooth_tau_secs
        self._active_key = tune.face_active
        self._axis_keys = {a: (getattr(tune, f"face_{a}_pos"), getattr(tune, f"face_{a}_neg"))
                           for a in AXES if getattr(tune, f"face_{a}_pos")}
        self._bound = list(tune.face_bindings().values())
        self._seat()

    def _seat(self) -> None:
        self._target = dict.fromkeys(AXES, 0.0)
        self._value = dict.fromkeys(AXES, 0.0)
        self._last_ts: Optional[float] = None
        self._in_use = False
        self._writing = False

    def sample(self, axis: str, value: float, now: float) -> None:
        self._target[axis] = _clamp_unit(value)

    def active(self, flag: bool) -> None:
        self._in_use = flag

    def tick(self, now: float) -> None:
        # The first tick after a seat only stamps the clock: the filters start from 0.
        dt = 0.0 if self._last_ts is None else max(0.0, now - self._last_ts)
        self._last_ts = now
        if self._tau <= 0.0:
            self._value = dict(self._target)             # unsmoothed, as QuantChannel passes through
        else:
            alpha = 1.0 - math.exp(-dt / self._tau)
            for a in AXES:
                self._value[a] += (self._target[a] - self._value[a]) * alpha

        values: Dict[str, float] = {}
        for a, (pos, neg) in self._axis_keys.items():
            v = self._value[a]
            values[pos], values[neg] = (v, 0.0) if v > 0.0 else (0.0, -v)
        if self._active_key:
            values[self._active_key] = 1.0 if self._in_use else 0.0

        if not self._in_use and all(_byte(v) == 0 for v in values.values()):
            if self._writing:
                self._face.clear_keys(self._bound)
                self._writing = False
            return
        self._face.set_keys(values)
        self._writing = True

    def reset(self) -> None:
        """Seat every filter at 0, drop "in use", and give the keys back."""
        self._seat()
        self._face.clear_keys(self._bound)


class _NoOutput:
    """Face keys bound with no sender: the puppet sends nothing (logged at construction)."""
    def sample(self, axis: str, value: float, now: float) -> None: return
    def active(self, flag: bool) -> None: return
    def tick(self, now: float) -> None: return
    def reset(self) -> None: return

# ----------------------------- Mapping ------------------------------------

@dataclass
class HandState:
    active: bool = False

class IndexPuppetMapping(Mapping):
    name = "index_puppet"

    def __init__(self, bridge: VRBridge, tuning: PuppetSettings | None = None,
                 face: "FaceSenderMapping | None" = None):
        super().__init__(bridge)
        self._tune = tuning if tuning is not None else settings().puppet
        self._lock = threading.Lock()
        self._state: Dict[Hand, HandState] = {"left": HandState(), "right": HandState()}
        self._last_contact_ts: float = 0.0
        self._touch_active: bool = False
        self._out = self._build_output(face)

    def _build_output(self, face):
        if not self._tune.face_bindings():
            return QuantOutput(self._tune, self.bridge.osc.send)
        if face is not None:
            return FaceOutput(self._tune, face)
        reason = ("[face] enabled is false" if not settings().face.enabled else
                  "the face sender is not listening, which it logged (VRCFaceTracking holding its port?)")
        self.bridge.log.error(
            "IndexPuppet: [puppet] face_* keys are bound, which selects the face feed, but there is "
            "no face sender: %s. The puppet sends nothing; it does not fall back to quant. Fix "
            "[face], or clear every face_* key to use quant.", reason)
        return _NoOutput()

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

    def _send_for_hand(self, hand: Hand, x: float, y: float, now: float):
        """Hand axis values to the output, honoring single_touch_mode."""
        # Determine if we should mirror to the other hand
        other = self._other_hand(hand)
        mirror = (self._tune.single_touch_mode == "together") and \
                 self._state[hand].active and \
                 (not self._state[other].active)

        targets = [_HAND_AXES[hand]]
        if mirror:
            targets.append(_HAND_AXES[other])

        for axis_x, axis_y in targets:
            self._out.sample(axis_x, x, now)
            self._out.sample(axis_y, y, now)

    def _send_zero_for_hand(self, hand: Hand, now: float):
        """Send zeros, mirroring when appropriate for single_touch_mode."""
        # Reuse generic logic with 0.0
        self._send_for_hand(hand, 0.0, 0.0, now)

    def _reset(self) -> None:
        """Clear the input's state and reset the output. Caller holds the lock."""
        self._touch_active = False
        self._state = {"left": HandState(), "right": HandState()}
        self._out.reset()

    def _locked(self, fn: Callable):
        """`_gate`, with `enabled` re-checked under the puppet lock: a handler that
        passed the gate just before `deactivate` must not run after its reset."""
        def wrapped(*args):
            with self._lock:
                if self.enabled:
                    fn(*args)
        return self._gate(wrapped)

    # -- controller handlers --

    def _on_touch(self, hand: Hand):
        def inner(ctx, evt):
            st = self._state[hand]
            st.active = True

            # Touch activity bookkeeping
            self._last_contact_ts = time.time()
            if not self._touch_active:
                self._touch_active = True
                self._out.active(True)

            # If absolute provided on TOUCH, send first sample immediately
            if evt.ax is not None and evt.ay is not None:
                x, y = self._apply_inversion(evt.ax, evt.ay, st)
                self._send_for_hand(hand, x, y, now=self._event_ts(evt))
        return self._locked(inner)

    def _on_raw(self, hand: Hand):
        def inner(ctx, evt):
            st = self._state[hand]

            # Update last contact time while still touching
            self._last_contact_ts = time.time()

            # We rely on VRBridge to provide floats here; if None, we let it fail (defensive 'or 0.0' removed)
            x, y = self._apply_inversion(evt.ax, evt.ay, st)
            self._send_for_hand(hand, x, y, now=self._event_ts(evt))
        return self._locked(inner)

    def _on_lift(self, hand: Hand):
        def inner(ctx, evt):
            st = self._state[hand]

            # Send zeros before marking state as inactive
            self._send_zero_for_hand(hand, now=self._event_ts(evt))
            st.active = False

            # If both hands are now lifted, update the idle timestamp
            if not (self._state["left"].active or self._state["right"].active):
                self._last_contact_ts = time.time()
        return self._locked(inner)

    def _on_avatar_change(self, ctx, address: str, value: str):
        """Handle avatar changes by clearing all state.

        Ungated, as it always was: a reset only zeros, so one while disabled cannot
        put a stale value back, and the quant wire keeps its zeros on every change."""
        with self._lock:
            self.bridge.log.info("IndexPuppet: Resetting state due to avatar change.")
            self._reset()

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

    def deactivate(self) -> None:
        # Inside the lock, so no handler or tick can land between the flag and the reset.
        with self._lock:
            super().deactivate()
            self._reset()

    def update(self, now: float) -> None:
        # The router ticks disabled mappings too; a tick now would undo deactivate's reset.
        if not self.enabled:
            return
        with self._lock:
            if not self.enabled:
                return
            # Clear the flag after N seconds of inactivity
            if not (self._state["left"].active or self._state["right"].active):
                if self._touch_active and (now - self._last_contact_ts) >= self._tune.touch_active_idle_secs:
                    self._out.active(False)
                    self._touch_active = False
            self._out.tick(now)
