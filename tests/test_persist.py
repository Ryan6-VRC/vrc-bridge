"""Bridge persistence: one swap's worth of avatar state, written back ahead of `Restore` 1.

Intent before test, per `docs/design.md`. Every case here is driven end to end over loopback: the
fake is the client, `FakeVRChat.emit` is its out-port stream and its `echo_inbound` is the client's
echo of every inbound write. The avatar answers nothing, so what it would place from is read off
the wire: the payload written before the 1 (`Rig.placed_from`). The intents the suite holds:

* **Restore exactly one swap to the same prefab.** A -> A' restores; A -> B -> A', a reload of the
  worn avatar (world join or Reset Avatar, one event on the wire), a failed swap followed by a real
  one, and a change of prefab all forget. An OSC change naming the worn avatar is an echo that
  reloads nothing, and changes nothing.
* **Validity waits for the incoming avatar's own `Announce`,** in either order against its `Boot`,
  and never matches an avatar that sent none against the outgoing avatar's value.
* **Anything that moves the avatar during a wait abandons the exchange,** and the timer that lost
  the race writes nothing.
* **Idempotent per value, and each value goes back with the type it arrived with.**

Timing: thread-per-datagram dispatch means two datagrams sent back to back can reach the mapping
in either order, so the avatar's boot steps are spaced by `STEP`, as the real ones are by frames.
`ANNOUNCE_SETTLE_SECS` is shortened for speed everywhere but the one test that holds the real
values. A same-id re-announcement is spaced past `REFIRE_FOLD_WINDOW_SECS`, as every real one is
by seconds, or the manager folds it as a twin.
"""
import random
import time
from unittest import mock

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

REAL_ANNOUNCE_SETTLE = osc_persist.ANNOUNCE_SETTLE_SECS
REAL_WRITE_SETTLE = osc_persist.WRITE_SETTLE_SECS
FAST_ANNOUNCE_SETTLE = 0.1

STEP = 0.05
REPEAT_GAP = REFIRE_FOLD_WINDOW_SECS + 0.1
# Long enough for a decision and its Restore write to have happened, at the shortened settle.
QUIET = 0.4


@pytest.fixture(autouse=True)
def fast_settle(monkeypatch):
    monkeypatch.setattr(osc_persist, "ANNOUNCE_SETTLE_SECS", FAST_ANNOUNCE_SETTLE)


def addr(ns: str, leaf: str) -> str:
    return f"{NAMESPACE_ROOT}{ns}/{leaf}"


def wait_for(cond, timeout=3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.005)
    return False


class Rig:
    """A started bridge pinned at the fake, the mapping registered and active."""

    def __init__(self, vrc: FakeVRChat, *, copies=1, activate=True, **kw):
        self.vrc = vrc
        self.bridge = VRBridge(enable_steamvr=False, advertise=False, discover=False,
                               target=("127.0.0.1", vrc.osc_port))
        self.bridge.osc.start()
        vrc.out_port = self.bridge.osc.osc_port
        vrc.copies = copies
        vrc.echo_inbound = True
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

    def completed(self, ns):
        """Restore 1 written, once."""
        return wait_for(lambda: self.restores(ns) == [1])

    def placed_from(self, ns):
        """What the avatar reads on the 1: each payload leaf's last value written before it."""
        restore, prefix, seen = addr(ns, "Restore"), addr(ns, ""), {}
        for a, v in list(self.vrc.messages):
            if a == restore and v == 1:
                return seen
            if a.startswith(prefix) and a != restore:
                seen[a[len(prefix):]] = v
        return None


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
    """Intended: A -> A' by the menu, one announcement at apply, restores A's final state, every
    value written before Restore 1, which is written once and never followed by anything."""
    r = rig()
    worn_a_with(r, Word0=137.0, Detached=True)
    time.sleep(QUIET)
    assert r.restores("GripSync") == [], "a join must not restore"

    r.load(A2, "GripSync")
    assert r.completed("GripSync"), f"no Restore 1: {r.restores('GripSync')}"
    assert r.placed_from("GripSync") == {"Word0": 137.0, "Detached": True}
    time.sleep(QUIET)
    assert r.restores("GripSync") == [1], "the bridge wrote Restore again"


def test_restore_1_follows_the_payload_by_the_write_settle_wait(rig):
    """Intended: the 1 reaches the client a settle wait after the last payload write, because the
    client applies the latest value per parameter per frame and the avatar reads the payload on
    the 1. Order alone does not show it: a 1 sent straight after the payload is still after it."""
    r = rig()
    arrived = {}
    r.vrc.on_receive = lambda a, v: arrived.setdefault((a, v), time.monotonic())
    worn_a_with(r, Word0=137.0, Detached=True)
    r.load(A2, "GripSync")
    assert r.completed("GripSync")
    last_payload = max(arrived[(addr("GripSync", "Word0"), 137.0)],
                       arrived[(addr("GripSync", "Detached"), True)])
    waited = arrived[(addr("GripSync", "Restore"), 1)] - last_payload
    # A timer never fires early; the margin is for the payload's own arrival lag on loopback.
    assert waited >= REAL_WRITE_SETTLE * 0.8, f"1 came {waited * 1e3:.1f} ms after the payload"


def test_restore_1_is_not_written_before_both_waits_on_the_real_constants(rig, monkeypatch):
    """Intended: the contract's timing, on the shipped values. The avatar sizes its window from
    ANNOUNCE_SETTLE_SECS + WRITE_SETTLE_SECS after Boot, and a 1 written sooner would mean the
    decision did not wait for a late Announce."""
    monkeypatch.setattr(osc_persist, "ANNOUNCE_SETTLE_SECS", REAL_ANNOUNCE_SETTLE)
    r = rig()
    arrived = {}
    r.vrc.on_receive = lambda a, v: arrived.setdefault((a, v), time.monotonic())
    worn_a_with(r, Word0=1.0)
    r.change(A2)
    r.set("GripSync", "Announce", 5)
    booted = time.monotonic()               # before the send, so any lag only adds
    r.vrc.emit(addr("GripSync", "Boot"), 0.5)
    assert r.completed("GripSync")
    took = arrived[(addr("GripSync", "Restore"), 1)] - booted
    assert took >= REAL_ANNOUNCE_SETTLE + REAL_WRITE_SETTLE, f"1 came {took * 1e3:.1f} ms after Boot"


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
    assert r.placed_from("GripSync") == {"Word0": 2.0}, "restored from the echo's checkpoint"


def test_a_to_b_to_a_forgets(rig):
    """Intended: the lifetime is one swap. B carries no namespace, so A' boots with two avatars
    announced since A booted."""
    r = rig()
    worn_a_with(r, Word0=1.0)
    r.change(B)
    r.load(A2, "GripSync")
    time.sleep(QUIET)
    assert r.restores("GripSync") == []


def test_away_and_back_to_the_same_avatar_forgets(rig):
    """Intended: A -> B -> A is two changes even though the avatar returned to is the one the
    namespace booted on. The swap away is announced once here, as a swap to an avatar that
    carries no OSC parameters was measured to be; the return is an echo and an announcement at
    apply."""
    r = rig()
    worn_a_with(r, Word0=1.0)
    r.change(B)
    time.sleep(REPEAT_GAP)
    r.change(A)                       # the request-time echo
    time.sleep(REPEAT_GAP)
    r.load(A, "GripSync")             # the announcement at apply, then the boot
    time.sleep(QUIET)
    assert r.restores("GripSync") == []


def test_reset_avatar_forgets(rig):
    """Intended: Reset Avatar, a world join and a rejoin are one event on the wire -- the worn id
    announced and the avatar reloaded -- and a join clears. So a reload of A restores nothing."""
    r = rig()
    worn_a_with(r, Word0=1.0)
    time.sleep(REPEAT_GAP)
    r.load(A, "GripSync")
    time.sleep(QUIET)
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
    assert r.placed_from("GripSync") == {"Word0": 3.0}


def test_a_failed_osc_swap_before_a_real_one_forgets(rig):
    """Intended, and an accepted cost: the echo of a swap the client refused is indistinguishable
    from a request, so the unwearable id stays in the list and A -> A' after it restores nothing."""
    r = rig()
    worn_a_with(r, Word0=1.0)
    r.change(UNWEARABLE)
    r.load(A2, "GripSync")
    time.sleep(QUIET)
    assert r.restores("GripSync") == []


def test_two_namespaces_on_one_avatar_restore_independently(rig):
    """Intended: each namespace is its own snapshot and exchange, and writes only its own names."""
    r = rig()
    r.load(A, "GripSync", "Lamp")
    r.set("GripSync", "Word0", 1.5)
    r.set("Lamp", "On", True)
    r.load(A2, "GripSync", "Lamp")
    assert r.completed("GripSync") and r.completed("Lamp")
    assert r.placed_from("GripSync") == {"Word0": 1.5}
    assert r.placed_from("Lamp") == {"On": True}


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
    time.sleep(QUIET)
    assert r.restores("Lamp") == []


def test_boot_before_its_announce_restores(rig):
    """Intended: the avatar writes Announce and Boot in one state, so either can reach the bridge
    first. An Announce arriving inside the settle wait after its Boot is the incoming avatar's,
    and the swap restores."""
    r = rig()
    worn_a_with(r, Word0=1.0)
    r.change(A2)
    r.set("GripSync", "Boot", 0.5)
    r.set("GripSync", "Announce", 5)
    assert r.completed("GripSync"), r.restores("GripSync")
    assert r.placed_from("GripSync") == {"Word0": 1.0}


def test_a_boot_from_an_avatar_that_sends_no_announce_does_not_restore(rig):
    """Intended: the outgoing avatar's Announce value stands across the change, because the next
    checkpoint needs it; only an Announce arriving since the change speaks for the incoming
    avatar. One that boots and sends none is not matched against the outgoing value."""
    r = rig()
    worn_a_with(r, Word0=1.0)
    r.change(A2)
    r.set("GripSync", "Boot", 0.5)
    time.sleep(QUIET)
    assert r.restores("GripSync") == []
    assert r.written("GripSync", "Word0") == []


@pytest.mark.parametrize("announce_first", [True, False], ids=["announce-first", "boot-first"])
def test_an_incoming_announce_that_differs_does_not_restore_in_either_order(rig, announce_first):
    """Intended: a different Id is a different prefab, whether its Announce arrives before its
    Boot or inside the settle wait after it."""
    r = rig()
    worn_a_with(r, Word0=1.0)
    r.change(A2)
    if announce_first:
        r.set("GripSync", "Announce", 6)
        r.set("GripSync", "Boot", 0.5)
    else:
        r.set("GripSync", "Boot", 0.5)
        r.set("GripSync", "Announce", 6)
    time.sleep(QUIET)
    assert r.restores("GripSync") == []
    assert r.written("GripSync", "Word0") == []


# --------------------------------------------------------------------------
# Abandonment: anything that moves the avatar during a wait
# --------------------------------------------------------------------------

@pytest.fixture
def long_settle(monkeypatch):
    """Room to land an event inside the wait without racing it."""
    monkeypatch.setattr(osc_persist, "ANNOUNCE_SETTLE_SECS", 0.3)


def test_an_avatar_change_inside_the_settle_wait_abandons_the_decision(rig, long_settle):
    """Intended: the Boot's animator is gone once another change arrives, so its decision must
    not run -- even when, by the time its timer fires, an equal Announce has arrived since that
    change and every captured rule holds."""
    r = rig()
    worn_a_with(r, Word0=1.0)
    r.change(A2)
    r.vrc.emit(addr("GripSync", "Boot"), 0.5)
    time.sleep(STEP)
    r.change(B)                           # inside the wait
    r.vrc.emit(addr("GripSync", "Announce"), 5)
    time.sleep(0.3 + QUIET)
    assert r.restores("GripSync") == []
    assert r.written("GripSync", "Word0") == []


def test_a_further_boot_inside_the_settle_wait_abandons_the_first(rig, long_settle):
    """Intended: a second Boot is a second load; the first decision is dropped and the second
    starts its own, which finds no change since the first Boot and restores nothing."""
    r = rig()
    worn_a_with(r, Word0=1.0)
    r.change(A2)
    r.set("GripSync", "Announce", 5)
    r.vrc.emit(addr("GripSync", "Boot"), 0.25)
    time.sleep(STEP)
    r.vrc.emit(addr("GripSync", "Boot"), 0.75)
    time.sleep(0.3 + QUIET)
    assert r.restores("GripSync") == []
    assert r.written("GripSync", "Word0") == []


def test_a_new_target_inside_the_settle_wait_abandons_even_a_same_name_namespace(rig, long_settle):
    """Intended: a newly selected target deletes every namespace, and a timer from before it must
    not act on a namespace of the same name that booted after it, with state it never had."""
    r = rig()
    worn_a_with(r, Word0=1.0)
    r.change(A2)
    r.set("GripSync", "Announce", 5)
    r.vrc.emit(addr("GripSync", "Boot"), 0.25)
    time.sleep(STEP)
    r.bridge._on_target_selected(("127.0.0.1", 9000))
    r.boot("GripSync")                    # the same name, new, booting inside the old wait
    time.sleep(0.3 + QUIET)
    assert r.restores("GripSync") == []
    assert r.written("GripSync", "Word0") == []


def test_an_avatar_change_inside_the_write_settle_wait_withholds_restore(rig, monkeypatch):
    """Intended: the payload is out, but the avatar it was for is being replaced; the 1 would tell
    the next animator to place from values that were never its snapshot."""
    monkeypatch.setattr(osc_persist, "WRITE_SETTLE_SECS", 0.3)
    r = rig()
    worn_a_with(r, Word0=1.0)
    r.load(A2, "GripSync")
    assert wait_for(lambda: r.written("GripSync", "Word0") == [1.0])
    r.change(B)
    time.sleep(0.3 + QUIET)
    assert r.restores("GripSync") == []


@pytest.mark.parametrize("phase", ["settle", "write"])
def test_a_timer_that_lost_the_race_to_an_abandonment_is_a_no_op(rig, monkeypatch, phase):
    """Intended: a timer can fire and block on the mapping's lock while the change that abandons
    it holds the lock, so cancelling it is not enough; run after the change, it must write
    nothing. Staged by running the abandoned timer's own call after the change, with an equal
    Announce arrived since, so every rule it would check holds."""
    monkeypatch.setattr(osc_persist, "ANNOUNCE_SETTLE_SECS", 0.3)
    monkeypatch.setattr(osc_persist, "WRITE_SETTLE_SECS", 0.3)
    r = rig()
    worn_a_with(r, Word0=1.0)
    r.change(A2)
    r.set("GripSync", "Announce", 5)
    r.vrc.emit(addr("GripSync", "Boot"), 0.5)
    if phase == "write":
        assert wait_for(lambda: r.written("GripSync", "Word0") == [1.0])
    else:
        time.sleep(STEP)
    t = r.m._ns["GripSync"].timer
    r.change(B)                           # abandons; the timer has "already fired"
    r.set("GripSync", "Announce", 5)
    t.function(*t.args)
    time.sleep(QUIET)
    assert r.restores("GripSync") == []
    if phase == "settle":
        assert r.written("GripSync", "Word0") == []


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
    placed = r.placed_from("GripSync")
    for leaf, v in payload.items():
        assert placed.get(leaf) == v, f"{leaf}: {placed.get(leaf)!r}"
        assert type(placed[leaf]) is type(v), f"{leaf} came back as {type(placed[leaf]).__name__}"


# --------------------------------------------------------------------------
# Delivery: doubled, reordered, and our own echo
# --------------------------------------------------------------------------

def test_doubled_delivery_of_everything_restores_once(rig):
    """Intended: idempotent per value. Every inbound message -- the announcements, Announce, Boot,
    payload, and the echo of every write -- arrives twice, and the bridge writes each thing once."""
    r = rig(copies=2)
    worn_a_with(r, Word0=137.0, Detached=True)
    r.load(A2, "GripSync")
    assert r.completed("GripSync"), r.restores("GripSync")
    time.sleep(QUIET)
    assert r.restores("GripSync") == [1]
    assert r.written("GripSync", "Word0") == [137.0]
    assert r.written("GripSync", "Detached") == [True]


@pytest.mark.parametrize("not_a_boot", [0.0, 1, True, 1.5])
def test_a_boot_outside_0_1_is_not_a_boot(rig, not_a_boot):
    """Intended: a boot draws a float from (0, 1]. The emulator re-sends the declared 0.0 on load;
    taken as a boot, it would abandon or consume the swap before the real draw. So it starts
    nothing and records nothing, and the real boot after it restores normally."""
    r = rig()
    worn_a_with(r, Word0=1.0)
    r.change(A2)
    r.set("GripSync", "Announce", 5)
    r.set("GripSync", "Boot", not_a_boot)
    time.sleep(QUIET)
    assert r.restores("GripSync") == [], f"Boot={not_a_boot!r} started an exchange"
    r.set("GripSync", "Boot", 0.5)
    assert r.completed("GripSync"), r.restores("GripSync")
    assert r.placed_from("GripSync") == {"Word0": 1.0}


def test_a_boot_redelivered_after_a_zero_is_still_one_boot(rig):
    """Intended: a doubled Boot is one boot. The change filter folds adjacent copies, but doubled
    and reordered delivery around the declared 0.0 the avatar also writes puts the draw back past
    the filter; taken as a further Boot, it would abandon the swap and restore nothing."""
    r = rig()
    worn_a_with(r, Word0=1.0)
    r.change(A2)
    r.set("GripSync", "Announce", 5)
    for v in (0.5, 0.0, 0.5):
        r.vrc.emit(addr("GripSync", "Boot"), v)
        time.sleep(0.01)
    assert r.completed("GripSync"), r.restores("GripSync")
    assert r.placed_from("GripSync") == {"Word0": 1.0}


def test_values_from_before_the_announcement_are_not_the_incoming_avatars(rig):
    """Intended: at Boot the live values become only what arrived since the last announcement.
    A value the outgoing avatar sent before it is that avatar's, and the next swap must not
    restore it as if the incoming avatar held it."""
    r = rig()
    worn_a_with(r, Word0=1.0, Extra=9.0)
    time.sleep(REPEAT_GAP)
    r.change(A)                           # Reset Avatar: A reloads
    r.set("GripSync", "Word0", 2.0)       # its home-pose commit; Extra is not re-sent
    r.boot("GripSync")
    time.sleep(QUIET)
    r.load(A2, "GripSync")
    assert r.completed("GripSync")
    assert r.placed_from("GripSync") == {"Word0": 2.0}


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
    assert r.placed_from("GripSync") == {"Word0": 2.0}


def test_a_restored_value_survives_a_second_swap(rig):
    """Intended: A -> A' -> A'' with the prop untouched restores the same place twice. The value
    A' holds arrives only as the client's echo of our write, and that echo equals what A last
    sent -- so unless the manager's cache forgets the namespace at the announcement, the change
    filter eats the echo and the second snapshot silently lacks it."""
    r = rig()
    worn_a_with(r, Word0=137.0)
    r.load(A2, "GripSync")
    assert r.completed("GripSync")
    r.vrc.messages.clear()
    r.load(A, "GripSync")
    assert r.completed("GripSync")
    assert r.placed_from("GripSync") == {"Word0": 137.0}, "the second swap lost the restored value"


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
    time.sleep(QUIET)
    assert r.restores("GripSync") == [], "a join must not restore"
    r.load(A2, "GripSync")
    assert r.completed("GripSync")
    assert r.placed_from("GripSync") == {"Word0": 2.0, "Detached": True}


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
    time.sleep(QUIET)
    assert r.restores("GripSync") == []
    r.load(A2, "GripSync")
    assert r.completed("GripSync")
    assert r.placed_from("GripSync") == {"Word0": 1.0}, "the equal pre-boot value was filtered away"


def test_a_new_send_target_clears_every_namespace_as_a_join(rig):
    """Intended: a newly selected target is a client that started or restarted, which is a join,
    and any join clears. A restarted client that comes back wearing a different avatar with the
    same prefab must not read as one swap from the old one. Once the namespace has booted in
    front of the new client, swaps restore again."""
    r = rig()
    worn_a_with(r, Word0=1.0)
    time.sleep(QUIET)
    r.bridge._on_target_selected(("127.0.0.1", 9000))   # as OSCManager fires it
    r.load(A2, "GripSync")                # the restarted client joins on A'
    time.sleep(QUIET)
    assert r.restores("GripSync") == [], "a restart read as a swap"

    r.set("GripSync", "Word0", 2.0)
    r.load(A, "GripSync")
    assert r.completed("GripSync"), f"no restore after the rejoin: {r.restores('GripSync')}"
    assert r.placed_from("GripSync") == {"Word0": 2.0}


def test_the_avatars_0_reaches_the_log_after_every_restore(rig):
    """Intended: the 0 is observation only, and it is observed each time. Against a peer that does
    not echo our 1, the previous restore's 0 stays cached and the next 0 would equal it and be
    filtered, so Restore is forgotten before each 1."""
    r = rig()
    r.vrc.echo_inbound = False
    r.m.log = mock.MagicMock(wraps=r.m.log)

    def zeros():
        return sum("Restore is back at 0" in c.args[0] for c in r.m.log.info.call_args_list)

    worn_a_with(r, Word0=1.0)
    for n, avatar in enumerate((A2, A), start=1):
        r.load(avatar, "GripSync")
        assert wait_for(lambda: r.restores("GripSync") == [1] * n)
        r.set("GripSync", "Restore", 0)   # the avatar ending the restore
        assert wait_for(lambda: zeros() == n), f"restore {n}'s 0 was not seen"


# --------------------------------------------------------------------------
# All or nothing
# --------------------------------------------------------------------------

def test_a_dropped_payload_write_withholds_restore(rig, monkeypatch):
    """Intended: all or nothing. If any payload write is dropped the bridge never writes the 1,
    even though it still could, so the avatar's window expires and it boots from defaults rather
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
    time.sleep(QUIET)
    assert r.restores("GripSync") == [], "1 was written after a payload write was dropped"


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
    time.sleep(QUIET)
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
    time.sleep(QUIET)
    assert r.restores("GripSync") == []

    r.set("GripSync", "Word0", 2.0)
    r.load(A, "GripSync")
    assert r.completed("GripSync")
    assert r.placed_from("GripSync") == {"Word0": 2.0}


def test_a_same_id_reload_is_a_swap_only_under_the_test_switch(rig):
    """Intended: for a peer that cannot change avatars (the emulator's play, stop, play announces
    the same id), `treat_reload_as_swap` makes that reload restore. Off by default, where the
    same sequence is a reload and forgets."""
    for switch, expect in ((False, []), (True, [1])):
        r = rig(treat_reload_as_swap=switch)
        worn_a_with(r, Word0=1.0)
        time.sleep(REPEAT_GAP)
        r.load(A, "GripSync")
        if expect:
            assert r.completed("GripSync")
        else:
            time.sleep(QUIET)
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
    time.sleep(QUIET)
    assert r.restores("GripSync") == []
    assert r.written("GripSync", "Word0") == []

    r.m.activate()
    r.set("GripSync", "Word0", 2.0)
    r.load(A, "GripSync")
    assert r.completed("GripSync")
    assert r.placed_from("GripSync") == {"Word0": 2.0}
