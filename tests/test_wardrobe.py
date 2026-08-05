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
import time
from pathlib import Path

import pytest
from zeroconf import ServiceInfo

from vrbridge.engine import VRBridge
from vrbridge.mappings.osc_wardrobe import (AVATAR_CHANGE_ADDR, MARKER_ADDR,
                                            REPEAT_GUARD_SECS, SLOT_ADDR,
                                            WardrobeMapping)
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

FAST = WardrobeSettings(fetch_timeout_secs=1.0)


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
        # to switch it on -- exactly as a user's router does. Arming (`_active`) is a
        # separate axis, and comes only from a marker read at press time.
        self.m.activate()

    def close(self):
        self.bridge.osc.stop()

    def slot(self, value):
        """Deliver a Slot datagram the way the OSC server would."""
        self.bridge._on_osc_event(SLOT_ADDR, value)

    def change(self, avatar_id):
        self.bridge._on_osc_event(AVATAR_CHANGE_ADDR, avatar_id)

    def armed(self):
        """The arm state is `_active`, never `enabled` -- which belongs to the router."""
        with self.m._lock:
            return self.m._active is not None

    def armed_id(self):
        with self.m._lock:
            return None if self.m._active is None else self.m._active.id

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
            assert not h.armed(), "nothing should be read before a press"
            assert vrc.node_gets == [], "the marker must not be polled ahead of a press"

            h.slot(1)
            assert vrc.wait_for_count(1)
            assert h.sent() == [A_ID]
            assert h.armed_id() == 7
            assert len(vrc.node_gets) == 1, "exactly one marker read, at the press"
        finally:
            h.close()


def test_a_later_press_reuses_the_marker_it_already_read(tmp_path):
    """Intended: the read is cached until the worn avatar changes, so a wardrobe does not
    pay an HTTP round trip on every button press."""
    with FakeVRChat() as vrc:
        h = rig(vrc, tmp_path)
        try:
            h.slot(1)
            h.slot(3)
            assert vrc.wait_for_count(2)
            assert len(vrc.node_gets) == 1, f"re-read the marker: {vrc.node_gets}"
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
            assert h.armed()

            h.change(B_ID)
            assert not h.armed(), "an avatar change must drop the manifest"
            assert len(vrc.node_gets) == 1, "the change itself must not read anything"

            h.slot(1)
            assert vrc.wait_for_count(2)
            assert len(vrc.node_gets) == 2, "the next press should have re-read"
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
            assert h.armed_id() == 7
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


def test_the_duplicate_guard_does_not_outlive_an_avatar_change(tmp_path):
    """Intended: the guard covers one press on one avatar. After a change the same slot is a
    genuine new press -- swapping back to where you were is an ordinary thing to do, and it
    must not be read as the previous press's duplicate."""
    with FakeVRChat() as vrc:
        h = rig(vrc, tmp_path)
        try:
            h.slot(1)
            assert vrc.wait_for_count(1)
            h.change(B_ID)
            h.slot(1)                    # immediately, but on a different avatar
            assert vrc.wait_for_count(2), "the guard swallowed a press after an avatar change"
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
            assert h.armed(), "one dead button must not disarm the wardrobe"
            h.slot(1)
            assert vrc.wait_for_count(1)
        finally:
            h.close()


def test_an_avatar_without_a_marker_says_so_once_not_per_press(tmp_path, caplog):
    """Intended: an avatar without the prefab is the common case and not an error, so it
    reports at INFO -- and settles, so a wardrobe-less avatar is not re-queried on every
    press and the message is not repeated until something could change the answer."""
    with FakeVRChat() as vrc:
        h = rig(vrc, tmp_path, marker=None)
        try:
            with caplog.at_level("INFO"):
                h.slot(1)
                h.slot(3)
            assert h.sent() == []
            assert caplog.text.count("declares no wardrobe marker") == 1
            assert len(vrc.node_gets) == 1, "a settled answer must not be re-read"

            h.change(A_ID)               # only a change may reopen the question
            h.slot(1)
            assert len(vrc.node_gets) == 2
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
            assert h.armed_id() == 7
        finally:
            h.close()


def test_a_marker_no_manifest_claims_names_the_id_and_settles(tmp_path, caplog):
    """Intended: fail loud. An avatar carrying a wardrobe whose manifest was never written is
    a real mistake and the fix needs the number -- but it is settled, so it is said once."""
    with FakeVRChat() as vrc:
        h = rig(vrc, tmp_path, marker=42)
        try:
            with caplog.at_level("WARNING"):
                h.slot(1)
            assert not h.armed()
            # Assert on the sentence, not a bare "7": A_ID itself contains a 7, so
            # substring-matching that digit passed whatever the code did.
            assert "marker is 42" in caplog.text
            assert "loaded: 7" in caplog.text
        finally:
            h.close()


def test_a_router_that_disables_the_wardrobe_keeps_it_disabled(tmp_path):
    """Intended: `enabled` is the router's and `_active` is the arm state. The ungated
    invalidate handler exists to drop a stale manifest, never to promote the mapping."""
    with FakeVRChat() as vrc:
        h = rig(vrc, tmp_path)
        try:
            h.slot(1)
            assert vrc.wait_for_count(1)
            h.m.deactivate()             # the router's decision

            h.change(B_ID)               # ungated: must still invalidate
            assert not h.armed(), "a stale manifest survived an avatar change while disabled"
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
            assert h.armed_id() == 7
            assert vrc.node_gets == [], "a named manifest must not be read over HTTP"
        finally:
            h.close()


def test_a_pinned_target_without_a_named_manifest_says_how_to_fix_it(tmp_path, caplog):
    """Intended: the dead end must name its own escape hatch, since nothing about a silent
    wardrobe would point at the pin as the cause."""
    with FakeVRChat() as vrc:
        h = rig(vrc, tmp_path, marker=None)
        try:
            h.bridge.osc._peer_http = None
            with caplog.at_level("WARNING"):
                h.slot(1)
            assert h.sent() == []
            assert "pinned_manifest_id" in caplog.text
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
