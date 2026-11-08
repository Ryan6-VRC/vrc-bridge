"""ALVR's face datagram to VRChat's `/tracking/face/v1` blob: decode, map, correct, smooth, pack.

Pure functions and small state objects, no sockets; `mappings/osc_face.py` drives them.

**The input** is ALVR's VRCFaceTracking sink (`alvr/server_core/src/tracking/face.rs`, alvr-org/ALVR):
one UDP datagram per tracking packet the headset sends, a run of segments, each an 8-byte ASCII tag
then little-endian f32s. The tags read here are the three a Meta headset produces: `EyesQuat` (left
then right gaze quaternion, x y z w), `CombQuat` (one quaternion for both eyes) and `Face2Fb\\0` (the
70 `XR_FB_face_tracking2` weights). ALVR repeats the latest values on every tracking packet, so most
datagrams are byte-identical to the one before, and a headset with no face tracking (a Quest 3)
produces zero-length datagrams.

**The mapping, the eye conversion and the correctors are ported, not derived**, so the blob is the one
VRCFaceTracking built from the same datagram: `FB_TO_UE` and `gaze_from_quat` from VRCFT-ALVR
(`ALVRModule/FbFaceTracking.cs`, `ALVRModule/EyeTracking.cs`, alvr-org/VRCFT-ALVR, Apache-2.0; NOTICE
carries the attribution), `correct` from VRCFaceTracking's Unified Correctors at its defaults, and
`pack` from VRCFaceTracking's `UnifiedTrackingData.CopyTo`. Their nonlinear terms (the 1/6 power on
upper lip suck, closed times lid tightener) are tuned values: change one only against a live mirror.

**The filter is VRCFaceTracking's One-Euro made time-aware.** VRCFT's `Filter` hardcodes a 10 Hz sample
period while its pipeline runs on a 100 Hz update request, so in real time its defaults (min cutoff 1,
beta 0.5, derivative cutoff 0.1) act as min cutoff 10 Hz, beta 0.5, derivative cutoff 1 Hz, which are
`FaceSettings`' defaults; `tests/test_face_blob.py` proves the equivalence at a 10 ms step.

**The blob** is 102 bytes in VRChat's published key order (`BLOB_KEYS`), each `clamp(v, 0, 1) * 255`
truncated as VRCFT's byte cast truncates. Gaze rides the first eight bytes as half-axes: x positive is
toward the wearer's right and y positive is up, in radians, so 1 rad is full scale. The pupil keys stay
0, since ALVR carries no pupil data.
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from typing import Optional

TAG_EYES = b"EyesQuat"
TAG_COMB = b"CombQuat"
TAG_FACE2 = b"Face2Fb\0"
_TAG_FLOATS = {TAG_EYES: 8, TAG_COMB: 4, TAG_FACE2: 70}
TAG_SIZE = 8

#: `XR_FB_face_tracking2` weight order, as ALVR sends it and VRCFT-ALVR's `FaceFb` enum names it.
FB_NAMES = (
    "BrowLowererL", "BrowLowererR", "CheekPuffL", "CheekPuffR", "CheekRaiserL", "CheekRaiserR",
    "CheekSuckL", "CheekSuckR", "ChinRaiserB", "ChinRaiserT", "DimplerL", "DimplerR",
    "EyesClosedL", "EyesClosedR", "EyesLookDownL", "EyesLookDownR", "EyesLookLeftL", "EyesLookLeftR",
    "EyesLookRightL", "EyesLookRightR", "EyesLookUpL", "EyesLookUpR", "InnerBrowRaiserL",
    "InnerBrowRaiserR", "JawDrop", "JawSidewaysLeft", "JawSidewaysRight", "JawThrust",
    "LidTightenerL", "LidTightenerR", "LipCornerDepressorL", "LipCornerDepressorR",
    "LipCornerPullerL", "LipCornerPullerR", "LipFunnelerLB", "LipFunnelerLT", "LipFunnelerRB",
    "LipFunnelerRT", "LipPressorL", "LipPressorR", "LipPuckerL", "LipPuckerR", "LipStretcherL",
    "LipStretcherR", "LipSuckLB", "LipSuckLT", "LipSuckRB", "LipSuckRT", "LipTightenerL",
    "LipTightenerR", "LipsToward", "LowerLipDepressorL", "LowerLipDepressorR", "MouthLeft",
    "MouthRight", "NoseWrinklerL", "NoseWrinklerR", "OuterBrowRaiserL", "OuterBrowRaiserR",
    "UpperLidRaiserL", "UpperLidRaiserR", "UpperLipRaiserL", "UpperLipRaiserR",
    "TongueTipInterdental", "TongueTipAlveolar", "TongueFrontDorsalPalate", "TongueMidDorsalPalate",
    "TongueBackDorsalVelar", "TongueOut", "TongueRetreat",
)

#: VRCFaceTracking's `UnifiedExpressions` order, which is also blob bytes 14..101.
UE_SHAPES = (
    "EyeSquintRight", "EyeSquintLeft", "EyeWideRight", "EyeWideLeft",
    "BrowPinchRight", "BrowPinchLeft", "BrowLowererRight", "BrowLowererLeft",
    "BrowInnerUpRight", "BrowInnerUpLeft", "BrowOuterUpRight", "BrowOuterUpLeft",
    "NasalDilationRight", "NasalDilationLeft", "NasalConstrictRight", "NasalConstrictLeft",
    "CheekSquintRight", "CheekSquintLeft", "CheekPuffRight", "CheekPuffLeft",
    "CheekSuckRight", "CheekSuckLeft", "JawOpen", "JawRight", "JawLeft", "JawForward",
    "JawBackward", "JawClench", "JawMandibleRaise", "MouthClosed",
    "LipSuckUpperRight", "LipSuckUpperLeft", "LipSuckLowerRight", "LipSuckLowerLeft",
    "LipSuckCornerRight", "LipSuckCornerLeft", "LipFunnelUpperRight", "LipFunnelUpperLeft",
    "LipFunnelLowerRight", "LipFunnelLowerLeft", "LipPuckerUpperRight", "LipPuckerUpperLeft",
    "LipPuckerLowerRight", "LipPuckerLowerLeft", "MouthUpperUpRight", "MouthUpperUpLeft",
    "MouthUpperDeepenRight", "MouthUpperDeepenLeft", "NoseSneerRight", "NoseSneerLeft",
    "MouthLowerDownRight", "MouthLowerDownLeft", "MouthUpperRight", "MouthUpperLeft",
    "MouthLowerRight", "MouthLowerLeft", "MouthCornerPullRight", "MouthCornerPullLeft",
    "MouthCornerSlantRight", "MouthCornerSlantLeft", "MouthFrownRight", "MouthFrownLeft",
    "MouthStretchRight", "MouthStretchLeft", "MouthDimpleRight", "MouthDimpleLeft",
    "MouthRaiserUpper", "MouthRaiserLower", "MouthPressRight", "MouthPressLeft",
    "MouthTightenerRight", "MouthTightenerLeft", "TongueOut", "TongueUp", "TongueDown",
    "TongueRight", "TongueLeft", "TongueRoll", "TongueBendDown", "TongueCurlUp", "TongueSquish",
    "TongueFlat", "TongueTwistRight", "TongueTwistLeft", "SoftPalateClose", "ThroatSwallow",
    "NeckFlexRight", "NeckFlexLeft",
)

EYE_KEYS = (
    "EyeLookOutRight", "EyeLookInRight", "EyeLookUpRight", "EyeLookDownRight",
    "EyeLookOutLeft", "EyeLookInLeft", "EyeLookUpLeft", "EyeLookDownLeft",
    "EyeClosedRight", "EyeClosedLeft", "EyeDilationRight", "EyeDilationLeft",
    "EyeConstrictRight", "EyeConstrictLeft",
)

#: The 102 keys of `/tracking/face/v1`, in byte order.
BLOB_KEYS = EYE_KEYS + UE_SHAPES
BLOB_SIZE = 102
assert len(BLOB_KEYS) == BLOB_SIZE

_FB = {n: i for i, n in enumerate(FB_NAMES)}
_UE = {n: i for i, n in enumerate(UE_SHAPES)}

#: Unified Expression <- FB weight, for every shape VRCFT-ALVR copies straight across. The shapes
#: it computes (upper lip suck, upper lip up and deepen) are in `map_face`.
FB_TO_UE = {
    "EyeSquintRight": "LidTightenerR", "EyeSquintLeft": "LidTightenerL",
    "EyeWideRight": "UpperLidRaiserR", "EyeWideLeft": "UpperLidRaiserL",
    "BrowPinchRight": "BrowLowererR", "BrowPinchLeft": "BrowLowererL",
    "BrowLowererRight": "BrowLowererR", "BrowLowererLeft": "BrowLowererL",
    "BrowInnerUpRight": "InnerBrowRaiserR", "BrowInnerUpLeft": "InnerBrowRaiserL",
    "BrowOuterUpRight": "OuterBrowRaiserR", "BrowOuterUpLeft": "OuterBrowRaiserL",
    "CheekSquintRight": "CheekRaiserR", "CheekSquintLeft": "CheekRaiserL",
    "CheekPuffRight": "CheekPuffR", "CheekPuffLeft": "CheekPuffL",
    "CheekSuckRight": "CheekSuckR", "CheekSuckLeft": "CheekSuckL",
    "JawOpen": "JawDrop", "JawRight": "JawSidewaysRight", "JawLeft": "JawSidewaysLeft",
    "JawForward": "JawThrust", "MouthClosed": "LipsToward",
    "LipSuckLowerRight": "LipSuckRB", "LipSuckLowerLeft": "LipSuckLB",
    "LipFunnelUpperRight": "LipFunnelerRT", "LipFunnelUpperLeft": "LipFunnelerLT",
    "LipFunnelLowerRight": "LipFunnelerRB", "LipFunnelLowerLeft": "LipFunnelerLB",
    "LipPuckerUpperRight": "LipPuckerR", "LipPuckerUpperLeft": "LipPuckerL",
    "LipPuckerLowerRight": "LipPuckerR", "LipPuckerLowerLeft": "LipPuckerL",
    "NoseSneerRight": "NoseWrinklerR", "NoseSneerLeft": "NoseWrinklerL",
    "MouthLowerDownRight": "LowerLipDepressorR", "MouthLowerDownLeft": "LowerLipDepressorL",
    "MouthUpperRight": "MouthRight", "MouthUpperLeft": "MouthLeft",
    "MouthLowerRight": "MouthRight", "MouthLowerLeft": "MouthLeft",
    "MouthCornerPullRight": "LipCornerPullerR", "MouthCornerPullLeft": "LipCornerPullerL",
    "MouthCornerSlantRight": "LipCornerPullerR", "MouthCornerSlantLeft": "LipCornerPullerL",
    "MouthFrownRight": "LipCornerDepressorR", "MouthFrownLeft": "LipCornerDepressorL",
    "MouthStretchRight": "LipStretcherR", "MouthStretchLeft": "LipStretcherL",
    "MouthDimpleRight": "DimplerR", "MouthDimpleLeft": "DimplerL",
    "MouthRaiserUpper": "ChinRaiserT", "MouthRaiserLower": "ChinRaiserB",
    "MouthPressRight": "LipPressorR", "MouthPressLeft": "LipPressorL",
    "MouthTightenerRight": "LipTightenerR", "MouthTightenerLeft": "LipTightenerL",
    "TongueOut": "TongueOut",
}
_COPIES = tuple((_UE[ue], _FB[fb]) for ue, fb in FB_TO_UE.items())

_IDENTITY = (0.0, 0.0, 0.0, 1.0)


# ------------------------------ Decode ---------------------------------------

@dataclass(frozen=True)
class Datagram:
    """One decoded datagram. A field is None when its segment was absent.

    `unknown` is the first tag this module does not read; parsing stops there, since a segment's
    length is known only from its tag, and what was read before it stands.
    """
    eyes: Optional[tuple[tuple[float, ...], tuple[float, ...]]] = None
    face: Optional[tuple[float, ...]] = None
    unknown: Optional[bytes] = None

    @property
    def has_data(self) -> bool:
        return self.eyes is not None or self.face is not None


def decode(payload: bytes) -> Datagram:
    eyes = face = unknown = None
    pos, end = 0, len(payload)
    while end - pos >= TAG_SIZE:
        tag = payload[pos:pos + TAG_SIZE]
        count = _TAG_FLOATS.get(tag)
        if count is None or end - pos - TAG_SIZE < 4 * count:
            unknown = tag
            break
        values = struct.unpack_from(f"<{count}f", payload, pos + TAG_SIZE)
        pos += TAG_SIZE + 4 * count
        if tag == TAG_EYES:
            eyes = (values[:4], values[4:])
        elif tag == TAG_COMB:
            eyes = (values, values)
        else:
            face = values
    return Datagram(eyes=eyes, face=face, unknown=unknown)


# ------------------------------ Map ------------------------------------------

def gaze_from_quat(q) -> tuple[float, float]:
    """(x, y) gaze in radians from an OpenXR quaternion: x positive toward the wearer's right, y up.

    VRCFT-ALVR's formula (its comments call the two angles pitch and yaw, but they land in gaze x and
    y in that order). A zero quaternion reads as straight ahead; the asin argument is clamped, since
    float error on a unit quaternion can step just past 1.
    """
    x, y, z, w = q
    m = math.sqrt(x * x + y * y + z * z + w * w)
    if not m > 0.0:
        x, y, z, w = _IDENTITY
    else:
        x, y, z, w = x / m, y / m, z / m, w / m
    gx = math.asin(max(-1.0, min(1.0, 2.0 * (x * z - w * y))))
    gy = math.atan2(2.0 * (y * z + w * x), w * w - x * x - y * y + z * z)
    return gx, gy


@dataclass
class FaceFrame:
    """The unified state the blob is packed from: 88 shapes, two gazes, two lid opennesses.

    A datagram updates only the parts it carries, as VRCFT's shared state does, so an eyes-only
    datagram leaves the face where the last face segment put it.
    """
    shapes: list = field(default_factory=lambda: [0.0] * len(UE_SHAPES))
    gaze_left: tuple = (0.0, 0.0)
    gaze_right: tuple = (0.0, 0.0)
    open_left: float = 1.0
    open_right: float = 1.0

    def apply(self, dgram: Datagram) -> None:
        if dgram.eyes is not None:
            self.gaze_left = gaze_from_quat(dgram.eyes[0])
            self.gaze_right = gaze_from_quat(dgram.eyes[1])
        if dgram.face is not None:
            map_face(dgram.face, self)

    def copy(self) -> "FaceFrame":
        return FaceFrame(list(self.shapes), self.gaze_left, self.gaze_right,
                         self.open_left, self.open_right)


def map_face(p, frame: FaceFrame) -> None:
    """Write VRCFT-ALVR's mapping of 70 FB weights into `frame`, then the correctors."""
    def clamp01(v):
        return min(1.0, max(0.0, v))

    frame.open_right = 1.0 - clamp01(p[_FB["EyesClosedR"]] * (1.0 + p[_FB["LidTightenerR"]]))
    frame.open_left = 1.0 - clamp01(p[_FB["EyesClosedL"]] * (1.0 + p[_FB["LidTightenerL"]]))
    s = frame.shapes
    for ue, fb in _COPIES:
        s[ue] = p[fb]
    for side in ("Right", "Left"):
        k = side[0]
        raiser = p[_FB[f"UpperLipRaiser{k}"]]
        up = max(0.0, raiser - p[_FB[f"NoseWrinkler{k}"]])
        s[_UE[f"MouthUpperUp{side}"]] = up
        s[_UE[f"MouthUpperDeepen{side}"]] = up
        # A negative raiser (float noise) has no real 1/6 power. VRCFT's Math.Pow answers NaN and the
        # shape packs as 0 for that sample; reading the raiser as 0 keeps the tracked suck instead.
        s[_UE[f"LipSuckUpper{side}"]] = min(1.0 - max(0.0, raiser) ** (1.0 / 6.0),
                                            p[_FB[f"LipSuck{k}T"]])
    correct(frame)


def correct(frame: FaceFrame) -> None:
    """VRCFaceTracking's Unified Correctors at their defaults: MouthClosed capped at JawOpen, and each
    lip suck scaled down by the lip movement that would contradict it. The eyelid blend and the gaze
    symmetrize are off by default and not carried."""
    s = frame.shapes
    s[_UE["MouthClosed"]] = min(s[_UE["MouthClosed"]], s[_UE["JawOpen"]])
    for side in ("Right", "Left"):
        s[_UE[f"LipSuckLower{side}"]] *= 1.0 - s[_UE[f"MouthLowerDown{side}"]]
        s[_UE[f"LipSuckUpper{side}"]] *= 1.0 - s[_UE[f"MouthUpperUp{side}"]]


# ------------------------------ Smooth ---------------------------------------

def _alpha(cutoff: float, dt: float) -> float:
    tau = 1.0 / (2.0 * math.pi * cutoff)
    return 1.0 / (1.0 + tau / dt)


class OneEuro:
    """VRCFaceTracking's `EuroFilter` with the real sample period in place of its fixed 10 Hz.

    The derivative is taken against the previous *filtered* value, as VRCFT's is. A NaN input returns
    0 and leaves the state untouched, as VRCFT's does.
    """
    __slots__ = ("x", "dx")

    def __init__(self):
        self.x = 0.0
        self.dx = 0.0

    def step(self, value: float, dt: float, min_cutoff: float, beta: float, d_cutoff: float) -> float:
        if math.isnan(value):
            return 0.0
        a_d = _alpha(d_cutoff, dt)
        self.dx = a_d * ((value - self.x) / dt) + (1.0 - a_d) * self.dx
        a = _alpha(min_cutoff + beta * abs(self.dx), dt)
        self.x = a * value + (1.0 - a) * self.x
        return self.x


class FaceSmoother:
    """One `OneEuro` per shape, per gaze axis and per lid, under one set of knobs."""

    def __init__(self, min_cutoff: float, beta: float, d_cutoff: float):
        self._knobs = (min_cutoff, beta, d_cutoff)
        self._shapes = [OneEuro() for _ in UE_SHAPES]
        self._eye = [OneEuro() for _ in range(6)]

    def step(self, frame: FaceFrame, dt: float) -> FaceFrame:
        k = self._knobs
        out = FaceFrame([f.step(v, dt, *k) for f, v in zip(self._shapes, frame.shapes)])
        e = self._eye
        out.gaze_left = (e[0].step(frame.gaze_left[0], dt, *k), e[1].step(frame.gaze_left[1], dt, *k))
        out.gaze_right = (e[2].step(frame.gaze_right[0], dt, *k), e[3].step(frame.gaze_right[1], dt, *k))
        out.open_left = e[4].step(frame.open_left, dt, *k)
        out.open_right = e[5].step(frame.open_right, dt, *k)
        return out


# ------------------------------ Pack -----------------------------------------

def _byte(v: float) -> int:
    if not v > 0.0:          # NaN and anything at or below 0
        return 0
    return int(min(v, 1.0) * 255)


def pack(frame: FaceFrame) -> bytes:
    """The 102-byte `/tracking/face/v1` payload for `frame`."""
    b = bytearray(BLOB_SIZE)
    rx, ry = frame.gaze_right
    lx, ly = frame.gaze_left
    b[0], b[1], b[2], b[3] = _byte(rx), _byte(-rx), _byte(ry), _byte(-ry)
    b[4], b[5], b[6], b[7] = _byte(-lx), _byte(lx), _byte(ly), _byte(-ly)
    b[8], b[9] = _byte(1.0 - frame.open_right), _byte(1.0 - frame.open_left)
    for i, v in enumerate(frame.shapes):
        b[len(EYE_KEYS) + i] = _byte(v)
    return bytes(b)
