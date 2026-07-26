"""Drive _poll_once against a fake OpenVR input.

This file exists because nothing did. The settings refactor shadowed the tuning
local with OpenVR action data partway through _poll_once, so every settings read
after that point raised AttributeError into an `except Exception` that logs at
debug -- silently killing all scroll, both thumbstick presses, and right-hand
touchpad presses. 84 tests passed over it.

The lesson generalises past the one bug: those broad excepts mean a poll loop can
be substantially dead and still look healthy, so the loop needs a test that
asserts events *come out*, not just that it does not raise.
"""
import pytest

from vrbridge.controller_manager import ControllerManager
from vrbridge.settings import ControllerSettings


class _Digital:
    def __init__(self, active=True, state=False):
        self.bActive, self.bState = active, state


class _Analog:
    def __init__(self, x=0.0, y=0.0, active=True):
        self.x, self.y, self.bActive = x, y, active


class FakeInput:
    """Stands in for openvr.VRInput(). Values are per (action, hand)."""

    def __init__(self):
        self.click = _Digital()
        self.touch = _Digital()
        self.pos = _Analog()
        self.joy_click = _Digital()

    def getDigitalActionData(self, action, hand):
        return {"click": self.click, "touch": self.touch, "joy": self.joy_click}[action]

    def getAnalogActionData(self, action, hand):
        return self.pos


@pytest.fixture
def mgr(tmp_path):
    m = ControllerManager.__new__(ControllerManager)   # skip __init__: no SteamVR, no file writes
    m.log = None
    m._tune = ControllerSettings()
    m._vrinput = FakeInput()
    m._a_track_click, m._a_track_touch, m._a_track_pos = "click", "touch", "pos"
    m._a_joy_click, m._a_joy_pos = "joy", "joypos"
    m._h_left, m._h_right = "L", "R"
    m._state = {"L": m._make_hand_state("left"), "R": m._make_hand_state("right")}
    m.events = []
    m._listener = m.events.append
    return m


def _types(m):
    return [e.type for e in m.events]


def test_a_poll_cycle_raises_nothing_internally(mgr, monkeypatch):
    """_poll_once wraps each block in `except Exception` and logs at debug, so a
    broken read is invisible. Fail the test on any swallowed exception instead."""
    raised = []
    real = ControllerManager._poll_once

    class Loud(FakeInput):
        def getDigitalActionData(self, action, hand):
            try:
                return super().getDigitalActionData(action, hand)
            except Exception as e:      # pragma: no cover
                raised.append(e); raise

    mgr._vrinput = Loud()
    mgr._vrinput.touch = _Digital(state=True)
    real(mgr)
    assert not raised


def test_touch_then_drag_emits_scroll(mgr):
    """The regression that shipped: every settings read in the scroll block came
    off an OpenVR struct, so no scroll event was ever emitted."""
    mgr._vrinput.touch = _Digital(state=True)
    mgr._vrinput.pos = _Analog(0.0, 0.0)
    mgr._poll_once()                                  # touch: anchors last_x/last_y
    assert "touchpad.touch" in _types(mgr)

    mgr.events.clear()
    mgr._vrinput.pos = _Analog(0.0, 0.5)              # a real vertical drag
    mgr._poll_once()
    assert "touchpad.scroll_raw" in _types(mgr), "raw scroll never fired"
    assert "touchpad.vscroll" in _types(mgr), "stepped vscroll never fired"

    vs = next(e for e in mgr.events if e.type == "touchpad.vscroll")
    assert vs.steps == 1                              # 0.5 / v_scroll_step 0.35 -> 1


def test_horizontal_drag_emits_hscroll(mgr):
    mgr._vrinput.touch = _Digital(state=True)
    mgr._vrinput.pos = _Analog(0.0, 0.0)
    mgr._poll_once()
    mgr.events.clear()
    mgr._vrinput.pos = _Analog(0.8, 0.0)              # > h_scroll_step 0.70
    mgr._poll_once()
    assert "touchpad.hscroll" in _types(mgr)


def test_steps_are_clamped_per_frame(mgr):
    """max_steps_per_frame is a settings read inside the scroll block."""
    mgr._vrinput.touch = _Digital(state=True)
    mgr._vrinput.pos = _Analog(0.0, 0.0)
    mgr._poll_once()
    mgr.events.clear()
    mgr._vrinput.pos = _Analog(0.0, 5.0)              # would be ~14 steps unclamped
    mgr._poll_once()
    vs = next(e for e in mgr.events if e.type == "touchpad.vscroll")
    assert vs.steps == ControllerSettings().max_steps_per_frame


def test_inversion_setting_reaches_the_scroll_block(mgr):
    """invert_vscroll was one of the eight attributes read off the wrong object."""
    mgr._tune = ControllerSettings(invert_vscroll=-1)
    mgr._vrinput.touch = _Digital(state=True)
    mgr._vrinput.pos = _Analog(0.0, 0.0)
    mgr._poll_once()
    mgr.events.clear()
    mgr._vrinput.pos = _Analog(0.0, 0.5)
    mgr._poll_once()
    vs = next(e for e in mgr.events if e.type == "touchpad.vscroll")
    assert vs.steps == -1


def test_both_hands_get_touchpad_presses(mgr):
    """The right hand is the second loop iteration, so it read its settings off
    the action data the first iteration had already bound. Left worked, right did not."""
    mgr._vrinput.click = _Digital(state=True)
    mgr._poll_once()
    assert _types(mgr).count("touchpad.press") == 2

    mgr.events.clear()
    mgr._vrinput.click = _Digital(state=False)
    mgr._poll_once()
    assert _types(mgr).count("touchpad.release") == 2
    assert _types(mgr).count("touchpad.short_press") == 2, "long_press_threshold unreachable"


def test_thumbstick_presses_classify(mgr):
    """Thumbstick handling sits after the scroll block and was fully dead."""
    mgr._vrinput.joy_click = _Digital(state=True)
    mgr._poll_once()
    assert _types(mgr).count("thumbstick.press") == 2

    mgr.events.clear()
    mgr._vrinput.joy_click = _Digital(state=False)
    mgr._poll_once()
    assert _types(mgr).count("thumbstick.short_press") == 2


def test_a_long_hold_classifies_as_long_press(mgr, monkeypatch):
    import vrbridge.controller_manager as cm

    now = [1000.0]
    monkeypatch.setattr(cm.time, "time", lambda: now[0])
    mgr._vrinput.click = _Digital(state=True)
    mgr._poll_once()
    now[0] += ControllerSettings().long_press_threshold + 0.1
    mgr.events.clear()
    mgr._vrinput.click = _Digital(state=False)
    mgr._poll_once()
    assert _types(mgr).count("touchpad.long_press") == 2
