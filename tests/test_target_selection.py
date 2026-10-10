"""Which discovered service becomes the send target, and when that choice is revised.

`docs/design.md` §What is proven records discovery as the never-exercised half, and
`tests/fake_vrchat.py` explains why mDNS itself stays unfaked. Target *selection* is
the separable part: `_consider_service` takes a name and a `ServiceInfo` and decides,
so handing it one directly exercises the ranking and the revision rules without
browsing a real network. What is still not covered here is `_BrowserListener` --
whether zeroconf calls those methods when we expect, which only a live run shows.

Intent before test, per `docs/design.md`: each case states the behavior it wants
before asserting it, because the tie rule below was a defect and a test written
against what the code did would have frozen it.
"""
from zeroconf import ServiceInfo

import vrbridge.osc_manager as om
from vrbridge.osc_manager import FETCH_NO_PEER, FETCH_NOT_FOUND, FETCH_PEER_GONE, OSCManager

from .fake_vrchat import FakeVRChat

VRCHAT = "VRChat-Client-161618._oscjson._tcp.local."
VRCFT = "VRCFT._oscjson._tcp.local."


def service(name: str, http_port: int, server: str = "somehost.local.") -> ServiceInfo:
    """A ServiceInfo as the browser would hand it to us, pointed at a fake's HTTP port."""
    return ServiceInfo(
        "_oscjson._tcp.local.", name,
        addresses=[bytes([127, 0, 0, 1])], port=http_port,
        properties={}, server=server,
    )


def test_a_restarted_vrchat_on_a_new_port_is_followed():
    """Intended: a service that republishes under the same name is an update to the
    target we hold, so we re-resolve and follow it.

    A republication under the same name on a fresh OSC port, rank-compared, ties, and a
    tie used to be refused -- leaving every send going to the abandoned port.
    """
    with FakeVRChat() as first, FakeVRChat() as second:
        mgr = OSCManager(advertise=False)
        mgr._consider_service(VRCHAT, service(VRCHAT, first.http_port))
        assert mgr._client_target == ("127.0.0.1", first.osc_port)

        mgr._consider_service(VRCHAT, service(VRCHAT, second.http_port))
        assert mgr._client_target == ("127.0.0.1", second.osc_port), \
            "a restarted VRChat was ignored; sends would go to the dead port"


def test_sends_reach_the_new_port_after_a_restart():
    """Intended: following the restart is not merely bookkeeping -- datagrams land
    at the new target and the abandoned one goes quiet."""
    with FakeVRChat() as first, FakeVRChat() as second:
        mgr = OSCManager(advertise=False)
        mgr._consider_service(VRCHAT, service(VRCHAT, first.http_port))
        mgr._consider_service(VRCHAT, service(VRCHAT, second.http_port))

        assert mgr.send("/input/Voice", 1) is True
        assert second.wait_for_count(1), "nothing arrived at the restarted client"
        assert second.messages[0] == ("/input/Voice", 1)
        assert first.messages == [], "a datagram went to the abandoned port"


def test_a_lower_ranked_service_does_not_steal_the_target():
    """Intended: only a strictly better rank unseats an incumbent.

    This is the guard on the rule above. VRCFT advertises the same service type and
    is discovered in ordinary use; `_service_rank` scores it below VRChat precisely
    so it can never become the sink for /input/*.
    """
    with FakeVRChat() as vrc, FakeVRChat() as vrcft:
        mgr = OSCManager(advertise=False)
        mgr._consider_service(VRCHAT, service(VRCHAT, vrc.http_port))
        mgr._consider_service(VRCFT, service(VRCFT, vrcft.http_port))

        assert mgr._client_target == ("127.0.0.1", vrc.osc_port)
        assert mgr._current_service_name == VRCHAT


def test_vrchat_takes_the_target_from_a_lesser_service():
    """Intended: a strictly better candidate does take the slot, so an OSC app
    discovered before VRChat does not hold it hostage."""
    with FakeVRChat() as other, FakeVRChat() as vrc:
        mgr = OSCManager(advertise=False)
        mgr._consider_service("SomeOtherApp._oscjson._tcp.local.",
                              service("SomeOtherApp._oscjson._tcp.local.", other.http_port))
        assert mgr._client_target == ("127.0.0.1", other.osc_port)

        mgr._consider_service(VRCHAT, service(VRCHAT, vrc.http_port))
        assert mgr._client_target == ("127.0.0.1", vrc.osc_port)


def test_an_unchanged_republication_does_not_rebuild_the_client():
    """Intended: re-resolving on every update must not churn the socket.

    mDNS republishes on its own refresh schedule. Now that a republication of the
    current target is always re-resolved, an unchanged one has to be recognised and
    dropped, or each refresh would rebuild the UDP client and re-log the selection.
    """
    with FakeVRChat() as vrc:
        mgr = OSCManager(advertise=False)
        mgr._consider_service(VRCHAT, service(VRCHAT, vrc.http_port))
        first_client = mgr._client

        mgr._consider_service(VRCHAT, service(VRCHAT, vrc.http_port))
        assert mgr._client is first_client, "an unchanged republication rebuilt the socket"


def test_a_removed_target_is_replaced_rather_than_held():
    """Intended: losing the target frees the slot, and the next candidate takes it
    even though it ranks no higher than the service that vacated."""
    with FakeVRChat() as first, FakeVRChat() as second:
        mgr = OSCManager(advertise=False)
        mgr._consider_service(VRCHAT, service(VRCHAT, first.http_port))
        listener = OSCManager._BrowserListener(mgr)
        listener.remove_service(None, "_oscjson._tcp.local.", VRCHAT)
        assert mgr._client is None

        mgr._consider_service(VRCHAT, service(VRCHAT, second.http_port))
        assert mgr._client_target == ("127.0.0.1", second.osc_port)


def test_our_own_advertisement_is_never_targeted():
    """Intended: we browse the same service type we advertise on, so the browser
    hands us our own record; sending our own /input/* to ourselves is never right."""
    with FakeVRChat() as vrc:
        mgr = OSCManager(advertise=False)
        mgr._service_info = service("VRBridge._oscjson._tcp.local.", vrc.http_port)
        mgr._consider_service("VRBridge._oscjson._tcp.local.",
                              service("VRBridge._oscjson._tcp.local.", vrc.http_port))
        assert mgr._client is None
        assert mgr._client_target is None


# A service whose HOST_INFO does not yield an OSC port must leave the current target
# standing. Reaching that guard needs a candidate that gets *past* the rank gate --
# either the incumbent itself, or a strictly better rival. A lower-ranked stranger
# never reaches it, so pointing one at a dead port tests nothing.


def test_a_restarted_vrchat_under_a_new_name_replaces_a_dead_incumbent(monkeypatch):
    """Intended: every client launch advertises a new service name, and a killed client
    sends no mDNS goodbye, so the restarted client arrives as a rival of equal rank. When
    the incumbent's OSCQuery no longer answers, the rival takes the slot and a target
    selection fires -- which is what tells persistence and the client-log binding that
    the client restarted. `_host_info` is patched for the dead port, as below, to skip
    its full timeout."""
    restarted = "VRChat-Client-ABCDEF._oscjson._tcp.local."
    with FakeVRChat() as first, FakeVRChat() as second:
        mgr = OSCManager(advertise=False)
        selected = []
        mgr.add_target_listener(selected.append)
        mgr._consider_service(VRCHAT, service(VRCHAT, first.http_port))
        real = OSCManager._host_info
        monkeypatch.setattr(OSCManager, "_host_info", staticmethod(
            lambda host, port: None if port == first.http_port else real(host, port)))
        monkeypatch.setattr(om, "INCUMBENT_RETRY_SECS", 0.0)

        mgr._consider_service(restarted, service(restarted, second.http_port))

        assert mgr._client_target == ("127.0.0.1", second.osc_port)
        assert mgr.current_service_name == restarted
        assert selected == [("127.0.0.1", first.osc_port), ("127.0.0.1", second.osc_port)]


def test_one_missed_probe_of_a_live_incumbent_does_not_hand_over_the_slot(monkeypatch):
    """Intended: a live client's HTTP can hitch once; the incumbent is asked again before a
    rival may displace it, so a second live client does not take the slot for good."""
    rival = "VRChat-Client-ABCDEF._oscjson._tcp.local."
    with FakeVRChat() as first, FakeVRChat() as second:
        mgr = OSCManager(advertise=False)
        mgr._consider_service(VRCHAT, service(VRCHAT, first.http_port))
        real, missed = OSCManager._host_info, []

        def flaky(host, port):
            if port == first.http_port and not missed:
                missed.append(port)
                return None
            return real(host, port)

        monkeypatch.setattr(OSCManager, "_host_info", staticmethod(flaky))
        monkeypatch.setattr(om, "INCUMBENT_RETRY_SECS", 0.0)
        mgr._consider_service(rival, service(rival, second.http_port))

        assert missed, "the incumbent was never probed"
        assert mgr._client_target == ("127.0.0.1", first.osc_port)
        assert mgr.current_service_name == VRCHAT


def test_a_second_live_vrchat_does_not_take_the_slot():
    """Intended: two clients on one PC each advertise; while the incumbent answers, the
    first keeps the slot, as before."""
    rival = "VRChat-Client-ABCDEF._oscjson._tcp.local."
    with FakeVRChat() as first, FakeVRChat() as second:
        mgr = OSCManager(advertise=False)
        mgr._consider_service(VRCHAT, service(VRCHAT, first.http_port))
        mgr._consider_service(rival, service(rival, second.http_port))

        assert mgr._client_target == ("127.0.0.1", first.osc_port)
        assert mgr.current_service_name == VRCHAT


def test_an_incumbent_that_republishes_without_an_osc_port_is_kept():
    """Intended: a republication we cannot resolve leaves the target we already hold.

    Following the incumbent means re-querying it, so its answer is now on a path that
    can fail. Clearing the target on a bad answer would be worse than the tie bug this
    replaced: a live client would be dropped over one malformed reply.
    """
    with FakeVRChat() as vrc, FakeVRChat(host_info={"NAME": "no port here"}) as broken:
        mgr = OSCManager(advertise=False)
        mgr._consider_service(VRCHAT, service(VRCHAT, vrc.http_port))
        mgr._consider_service(VRCHAT, service(VRCHAT, broken.http_port))

        assert mgr._client_target == ("127.0.0.1", vrc.osc_port)
        assert mgr.send("/input/Voice", 1) is True
        assert vrc.wait_for_count(1), "the surviving target stopped receiving"


def test_an_incumbent_whose_oscquery_is_unreachable_is_kept(monkeypatch):
    """Intended: same, for the answer that never arrives rather than the wrong answer.

    This is the restart window itself -- VRChat's mDNS record can republish before its
    OSCQuery HTTP is accepting, and dropping the target there would blank output at the
    exact moment this whole mechanism exists to cover.

    `_host_info` is patched rather than pointed at a closed port because that costs its
    full 2s timeout on Windows, which drops rather than refuses; that path is already
    exercised in test_osc_roundtrip.py.
    """
    with FakeVRChat() as vrc:
        mgr = OSCManager(advertise=False)
        mgr._consider_service(VRCHAT, service(VRCHAT, vrc.http_port))

        monkeypatch.setattr(OSCManager, "_host_info", staticmethod(lambda host, port: None))
        mgr._consider_service(VRCHAT, service(VRCHAT, vrc.http_port + 1))

        assert mgr._client_target == ("127.0.0.1", vrc.osc_port)
        assert mgr.send("/input/Voice", 1) is True
        assert vrc.wait_for_count(1), "the surviving target stopped receiving"


def test_a_better_ranked_candidate_that_does_not_resolve_does_not_displace():
    """Intended: the guard holds from the rival direction too -- outranking the
    incumbent wins the right to be queried, not the target itself."""
    other = "SomeOtherApp._oscjson._tcp.local."
    with FakeVRChat() as app, FakeVRChat(host_info={"NAME": "no port here"}) as broken:
        mgr = OSCManager(advertise=False)
        mgr._consider_service(other, service(other, app.http_port))
        assert mgr._client_target == ("127.0.0.1", app.osc_port)

        mgr._consider_service(VRCHAT, service(VRCHAT, broken.http_port))

        assert mgr._client_target == ("127.0.0.1", app.osc_port)
        assert mgr._current_service_name == other


# A pinned target -- every rule above, refused. Kept in this file and not beside the
# emulator wiring, because the thing that would break the pin is a change to
# _consider_service, and its tests are here.


class _ZcStub:
    """Stands in for the Zeroconf handed to _BrowserListener, which only ever asks it to
    resolve the service it has just announced."""

    def __init__(self, info):
        self._info = info

    def get_service_info(self, stype, name, timeout=None):
        return self._info


def test_a_pinned_target_is_the_target_without_any_discovery():
    """Intended: naming a peer's port is enough to send to it.

    That is the whole capability. The Av3Emulator advertises nothing whatsoever, so no
    amount of browsing ever yields a candidate for it, and every send before this was
    dropped for want of a target.
    """
    with FakeVRChat() as peer:
        mgr = OSCManager(advertise=False, target=("127.0.0.1", peer.osc_port))

        assert mgr._client_target == ("127.0.0.1", peer.osc_port)
        assert mgr.send("/avatar/parameters/Thing", 1.0) is True
        assert peer.wait_for_count(1), "nothing arrived at the pinned peer"
        assert peer.messages[0] == ("/avatar/parameters/Thing", 1.0)


def test_a_live_vrchat_does_not_take_the_slot_from_a_pinned_target():
    """Intended: an explicit target outranks the ranking.

    This is the case the rule exists for. VRChat scores 3, above anything a pin could be
    scored as, and on a machine where a live client and the emulator are both plausibly
    up, a rankable pin would move a run aimed at the emulator onto the real avatar --
    silently, on mDNS callback timing.
    """
    with FakeVRChat() as emulator, FakeVRChat() as vrc:
        mgr = OSCManager(advertise=False, target=("127.0.0.1", emulator.osc_port))

        mgr._consider_service(VRCHAT, service(VRCHAT, vrc.http_port))

        assert mgr._client_target == ("127.0.0.1", emulator.osc_port)
        assert mgr.send("/input/Voice", 1) is True
        assert emulator.wait_for_count(1)
        assert vrc.messages == [], "a datagram reached the discovered client"
        assert mgr._peer_http is None, "a client on another port became the readable peer"
        assert mgr.fetch("/avatar/parameters/Thing").reason == FETCH_NO_PEER


def test_a_pinned_target_reads_the_client_advertising_its_port():
    """Intended: a pin on a VRChat client's OSC port reads that client's tree.

    The two-clients-one-PC case: each bridge is pinned to its own client's ports, and the
    client still advertises OSCQuery, so its HOST_INFO naming the pinned port identifies
    the tree to read. Only the readable peer is set -- the send target is the pin's, and
    no target listener fires, because nothing was selected.
    """
    with FakeVRChat() as vrc:
        mgr = OSCManager(advertise=False, target=("127.0.0.1", vrc.osc_port))
        client = mgr._client
        fired = []
        mgr.add_target_listener(fired.append)
        vrc.set_node("/avatar/parameters/OscWardrobe/Manifest", 7)

        mgr._consider_service(VRCHAT, service(VRCHAT, vrc.http_port))

        result = mgr.fetch("/avatar/parameters/OscWardrobe/Manifest")
        assert result.ok and result.value == 7
        assert result.peer.name == VRCHAT and result.peer.is_vrchat
        assert mgr.current_service_name == VRCHAT
        assert mgr._client is client, "the pinned sender was rebuilt by discovery"
        assert fired == [], "a pin's readable peer was announced as a target selection"


def lan_service(name: str, http_port: int, address: str) -> ServiceInfo:
    """A service whose mDNS address record is `address`, as VRChat advertises its LAN one."""
    return ServiceInfo("_oscjson._tcp.local.", name,
                       addresses=[bytes(int(o) for o in address.split("."))],
                       port=http_port, properties={}, server="somehost.local.")


def test_a_loopback_pin_matches_a_client_advertising_this_machines_lan_address(monkeypatch):
    """Intended: the two-clients-one-PC case. The pin is 127.0.0.1 and VRChat's address
    record is this machine's LAN address; both are this machine, so the port match fires.
    HOST_INFO is answered by the fake on loopback, since the LAN address is made up."""
    with FakeVRChat() as vrc:
        mgr = OSCManager(advertise=False, target=("127.0.0.1", vrc.osc_port))
        monkeypatch.setattr(mgr, "_own_addrs", {"192.168.1.50"})
        real = OSCManager._host_info
        monkeypatch.setattr(OSCManager, "_host_info",
                            staticmethod(lambda host, port: real("127.0.0.1", port)))

        mgr._consider_service(VRCHAT, lan_service(VRCHAT, vrc.http_port, "192.168.1.50"))

        assert mgr._peer_http == ("192.168.1.50", vrc.http_port)
        assert mgr._client_target == ("127.0.0.1", vrc.osc_port)


def test_a_loopback_pin_does_not_match_another_machine(monkeypatch):
    """Intended: a LAN address that is not this machine's is someone else's client, even
    on the pinned port."""
    with FakeVRChat() as vrc:
        mgr = OSCManager(advertise=False, target=("127.0.0.1", vrc.osc_port))
        monkeypatch.setattr(mgr, "_own_addrs", {"192.168.1.50"})
        real = OSCManager._host_info
        monkeypatch.setattr(OSCManager, "_host_info",
                            staticmethod(lambda host, port: real("127.0.0.1", port)))

        mgr._consider_service(VRCHAT, lan_service(VRCHAT, vrc.http_port, "192.168.1.77"))

        assert mgr._peer_http is None


def test_a_pinned_peer_that_moves_off_the_pinned_port_stops_being_read():
    """Intended: the tree we read must be the client we send to. A peer re-resolving onto
    another OSC port is no longer that client, so it is dropped and reads say it went."""
    with FakeVRChat() as vrc:
        mgr = OSCManager(advertise=False, target=("127.0.0.1", vrc.osc_port))
        mgr._consider_service(VRCHAT, service(VRCHAT, vrc.http_port))
        assert mgr._peer_http is not None

        vrc._host_info_override = {"OSC_PORT": vrc.osc_port + 1}
        mgr._consider_service(VRCHAT, service(VRCHAT, vrc.http_port))

        assert mgr._peer_http is None and mgr.current_service_name is None
        assert mgr.fetch("/avatar/parameters/Thing").reason == FETCH_PEER_GONE
        assert mgr._client_target == ("127.0.0.1", vrc.osc_port)


def test_a_restarted_client_on_the_pinned_port_becomes_the_peer():
    """Intended: a client restarted with the same --osc ports advertises under a new name,
    and a killed one sends no goodbye, so the new name on the pinned port takes over."""
    restarted_name = "VRChat-Client-777777._oscjson._tcp.local."
    with FakeVRChat() as first, FakeVRChat() as second:
        second._host_info_override = {"OSC_PORT": first.osc_port}
        mgr = OSCManager(advertise=False, target=("127.0.0.1", first.osc_port))
        mgr._consider_service(VRCHAT, service(VRCHAT, first.http_port))

        mgr._consider_service(restarted_name, service(restarted_name, second.http_port))

        assert mgr._peer_http == ("127.0.0.1", second.http_port)
        assert mgr.current_service_name == restarted_name


def test_a_pinned_target_survives_its_peer_being_removed():
    """Intended: nothing on the discovery side clears a target discovery never set.

    The pin's own client is named in `_current_service_name` once it is the readable
    peer, so this is the removal `remove_service` matches: the peer goes, the send target
    stays, and a read says the peer withdrew.
    """
    with FakeVRChat() as vrc:
        mgr = OSCManager(advertise=False, target=("127.0.0.1", vrc.osc_port))
        listener = OSCManager._BrowserListener(mgr)
        mgr._consider_service(VRCHAT, service(VRCHAT, vrc.http_port))
        assert mgr.current_service_name == VRCHAT

        listener.remove_service(None, "_oscjson._tcp.local.", VRCHAT)

        assert mgr._client_target == ("127.0.0.1", vrc.osc_port)
        assert mgr._client is not None
        assert mgr.send("/input/Voice", 1) is True
        assert mgr.fetch("/avatar/parameters/Thing").reason == FETCH_PEER_GONE


def test_discovery_still_observes_while_pinned():
    """Intended: the pin stops discovery *deciding*, not *observing*.

    `osc_vrcft` asks `is_service_running` whether VRCFaceTracking is up, which reads the
    browser's record of the network and has nothing to do with who receives our sends.
    Suppressing the browser rather than the choice would have broken it.
    """
    with FakeVRChat() as peer, FakeVRChat() as vrcft:
        mgr = OSCManager(advertise=False, target=("127.0.0.1", peer.osc_port))
        listener = OSCManager._BrowserListener(mgr)

        listener.add_service(_ZcStub(service(VRCFT, vrcft.http_port)),
                             "_oscjson._tcp.local.", VRCFT)

        assert mgr.is_service_running("VRCFT")
        assert mgr._client_target == ("127.0.0.1", peer.osc_port)


# --------------------------------------------------------------------------
# Who answered the read
# --------------------------------------------------------------------------
# A fetch's 404 is ambiguous without this: from VRChat it says the worn avatar declares
# no such node, from anything else it says only that we asked a tree with no avatar
# parameters in it. The identity rides on the result rather than being offered as a
# property to ask afterwards, because fetch() drops the lock for the whole GET and the
# rank-3-displaces-rank-1 transient below is exactly when a later question gets a
# different answer than the read did.

def test_a_fetch_names_the_service_that_answered_it():
    """Intended: a caller can attribute the answer, and a VRChat-named service reads as
    VRChat so today's avatar-facing 404 message keeps firing where it is right."""
    with FakeVRChat() as vrc:
        mgr = OSCManager(advertise=False)
        mgr._consider_service(VRCHAT, service(VRCHAT, vrc.http_port))
        vrc.set_node("/avatar/parameters/OscWardrobe/Manifest", 7)

        result = mgr.fetch("/avatar/parameters/OscWardrobe/Manifest")
        assert result.ok
        assert result.peer is not None, "a read that reached a peer named nobody"
        assert result.peer.name == VRCHAT
        assert result.peer.is_vrchat


def test_a_fetch_from_a_stranger_is_marked_as_not_vrchat():
    """Intended: the 404 a stranger returns must be attributable to the stranger.

    VRCFaceTracking and VRCOSC both advertise `_oscjson._tcp` and both serve OSC_PORT in
    their HOST_INFO, so either satisfies `_consider_service` and takes an empty slot until
    a VRChat client is discovered. Neither serves avatar parameters.
    """
    with FakeVRChat() as vrcft:
        mgr = OSCManager(advertise=False)
        mgr._consider_service(VRCFT, service(VRCFT, vrcft.http_port))

        result = mgr.fetch("/avatar/parameters/OscWardrobe/Manifest")
        assert result.reason == FETCH_NOT_FOUND
        assert result.peer is not None
        assert result.peer.name == VRCFT
        assert not result.peer.is_vrchat, \
            "a peer that never claimed to be VRChat was reported as VRChat"


def test_a_stopped_manager_names_nobody_though_it_remembers_the_service():
    """Intended: identity follows the readable endpoint, not the leftover service name.

    The only state where a service name outlives the peer: `stop()` drops `_peer_http` and
    deliberately leaves `_current_service_name` standing, so this is the one case that
    exercises the endpoint half of the guard -- a cold, pinned or withdrawn manager has
    both cleared and would pass whatever the guard said.
    """
    with FakeVRChat() as vrc:
        mgr = OSCManager(advertise=False)
        mgr.start()
        try:
            mgr._consider_service(VRCHAT, service(VRCHAT, vrc.http_port))
            assert mgr.fetch("/avatar/parameters/OscWardrobe/Manifest").peer is not None
        finally:
            mgr.stop()

        assert mgr._current_service_name == VRCHAT, \
            "stop() began clearing the service name, so this no longer tests the guard"
        assert mgr.fetch("/avatar/parameters/OscWardrobe/Manifest").peer is None
