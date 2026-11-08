"""`index_puppet`: the quant wire it has always sent, and the face feed it sends instead when bound.

The handlers are driven directly, as `ControllerLoop` and the OSC server would call them, against a
fake bridge whose `osc.send` is the one wire both `ctx.send` and `update` reach, as in production.
"""
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from vrbridge.face_blob import _byte
from vrbridge.mappings import index_puppet
from vrbridge.mappings.index_puppet import IndexPuppetMapping
from vrbridge.settings import PuppetSettings

QUANT_WIRE = Path(__file__).parent / "fixtures" / "puppet_quant_wire.json"


class _Wire:
    def __init__(self):
        self.sent: list = []

    def send(self, address, value):
        self.sent.append((address, value))
        return True


class _Log:
    def __init__(self):
        self.errors: list = []

    def info(self, *a): pass
    def debug(self, *a): pass
    def warning(self, *a): pass
    def exception(self, *a): pass
    def error(self, *a): self.errors.append(a)


class _Bridge:
    def __init__(self):
        self.osc = _Wire()
        self.log = _Log()
        self.ctx = SimpleNamespace(osc=self.osc, send=self.osc.send)

    def on_osc(self, address, callback, *, watch=None): pass
    def on_controller(self, event_type, hand, callback, *, watch=None): pass


class _Clock:
    """Stands in for the module's `time`, so the wall-clock stamps the mapping takes itself (the
    idle window's contact time, a reset's seat time) line up with the event times the script gives."""
    def __init__(self, t: float):
        self.t = t

    def time(self) -> float:
        return self.t


def _evt(ax, ay, when):
    return SimpleNamespace(ax=ax, ay=ay, when=when)


def _quant_script(m: IndexPuppetMapping, ctx, clock: _Clock) -> None:
    """Touch, mirror, two hands, lifts, the idle drop, a touch with no position, clamping and an
    avatar change: every path the quant wire has."""
    def at(t):
        clock.t = t
        return t
    m._on_touch("left")(ctx, _evt(0.5, -0.25, at(100.00)))       # mirrored to the right
    m._on_raw("left")(ctx, _evt(0.8, 0.1, at(100.02)))
    m._on_raw("left")(ctx, _evt(0.9, -0.6, at(100.04)))
    m._on_touch("right")(ctx, _evt(-0.3, 0.4, at(100.05)))       # both down: no mirror
    m._on_raw("right")(ctx, _evt(-0.35, 0.45, at(100.07)))
    m._on_raw("left")(ctx, _evt(0.7, -0.5, at(100.07)))
    m._on_lift("left")(ctx, _evt(None, None, at(100.09)))
    m.update(at(100.10))                                          # right still down: Enable holds
    m._on_lift("right")(ctx, _evt(None, None, at(100.12)))       # the lone right lift mirrors
    m.update(at(100.15))                                          # inside the idle window
    m.update(at(100.25))                                          # past it: Enable drops
    m._on_touch("right")(ctx, _evt(None, None, at(100.30)))      # no position: Enable only
    m._on_raw("right")(ctx, _evt(1.2, -1.5, at(100.32)))         # clamped
    at(100.35)
    m._on_avatar_change(ctx, "/avatar/change", "avtr_next")
    m._on_raw("right")(ctx, _evt(0.2, 0.2, at(100.40)))          # hand state reset: zeros
    m.update(at(100.60))


def _record_quant_wire(monkeypatch, tuning: PuppetSettings) -> list:
    clock = _Clock(0.0)
    monkeypatch.setattr(index_puppet, "time", clock)
    bridge = _Bridge()
    m = IndexPuppetMapping(bridge, tuning=tuning)
    m.activate()
    _quant_script(m, bridge.ctx, clock)
    return [[a, v, type(v).__name__] for a, v in bridge.osc.sent]


QUANT_CASES = {
    "defaults": PuppetSettings(),
    "separate_inverted_float_only": PuppetSettings(
        quant_level=0, single_touch_mode="separate", invert_x=-1, invert_y=-1,
        float_smooth_tau_secs=0.0),
    "two_bits_slow": PuppetSettings(quant_level=2, float_smooth_tau_secs=0.2,
                                    touch_active_idle_secs=0.0),
}


@pytest.mark.parametrize("case", sorted(QUANT_CASES))
def test_with_no_face_key_bound_the_wire_is_the_recorded_quant_wire(monkeypatch, case):
    """Recorded from `index_puppet` before the input/output split; addresses, values, their order
    and their Python types (an int to a declared float is dropped) all have to match."""
    expected = json.loads(QUANT_WIRE.read_text())[case]
    assert _record_quant_wire(monkeypatch, QUANT_CASES[case]) == expected


# ---- the face feed ----------------------------------------------------------------------------

FACE_KEYS = dict(
    face_active="SoftPalateClose",
    face_left_x_pos="NasalDilationLeft", face_left_x_neg="NasalConstrictLeft",
    face_left_y_pos="NeckFlexLeft", face_left_y_neg="ThroatSwallow",
    face_right_x_pos="NasalDilationRight", face_right_x_neg="NasalConstrictRight",
    face_right_y_pos="NeckFlexRight", face_right_y_neg="JawBackward")
BOUND = set(FACE_KEYS.values())
TICK = 1.0 / 45                       # the router's update rate


class _Face:
    """The feed's contract without the sender: overrides over what `pack` would carry, which is
    `tracked` here. `byte(key)` is what the next blob would hold."""
    def __init__(self, tracked=None):
        self.tracked = dict(tracked or {})
        self.overrides: dict = {}
        self.calls: list = []

    def set_keys(self, values):
        self.calls.append(("set", dict(values)))
        self.overrides.update({k: _byte(v) for k, v in values.items()})

    def clear_keys(self, names):
        names = list(names)
        self.calls.append(("clear", set(names)))
        for n in names:
            self.overrides.pop(n, None)

    def byte(self, key):
        return self.overrides.get(key, _byte(self.tracked.get(key, 0.0)))


def _face_rig(tracked=None, **tune):
    bridge = _Bridge()
    face = _Face(tracked)
    m = IndexPuppetMapping(bridge, tuning=PuppetSettings(**{**FACE_KEYS, **tune}), face=face)
    m.activate()
    return m, face, bridge


def test_with_any_face_key_bound_no_osc_is_sent_at_all(monkeypatch):
    """Intended: the binding selects one transport. Not one `IndexPuppet/*` address, Enable
    included, goes out while the face feed is bound, even for a single bound key."""
    clock = _Clock(0.0)
    monkeypatch.setattr(index_puppet, "time", clock)
    bridge = _Bridge()
    face = _Face()
    m = IndexPuppetMapping(bridge, tuning=PuppetSettings(face_active="SoftPalateClose"), face=face)
    m.activate()
    _quant_script(m, bridge.ctx, clock)
    assert bridge.osc.sent == []
    assert ("set", {"SoftPalateClose": 1.0}) in face.calls


@pytest.mark.parametrize("face_enabled, reason", [(False, "[face] enabled is false"),
                                                   (True, "not listening")])
def test_bound_keys_with_no_sender_log_once_and_send_nothing(monkeypatch, face_enabled, reason):
    from vrbridge.settings import FaceSettings, Settings, set_settings
    set_settings(Settings(face=FaceSettings(enabled=face_enabled)))
    try:
        clock = _Clock(0.0)
        monkeypatch.setattr(index_puppet, "time", clock)
        bridge = _Bridge()
        m = IndexPuppetMapping(bridge, tuning=PuppetSettings(**FACE_KEYS), face=None)
        m.activate()
        _quant_script(m, bridge.ctx, clock)
        m.deactivate()
    finally:
        set_settings(None)
    assert bridge.osc.sent == []                             # no fallback to quant
    assert len(bridge.log.errors) == 1
    assert reason in bridge.log.errors[0][1]


def test_each_axis_splits_onto_its_pos_and_neg_keys_and_a_lone_pad_mirrors():
    m, face, bridge = _face_rig(float_smooth_tau_secs=0.0)
    m._on_touch("left")(bridge.ctx, _evt(0.6, -0.4, 1.0))
    m.update(1.0)
    assert face.calls[-1] == ("set", {
        "SoftPalateClose": 1.0,
        "NasalDilationLeft": 0.6, "NasalConstrictLeft": 0.0,
        "NeckFlexLeft": 0.0, "ThroatSwallow": 0.4,
        "NasalDilationRight": 0.6, "NasalConstrictRight": 0.0,       # "together": mirrored
        "NeckFlexRight": 0.0, "JawBackward": 0.4})

    m._on_touch("right")(bridge.ctx, _evt(-0.2, 0.3, 1.01))           # both down: each its own
    m._on_raw("left")(bridge.ctx, _evt(-0.5, 0.1, 1.01))
    m.update(1.02)
    assert face.calls[-1] == ("set", {
        "SoftPalateClose": 1.0,
        "NasalDilationLeft": 0.0, "NasalConstrictLeft": 0.5,
        "NeckFlexLeft": 0.1, "ThroatSwallow": 0.0,
        "NasalDilationRight": 0.0, "NasalConstrictRight": 0.2,
        "NeckFlexRight": 0.3, "JawBackward": 0.0})


def test_a_lift_eases_to_byte_zero_and_then_the_keys_are_cleared_not_held(monkeypatch):
    """At the default tau. Released rather than written as 0, so a key the tracker fills (here
    ThroatSwallow and the active key) carries the tracker's value again once the puppet rests."""
    clock = _Clock(10.0)
    monkeypatch.setattr(index_puppet, "time", clock)
    tracked = {"ThroatSwallow": 0.4, "SoftPalateClose": 0.2}
    m, face, bridge = _face_rig(tracked)

    def tick():
        clock.t += TICK
        m.update(clock.t)

    tick()                                                   # at rest from the start: no writes
    assert face.calls == []

    m._on_touch("left")(bridge.ctx, _evt(0.0, -1.0, clock.t))
    for _ in range(30):                                      # settle at full -Y
        tick()
    assert face.byte("ThroatSwallow") >= 254                 # an exponential only nears 1.0
    m._on_lift("left")(bridge.ctx, _evt(None, None, clock.t))

    eased = []
    while face.calls[-1][0] == "set":
        tick()
        eased.append(face.byte("ThroatSwallow"))
        assert len(eased) < 200, "the lift never came to rest"
    assert len(eased) > 3                                    # eased over ticks, not snapped
    assert eased[:-1] == sorted(eased[:-1], reverse=True)
    assert face.calls[-2][1]["ThroatSwallow"] > 0.0          # the last write was not a held 0
    assert face.calls[-1] == ("clear", BOUND)

    calls = len(face.calls)
    for _ in range(20):
        tick()
    assert len(face.calls) == calls                          # cleared once, then silent
    assert face.byte("ThroatSwallow") == _byte(0.4)
    assert face.byte("SoftPalateClose") == _byte(0.2)

    m._on_touch("left")(bridge.ctx, _evt(0.5, 0.0, clock.t))  # the next touch writes again
    tick()
    assert face.calls[-1][0] == "set" and face.byte("SoftPalateClose") == 255


def test_active_holds_through_the_idle_window_after_a_lift(monkeypatch):
    clock = _Clock(5.0)
    monkeypatch.setattr(index_puppet, "time", clock)
    m, face, bridge = _face_rig(float_smooth_tau_secs=0.0, touch_active_idle_secs=0.5)
    m._on_touch("right")(bridge.ctx, _evt(0.5, 0.5, 5.0))
    m._on_lift("right")(bridge.ctx, _evt(None, None, 5.0))
    m.update(5.1)
    assert face.calls[-1] == ("set", {**dict.fromkeys(BOUND, 0.0), "SoftPalateClose": 1.0})
    m.update(5.6)
    assert face.calls[-1] == ("clear", BOUND)


def test_update_while_disabled_writes_nothing_on_the_face_feed():
    """The router ticks disabled mappings. `enabled` is cleared directly here, not through
    `deactivate`, so the puppet still holds a touch and only the guard in `update` stands
    between that state and the feed."""
    m, face, bridge = _face_rig(float_smooth_tau_secs=0.0)
    m._on_touch("left")(bridge.ctx, _evt(0.5, 0.5, 1.0))
    m.enabled = False
    for i in range(5):
        m.update(1.0 + i * TICK)
    assert face.calls == []


def test_update_while_disabled_writes_nothing_on_the_quant_wire():
    """As above, with Enable due to drop: the idle drop is `update`'s too."""
    bridge = _Bridge()
    m = IndexPuppetMapping(bridge, tuning=PuppetSettings(touch_active_idle_secs=0.0))
    m.activate()
    m._on_touch("left")(bridge.ctx, _evt(0.5, 0.5, 1.0))
    m._on_lift("left")(bridge.ctx, _evt(None, None, 1.0))
    sent = len(bridge.osc.sent)
    m.enabled = False
    m.update(1e12)
    assert len(bridge.osc.sent) == sent


def test_a_mode_switch_mid_touch_leaves_the_puppet_consistent_on_reactivation():
    m, face, bridge = _face_rig(float_smooth_tau_secs=0.0)
    m._on_touch("left")(bridge.ctx, _evt(0.5, 0.5, 1.0))
    m.update(1.0)
    m.deactivate()                                           # the camera opened mid-touch
    assert face.calls[-1] == ("clear", BOUND) and face.overrides == {}
    m._on_lift("left")(bridge.ctx, _evt(None, None, 1.1))   # dropped: disabled
    m.update(1.2)
    m.activate()
    m.update(1.3)
    assert face.overrides == {}                              # nothing came back on reactivation
    m._on_touch("left")(bridge.ctx, _evt(0.7, 0.0, 2.0))
    m.update(2.0)
    assert face.byte("SoftPalateClose") == 255
    assert face.byte("NasalDilationLeft") == _byte(0.7)
    assert face.byte("NasalDilationRight") == _byte(0.7)     # mirrored: the old left touch is gone


def test_a_mode_switch_mid_touch_raises_enable_again_on_reactivation():
    bridge = _Bridge()
    m = IndexPuppetMapping(bridge, tuning=PuppetSettings())
    m.activate()
    m._on_touch("left")(bridge.ctx, _evt(0.5, 0.5, 1.0))
    m.deactivate()
    assert ("/avatar/parameters/IndexPuppet/Enable", 0) in bridge.osc.sent
    m._on_lift("left")(bridge.ctx, _evt(None, None, 1.1))
    m.activate()
    bridge.osc.sent.clear()
    m._on_touch("left")(bridge.ctx, _evt(0.7, 0.0, 2.0))
    assert bridge.osc.sent[0] == ("/avatar/parameters/IndexPuppet/Enable", 1)


def test_an_avatar_change_resets_and_clears_with_no_ease_across_it():
    m, face, bridge = _face_rig()                            # default tau: a lift would ease
    now = 1.0
    m._on_touch("left")(bridge.ctx, _evt(1.0, 1.0, now))
    for _ in range(30):
        now += TICK
        m.update(now)
    m._on_avatar_change(bridge.ctx, "/avatar/change", "avtr_next")
    assert face.calls[-1] == ("clear", BOUND) and face.overrides == {}
    calls = len(face.calls)
    for _ in range(5):
        now += TICK
        m.update(now)
    assert len(face.calls) == calls                          # nothing eases onto the new avatar
    m._on_touch("left")(bridge.ctx, _evt(1.0, 0.0, now))     # the filter starts again from 0
    m.update(now + TICK)
    m.update(now + 2 * TICK)
    assert 0 < face.byte("NasalDilationLeft") < 255


# ---- the lock ---------------------------------------------------------------------------------

class _Park:
    """Parks the first call it wraps until released, so a second thread can run against it."""
    def __init__(self, fn):
        self.fn, self.parked, self.go, self.armed = fn, threading.Event(), threading.Event(), True

    def __call__(self, *a):
        if self.armed:
            self.armed = False
            self.parked.set()
            self.go.wait(5.0)
        return self.fn(*a)


def test_a_deactivate_waits_for_a_tick_in_flight_and_its_clear_is_the_last_word():
    """A tick parked inside its write, with deactivate arriving from an OSC thread. Unlocked, the
    clear lands first and the tick's write puts the puppet back on the feed for the whole stint."""
    m, face, bridge = _face_rig(float_smooth_tau_secs=0.0)
    m._on_touch("left")(bridge.ctx, _evt(0.5, 0.5, 1.0))
    face.set_keys = _Park(face.set_keys)
    tick = threading.Thread(target=m.update, args=(1.0,))
    tick.start()
    assert face.set_keys.parked.wait(5.0)
    off = threading.Thread(target=m.deactivate)
    off.start()
    off.join(0.2)
    assert off.is_alive(), "deactivate ran while a tick held the puppet"
    face.set_keys.go.set()
    tick.join(5.0)
    off.join(5.0)
    assert face.calls[-1] == ("clear", BOUND) and face.overrides == {}


def test_a_handler_in_flight_finishes_before_the_reset_and_leaves_nothing_behind():
    """A touch parked on its first send (quant: Enable), past the gate, as deactivate arrives.
    Unlocked, the reset runs mid-handler and the handler's samples land after its zeros."""
    bridge = _Bridge()
    m = IndexPuppetMapping(bridge, tuning=PuppetSettings())
    m.activate()
    park = _Park(bridge.osc.send)
    m._out._send = park
    touch = threading.Thread(target=m._on_touch("left"), args=(bridge.ctx, _evt(0.5, 0.5, 1.0)))
    touch.start()
    assert park.parked.wait(5.0)
    off = threading.Thread(target=m.deactivate)
    off.start()
    off.join(0.2)
    assert off.is_alive(), "deactivate ran while a handler held the puppet"
    park.go.set()
    touch.join(5.0)
    off.join(5.0)
    assert not m._touch_active and not any(st.active for st in m._state.values())
    tail = bridge.osc.sent[-21:]                             # the reset: Enable 0, four zeroed axes
    assert tail[0] == ("/avatar/parameters/IndexPuppet/Enable", 0)
    assert all(v == 0 for _, v in tail[1:])
