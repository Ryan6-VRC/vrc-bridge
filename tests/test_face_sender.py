"""`osc_face`: when a blob is sent, against a fake VRChat on loopback.

The datagrams are the recorded fixture or synthetic ones sent to the mapping's real socket, so the
path under test is the production one from `recv` to the client's `/tracking/face/v1` receiver.
"""
import contextlib
import socket
import struct
import threading
import time
from pathlib import Path

import pytest

from vrbridge.engine import VRBridge
from vrbridge.face_blob import BLOB_KEYS
from vrbridge.mappings import FaceSenderMapping, FaceTrackingToggles, VRCFTMapping
from vrbridge.mappings.osc_face import FACE_ADDR, FULL_FACE_ADDR
from vrbridge.settings import FaceSettings, Settings, set_settings

from .fake_vrchat import FakeVRChat
from .test_vrcft import _Bridge
from .test_wardrobe import peer

FIXTURE = Path(__file__).parent / "fixtures" / "alvr_face_docked.bin"
TUNE = FaceSettings(enabled=True, port=0, rate=100.0, presence_timeout_secs=0.3,
                    gate_poll_secs=0.05)


def _payloads():
    data, i, out = FIXTURE.read_bytes(), 0, []
    while i < len(data):
        _, n = struct.unpack_from("<dI", data, i)
        out.append(data[i + 12:i + 12 + n])
        i += 12 + n
    return out


class _Harness:
    def __init__(self, vrc: FakeVRChat, tune: FaceSettings = TUNE):
        self.bridge = VRBridge(enable_steamvr=False, advertise=False, discover=False)
        peer(self.bridge.osc, vrc)
        self.face = FaceSenderMapping(self.bridge, tuning=tune)
        self.face.register()
        self.face.activate()
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(self, payload: bytes, times: int = 1, gap: float = 0.0) -> None:
        for _ in range(times):
            self.tx.sendto(payload, ("127.0.0.1", self.face.port))
            if gap:
                time.sleep(gap)

    def close(self):
        self.face.deactivate()
        self.tx.close()


@pytest.fixture
def vrc():
    with FakeVRChat() as v:
        yield v


@pytest.fixture
def h(vrc):
    harness = _Harness(vrc)
    yield harness
    harness.close()


def _wait(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def test_face_data_on_a_native_avatar_reaches_the_client_as_a_102_byte_blob(vrc, h):
    vrc.set_node(FULL_FACE_ADDR, True)
    for p in _payloads():
        h.send(p)
    assert _wait(lambda: vrc.values_for(FACE_ADDR))
    blob = vrc.values_for(FACE_ADDR)[-1]
    assert isinstance(blob, bytes) and len(blob) == 102


def test_nothing_is_sent_to_an_avatar_without_native_face_tracking(vrc, h):
    vrc.set_node(FULL_FACE_ADDR, False)
    h.send(_payloads()[0], times=30, gap=0.01)
    assert _wait(lambda: h.face._full_face is False)
    time.sleep(0.1)
    assert vrc.values_for(FACE_ADDR) == []


def test_a_404_on_the_flag_reads_as_not_native(vrc, h):
    h.send(_payloads()[0], times=30, gap=0.01)
    assert _wait(lambda: h.face._full_face is False)
    assert vrc.values_for(FACE_ADDR) == []


def test_the_streamed_flag_opens_the_gate_without_a_read(vrc):
    harness = _Harness(vrc, FaceSettings(enabled=True, port=0, rate=100.0, gate_poll_secs=60.0))
    try:
        vrc.node_fault = True                  # every read fails: only the stream can say
        harness.face._on_full_face(None, FULL_FACE_ADDR, True)
        harness.send(_payloads()[0], times=10, gap=0.01)
        assert _wait(lambda: vrc.values_for(FACE_ADDR))
    finally:
        harness.close()


def test_a_failed_read_leaves_the_gate_where_it_was(vrc, h):
    vrc.set_node(FULL_FACE_ADDR, True)
    h.send(_payloads()[0], times=10, gap=0.01)
    assert _wait(lambda: h.face._full_face is True)
    vrc.node_fault = True
    time.sleep(0.2)
    assert h.face._full_face is True


def test_an_avatar_change_rereads_the_flag(vrc, h):
    vrc.set_node(FULL_FACE_ADDR, True)
    h.send(_payloads()[0], times=10, gap=0.01)
    assert _wait(lambda: h.face._full_face is True)
    vrc.set_node(FULL_FACE_ADDR, False)
    h.face._on_avatar_change(None, "/avatar/change", "avtr_other")
    assert _wait(lambda: h.face._full_face is False, timeout=0.5)


def test_zero_length_datagrams_are_not_a_tracking_headset(h):
    h.send(b"", times=20, gap=0.005)
    time.sleep(0.05)
    assert not h.face.source_active()


def test_presence_follows_face_data_and_lapses_after_the_timeout(h):
    h.send(_payloads()[0])
    assert _wait(h.face.source_active)
    assert _wait(lambda: not h.face.source_active(), timeout=1.0)


def test_repeated_identical_datagrams_keep_the_source_present(h):
    p = _payloads()[0]
    h.send(p)
    assert _wait(h.face.source_active)
    h.send(p, times=40, gap=0.01)              # 0.4 s of repeats, past the 0.3 s timeout
    assert h.face.source_active()


def test_sending_stops_when_the_data_stops(vrc, h):
    vrc.set_node(FULL_FACE_ADDR, True)
    h.send(_payloads()[0], times=10, gap=0.01)
    assert _wait(lambda: vrc.values_for(FACE_ADDR))
    assert _wait(lambda: not h.face.source_active(), timeout=1.0)
    n = len(vrc.values_for(FACE_ADDR))
    time.sleep(0.1)
    assert len(vrc.values_for(FACE_ADDR)) == n


def test_a_held_port_leaves_the_sender_off_and_says_so(vrc, caplog):
    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    holder.bind(("127.0.0.1", 0))
    try:
        port = holder.getsockname()[1]
        harness = _Harness(vrc, FaceSettings(enabled=True, port=port))
        try:
            assert harness.face.port is None
            assert harness.face._sock is None
        finally:
            harness.close()
    finally:
        holder.close()


def test_deactivate_closes_the_socket_and_activate_reopens_it(h):
    first = h.face.port
    h.face.deactivate()
    assert h.face._sock is None and not h.face.source_active()
    h.face.activate()
    assert h.face._sock is not None and h.face.port is not None and first is not None


def test_a_read_primes_the_change_filter_so_the_next_streamed_flag_lands(vrc, h):
    """A native avatar left True in the stream's cache; a read then closed the gate. The next
    native avatar's streamed True must open it, not be eaten by the change filter as a repeat."""
    h.bridge.osc.watch(FULL_FACE_ADDR)
    h.bridge.osc._update_cache_and_fire(FULL_FACE_ADDR, True)
    h.send(_payloads()[0], times=10, gap=0.01)          # node absent: the read 404s to False
    assert _wait(lambda: h.face._full_face is False)
    h.bridge.osc._update_cache_and_fire(FULL_FACE_ADDR, True)
    assert h.face._full_face is True


def test_a_read_in_flight_does_not_overwrite_a_streamed_flag(vrc):
    harness = _Harness(vrc, FaceSettings(enabled=True, port=0, rate=100.0, gate_poll_secs=60.0))
    try:
        vrc.set_node(FULL_FACE_ADDR, False)
        with vrc.hold_next_node_get() as gate:
            harness.face._on_avatar_change(None, "/avatar/change", "avtr_next")
            assert gate.wait_until_parked()
            harness.face._on_full_face(None, FULL_FACE_ADDR, True)   # lands while the read waits
            gate.release()
        time.sleep(0.1)
        assert harness.face._full_face is True
    finally:
        harness.close()


def test_data_resuming_with_the_same_bytes_rereads_the_flag(vrc):
    harness = _Harness(vrc, FaceSettings(enabled=True, port=0, rate=100.0,
                                         presence_timeout_secs=0.1, gate_poll_secs=60.0))
    try:
        p = _payloads()[0]
        harness.send(p)
        assert _wait(lambda: len(vrc.node_gets) == 1)       # the source arriving reads once
        assert _wait(lambda: not harness.face.source_active(), timeout=1.0)
        harness.send(p)                                      # back, byte-identical
        assert _wait(lambda: len(vrc.node_gets) == 2, timeout=1.0)
    finally:
        harness.close()


# ---- the toggles -----------------------------------------------------------------------------

class _Source:
    def __init__(self):
        self.active = False

    def source_active(self, now=None):
        return self.active


def test_toggles_follow_the_face_source_not_vrcfts_advertisement():
    """Intended: `osc_vrcft`'s timing and resend rules, asked about face data instead. The fake
    advertises VRCFT running, so a mapping still asking mDNS would send True on the first poll."""
    from vrbridge.settings import VRCFTSettings
    bridge = _Bridge(vrcft_running=True)
    src = _Source()
    m = FaceTrackingToggles(bridge, src, tuning=VRCFTSettings(service_poll_secs=0.01))
    m.register()
    m.activate()
    m.update(1.0)                              # first look records False, sends nothing
    assert bridge.osc.sent == []
    src.active = True
    m.update(2.0)
    assert bridge.osc.sent == [("/avatar/parameters/LipTrackingActive", True),
                               ("/avatar/parameters/EyeTrackingActive", True)]
    assert any("ALVR face data detected" in a[0] for a in bridge.log.infos)
    src.active = False
    m.update(3.0)
    assert bridge.osc.sent[-2:] == [("/avatar/parameters/LipTrackingActive", False),
                                    ("/avatar/parameters/EyeTrackingActive", False)]


def test_toggles_send_true_when_face_data_already_flows_at_startup():
    """ALVR may be streaming before the router starts. VRCFT's first-look rule would record that
    True and never send it; the face source has no discovery latency to wait out."""
    from vrbridge.settings import VRCFTSettings
    bridge = _Bridge()
    src = _Source()
    src.active = True
    m = FaceTrackingToggles(bridge, src, tuning=VRCFTSettings(service_poll_secs=0.01))
    m.register()
    m.activate()
    m.update(1.0)
    assert bridge.osc.sent == [("/avatar/parameters/LipTrackingActive", True),
                               ("/avatar/parameters/EyeTrackingActive", True)]


# ---- the routers -----------------------------------------------------------------------------

@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("router_name", ["default", "camera"])
def test_shipped_routers_register_it_only_when_enabled(router_name, enabled):
    """Intended: off by default; `[face] enabled` registers the sender and its toggles active in every
    shipped router, and the default router then leaves VRCFT's toggles out."""
    from vrbridge.cli import ROUTERS
    set_settings(Settings(face=FaceSettings(enabled=enabled, port=0)))
    router = None
    try:
        bridge = VRBridge(enable_steamvr=False, advertise=False, discover=False)
        router = ROUTERS[router_name](bridge)
        face = router._mappings.get("osc_face")
        toggles = router._mappings.get("osc_face_toggles")
        vrcft = router._mappings.get("osc_vrcft")
        if not enabled:
            assert face is None and toggles is None
            assert (vrcft is not None) == (router_name == "default")
            return
        assert isinstance(face, FaceSenderMapping) and face.enabled
        assert isinstance(toggles, FaceTrackingToggles) and toggles.enabled
        assert vrcft is None
        router.evaluate()
        assert face.enabled and toggles.enabled
    finally:
        if router is not None and "osc_face" in router._mappings:
            router._mappings["osc_face"].deactivate()
        set_settings(None)


def test_a_held_port_keeps_vrcfts_toggles_in_the_default_router():
    """VRCFaceTracking holding the port is the documented interlock: the face sender stays off, so
    its toggles must not replace VRCFT's, which would send both False while VRCFT is the source."""
    from vrbridge.cli import ROUTERS
    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    holder.bind(("127.0.0.1", 0))
    set_settings(Settings(face=FaceSettings(enabled=True, port=holder.getsockname()[1])))
    try:
        bridge = VRBridge(enable_steamvr=False, advertise=False, discover=False)
        router = ROUTERS["default"](bridge)
        assert router._mappings["osc_face"].port is None
        assert "osc_face_toggles" not in router._mappings
        assert isinstance(router._mappings.get("osc_vrcft"), VRCFTMapping)
    finally:
        set_settings(None)
        holder.close()


# ---- the feed ---------------------------------------------------------------------------------

@contextlib.contextmanager
def _flowing(h):
    """Keep the fixture's first datagram arriving every 10 ms, as a worn headset would."""
    stop = threading.Event()

    def run():
        while not stop.is_set():
            h.send(_payloads()[0])
            time.sleep(0.01)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join()


def _byte_at(vrc, key, nth=-1):
    return vrc.values_for(FACE_ADDR)[nth][BLOB_KEYS.index(key)]


def test_set_keys_refuses_a_name_outside_the_blob_and_writes_nothing(h):
    with pytest.raises(ValueError, match="NotAKey"):
        h.face.set_keys({"SoftPalateClose": 1.0, "NotAKey": 1.0})
    assert h.face._overrides == {}
    with pytest.raises(ValueError, match="NotAKey"):
        h.face.clear_keys(["NotAKey"])


def test_an_override_lands_in_a_key_pack_leaves_empty(vrc, h):
    vrc.set_node(FULL_FACE_ADDR, True)
    h.face.set_keys({"SoftPalateClose": 1.0, "NasalDilationLeft": 0.5, "NeckFlexLeft": 0.0})
    with _flowing(h):
        assert _wait(lambda: vrc.values_for(FACE_ADDR))
    assert _byte_at(vrc, "SoftPalateClose") == 255
    assert _byte_at(vrc, "NasalDilationLeft") == int(0.5 * 255)
    assert _byte_at(vrc, "NeckFlexLeft") == 0


def test_an_override_beats_a_tracked_key_until_it_is_cleared(vrc, h):
    """Intended: priority over the tracker lasts exactly as long as the override is held."""
    vrc.set_node(FULL_FACE_ADDR, True)
    with _flowing(h):
        assert _wait(lambda: len(vrc.values_for(FACE_ADDR)) > 5)
        tracked = _byte_at(vrc, "JawOpen")
        assert tracked > 0                                   # the fixture opens the jaw a little
        h.face.set_keys({"JawOpen": 0.0})                    # 0 is an override like any other
        assert _wait(lambda: _byte_at(vrc, "JawOpen") == 0)
        h.face.clear_keys(["JawOpen"])
        assert _wait(lambda: _byte_at(vrc, "JawOpen") == tracked)


def test_an_override_skips_the_filter(vrc, h):
    """At 100 Hz the One-Euro filter takes several ticks over a step; an override is on the wire
    whole from the first blob that copied it. The blob after the set may have copied before it."""
    vrc.set_node(FULL_FACE_ADDR, True)
    with _flowing(h):
        assert _wait(lambda: vrc.values_for(FACE_ADDR))
        h.face.set_keys({"MouthClosed": 1.0})
        n = len(vrc.values_for(FACE_ADDR))
        assert _wait(lambda: len(vrc.values_for(FACE_ADDR)) >= n + 2)
    assert _byte_at(vrc, "MouthClosed", n + 1) == 255


def test_nothing_is_sent_while_the_gate_is_closed_and_a_held_override_travels_once_it_opens(vrc, h):
    vrc.set_node(FULL_FACE_ADDR, False)
    with _flowing(h):
        assert _wait(lambda: h.face._full_face is False)
        h.face.set_keys({"SoftPalateClose": 1.0})
        time.sleep(0.1)
        assert vrc.values_for(FACE_ADDR) == []
        vrc.set_node(FULL_FACE_ADDR, True)                   # so the next poll agrees
        h.face._on_full_face(None, FULL_FACE_ADDR, True)
        assert _wait(lambda: vrc.values_for(FACE_ADDR))
    assert _byte_at(vrc, "SoftPalateClose", 0) == 255
