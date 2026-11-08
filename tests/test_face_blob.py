"""`face_blob`: ALVR's face datagram decoded, mapped, corrected, smoothed and packed.

The key order is VRChat's published table and the mapping is VRCFT-ALVR's, both externally sourced,
so these tests pin them verbatim (`design.md` §The warrant criterion): the expected values below
were read from those sources, not computed by the module under test. The fixture is 60 datagrams
ALVR sent from a docked Quest Pro nobody was wearing, so it proves the wire format and nothing about
a face.
"""
import math
import random
import struct
from pathlib import Path

import pytest

from vrbridge import face_blob as fb

FIXTURE = Path(__file__).parent / "fixtures" / "alvr_face_docked.bin"

# creators.vrchat.com/avatars/face-tracking/osc, the tracking key order, index 0..101.
VRCHAT_KEY_ORDER = [
    "EyeLookOutRight", "EyeLookInRight", "EyeLookUpRight", "EyeLookDownRight",
    "EyeLookOutLeft", "EyeLookInLeft", "EyeLookUpLeft", "EyeLookDownLeft",
    "EyeClosedRight", "EyeClosedLeft", "EyeDilationRight", "EyeDilationLeft",
    "EyeConstrictRight", "EyeConstrictLeft", "EyeSquintRight", "EyeSquintLeft",
    "EyeWideRight", "EyeWideLeft", "BrowPinchRight", "BrowPinchLeft",
    "BrowLowererRight", "BrowLowererLeft", "BrowInnerUpRight", "BrowInnerUpLeft",
    "BrowOuterUpRight", "BrowOuterUpLeft", "NasalDilationRight", "NasalDilationLeft",
    "NasalConstrictRight", "NasalConstrictLeft", "CheekSquintRight", "CheekSquintLeft",
    "CheekPuffRight", "CheekPuffLeft", "CheekSuckRight", "CheekSuckLeft",
    "JawOpen", "JawRight", "JawLeft", "JawForward", "JawBackward", "JawClench",
    "JawMandibleRaise", "MouthClosed", "LipSuckUpperRight", "LipSuckUpperLeft",
    "LipSuckLowerRight", "LipSuckLowerLeft", "LipSuckCornerRight", "LipSuckCornerLeft",
    "LipFunnelUpperRight", "LipFunnelUpperLeft", "LipFunnelLowerRight", "LipFunnelLowerLeft",
    "LipPuckerUpperRight", "LipPuckerUpperLeft", "LipPuckerLowerRight", "LipPuckerLowerLeft",
    "MouthUpperUpRight", "MouthUpperUpLeft", "MouthUpperDeepenRight", "MouthUpperDeepenLeft",
    "NoseSneerRight", "NoseSneerLeft", "MouthLowerDownRight", "MouthLowerDownLeft",
    "MouthUpperRight", "MouthUpperLeft", "MouthLowerRight", "MouthLowerLeft",
    "MouthCornerPullRight", "MouthCornerPullLeft", "MouthCornerSlantRight", "MouthCornerSlantLeft",
    "MouthFrownRight", "MouthFrownLeft", "MouthStretchRight", "MouthStretchLeft",
    "MouthDimpleRight", "MouthDimpleLeft", "MouthRaiserUpper", "MouthRaiserLower",
    "MouthPressRight", "MouthPressLeft", "MouthTightenerRight", "MouthTightenerLeft",
    "TongueOut", "TongueUp", "TongueDown", "TongueRight", "TongueLeft", "TongueRoll",
    "TongueBendDown", "TongueCurlUp", "TongueSquish", "TongueFlat",
    "TongueTwistRight", "TongueTwistLeft", "SoftPalateClose", "ThroatSwallow",
    "NeckFlexRight", "NeckFlexLeft",
]

# VRCFT-ALVR `FbFaceTracking.cs`: the blob keys each FB weight drives to full on its own, every other
# weight at 0. A weight absent here drives nothing alone (gaze comes from the quaternions, and the
# tongue beyond TongueOut is not mapped). LipsToward is absent because the corrector caps MouthClosed
# at JawOpen, which is 0 here.
SOLO = {
    "BrowLowererL": {"BrowPinchLeft", "BrowLowererLeft"},
    "BrowLowererR": {"BrowPinchRight", "BrowLowererRight"},
    "CheekPuffL": {"CheekPuffLeft"}, "CheekPuffR": {"CheekPuffRight"},
    "CheekRaiserL": {"CheekSquintLeft"}, "CheekRaiserR": {"CheekSquintRight"},
    "CheekSuckL": {"CheekSuckLeft"}, "CheekSuckR": {"CheekSuckRight"},
    "ChinRaiserB": {"MouthRaiserLower"}, "ChinRaiserT": {"MouthRaiserUpper"},
    "DimplerL": {"MouthDimpleLeft"}, "DimplerR": {"MouthDimpleRight"},
    "EyesClosedL": {"EyeClosedLeft"}, "EyesClosedR": {"EyeClosedRight"},
    "InnerBrowRaiserL": {"BrowInnerUpLeft"}, "InnerBrowRaiserR": {"BrowInnerUpRight"},
    "JawDrop": {"JawOpen"}, "JawSidewaysLeft": {"JawLeft"}, "JawSidewaysRight": {"JawRight"},
    "JawThrust": {"JawForward"},
    "LidTightenerL": {"EyeSquintLeft"}, "LidTightenerR": {"EyeSquintRight"},
    "LipCornerDepressorL": {"MouthFrownLeft"}, "LipCornerDepressorR": {"MouthFrownRight"},
    "LipCornerPullerL": {"MouthCornerPullLeft", "MouthCornerSlantLeft"},
    "LipCornerPullerR": {"MouthCornerPullRight", "MouthCornerSlantRight"},
    "LipFunnelerLB": {"LipFunnelLowerLeft"}, "LipFunnelerLT": {"LipFunnelUpperLeft"},
    "LipFunnelerRB": {"LipFunnelLowerRight"}, "LipFunnelerRT": {"LipFunnelUpperRight"},
    "LipPressorL": {"MouthPressLeft"}, "LipPressorR": {"MouthPressRight"},
    "LipPuckerL": {"LipPuckerUpperLeft", "LipPuckerLowerLeft"},
    "LipPuckerR": {"LipPuckerUpperRight", "LipPuckerLowerRight"},
    "LipStretcherL": {"MouthStretchLeft"}, "LipStretcherR": {"MouthStretchRight"},
    "LipSuckLB": {"LipSuckLowerLeft"}, "LipSuckLT": {"LipSuckUpperLeft"},
    "LipSuckRB": {"LipSuckLowerRight"}, "LipSuckRT": {"LipSuckUpperRight"},
    "LipTightenerL": {"MouthTightenerLeft"}, "LipTightenerR": {"MouthTightenerRight"},
    "LowerLipDepressorL": {"MouthLowerDownLeft"}, "LowerLipDepressorR": {"MouthLowerDownRight"},
    "MouthLeft": {"MouthUpperLeft", "MouthLowerLeft"},
    "MouthRight": {"MouthUpperRight", "MouthLowerRight"},
    "NoseWrinklerL": {"NoseSneerLeft"}, "NoseWrinklerR": {"NoseSneerRight"},
    "OuterBrowRaiserL": {"BrowOuterUpLeft"}, "OuterBrowRaiserR": {"BrowOuterUpRight"},
    "UpperLidRaiserL": {"EyeWideLeft"}, "UpperLidRaiserR": {"EyeWideRight"},
    "UpperLipRaiserL": {"MouthUpperUpLeft", "MouthUpperDeepenLeft"},
    "UpperLipRaiserR": {"MouthUpperUpRight", "MouthUpperDeepenRight"},
    "TongueOut": {"TongueOut"},
}

# Keys no ALVR input ever writes. Pinned because another sender may own some of them (a puppet
# channel riding the dilation, constriction and soft-palate keys reads 0 as rest), so a mapping
# change that starts writing one is a contract change, not a refinement.
NEVER_WRITTEN = {
    "EyeDilationRight", "EyeDilationLeft", "EyeConstrictRight", "EyeConstrictLeft",
    "NasalDilationRight", "NasalDilationLeft", "NasalConstrictRight", "NasalConstrictLeft",
    "JawBackward", "JawClench", "JawMandibleRaise", "LipSuckCornerRight", "LipSuckCornerLeft",
    "TongueUp", "TongueDown", "TongueRight", "TongueLeft", "TongueRoll", "TongueBendDown",
    "TongueCurlUp", "TongueSquish", "TongueFlat", "TongueTwistRight", "TongueTwistLeft",
    "SoftPalateClose", "ThroatSwallow", "NeckFlexRight", "NeckFlexLeft",
}

KEY = {k: i for i, k in enumerate(VRCHAT_KEY_ORDER)}


def _face_payload(weights) -> bytes:
    return fb.TAG_FACE2 + struct.pack("<70f", *weights)


def _blob_for(**named) -> bytes:
    w = [0.0] * 70
    for name, v in named.items():
        w[fb.FB_NAMES.index(name)] = v
    frame = fb.FaceFrame()
    frame.apply(fb.decode(_face_payload(w)))
    return fb.pack(frame)


def _nonzero(blob: bytes) -> set:
    return {VRCHAT_KEY_ORDER[i] for i, b in enumerate(blob) if b}


def _records(path: Path):
    data, i = path.read_bytes(), 0
    while i < len(data):
        _, n = struct.unpack_from("<dI", data, i)
        yield data[i + 12:i + 12 + n]
        i += 12 + n


# ---- the layout ----------------------------------------------------------------------------

def test_blob_keys_are_vrchats_published_order():
    assert list(fb.BLOB_KEYS) == VRCHAT_KEY_ORDER


def test_fb_names_are_the_seventy_face_tracking2_weights():
    assert len(fb.FB_NAMES) == 70 and len(set(fb.FB_NAMES)) == 70
    assert fb.FB_NAMES[12:14] == ("EyesClosedL", "EyesClosedR")
    assert fb.FB_NAMES[24] == "JawDrop" and fb.FB_NAMES[68] == "TongueOut"


# ---- the mapping ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", fb.FB_NAMES)
def test_each_fb_weight_alone_drives_exactly_its_keys(name):
    blob = _blob_for(**{name: 1.0})
    expected = SOLO.get(name, set())
    assert _nonzero(blob) == expected
    for key in expected:
        assert blob[KEY[key]] == 255


def test_all_zero_weights_pack_an_all_zero_blob():
    assert _blob_for() == bytes(102)


def test_lid_closure_grows_with_lid_tightener():
    # Openness = 1 - clamp(closed + closed * tightener): 0.5 + 0.25 closes to 0.75.
    blob = _blob_for(EyesClosedL=0.5, LidTightenerL=0.5)
    assert blob[KEY["EyeClosedLeft"]] == int(0.75 * 255)
    assert blob[KEY["EyeSquintLeft"]] == int(0.5 * 255)


def test_upper_lip_suck_yields_to_the_upper_lip_raiser():
    # min(1 - 0.5 ** (1/6), 1) from the table, then the corrector scales it by 1 - MouthUpperUp (0.5).
    blob = _blob_for(LipSuckRT=1.0, UpperLipRaiserR=0.5)
    assert blob[KEY["LipSuckUpperRight"]] == int((1 - 0.5 ** (1 / 6)) * 0.5 * 255)
    assert blob[KEY["MouthUpperUpRight"]] == int(0.5 * 255)


def test_nose_wrinkler_takes_its_share_out_of_upper_lip_up():
    blob = _blob_for(UpperLipRaiserL=0.75, NoseWrinklerL=0.25)
    assert blob[KEY["MouthUpperUpLeft"]] == int(0.5 * 255)
    assert blob[KEY["NoseSneerLeft"]] == int(0.25 * 255)


def test_lower_lip_suck_yields_to_the_lower_lip_depressor():
    blob = _blob_for(LipSuckLB=0.8, LowerLipDepressorL=0.5)
    assert blob[KEY["LipSuckLowerLeft"]] == int(0.4 * 255)


def test_mouth_closed_is_capped_at_jaw_open():
    assert _blob_for(LipsToward=0.6, JawDrop=0.3)[KEY["MouthClosed"]] == int(0.3 * 255)
    assert _blob_for(LipsToward=0.2, JawDrop=0.3)[KEY["MouthClosed"]] == int(0.2 * 255)


def test_no_input_ever_writes_the_unmapped_keys():
    rng = random.Random(7)
    frame = fb.FaceFrame()
    for _ in range(200):
        frame.apply(fb.decode(_face_payload([rng.random() for _ in range(70)])))
        blob = fb.pack(frame)
        assert not (_nonzero(blob) & NEVER_WRITTEN)
    assert not (_nonzero(_blob_for(**{n: 1.0 for n in fb.FB_NAMES})) & NEVER_WRITTEN)


# ---- gaze ----------------------------------------------------------------------------------

def _yaw(theta):     # OpenXR: +Y up, -Z forward; a positive turn about +Y looks left
    return (0.0, math.sin(theta / 2), 0.0, math.cos(theta / 2))


def _pitch(phi):     # a positive turn about +X looks up
    return (math.sin(phi / 2), 0.0, 0.0, math.cos(phi / 2))


def _eyes_blob(left, right) -> bytes:
    frame = fb.FaceFrame()
    frame.apply(fb.decode(fb.TAG_EYES + struct.pack("<8f", *left, *right)))
    return fb.pack(frame)


def test_looking_left_turns_the_right_eye_in_and_the_left_eye_out():
    blob = _eyes_blob(_yaw(0.3), _yaw(0.3))
    assert _nonzero(blob) == {"EyeLookInRight", "EyeLookOutLeft"}
    assert blob[KEY["EyeLookInRight"]] == blob[KEY["EyeLookOutLeft"]] == int(0.3 * 255)


def test_looking_up_raises_both_eyes():
    # 0.21, not 0.2: 0.2 * 255 sits exactly on a byte boundary, and the quaternion's f32 rounding
    # lands it a hair under, which truncation (correctly) takes to 50.
    blob = _eyes_blob(_pitch(0.21), _pitch(0.21))
    assert _nonzero(blob) == {"EyeLookUpRight", "EyeLookUpLeft"}
    assert blob[KEY["EyeLookUpRight"]] == int(0.21 * 255)


def test_converging_eyes_each_turn_in():
    blob = _eyes_blob(_yaw(-0.2), _yaw(0.2))   # left eye turns right, right eye turns left
    assert _nonzero(blob) == {"EyeLookInLeft", "EyeLookInRight"}


def test_a_combined_quaternion_drives_both_eyes():
    frame = fb.FaceFrame()
    frame.apply(fb.decode(fb.TAG_COMB + struct.pack("<4f", *_yaw(-0.4))))
    assert frame.gaze_left == frame.gaze_right
    assert frame.gaze_left[0] == pytest.approx(0.4, abs=1e-6)


def test_a_zero_quaternion_reads_as_straight_ahead():
    assert fb.gaze_from_quat((0.0, 0.0, 0.0, 0.0)) == (0.0, 0.0)


def test_an_eyes_only_datagram_leaves_the_face_where_it_was():
    frame = fb.FaceFrame()
    w = [0.0] * 70
    w[fb.FB_NAMES.index("JawDrop")] = 0.5
    frame.apply(fb.decode(_face_payload(w)))
    frame.apply(fb.decode(fb.TAG_EYES + struct.pack("<8f", *_yaw(0.1), *_yaw(0.1))))
    assert fb.pack(frame)[KEY["JawOpen"]] == int(0.5 * 255)


# ---- decode --------------------------------------------------------------------------------

def test_every_recorded_datagram_decodes_to_both_segments():
    payloads = list(_records(FIXTURE))
    assert len(payloads) == 60
    frame = fb.FaceFrame()
    for p in payloads:
        d = fb.decode(p)
        assert d.unknown is None and d.eyes is not None and len(d.face) == 70
        for q in d.eyes:
            assert math.sqrt(sum(c * c for c in q)) == pytest.approx(1.0, abs=1e-3)
        frame.apply(d)
        assert len(fb.pack(frame)) == 102


def test_the_first_recorded_datagram_reads_as_recorded():
    d = fb.decode(next(_records(FIXTURE)))
    assert d.eyes[0] == pytest.approx((-0.0283, -0.0098, 0.0, 0.9996), abs=1e-4)
    assert d.face[:2] == pytest.approx((0.0258, 0.0093), abs=1e-4)


def test_an_unknown_tag_stops_the_parse_and_keeps_what_came_before():
    p = fb.TAG_EYES + struct.pack("<8f", *_yaw(0.1), *_yaw(0.1)) + b"FacePico" + bytes(40)
    d = fb.decode(p)
    assert d.unknown == b"FacePico" and d.eyes is not None and d.face is None


def test_a_truncated_segment_is_dropped():
    d = fb.decode(fb.TAG_FACE2 + bytes(4 * 69))
    assert d.face is None and d.unknown == fb.TAG_FACE2


def test_an_empty_datagram_carries_nothing():
    d = fb.decode(b"")
    assert not d.has_data and d.unknown is None


# ---- pack ----------------------------------------------------------------------------------

@pytest.mark.parametrize("v, byte", [(0.999, 254), (1.0, 255), (1.7, 255), (-0.2, 0),
                                     (float("nan"), 0), (0.5, 127)])
def test_bytes_truncate_and_clamp_as_vrcfts_cast_does(v, byte):
    frame = fb.FaceFrame()
    frame.shapes[fb.UE_SHAPES.index("JawOpen")] = v
    assert fb.pack(frame)[KEY["JawOpen"]] == byte


# ---- the filter ----------------------------------------------------------------------------

class _VrcftEuro:
    """VRCFaceTracking's `Filter.EuroFilter` as shipped: a fixed 10 Hz period, its defaults."""
    HZ, MIN_CUTOFF, BETA, D_CUTOFF = 10.0, 1.0, 0.5, 0.1

    def __init__(self):
        self.x = self.dx = 0.0

    @staticmethod
    def _alpha(hz, cutoff):
        tau = 1.0 / (2 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / (1.0 / hz))

    def filter(self, x):
        dx = (x - self.x) * self.HZ
        a_d = self._alpha(self.HZ, self.D_CUTOFF)
        self.dx = a_d * dx + (1 - a_d) * self.dx
        a = self._alpha(self.HZ, self.MIN_CUTOFF + self.BETA * abs(self.dx))
        self.x = a * x + (1 - a) * self.x
        return self.x


def test_the_default_filter_reproduces_vrcft_stepped_at_100_hz():
    """VRCFT runs its filter once per 10 ms update request while assuming a 10 Hz period, so the
    real-time filter with every cutoff times ten and beta unchanged is the same filter."""
    from vrbridge.settings import FaceSettings
    s = FaceSettings()
    ours, theirs = fb.OneEuro(), _VrcftEuro()
    rng = random.Random(3)
    x = 0.0
    for _ in range(500):
        x = min(1.0, max(0.0, x + rng.uniform(-0.1, 0.1)))
        assert ours.step(x, 0.01, s.min_cutoff, s.beta, s.d_cutoff) == pytest.approx(
            theirs.filter(x), abs=1e-9)


def test_a_constant_input_settles_on_itself():
    f = fb.OneEuro()
    for _ in range(200):
        y = f.step(0.6, 1 / 60, 10.0, 0.5, 1.0)
    assert y == pytest.approx(0.6, abs=1e-6)


def test_nan_returns_zero_and_leaves_the_filter_alone():
    f = fb.OneEuro()
    f.step(0.5, 0.01, 10.0, 0.0, 1.0)
    before = (f.x, f.dx)
    assert f.step(float("nan"), 0.01, 10.0, 0.0, 1.0) == 0.0
    assert (f.x, f.dx) == before
