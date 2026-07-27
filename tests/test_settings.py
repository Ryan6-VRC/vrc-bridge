"""Settings: the defaults are the values this repo shipped, and bad input is named.

`test_defaults_are_the_shipped_values` is the load-bearing one. Moving ~50 tuned
constants out of five modules is only safe if "no config file" reproduces the old
behavior exactly, and these are feel-tuned against real hardware -- re-deriving
one costs headset time. Every literal below was read off the pre-change source,
not off the new defaults, so the test can actually fail.
"""
from pathlib import Path

import pytest

from vrbridge.settings import (ConfigError, Settings, exposure_ev_rungs,
                               filter_to_range, load_settings, log_unlerp)


def test_defaults_are_the_shipped_values():
    s = Settings()

    # config.py
    c = s.controller
    assert (c.poll_interval, c.v_scroll_step, c.h_scroll_step) == (0.02, 0.35, 0.70)
    assert (c.deadzone, c.max_steps_per_frame) == (0.01, 2)
    assert (c.invert_vscroll, c.invert_hscroll) == (1, 1)
    assert (c.raw_scroll_min_delta, c.long_press_threshold) == (0.0005, 0.40)

    # index_puppet.py
    p = s.puppet
    assert (p.quant_level, p.touch_active_idle_secs) == (3, 0.5)
    assert p.single_touch_mode == "together"
    assert (p.invert_x, p.invert_y, p.float_smooth_tau_secs) == (1, 1, 0.12)

    # The five smooth-scroll values were triplicated verbatim across the three
    # camera mappings. They stay three separate tables because they are scaled
    # differently downstream, but they start from the same numbers.
    for ss in (s.usercamera.smooth_scroll, s.virtuallens.smooth_scroll, s.vrclens.smooth_scroll):
        assert (ss.sensitivity, ss.max_delta) == (0.15, 0.10)
        assert (ss.sticky_abs, ss.sticky_reset_gap) == (0.06, 0.20)
        assert ss.reset_sticky_on_step is True

    # index_usercamera.py -- zoom_max_mm is 300, measured against the live client
    # rather than VRChat's published table, which reads 150 and is wrong.
    u = s.usercamera
    assert (u.zoom_min_mm, u.zoom_max_mm) == (20.0, 300.0)
    assert (u.exposure_min_ev, u.exposure_max_ev) == (-3.0, 3.0)
    assert (u.focaldist_min, u.focaldist_max) == (0.0, 10.0)
    assert (u.aperture_min_f, u.aperture_max_f) == (1.4, 32.0)
    assert u.focaldist_log_eps == 0.10
    assert list(u.zoom_steps_mm) == [20, 22, 26, 30, 35, 45, 55, 70, 85, 105, 135, 200, 300]
    assert list(u.aperture_steps_f) == [1.4, 1.8, 2.2, 2.8, 4.0, 5.6, 8.0, 11.0, 16.0, 22.0, 32.0]

    # index_virtuallens.py
    v = s.virtuallens
    assert (v.focal_min_mm, v.focal_max_mm) == (12.0, 300.0)
    assert (v.fnumber_min, v.fnumber_max) == (1.0, 22.0)
    assert (v.exposure_range_ev, v.aperture_min_x) == (3.0, 0.0001)
    # No press_duration: index_virtuallens latches VirtualLens2_Control rather than
    # pulsing it, so the key would configure nothing. See toggle_drop's docstring.
    assert not hasattr(v, "press_duration")
    assert list(v.zoom_steps_mm) == [12, 16, 20, 24, 28, 35, 50, 70, 85, 105, 135, 200, 300]
    assert list(v.aperture_steps_f) == [1.0, 1.4, 1.8, 2.2, 2.8, 4.0, 5.6, 8.0, 11.0, 16.0, 22.0]

    # index_vrclens.py
    r = s.vrclens
    assert r.press_duration == 0.1
    assert list(r.zoom_steps) == [0.00, 0.12, 0.25, 0.38, 0.50, 0.60, 0.65, 0.75, 0.82, 0.90, 1.00]

    # osc_muteproxy.py / osc_vrcft.py / index_remy.py
    assert s.muteproxy.press_duration == 1.0 / 30
    assert s.vrcft.service_name == "VRCFT"
    assert s.vrcft.avatar_load_delay_secs == 1.0
    assert (s.remy.http_timeout_sec, s.remy.work_queue_maxsize, s.remy.max_retries) == (1.0, 8, 1)
    assert s.remy.target_height == 480
    assert s.remy.resize_on_upload is True


def test_startup_mirrors_name_a_value_not_a_ladder_index():
    """The two VL2 mirrors used to be ZOOM_STEPS_X[5] and APERTURE_STEPS_X[7].

    Naming the optical value instead means a retuned ladder cannot silently move
    the startup position to a different rung.
    """
    v = Settings().virtuallens
    assert v.default_zoom_mm == 35.0      # was zoom ladder index 5
    assert v.default_aperture_f == 8.0    # was aperture ladder index 7
    assert Settings().vrclens.default_zoom == 0.12  # was ZOOM_STEPS[1]


def test_missing_file_is_silent_and_gives_defaults(tmp_path):
    assert load_settings(tmp_path / "absent.toml") == Settings()


def test_a_present_file_overrides_only_what_it_names(tmp_path):
    p = tmp_path / "vrbridge.toml"
    p.write_text("[controller]\ninvert_vscroll = -1\n\n[usercamera.smooth_scroll]\nsensitivity = 0.3\n")
    s = load_settings(p)
    assert s.controller.invert_vscroll == -1
    assert s.controller.invert_hscroll == 1           # untouched
    assert s.usercamera.smooth_scroll.sensitivity == 0.3
    assert s.vrclens.smooth_scroll.sensitivity == 0.15  # the other two tables are independent


@pytest.mark.parametrize("body, must_name", [
    ("[controller]\npoll_intervel = 0.05\n", "poll_intervel"),
    ("[contoller]\npoll_interval = 0.05\n", "contoller"),
    ("[virtuallens]\nfocal_min_mm = 50.0\nfocal_max_mm = 50.0\n", "focal_min_mm"),
    ("[virtuallens]\nfocal_min_mm = 0.0\n", "focal_min_mm"),
    ("[virtuallens]\ndefault_zoom_mm = 500.0\n", "default_zoom_mm"),
    ("[vrclens]\nzoom_steps = []\n", "zoom_steps"),
    ("[controller]\ninvert_vscroll = 0\n", "invert_vscroll"),
    ("[controller]\npoll_interval = 'fast'\n", "poll_interval"),
    # Must exceed zoom_max_mm, or it is not a range violation at all.
    ("[usercamera]\nzoom_min_mm = 400.0\n", "zoom_min_mm"),
    # All three below were added in response to review; none was pinned until now.
    ("[vrclens]\nzoom_steps = ['a']\n", "zoom_steps[0]"),
    ("[virtuallens]\naperture_min_x = 1.5\n", "aperture_min_x"),
    ("[usercamera]\nfocaldist_min = -1.0\n", "focaldist_min"),
])
def test_bad_settings_are_refused_and_name_the_key(tmp_path, body, must_name):
    """A refusal has to name the offending key.

    Silently ignoring an unknown key is the failure this repo already paid for:
    three range constants sat in index_usercamera declared and never read, which
    is indistinguishable from a setting that does nothing.
    """
    p = tmp_path / "vrbridge.toml"
    p.write_text(body)
    with pytest.raises(ConfigError) as exc:
        load_settings(p)
    assert must_name in str(exc.value)
    # ConfigError's docstring is an invariant: the key *and* the file. A coercion
    # error used to escape above the wrapper that adds the path.
    assert str(p) in str(exc.value), "refusal did not name the settings file"


def test_malformed_toml_is_refused_rather_than_skipped(tmp_path):
    p = tmp_path / "vrbridge.toml"
    p.write_text("[controller\n")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_settings(p)


def test_exposure_ladder_keeps_its_top_rung():
    """int(span / (1/3)) can land one under. Counting in whole steps cannot.

    -3..3 survives the old float division by luck; -9.7..-7.7 did not.
    """
    assert exposure_ev_rungs(-3.0, 3.0, 1 / 3)[-1] == 3.0
    assert len(exposure_ev_rungs(-3.0, 3.0, 1 / 3)) == 19
    assert exposure_ev_rungs(-9.7, -7.7, 1 / 3)[-1] == pytest.approx(-7.7)
    assert int((-7.7 - -9.7) / (1 / 3)) == 5  # the arithmetic that used to drop it


def test_filter_to_range_reports_what_it_dropped():
    kept, dropped = filter_to_range([12, 20, 200, 300], 20.0, 150.0)
    assert kept == [20.0]
    assert dropped == [12.0, 200.0, 300.0]


def test_log_unlerp_spans_its_endpoints():
    assert log_unlerp(20.0, 20.0, 150.0) == 0.0
    assert log_unlerp(150.0, 20.0, 150.0) == 1.0
    assert log_unlerp(1000.0, 20.0, 150.0) == 1.0  # clamps rather than extrapolating
