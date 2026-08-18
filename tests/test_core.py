"""The headlessly-provable core: the codecs, the smoothers, the ladder walk.

Every test here states the behavior it *intends* before asserting it. The suite
was thin enough that a test written against observed behavior would have frozen
a known defect as expected -- this file exists partly because three such defects
were found in the mappings it covers, so it does not repeat the mistake.

Where a value is externally sourced (an OSCmooth encoding, a VirtualLens2
parameter domain) the test *pins* it rather than verifying it. Pinning is
preservation: it catches a refactor that moves the value, and claims nothing
about whether the value is right.
"""
import math

import pytest

from vrbridge.mappings.index_puppet import (_derive_bool_addrs,
                                            _quant_encode_unit, quant_addr_map)
from vrbridge.mappings.index_virtuallens import (aperture_f_to_x, aperture_ladder, ev_map_x,
                                                 exposure_ladder, zoom_ladder, zoom_mm_to_x)
from vrbridge.settings import Settings, exposure_ev_rungs
from vrbridge.utils import ParamState, SmoothScroller, clamp, clamp01, nearest_index, step_param


class Ctx:
    """A context that accepts everything, recording what it was given."""
    def __init__(self):
        self.sent = []

    def send(self, addr, value):
        self.sent.append((addr, value))
        return True


# --------------------------- OSCmooth float->bool codec ----------------------

def test_derive_bool_addrs_follows_the_oscmooth_naming():
    """Intended: a float address gains a `Negative` sibling and one address per
    magnitude bit, named for the bit's value (1, 2, 4, ...). This is OSCmooth's
    convention, not ours -- pinned, not verified."""
    got = _derive_bool_addrs("/avatar/parameters/IndexPuppet/Left_X", 3)
    assert got["neg"] == "/avatar/parameters/IndexPuppet/Left_XNegative"
    assert got["bits"] == ["/avatar/parameters/IndexPuppet/Left_X1",
                           "/avatar/parameters/IndexPuppet/Left_X2",
                           "/avatar/parameters/IndexPuppet/Left_X4"]


def test_derive_bool_addrs_at_zero_bits_produces_nothing():
    """Intended: quant_level 0 means "float addresses only"."""
    assert _derive_bool_addrs("/a/b", 0) == {"neg": None, "bits": []}


@pytest.mark.parametrize("x, n, expected", [
    (0.0, 3, (False, 0)),
    (1.0, 3, (False, 7)),      # full scale is 2^n - 1
    (-1.0, 3, (True, 7)),
    (0.5, 3, (False, 4)),      # round(0.5 * 7) == 4, not 3: round-half-even on 3.5
    (-0.5, 3, (True, 4)),
    (1.5, 3, (False, 7)),      # clamps rather than overflowing the code
    (-1.5, 3, (True, 7)),
    (0.0, 0, (False, 0)),
])
def test_quant_encode_unit(x, n, expected):
    """Intended: encode [-1,1] as a sign flag plus a magnitude code in [0, 2^n-1],
    clamping out-of-range input."""
    assert _quant_encode_unit(x, n) == expected


def test_quant_encode_has_no_negative_zero():
    """Intended: a value that rounds to code 0 is not also flagged negative --
    otherwise the avatar sees `Negative` high with every magnitude bit low, which
    is a different state from plain zero."""
    neg, k = _quant_encode_unit(-0.01, 3)
    assert (neg, k) == (False, 0)


def test_quant_round_trips_to_within_one_step():
    """Intended: the encoding is a uniform quantizer, so error never exceeds half
    a step. This is what makes it usable as a position readout at all."""
    n = 3
    step = 1.0 / ((1 << n) - 1)
    for i in range(-100, 101):
        x = i / 100
        neg, k = _quant_encode_unit(x, n)
        decoded = (-1 if neg else 1) * k * step
        assert abs(decoded - x) <= step / 2 + 1e-12, x


def test_quant_addr_map_covers_all_four_axes():
    assert set(quant_addr_map(3)) == {
        "/avatar/parameters/IndexPuppet/Left_X", "/avatar/parameters/IndexPuppet/Left_Y",
        "/avatar/parameters/IndexPuppet/Right_X", "/avatar/parameters/IndexPuppet/Right_Y"}


# The float-axis smoother now lives inside `vrbridge.quant_channel.QuantChannel`
# (per-instance state, one instance per address); its semantics -- first-sample
# seat, tau <= 0 passthrough, monotone approach, per-address independence -- are
# pinned in tests/test_quant_channel.py.

# -------------------------------- SmoothScroller -----------------------------

def test_scroller_is_sticky_until_the_threshold_is_crossed():
    """Intended: a brush against the pad emits nothing. Movement is only
    converted to deltas once cumulative travel proves intent."""
    s = SmoothScroller(sensitivity=0.15, max_delta=0.10, sticky_abs=0.06, reset_gap=0.20)
    assert s.on_sample(0.01, when=0.00) == 0.0
    assert s.on_sample(0.01, when=0.01) == 0.0
    assert s.on_sample(0.05, when=0.02) == 0.0      # crosses 0.06, but arms rather than emits
    assert s.on_sample(0.10, when=0.03) != 0.0      # now unstuck


def test_scroller_output_is_sensitivity_times_input_once_unstuck():
    s = SmoothScroller(sensitivity=0.15, max_delta=10.0, sticky_abs=0.0, reset_gap=0.20)
    s.on_sample(0.0, when=0.0)
    assert s.on_sample(0.2, when=0.01) == pytest.approx(0.03)


def test_scroller_clamps_a_spike_in_both_directions():
    """Intended: a tracking glitch must not translate into a full-range jump."""
    s = SmoothScroller(sensitivity=1.0, max_delta=0.10, sticky_abs=0.0, reset_gap=0.20)
    s.on_sample(0.0, when=0.0)
    assert s.on_sample(99.0, when=0.01) == 0.10
    assert s.on_sample(-99.0, when=0.02) == -0.10


def test_scroller_re_sticks_after_a_gap():
    """Intended: a pause longer than reset_gap reads as a finger lift, so the next
    contact has to prove intent again."""
    s = SmoothScroller(sensitivity=1.0, max_delta=1.0, sticky_abs=0.06, reset_gap=0.20)
    s.on_sample(0.1, when=0.0)
    assert s.on_sample(0.1, when=0.01) != 0.0
    assert s.on_sample(0.1, when=5.0) == 0.0        # gap -> sticky again


# ------------------------------ step_param / ladders -------------------------

def test_nearest_index_picks_the_closest_rung():
    assert nearest_index([0.0, 0.5, 1.0], 0.4) == 1
    assert nearest_index([], 0.4) == 0


def test_step_param_walks_and_clamps_at_both_ends():
    """Intended: a step moves one rung from wherever the mirror currently sits,
    and stops at the ends rather than wrapping."""
    ctx = Ctx()
    ladder = [0.0, 0.25, 0.5, 0.75, 1.0]
    st = ParamState("/p", default=0.5)
    step_param(ctx, st, ladder, 1)
    assert st.last == 0.75
    step_param(ctx, st, ladder, 5)
    assert st.last == 1.0        # clamps, does not wrap
    step_param(ctx, st, ladder, -99)
    assert st.last == 0.0


def test_step_param_is_a_no_op_for_zero_or_an_empty_ladder():
    ctx = Ctx()
    st = ParamState("/p", default=0.5)
    step_param(ctx, st, [0.0, 1.0], 0)
    step_param(ctx, st, [], 3)
    assert ctx.sent == []


def test_step_param_snaps_from_an_off_ladder_value():
    """Intended: after a smooth scroll the mirror sits between rungs. A step
    re-anchors to the nearest rung and moves from there."""
    ctx = Ctx()
    ladder = [0.0, 0.5, 1.0]
    st = ParamState("/p", default=0.4)
    step_param(ctx, st, ladder, 1)
    assert st.last == 1.0        # nearest to 0.4 is 0.5, +1 -> 1.0


# ---------------------------------- ParamState -------------------------------

def test_paramstate_ingest_does_not_send():
    """Intended: an inbound OSC update refreshes the mirror only. Echoing it back
    would loop."""
    ctx = Ctx()
    st = ParamState("/p", default=0.0)
    st.ingest(0.7)
    assert st.get() == 0.7
    assert ctx.sent == []


def test_paramstate_ignores_an_empty_osc_argument():
    """Intended: a value-less OSC message is a glitch, not a request to reset."""
    st = ParamState("/p", default=0.0)
    st.ingest(0.7)
    st.ingest(None)
    assert st.get() == 0.7


def test_paramstate_reset_restores_the_default():
    ctx = Ctx()
    st = ParamState("/p", default=0.25)
    st.ingest(0.9)
    assert st.reset() == 0.25
    assert ctx.sent == []
    st.ingest(0.9)
    st.reset(ctx, send=True)
    assert ctx.sent == [("/p", 0.25)]


def test_paramstate_does_not_advance_on_a_refused_send():
    """Intended: the mirror tracks what the avatar has, so a send that never left
    must not move it. Advancing anyway meant a scroll before VRChat was discovered
    walked a ladder nothing received, then jumped the camera on the first send
    that landed."""
    class Refusing:
        def send(self, a, v): return False

    st = ParamState("/p", default=1.0)
    st.set(Refusing(), 9.0)
    assert st.last == 1.0


def test_paramstate_advances_when_send_returns_none():
    """Intended: only an explicit False is a refusal, so a bare test double or an
    embedder's context that returns nothing still behaves as before."""
    class Silent:
        def send(self, a, v): return None

    st = ParamState("/p", default=1.0)
    st.set(Silent(), 9.0)
    assert st.last == 9.0


# ------------------------------- clamp helpers -------------------------------

def test_clamp_helpers():
    assert clamp(5.0) == 1.0 and clamp(-5.0) == -1.0 and clamp(0.25) == 0.25
    assert clamp(5.0, 0.0, 10.0) == 5.0
    assert clamp01(2.0) == 1.0 and clamp01(-1.0) == 0.0


# --------------------- VirtualLens2 parameter encodings ----------------------
# These carry VL2's parameter domains. Pinned, not verified -- the authority is
# VirtualLens2's own documentation, and re-deriving one costs headset time.

def test_vl2_zoom_encoding_spans_its_range():
    """Intended: log-scaled, so equal scroll travel gives equal zoom *ratios*;
    the configured focal ends map to exactly 0 and 1."""
    assert zoom_mm_to_x(12.0, 12.0, 300.0) == 0.0
    assert zoom_mm_to_x(300.0, 12.0, 300.0) == 1.0
    assert zoom_mm_to_x(60.0, 12.0, 300.0) == pytest.approx(
        math.log(5) / math.log(25))


def test_vl2_exposure_encoding_puts_zero_ev_at_the_midpoint():
    """Intended: linear, symmetric, 0 EV at 0.5 -- which is why 0.5 is the
    startup mirror."""
    assert ev_map_x(0.0, 3.0) == 0.5
    assert ev_map_x(-3.0, 3.0) == 0.0
    assert ev_map_x(3.0, 3.0) == 1.0
    assert ev_map_x(-99.0, 3.0) == 0.0      # clamps


def test_vl2_aperture_encoding_is_descending_and_clears_the_infinity_sentinel():
    """Intended: x == 0.0 means Infinity (no blur) in VL2, so the widest finite
    aperture must stay strictly above it. Higher x is a wider aperture, so the
    mapping descends as the f-number climbs."""
    wide = aperture_f_to_x(1.0, 1.0, 22.0, 0.0001)
    narrow = aperture_f_to_x(22.0, 1.0, 22.0, 0.0001)
    assert wide == 1.0
    assert narrow == 0.0001          # floored off the sentinel
    assert narrow > 0.0
    assert aperture_f_to_x(8.0, 1.0, 22.0, 0.0001) < wide


def test_vl2_ladders_are_unchanged_by_the_settings_move():
    """A golden table. Moving ~50 constants into a config file is only safe if the
    derived ladders come out bit-identical; these values were computed from the
    pre-change module-level code."""
    t = Settings().virtuallens
    zoom, dropped = zoom_ladder(t.zoom_steps_mm, t.focal_min_mm, t.focal_max_mm)
    assert dropped == []
    assert zoom[0] == 0.0 and zoom[-1] == 1.0
    assert zoom[5] == pytest.approx(0.3325513222446981, abs=1e-15)   # 35 mm

    aperture, dropped = aperture_ladder(t.aperture_steps_f, t.fnumber_min,
                                        t.fnumber_max, t.aperture_min_x)
    assert dropped == []
    assert aperture[7] == pytest.approx(0.32726852734727363, abs=1e-15)  # f/8
    assert aperture[-3:] == pytest.approx([0.10302470312969822, 0.0001, 0.0])

    exposure, dropped = exposure_ladder(
        exposure_ev_rungs(-t.exposure_range_ev, t.exposure_range_ev, t.exposure_step_ev),
        t.exposure_range_ev)
    assert dropped == [] and len(exposure) == 19
    assert exposure[0] == 0.0 and exposure[9] == 0.5 and exposure[-1] == 1.0


def test_a_narrowed_range_reports_the_rungs_it_drops():
    """Intended: an operator whose VL2 prefab has a narrower focal range should be
    told which rungs went away, not silently handed a shorter ladder."""
    _, dropped = zoom_ladder([12, 16, 20, 300], 20.0, 150.0)
    assert dropped == [12.0, 16.0, 300.0]
