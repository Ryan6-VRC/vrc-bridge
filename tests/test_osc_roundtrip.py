"""End-to-end against a fake VRChat: HOST_INFO parsing, and datagrams actually landing.

The design record's "what is proven" section is blunt that the handshake works in
daily use but almost nothing about it is exercised. These are the parts a test
can hold without a headset. mDNS discovery is not among them -- browsing a real
network from a test is flaky and would prove nothing pointing at a known port
does not.
"""
import pytest

from vrbridge.engine import CallbackContext
from vrbridge.osc_manager import OSCManager
from vrbridge.utils import ParamState

from .fake_vrchat import FakeVRChat


def test_host_info_is_parsed_from_a_live_endpoint():
    """Intended: read the OSC port VRChat advertises rather than assuming 9000."""
    with FakeVRChat() as vrc:
        hi = OSCManager._host_info("127.0.0.1", vrc.http_port)
        assert hi["OSC_PORT"] == vrc.osc_port


def test_host_info_returns_none_when_nothing_is_listening():
    """Intended: a service that does not answer is skipped, not fatal."""
    import socket
    with socket.socket() as s:          # bind then release, so the port is known-closed
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]
    assert OSCManager._host_info("127.0.0.1", closed_port) is None


def test_the_listener_binds_the_port_it_was_given():
    """Intended: a peer that cannot read our /?HOST_INFO cannot learn a floating port,
    so we take the one it already sends to -- the emulator's fixed 9001.

    A free port is asked for here rather than 9001 itself: the test must not fail
    because something on the machine holds VRChat's conventional port.
    """
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        wanted = s.getsockname()[1]

    mgr = OSCManager(advertise=False, bind_port=wanted)
    try:
        mgr.start()
        assert mgr.osc_port == wanted
    finally:
        mgr.stop()


def test_an_unavailable_bind_port_names_itself_and_the_option():
    """Intended: fail loud. Requesting a port is the one way this bind can fail, and
    the OSError the socket layer raises names neither the port nor who asked for it."""
    holder = OSCManager(advertise=False, bind_port=0)
    holder.start()
    try:
        blocked = OSCManager(advertise=False, bind_port=holder.osc_port)
        with pytest.raises(OSError) as exc:
            blocked.start()
        assert str(holder.osc_port) in str(exc.value)
        assert "--osc-bind-port" in str(exc.value)
        blocked.stop()
    finally:
        holder.stop()


def test_a_pinned_target_and_a_fixed_bind_close_the_loop_both_ways():
    """Intended: the two options together are an end-to-end loop with a peer that
    neither advertises nor discovers -- our sends reach it, its sends reach us.

    Half of this is what the emulator needs and half is what it already does: it
    listens on a port it is told about, and sends to a fixed 127.0.0.1:9001 that it
    will never be talked out of.
    """
    import socket
    import time
    from pythonosc import udp_client

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        inbound = s.getsockname()[1]

    with FakeVRChat() as peer:
        mgr = OSCManager(advertise=False,
                         target=("127.0.0.1", peer.osc_port),
                         bind_port=inbound)
        mgr.start()
        try:
            seen = []
            mgr.set_listener(lambda addr, val: seen.append((addr, val)))
            mgr.watch("/avatar/parameters/Thing")

            assert mgr.send("/avatar/parameters/Thing", 1.0) is True
            assert peer.wait_for_count(1), "outbound never reached the peer"

            # The peer replies to the fixed port it was told about, not to one we
            # advertised -- there is nothing to advertise to.
            udp_client.SimpleUDPClient("127.0.0.1", inbound).send_message(
                "/avatar/parameters/Thing", 0.25)
            deadline = time.monotonic() + 2.0
            while not seen and time.monotonic() < deadline:
                time.sleep(0.01)
            assert seen == [("/avatar/parameters/Thing", pytest.approx(0.25))]
        finally:
            mgr.stop()


@pytest.fixture
def wired():
    """An OSCManager pointed at a fake VRChat, without going through mDNS."""
    with FakeVRChat() as vrc:
        mgr = OSCManager(advertise=False)
        mgr.start()
        from pythonosc import udp_client
        mgr._client = udp_client.SimpleUDPClient("127.0.0.1", vrc.osc_port)
        mgr._client_target = ("127.0.0.1", vrc.osc_port)
        try:
            yield mgr, vrc
        finally:
            mgr.stop()


def test_send_reaches_vrchat(wired):
    mgr, vrc = wired
    assert mgr.send("/input/Voice", 1) is True
    assert vrc.wait_for_count(1), "no datagram arrived"
    assert vrc.messages[0] == ("/input/Voice", 1)


def test_send_without_a_target_is_refused_not_silent(wired):
    """Intended: report the drop so a caller mirroring the value can decline to
    advance its mirror."""
    mgr, _ = wired
    mgr._client = None
    assert mgr.send("/input/Voice", 1) is False


def test_paramstate_set_travels_to_vrchat(wired):
    """Intended: the mapping-level abstraction puts the value on the wire at the
    address it names, and advances its mirror only because the send landed."""
    mgr, vrc = wired
    ctx = CallbackContext(osc=mgr)
    st = ParamState("/avatar/parameters/VirtualLens2_Zoom", default=0.0)
    st.set(ctx, 0.42)
    assert vrc.wait_for_count(1)
    assert vrc.messages[0] == ("/avatar/parameters/VirtualLens2_Zoom", pytest.approx(0.42))
    assert st.last == 0.42


def test_a_mappings_sends_land_at_the_addresses_it_declares(wired):
    """The whole point of the address inventory, exercised rather than asserted:
    drive a real mapping and read back what VRChat actually received."""
    mgr, vrc = wired
    ctx = CallbackContext(osc=mgr)

    from vrbridge.mappings.index_puppet import LEFT_X_ADDR
    from vrbridge.quant_channel import ChannelSpec, QuantChannel
    ch = QuantChannel(ChannelSpec(name="IndexPuppet/Left_X", address=LEFT_X_ADDR,
                                  bits=3, signed=True, float_tau=0.0))
    ch.send(ctx.send, -1.0, now=0.0)

    assert vrc.wait_for_count(5), f"expected sign + 3 bits + float, got {vrc.messages}"
    got = dict(vrc.messages)
    assert got["/avatar/parameters/IndexPuppet/Left_XNegative"] == 1
    assert got["/avatar/parameters/IndexPuppet/Left_X1"] == 1
    assert got["/avatar/parameters/IndexPuppet/Left_X2"] == 1
    assert got["/avatar/parameters/IndexPuppet/Left_X4"] == 1   # -1.0 -> code 7
    assert got["/avatar/parameters/IndexPuppet/Left_X"] == pytest.approx(-1.0)


def test_inbound_osc_updates_a_watched_mirror(wired):
    """Intended: a value VRChat pushes refreshes our cache, and only a *changed*
    value wakes the listener."""
    mgr, _ = wired
    seen = []
    mgr.set_listener(lambda addr, val: seen.append((addr, val)))
    mgr.watch("/avatar/parameters/Thing")

    from pythonosc import udp_client
    client = udp_client.SimpleUDPClient("127.0.0.1", mgr.osc_port)
    client.send_message("/avatar/parameters/Thing", 0.5)

    import time
    deadline = time.monotonic() + 2.0
    while not seen and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen == [("/avatar/parameters/Thing", pytest.approx(0.5))]
    assert mgr.get_cached("/avatar/parameters/Thing") == pytest.approx(0.5)


def test_oscquery_tree_is_a_two_node_constant(wired):
    """Not an endorsement -- a pin on a known gap.

    The design record records that watch() does not reach the served tree: it is
    a hardcoded two-node constant, and the docstring claiming otherwise is false.
    VRChat tolerates it and is the only consumer. This test states the current
    shape so that replacing osc_manager with python-oscquery (plan step 7, held
    behind an operator smoke test) has something concrete to change.
    """
    import json
    import urllib.request
    mgr, _ = wired
    mgr.watch("/avatar/parameters/Thing")
    with urllib.request.urlopen(f"http://127.0.0.1:{mgr.http_port}/", timeout=2) as r:
        tree = json.loads(r.read())
    assert sorted(tree["CONTENTS"]) == ["avatar", "usercamera"]
    assert "Thing" not in json.dumps(tree)
