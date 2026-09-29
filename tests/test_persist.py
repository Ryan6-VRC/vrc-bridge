"""Bridge persistence: one swap's worth of avatar state, written back through the Restore handshake.

Intent before test, per `docs/design.md`. Every case here is driven end to end over loopback: the
fake is the client, `FakeVRChat.emit` is its out-port stream, its `echo_inbound` is the client's
echo of every inbound write, and an `Avatar` answering on `on_receive` is the avatar half of the
handshake. The intents the suite exists to hold:

* **Restore exactly one swap to the same prefab.** A -> A' restores; A -> B -> A', a reload of the
  worn avatar (world join or Reset Avatar, one event on the wire), a failed swap followed by a real
  one, and a change of prefab all forget. An OSC change naming the worn avatar is an echo that
  reloads nothing, and changes nothing.
* **The last checkpoint before `Boot` is the one restored**, which after an OSC swap is the one
  taken at apply, not at the request-time echo.
* **Idempotent per value.** Doubled delivery of everything, and the bridge's own echoed 1 and 3,
  advance nothing.
* **Each value goes back with the type it arrived with.** An int written to a declared float
  writes garbage, so a whole-number float must stay a float and a bool must stay a bool.

Timing: thread-per-datagram dispatch means two datagrams sent back to back can reach the mapping
in either order, so the avatar's boot steps are spaced by `STEP`, as the real ones are by frames.
A same-id re-announcement is spaced past `REFIRE_FOLD_WINDOW_SECS`, as every real one is by
seconds, or the manager folds it as a twin.
"""
import random
import time

import pytest

import vrbridge.mappings.osc_persist as osc_persist
from vrbridge.engine import VRBridge
from vrbridge.mappings.osc_persist import (AVATAR_CHANGE_ADDR, NAMESPACE_ROOT,
                                           BridgePersistMapping)
from vrbridge.osc_manager import REFIRE_FOLD_WINDOW_SECS

from .fake_vrchat import FakeVRChat

A = "avtr_aaaaaaaa-0000-0000-0000-000000000001"
A2 = "avtr_aaaaaaaa-0000-0000-0000-000000000002"   # A', a different avatar with the same prefab
B = "avtr_bbbbbbbb-0000-0000-0000-000000000001"
UNWEARABLE = "avtr_ffffffff-0000-0000-0000-000000000000"

STEP = 0.05
REPEAT_GAP = REFIRE_FOLD_WINDOW_SECS + 0.1


def addr(ns: str, leaf: str) -> str:
    return f"{NAMESPACE_ROOT}{ns}/{leaf}"


def wait_for(cond, timeout=3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.005)
    return False


class Avatar:
    """The avatar half of the handshake, answering the bridge's Restore writes.

    `answer_request` / `answer_written` switch off the answer to 1 and to 3, for the timeouts.
    `reply_first` sends the answer before the client's echo of the write it answers, which is the
    reverse of the usual arrival order on the Restore address; the fake's own echo is switched off
    for it and the echo sent here instead, spaced so the order is the one intended.
    """

    def __init__(self, vrc: FakeVRChat):
        self.vrc = vrc
        self.answer_request = True
        self.answer_written = True
        self.reply_first = False

    def receive(self, address, value):
        reply = None
        if address.endswith("/Restore"):
            if value == 1 and self.answer_request:
                reply = 2
            elif value == 3 and self.answer_written:
                reply = 0
        if self.reply_first:
            if reply is not None:
                self.vrc.emit(address, reply)
                time.sleep(0.03)
            self.vrc.emit(address, value)
        elif reply is not None:
            self.vrc.emit(address, reply)


class Rig:
    """A started bridge pinned at the fake, the mapping registered and active, an avatar answering."""

    def __init__(self, vrc: FakeVRChat, *, copies=1, activate=True, **kw):
        self.vrc = vrc
        self.bridge = VRBridge(enable_steamvr=False, advertise=False, discover=False,
                               target=("127.0.0.1", vrc.osc_port))
        self.bridge.osc.start()
        vrc.out_port = self.bridge.osc.osc_port
        vrc.copies = copies
        vrc.echo_inbound = True
        self.avatar = Avatar(vrc)
        vrc.on_receive = self.avatar.receive
        self.m = BridgePersistMapping(self.bridge, **kw)
        self.m.register()
        if activate:
            self.m.activate()

    def close(self):
        self.bridge.osc.stop()

    # -- the client's side of the wire --

    def change(self, avatar_id):
        self.vrc.emit(AVATAR_CHANGE_ADDR, avatar_id)
        time.sleep(STEP)

    def boot(self, *namespaces, announce=5):
        """The incoming avatar's boot: every namespace's Announce, then its Boot a step later."""
        for ns in namespaces:
            self.vrc.emit(addr(ns, "Announce"), announce)
        time.sleep(STEP)
        for ns in namespaces:
            self.vrc.emit(addr(ns, "Boot"), random.uniform(0.001, 1.0))
        time.sleep(STEP)

    def load(self, avatar_id, *namespaces, announce=5):
        """An announcement at apply followed by the new avatar's boot: a menu swap, a join."""
        self.change(avatar_id)
        self.boot(*namespaces, announce=announce)

    def set(self, ns, leaf, value):
        self.vrc.emit(addr(ns, leaf), value)
        time.sleep(STEP)

    # -- what the bridge did --

    def restores(self, ns):
        return self.vrc.values_for(addr(ns, "Restore"))

    def written(self, ns, leaf):
        return self.vrc.values_for(addr(ns, leaf))

    def idle(self, ns):
        return self.m._ns[ns].awaiting is None

    def completed(self, ns):
        """Restore 1 and 3 written, and the avatar's 0 accepted."""
        return wait_for(lambda: self.restores(ns) == [1, 3] and self.idle(ns))


@pytest.fixture
def rig():
    rigs = []

    def make(**kw):
        vrc = FakeVRChat().__enter__()
        r = Rig(vrc, **kw)
        rigs.append((vrc, r))
        return r

    yield make
    for vrc, r in rigs:
        r.close()
        vrc.__exit__(None, None, None)


def worn_a_with(r, ns="GripSync", **payload):
    """World join into A, then A's prop state emitted: the bridge's normal starting point."""
    r.load(A, ns)
    for leaf, value in payload.items():
        r.set(ns, leaf, value)


# --------------------------------------------------------------------------
# The worked cases
# --------------------------------------------------------------------------

def test_a_menu_swap_to_the_same_prefab_restores(rig):
    """Intended: A -> A' by the menu, one announcement at apply, restores A's final state, and the
    payload is written before Restore=3 so the avatar reads it after the settle wait."""
    r = rig()
    worn_a_with(r, Word0=137.0, Detached=True)
    assert r.restores("GripSync") == [], "a join must not restore"

    r.load(A2, "GripSync")
    assert r.completed("GripSync"), f"no completed handshake: {r.restores('GripSync')}"
    assert r.written("GripSync", "Word0") == [137.0]
    assert r.written("GripSync", "Detached") == [True]
    msgs = r.vrc.messages
    three = msgs.index((addr("GripSync", "Restore"), 3))
    assert msgs.index((addr("GripSync", "Word0"), 137.0)) < three
    assert msgs.index((addr("GripSync", "Detached"), True)) < three


def test_restore_3_follows_the_payload_by_the_settle_wait(rig):
    """Intended: Restore=3 reaches the client a settle wait after the last payload write, because
    the client applies the latest value per parameter per frame and the avatar reads the payload
    on the 3. Order alone does not show it: a 3 sent straight after the payload is still after it."""
    r = rig()
    arrived = {}
    answer = r.vrc.on_receive

    def stamp(address, value):
        arrived.setdefault((address, value), time.monotonic())
        answer(address, value)

    r.vrc.on_receive = stamp
    worn_a_with(r, Word0=137.0, Detached=True)
    r.load(A2, "GripSync")
    assert r.completed("GripSync"), f"no completed handshake: {r.restores('GripSync')}"
    last_payload = max(arrived[(addr("GripSync", "Word0"), 137.0)],
                       arrived[(addr("GripSync", "Detached"), True)])
    waited = arrived[(addr("GripSync", "Restore"), 3)] - last_payload
    # A timer never fires early; the margin is for the payload's own arrival lag on loopback.
    assert waited >= osc_persist.WRITE_SETTLE_SECS * 0.8, f"3 came {waited * 1e3:.1f} ms after the payload"


def test_an_osc_swap_restores_from_the_checkpoint_taken_at_apply(rig):
    """Intended: an OSC swap announces twice, the echo at the request and again at apply, and the
    outgoing avatar keeps emitting in between. The later checkpoint is the final state."""
    r = rig()
    worn_a_with(r, Word0=1.0)
    r.change(A2)                      # the request-time echo
    r.set("GripSync", "Word0", 2.0)   # A is still worn through the download
    time.sleep(REPEAT_GAP)
    r.load(A2, "GripSync")            # the announcement at apply, then A' boots
    assert r.completed("GripSync")
    assert r.written("GripSync", "Word0") == [2.0], "restored from the echo's checkpoint"


def test_a_to_b_to_a_forgets(rig):
    """Intended: the lifetime is one swap. B carries no namespace, so A' boots with two avatars
    announced since A booted."""
    r = rig()
    worn_a_with(r, Word0=1.0)
    r.change(B)
    r.load(A2, "GripSync")
    time.sleep(0.2)
    assert r.restores("GripSync") == []


def test_reset_avatar_forgets(rig):
    """Intended: Reset Avatar, a world join and a rejoin are one event on the wire -- the worn id
    announced and the avatar reloaded -- and a join clears. So a reload of A restores nothing."""
    r = rig()
    worn_a_with(r, Word0=1.0)
    time.sleep(REPEAT_GAP)
    r.load(A, "GripSync")
    time.sleep(0.2)
    assert r.restores("GripSync") == []


def test_an_osc_change_naming_the_worn_avatar_changes_nothing(rig):
    """Intended: that change is echoed and reloads nothing, so it must neither count as a swap nor
    spoil the next one: a later A -> A' still restores A's latest state."""
    r = rig()
    worn_a_with(r, Word0=1.0)
    time.sleep(REPEAT_GAP)
    r.change(A)                       # echo only: no boot follows
    r.set("GripSync", "Word0", 3.0)
    assert r.restores("GripSync") == []
    r.load(A2, "GripSync")
    assert r.completed("GripSync")
    assert r.written("GripSync", "Word0") == [3.0]


def test_a_failed_osc_swap_before_a_real_one_forgets(rig):
    """Intended, and an accepted cost: the echo of a swap the client refused is indistinguishable
    from a request, so the unwearable id stays in the list and A -> A' after it restores nothing."""
    r = rig()
    worn_a_with(r, Word0=1.0)
    r.change(UNWEARABLE)
    r.load(A2, "GripSync")
    time.sleep(0.2)
    assert r.restores("GripSync") == []


def test_two_namespaces_on_one_avatar_restore_independently(rig):
    """Intended: each namespace is its own snapshot and handshake, and writes only its own names."""
    r = rig()
    r.load(A, "GripSync", "Lamp")
    r.set("GripSync", "Word0", 1.5)
    r.set("Lamp", "On", True)
    r.load(A2, "GripSync", "Lamp")
    assert r.completed("GripSync") and r.completed("Lamp")
    assert r.written("GripSync", "Word0") == [1.5]
    assert r.written("Lamp", "On") == [True]
    assert r.written("GripSync", "On") == [] and r.written("Lamp", "Word0") == []


def test_a_namespace_whose_prefab_changed_forgets_while_its_neighbour_restores(rig):
    """Intended: identity is per prefab. A' announcing a different Id under Lamp forgets Lamp, and
    that decision is Lamp's alone."""
    r = rig()
    r.load(A, "GripSync", "Lamp")
    r.set("GripSync", "Word0", 1.5)
    r.set("Lamp", "On", True)
    r.change(A2)
    r.vrc.emit(addr("GripSync", "Announce"), 5)
    r.vrc.emit(addr("Lamp", "Announce"), 6)
    time.sleep(STEP)
    r.vrc.emit(addr("GripSync", "Boot"), 0.25)
    r.vrc.emit(addr("Lamp", "Boot"), 0.75)
    assert r.completed("GripSync")
    time.sleep(0.2)
    assert r.restores("Lamp") == []


# --------------------------------------------------------------------------
# Wire types
# --------------------------------------------------------------------------

def test_every_wire_type_goes_back_with_the_type_it_arrived_with(rig):
    """Intended: replay as cached. A whole-number float is the trap: an int in its place writes
    garbage into a declared float, and a bool sent as an int is a different message."""
    r = rig()
    payload = {"Int": 3, "Float": 0.5, "WholeFloat": 137.0, "On": True, "Off": False,
               "Prop/Rig/Deep": 42.0}
    r.load(A, "GripSync")
    for leaf, v in payload.items():
        r.set("GripSync", leaf, v)
    r.load(A2, "GripSync")
    assert r.completed("GripSync")
    for leaf, v in payload.items():
        got = r.written("GripSync", leaf)
        assert got == [v], f"{leaf}: {got}"
        assert type(got[0]) is type(v), f"{leaf} came back as {type(got[0]).__name__}"


# --------------------------------------------------------------------------
# Delivery: doubled, reordered, and our own echo
# --------------------------------------------------------------------------

def test_doubled_delivery_of_everything_restores_once(rig):
    """Intended: idempotent per value. Every inbound message -- the announcements, Announce, Boot,
    payload, the avatar's 2 and 0, and the echo of every write -- arrives twice, and the bridge
    writes each thing once."""
    r = rig(copies=2)
    worn_a_with(r, Word0=137.0, Detached=True)
    r.load(A2, "GripSync")
    assert r.completed("GripSync"), r.restores("GripSync")
    time.sleep(0.2)
    assert r.restores("GripSync") == [1, 3]
    assert r.written("GripSync", "Word0") == [137.0]
    assert r.written("GripSync", "Detached") == [True]


def test_the_avatars_answer_arriving_before_our_echo_still_advances(rig):
    """Intended: on the Restore address the avatar's 2 can land before the echo of our 1, and its
    0 before the echo of our 3. The step is the value becoming 2 (or 0), whatever arrives around
    it, and the late echo is ignored rather than restarting anything."""
    r = rig()
    worn_a_with(r, Word0=1.0)
    r.vrc.echo_inbound = False
    r.avatar.reply_first = True
    r.load(A2, "GripSync")
    assert r.completed("GripSync"), r.restores("GripSync")
    time.sleep(0.2)
    assert r.restores("GripSync") == [1, 3]
    assert r.written("GripSync", "Word0") == [1.0]


@pytest.mark.parametrize("not_a_boot", [0.0, 1, True, 1.5])
def test_a_boot_outside_0_1_is_not_a_boot(rig, not_a_boot):
    """Intended: a boot draws a float from (0, 1]. The avatar resetting its namespace writes Boot
    back to 0.0 before its real draw; taken as a boot, that 0 would start a handshake against an
    animator that has not booted and the real boot would then find the swap already consumed.
    So it starts nothing and records nothing, and the real boot after it restores normally."""
    r = rig()
    worn_a_with(r, Word0=1.0)
    r.change(A2)
    r.set("GripSync", "Announce", 5)
    r.set("GripSync", "Boot", not_a_boot)
    time.sleep(0.2)
    assert r.restores("GripSync") == [], f"Boot={not_a_boot!r} started a handshake"
    r.set("GripSync", "Boot", 0.5)
    assert r.completed("GripSync"), r.restores("GripSync")
    assert r.written("GripSync", "Word0") == [1.0]


def test_a_payload_listener_fired_out_of_order_keeps_the_newer_value(rig):
    """Intended: two datagrams for one address can reach the mapping in reverse arrival order
    (the listener fires after the cache lock is released). The snapshot must hold the value the
    avatar holds -- the later one -- so a stale fire after it must not overwrite it."""
    r = rig()
    worn_a_with(r, Word0=1.0)
    newer = addr("GripSync", "Word0")
    r.set("GripSync", "Word0", 2.0)
    r.bridge._on_osc_event(newer, 1.0)    # the older datagram's listener, firing late
    r.load(A2, "GripSync")
    assert r.completed("GripSync")
    assert r.written("GripSync", "Word0") == [2.0]


def test_a_restored_value_survives_a_second_swap(rig):
    """Intended: A -> A' -> A'' with the prop untouched restores the same place twice. The value
    A' holds arrives only as the client's echo of our write, and that echo equals what A last
    sent -- so unless the manager's cache forgets the namespace at boot, the change filter eats
    the echo and the second snapshot silently lacks it."""
    r = rig()
    worn_a_with(r, Word0=137.0)
    r.load(A2, "GripSync")
    assert r.completed("GripSync")
    r.vrc.messages.clear()
    r.load(A, "GripSync")
    assert r.completed("GripSync")
    assert r.written("GripSync", "Word0") == [137.0], "the second swap lost the restored value"


def test_values_sent_before_boot_survive_a_boot_that_restores_nothing(rig):
    """Intended: the walks commit the home pose about 0.2 s into a load, before `Boot`, and a
    name that never changes afterwards is never re-sent. A join restores nothing, yet the next
    swap must replay those pre-boot values; emptying them at the boot would restore that name to
    its declared default, a wrong pose."""
    r = rig()
    r.change(A)                           # the join
    r.set("GripSync", "Word0", 2.0)       # the home-pose commit, ahead of the boot, never again
    r.set("GripSync", "Detached", True)
    r.boot("GripSync")
    time.sleep(0.2)
    assert r.restores("GripSync") == [], "a join must not restore"
    r.load(A2, "GripSync")
    assert r.completed("GripSync")
    assert r.written("GripSync", "Word0") == [2.0]
    assert r.written("GripSync", "Detached") == [True]


def test_an_incoming_value_equal_to_the_outgoing_one_is_still_seen(rig):
    """Intended: the manager's change filter outlives the avatar, so the reloaded avatar's pre-boot
    commit of the value the outgoing one last sent would be eaten unless the filter is reset at
    the announcement. Reset Avatar with the prop where it was is exactly that case, and the swap
    after it must still restore the value."""
    r = rig()
    worn_a_with(r, Word0=1.0)
    time.sleep(REPEAT_GAP)
    r.change(A)                           # Reset Avatar
    r.set("GripSync", "Word0", 1.0)       # the same pose, committed before the boot
    r.boot("GripSync")
    time.sleep(0.2)
    assert r.restores("GripSync") == []
    r.load(A2, "GripSync")
    assert r.completed("GripSync")
    assert r.written("GripSync", "Word0") == [1.0], "the equal pre-boot value was filtered away"


def test_a_new_send_target_clears_every_namespace_as_a_join(rig):
    """Intended: a newly selected target is a client that started or restarted, which is a join,
    and any join clears. A restarted client that comes back wearing a different avatar with the
    same prefab must not read as one swap from the old one. Once the namespace has booted in
    front of the new client, swaps restore again -- which needs the equal `Announce` the new
    client sends to get past the change filter."""
    r = rig()
    worn_a_with(r, Word0=1.0)
    r.bridge._on_target_selected(("127.0.0.1", 9000))   # as OSCManager fires it
    r.load(A2, "GripSync")                # the restarted client joins on A'
    time.sleep(0.2)
    assert r.restores("GripSync") == [], "a restart read as a swap"

    r.set("GripSync", "Word0", 2.0)
    r.load(A, "GripSync")
    assert r.completed("GripSync"), f"no restore after the rejoin: {r.restores('GripSync')}"
    assert r.written("GripSync", "Word0") == [2.0]


def test_a_2_left_from_an_abandoned_handshake_does_not_eat_the_next_one(rig, monkeypatch):
    """Intended: a 2 that arrived after its handshake expired stays in the manager's cache, and
    against a peer that does not echo our 1 (nothing then moves the cache) the next avatar's 2
    would equal it and be filtered. Restore is forgotten before each 1 so the next 2 is an edge."""
    monkeypatch.setattr(osc_persist, "ACK_WAIT_SECS", 0.3)
    r = rig()
    r.vrc.echo_inbound = False
    worn_a_with(r, Word0=1.0)
    r.avatar.answer_request = False
    r.load(A2, "GripSync")
    assert wait_for(lambda: r.idle("GripSync") and r.restores("GripSync") == [1])
    time.sleep(0.35)
    r.set("GripSync", "Restore", 2)       # the late answer, now cached
    r.set("GripSync", "Word0", 2.0)
    r.avatar.answer_request = True
    r.vrc.messages.clear()
    r.load(A, "GripSync")
    assert r.completed("GripSync"), f"the next handshake stalled: {r.restores('GripSync')}"


# --------------------------------------------------------------------------
# Timeouts: each abandons, drops the snapshot, never retries
# --------------------------------------------------------------------------

@pytest.fixture
def short_ack(monkeypatch):
    monkeypatch.setattr(osc_persist, "ACK_WAIT_SECS", 0.3)


def test_no_answer_to_1_abandons_and_our_echoed_1_is_not_an_answer(rig, short_ack):
    """Intended: the bridge acts only on 2 and 0. With the avatar silent the only thing on the
    Restore address is our own 1 echoed back, which must not stand in for the avatar's 2; the
    wait expires, and a 2 arriving after that writes nothing."""
    r = rig()
    worn_a_with(r, Word0=1.0)
    r.avatar.answer_request = False
    r.load(A2, "GripSync")
    assert wait_for(lambda: r.restores("GripSync") == [1])
    assert wait_for(lambda: r.idle("GripSync")), "the wait for 2 never expired"
    assert r.written("GripSync", "Word0") == [], "our own echoed 1 advanced the handshake"
    r.set("GripSync", "Restore", 2)       # the avatar, too late
    time.sleep(0.2)
    assert r.written("GripSync", "Word0") == [], "a late 2 restored a dropped snapshot"
    assert r.restores("GripSync") == [1], "an abandoned handshake was retried"


def test_no_answer_to_3_abandons_and_our_echoed_3_is_not_an_answer(rig, short_ack):
    """Intended: after 3 the bridge waits for the avatar's 0, and our own 3 echoed back is not
    it. The handshake stays open until the wait expires, then is dropped without a retry."""
    r = rig()
    worn_a_with(r, Word0=1.0)
    r.avatar.answer_written = False
    r.load(A2, "GripSync")
    assert wait_for(lambda: r.restores("GripSync") == [1, 3])
    time.sleep(0.1)
    assert not r.idle("GripSync"), "our own echoed 3 was taken as the avatar's 0"
    assert wait_for(lambda: r.idle("GripSync")), "the wait for 0 never expired"
    assert r.m._ns["GripSync"].snapshot is None
    time.sleep(0.4)
    assert r.restores("GripSync") == [1, 3], "an abandoned handshake was retried"


def test_a_dropped_payload_write_withholds_3(rig, monkeypatch):
    """Intended: all or nothing. If any payload write is dropped the bridge never writes 3, even
    though it still could, so the avatar's own wait expires and it boots from defaults rather
    than placing from a partial payload. The drop is staged in the manager's send, where
    production drops one (no target, a socket error), and only for one name."""
    r = rig()
    worn_a_with(r, Word0=1.0, Detached=True)
    real_send = r.bridge.osc.send
    dropped = addr("GripSync", "Detached")
    monkeypatch.setattr(r.bridge.osc, "send",
                        lambda a, v: False if a == dropped else real_send(a, v))
    r.load(A2, "GripSync")
    assert wait_for(lambda: r.written("GripSync", "Word0") == [1.0])
    assert wait_for(lambda: r.idle("GripSync"))
    time.sleep(0.2)
    assert r.restores("GripSync") == [1], "3 was written after a payload write was dropped"


# --------------------------------------------------------------------------
# Validity edges
# --------------------------------------------------------------------------

def test_an_announce_of_0_never_restores(rig):
    """Intended: 0 is the shipped Id and means persistence is off, even when every other rule
    holds -- one swap, and the same value both sides."""
    r = rig()
    r.load(A, "GripSync", announce=0)
    r.set("GripSync", "Word0", 1.0)
    r.load(A2, "GripSync", announce=0)
    time.sleep(0.2)
    assert r.restores("GripSync") == []


def test_a_bridge_started_after_the_avatar_loaded_waits_for_a_boot(rig):
    """Intended: values that arrive with no boot seen cannot be attributed to a load, so the first
    swap restores nothing; once a boot has been seen the next swap does.

    The bridge here starts between A's Announce and its Boot, which is the window where the
    baseline is the only rule that decides: a bridge started any later never sees the outgoing
    Announce, and the checkpoint's Announce mismatching the incoming one already refuses."""
    r = rig()
    r.set("GripSync", "Announce", 5)      # caught A's Announce, missed its Boot
    r.set("GripSync", "Word0", 1.0)
    r.load(A2, "GripSync")
    time.sleep(0.2)
    assert r.restores("GripSync") == []

    r.set("GripSync", "Word0", 2.0)
    r.load(A, "GripSync")
    assert r.completed("GripSync")
    assert r.written("GripSync", "Word0") == [2.0]


def test_a_same_id_reload_is_a_swap_only_under_the_test_switch(rig):
    """Intended: for a peer that cannot change avatars (the emulator's play, stop, play announces
    the same id), `treat_reload_as_swap` makes that reload restore. Off by default, where the
    same sequence is a reload and forgets."""
    for switch, expect in ((False, []), (True, [1, 3])):
        r = rig(treat_reload_as_swap=switch)
        worn_a_with(r, Word0=1.0)
        time.sleep(REPEAT_GAP)
        r.load(A, "GripSync")
        if expect:
            assert r.completed("GripSync")
        else:
            time.sleep(0.2)
        assert r.restores("GripSync") == expect, f"treat_reload_as_swap={switch}"


@pytest.mark.parametrize("router_name", ["default", "camera", "remy"])
def test_every_shipped_router_runs_persistence_in_every_mode(router_name):
    """Intended: `vrbridge --router <any shipped name>` restores with nothing else to type, and a
    swap can happen in any mode, so the mapping is registered active and no mode switch may
    disable it. Driven through each router's own mode inputs, then its evaluate()."""
    from vrbridge.cli import ROUTERS
    from vrbridge.routers import (USERCAMERA_MODE_ADDR, VIRTUALLENS_ENABLE_ADDR,
                                  VRCL_FEATURE_TOGGLE_ADDR)
    bridge = VRBridge(enable_steamvr=False, advertise=False, discover=False)
    router = ROUTERS[router_name](bridge)
    persist = router._mappings.get("osc_persist")
    assert isinstance(persist, BridgePersistMapping), f"{router_name} does not register it"
    assert persist.enabled and not persist._reload_as_swap
    router.evaluate()
    for address, value in ((USERCAMERA_MODE_ADDR, 1.0), (VIRTUALLENS_ENABLE_ADDR, 1.0),
                           (VRCL_FEATURE_TOGGLE_ADDR, 1.0), (AVATAR_CHANGE_ADDR, A),
                           (USERCAMERA_MODE_ADDR, 0.0)):
        bridge.osc._update_cache_and_fire(address, value)
        router.evaluate()
        assert persist.enabled, f"{router_name} disabled it after {address}={value!r}"


def test_a_disabled_mapping_writes_nothing_but_keeps_watching(rig):
    """Intended: `enabled` belongs to the router. Disabled, a valid snapshot is not restored; and
    since observation is ungated, re-enabling resumes from state that saw every swap."""
    r = rig(activate=False)
    worn_a_with(r, Word0=1.0)
    r.load(A2, "GripSync")
    time.sleep(0.2)
    assert r.restores("GripSync") == []

    r.m.activate()
    r.set("GripSync", "Word0", 2.0)
    r.load(A, "GripSync")
    assert r.completed("GripSync")
    assert r.written("GripSync", "Word0") == [2.0]
