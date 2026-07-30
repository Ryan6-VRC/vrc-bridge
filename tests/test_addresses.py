"""Every OSC address this repo sends or receives, pinned verbatim.

These are externally-sourced facts: they come from VRChat's, VirtualLens2's and
VRCLens's documentation, not from anything derivable here. The design record's
warrant criterion says preserve where the fact is the value -- this file is what
makes that checkable instead of aspirational. A refactor that renames, drops or
typos an address fails here rather than in-headset.

Pinning is not verification. A wrong address stays wrong and stays pinned; the
test only asserts that it has not *changed*. The one address corrected during
this arc (VRChat's Zoom range, not an address) was corrected against the vendor's
published table, which is the only thing that licenses changing a pinned value.
"""
from vrbridge.mappings import index_puppet as puppet
from vrbridge.mappings import index_remy as remy
from vrbridge.mappings import index_usercamera as uc
from vrbridge.mappings import index_virtuallens as vl
from vrbridge.mappings import index_vrclens as vc
from vrbridge.mappings import osc_muteproxy as mute


def test_usercamera_addresses():
    """VRChat's built-in camera endpoints."""
    assert uc.USERC_MODE == "/usercamera/Mode"
    assert uc.USERC_REMOTE_MASK == "/usercamera/RemotePlayer"
    assert uc.USERC_AUTOLEVELROLL == "/usercamera/AutoLevelRoll"
    assert uc.USERC_SHOWFOCUS == "/usercamera/ShowFocus"
    assert uc.USERC_CAPTURE == "/usercamera/Capture"
    assert uc.USERC_ZOOM == "/usercamera/Zoom"
    assert uc.USERC_EXPOSURE == "/usercamera/Exposure"
    assert uc.USERC_FOCALDIST == "/usercamera/FocalDistance"
    assert uc.USERC_APERTURE == "/usercamera/Aperture"
    assert uc.USERC_SCROLL == uc.USERC_ZOOM


def test_virtuallens_addresses():
    """VirtualLens2's expression parameters, under VRChat's /avatar/parameters/ prefix.

    VL2 names these with a space -- `VirtualLens2 Zoom` -- and VRChat's OSC interface
    replaces spaces in a parameter name with underscores, which is why the underscore
    form here is right and matching the prefab's spelling literally would be wrong.
    """
    assert vl.VL2_ZOOM == "/avatar/parameters/VirtualLens2_Zoom"
    assert vl.VL2_SCROLL == "/avatar/parameters/VirtualLens2_Zoom"
    assert vl.VL2_APERTURE == "/avatar/parameters/VirtualLens2_Aperture"
    assert vl.VL2_EXPOSURE == "/avatar/parameters/VirtualLens2_Exposure"
    assert vl.VL2_CONTROL == "/avatar/parameters/VirtualLens2_Control"
    assert vl.VL2_POSITION_MODE == "/avatar/parameters/VirtualLens2_PositionMode"
    assert vl.VL2_AUTOLEVELER == "/avatar/parameters/VirtualLens2_AutoLeveler"
    assert vl.VL2_REMOTE_MASK == "/avatar/parameters/VirtualLens2_RemotePlayerMask"
    assert vl.VL2_AF_MODE == "/avatar/parameters/VirtualLens2_AFMode"


def test_virtuallens_control_codes():
    """Checked against VL2's FX controller: its API state for each code drives the
    target parameter and `Control = 0` on entry, so these fire on the transition into
    the value and the channel self-clears. 12 sets PositionControl to pickup, 13 to
    drop -- which is why toggle_drop latches rather than pulsing."""
    assert (vl.CMD_PICKUP, vl.CMD_DROP) == (12, 13)


def test_vrclens_addresses():
    assert vc.VRCL_ZOOM == "/avatar/parameters/VRCLZoomRadial"
    assert vc.VRCL_SCROLL == "/avatar/parameters/VRCLZoomRadial"
    assert vc.VRCL_TOGGLE == "/avatar/parameters/VRCLFeatureToggle"


def test_vrclens_feature_codes():
    """Opaque command identifiers from VRCLens.

    FEATURE_EXPOSURE_PLUS = 110 looks like it breaks the pattern its neighbour sets --
    aperture is the adjacent pair 192/193, so exposure "should" be 108/109. It is not:
    109 is Exposure Reset. Checked against VRCLens's own expression menus and its FX
    controller, where 108 decrements, 109 resets and 110 increments; "correcting" 110
    to 109 would have wired exposure-increase to exposure-reset. The gap was never a
    gap, and the pattern was never the authority.
    """
    assert vc.FEATURE_DROP == 251
    assert vc.FEATURE_AUTOFOCUS == 13
    assert vc.FEATURE_STABILIZE == 14
    assert vc.FEATURE_PORTRAIT == 222
    assert (vc.FEATURE_APERTURE_MINUS, vc.FEATURE_APERTURE_PLUS) == (192, 193)
    assert (vc.FEATURE_EXPOSURE_MINUS, vc.FEATURE_EXPOSURE_PLUS) == (108, 110)


def test_puppet_addresses_and_derived_booleans():
    assert puppet.LEFT_X_ADDR == "/avatar/parameters/IndexPuppet/Left_X"
    assert puppet.LEFT_Y_ADDR == "/avatar/parameters/IndexPuppet/Left_Y"
    assert puppet.RIGHT_X_ADDR == "/avatar/parameters/IndexPuppet/Right_X"
    assert puppet.RIGHT_Y_ADDR == "/avatar/parameters/IndexPuppet/Right_Y"
    assert puppet.TOUCH_ACTIVE_ADDR == "/avatar/parameters/IndexPuppet/Enable"

    # At the shipped quant level the codec puts 16 more addresses on the wire.
    derived = sorted(a for m in quant_all().values() for a in ([m["neg"]] + m["bits"]))
    assert derived == [
        "/avatar/parameters/IndexPuppet/Left_X1",
        "/avatar/parameters/IndexPuppet/Left_X2",
        "/avatar/parameters/IndexPuppet/Left_X4",
        "/avatar/parameters/IndexPuppet/Left_XNegative",
        "/avatar/parameters/IndexPuppet/Left_Y1",
        "/avatar/parameters/IndexPuppet/Left_Y2",
        "/avatar/parameters/IndexPuppet/Left_Y4",
        "/avatar/parameters/IndexPuppet/Left_YNegative",
        "/avatar/parameters/IndexPuppet/Right_X1",
        "/avatar/parameters/IndexPuppet/Right_X2",
        "/avatar/parameters/IndexPuppet/Right_X4",
        "/avatar/parameters/IndexPuppet/Right_XNegative",
        "/avatar/parameters/IndexPuppet/Right_Y1",
        "/avatar/parameters/IndexPuppet/Right_Y2",
        "/avatar/parameters/IndexPuppet/Right_Y4",
        "/avatar/parameters/IndexPuppet/Right_YNegative",
    ]


def quant_all():
    from vrbridge.settings import Settings
    return puppet.quant_addr_map(Settings().puppet.quant_level)


def test_muteproxy_addresses():
    assert mute.MUTE_PROXY_ADDR == "/avatar/parameters/GestureControl/MuteProxy"
    assert mute.VOICE_INPUT_ADDR == "/input/Voice"


def test_vrcft_addresses():
    from vrbridge.mappings import osc_vrcft as vrcft
    assert vrcft.ACTIVE_PARAMS == {"/avatar/parameters/LipTrackingActive": 1,
                                   "/avatar/parameters/EyeTrackingActive": 1}
    assert vrcft.INACTIVE_PARAMS == {"/avatar/parameters/LipTrackingActive": 0,
                                     "/avatar/parameters/EyeTrackingActive": 0}


def test_remy_addresses():
    assert remy.SELFAUDIO_GRAB_ADDR == "/avatar/parameters/GrabSync/SelfAudio"
    assert remy.GAMEAUDIO_GRAB_ADDR == "/avatar/parameters/GrabSync/GameAudio_IsGrabbed"


def test_router_selector_addresses():
    """The parameters the routers switch on. Two of these duplicate a mapping's
    own constant; the pin catches an edit that moves one copy and not the other."""
    from vrbridge import routers
    assert routers.VIRTUALLENS_ENABLE_ADDR == "/avatar/parameters/VirtualLens2_Enable"
    assert routers.VRCL_FEATURE_TOGGLE_ADDR == vc.VRCL_TOGGLE
    assert routers.USERCAMERA_MODE_ADDR == uc.USERC_MODE
    assert routers.AVATAR_CHANGE_ADDR == "/avatar/change"
