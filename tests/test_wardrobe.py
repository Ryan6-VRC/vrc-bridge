"""The wardrobe: manifest loading, the OSCQuery single-node read, and the swap mapping.

Intent before test, per `docs/design.md`. Three behaviours here are deliberate designs that a
test written against observed behaviour would freeze backwards, so each states its intent: the
mapping acts on the transition *to* a non-zero slot (not on every change, which is
`osc_muteproxy`'s opposite contract); the marker is read on **every** press rather than cached,
because a cache is only correct while invalidation is provably complete and a read costs a
millisecond; and a repeat of one slot inside 150 ms is a doubled
delivery rather than a second press.

The live client is not needed for any of this. Whether VRChat accepts an inbound
`/avatar/change` is settled by its own patch notes, and what it does with an *ineligible*
avatar id is a client behaviour no fake can answer -- `docs/design.md` rules that "only
provable in a live client" is the ecosystem's property, not a defect here.
"""
import threading
import time
from pathlib import Path

import pytest
from zeroconf import ServiceInfo

import vrbridge.mappings.osc_wardrobe as osc_wardrobe
from vrbridge.engine import VRBridge
from vrbridge.mappings.osc_wardrobe import (AVATAR_CHANGE_ADDR, MARKER_ADDR,
                                            REPEAT_GUARD_SECS, SLOT_ADDR,
                                            WardrobeMapping)
from vrbridge.osc_manager import (FETCH_MALFORMED, FETCH_NO_PEER,
                                  FETCH_NOT_FOUND, FETCH_PEER_GONE,
                                  FETCH_TRANSPORT, OSCManager)
from vrbridge.settings import ConfigError, WardrobeSettings
from vrbridge.wardrobe import discover_manifests, load_manifest

from .fake_vrchat import FakeVRChat

A_ID = "avtr_26187637-0c30-4a09-86e1-bc928c07309e"
B_ID = "avtr_11111111-2222-3333-4444-555555555555"

ONE_MANIFEST = f"""
id = 7

[[slots]]
slot  = 1
label = "a"
id    = "{A_ID}"

[[slots]]
slot  = 3
id    = "{B_ID}"
"""


def write(tmp_path: Path, body: str, name: str = "w.toml") -> Path:
    p = tmp_path / name
    p.write_text(body)
    return p


# --------------------------------------------------------------------------
# Manifest loading
# --------------------------------------------------------------------------

def test_a_manifest_loads_its_id_and_slot_table(tmp_path):
    """Intended: slots are addressed by their declared number, not by file order, so
    reordering the file cannot remap the wearer's buttons."""
    m = load_manifest(write(tmp_path, ONE_MANIFEST))
    assert m.id == 7
    assert m.avatar_for(1).avatar_id == A_ID
    assert m.avatar_for(1).label == "a"
    assert m.avatar_for(3).avatar_id == B_ID
    assert m.avatar_for(2) is None, "a gap must read as absent, not as an error"


@pytest.mark.parametrize("body, expect", [
    (f'id = 900\n[[slots]]\nslot=1\nid="{A_ID}"\n', "1-255"),
    (f'id = 0\n[[slots]]\nslot=1\nid="{A_ID}"\n', "1-255"),
    (f'id = true\n[[slots]]\nslot=1\nid="{A_ID}"\n', "whole number"),
    (f'[[slots]]\nslot=1\nid="{A_ID}"\n', "no 'id'"),
    ("id = 1\n", "no [[slots]]"),
    ("id = 1\nslots = []\n", "one or more"),
    (f'id = 1\n[[slots]]\nslot=0\nid="{A_ID}"\n', "1-8"),
    (f'id = 1\n[[slots]]\nslot=9\nid="{A_ID}"\n', "1-8"),
    (f'id = 1\n[[slots]]\nslot=2\nid="{A_ID}"\n[[slots]]\nslot=2\nid="{B_ID}"\n', "already used"),
    ('id = 1\n[[slots]]\nslot=1\nid="avtr_nope"\n', "not an avatar id"),
    # A TOML multi-line string keeps the trailing newline. `$` matches just before one, so
    # under `.match()` this passed validation and put "avtr_...\n" on the wire -- which
    # VRChat ignores silently, the exact dead button the check exists to turn into an error.
    (f'id = 1\n[[slots]]\nslot=1\nid="""{A_ID}\n"""\n', "not an avatar id"),
    (f'id = 1\nrevision = 2\n[[slots]]\nslot=1\nid="{A_ID}"\n', "unknown key"),
    (f'id = 1\n[[slots]]\nslot=1\nname="x"\nid="{A_ID}"\n', "unknown key"),
])
def test_a_bad_manifest_is_refused_and_says_why(tmp_path, body, expect):
    """Intended: every rejection names the offending key and the file. A manifest is
    hand-edited, and a loader that only says "invalid" leaves the author guessing."""
    p = write(tmp_path, body)
    with pytest.raises(ConfigError) as exc:
        load_manifest(p)
    assert expect in str(exc.value)
    assert p.name in str(exc.value), "the message must name the file"


def test_the_id_range_names_the_reason_it_is_bounded(tmp_path):
    """Intended: the bound is Modular Avatar's inspector clamp, not a choice here, so the
    message routes the author to the substrate rather than looking arbitrary."""
    with pytest.raises(ConfigError) as exc:
        load_manifest(write(tmp_path, f'id = 300\n[[slots]]\nslot=1\nid="{A_ID}"\n'))
    assert "Modular Avatar" in str(exc.value)


def test_two_manifests_claiming_one_id_are_refused_naming_both(tmp_path):
    """Intended: the id is how the worn avatar selects its table, so a collision inside the
    loaded set makes the selection arbitrary. Several *avatars* sharing one id stays legal --
    that is how two avatars share a wardrobe -- which is why this is checked per loaded set
    and not per avatar."""
    d = tmp_path / "set"
    d.mkdir()
    write(d, ONE_MANIFEST, "a.toml")
    write(d, ONE_MANIFEST, "b.toml")
    with pytest.raises(ConfigError) as exc:
        discover_manifests(d)
    assert "a.toml" in str(exc.value) and "b.toml" in str(exc.value)


def test_a_missing_manifest_directory_is_empty_not_an_error(tmp_path):
    """Intended: a user who has not written a wardrobe has none. The mapping reports the
    emptiness when someone activates it, because only then has anyone asked."""
    assert discover_manifests(tmp_path / "absent") == {}


# --------------------------------------------------------------------------
# fetch: the OSCQuery single-node read
# --------------------------------------------------------------------------

def peer(mgr: OSCManager, vrc: FakeVRChat) -> None:
    """Point a manager at a fake's OSC and HTTP endpoints without going through mDNS."""
    from pythonosc import udp_client
    mgr._client = udp_client.SimpleUDPClient("127.0.0.1", vrc.osc_port)
    mgr._client_target = ("127.0.0.1", vrc.osc_port)
    mgr._peer_http = ("127.0.0.1", vrc.http_port)


def test_fetch_reads_a_node_value_and_unwraps_the_array(tmp_path):
    """Intended: callers compare against a scalar. OSCQuery types VALUE as an array, one
    entry per type tag, and a parameter node carries exactly one."""
    with FakeVRChat() as vrc:
        mgr = OSCManager(advertise=False)
        peer(mgr, vrc)
        vrc.set_node(MARKER_ADDR, 7)
        r = mgr.fetch(MARKER_ADDR)
        assert r.ok and r.value == 7


def test_fetch_separates_a_404_from_a_failure():
    """Intended: a 404 is an answer *about the avatar* -- it declares no such parameter --
    and must not read as "ask again". Collapsing both into None is what this exists to
    avoid; the wardrobe treats one as "no wardrobe here" and the other as a retry."""
    with FakeVRChat() as vrc:
        mgr = OSCManager(advertise=False)
        peer(mgr, vrc)
        assert mgr.fetch(MARKER_ADDR).reason == FETCH_NOT_FOUND

        vrc.node_fault = True
        assert mgr.fetch(MARKER_ADDR).reason == FETCH_TRANSPORT


def test_fetch_reports_a_malformed_node():
    """Intended: a peer that answers 200 with something unusable is worth saying once,
    not retrying."""
    with FakeVRChat() as vrc:
        mgr = OSCManager(advertise=False)
        peer(mgr, vrc)
        vrc.node_garbage = "{not json"
        assert mgr.fetch(MARKER_ADDR).reason == FETCH_MALFORMED
        vrc.node_garbage = '{"FULL_PATH": "/x"}'
        assert mgr.fetch(MARKER_ADDR).reason == FETCH_MALFORMED


def test_fetch_percent_encodes_the_address():
    """Intended: the address is data, not URL syntax.

    Spliced in raw, a `#` in a parameter name is stripped client-side as a fragment and the
    GET returns 200 for a *different* node -- a silently wrong answer, which is the one
    outcome this function's named-failure vocabulary cannot express. A space would instead
    report FETCH_TRANSPORT ("ask again") for a node that is there.

    Asserted on the URL rather than through a live server, because a fake would have to
    reproduce the exact fragment handling to make the wrong version fail.
    """
    seen = {}

    class FakeResp:
        def read(self): return b'{"FULL_PATH": "/x", "VALUE": [1]}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    mgr = OSCManager(advertise=False)
    mgr._peer_http = ("127.0.0.1", 9999)

    import urllib.request
    real = urllib.request.urlopen

    def spy(url, timeout=None):
        seen["url"] = url
        return FakeResp()

    urllib.request.urlopen = spy
    try:
        mgr.fetch("/avatar/parameters/Wardrobe#2")
        assert seen["url"].endswith("/avatar/parameters/Wardrobe%232"), seen["url"]
        mgr.fetch("/avatar/parameters/Has Space")
        assert seen["url"].endswith("/avatar/parameters/Has%20Space"), seen["url"]
        # The separators must survive, or every address becomes one flat node name.
        assert "%2F" not in seen["url"]
    finally:
        urllib.request.urlopen = real


def test_fetch_without_a_discovered_peer_says_so():
    """Intended: a pinned target advertises nothing and serves no tree, so there is no one
    to ask -- distinct from asking and being refused."""
    mgr = OSCManager(advertise=False, target=("127.0.0.1", 9000))
    assert mgr.fetch(MARKER_ADDR).reason == FETCH_NO_PEER


def test_a_removed_service_stops_fetch_answering_from_its_tree():
    """Intended: fetch must not keep reading the HTTP endpoint of a peer we have stopped
    sending to and report its answers as the worn avatar's."""
    with FakeVRChat() as vrc:
        mgr = OSCManager(advertise=False)
        name = "VRChat-Client-1._oscjson._tcp.local."
        info = ServiceInfo("_oscjson._tcp.local.", name,
                           addresses=[bytes([127, 0, 0, 1])], port=vrc.http_port,
                           properties={}, server="h.local.")
        mgr._consider_service(name, info)
        vrc.set_node(MARKER_ADDR, 3)
        assert mgr.fetch(MARKER_ADDR).ok

        OSCManager._BrowserListener(mgr).remove_service(
            None, "_oscjson._tcp.local.", name)
        gone = mgr.fetch(MARKER_ADDR)
        assert gone.reason == FETCH_PEER_GONE, \
            "fetch still had a peer after the service backing it went away"
        assert "withdrew" in gone.detail


def test_a_withdrawn_peer_is_not_reported_as_one_never_found():
    """Intended: fetch's named-failure vocabulary separates "nothing was ever there" from
    "what was there went away", because the remedies differ -- the first is answered by
    waiting for discovery, the second only by the client coming back. `target_is_pinned`'s
    docstring promises a caller can tell all three of these apart, and while both withdrawal
    and a cold start answered FETCH_NO_PEER it could tell two."""
    mgr = OSCManager(advertise=False)
    assert mgr.fetch(MARKER_ADDR).reason == FETCH_NO_PEER, "a cold manager lost nothing"

    with FakeVRChat() as vrc:
        name = "VRChat-Client-1._oscjson._tcp.local."
        info = ServiceInfo("_oscjson._tcp.local.", name,
                           addresses=[bytes([127, 0, 0, 1])], port=vrc.http_port,
                           properties={}, server="h.local.")
        mgr._consider_service(name, info)
        OSCManager._BrowserListener(mgr).remove_service(
            None, "_oscjson._tcp.local.", name)
        assert mgr.fetch(MARKER_ADDR).reason == FETCH_PEER_GONE

        # A rediscovery clears it, or one dropped client would mislabel the rest of the run.
        mgr._consider_service(name, info)
        assert mgr.fetch(MARKER_ADDR).reason != FETCH_PEER_GONE


def test_an_unrelated_service_withdrawing_leaves_our_peer_alone():
    """Intended: only the withdrawal of the service backing *our* target says the peer went
    away. VRCFaceTracking closing is an unrelated service removal, and reporting it as our
    peer's departure would send a wardrobe press chasing a client that never left."""
    with FakeVRChat() as vrc:
        mgr = OSCManager(advertise=False)
        name = "VRChat-Client-1._oscjson._tcp.local."
        info = ServiceInfo("_oscjson._tcp.local.", name,
                           addresses=[bytes([127, 0, 0, 1])], port=vrc.http_port,
                           properties={}, server="h.local.")
        mgr._consider_service(name, info)
        vrc.set_node(MARKER_ADDR, 3)

        OSCManager._BrowserListener(mgr).remove_service(
            None, "_oscjson._tcp.local.", "VRCFaceTracking._oscjson._tcp.local.")
        assert mgr.fetch(MARKER_ADDR).ok, "an unrelated removal took our peer down"


def test_a_stopped_manager_stops_answering_from_the_peer_it_served():
    """Intended: a stopped manager must not answer from the tree of a peer it no longer serves
    -- the rule remove_service already states -- and it must not claim that peer withdrew,
    because tearing down our own end is not a fact about the network. A caller told "press
    again once VRChat is rediscovered" about a client that never left is worse off than one
    told there is no peer."""
    with FakeVRChat() as vrc:
        mgr = OSCManager(advertise=False)
        mgr.start()
        try:
            name = "VRChat-Client-1._oscjson._tcp.local."
            info = ServiceInfo("_oscjson._tcp.local.", name,
                               addresses=[bytes([127, 0, 0, 1])], port=vrc.http_port,
                               properties={}, server="h.local.")
            mgr._consider_service(name, info)
            vrc.set_node(MARKER_ADDR, 3)
            assert mgr.fetch(MARKER_ADDR).ok
        finally:
            mgr.stop()
        after = mgr.fetch(MARKER_ADDR)
        assert not after.ok, "a stopped manager still answered from the peer it stopped serving"
        assert after.reason == FETCH_NO_PEER, \
            "a local teardown was reported as the peer withdrawing"


def test_a_peer_returning_on_the_same_port_after_a_restart_is_readable_again():
    """Intended: stop() then start() leaves the manager able to read the peer it finds, and
    the unchanged-port case is the normal one because VRChat sits on 9000.

    The republication dedupe exists so an mDNS refresh does not rebuild the socket, and it
    compared only the send target -- all of which survives stop(). Since stop() drops
    `_peer_http`, the peer's return on an unchanged port matched the dedupe, returned early,
    and left fetch() permanently peerless while send kept working: a half-alive bridge on the
    library path, with no symptom until a read was attempted."""
    with FakeVRChat() as vrc:
        mgr = OSCManager(advertise=False)
        name = "VRChat-Client-1._oscjson._tcp.local."
        info = ServiceInfo("_oscjson._tcp.local.", name,
                           addresses=[bytes([127, 0, 0, 1])], port=vrc.http_port,
                           properties={}, server="h.local.")
        mgr.start()
        try:
            mgr._consider_service(name, info)
            vrc.set_node(MARKER_ADDR, 3)
            assert mgr.fetch(MARKER_ADDR).ok
        finally:
            mgr.stop()

        mgr.start()
        try:
            # Same service, same name, same ports -- exactly what a client that never moved
            # republishes, and what the dedupe is entitled to treat as unchanged.
            mgr._consider_service(name, info)
            assert mgr.fetch(MARKER_ADDR).ok, \
                "the peer came back on the same port and stayed unreadable"
        finally:
            mgr.stop()


def test_target_selection_fires_the_hook():
    """Intended: a consumer that must act when VRChat appears needs an event. Nothing else
    announces it -- _consider_service sets the target under the lock and only logs -- so a
    mapping would otherwise poll a private field."""
    with FakeVRChat() as vrc:
        mgr = OSCManager(advertise=False)
        seen = []
        mgr.add_target_listener(seen.append)
        name = "VRChat-Client-1._oscjson._tcp.local."
        info = ServiceInfo("_oscjson._tcp.local.", name,
                           addresses=[bytes([127, 0, 0, 1])], port=vrc.http_port,
                           properties={}, server="h.local.")
        mgr._consider_service(name, info)
        assert seen == [("127.0.0.1", vrc.osc_port)]

        # An unchanged republication is dropped before the target is rewritten, so a
        # listener sees one event per real selection rather than per mDNS refresh.
        mgr._consider_service(name, info)
        assert len(seen) == 1


def test_a_second_target_listener_does_not_displace_the_first():
    """Intended: registering is additive, because `VRBridge.__init__` has already claimed
    this hook for its own multiplexer.

    With a single settable slot, an embedder calling the manager directly silently
    unregistered every mapping's target callback -- the wardrobe's invalidate among them --
    with nothing logged and no symptom until an avatar change went unnoticed.
    """
    with FakeVRChat() as vrc:
        mgr = OSCManager(advertise=False)
        first, second = [], []
        mgr.add_target_listener(first.append)
        mgr.add_target_listener(second.append)
        name = "VRChat-Client-1._oscjson._tcp.local."
        info = ServiceInfo("_oscjson._tcp.local.", name,
                           addresses=[bytes([127, 0, 0, 1])], port=vrc.http_port,
                           properties={}, server="h.local.")
        mgr._consider_service(name, info)
        assert first == [("127.0.0.1", vrc.osc_port)], "the first listener was displaced"
        assert second == [("127.0.0.1", vrc.osc_port)]


def test_one_throwing_target_listener_still_lets_the_others_hear():
    """Intended: the per-listener catch is inside the loop, not around it. Around it, the
    first consumer to raise would silently deprive every later one of the event."""
    with FakeVRChat() as vrc:
        mgr = OSCManager(advertise=False)
        heard = []
        mgr.add_target_listener(lambda t: (_ for _ in ()).throw(RuntimeError("boom")))
        mgr.add_target_listener(heard.append)
        name = "VRChat-Client-1._oscjson._tcp.local."
        info = ServiceInfo("_oscjson._tcp.local.", name,
                           addresses=[bytes([127, 0, 0, 1])], port=vrc.http_port,
                           properties={}, server="h.local.")
        mgr._consider_service(name, info)
        assert heard == [("127.0.0.1", vrc.osc_port)], "a throwing listener ate the event"


def test_a_throwing_target_listener_costs_neither_the_target_nor_the_thread():
    """Intended: this fires on zeroconf's single dispatch thread, where an escape would
    take out every later service callback."""
    with FakeVRChat() as vrc:
        mgr = OSCManager(advertise=False)
        mgr.add_target_listener(lambda t: (_ for _ in ()).throw(RuntimeError("boom")))
        name = "VRChat-Client-1._oscjson._tcp.local."
        info = ServiceInfo("_oscjson._tcp.local.", name,
                           addresses=[bytes([127, 0, 0, 1])], port=vrc.http_port,
                           properties={}, server="h.local.")
        mgr._consider_service(name, info)
        assert mgr._client_target == ("127.0.0.1", vrc.osc_port)


# --------------------------------------------------------------------------
# The mapping
# --------------------------------------------------------------------------

FAST = WardrobeSettings(fetch_timeout_secs=1.0)

#: For the tests that park a read with `FakeVRChat.hold_next_node_get`. The timeout has to
#: exceed the gate's 5s park bound, or the fetch unparks itself first: at FAST's 1.0s the
#: parked thread returns FETCH_TRANSPORT on its own, the rendezvous the test was built on is
#: gone, and the abandoned handler's write to a closed socket surfaces as a traceback against
#: whichever test runs next. The gate must be the only thing controlling the park.
GATED = WardrobeSettings(fetch_timeout_secs=10.0)


class Harness:
    """A bridge + fake VRChat + a registered wardrobe, driven without a router.

    No shipped router registers this mapping, so tests drive it the way a user's own router
    would: construct, register, activate, then invoke through the bridge's dispatch.
    """

    def __init__(self, vrc: FakeVRChat, manifests, tuning=FAST, **kw):
        self.vrc = vrc
        self.bridge = VRBridge(enable_steamvr=False, advertise=False)
        self.bridge.osc.start()
        peer(self.bridge.osc, vrc)
        self.m = WardrobeMapping(self.bridge, manifests, tuning=tuning, **kw)
        self.m.register()
        # `enabled` is the router's and the mapping never writes its own, so something has
        # to switch it on -- exactly as a user's router does. Whether a press finds a
        # manifest is a separate question, settled by the marker read at press time.
        self.m.activate()

    def close(self):
        self.bridge.osc.stop()

    def slot(self, value):
        """Deliver a Slot datagram the way the OSC server would."""
        self.bridge._on_osc_event(SLOT_ADDR, value)

    def change(self, avatar_id):
        self.bridge._on_osc_event(AVATAR_CHANGE_ADDR, avatar_id)

    def deliver(self, address, value):
        """Deliver through the manager's change-filter, as a real datagram would.

        `bridge._on_osc_event` bypasses it, which is what let a test claim to cover the
        repeat-press dedupe while never exercising it.
        """
        self.bridge.osc._update_cache_and_fire(address, value)

    def deliver_threaded(self, address, value, name="deliver"):
        """Deliver on its own thread, as the OSC server's thread-per-datagram dispatch does.

        Every other delivery helper here runs the whole handler -- guard, the blocking read,
        the send -- on the calling thread, so delivery n finishes before n+1 starts and no
        interleaving is reachable. Pair this with `FakeVRChat.hold_next_node_get` to make one
        deterministic, rather than racing two threads and hoping.
        """
        t = threading.Thread(target=self.deliver, args=(address, value),
                             name=name, daemon=True)
        t.start()
        return t

    def sent(self):
        return self.vrc.values_for(AVATAR_CHANGE_ADDR)


def discover_manifests_from(tmp_path: Path, body: str):
    d = tmp_path / "manifests"
    d.mkdir(exist_ok=True)
    (d / "w.toml").write_text(body)
    return discover_manifests(d)


def rig(vrc, tmp_path, marker=7, body=ONE_MANIFEST, **kw):
    h = Harness(vrc, discover_manifests_from(tmp_path, body), **kw)
    if marker is not None:
        vrc.set_node(MARKER_ADDR, marker)
    return h


def test_the_marker_is_read_on_the_first_press_not_before(tmp_path):
    """Intended: the press is the readiness signal. A Slot datagram can only arrive if the
    new avatar is loaded and emitting, so a marker read then necessarily describes the avatar
    being worn -- which is what removes any need for a settling window. Reading on the avatar
    change instead means reading mid-transition, and no window can be sized for that: a cold
    download runs 30-60 s while the client acknowledges the change immediately."""
    with FakeVRChat() as vrc:
        h = rig(vrc, tmp_path)
        try:
            assert vrc.node_gets == [], "the marker must not be polled ahead of a press"

            h.slot(1)
            assert vrc.wait_for_count(1)
            assert h.sent() == [A_ID], "the swap must use manifest 7's slot-1 avatar"
            assert len(vrc.node_gets) == 1, "exactly one marker read, at the press"
        finally:
            h.close()


def test_every_press_re_reads_the_marker(tmp_path):
    """Intended: the read is deliberately NOT cached.

    A press can only arrive from a loaded avatar (the loading placeholder emits no OSC), so a
    cache would in fact stay correct as long as every avatar change reaches the invalidation
    path -- including one made from VRChat's own menu, and the case where the change filter
    suppresses a repeated identical echo. Re-reading needs none of that reasoning and costs one
    loopback GET at human press rates. Pinned so the cache is not quietly reintroduced as an
    optimisation."""
    with FakeVRChat() as vrc:
        h = rig(vrc, tmp_path)
        try:
            h.slot(1)
            h.slot(3)
            assert vrc.wait_for_count(2)
            assert len(vrc.node_gets) == 2, f"a press reused a cached marker: {vrc.node_gets}"
        finally:
            h.close()


def test_an_avatar_change_forces_the_next_press_to_re_read(tmp_path):
    """Intended: a manifest describes one avatar, so the change must drop it -- and the
    re-read happens at the next press rather than during the transition."""
    with FakeVRChat() as vrc:
        h = rig(vrc, tmp_path)
        try:
            h.slot(1)
            assert vrc.wait_for_count(1)

            h.change(B_ID)
            assert len(vrc.node_gets) == 1, "the change itself must not read anything"

            # Past the duplicate window before pressing the same slot again. The guard now
            # survives an avatar change, because the change is also how our own echo arrives
            # (see test_our_own_echo_does_not_disarm_the_duplicate_guard). A wearer cannot
            # press twice this fast anyway -- a Button holds for 200 ms.
            time.sleep(REPEAT_GUARD_SECS * 1.5)
            h.slot(1)
            assert vrc.wait_for_count(2)
            assert len(vrc.node_gets) == 2, "the next press should have read again"
        finally:
            h.close()


def test_a_cold_avatar_that_loads_slowly_still_arms(tmp_path):
    """Intended: the case the old scheduled design got wrong. A cold download can take a
    minute; because nothing is read until the press, an arbitrarily slow load costs nothing
    and the first press after it arms normally."""
    with FakeVRChat() as vrc:
        h = rig(vrc, tmp_path, marker=None)
        try:
            h.change(B_ID)
            # Long after the change, the avatar finally exists and starts serving.
            vrc.set_node(MARKER_ADDR, 7)
            h.slot(1)
            assert vrc.wait_for_count(1)
        finally:
            h.close()


def test_the_rest_slot_never_swaps_and_never_reads(tmp_path):
    """Intended: a Button returns to 0 on release and an avatar load initialises it to 0, so
    acting on 0 would swap on every release and every load. It must not even cost a read."""
    with FakeVRChat() as vrc:
        h = rig(vrc, tmp_path)
        try:
            h.slot(0)
            assert h.sent() == []
            assert vrc.node_gets == []
        finally:
            h.close()


def test_the_same_slot_twice_swaps_twice_through_the_real_change_filter(tmp_path):
    """Intended: the release edge makes a second press of one slot a second event. Measured
    live, a menu Button holds 200 ms then returns to 0, so 1 -> 0 -> 1 is the real sequence.

    Driven through the manager's change-filter rather than around it, since the filter drops
    a value equal to the last seen and a test that bypassed it could not show this."""
    with FakeVRChat() as vrc:
        h = rig(vrc, tmp_path)
        try:
            h.deliver(SLOT_ADDR, 1)
            h.deliver(SLOT_ADDR, 0)
            # A real Button holds 200 ms, so two rising edges cannot be closer than that.
            # Pressing faster than the hardware can is what the duplicate guard rejects.
            time.sleep(REPEAT_GUARD_SECS * 1.5)
            h.deliver(SLOT_ADDR, 1)
            assert vrc.wait_for_count(2)
            assert h.sent() == [A_ID, A_ID]
        finally:
            h.close()


def test_a_repeat_press_survives_a_lost_release(tmp_path):
    """Intended: a swap can eat the Button's release-to-0, because the outgoing avatar stops
    emitting first. The cached slot would then equal the next press and the change-filter
    would drop it before the mapping saw it -- a dead button until a different slot was
    pressed. Forgetting the cached value on a successful send prevents that."""
    with FakeVRChat() as vrc:
        h = rig(vrc, tmp_path)
        try:
            h.deliver(SLOT_ADDR, 1)
            assert vrc.wait_for_count(1)
            time.sleep(REPEAT_GUARD_SECS * 1.5)
            h.deliver(SLOT_ADDR, 1)      # no release-to-0 at all
            assert vrc.wait_for_count(2), "the repeat press was filtered away"
        finally:
            h.close()


def test_one_press_delivered_twice_swaps_once(tmp_path):
    """Intended: one press is one swap, even though `design.md` records that every inbound
    message is delivered twice.

    Found live, not here: acting on both copies makes the client answer the second with a
    visible "you are already using this avatar" error and *then* complete the swap, so the
    wearer sees a failure on every successful press. The change filter does not save us --
    `forget()` deliberately clears the cached slot so a genuine repeat is deliverable, and
    that is exactly what lets the duplicate through. Measured, the two copies arrive 1 ms
    apart while a real repeat cannot beat the Button's 200 ms hold."""
    with FakeVRChat() as vrc:
        h = rig(vrc, tmp_path)
        try:
            h.deliver(SLOT_ADDR, 1)
            h.deliver(SLOT_ADDR, 1)      # the duplicate delivery, immediately
            assert vrc.wait_for_count(1)
            time.sleep(0.3)
            assert h.sent() == [A_ID], f"one press produced {len(h.sent())} swaps"
        finally:
            h.close()


def test_our_own_echo_does_not_disarm_the_duplicate_guard(tmp_path):
    """Intended: the guard survives the `/avatar/change` echo of the swap that armed it.

    This test replaces one that asserted the opposite. That test pressed the same slot
    immediately after an avatar change and demanded a second swap -- a sequence the live
    measurements say cannot occur, because a Button holds for 200 ms and a loading wearer is
    the placeholder, which emits no OSC at all. Pinning it cost the guard its life: clearing
    `_last_slot` on invalidate meant our own echo, back within 5 ms, destroyed the 150 ms
    window from the inside. `forget()` has already reopened the change filter by then, so the
    second copy of the press swaps again -- the doubled swap `58a70a5` fixed.

    Ordering here is the real one: press, then the echo carrying the id *we sent*, then the
    delayed duplicate. Against the old code this produced two swaps."""
    with FakeVRChat() as vrc:
        h = rig(vrc, tmp_path)
        try:
            h.deliver(SLOT_ADDR, 1)
            assert vrc.wait_for_count(1)
            h.change(A_ID)               # the echo of our own swap, ~5 ms later live
            h.deliver(SLOT_ADDR, 1)      # the duplicate copy, dispatched late
            time.sleep(0.3)
            assert h.sent() == [A_ID], f"the echo re-opened the guard: {len(h.sent())} swaps"
        finally:
            h.close()


def test_a_genuine_repeat_after_the_guard_window_still_swaps(tmp_path):
    """Intended: keeping the guard across an avatar change does not wedge the button.

    The guard is time-bounded, so the press that the replaced test was reaching for -- the
    wearer swapping back to where they were -- still works; it just has to arrive after the
    window, which at 200 ms per Button press it always does."""
    with FakeVRChat() as vrc:
        h = rig(vrc, tmp_path)
        try:
            h.deliver(SLOT_ADDR, 1)
            assert vrc.wait_for_count(1)
            h.change(B_ID)
            time.sleep(REPEAT_GUARD_SECS * 1.5)
            h.deliver(SLOT_ADDR, 1)
            assert vrc.wait_for_count(2), "the guard outlived its own window"
        finally:
            h.close()


# --------------------------------------------------------------------------
# Interleaved delivery
#
# Everything above delivers on the calling thread, so delivery n completes -- guard, the
# blocking read, the send, forget() -- before n+1 begins. Two defects live in what a *stalled*
# read does on return to state a later press established, and neither is reachable that way.
# `FakeVRChat.hold_next_node_get` parks the read; `Harness.deliver_threaded` puts it on its own
# thread, as thread-per-datagram dispatch does.
#
# One production fact bounds what this can be aimed at, and it is worth stating because it
# invalidates the obvious test. `_update_cache_and_fire` computes `old` and writes the cache in
# one locked block, and forget() runs only after a successful send -- so a *duplicate copy of
# one press* cannot reach _on_slot while that press is still mid-read; the change filter eats
# it. The duplicate guard's live window opens at forget(), ~1 ms after arrival. A test that
# parks a press and then re-delivers the same slot is therefore testing the change filter, and
# passes against code that arms the guard on either side of the read. Both tests below drive
# the Button's real release-to-0 first, which is what makes the next value a change at all.
# --------------------------------------------------------------------------

def test_a_stalled_read_does_not_disarm_a_later_presss_guard(tmp_path, monkeypatch):
    """Intended: a press that failed to read releases its own guard and nobody else's.

    `_on_slot` releases the guard when it could not read a manifest, so that the wearer's
    retry of a transport failure is not swallowed as a duplicate. That release matched on the
    slot alone, and a read can outlast REPEAT_GUARD_SECS by an order of magnitude
    (`fetch_timeout_secs`) -- so a stalled press returning could clear a guard a *later* press
    of the same slot had armed, and that later press's duplicate copy then swapped again. One
    press, two swaps, and the wearer sees the client's "you are already using this avatar"
    error on a press that worked.

    Two conditions are stated rather than raced, and both margins are absurd on purpose so the
    wall clock cannot participate in the result at all: the guard is widened to 30 s so the
    duplicate at the end lands inside press 2's window even if `join` takes its full timeout,
    and press 1's stamp is backdated 100 s so its own window has certainly expired while it sat
    parked. Sleeping for either would put a real duplicate-swap report at the mercy of a
    scheduling hiccup.
    """
    monkeypatch.setattr(osc_wardrobe, "REPEAT_GUARD_SECS", 30.0)
    with FakeVRChat() as vrc:
        h = rig(vrc, tmp_path, tuning=GATED)
        try:
            vrc.node_404_first = 1        # press 1's read 404s, taking it to the release path
            with vrc.hold_next_node_get() as gate:
                stalled = h.deliver_threaded(SLOT_ADDR, 1)
                assert gate.wait_until_parked(), "press 1 never reached its marker read"
                with h.m._lock:
                    # Press 1's guard window expired while it was stalled. Backdated rather
                    # than slept for: the condition is the point, not the wall clock.
                    h.m._last_slot_at -= 100.0
                h.deliver(SLOT_ADDR, 0)   # the Button's release, press 1 still stalled
                h.deliver(SLOT_ADDR, 1)   # press 2: a genuine second press of the same slot
                # Press 2 must be the press that read successfully. If the gate were claimed
                # before the 404 count were consumed, press 2 would take the 404 instead and
                # this test would exercise a different mechanism that the fix does not cure.
                assert vrc.wait_for_count(1), "press 2 did not swap"
                gate.release()
            stalled.join(2)
            assert not stalled.is_alive(), "press 1's thread outlived its read"
            h.deliver(SLOT_ADDR, 0)
            h.deliver(SLOT_ADDR, 1)       # press 2's duplicate copy, delivered late
            time.sleep(0.3)
            assert h.sent() == [A_ID], \
                f"a stalled press disarmed a later press's guard: {len(h.sent())} swaps"
        finally:
            h.close()


def test_a_stalled_press_does_not_swap_over_a_later_one(tmp_path):
    """Intended: the wearer ends on the last slot they pressed.

    The guard is one `_last_slot` field, so a later press silently supersedes an in-flight one
    and nothing checks on return whether the press still holds it. Measured against the
    unfixed mapping, the sends arrive B then A: the wearer presses slot 1, presses slot 3
    while slot 1's read is still open, and ends up wearing slot 1's avatar. This needs no
    duplicate delivery and no failed read -- an ordinary slow OSCQuery answer is enough.
    """
    with FakeVRChat() as vrc:
        h = rig(vrc, tmp_path, tuning=GATED)
        try:
            with vrc.hold_next_node_get() as gate:
                stalled = h.deliver_threaded(SLOT_ADDR, 1)
                assert gate.wait_until_parked(), "slot 1 never reached its marker read"
                h.deliver(SLOT_ADDR, 0)
                h.deliver(SLOT_ADDR, 3)   # the wearer presses slot 3; this is the live one
                assert vrc.wait_for_count(1), "slot 3 did not swap"
                gate.release()
            stalled.join(2)
            assert not stalled.is_alive(), "slot 1's thread outlived its read"
            time.sleep(0.3)
            assert h.sent() == [B_ID], \
                f"a stalled press swapped over a later one: {h.sent()}"
        finally:
            h.close()


@pytest.mark.parametrize("bogus", [True, 1.9, "1", float("inf")])
def test_a_slot_that_is_not_a_whole_number_is_ignored(tmp_path, bogus):
    """Intended: the slot path tests the type instead of coercing it, exactly as the marker
    read does eleven lines below it.

    `int()` truncates and bool is an int subclass, so `True`, `1.9` and `"1"` all used to
    become slot 1 and swap the avatar; `int(inf)` raises OverflowError, which the old
    `except (TypeError, ValueError)` did not catch at all. A `Slot` parameter mis-authored as
    Bool is a live authoring risk -- the entry README warns about the adjacent MA sync-type
    trap -- and it would have swapped on every toggle-on.
    """
    with FakeVRChat() as vrc:
        h = rig(vrc, tmp_path)
        try:
            h.slot(bogus)
            time.sleep(0.1)
            assert h.sent() == [], f"{bogus!r} was accepted as a slot"
        finally:
            h.close()


def test_a_slot_with_no_entry_warns_and_leaves_the_wardrobe_working(tmp_path, caplog):
    """Intended: gaps are legal and the menu always ships eight buttons, so a pressable slot
    with no row is an ordinary authoring state. Taking the wardrobe down over one unused
    button would be far worse than a warning."""
    with FakeVRChat() as vrc:
        h = rig(vrc, tmp_path)
        try:
            with caplog.at_level("WARNING"):
                h.slot(2)
            assert "no entry" in caplog.text
            assert h.sent() == []
            h.slot(1)
            assert vrc.wait_for_count(1), "one dead button disarmed the whole wardrobe"
        finally:
            h.close()


def test_an_avatar_without_a_marker_says_so_once_but_is_still_re_read(tmp_path, caplog):
    """Intended: an avatar without the prefab is the common case and not an error, so it
    reports at INFO and does not repeat per press.

    Only the *message* is suppressed. Suppressing the read was a defect: a 404 taken in the
    gap between avatars would have marked the incoming avatar wardrobe-less for the session,
    which is the failure the scheduled design was abandoned for, through a new door."""
    with FakeVRChat() as vrc:
        h = rig(vrc, tmp_path, marker=None)
        try:
            with caplog.at_level("INFO"):
                h.slot(1)
                h.slot(3)
            assert h.sent() == []
            assert caplog.text.count("declares no wardrobe marker") == 1
            assert len(vrc.node_gets) == 2, "the read must not be suppressed, only the log"
        finally:
            h.close()


def test_a_transport_failure_is_retried_on_the_next_press(tmp_path, caplog):
    """Intended: a failed read says nothing about the avatar, unlike a 404 -- so it must not
    settle. The wearer pressing again is the retry, and it must work."""
    with FakeVRChat() as vrc:
        h = rig(vrc, tmp_path)
        try:
            vrc.node_fault = True
            with caplog.at_level("WARNING"):
                h.slot(1)
            assert h.sent() == []
            assert "this press does nothing" in caplog.text

            vrc.node_fault = False
            h.slot(1)
            assert vrc.wait_for_count(1), "the retry press should have armed and swapped"
        finally:
            h.close()


def test_a_marker_no_manifest_claims_names_the_id(tmp_path, caplog):
    """Intended: fail loud. An avatar carrying a wardrobe whose manifest was never written is
    a real mistake and the fix needs the number -- but it is settled, so it is said once."""
    with FakeVRChat() as vrc:
        h = rig(vrc, tmp_path, marker=42)
        try:
            with caplog.at_level("WARNING"):
                h.slot(1)
            assert h.sent() == []
            # Assert on the sentence, not a bare "7": A_ID itself contains a 7, so
            # substring-matching that digit passed whatever the code did.
            assert "marker is 42" in caplog.text
            assert "loaded: 7" in caplog.text
        finally:
            h.close()


def test_a_router_that_disables_the_wardrobe_keeps_it_disabled(tmp_path):
    """Intended: `enabled` belongs to the router, and the ungated invalidate handler must
    never promote the mapping. It only ever clears, so passing an avatar change through a
    disabled wardrobe cannot bring it back."""
    with FakeVRChat() as vrc:
        h = rig(vrc, tmp_path)
        try:
            h.slot(1)
            assert vrc.wait_for_count(1)
            h.m.deactivate()             # the router's decision

            h.change(B_ID)               # ungated: runs even while disabled
            assert not h.m.enabled, "a disabled wardrobe re-enabled itself"

            h.slot(3)
            assert h.sent() == [A_ID], "a disabled wardrobe swapped an avatar"
        finally:
            h.close()


def test_a_press_is_always_sent_because_the_echo_is_only_an_acknowledgement(tmp_path):
    """Intended: no "already wearing it" suppression.

    Measured live: the /avatar/change echo carries whatever id was *sent*, arrives inside
    5 ms, and fires for an ineligible or malformed id too. It acknowledges the request rather
    than stating what is worn. Skipping a send on the strength of it would mean that after a
    swap the client declined, the wearer's retry of that slot is suppressed in silence -- a
    button that stops working once it fails."""
    with FakeVRChat() as vrc:
        h = rig(vrc, tmp_path)
        try:
            h.change(A_ID)               # the echo names A; that does not mean A is worn
            h.slot(1)                    # slot 1 is the id that echo named
            assert vrc.wait_for_count(1)
            assert h.sent() == [A_ID], "a press must be sent, not suppressed on an echo"
        finally:
            h.close()


def test_a_pinned_target_can_name_its_manifest_instead_of_reading_one(tmp_path):
    """Intended: a pinned send target (the Av3Emulator, --osc-port) advertises nothing and
    serves no tree, so there is no marker to read and such a session could never arm. Naming
    the manifest takes the question away, which is design.md's rule for a named peer."""
    with FakeVRChat() as vrc:
        h = rig(vrc, tmp_path, marker=None, pinned_manifest_id=7)
        try:
            h.bridge.osc._peer_http = None      # as a pinned target leaves it
            h.slot(1)
            assert vrc.wait_for_count(1)
            assert h.sent() == [A_ID], "the named manifest's slot table must be the one used"
            assert vrc.node_gets == [], "a named manifest must not be read over HTTP"
        finally:
            h.close()


def test_a_pinned_target_without_a_named_manifest_says_how_to_fix_it(tmp_path, caplog):
    """Intended: the dead end must name its own escape hatch, since nothing about a silent
    wardrobe would point at the pin as the cause -- and it must name the *right* one. A
    missing peer also means "discovery has not resolved yet", which is the normal state for
    the first seconds of any run and is not fixed by naming a manifest."""
    with FakeVRChat() as vrc:
        h = rig(vrc, tmp_path, marker=None)
        try:
            h.bridge.osc._peer_http = None
            h.bridge.osc._pinned_target = ("127.0.0.1", 9000)
            with caplog.at_level("WARNING"):
                h.slot(1)
            assert h.sent() == []
            assert "pinned_manifest_id" in caplog.text
        finally:
            h.close()


def test_a_404_in_the_swap_gap_does_not_condemn_the_next_avatar(tmp_path):
    """Intended: a 404 answers "right now", never "this avatar, forever".

    Remembering it would mean any transient 404 -- a read landing between avatars, a tree not
    yet serving -- marks an avatar wardrobe-less until something else invalidates, which is the
    failure mode the scheduled design was abandoned for. `node_404_first` reproduces a
    transient 404 deterministically rather than racing a sleep."""
    with FakeVRChat() as vrc:
        h = rig(vrc, tmp_path)
        try:
            vrc.node_404_first = 1          # the first read lands in the gap
            h.slot(1)
            assert h.sent() == [], "a read in the gap should find no marker"

            time.sleep(REPEAT_GUARD_SECS * 1.5)
            h.slot(1)                       # the incoming avatar is now serving
            assert vrc.wait_for_count(1), "the 404 was remembered and killed the wardrobe"
        finally:
            h.close()


def test_an_empty_wardrobe_declines_a_press(tmp_path, caplog):
    """Intended: a mapping that can never do anything must say why when asked to act."""
    with FakeVRChat() as vrc:
        h = Harness(vrc, {})
        try:
            vrc.set_node(MARKER_ADDR, 7)
            with caplog.at_level("WARNING"):
                h.slot(1)
            assert h.sent() == []
            assert "no loaded manifest claims" in caplog.text
        finally:
            h.close()


def test_the_tested_module_is_this_checkout():
    """A Python editable install records one absolute path, so a second checkout can import
    the first one's src/ and pass regardless of its own changes. pyproject's
    `pythonpath = ["src"]` is what prevents it; this reports the resolved path so a green run
    can be attributed to a tree."""
    import vrbridge.wardrobe as w
    print(f"\nvrbridge.wardrobe resolved to: {w.__file__}")
    assert Path(w.__file__).is_file()
