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

from vrbridge.osc_manager import OSCManager

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

    VRChat keeps its service name across a restart and returns on a fresh OSC port.
    Rank-comparing that republication ties, and a tie used to be refused -- leaving
    every send going to the port the previous run had abandoned.
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
