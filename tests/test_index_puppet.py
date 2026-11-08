"""`index_puppet`: the quant wire it has always sent, and the face feed it sends instead when bound.

The handlers are driven directly, as `ControllerLoop` and the OSC server would call them, against a
fake bridge whose `osc.send` is the one wire both `ctx.send` and `update` reach, as in production.
"""
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

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
