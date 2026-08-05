"""The wardrobe: manifest loading, the OSCQuery single-node read, and the swap mapping.

Intent before test, per `docs/design.md`. Two behaviours here are deliberate designs that a
test written against observed behaviour would freeze backwards, so each states its intent:
the mapping acts on the transition *to* a non-zero slot (not on every change, which is
`osc_muteproxy`'s opposite contract), and an avatar change makes it inert until a marker read
agrees with itself twice -- because a read taken mid-transition can return the *outgoing*
avatar's marker and look correct.

The live client is not needed for any of this. Whether VRChat accepts an inbound
`/avatar/change` is settled by its own patch notes, and what it does with an *ineligible*
avatar id is a client behaviour no fake can answer -- `docs/design.md` rules that "only
provable in a live client" is the ecosystem's property, not a defect here.
"""
from pathlib import Path

import pytest
from zeroconf import ServiceInfo

from vrbridge.engine import VRBridge
from vrbridge.mappings.osc_wardrobe import (AVATAR_CHANGE_ADDR, MARKER_ADDR,
                                            SLOT_ADDR, WardrobeMapping)
from vrbridge.osc_manager import (FETCH_MALFORMED, FETCH_NO_PEER,
                                  FETCH_NOT_FOUND, FETCH_TRANSPORT, OSCManager)
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
        assert mgr.fetch(MARKER_ADDR).reason == FETCH_NO_PEER, \
            "fetch still had a peer after the service backing it went away"


def test_target_selection_fires_the_hook():
    """Intended: a consumer that must act when VRChat appears needs an event. Nothing else
    announces it -- _consider_service sets the target under the lock and only logs -- so a
    mapping would otherwise poll a private field."""
    with FakeVRChat() as vrc:
        mgr = OSCManager(advertise=False)
        seen = []
        mgr.set_target_listener(seen.append)
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


def test_a_throwing_target_listener_costs_neither_the_target_nor_the_thread():
    """Intended: this fires on zeroconf's single dispatch thread, where an escape would
    take out every later service callback."""
    with FakeVRChat() as vrc:
        mgr = OSCManager(advertise=False)
        mgr.set_target_listener(lambda t: (_ for _ in ()).throw(RuntimeError("boom")))
        name = "VRChat-Client-1._oscjson._tcp.local."
        info = ServiceInfo("_oscjson._tcp.local.", name,
                           addresses=[bytes([127, 0, 0, 1])], port=vrc.http_port,
                           properties={}, server="h.local.")
        mgr._consider_service(name, info)
        assert mgr._client_target == ("127.0.0.1", vrc.osc_port)


# --------------------------------------------------------------------------
# The mapping
# --------------------------------------------------------------------------

FAST = WardrobeSettings(settle_delay_secs=0.0, stable_gap_secs=0.01,
                        max_reads=4, fetch_timeout_secs=1.0)


class Harness:
    """A bridge + fake VRChat + a registered wardrobe, driven without a router.

    No shipped router registers this mapping, so tests drive it the way a user's own
    router would: construct, register, activate, then invoke through the bridge's dispatch.
    """

    def __init__(self, vrc: FakeVRChat, manifests, tuning=FAST):
        self.vrc = vrc
        self.bridge = VRBridge(enable_steamvr=False, advertise=False)
        self.bridge.osc.start()
        peer(self.bridge.osc, vrc)
        self.m = WardrobeMapping(self.bridge, manifests, tuning=tuning)
        self.m.register()
        # `enabled` is the router's and the mapping never writes its own, so something has
        # to switch it on -- exactly as a user's router does. Arming (`_active`) is a
        # separate axis and comes only from a marker read.
        self.m.activate()

    def close(self):
        self.bridge.osc.stop()

    def slot(self, value):
        """Deliver a Slot datagram the way the OSC server would."""
        self.bridge._on_osc_event(SLOT_ADDR, value)

    def change(self, avatar_id):
        self.bridge._on_osc_event(AVATAR_CHANGE_ADDR, avatar_id)

    def settle(self, timeout=3.0):
        """Wait until no marker read is running or pending."""
        return self.m._idle.wait(timeout)

    def armed(self):
        """The arm state is `_active`, never `enabled` -- which belongs to the router."""
        with self.m._lock:
            return self.m._active is not None

    def deliver(self, address, value):
        """Deliver through the manager's change-filter, as a real datagram would.

        `bridge._on_osc_event` bypasses it, which is what let a test claim to cover the
        repeat-press dedupe while never exercising it.
        """
        self.bridge.osc._update_cache_and_fire(address, value)

    def sent(self):
        return self.vrc.values_for(AVATAR_CHANGE_ADDR)


def discover_manifests_from(tmp_path: Path, body: str):
    d = tmp_path / "manifests"
    d.mkdir(exist_ok=True)
    (d / "w.toml").write_text(body)
    return discover_manifests(d)




def armed_manifest(h):
    with h.m._lock:
        return None if h.m._active is None else h.m._active.id


def test_a_slot_press_sends_the_avatar_id_as_a_string(tmp_path):
    """Intended: /avatar/change carries the id as an OSC string. The client dispatches on
    the argument's runtime type with no coercion, so nothing else would land."""
    with FakeVRChat() as vrc:
        h = Harness(vrc, discover_manifests_from(tmp_path, ONE_MANIFEST))
        try:
            vrc.set_node(MARKER_ADDR, 7)
            h.m._schedule("target-selected")
            assert h.settle()
            assert h.armed(), "a served marker naming a loaded manifest should arm it"

            h.slot(1)
            assert vrc.wait_for_count(1)
            assert h.sent() == [A_ID]
            assert isinstance(h.sent()[0], str)
        finally:
            h.m.close()
            h.close()


def test_the_rest_slot_never_swaps(tmp_path):
    """Intended: a Button returns to 0 on release and an avatar load initialises the
    parameter to 0, so acting on 0 would swap on every load and on every release."""
    with FakeVRChat() as vrc:
        h = Harness(vrc, discover_manifests_from(tmp_path, ONE_MANIFEST))
        try:
            vrc.set_node(MARKER_ADDR, 7)
            h.m._schedule("target-selected")
            assert h.settle()
            h.slot(0)
            assert h.sent() == []
        finally:
            h.m.close()
            h.close()


def test_the_same_slot_twice_swaps_twice_through_the_real_change_filter(tmp_path):
    """Intended: the release edge makes a second press of one slot a second event, which is
    how a retry works after a swap that did not take.

    Driven through the manager's change-filter rather than around it: the filter drops a
    value equal to the last seen, so 1 -> 0 -> 1 is the only sequence that reaches the
    mapping twice, and a test that bypassed the filter could not show that."""
    with FakeVRChat() as vrc:
        h = Harness(vrc, discover_manifests_from(tmp_path, ONE_MANIFEST))
        try:
            vrc.set_node(MARKER_ADDR, 7)
            h.m._schedule("target-selected")
            assert h.settle()

            h.deliver(SLOT_ADDR, 1)
            h.deliver(SLOT_ADDR, 0)
            h.deliver(SLOT_ADDR, 1)
            assert vrc.wait_for_count(2)
            assert h.sent() == [A_ID, A_ID]
        finally:
            h.m.close()
            h.close()


def test_a_repeat_press_survives_a_lost_release(tmp_path):
    """Intended: a swap can eat the Button's release-to-0, because the outgoing avatar stops
    emitting first. The cached slot would then equal the next press and the change-filter
    would drop it *before* the mapping saw it -- a dead button until a different slot was
    pressed. Forgetting the cached value on a successful send is what prevents that."""
    with FakeVRChat() as vrc:
        h = Harness(vrc, discover_manifests_from(tmp_path, ONE_MANIFEST))
        try:
            vrc.set_node(MARKER_ADDR, 7)
            h.m._schedule("target-selected")
            assert h.settle()

            h.deliver(SLOT_ADDR, 1)
            assert vrc.wait_for_count(1)
            # No release-to-0 at all, then the same slot again.
            h.deliver(SLOT_ADDR, 1)
            assert vrc.wait_for_count(2), "the repeat press was filtered away"
            assert h.sent() == [A_ID, A_ID]
        finally:
            h.m.close()
            h.close()


def test_a_slot_with_no_entry_warns_and_leaves_the_wardrobe_working(tmp_path, caplog):
    """Intended: gaps are legal and the menu always ships eight buttons, so a pressable
    slot with no row is an ordinary authoring state. Taking the whole wardrobe down over one
    unused button would be far worse than a warning."""
    with FakeVRChat() as vrc:
        h = Harness(vrc, discover_manifests_from(tmp_path, ONE_MANIFEST))
        try:
            vrc.set_node(MARKER_ADDR, 7)
            h.m._schedule("target-selected")
            assert h.settle()

            with caplog.at_level("WARNING"):
                h.slot(2)
            assert "no entry" in caplog.text
            assert h.sent() == []
            assert h.armed(), "one dead button must not disarm the wardrobe"

            h.slot(1)
            assert vrc.wait_for_count(1)
        finally:
            h.m.close()
            h.close()


def test_a_press_before_any_marker_is_read_warns_and_swaps_nothing(tmp_path, caplog):
    """Intended: with no manifest adopted the mapping does not know what the buttons mean,
    so it declines and says so rather than guessing.

    Reached without touching the mapping by hand, which matters: while the mapping wrote its
    own `enabled`, the press was swallowed by the gate *before* this warning, so the
    fail-loud path existed only for a test that faked the state."""
    with FakeVRChat() as vrc:
        h = Harness(vrc, discover_manifests_from(tmp_path, ONE_MANIFEST))
        try:
            with caplog.at_level("WARNING"):
                h.slot(1)
            assert h.sent() == []
            assert "no manifest is active" in caplog.text
        finally:
            h.m.close()
            h.close()


def test_a_router_that_disables_the_wardrobe_keeps_it_disabled(tmp_path):
    """Intended: `enabled` is the router's and `_active` is the arm state. An avatar change
    must be able to re-read the marker without re-enabling a mapping the router switched
    off -- the ungated handler exists for recovery, not for self-promotion."""
    with FakeVRChat() as vrc:
        h = Harness(vrc, discover_manifests_from(tmp_path, ONE_MANIFEST))
        try:
            vrc.set_node(MARKER_ADDR, 7)
            h.m.activate()
            h.m.deactivate()          # the router's decision

            h.change(A_ID)            # ungated: re-reads and re-arms
            assert h.settle()
            assert h.armed(), "the marker should still have been read while disabled"
            assert not h.m.enabled, "a disabled wardrobe re-enabled itself"

            h.slot(3)
            assert h.sent() == [], "a disabled wardrobe swapped an avatar"
        finally:
            h.m.close()
            h.close()


def test_a_press_is_always_sent_because_the_echo_is_only_an_acknowledgement(tmp_path):
    """Intended: no "already wearing it" suppression. The client no-ops a swap to the
    avatar already worn -- so a press is always sent, even for the avatar we appear to be
    wearing.

    Measured against a live client: the /avatar/change echo carries whatever id was *sent*,
    arrives inside 5 ms, and fires for an ineligible or malformed id too. It is an
    acknowledgement, not a statement of what is worn. Skipping a send on the strength of it
    would mean that after a swap the client declined, the wearer's retry of that same slot is
    suppressed in silence -- a button that stops working once it fails. A redundant send the
    client no-ops is much cheaper."""
    with FakeVRChat() as vrc:
        h = Harness(vrc, discover_manifests_from(tmp_path, ONE_MANIFEST))
        try:
            vrc.set_node(MARKER_ADDR, 7)
            h.change(A_ID)                 # the echo names A; that does not mean A is worn
            assert h.settle()
            assert h.armed()
            h.slot(1)                      # slot 1 is the id that echo named
            assert vrc.wait_for_count(1)
            assert h.sent() == [A_ID], "a press must be sent, not suppressed on an echo"

            h.slot(1)                      # and a retry must work too
            assert vrc.wait_for_count(2)
        finally:
            h.m.close()
            h.close()


def test_a_marker_is_adopted_once_two_reads_a_gap_apart_agree(tmp_path, caplog):
    """Intended: the stability rule, actually exercised -- the node stays served throughout,
    so the read loop runs rather than short-circuiting.

    This is the flagship of the design and its first version never entered the loop: it
    cleared the node, so the read 404'd and returned before any stability check ran. That is
    how a 404 ending the whole schedule went unnoticed."""
    with FakeVRChat() as vrc:
        h = Harness(vrc, discover_manifests_from(tmp_path, ONE_MANIFEST))
        try:
            vrc.set_node(MARKER_ADDR, 7)
            with caplog.at_level("INFO"):
                h.change(B_ID)
                assert h.settle()
            assert armed_manifest(h) == 7
            assert "stable across two reads" in caplog.text, \
                "adopted by some route other than the stability rule"
            assert len(vrc.node_gets) >= 2, "the stability rule needs two reads"
        finally:
            h.m.close()
            h.close()


def test_a_404_during_the_swap_does_not_end_the_schedule(tmp_path, caplog):
    """Intended: during a swap the new avatar's node is not published yet, so the first read
    of a perfectly good wardrobe 404s. Returning there left a wardrobed avatar permanently
    unarmed, recoverable only through the very menu this feature replaces -- and an observed
    404 is also the teardown proof that makes the next value trustworthy."""
    with FakeVRChat() as vrc:
        h = Harness(vrc, discover_manifests_from(tmp_path, ONE_MANIFEST))
        try:
            # The node exists but 404s for the first two reads, which is the swap window:
            # the new avatar's node is not published the instant the change is announced.
            # Deterministic rather than a sleep raced against the read schedule.
            vrc.set_node(MARKER_ADDR, 7)
            vrc.node_404_first = 2

            with caplog.at_level("INFO"):
                h.change(B_ID)
                assert h.settle()
            assert armed_manifest(h) == 7, "a 404 then a value must still arm the wardrobe"
            assert "after an observed 404" in caplog.text
        finally:
            h.m.close()
            h.close()


def test_a_marker_absent_for_the_whole_schedule_reads_as_no_wardrobe(tmp_path, caplog):
    """Intended: an avatar without the prefab is the common case and not an error, so it
    reports at INFO and leaves the wardrobe unarmed rather than warning."""
    with FakeVRChat() as vrc:
        h = Harness(vrc, discover_manifests_from(tmp_path, ONE_MANIFEST))
        try:
            vrc.clear_node(MARKER_ADDR)
            with caplog.at_level("INFO"):
                h.change(B_ID)
                assert h.settle()
            assert not h.armed()
            assert "No wardrobe marker" in caplog.text
            h.slot(1)
            assert h.sent() == [], "an unarmed wardrobe must not swap"
        finally:
            h.m.close()
            h.close()


def test_a_marker_that_changes_between_reads_is_not_adopted(tmp_path, caplog):
    """Intended: a value still settling is not a value. Adopting the first read is what
    would let the outgoing avatar's table survive into the new avatar."""
    with FakeVRChat() as vrc:
        h = Harness(vrc, discover_manifests_from(tmp_path, ONE_MANIFEST))
        try:
            import itertools
            counter = itertools.count(100)
            real_fetch = h.bridge.osc.fetch

            def unstable(address, timeout=2.0):
                from vrbridge.osc_manager import FETCH_OK, FetchResult
                return FetchResult(FETCH_OK, value=next(counter))
            h.bridge.osc.fetch = unstable
            try:
                with caplog.at_level("WARNING"):
                    h.change(B_ID)
                    assert h.settle()
            finally:
                h.bridge.osc.fetch = real_fetch
            assert not h.armed()
            assert "never held the same value" in caplog.text
        finally:
            h.m.close()
            h.close()


def test_transport_failures_exhaust_the_budget_and_say_so(tmp_path, caplog):
    """Intended: a peer that cannot be reached is distinct from one that answers 404. It
    leaves the wardrobe unarmed and names the failure count rather than reading as
    "this avatar has no wardrobe"."""
    with FakeVRChat() as vrc:
        h = Harness(vrc, discover_manifests_from(tmp_path, ONE_MANIFEST))
        try:
            vrc.set_node(MARKER_ADDR, 7)
            vrc.node_fault = True
            with caplog.at_level("WARNING"):
                h.change(B_ID)
                assert h.settle()
            assert not h.armed()
            assert "transport failures" in caplog.text
        finally:
            h.m.close()
            h.close()


def test_a_pinned_target_can_name_its_manifest_instead_of_reading_one(tmp_path):
    """Intended: a pinned send target (the Av3Emulator, --osc-port) advertises nothing and
    serves no tree, so there is no marker to read and such a session could never arm. Naming
    the manifest takes the question away, which is design.md's rule for a named peer."""
    with FakeVRChat() as vrc:
        h = Harness(vrc, discover_manifests_from(tmp_path, ONE_MANIFEST))
        try:
            h.m._pinned_manifest_id = 7
            h.bridge.osc._peer_http = None      # as a pinned target leaves it
            h.change(B_ID)
            assert h.settle()
            assert armed_manifest(h) == 7
            h.slot(1)
            assert vrc.wait_for_count(1)
            assert h.sent() == [A_ID]
        finally:
            h.m.close()
            h.close()


def test_a_second_avatar_change_supersedes_an_in_flight_read(tmp_path, caplog):
    """Intended: scrolling through two avatars inside one read window must not arm the
    wardrobe with the table of an avatar nobody is wearing.

    The first version of this test sent the *same* id twice and asserted that only one read
    happened -- freezing the defect as intent. It also guarded an impossible event: the
    manager's change-filter already drops a repeated identical /avatar/change one layer
    below the mapping."""
    d = tmp_path / "two"
    d.mkdir()
    (d / "a.toml").write_text(ONE_MANIFEST)
    (d / "b.toml").write_text(f'id = 9\n\n[[slots]]\nslot = 1\nid = "{B_ID}"\n')
    with FakeVRChat() as vrc:
        h = Harness(vrc, discover_manifests(d))
        try:
            vrc.set_node(MARKER_ADDR, 7)
            h.change(A_ID)                  # schedules a read that would adopt manifest 7
            vrc.set_node(MARKER_ADDR, 9)    # the next avatar carries a different wardrobe
            h.change(B_ID)                  # supersedes it
            with caplog.at_level("INFO"):
                assert h.settle()
            assert armed_manifest(h) == 9, \
                "adopted a manifest for an avatar that is no longer worn"
            assert armed_manifest(h) == 9
        finally:
            h.m.close()
            h.close()


def test_a_read_from_a_superseded_generation_refuses_to_commit(tmp_path, caplog):
    """Intended: a read that finishes after the worn avatar moved on must not arm anything.

    Tested at the guard rather than by staging a thread race, because the end-to-end version
    cannot see this: whichever way the guard behaves, the *last* read wins and the final
    state looks identical. Asserting the final state passed with the guard removed, which is
    exactly the check-that-survives-the-bug this suite is supposed to avoid."""
    with FakeVRChat() as vrc:
        h = Harness(vrc, discover_manifests_from(tmp_path, ONE_MANIFEST))
        try:
            with h.m._lock:
                stale_gen = h.m._generation
                h.m._generation += 1        # a later avatar change arrived
            with caplog.at_level("INFO"):
                h.m._adopt(7, stale_gen, "stable across two reads")
            assert not h.armed(), "a stale read armed the wardrobe for an unworn avatar"
            assert "superseded" in caplog.text
        finally:
            h.m.close()
            h.close()


def test_a_marker_no_manifest_claims_leaves_it_unarmed_and_names_the_id(tmp_path, caplog):
    """Intended: fail loud. An avatar carrying a wardrobe menu whose manifest was never
    written is a real mistake, and the fix needs the number."""
    with FakeVRChat() as vrc:
        h = Harness(vrc, discover_manifests_from(tmp_path, ONE_MANIFEST))
        try:
            vrc.set_node(MARKER_ADDR, 42)
            with caplog.at_level("WARNING"):
                h.change(B_ID)
                assert h.settle()
            assert not h.armed()
            # Assert on the sentence, not on a bare "7": A_ID itself contains a 7, so
            # substring-matching that digit passed whatever the code did.
            assert "marker is 42" in caplog.text
            assert "loaded: 7" in caplog.text
        finally:
            h.m.close()
            h.close()


def test_an_empty_wardrobe_declines_a_press(tmp_path, caplog):
    """Intended: a mapping that can never do anything must say why when asked to act."""
    with FakeVRChat() as vrc:
        h = Harness(vrc, {})
        try:
            with caplog.at_level("WARNING"):
                h.slot(1)
            assert h.sent() == []
            assert "no manifest is active" in caplog.text
        finally:
            h.m.close()
            h.close()


def test_the_tested_module_is_this_checkout():
    """A Python editable install records one absolute path, so a second checkout can import
    the first one's src/ and pass regardless of its own changes. pyproject's
    `pythonpath = ["src"]` is what prevents it; this reports the resolved path so a green
    run can be attributed to a tree."""
    import vrbridge.wardrobe as w
    print(f"\nvrbridge.wardrobe resolved to: {w.__file__}")
    assert Path(w.__file__).is_file()
