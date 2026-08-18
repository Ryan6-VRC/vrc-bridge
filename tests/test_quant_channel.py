"""The quant-channel codec helper: encoder pins, wire typing, and smoother semantics.

These tests land BEFORE `index_puppet` is refactored onto the helper, deliberately: they pin
the behavior the shipped mapping already has (`docs/design.md`: name the value first, then
move it), so the refactor is checked against the pins rather than against itself.

Intent before test, per `docs/design.md`. Two behaviours here are wire contracts a test
written casually would miss sideways: the float companion must leave the boundary as a
**float object** (an int reaching a declared float parameter is dropped silently on both
venues -- `docs/osc.md`), and the bits leave as plain ints, which is a *valid* bool write
that must not be "fixed" into OSC booleans for symmetry with VRCFT.
"""
import math

import pytest

from vrbridge.quant_channel import (MAX_BITS, ChannelSpec, QuantChannel,
                                    derive_bool_addrs, encode_unit)

ADDR = "/avatar/parameters/QDemo/LX"


# --------------------------------------------------------------------------
# encode_unit -- the exact codebook, pinned
# --------------------------------------------------------------------------

@pytest.mark.parametrize("x, n, expect", [
    (0.0, 3, (False, 0)),
    (1.0, 3, (False, 7)),          # full scale is all bits, exactly
    (-1.0, 3, (True, 7)),
    (5 / 7, 3, (False, 5)),        # code values are exact at the codebook points
    (-5 / 7, 3, (True, 5)),
    (1 / 7, 3, (False, 1)),
    (0.5, 3, (False, 4)),          # 3.5 rounds up: round-half-to-even, pinned as shipped
    (1.0, 1, (False, 1)),
    (1.0, 8, (False, 255)),
])
def test_the_encoder_matches_the_decode_codebook_exactly(x, n, expect):
    """Intended: the avatar decodes x_hat = +-k/(2^n - 1), so the encoder must land on those
    codebook points exactly -- an off-by-one here is a visible step error on every remote."""
    assert encode_unit(x, n) == expect


def test_out_of_range_input_is_clamped_not_wrapped():
    """Intended: a controller glitch past full deflection must read as full deflection.
    Overflowing the code instead would flip high bits and jump the remote to the far side."""
    assert encode_unit(2.0, 3) == (False, 7)
    assert encode_unit(-3.0, 3) == (True, 7)


def test_no_negative_zero_leaves_the_encoder():
    """Intended: `Negative` is suppressed when the code itself is zero, so a decoder of THIS
    sender never needs the sign-with-zero-magnitude special case. (VRCFT's own encoder does
    emit it, which is why the avatar-side decode still handles it structurally -- but that is
    tolerance of a foreign sender, not licence for us to emit it.)"""
    assert encode_unit(-0.0, 3) == (False, 0)
    assert encode_unit(-0.01, 3) == (False, 0), \
        "a magnitude that rounds to 0 must drop its sign with it"


def test_zero_bits_encodes_to_nothing():
    """Intended: bits == 0 is the float-only mode (`index_puppet`'s quant_level=0); the
    encoder has no wire to speak on and must say nothing rather than divide by zero."""
    assert encode_unit(0.7, 0) == (False, 0)
    assert encode_unit(0.7, -1) == (False, 0)


def test_the_promoted_encoder_is_the_shipped_encoder():
    """The pin that licenses the refactor: the helper's encoder and the one `index_puppet`
    ships agree everywhere, checked across the full signed range at every legal width."""
    from vrbridge.mappings.index_puppet import _quant_encode_unit
    for n in range(0, MAX_BITS + 1):
        for i in range(-40, 41):
            x = i / 40 * 1.2          # past full scale on both sides, through the clamp
            assert encode_unit(x, n) == _quant_encode_unit(x, n), (x, n)


# --------------------------------------------------------------------------
# The VRCFT floor-encoder fixture (probe 2's headless half)
#
# VRCFT is the other sender on this wire (`BinaryBaseParameter.cs`): it floors where we
# round -- `k = floor(|x| * 2^n)`, all-bits at >= 0.99999 -- and it sends `Negative` even at
# zero magnitude. Compatibility is the names plus the decode codebook `k/(2^n - 1)`, not
# sender rounding, so these tests synthesize VRCFT's codes and assert what the shared
# decode does with them. No live VRCFT needed.
# --------------------------------------------------------------------------

def vrcft_encode(x: float, n: int):
    """VRCFT's encoder, synthesized: floor, the >=0.99999 all-bits clamp, and
    sign-at-zero-magnitude (`Negative = x < 0` unconditionally)."""
    mag = min(abs(x), 1.0)
    k = (1 << n) - 1 if mag >= 0.99999 else int(mag * (1 << n))
    return (x < 0.0), k


def decode(neg: bool, k: int, n: int) -> float:
    """The avatar-side codebook both senders target: x_hat = +-k/(2^n - 1), with
    sign-with-zero-magnitude reading as zero (which the avatar decode guarantees
    structurally -- its Negative branch selects between +-k subtrees, and k = 0 is 0 in
    both)."""
    if k == 0:
        return 0.0
    return (-1.0 if neg else 1.0) * k / ((1 << n) - 1)


def test_vrcft_codes_decode_within_one_step_with_a_signed_one_sided_code_bias():
    """Intended: the interop claim, with its exact shape. Swept over the full signed range:
    the round-trip error stays within one codebook step 1/(2^n - 1) -- and per *code*, the
    floor-encoder's bin floor k/2^n maps to the strictly-not-smaller magnitude k/(2^n - 1),
    a one-sided upward stretch bounded by that same step. The one-sidedness lives at the
    code points; against a uniformly-sampled x the error is two-sided (a bin's low half
    decodes high, its top can decode low), which is why the assertion sweeps both."""
    n = 3
    step = 1.0 / ((1 << n) - 1)
    for i in range(-2000, 2001):
        x = i / 2000
        x_hat = decode(*vrcft_encode(x, n), n)
        assert abs(x_hat - x) < step, (x, x_hat)
        # The signed one-sided half: the decode never undershoots the magnitude the code
        # was floored FROM. |x_hat| >= k/2^n means the stretch is upward-only.
        neg, k = vrcft_encode(x, n)
        assert abs(x_hat) >= k / (1 << n) - 1e-12, (x, k, x_hat)
        assert abs(x_hat) - k / (1 << n) <= step + 1e-12


def test_vrcft_sign_with_zero_magnitude_decodes_to_zero():
    """Intended: VRCFT sends `Negative` high for any x < 0, including one whose magnitude
    floors to code 0. A decoder that read the sign bit as a value would emit a phantom
    negative step; the codebook decode reads it as zero, and our own encoder never emits
    the state at all (test_no_negative_zero_leaves_the_encoder)."""
    neg, k = vrcft_encode(-0.05, 3)          # floor(0.4) = 0, sign still sent
    assert (neg, k) == (True, 0)
    assert decode(neg, k, 3) == 0.0


def test_vrcft_full_scale_clamps_to_all_bits_and_decodes_exactly():
    """Intended: the >=0.99999 clamp is what makes +-1.0 representable at all under floor
    encoding -- without it, 1.0 would floor to 2^n and overflow the code."""
    assert vrcft_encode(1.0, 3) == (False, 7)
    assert vrcft_encode(-1.0, 3) == (True, 7)
    assert decode(*vrcft_encode(-1.0, 3), 3) == -1.0


# --------------------------------------------------------------------------
# derive_bool_addrs -- the name grammar
# --------------------------------------------------------------------------

def test_bool_addresses_follow_the_power_of_two_grammar():
    """Intended: `<Name>1/2/4...` + `<Name>Negative` is the OSCmooth/VRCFT wire contract --
    the names are the compatibility surface, so they are pinned verbatim."""
    assert derive_bool_addrs(ADDR, 3) == {
        "neg": "/avatar/parameters/QDemo/LXNegative",
        "bits": ["/avatar/parameters/QDemo/LX1",
                 "/avatar/parameters/QDemo/LX2",
                 "/avatar/parameters/QDemo/LX4"],
    }


def test_zero_bits_derives_no_bool_addresses():
    assert derive_bool_addrs(ADDR, 0) == {"neg": None, "bits": []}


def test_the_promoted_derivation_is_the_shipped_derivation():
    from vrbridge.mappings.index_puppet import _derive_bool_addrs
    for n in (0, 1, 3, 8):
        assert derive_bool_addrs(ADDR, n) == _derive_bool_addrs(ADDR, n)


# --------------------------------------------------------------------------
# QuantChannel.send -- wire typing
# --------------------------------------------------------------------------

class Wire:
    """Records (address, value) pairs the way OSCManager.send would receive them."""

    def __init__(self):
        self.sent = []

    def __call__(self, address, value):
        self.sent.append((address, value))

    def values_for(self, address):
        return [v for a, v in self.sent if a == address]


def spec(**kw):
    base = dict(name="QDemo/LX", address=ADDR, bits=3, signed=True, float_tau=0.0)
    base.update(kw)
    return ChannelSpec(**base)


def test_the_float_companion_is_a_float_object_even_for_int_input():
    """Intended: the boundary coercion is load-bearing. `_clamp_unit(0)` is type-preserving,
    so an int 0 survives arithmetic that looks float-typed -- and an int arriving at a
    declared float parameter is dropped silently on both venues (`docs/osc.md`). The
    assertion is on the *type*, which is the only place this defect is visible headlessly."""
    wire = Wire()
    ch = QuantChannel(spec())
    ch.send(wire, 0, now=1.0)          # int input, the hazardous case
    floats = wire.values_for(ADDR)
    assert floats == [0.0]
    assert type(floats[0]) is float, "an int on the float address is silently dropped live"


def test_the_bits_are_plain_ints_and_the_sign_rides_with_them():
    """Intended: ints are a valid bool write on client and emulator alike, and are what the
    shipped mapping has always sent -- pinned so nobody 'fixes' them into OSC booleans.
    `bool` is excluded explicitly because it is an int subclass and would pass an
    isinstance check while changing the wire type tag."""
    wire = Wire()
    ch = QuantChannel(spec())
    ch.send(wire, -5 / 7, now=1.0)
    assert wire.values_for(ADDR + "Negative") == [1]
    assert wire.values_for(ADDR + "1") == [1]
    assert wire.values_for(ADDR + "2") == [0]
    assert wire.values_for(ADDR + "4") == [1]
    for a, v in wire.sent:
        if a != ADDR:
            assert type(v) is int, f"{a} carried {type(v).__name__}, not a plain int"


def test_an_unsigned_channel_never_emits_a_negative_address():
    wire = Wire()
    ch = QuantChannel(spec(signed=False))
    ch.send(wire, 0.5, now=1.0)
    assert not any(a.endswith("Negative") for a, _ in wire.sent)


def test_a_float_only_channel_emits_exactly_the_float():
    """Intended: bits == 0 puts nothing on the bool wire at all -- the float itself is the
    synced carrier there, and stray Negative/bit sends would hit undeclared parameters."""
    wire = Wire()
    ch = QuantChannel(spec(bits=0, signed=False))
    ch.send(wire, 0.25, now=1.0)
    assert wire.sent == [(ADDR, 0.25)]


def test_bits_out_of_range_are_refused_at_construction():
    with pytest.raises(ValueError, match="bits"):
        QuantChannel(spec(bits=MAX_BITS + 1))
    with pytest.raises(ValueError, match="bits"):
        QuantChannel(spec(bits=-1))


# --------------------------------------------------------------------------
# QuantChannel -- smoother semantics (FloatAxisSmoother's, promoted per-instance)
# --------------------------------------------------------------------------

def test_the_bits_snap_while_the_float_eases():
    """Intended: the measured split this helper exists to preserve. Quantized output is raw
    and immediate; the float is low-passed at float_tau. A helper that smoothed the bits
    would lag every remote decode behind the sender's own hand."""
    wire = Wire()
    ch = QuantChannel(spec(float_tau=0.5))
    ch.send(wire, 0.0, now=0.0)              # first sample seats the filter
    ch.send(wire, 1.0, now=0.5)              # dt == tau
    assert wire.values_for(ADDR + "4")[-1] == 1, "bits must snap to the raw value"
    expect = 1.0 - math.exp(-1.0)            # one time-constant step from 0 toward 1
    assert wire.values_for(ADDR)[-1] == pytest.approx(expect)


def test_the_first_sample_seats_the_filter_instead_of_easing_from_zero():
    """Intended: an uninitialized filter adopts its first target outright. Easing from an
    assumed 0 would sweep the remote through half the range on the first touch."""
    wire = Wire()
    ch = QuantChannel(spec(float_tau=0.5))
    ch.send(wire, 0.8, now=10.0)
    assert wire.values_for(ADDR) == [0.8]


def test_tau_zero_is_exact_passthrough():
    """Intended: float_tau <= 0 disables smoothing entirely -- the float tracks the input
    sample for sample, which is the shipped default feel when smoothing is off."""
    wire = Wire()
    ch = QuantChannel(spec(float_tau=0.0))
    for i, x in enumerate((0.3, -0.9, 0.0, 1.0)):
        ch.send(wire, x, now=float(i))
    assert wire.values_for(ADDR) == [0.3, -0.9, 0.0, 1.0]


def test_smoothing_runs_on_the_callers_clock_not_the_wall():
    """Intended: `now` is the event's own timestamp (`evt.when`), never time.time() read
    inside the helper. Driven with a synthetic clock: if the helper consulted the wall,
    dt would be enormous and the output would snap to the target instead of easing."""
    wire = Wire()
    ch = QuantChannel(spec(float_tau=1.0))
    ch.send(wire, 0.0, now=1000.0)           # a clock nowhere near time.time()
    ch.send(wire, 1.0, now=1000.5)           # dt = 0.5 on that clock
    expect = 1.0 - math.exp(-0.5)
    got = wire.values_for(ADDR)[-1]
    assert got == pytest.approx(expect), \
        "the step size must follow the passed clock; a wall-clock read would snap to 1.0"
    assert got < 0.5, "eased, not snapped"


def test_the_float_approaches_the_target_without_overshooting():
    """Intended: a first-order low pass -- each step closes a fraction of the gap,
    monotonically, never past the target."""
    wire = Wire()
    ch = QuantChannel(spec(float_tau=0.12))
    ch.send(wire, 0.0, now=0.0)
    prev, t = 0.0, 0.0
    for _ in range(50):
        t += 0.02
        ch.send(wire, 1.0, now=t)
        v = wire.values_for(ADDR)[-1]
        assert prev <= v <= 1.0
        prev = v
    assert v == pytest.approx(1.0, abs=1e-3)


def test_a_non_advancing_clock_snaps_to_target():
    """Pinned from the shipped smoother: dt <= 0 takes alpha = 1. Two events stamped alike
    are one instant, and holding the old value would freeze the channel on a clock stall."""
    wire = Wire()
    ch = QuantChannel(spec(float_tau=0.5))
    ch.send(wire, 0.0, now=5.0)
    ch.send(wire, 1.0, now=5.0)
    assert wire.values_for(ADDR)[-1] == 1.0


def test_two_instances_do_not_share_filter_state():
    """Intended: smoother state is per **address** -- under `single_touch_mode: "together"`
    one raw value feeds two addresses' independent filters, so the mapping holds one
    instance per address. Sharing state would make the mirror leg double-step."""
    wire = Wire()
    a = QuantChannel(spec(float_tau=0.5))
    b = QuantChannel(spec(name="QDemo/LY", address="/avatar/parameters/QDemo/LY",
                          float_tau=0.5))
    a.send(wire, 1.0, now=0.0)               # a seats at 1.0
    b.send(wire, 0.0, now=0.0)               # b seats at 0.0, unmoved by a
    b.send(wire, 0.0, now=1.0)
    assert wire.values_for("/avatar/parameters/QDemo/LY")[-1] == 0.0
    a.send(wire, 1.0, now=1.0)
    assert wire.values_for(ADDR)[-1] == 1.0


def test_reset_seeds_the_filter_without_emitting():
    """Intended: an avatar-change reset seats the next send's starting point; it must not
    itself put anything on the wire, because the caller owns what (if anything) to send."""
    wire = Wire()
    ch = QuantChannel(spec(float_tau=0.5))
    ch.reset(0.0, now=0.0)
    assert wire.sent == []
    ch.send(wire, 1.0, now=0.5)              # eases from the seeded 0.0, not first-sample
    assert wire.values_for(ADDR)[-1] == pytest.approx(1.0 - math.exp(-1.0))
