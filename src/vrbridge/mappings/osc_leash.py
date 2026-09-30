"""The leash: an avatar's leash sensing turned into `/input/` movement, so a held or planted leash
pulls the wearer.

**Provenance.** The idea's ancestor is OSCLeash (MIT, copyright 2022 ZenithVal), credited here by
name. This module is a clean rewrite that shares no code with it: the sensing, the gating and the
pull below are this workspace's own, and every tuned value in `settings.LeashSettings` was measured
here or is the prototype avatar's declared geometry.

**Inbound**, all under one configurable prefix (`LeashSettings.prefix`, default `Leash`):

* `<prefix>/Right`, `<prefix>/Forward` -- raw float readings of two face-proximity box receivers
  on the wearer, one per horizontal axis of the wearer's root-yaw frame. Each decodes to metres as
  `ratio * (span * reading - (span / 2 + sender_radius))`; the avatar declares the geometry and the
  settings must match it. A reading of exactly 0.0 is the sender outside the boxes, never a
  position, so two nonzero readings are also the sender's presence.
* `<prefix>/Planted`, `<prefix>/Held` -- latching bools the avatar's own machine drives.
* `<prefix>/Slack` -- an unsynced float the avatar declares with a default and no menu, in metres
  and used as-is. While the worn avatar has sent one it is the pull's `slack`. Until then, and for
  a NaN, infinite or negative value, `LeashSettings.slack` is the fallback.

The mapping is active while it is enabled, `Held` or `Planted` is true, and both readings are
nonzero. Otherwise it writes zeros and holds.

**Slack's first read.** The client sends a parameter over OSC only when its value changes, so a
bridge started against a wearer already in the avatar never hears Slack. The first step that finds
the gate open with no Slack stored therefore reads it off the client's OSCQuery node, on a thread of
its own because the read blocks. The read happens on use rather than on the avatar change because a
cold load answers with a 404 or the outgoing avatar's tree for up to a minute (docs/design.md,
the quant-channel directory's read-on-use ruling); an open gate means the new avatar is loaded and sending. A failed read leaves the
fallback in place and is retried at most every `SLACK_REREAD_SECS`. A value that arrives over OSC
wins over a read in flight, and an avatar change or a target selection discards one.

**The pull, a ratchet.** `d = hypot(forward, right)`. Its strength is
`u = min(u_max, (peak - slack) / ramp)`, where `peak` is the furthest distance this pull has held
for two consecutive steps, `max(peak, min(d, d_prev))`, so the strength never falls while the pull
lasts: a deeper overshoot pulls faster rather than longer. A pull begins the step that `peak` first
exceeds `slack`, and ends, writing zeros, the step `d` is back at or inside it. The two steps are
there because a frame's readings arrive as separate datagrams, measured about a millisecond apart
against the Av3Emulator, so a step can land between them and read a distance from two frames
that the leash never reached; under a ratchet that one reading would set the whole pull's strength.
The cost is one step on a rising pull: the strength reached at a step is the one the previous
step's distance supports, and a pull begins a step after `d` first passes `slack`. A step deferred
while a reading is fresh would cost less, but needs a window sized to a burst spread only the
emulator's has been measured, and a starvation bound past which a torn read still lands. The
pairing guards the peak only: a torn step still steers for its tick, and one reading inside `slack`
ends the pull. The history is forgotten at every activation, avatar change and target selection.
The direction is recomputed every tick from the current offset, and an axis whose share of it
(`|component| / d`) is under `axis_min` is left at zero so the wearer keeps that axis. Each nonzero
component is lifted over the client's deadzone: `sign * (0.1 + 0.9 * min(|u * share|, 1))`, forward
to `/input/Vertical`, right to `/input/Horizontal`. `/input/Run` is never raised; it is written 0
with every set of zeros.

**The client facts the pull rests on.**

* Each `/input/` movement axis has its own deadzone of 0.1 and a linear speed law above it; the
  deadzone is per axis, not radial, which is why each component is lifted on its own rather than
  a unit vector being scaled.
* A written value above the deadzone replaces the wearer's own input on that axis until a zero is
  written, and leaves the other axis to the wearer. Hence `axis_min`.
* Movement addresses latch until zeroed, so every exit path writes zeros: the pull ending, the
  sender lost, the gate closing, `deactivate()`, an avatar change, the bridge stopping, and an
  exception in the step.
* The client applies the latest value per parameter per frame, and delivers to an advertised
  bridge twice. So the handlers only store values and are idempotent per value, and the step
  writes an axis only when its value changes at the wire's 32-bit precision.
* `/input/Horizontal` unboxes a float directly and `/input/Vertical` reads anything that is not a
  float as 0.0, so both are always sent as floats.

**Threads.** The handlers only store the latest values, reading the manager's cache under the
mapping's lock so a reordered older datagram never overwrites a newer one. The step runs on the
router's tick (`update()`), at most `LeashSettings.rate` times a second and never faster than the
router ticks; nothing runs on the datagram thread but the store, and the avatar change's zeros.
Slack's first read runs on its own thread and stores under the same lock.

**Avatar change.** The inputs, Slack among them, are cleared and forgotten in the manager's cache,
so the incoming avatar's first values count as changes. An OSC change naming the worn avatar clears them too,
which fails safe: the wearer keeps control until the avatar's values next change.

**`enabled` belongs to the router.** Observation is ungated; only the step checks `enabled`.
"""

from __future__ import annotations

import math
import struct
import threading
import time
from typing import Dict, Optional

from vrbridge import VRBridge
from vrbridge.mappings.mapping_base import Mapping
from vrbridge.settings import settings

# ------------------------------ Config ------------------------------------

# Addresses and the client's deadzone are contracts, not settings (settings.py's header rule).
PARAM_ROOT = "/avatar/parameters/"
AVATAR_CHANGE_ADDR = "/avatar/change"
VERTICAL_ADDR = "/input/Vertical"
HORIZONTAL_ADDR = "/input/Horizontal"
RUN_ADDR = "/input/Run"

RIGHT, FORWARD = "Right", "Forward"
PLANTED, HELD = "Planted", "Held"
SLACK = "Slack"
READINGS = (RIGHT, FORWARD)
FLAGS = (PLANTED, HELD)

#: The client's per-axis `/input/` deadzone: a value at or under it moves nothing.
CLIENT_DEADZONE = 0.1
# On bridge stop a dropped zero has no next step to retry on, so it is retried here, briefly.
STOP_RETRIES = 3
# A decoded component smaller than this, in metres, carries nothing and is never lifted.
AXIS_EPSILON = 1e-3
STOP_RETRY_WAIT = 0.02
# Slack's first read: a loopback GET, and the floor between attempts that fail or find no node.
SLACK_FETCH_TIMEOUT = 1.0
SLACK_REREAD_SECS = 2.0


def decode(reading: float, *, ratio: float, span: float, sender_radius: float) -> float:
    """A box receiver's raw reading, in metres along its axis."""
    return ratio * (span * reading - (span / 2 + sender_radius))


def _wire(v: float) -> float:
    """`v` as the client receives it: OSC floats are 32-bit, so a smaller change is no change."""
    return struct.unpack("f", struct.pack("f", v))[0]


def lift(u: float, share: float) -> float:
    """One axis of a pull at strength `u`, lifted over the client's deadzone. `share` is signed."""
    return math.copysign(CLIENT_DEADZONE + (1.0 - CLIENT_DEADZONE) * min(abs(u * share), 1.0),
                         share)


# ----------------------------- Mapping ------------------------------------

class LeashMapping(Mapping):
    """Pulls the wearer toward a held or planted leash's far end over `/input/`."""
    name = "osc_leash"

    def __init__(self, bridge: VRBridge, tuning=None):
        super().__init__(bridge)
        self._tune = tuning if tuning is not None else settings().leash
        self._addr = {leaf: f"{PARAM_ROOT}{self._tune.prefix}/{leaf}"
                      for leaf in READINGS + FLAGS + (SLACK,)}
        # One lock over the stored inputs, the pull and what was last written. Held across UDP
        # sends (sendto, never a fetch) and across a cache read, which nests _lock ->
        # _cache_lock; nothing takes them the other way.
        self._lock = threading.Lock()
        self._in: Dict[str, object] = {}
        # The furthest distance the pull in progress held for two consecutive steps; None when
        # not pulling.
        self._peak: Optional[float] = None
        # The previous step's distance; None when that step read no distance, and forgotten at
        # every boundary outside the tick, so a new activation, avatar or target never pairs a
        # new distance with an old one.
        self._d_prev: Optional[float] = None
        # What the client holds on each address, as far as a successful send says. None is
        # unknown: a fresh mapping or a newly selected target may face a client still holding a
        # value a previous bridge latched, so the first rest sends zeros rather than trusting it.
        self._sent: Dict[str, Optional[float]] = {VERTICAL_ADDR: None, HORIZONTAL_ADDR: None,
                                                  RUN_ADDR: None}
        self._last_step: Optional[float] = None
        # Slack's first read (module docstring). A read carries the generation it started in and
        # stores only if no avatar change or target selection has bumped it since.
        self._gen = 0
        self._slack_asked: Optional[float] = None
        self._slack_reader: Optional[threading.Thread] = None

    # ---- lifecycle -------------------------------------------------------

    def _attach(self) -> None:
        for leaf, address in self._addr.items():
            self.bridge.on_osc(address, self._on_input)
        self.bridge.on_osc(AVATAR_CHANGE_ADDR, self._on_avatar_change)
        self.bridge.on_target_selected(self._on_target)
        self.bridge.on_stop(self._on_stop)

    def activate(self) -> None:
        with self._lock:
            super().activate()
            # the client's /input/ state is unknown here; start it at rest rather than trust it
            self._d_prev = None
            self._rest_locked("activated")

    def deactivate(self) -> None:
        with self._lock:
            super().deactivate()
            self._d_prev = None
            self._rest_locked("the mapping was disabled")

    # ---- events ----------------------------------------------------------

    def _on_input(self, ctx, address: str, value) -> None:
        with self._lock:
            # The cache is last-arrival-wins under its lock; a handler handed an older datagram
            # after a newer one stores the newer (module docstring, Threads).
            self._in[address] = ctx.get(address, value)

    def _on_avatar_change(self, ctx, address: str, value) -> None:
        with self._lock:
            self._in.clear()
            for a in self._addr.values():
                self.bridge.osc.forget(a)
            self._d_prev = None
            self._gen += 1
            self._slack_asked = None
            self._rest_locked("the avatar changed")

    def _on_target(self, ctx, target) -> None:
        # A new target's /input/ state is unknown; the next rest sends the zeros regardless.
        with self._lock:
            self._d_prev = None
            self._gen += 1
            self._slack_asked = None
            for address in self._sent:
                self._sent[address] = None

    def _on_stop(self, ctx) -> None:
        with self._lock:
            self.enabled = False
            self._d_prev = None
            self._rest_locked("the bridge is stopping", final=True)

    # ---- the tick --------------------------------------------------------

    def update(self, now: float) -> None:
        if self._last_step is not None and now - self._last_step < 1.0 / self._tune.rate:
            return
        self._last_step = now
        with self._lock:
            try:
                self._step_locked(now)
            except Exception:
                self._rest_locked("the step raised", force=True)
                raise

    def _reading(self, leaf: str) -> Optional[float]:
        v = self._in.get(self._addr[leaf])
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v == 0.0:
            return None
        return float(v)

    def _flag(self, leaf: str) -> bool:
        return bool(self._in.get(self._addr[leaf]))

    def _slack(self) -> float:
        """The worn avatar's Slack in metres, or the config's when it has sent none usable."""
        v = self._in.get(self._addr[SLACK])
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            return self._tune.slack
        return float(v)

    def _ask_slack_locked(self, now: float) -> None:
        """Start Slack's first read unless one is stored, running, or asked inside the floor."""
        if self._addr[SLACK] in self._in:
            return
        if self._slack_reader is not None and self._slack_reader.is_alive():
            return
        if self._slack_asked is not None and now - self._slack_asked < SLACK_REREAD_SECS:
            return
        self._slack_asked = now
        self._slack_reader = threading.Thread(target=self._read_slack, args=(self._gen,),
                                              name="LeashSlackRead", daemon=True)
        self._slack_reader.start()

    def _read_slack(self, gen: int) -> None:
        address = self._addr[SLACK]
        # The blocking read, on this thread only, with no lock held.
        result = self.bridge.osc.fetch(address, timeout=SLACK_FETCH_TIMEOUT)
        if not result.ok:
            self.bridge.log.debug("Leash: %s unread (%s); slack stays %.2f m from the config.",
                                  address, result.reason, self._tune.slack)
            return
        with self._lock:
            if gen == self._gen and address not in self._in:
                self._in[address] = result.value

    def _step_locked(self, now: float) -> None:
        d_prev, self._d_prev = self._d_prev, None
        if not self.enabled:
            # Only reachable with an axis still off zero when a zero was dropped on the way out.
            self._rest_locked()
            return
        if not (self._flag(HELD) or self._flag(PLANTED)):
            self._rest_locked()
            return
        raw = {leaf: self._reading(leaf) for leaf in READINGS}
        if any(v is None for v in raw.values()):
            self._rest_locked()
            return
        self._ask_slack_locked(now)
        t = self._tune
        slack = self._slack()
        m = {leaf: decode(v, ratio=t.ratio, span=t.span, sender_radius=t.sender_radius)
             for leaf, v in raw.items()}
        forward, right = m[FORWARD], m[RIGHT]
        d = self._d_prev = math.hypot(forward, right)
        if d <= slack:
            self._rest_locked()
            return
        # A distance seen on one step alone may be a torn frame (module docstring, the ratchet).
        held = min(d, d_prev) if d_prev is not None else slack
        if held > slack:
            self._peak = held if self._peak is None else max(self._peak, held)
        if self._peak is None:
            # past slack for one step only: not yet a pull
            self._rest_locked()
            return
        u = min(t.u_max, (self._peak - slack) / t.ramp)
        out = {}
        for address, component in ((VERTICAL_ADDR, forward), (HORIZONTAL_ADDR, right)):
            # a component under a millimetre is zero: float32 rounding leaves a reported zero a
            # hair off it, and a lifted hair is a deadzone push in a random sign
            share = component / d if abs(component) >= AXIS_EPSILON else 0.0
            out[address] = lift(u, share) if share != 0.0 and abs(share) >= t.axis_min else 0.0
        self._write_locked(out)

    # ---- writes ----------------------------------------------------------

    def _write_locked(self, values: Dict[str, float]) -> None:
        """Send each axis whose wire value changed; record it only once a send succeeded."""
        for address, v in values.items():
            v = _wire(v)
            if v != self._sent[address] and self.bridge.osc.send(address, v):
                self._sent[address] = v

    def _rest_locked(self, why: Optional[str] = None, *, force: bool = False,
                     final: bool = False) -> None:
        """End any pull and put both axes and Run at zero, unless the client is known to hold them.

        A dropped zero stays pending (its `_sent` entry keeps the old value) and is re-sent on the
        next step. With `final` there is no next step, so the send is retried a few times here.
        """
        self._peak = None
        if not force and all(v == 0 for v in self._sent.values()):
            return
        if why:
            self.bridge.log.info("Leash: %s; releasing movement.", why)
        # a release is all three or nothing: Run goes with the axes every time they are zeroed
        pending = list(self._sent)
        for attempt in range(STOP_RETRIES if final else 1):
            for address in list(pending):
                zero = 0 if address == RUN_ADDR else 0.0
                if self.bridge.osc.send(address, zero):
                    self._sent[address] = zero
                    pending.remove(address)
            if not pending:
                return
            if final:
                time.sleep(STOP_RETRY_WAIT)
        self.bridge.log.warning("Leash: a zero to %s was dropped; the wearer may still be moving.%s",
                                ", ".join(pending),
                                "" if final else " It is re-sent on the next step.")
