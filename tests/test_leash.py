"""The leash: sensing in, a ratchet pull out on `/input/`, zeros on every way out.

Intent before test, per `docs/design.md`. Inputs are driven end to end over loopback, the fake as
the client's out-port stream (`FakeVRChat.emit`, `copies=2` for the doubled delivery); the step is
driven by calling `update()` with a clock the test owns, as the router's tick would. What the
wearer feels is read off the fake: the writes that reached it on `/input/`. The intents:

* **Active only while the leash is held or planted and the sender is present;** a reading of 0.0
  is the sender gone, never a position.
* **The pull is a ratchet:** it begins past `slack`, its strength follows the furthest distance
  of this pull and never falls while the pull lasts, its direction follows the offset every tick,
  and it ends, zeroed, the tick the wearer is back at or inside `slack`.
* **Every written axis is over the client's deadzone,** and an axis carrying under `axis_min` of
  the pull is never written, so the wearer keeps it.
* **Idempotent per value:** an axis is written only when its value changes, doubled delivery
  included.
* **Zeros on every exit path,** `/input/Run` among them, and `Run` never raised.
"""
import math
import time

import pytest

import vrbridge.mappings.osc_leash as osc_leash
from vrbridge.engine import VRBridge
from vrbridge.mappings.osc_leash import (AVATAR_CHANGE_ADDR, HORIZONTAL_ADDR, RUN_ADDR,
                                         VERTICAL_ADDR, LeashMapping, decode, lift)
from vrbridge.settings import ConfigError, LeashSettings, Settings, set_settings

from .fake_vrchat import FakeVRChat

T = LeashSettings()
INPUTS = (VERTICAL_ADDR, HORIZONTAL_ADDR, RUN_ADDR)
# Readings and writes cross the wire as float32; a decoded metre carries about 1e-6 of it.
TOL = 1e-4


def reading(metres: float, t: LeashSettings = T) -> float:
    """The raw box reading that decodes to `metres`: `decode`'s inverse."""
    return (metres / t.ratio + t.span / 2 + t.sender_radius) / t.span


def pulled(u: float, share: float = 1.0) -> float:
    """The value the contract writes for strength u and a signed share, spelled out here."""
    return math.copysign(0.1 + 0.9 * min(abs(u * share), 1.0), share)


def wait_for(cond, timeout=3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.002)
    return False


class Rig:
    """A started bridge pinned at the fake, the mapping registered and active."""

    def __init__(self, vrc: FakeVRChat, *, copies=1, activate=True, tuning=T):
        self.vrc = vrc
        self.bridge = VRBridge(enable_steamvr=False, advertise=False, discover=False,
                               target=("127.0.0.1", vrc.osc_port))
        self.bridge.osc.start()
        vrc.out_port = self.bridge.osc.osc_port
        vrc.copies = copies
        self.t = tuning
        self.m = LeashMapping(self.bridge, tuning=tuning)
        self.m.register()
        if activate:
            self.m.activate()
        self.now = 1000.0
        self.sends = 0
        real_send = self.bridge.osc.send

        def counting_send(address, value):
            ok = real_send(address, value)
            if ok and address.startswith("/input/"):
                self.sends += 1
            return ok
        self.bridge.osc.send = counting_send

    def close(self):
        self.bridge.osc.stop()

    # -- the client's out-port stream --

    def emit(self, **values):
        """Send leash parameters by leaf name and wait until the mapping has stored each."""
        for leaf, value in values.items():
            address = self.m._addr[leaf]
            self.vrc.emit(address, value)
        for leaf, value in values.items():
            address = self.m._addr[leaf]
            assert wait_for(lambda: self.m._in.get(address) == pytest.approx(value, abs=1e-6)
                            and type(self.m._in.get(address)) is type(value)), \
                f"{leaf}={value!r} never reached the mapping"

    def _stored(self, address, value) -> bool:
        got = self.m._in.get(address)
        if type(got) is not type(value):
            return False
        return got == value if isinstance(value, bool) else abs(got - value) < 1e-6

    def at(self, forward=0.0, right=0.0, up=0.3, **flags):
        """Place the sensed object at an offset in metres, with any flags."""
        self.emit(Forward=reading(forward, self.t), Right=reading(right, self.t),
                  Up=reading(up, self.t), **flags)

    def held_at(self, forward=0.0, right=0.0):
        self.at(forward, right, Held=True, Present=True)

    # -- the tick --

    def step(self, n=1):
        """One router tick past the rate limit, then wait for its writes to land."""
        for _ in range(n):
            self.now += 1.0
            self.m.update(self.now)
            self.landed()

    def landed(self):
        assert wait_for(lambda: len(self.written()) >= self.sends), "a write never landed"

    # -- what reached the client --

    def written(self, address=None):
        return [(a, v) for a, v in list(self.vrc.messages)
                if (a == address if address else a in INPUTS)]

    def values(self, address):
        return [v for _, v in self.written(address)]

    def last(self, address):
        vs = self.values(address)
        return vs[-1] if vs else 0.0


@pytest.fixture
def rig():
    rigs = []

    def make(**kw):
        vrc = FakeVRChat().__enter__()
        r = Rig(vrc, **kw)
        rigs.append((vrc, r))
        return r

    yield make
    for vrc, r in rigs:
        r.close()
        vrc.__exit__(None, None, None)


def assert_at_rest(r):
    """Both axes' last write is zero and Run was zeroed with them."""
    assert r.last(VERTICAL_ADDR) == 0.0 and r.last(HORIZONTAL_ADDR) == 0.0
    assert r.values(RUN_ADDR) and r.values(RUN_ADDR)[-1] == 0


# --------------------------------------------------------------------------
# The decode and the lift, as arithmetic
# --------------------------------------------------------------------------

def test_the_decode_is_the_declared_geometry():
    """Intended: metres = ratio * (span * reading - (span / 2 + sender_radius)), at the
    prototype's 10, 6 and 0.05: the box centre plus the sender's radius reads zero, and a
    reading step of 1/60 is a metre."""
    kw = dict(ratio=10.0, span=6.0, sender_radius=0.05)
    assert decode(3.05 / 6, **kw) == pytest.approx(0.0)
    assert decode(3.05 / 6 + 1 / 60, **kw) == pytest.approx(1.0)
    assert decode(1.0, **kw) == pytest.approx(29.5)
    assert decode(0.05 / 6, **kw) == pytest.approx(-30.0)


def test_the_lift_clears_the_deadzone_and_keeps_the_sign():
    """Intended: sign * (0.1 + 0.9 * min(|u * share|, 1)): any pull at all is over the client's
    0.1, the law is linear above it, and it saturates at 1."""
    assert lift(1e-6, 1.0) > 0.1
    assert lift(0.5, 1.0) == pytest.approx(0.55)
    assert lift(0.5, -0.5) == pytest.approx(-0.325)
    assert lift(1.0, 1.0) == pytest.approx(1.0)
    assert lift(3.0, -1.0) == pytest.approx(-1.0)


# --------------------------------------------------------------------------
# Gating
# --------------------------------------------------------------------------

@pytest.mark.parametrize("flags, active", [
    (dict(Held=True, Planted=False, Present=True), True),
    (dict(Held=False, Planted=True, Present=True), True),
    (dict(Held=True, Planted=True, Present=True), True),
    (dict(Held=False, Planted=False, Present=True), False),
    (dict(Held=True, Planted=False, Present=False), False),
    (dict(Held=False, Planted=True, Present=False), False),
])
def test_active_only_while_held_or_planted_and_present(rig, flags, active):
    """Intended: the gate is (Held or Planted) and Present. Well past slack, so an open gate
    pulls and a closed one writes nothing at all."""
    r = rig()
    r.at(forward=1.4, **flags)
    r.step()
    if active:
        assert r.last(VERTICAL_ADDR) == pytest.approx(1.0, abs=TOL)
    else:
        assert r.written() == []


def test_a_zero_reading_is_nothing_sensed_never_a_position(rig):
    """Intended: 0.0 on any box is the sender outside the boxes. Decoded, 0.0 would be about
    30 m away and pull at full strength; it must write nothing instead, Up included."""
    r = rig()
    for leaf in ("Forward", "Right", "Up"):
        r.at(forward=1.4, Held=True, Present=True)
        r.emit(**{leaf: 0.0})
        r.step()
        assert r.written() == [], f"a 0.0 on {leaf} was read as a position"


# --------------------------------------------------------------------------
# The ratchet
# --------------------------------------------------------------------------

def test_the_taut_and_slack_edges(rig):
    """Intended: nothing inside slack; a pull the tick d passes it, just over the deadzone; zeros,
    Run with them, the tick d is back inside."""
    r = rig()
    r.held_at(forward=T.slack - 0.02)
    r.step()
    assert r.written() == []

    r.held_at(forward=T.slack + 0.03)
    r.step()
    assert r.values(VERTICAL_ADDR) == [pytest.approx(pulled(0.03 / T.ramp), abs=TOL)]
    assert r.values(HORIZONTAL_ADDR) == [], "a pure forward pull wrote the wearer's other axis"

    r.held_at(forward=T.slack - 0.02)
    r.step()
    assert_at_rest(r)


def test_strength_holds_while_the_distance_falls(rig):
    """Intended: u follows the pull's furthest point, so walking back in keeps the pull's
    strength until the wearer is inside slack; it never fades on the way in."""
    r = rig()
    r.held_at(forward=1.1)                     # u = 0.5
    r.step()
    r.held_at(forward=0.95)                    # nearer, still past slack
    r.step()
    r.held_at(forward=0.85)
    r.step()
    assert r.values(VERTICAL_ADDR) == [pytest.approx(pulled(0.5), abs=TOL)], \
        "the strength moved while the distance fell"


def test_strength_rises_when_the_distance_grows(rig):
    """Intended: going further out raises u to the new furthest point, capped at u_max."""
    r = rig()
    for forward, u in ((1.0, 0.2 / 0.6), (1.2, 0.4 / 0.6), (1.1, 0.4 / 0.6), (2.5, 1.0)):
        r.held_at(forward=forward)
        r.step()
        assert r.last(VERTICAL_ADDR) == pytest.approx(pulled(u), abs=TOL), f"at {forward} m"


def test_a_new_pull_starts_its_ratchet_afresh(rig):
    """Intended: the peak belongs to one pull. After slack ends it, the next pull's strength is
    its own, not the last pull's furthest point."""
    r = rig()
    r.held_at(forward=2.0)
    r.step()
    r.held_at(forward=0.5)
    r.step()
    r.held_at(forward=1.1)
    r.step()
    assert r.last(VERTICAL_ADDR) == pytest.approx(pulled(0.5), abs=TOL)


def test_the_direction_is_recomputed_every_tick(rig):
    """Intended: at one held strength the heading follows the current offset: the far end moving
    from ahead-right to behind-right flips Vertical and keeps Horizontal."""
    r = rig()
    r.held_at(forward=1.0, right=1.0)          # d = 1.414, u capped at 1
    r.step()
    share = 1.0 / math.sqrt(2.0)
    assert r.last(VERTICAL_ADDR) == pytest.approx(pulled(1.0, share), abs=TOL)
    assert r.last(HORIZONTAL_ADDR) == pytest.approx(pulled(1.0, share), abs=TOL)

    r.held_at(forward=-1.0, right=1.0)
    r.step()
    assert r.last(VERTICAL_ADDR) == pytest.approx(pulled(1.0, -share), abs=TOL)
    assert r.last(HORIZONTAL_ADDR) == pytest.approx(pulled(1.0, share), abs=TOL)


def test_an_axis_under_axis_min_is_left_to_the_wearer(rig):
    """Intended: an axis carrying under a quarter of the pull is never written, so the wearer's
    own stick keeps it; the axis carrying the pull is written."""
    r = rig()
    r.held_at(forward=1.2, right=0.2)          # right share 0.16
    r.step()
    assert r.values(HORIZONTAL_ADDR) == []
    d = math.hypot(1.2, 0.2)
    assert r.last(VERTICAL_ADDR) == pytest.approx(pulled((d - 0.8) / 0.6, 1.2 / d), abs=TOL)

    r.held_at(forward=1.2, right=0.5)          # right share 0.38: now written
    r.step()
    assert r.last(HORIZONTAL_ADDR) > 0.1


def test_every_written_axis_clears_the_deadzone(rig):
    """Intended: a nonzero write is over 0.1 whatever the strength and share, or the client
    ignores it; the smallest pull just past slack is the hard case."""
    r = rig()
    for forward, right in ((0.81, 0.0), (0.7, 0.5), (-0.6, -0.6), (0.3, -0.9), (3.0, 1.0)):
        r.held_at(forward=forward, right=right)
        r.step()
    nonzero = [v for a, v in r.written() if a != RUN_ADDR and v != 0.0]
    assert nonzero and all(abs(v) > 0.1 for v in nonzero), nonzero


# --------------------------------------------------------------------------
# Idempotence
# --------------------------------------------------------------------------

def test_doubled_delivery_and_repeated_ticks_write_each_value_once(rig):
    """Intended: the client delivers every inbound value twice and the tick runs many times per
    change; neither multiplies a write. An axis is written only when its value changes."""
    r = rig(copies=2)
    r.held_at(forward=1.1, right=0.6)
    r.step(5)
    r.held_at(forward=1.1, right=0.6)          # the same values again, doubled again
    r.step(5)
    assert len(r.values(VERTICAL_ADDR)) == 1
    assert len(r.values(HORIZONTAL_ADDR)) == 1
    r.held_at(forward=0.2)
    r.step(5)
    assert len(r.values(VERTICAL_ADDR)) == 2 and len(r.values(RUN_ADDR)) == 1
    assert_at_rest(r)


def test_the_step_is_limited_to_rate(rig):
    """Intended: under a router ticking faster than `rate`, the step runs at most `rate` times a
    second; a tick inside one step period does nothing."""
    r = rig()
    r.held_at(forward=1.4)
    r.m.update(2000.0)
    r.landed()
    r.held_at(forward=0.2)
    r.m.update(2000.0 + 0.5 / T.rate)
    time.sleep(0.05)
    assert r.values(RUN_ADDR) == [], "a tick inside the step period stepped"
    r.m.update(2000.0 + 1.01 / T.rate)
    r.landed()
    assert_at_rest(r)


# --------------------------------------------------------------------------
# Zeros on every exit path
# --------------------------------------------------------------------------

def pulling(r):
    r.held_at(forward=1.4, right=1.0)
    r.step()
    assert r.last(VERTICAL_ADDR) > 0.1 and r.last(HORIZONTAL_ADDR) > 0.1


@pytest.mark.parametrize("lose", [
    dict(Present=False),
    dict(Forward=0.0),
    dict(Right=0.0),
    dict(Held=False),
])
def test_losing_the_sender_or_the_gate_zeros(rig, lose):
    """Intended: mid-pull, the sender leaving the boxes (Present false, or a reading at 0.0) or
    the gate closing writes zeros the next step, and holds."""
    r = rig()
    pulling(r)
    r.emit(**lose)
    r.step(3)
    assert_at_rest(r)
    assert len(r.values(RUN_ADDR)) == 1, "zeros were repeated"


def test_disabling_zeros_and_then_writes_nothing(rig):
    """Intended: `deactivate()` mid-pull zeros at once, and a disabled mapping writes nothing
    however far the leash is pulled."""
    r = rig()
    pulling(r)
    r.m.deactivate()
    r.landed()
    assert_at_rest(r)
    before = len(r.written())
    r.held_at(forward=3.0)
    r.step(3)
    assert len(r.written()) == before


def test_an_avatar_change_zeros_and_forgets_the_inputs(rig):
    """Intended: an avatar change mid-pull zeros at once. The inputs are forgotten, so the
    incoming avatar's first values reach the mapping even when they equal the outgoing
    avatar's, which the change filter would otherwise eat."""
    r = rig()
    pulling(r)
    r.vrc.emit(AVATAR_CHANGE_ADDR, "avtr_00000000-0000-0000-0000-000000000001")
    assert wait_for(lambda: len(r.values(RUN_ADDR)) == 1), "no zeros on the change"
    assert r.m._in == {}, "the inputs survived the change"
    assert_at_rest(r)
    r.step()
    assert len(r.values(RUN_ADDR)) == 1

    pulling(r)                                 # the same values as before the change
    assert len(r.values(VERTICAL_ADDR)) == 3


def test_stopping_the_bridge_zeros(rig):
    """Intended: `VRBridge.stop()` mid-pull leaves both axes at zero at the client, since a latched
    axis would walk the wearer away after the bridge is gone."""
    r = rig()
    pulling(r)
    r.bridge.stop()
    r.landed()
    assert_at_rest(r)


def test_an_exception_in_the_step_zeros_and_is_raised(rig, monkeypatch):
    """Intended: a step that raises leaves the client at rest and still raises, so the router
    reports it rather than a silent mapping."""
    r = rig()
    pulling(r)

    def broken(*a, **kw):
        raise RuntimeError("decode broke")
    monkeypatch.setattr(osc_leash, "decode", broken)
    with pytest.raises(RuntimeError, match="decode broke"):
        r.step()
    r.landed()
    assert_at_rest(r)


def test_run_is_never_raised(rig):
    """Intended: `/input/Run` is only ever written 0, through a full pull at the cap and out."""
    r = rig()
    for forward in (1.0, 2.5, 5.0, 1.0, 0.1):
        r.held_at(forward=forward, right=0.3)
        r.step()
    r.m.deactivate()
    r.landed()
    assert r.values(RUN_ADDR) and set(r.values(RUN_ADDR)) == {0}


def test_both_axes_go_out_as_floats(rig):
    """Intended: `/input/Horizontal` unboxes a float and `/input/Vertical` reads anything else as
    0.0, so every axis write, zeros included, is a float."""
    r = rig()
    pulling(r)
    r.m.deactivate()
    r.landed()
    for address in (VERTICAL_ADDR, HORIZONTAL_ADDR):
        assert all(type(v) is float for v in r.values(address)), address


# --------------------------------------------------------------------------
# Settings and registration
# --------------------------------------------------------------------------

def test_the_prefix_names_every_input():
    """Intended: all six inputs live under one configurable prefix."""
    bridge = VRBridge(enable_steamvr=False, advertise=False, discover=False)
    m = LeashMapping(bridge, tuning=LeashSettings(prefix="Pet/Leash"))
    assert sorted(m._addr.values()) == sorted(
        f"/avatar/parameters/Pet/Leash/{leaf}"
        for leaf in ("Right", "Up", "Forward", "Present", "Planted", "Held"))


@pytest.mark.parametrize("kw, must_name", [
    (dict(prefix=""), "prefix"),
    (dict(prefix="/Leash"), "prefix"),
    (dict(ramp=0.0), "ramp"),
    (dict(u_max=1.5), "u_max"),
    (dict(axis_min=1.0), "axis_min"),
    (dict(rate=0.0), "rate"),
    (dict(span=0.0), "span"),
])
def test_bad_leash_settings_name_the_key(kw, must_name):
    with pytest.raises(ConfigError, match=must_name):
        LeashSettings(**kw).validate("leash")


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("router_name", ["default", "camera", "remy"])
def test_shipped_routers_register_it_only_when_enabled(router_name, enabled):
    """Intended: off by default, because it moves the wearer; `[leash] enabled` registers it
    active in every shipped router, outside mode switching."""
    from vrbridge.cli import ROUTERS
    set_settings(Settings(leash=LeashSettings(enabled=enabled)))
    try:
        bridge = VRBridge(enable_steamvr=False, advertise=False, discover=False)
        router = ROUTERS[router_name](bridge)
        leash = router._mappings.get("osc_leash")
        if not enabled:
            assert leash is None
            return
        assert isinstance(leash, LeashMapping) and leash.enabled
        router.evaluate()
        assert leash.enabled
    finally:
        set_settings(None)
