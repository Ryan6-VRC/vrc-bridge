"""`index_puppet` writing the puppet into the face sender's blob keys.

The sender is a stub recording `set_keys`, since the blob path itself is `test_face_sender`'s; the
mapping's controller handlers are driven directly, as the engine would call them.
"""
from types import SimpleNamespace

import pytest

from vrbridge.engine import VRBridge
from vrbridge.mappings import IndexPuppetMapping
from vrbridge.mappings.index_puppet import (ACTIVE_FACE_KEY, FACE_KEYS, LEFT_X_ADDR, LEFT_Y_ADDR,
                                            RIGHT_X_ADDR, RIGHT_Y_ADDR, TOUCH_ACTIVE_ADDR)
from vrbridge.mappings.osc_face import WRITABLE_KEYS
from vrbridge.settings import PuppetSettings

ALL_KEYS = {k for pair in FACE_KEYS.values() for k in pair} | {ACTIVE_FACE_KEY}


class _Face:
    def __init__(self):
        self.keys = {}

    def set_keys(self, values):
        assert set(values) <= WRITABLE_KEYS
        self.keys.update(values)


class _Ctx:
    def __init__(self):
        self.sent = []

    def send(self, addr, value):
        self.sent.append((addr, value))


def _evt(ax, ay, when):
    return SimpleNamespace(ax=ax, ay=ay, when=when)


@pytest.fixture
def rig():
    bridge = VRBridge(enable_steamvr=False, advertise=False, discover=False)
    face = _Face()
    tune = PuppetSettings(float_smooth_tau_secs=0.0, single_touch_mode="separate",
                          touch_active_idle_secs=0.5)
    m = IndexPuppetMapping(bridge, tuning=tune, face=face)
    m.activate()
    yield m, face, _Ctx()
    m.deactivate()


def test_the_key_table_is_pinned():
    """A contract with the avatar's face-tracking block, which forwards these keys by name: the
    pin is what makes a change here a deliberate one on both sides."""
    assert FACE_KEYS == {
        LEFT_X_ADDR:  ("NasalDilationLeft", "NasalConstrictLeft"),
        LEFT_Y_ADDR:  ("EyeDilationLeft", "EyeConstrictLeft"),
        RIGHT_X_ADDR: ("NasalDilationRight", "NasalConstrictRight"),
        RIGHT_Y_ADDR: ("EyeDilationRight", "EyeConstrictRight"),
    }
    assert ACTIVE_FACE_KEY == "SoftPalateClose"
    assert ALL_KEYS <= WRITABLE_KEYS


def test_a_touch_sets_active_and_each_axis_rides_its_signed_pair(rig):
    m, face, ctx = rig
    m._on_touch("left")(ctx, _evt(0.6, -0.4, 1.0))
    assert face.keys[ACTIVE_FACE_KEY] == 1.0
    assert face.keys["NasalDilationLeft"] == pytest.approx(0.6)
    assert face.keys["NasalConstrictLeft"] == 0.0
    assert face.keys["EyeDilationLeft"] == 0.0
    assert face.keys["EyeConstrictLeft"] == pytest.approx(0.4)
    assert (TOUCH_ACTIVE_ADDR, 1) in ctx.sent        # the OSC addresses are still written

    m._on_raw("left")(ctx, _evt(-0.3, 0.2, 1.1))
    assert face.keys["NasalDilationLeft"] == 0.0
    assert face.keys["NasalConstrictLeft"] == pytest.approx(0.3)
    assert face.keys["EyeDilationLeft"] == pytest.approx(0.2)
    assert face.keys["EyeConstrictLeft"] == 0.0


def test_the_blob_carries_the_smoothed_float_not_the_raw_sample():
    bridge = VRBridge(enable_steamvr=False, advertise=False, discover=False)
    face = _Face()
    m = IndexPuppetMapping(bridge, face=face, tuning=PuppetSettings(
        float_smooth_tau_secs=0.1, single_touch_mode="separate"))
    m.activate()
    ctx = _Ctx()
    m._on_touch("right")(ctx, _evt(0.0, 0.0, 1.0))
    m._on_raw("right")(ctx, _evt(1.0, 0.0, 1.05))
    floats = [v for a, v in ctx.sent if a == RIGHT_X_ADDR]
    assert 0.0 < face.keys["NasalDilationRight"] < 1.0
    assert face.keys["NasalDilationRight"] == pytest.approx(floats[-1])


def test_a_lift_rests_the_axes_and_active_drops_after_the_idle_window(rig):
    m, face, ctx = rig
    m._on_touch("left")(ctx, _evt(0.5, 0.5, 1.0))
    m._on_lift("left")(ctx, _evt(None, None, 1.1))
    assert all(face.keys.get(k, 0.0) == 0.0 for pair in FACE_KEYS.values() for k in pair)
    assert face.keys[ACTIVE_FACE_KEY] == 1.0          # held through the idle window, as Enable is
    m.update(m._last_contact_ts + 0.6)
    assert face.keys[ACTIVE_FACE_KEY] == 0.0


def test_an_avatar_change_and_a_deactivation_each_clear_every_key(rig):
    m, face, ctx = rig
    m._on_touch("left")(ctx, _evt(0.5, 0.5, 1.0))
    m._on_avatar_change(ctx, "/avatar/change", "avtr_x")
    assert {k: face.keys[k] for k in ALL_KEYS} == dict.fromkeys(ALL_KEYS, 0.0)

    m._on_touch("left")(ctx, _evt(0.5, 0.5, 2.0))
    m.deactivate()
    assert {k: face.keys[k] for k in ALL_KEYS} == dict.fromkeys(ALL_KEYS, 0.0)


def test_without_a_face_sender_the_puppet_writes_only_its_addresses():
    bridge = VRBridge(enable_steamvr=False, advertise=False, discover=False)
    m = IndexPuppetMapping(bridge, tuning=PuppetSettings(float_smooth_tau_secs=0.0))
    m.activate()
    ctx = _Ctx()
    m._on_touch("left")(ctx, _evt(0.5, 0.5, 1.0))
    assert (TOUCH_ACTIVE_ADDR, 1) in ctx.sent
