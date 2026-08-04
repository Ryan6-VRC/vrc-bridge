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
    router would: construct, register, then invoke through the bridge's dispatch.
    """

    def __init__(self, vrc: FakeVRChat, manifests, tuning=FAST):
        self.vrc = vrc
        self.bridge = VRBridge(enable_steamvr=False, advertise=False)
        self.bridge.osc.start()
        peer(self.bridge.osc, vrc)
        self.m = WardrobeMapping(self.bridge, manifests, tuning=tuning)
        self.m.register()

    def close(self):
        self.bridge.osc.stop()

    def slot(self, value):
        """Deliver a Slot datagram the way the OSC server would."""
        self.bridge._on_osc_event(SLOT_ADDR, value)

    def change(self, avatar_id):
        self.bridge._on_osc_event(AVATAR_CHANGE_ADDR, avatar_id)

    def settle(self, timeout=3.0):
        """Wait for the marker-read worker to finish its current job."""
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.m._rearm_queued and self.m._work.unfinished_tasks == 0:
                return True
            time.sleep(0.005)
        return False

    def sent(self):
        return self.vrc.values_for(AVATAR_CHANGE_ADDR)


def discover_manifests_from(tmp_path: Path, body: str):
    d = tmp_path / "manifests"
    d.mkdir(exist_ok=True)
    (d / "w.toml").write_text(body)
    return discover_manifests(d)


def test_a_slot_press_sends_the_avatar_id_as_a_string(tmp_path):
    """Intended: /avatar/change carries the id as an OSC string. The client dispatches on
    the argument's runtime type with no coercion, so nothing else would land."""
    with FakeVRChat() as vrc:
        h = Harness(vrc, discover_manifests_from(tmp_path, ONE_MANIFEST))
        try:
            vrc.set_node(MARKER_ADDR, 7)
            h.m._queue_rearm("target-selected")
            assert h.settle()
            assert h.m.enabled, "a served marker naming a loaded manifest should arm it"

            h.slot(1)
            assert vrc.wait_for_count(1)
            assert h.sent() == [A_ID]
            assert isinstance(h.sent()[0], str)
        finally:
            h.close()


def test_the_rest_slot_never_swaps(tmp_path):
    """Intended: a Button returns to 0 on release and an avatar load initialises the
    parameter to 0, so acting on 0 would swap on every load and on every release."""
    with FakeVRChat() as vrc:
        h = Harness(vrc, discover_manifests_from(tmp_path, ONE_MANIFEST))
        try:
            vrc.set_node(MARKER_ADDR, 7)
            h.m._queue_rearm("target-selected")
            assert h.settle()
            h.slot(0)
            assert h.sent() == []
        finally:
            h.close()


def test_pressing_the_same_slot_twice_swaps_twice(tmp_path):
    """Intended: the Button's release edge is load-bearing, not incidental -- it is what
    makes a second press of the same slot a second event, which is how a retry works after
    a swap that did not take. Momentary is the required source here, the opposite of
    osc_muteproxy's latching contract."""
    with FakeVRChat() as vrc:
        h = Harness(vrc, discover_manifests_from(tmp_path, ONE_MANIFEST))
        try:
            vrc.set_node(MARKER_ADDR, 7)
            h.m._queue_rearm("target-selected")
            assert h.settle()
            h.slot(1)
            h.slot(0)
            h.slot(1)
            assert vrc.wait_for_count(2)
            assert h.sent() == [A_ID, A_ID]
        finally:
            h.close()


def test_a_slot_with_no_entry_warns_and_leaves_the_wardrobe_working(tmp_path, caplog):
    """Intended: gaps are legal and the menu always ships eight buttons, so a pressable
    slot with no row is an ordinary authoring state. Taking the whole wardrobe down over one
    unused button would be a far worse failure than a warning."""
    with FakeVRChat() as vrc:
        h = Harness(vrc, discover_manifests_from(tmp_path, ONE_MANIFEST))
        try:
            vrc.set_node(MARKER_ADDR, 7)
            h.m._queue_rearm("target-selected")
            assert h.settle()

            with caplog.at_level("WARNING"):
                h.slot(2)
            assert "no entry" in caplog.text
            assert h.sent() == []
            assert h.m.enabled, "one dead button must not disarm the wardrobe"

            h.slot(1)
            assert vrc.wait_for_count(1)
            assert h.sent() == [A_ID]
        finally:
            h.close()


def test_a_press_before_any_marker_is_read_swaps_nothing(tmp_path, caplog):
    """Intended: with no manifest adopted the mapping does not know what the wearer's
    buttons mean, so it must decline rather than guess at a table."""
    with FakeVRChat() as vrc:
        h = Harness(vrc, discover_manifests_from(tmp_path, ONE_MANIFEST))
        try:
            h.m.activate()          # armed by hand, but no marker read has happened
            with caplog.at_level("WARNING"):
                h.slot(1)
            assert h.sent() == []
            assert "no manifest is active" in caplog.text
        finally:
            h.close()


def test_the_avatar_already_worn_is_not_re_sent(tmp_path):
    """Intended: a courtesy, not a correctness mechanism -- the client no-ops a swap to the
    avatar already worn, and skipping it keeps the log honest about what was asked for."""
    with FakeVRChat() as vrc:
        h = Harness(vrc, discover_manifests_from(tmp_path, ONE_MANIFEST))
        try:
            vrc.set_node(MARKER_ADDR, 7)
            h.change(A_ID)                 # now wearing A, and re-armed from the marker
            assert h.settle()
            h.slot(1)                      # slot 1 *is* A
            assert h.sent() == []
        finally:
            h.close()


def test_an_avatar_change_disarms_until_a_marker_read_agrees_with_itself(tmp_path):
    """Intended: this is the defence against reading the *outgoing* avatar's marker. The
    client 404s an address no worn avatar declares, but a wardrobe's whole purpose is that
    several avatars declare this one at different values, so a read during the transition
    can return the old id and be indistinguishable from a correct answer. Two agreeing reads
    a gap apart are required, and in between the mapping is inert -- so a press during the
    transition does nothing rather than indexing the previous avatar's table."""
    with FakeVRChat() as vrc:
        h = Harness(vrc, discover_manifests_from(tmp_path, ONE_MANIFEST))
        try:
            vrc.set_node(MARKER_ADDR, 7)
            h.m._queue_rearm("target-selected")
            assert h.settle()
            assert h.m.enabled

            vrc.clear_node(MARKER_ADDR)    # mid-transition: nothing served yet
            h.change(B_ID)
            assert h.settle()
            assert not h.m.enabled, "a change must disarm until a marker is re-read"
            h.slot(1)
            assert h.sent() == [], "an inert wardrobe must not swap"
        finally:
            h.close()


def test_a_marker_that_changes_between_reads_is_not_adopted(tmp_path, caplog):
    """Intended: a value still settling is not a value. Adopting the first read is what
    would let the outgoing avatar's table survive into the new avatar."""
    with FakeVRChat() as vrc:
        h = Harness(vrc, discover_manifests_from(tmp_path, ONE_MANIFEST))
        try:
            import itertools
            # A different value on every read, so no two ever agree.
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
            assert not h.m.enabled
            assert "never held the same value" in caplog.text
        finally:
            h.close()


def test_a_change_re_arms_after_an_earlier_read_found_nothing(tmp_path):
    """Intended: the /avatar/change handler stays ungated so deactivation is recoverable.
    Gating it would make the first 404 terminal -- and because a *successful* swap is what
    triggers the read, the feature would break precisely on working."""
    with FakeVRChat() as vrc:
        h = Harness(vrc, discover_manifests_from(tmp_path, ONE_MANIFEST))
        try:
            h.change(B_ID)                 # no node served: no wardrobe on this avatar
            assert h.settle()
            assert not h.m.enabled

            vrc.set_node(MARKER_ADDR, 7)   # next avatar carries one
            h.change(A_ID)
            assert h.settle()
            assert h.m.enabled, "a later avatar change must be able to re-arm the wardrobe"
        finally:
            h.close()


def test_a_marker_no_manifest_claims_disarms_and_names_the_id(tmp_path, caplog):
    """Intended: fail loud. An avatar carrying a wardrobe menu whose manifest was never
    written is a real mistake, and the fix needs the number."""
    with FakeVRChat() as vrc:
        h = Harness(vrc, discover_manifests_from(tmp_path, ONE_MANIFEST))
        try:
            vrc.set_node(MARKER_ADDR, 42)
            with caplog.at_level("WARNING"):
                h.change(A_ID)
                assert h.settle()
            assert not h.m.enabled
            assert "42" in caplog.text and "7" in caplog.text
        finally:
            h.close()


def test_a_doubled_avatar_change_reads_the_marker_once(tmp_path):
    """Intended: `docs/design.md` records that every inbound message is delivered twice
    until its open measurement lands, so a swap arrives twice. Without coalescing, one swap
    would start two read schedules racing to adopt a manifest."""
    with FakeVRChat() as vrc:
        h = Harness(vrc, discover_manifests_from(tmp_path, ONE_MANIFEST))
        try:
            vrc.set_node(MARKER_ADDR, 7)
            h.change(A_ID)
            h.change(A_ID)              # the duplicate
            assert h.settle()
            assert h.m.enabled
            # Two agreeing reads arm it, so a single schedule reads exactly twice. The lower
            # bound matters as much as the upper: without it this passes when nothing read
            # at all, which is the failure mode a coalescing test is most likely to have.
            assert len(vrc.node_gets) == 2, \
                f"expected one read schedule of two reads, got {vrc.node_gets}"
        finally:
            h.close()


def test_an_empty_wardrobe_says_so_when_activated(tmp_path, caplog):
    """Intended: a mapping that can never do anything must say why, naming where the
    manifests were looked for -- silence reads identically to a broken avatar."""
    with FakeVRChat() as vrc:
        h = Harness(vrc, {})
        try:
            with caplog.at_level("WARNING"):
                h.m.activate()
            assert "no wardrobe manifests" in caplog.text
        finally:
            h.close()


def test_the_tested_module_is_this_checkout():
    """A Python editable install records one absolute path, so a second checkout can import
    the first one's src/ and pass regardless of its own changes. pyproject's
    `pythonpath = ["src"]` is what prevents it; this reports the resolved path so a green
    run can be attributed to a tree."""
    import vrbridge.wardrobe as w
    print(f"\nvrbridge.wardrobe resolved to: {w.__file__}")
    assert Path(w.__file__).is_file()
