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
* **A calibration reload restores and holds until the accept;** the client log tells it from
  Reset Avatar and a join, and anything the log does not prove -- no bound switch, a log not
  chosen by service name, desktop -- forgets. A swap never writes 3.
* **`Scope` widens the lifetime to the instance,** only on proof from the log, and is never
  payload.

The client log is a real file the tailer follows (`ClientLogFile`), in the temp directory the
suite's autouse `persist_log_dir` points every tailer at.

Timing: thread-per-datagram dispatch means two datagrams sent back to back can reach the mapping
in either order, so the avatar's boot steps are spaced by `STEP`, as the real ones are by frames.
`ANNOUNCE_SETTLE_SECS` is shortened for speed everywhere but the one test that holds the real
values. A same-id re-announcement is spaced past `REFIRE_FOLD_WINDOW_SECS`, as every real one is
by seconds, or the manager folds it as a twin.
"""
import random
import threading
import time
from unittest import mock

import pytest

import vrbridge.mappings.osc_persist as osc_persist
from vrbridge.engine import VRBridge
from vrbridge.mappings.osc_persist import (AVATAR_CHANGE_ADDR, NAMESPACE_ROOT,
                                           BridgePersistMapping)
from vrbridge.osc_manager import REFIRE_FOLD_WINDOW_SECS
from zeroconf import ServiceInfo

from .fake_vrchat import FakeVRChat

SERVICE = "VRChat-Client-ABC123"
ME = "Local Player"
WORLD = "wrld_00000000-0000-4000-8000-0000000000aa"

A = "avtr_aaaaaaaa-0000-0000-0000-000000000001"
A2 = "avtr_aaaaaaaa-0000-0000-0000-000000000002"   # A', a different avatar with the same prefab
B = "avtr_bbbbbbbb-0000-0000-0000-000000000001"
UNWEARABLE = "avtr_ffffffff-0000-0000-0000-000000000000"

REAL_ANNOUNCE_SETTLE = osc_persist.ANNOUNCE_SETTLE_SECS
REAL_WRITE_SETTLE = osc_persist.WRITE_SETTLE_SECS
REAL_LATE_LIMIT = osc_persist.LATE_LIMIT_SECS
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


class ClientLogFile:
    """The client's log, written as the client writes it: one file per launch, advertising its
    OSCQuery service near the top, in a room from the start."""

    def __init__(self, log_dir, instance="1"):
        self.path = log_dir / "output_log_2026-10-05_12-00-00.txt"
        self.path.write_text("", encoding="utf-8")
        self.write(f"Advertising Service {SERVICE} of type OSCQuery on 9001",
                   f"User Authenticated: {ME} (usr_00000000-0000-4000-8000-000000000001)")
        self.room(instance)

    def write(self, *msgs):
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write("".join(f"2026.10.05 12:00:00 Debug      -  {m}\n" for m in msgs))

    def room(self, instance):
        self.write("[Behaviour] Entering Room: Example World",
                   f"[Behaviour] Joining {WORLD}:{instance}~region(us)",
                   "[Behaviour] Successfully joined room")

    def switch(self, kind, worn, before=None, instance="1"):
        """One local avatar load as the log shows it. `kind`: calibration, reset, join (into
        `instance`), swap (from `before`), or none (no signature at all)."""
        if kind == "join":
            self.write("[Behaviour] OnLeftRoom")
            self.room(instance)
        self.write(f"[Behaviour] Switching {ME} to avatar Some Avatar")
        if kind == "calibration":
            self.write(f"Saving Avatar Data:{worn}")
        elif kind == "swap":
            self.write(f"Saving Avatar Data:{before}")
        self.write("[Behaviour] Initialize Limb Avatar VRCPlayer[Local] 2 True 1",
                   "Measure Human Avatar Avatar isRemeasure:True")   # the placeholder's
        if kind in ("reset", "join", "swap"):
            self.write(f"Loading Avatar Data:{worn}")
        self.write("[Behaviour] Initialize SixPoint Avatar VRCPlayer[Local] 2 True 8")

    def accept(self):
        self.write("Measure Human Avatar Avatar isRemeasure:True")


class Rig:
    """A started bridge pinned at the fake, the mapping registered and active, its tailer
    following `self.client_log`."""

    def __init__(self, vrc: FakeVRChat, *, log_dir, copies=1, activate=True, tree=False, **kw):
        self.vrc = vrc
        # tree=True: a bridge that found the fake as VRChat, so it has an OSCQuery tree to
        # read; pinned, the default, it has none and the stream is all it sees.
        self.bridge = VRBridge(enable_steamvr=False, advertise=False, discover=False,
                               target=None if tree else ("127.0.0.1", vrc.osc_port))
        self.bridge.osc.start()
        self.client_log = ClientLogFile(log_dir)
        if tree:
            name = f"{SERVICE}._oscjson._tcp.local."
            self.bridge.osc._consider_service(name, ServiceInfo(
                "_oscjson._tcp.local.", name, addresses=[bytes([127, 0, 0, 1])],
                port=vrc.http_port, properties={}, server="h.local."))
        self.boots: dict[str, float] = {}
        vrc.out_port = self.bridge.osc.osc_port
        vrc.copies = copies
        vrc.echo_inbound = True
        self.m = BridgePersistMapping(self.bridge, **kw)
        self.m.register()
        if activate:
            self.m.activate()
        # The tailer's replay done: chosen by service name with a tree, else by newest.
        assert wait_for(lambda: self.m._client_log.rule is not None)

    def close(self):
        self.m.close()
        self.bridge.osc.stop()

    # -- the client's side of the wire --

    def change(self, avatar_id, log=None, **switch):
        """The announcement at apply; with `log`, the client log's switch for it, written as the
        change goes out (`ClientLogFile.switch`)."""
        if log is not None:
            self.client_log.switch(log, avatar_id, **switch)
        self.vrc.emit(AVATAR_CHANGE_ADDR, avatar_id)
        time.sleep(STEP)

    def boot(self, *namespaces, announce=5, scope=None):
        """The incoming avatar's boot: every namespace's Announce (and Scope), then its Boot a
        step later."""
        for ns in namespaces:
            self.vrc.emit(addr(ns, "Announce"), announce)
            if scope is not None:
                self.vrc.emit(addr(ns, "Scope"), scope)
        time.sleep(STEP)
        for ns in namespaces:
            self.boots[ns] = random.uniform(0.001, 1.0)
            self.vrc.emit(addr(ns, "Boot"), self.boots[ns])
        time.sleep(STEP)

    def load(self, avatar_id, *namespaces, announce=5, scope=None, log=None, **switch):
        """An announcement at apply followed by the new avatar's boot: a menu swap, a join."""
        self.change(avatar_id, log, **switch)
        self.boot(*namespaces, announce=announce, scope=scope)

    def set(self, ns, leaf, value):
        self.vrc.emit(addr(ns, leaf), value)
        time.sleep(STEP)

    def holds(self, ns, boot=None, **payload):
        """What the worn avatar's tree serves: its Boot (the last one emitted, by default) and
        these payload values, whatever the stream did or did not deliver."""
        self.vrc.set_node(addr(ns, "Boot"), self.boots[ns] if boot is None else boot)
        for leaf, value in payload.items():
            self.vrc.set_node(addr(ns, leaf), value)

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
def rig(persist_log_dir):
    rigs = []

    def make(**kw):
        vrc = FakeVRChat().__enter__()
        r = Rig(vrc, log_dir=persist_log_dir, **kw)
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
    r.vrc.on_receive = lambda a, v: arrived.setdefault((a, v), time.perf_counter())
    worn_a_with(r, Word0=137.0, Detached=True)
    r.load(A2, "GripSync")
    # The fake stamps in on_receive, after it records the message completed() reads.
    assert wait_for(lambda: (addr("GripSync", "Restore"), 1) in arrived)
    last_payload = max(arrived[(addr("GripSync", "Word0"), 137.0)],
                       arrived[(addr("GripSync", "Detached"), True)])
    waited = arrived[(addr("GripSync", "Restore"), 1)] - last_payload
    # A timer never fires early; the margin is for the payload's own arrival lag on loopback.
    assert waited >= REAL_WRITE_SETTLE * 0.8, f"1 came {waited * 1e3:.1f} ms after the payload"


def test_restore_1_is_not_written_before_both_waits_on_the_real_constants(rig, monkeypatch):
    """Intended: the contract's timing, on the shipped values. The bridge writes the 1 no sooner
    than ANNOUNCE_SETTLE_SECS + WRITE_SETTLE_SECS after Boot, and a 1 written sooner would mean
    the decision did not wait for a late Announce."""
    monkeypatch.setattr(osc_persist, "ANNOUNCE_SETTLE_SECS", REAL_ANNOUNCE_SETTLE)
    r = rig()
    arrived = {}
    r.vrc.on_receive = lambda a, v: arrived.setdefault((a, v), time.perf_counter())
    worn_a_with(r, Word0=1.0)
    r.change(A2)
    r.set("GripSync", "Announce", 5)
    booted = time.perf_counter()            # before the send, so any lag only adds
    r.vrc.emit(addr("GripSync", "Boot"), 0.5)
    # The fake stamps in on_receive, after it records the message completed() reads.
    assert wait_for(lambda: (addr("GripSync", "Restore"), 1) in arrived)
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
        # The payload leaves before the write-settle timer is armed, under the same lock hold.
        assert wait_for(lambda: r.m._ns["GripSync"].timer is not None)
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


@pytest.mark.parametrize("limit, restored", [(FAST_ANNOUNCE_SETTLE / 2, False),
                                             (REAL_LATE_LIMIT, True)], ids=["late", "on-time"])
def test_a_decision_past_the_late_limit_writes_nothing(rig, monkeypatch, limit, restored):
    """Intended: payload landing after the avatar's window closed lands on a running prop, so a
    decision running more than LATE_LIMIT_SECS after its Boot arrived -- a stalled bridge --
    writes no payload and no 1, and one inside the limit is unaffected. The stall is staged by
    shrinking the limit below the settle wait, which the decision always runs after."""
    monkeypatch.setattr(osc_persist, "LATE_LIMIT_SECS", limit)
    r = rig()
    worn_a_with(r, Word0=1.0)
    r.load(A2, "GripSync")
    if restored:
        assert r.completed("GripSync")
        assert r.placed_from("GripSync") == {"Word0": 1.0}
    else:
        time.sleep(QUIET)
        assert r.restores("GripSync") == []
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


def test_a_payload_listener_overtaken_while_waiting_for_the_lock_keeps_the_newer_value(rig):
    """Intended: a listener for the older value can wait on the mapping's lock while the newer
    datagram lands in the manager's cache. What it stores once it runs must be what the cache
    holds then, or the snapshot places the prop from a pose the avatar has left. Staged by holding
    the mapping's lock, parking the older value's listener on it, landing the newer value in the
    cache, and only then letting the listener run."""
    r = rig()
    worn_a_with(r, Word0=1.0)
    time.sleep(QUIET)
    word = addr("GripSync", "Word0")
    osc = r.bridge.osc
    real_lock = r.m._lock
    parked = threading.Event()

    class ParkingLock:
        """The mapping's lock, flagging the listener thread as it starts to wait on it."""
        def __enter__(self):
            if threading.current_thread() is listener:
                parked.set()
            return real_lock.__enter__()

        def __exit__(self, *exc):
            return real_lock.__exit__(*exc)

    listener = threading.Thread(target=r.bridge._on_osc_event, args=(word, 1.5))
    r.m._lock = ParkingLock()
    with real_lock:
        with osc._cache_lock:
            osc._cache[word] = 1.5            # the older datagram's cache write
        listener.start()
        assert parked.wait(3.0), "the listener never reached the mapping's lock"
        with osc._cache_lock:
            osc._cache[word] = 2.0            # the newer datagram's, while the older one waits
    listener.join(3.0)
    r.m._lock = real_lock
    assert r.m._ns["GripSync"].live[word] == osc.get_cached(word) == 2.0
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


@pytest.mark.parametrize("router_name", ["default", "camera"])
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


# --------------------------------------------------------------------------
# Reconciling against the client's tree
# --------------------------------------------------------------------------

@pytest.fixture
def fast_reconcile(monkeypatch):
    monkeypatch.setattr(osc_persist, "RECONCILE_SECS", 0.05)


# Long enough for several reconcile reads at the shortened period.
RECONCILED = 0.4


def test_a_value_the_stream_never_delivered_is_restored_from_the_tree(rig, fast_reconcile):
    """Intended: the client sends a value only when it changes, and a change that never reaches
    the bridge is never re-sent while it stands. Measured live, a placed box's coarse word went
    that way and the next swap put the box at cell 0, out of reach. The tree holds what the stream
    lost, so the snapshot has to come out whole, each value with its wire type."""
    r = rig(tree=True)
    r.vrc.whole_floats_as_ints = True        # 98.0 reads back as a float only through its tag
    worn_a_with(r, Word0=2.0)
    r.holds("GripSync", Word0=2.0, Coarse=98.0, Detached=True)   # Coarse, Detached never sent
    time.sleep(RECONCILED)
    r.load(A2, "GripSync")
    assert r.completed("GripSync")
    placed = r.placed_from("GripSync")
    assert placed == {"Word0": 2.0, "Coarse": 98.0, "Detached": True}
    assert type(placed["Coarse"]) is float and type(placed["Detached"]) is bool


def test_a_restored_value_whose_echo_never_came_back_survives_a_second_swap(rig, fast_reconcile):
    """Intended: after a restore the bridge learned the restored values only from the client's
    echo. Measured live, an echo did not come back; the box re-placed in the same cell held that
    value unchanged, so nothing re-sent it, and the second swap lost the box. The second snapshot
    has to carry it."""
    r = rig(tree=True)
    worn_a_with(r, Word0=137.0)
    r.vrc.echo_inbound = False
    r.load(A2, "GripSync")
    assert r.completed("GripSync")
    r.holds("GripSync", Word0=137.0)          # A' placed from the restore; no echo arrived
    time.sleep(RECONCILED)
    r.vrc.messages.clear()
    r.load(A, "GripSync")
    assert r.completed("GripSync")
    assert r.placed_from("GripSync") == {"Word0": 137.0}


def test_a_tree_from_another_boot_is_never_folded_in(rig, fast_reconcile):
    """Intended: the tree switches avatars at apply, before the announcement reaches the bridge,
    so a read can return the incoming avatar's reset while the bridge still keeps the outgoing
    one's state. Folded in, the reset would become the snapshot and every box would go home."""
    r = rig(tree=True)
    worn_a_with(r, Word0=137.0, Detached=True)
    r.holds("GripSync", boot=0.5, Word0=0.0, Detached=False)    # someone else's boot
    time.sleep(RECONCILED)
    r.load(A2, "GripSync")
    assert r.completed("GripSync")
    assert r.placed_from("GripSync") == {"Word0": 137.0, "Detached": True}


def test_a_reconciled_value_keeps_the_change_filter_honest(rig, fast_reconcile):
    """Intended: the stream delivered 1.0, lost the change to 2.0, and the tree supplied it. When
    the value then moves back to 1.0 the client sends 1.0 -- equal to what the change filter last
    saw from the stream -- and that send has to reach the snapshot, or the prop restores to a place
    it already left."""
    r = rig(tree=True)
    worn_a_with(r, Word0=1.0)
    r.holds("GripSync", Word0=2.0)
    assert wait_for(lambda: r.m._ns["GripSync"].live.get(addr("GripSync", "Word0")) == 2.0)
    r.vrc.clear_node(addr("GripSync", "Boot"))     # stop reading, so only the stream speaks
    time.sleep(RECONCILED)                         # and let a read already out land first
    r.set("GripSync", "Word0", 1.0)
    r.load(A2, "GripSync")
    assert r.completed("GripSync")
    assert r.placed_from("GripSync") == {"Word0": 1.0}


def test_a_read_out_across_an_avatar_change_is_never_folded_in(rig, fast_reconcile):
    """Intended: a read that left before the announcement and lands after it describes a tree that
    may already be the incoming avatar's, even where its Boot still matches. Folded in, it would
    seed the incoming avatar's values with it, and the next swap would restore them. The value is
    one the stream never delivered: nothing in the manager's cache can stand guard over it."""
    r = rig(tree=True)
    worn_a_with(r, Word0=137.0)
    r.vrc.echo_inbound = False
    r.holds("GripSync", Coarse=5.0)              # what the parked read will answer
    with r.vrc.hold_next_node_get() as gate:
        assert gate.wait_until_parked()
        r.change(A2)
        r.vrc.clear_node(addr("GripSync", "Boot"))   # the tree is no longer A's; the parked
    time.sleep(RECONCILED)                           # read answered before that
    ns = r.m._ns["GripSync"]
    assert addr("GripSync", "Coarse") not in ns.since_announce


def test_a_read_never_overwrites_a_value_the_stream_delivered_while_it_was_out(rig, fast_reconcile):
    """Intended: the stream's value is newer than any read that was out when it arrived. A read
    answering 2.0 that lands after the stream delivered 3.0 must leave 3.0, or a swap before the
    next read restores the prop to where it was a moment ago."""
    r = rig(tree=True)
    worn_a_with(r, Word0=1.0)
    r.holds("GripSync", Word0=2.0)
    with r.vrc.hold_next_node_get() as gate:
        assert gate.wait_until_parked()
        r.vrc.clear_node(addr("GripSync", "Boot"))   # every later read is refused
        r.set("GripSync", "Word0", 3.0)
    time.sleep(RECONCILED)
    assert r.m._ns["GripSync"].live[addr("GripSync", "Word0")] == 3.0


def test_a_pinned_bridge_reads_no_tree(rig, fast_reconcile):
    """Intended: a pinned target serves no tree, so the stream is all there is and nothing is
    asked of the peer. The snapshot is exactly what arrived."""
    r = rig()
    worn_a_with(r, Word0=2.0)
    r.vrc.set_node(addr("GripSync", "Boot"), r.boots["GripSync"])
    r.vrc.set_node(addr("GripSync", "Coarse"), 98.0)
    time.sleep(RECONCILED)
    assert r.vrc.node_gets == []
    r.load(A2, "GripSync")
    assert r.completed("GripSync")
    assert r.placed_from("GripSync") == {"Word0": 2.0}


# --------------------------------------------------------------------------
# Calibration reloads
# --------------------------------------------------------------------------

VRMODE = osc_persist.VRMODE_ADDR
# Long enough for a decision that waited out the log deadline.
DECIDED = osc_persist.LOG_DEADLINE_SECS + QUIET


def watch_log(r):
    r.m.log = mock.MagicMock(wraps=r.m.log)


def said(r, text, level="info"):
    return any(text in (c.args[0] % c.args[1:])
               for c in getattr(r.m.log, level).call_args_list)


def decided(r, text, level="info"):
    """The mapping logged `text` since the setup ended (`worn_in_vr` clears what it logged): what
    a forget is asserted on, so a test cannot pass on a decision that has not happened yet, nor
    on the setup's own."""
    return wait_for(lambda: said(r, text, level), timeout=DECIDED + 1.0)


def worn_in_vr(r, ns="GripSync", vr=1, scope=None, **payload):
    """A worn after a join the log shows, with the headset's VRMode over the wire (never primed,
    so a VRMode the mapping does not watch is never seen). Spaced so a reload's announcement of
    A is not folded into the join's, and past the bind window."""
    r.load(A, ns, scope=scope, log="join")
    for leaf, value in payload.items():
        r.set(ns, leaf, value)
    r.vrc.emit(VRMODE, vr)
    # Past the bind window too, so the join's own switch is never the next change's.
    time.sleep(max(REPEAT_GAP, osc_persist.LOG_BIND_SECS + 0.3))
    if isinstance(r.m.log, mock.MagicMock):
        r.m.log.reset_mock()


def test_a_calibration_reload_restores_and_holds_until_the_accept(rig):
    """Intended: in VR, with the log chosen by service name, a reload whose switch saved the worn
    avatar's data before the placeholder is a calibration's, and the avatar is held until the
    user accepts. The snapshot goes out ahead of 1, then 3 holds the avatar, and 2 releases it
    only at the accept: the first remeasure after the avatar settled, not the placeholder's,
    which the switch also writes."""
    r = rig(tree=True)
    worn_in_vr(r, Word0=137.0, Detached=True)
    r.load(A, "GripSync", log="calibration")
    assert wait_for(lambda: r.restores("GripSync") == [1, 3]), r.restores("GripSync")
    assert r.placed_from("GripSync") == {"Word0": 137.0, "Detached": True}
    time.sleep(QUIET)
    assert r.restores("GripSync") == [1, 3], "released before any accept"
    r.client_log.accept()
    assert wait_for(lambda: r.restores("GripSync") == [1, 3, 2]), r.restores("GripSync")


@pytest.mark.parametrize("kind, why", [
    ("reset", "the client log shows Reset Avatar"),
    ("join", "the client log shows a world join"),
    ("none", "no save of the worn avatar's data"),
])
def test_a_reload_the_log_does_not_show_as_a_calibration_forgets(rig, kind, why):
    """Intended: Reset Avatar (Loading of the worn id), a join (a room transition) and a switch
    with no signature are all the worn avatar reloaded, and the lifetime rule forgets each:
    Reset Avatar stays the user's escape from a bad persisted state."""
    r = rig(tree=True)
    watch_log(r)
    worn_in_vr(r, Word0=137.0)
    r.load(A, "GripSync", log=kind)
    assert decided(r, why)
    time.sleep(QUIET)
    assert r.restores("GripSync") == []
    assert r.written("GripSync", "Word0") == []


def test_a_join_forgets_even_with_a_save_in_its_switch(rig):
    """Intended: the room transition decides before any save does; a join's switch that also
    saved the worn avatar's data is still a join."""
    r = rig(tree=True)
    watch_log(r)
    worn_in_vr(r, Word0=137.0)
    r.client_log.write("[Behaviour] OnLeftRoom")
    r.client_log.room("1")
    r.load(A, "GripSync", log="calibration")
    assert decided(r, "the client log shows a world join")
    assert r.restores("GripSync") == []


def test_a_room_event_inside_the_switch_makes_it_a_join(rig):
    """Intended: a room transition landing while the switch is current marks that switch, not
    only the next one."""
    r = rig(tree=True)
    watch_log(r)
    worn_in_vr(r, Word0=137.0)
    r.client_log.write(f"[Behaviour] Switching {ME} to avatar Some Avatar",
                       f"Saving Avatar Data:{A}", "[Behaviour] OnLeftRoom",
                       "[Behaviour] Initialize Limb Avatar VRCPlayer[Local] 2 True 1",
                       "[Behaviour] Initialize SixPoint Avatar VRCPlayer[Local] 2 True 8")
    r.load(A, "GripSync")
    assert decided(r, "the client log shows a world join")
    assert r.restores("GripSync") == []


def test_a_remote_switch_inside_the_local_one_is_ignored(rig):
    """Intended: every player's switch is logged; a remote one landing inside ours neither
    replaces nor settles it, so the calibration still restores."""
    r = rig(tree=True)
    worn_in_vr(r, Word0=137.0)
    r.client_log.write(f"[Behaviour] Switching {ME} to avatar Some Avatar",
                       f"Saving Avatar Data:{A}",
                       "[Behaviour] Switching Someone Else to avatar Other Avatar",
                       "[Behaviour] Initialize Limb Avatar VRCPlayer[Local] 2 True 1",
                       "[Behaviour] Initialize Limb Avatar VRCPlayer[Remote] 3 False 1",
                       "[Behaviour] Initialize SixPoint Avatar VRCPlayer[Local] 2 True 8")
    r.load(A, "GripSync")
    assert wait_for(lambda: r.restores("GripSync") == [1, 3]), r.restores("GripSync")


def test_a_spontaneous_save_after_the_placeholder_is_not_a_calibration(rig):
    """Intended: the client also saves the worn avatar's data on its own, seconds after a load;
    only a save before the placeholder initialises is the switch's."""
    r = rig(tree=True)
    watch_log(r)
    worn_in_vr(r, Word0=137.0)
    r.client_log.write(f"[Behaviour] Switching {ME} to avatar Some Avatar",
                       "[Behaviour] Initialize Limb Avatar VRCPlayer[Local] 2 True 1",
                       f"Saving Avatar Data:{A}",
                       "[Behaviour] Initialize SixPoint Avatar VRCPlayer[Local] 2 True 8")
    r.load(A, "GripSync")
    assert decided(r, "no save of the worn avatar's data")
    assert r.restores("GripSync") == []


def test_a_reload_with_no_switch_in_the_log_forgets_by_the_deadline_and_never_late(rig):
    """Intended: the decision waits on the log only until LOG_DEADLINE_SECS after Boot, then
    forgets inside the late limit, warns once that a VR reload showed no switch, and a switch
    that settles afterwards writes nothing; it only logs how late it was."""
    r = rig(tree=True)
    watch_log(r)
    worn_in_vr(r, Word0=137.0)
    booted = time.monotonic()
    r.load(A, "GripSync")
    assert decided(r, "the client log showed no switch for this change")
    assert time.monotonic() - booted < osc_persist.LATE_LIMIT_SECS + 2 * STEP + 0.1
    assert said(r, "a VR reload, and no switch settled", "warning")
    r.client_log.switch("calibration", A)
    assert decided(r, "after the decision went without it")
    time.sleep(QUIET)
    assert r.restores("GripSync") == []
    assert r.written("GripSync", "Word0") == []


@pytest.mark.parametrize("side", ["before", "after"])
def test_a_switch_settled_outside_the_bind_window_is_not_this_changes(rig, monkeypatch, side):
    """Intended: a switch is this change's only when its avatar settled within LOG_BIND_SECS of
    the change, either side; one from an earlier load, or one landing well after, proves
    nothing about this reload, and it forgets."""
    monkeypatch.setattr(osc_persist, "LOG_BIND_SECS", 0.1)
    r = rig(tree=True)
    watch_log(r)
    worn_in_vr(r, Word0=137.0)
    if side == "before":
        r.client_log.switch("calibration", A)
        time.sleep(0.3)
        r.load(A, "GripSync")
    else:
        r.change(A)
        time.sleep(0.15)
        r.client_log.switch("calibration", A)
        r.boot("GripSync")
    assert decided(r, "the client log showed no switch for this change")
    time.sleep(QUIET)
    assert r.restores("GripSync") == []


def test_a_pinned_bridge_forgets_a_calibration(rig):
    """Intended: a pinned bridge has no service name, so its tailer follows the newest log,
    which may be another client's; nothing it shows may make a reload restore."""
    r = rig()
    watch_log(r)
    worn_in_vr(r, Word0=137.0)
    r.load(A, "GripSync", log="calibration")
    assert decided(r, "chosen by newest")
    time.sleep(QUIET)
    assert r.restores("GripSync") == []


def test_a_desktop_reload_forgets(rig):
    """Intended: desktop has no calibration, so a reload in VRMode 0 forgets even when its
    switch has a calibration's signature."""
    r = rig(tree=True)
    watch_log(r)
    worn_in_vr(r, vr=0, Word0=137.0)
    r.load(A, "GripSync", log="calibration")
    assert decided(r, "VRMode is 0, not 1")
    assert r.restores("GripSync") == []


def test_a_vr_reload_whose_switch_names_nothing_warns_once_per_replay(rig):
    """Intended: a bound switch that neither saved nor loaded the worn id is how a reworded
    client line shows, so it warns -- once per log replay, not once per reload."""
    r = rig(tree=True)
    watch_log(r)
    worn_in_vr(r, Word0=137.0)
    for _ in range(2):
        r.load(A, "GripSync", log="none")
        assert decided(r, "no save of the worn avatar's data")
        time.sleep(REPEAT_GAP)
    warned = [c for c in r.m.log.warning.call_args_list if "neither saved nor loaded" in c.args[0]
              % c.args[1:]]
    assert len(warned) == 1


def test_an_accept_inside_the_write_settle_keeps_1_3_2_in_separate_frames(rig, monkeypatch):
    """Intended: the client applies the latest value per parameter per frame, so 1, 3 and 2 must
    land in separate frames. An accept arriving before the 1 is out still waits for the 3, and
    the 2 follows the 3 by the write settle."""
    monkeypatch.setattr(osc_persist, "WRITE_SETTLE_SECS", 0.3)
    r = rig(tree=True)
    arrived = {}
    restore = addr("GripSync", "Restore")
    r.vrc.on_receive = lambda a, v: arrived.setdefault((a, v), time.perf_counter())
    worn_in_vr(r, Word0=137.0)
    r.load(A, "GripSync", log="calibration")
    assert wait_for(lambda: r.written("GripSync", "Word0") == [137.0])
    r.client_log.accept()                     # inside the 0.3 s before the 1
    assert wait_for(lambda: (restore, 2) in arrived), r.restores("GripSync")
    assert r.restores("GripSync") == [1, 3, 2]
    # A timer never fires early; the margin is for loopback arrival lag.
    assert arrived[(restore, 3)] - arrived[(restore, 1)] >= osc_persist.MARK_FLOOR_SECS * 0.8
    assert arrived[(restore, 2)] - arrived[(restore, 3)] >= 0.3 * 0.8


def test_an_avatar_change_before_the_accept_abandons_the_release(rig):
    """Intended: the held animator is gone once another change arrives, so the accept after it
    releases nothing."""
    r = rig(tree=True)
    worn_in_vr(r, Word0=137.0)
    r.load(A, "GripSync", log="calibration")
    assert wait_for(lambda: r.restores("GripSync") == [1, 3])
    r.change(B)
    r.client_log.accept()
    time.sleep(QUIET)
    assert r.restores("GripSync") == [1, 3]


def test_a_reload_during_a_pending_release_restores_what_the_tree_holds(rig, fast_reconcile):
    """Intended: the release wait can last a minute, and the reconcile has to keep reading
    through it. With no echo of the restore, the tree is the only source of the restored
    values, so a second reload inside the hold -- one calibration can reload twice -- restores
    them rather than an empty snapshot."""
    r = rig(tree=True)
    worn_in_vr(r, Word0=137.0)
    r.vrc.echo_inbound = False
    r.load(A, "GripSync", log="calibration")
    assert wait_for(lambda: r.restores("GripSync") == [1, 3])
    r.holds("GripSync", Word0=137.0)          # placed from the restore; no echo arrived
    time.sleep(RECONCILED)
    r.vrc.messages.clear()
    r.load(A, "GripSync", log="calibration")
    assert wait_for(lambda: r.restores("GripSync") == [1, 3]), r.restores("GripSync")
    assert r.placed_from("GripSync") == {"Word0": 137.0}
    r.client_log.accept()
    assert wait_for(lambda: r.restores("GripSync") == [1, 3, 2])
    time.sleep(QUIET)
    assert r.restores("GripSync") == [1, 3, 2]


def test_a_log_replay_during_a_hold_says_the_release_is_lost(rig):
    """Intended: a replay forgets the switch the accept would be matched against, so the
    release falls to the give-up; that must be said, not left silent."""
    r = rig(tree=True)
    watch_log(r)
    worn_in_vr(r, Word0=137.0)
    r.load(A, "GripSync", log="calibration")
    assert wait_for(lambda: r.restores("GripSync") == [1, 3])
    r.m._tailer.retarget(SERVICE)
    assert decided(r, "re-read during a calibration hold", "warning")


def test_a_release_with_no_accept_gives_up_at_the_limit(rig, monkeypatch):
    """Intended: the bridge stops waiting below the avatar's own timeout, says so, and a late
    accept after that writes nothing."""
    monkeypatch.setattr(osc_persist, "RELEASE_LIMIT_SECS", 0.2)
    r = rig(tree=True)
    watch_log(r)
    worn_in_vr(r, Word0=137.0)
    r.load(A, "GripSync", log="calibration")
    assert wait_for(lambda: r.restores("GripSync") == [1, 3])
    time.sleep(0.2 + QUIET)
    r.client_log.accept()
    time.sleep(QUIET)
    assert r.restores("GripSync") == [1, 3]
    assert said(r, "no accept within", "warning")


def test_a_swap_never_writes_3(rig):
    """Intended: a swap's avatar is not held, so it keeps 1 alone and its timing, even in VR
    with the log bound."""
    r = rig(tree=True)
    worn_in_vr(r, Word0=137.0)
    r.load(A2, "GripSync", log="swap", before=A)
    assert r.completed("GripSync")
    time.sleep(osc_persist.MARK_FLOOR_SECS + QUIET)
    r.client_log.accept()
    time.sleep(QUIET)
    assert r.restores("GripSync") == [1]


# --------------------------------------------------------------------------
# Scope
# --------------------------------------------------------------------------

@pytest.mark.parametrize("scope, restored", [(None, False), (1, True), (2, True)])
def test_a_swap_chain_inside_one_instance_restores_from_scope_1(rig, scope, restored):
    """Intended: A -> B -> A' forgets at the default scope, and from Scope 1 restores A's state,
    B carrying no namespace, because the instance is the lifetime there."""
    r = rig(tree=True)
    watch_log(r)
    worn_in_vr(r, scope=scope, Word0=137.0)
    r.change(B, log="swap", before=A)
    r.load(A2, "GripSync", scope=scope, log="swap", before=B)
    if restored:
        assert r.completed("GripSync"), r.restores("GripSync")
        assert r.placed_from("GripSync") == {"Word0": 137.0}
    else:
        assert decided(r, "which is not a single swap")
        assert r.restores("GripSync") == []


@pytest.mark.parametrize("vr", [0, 1], ids=["desktop", "vr"])
@pytest.mark.parametrize("scope, restored", [(None, False), (1, False), (2, True)])
def test_reset_avatar_restores_only_at_scope_2(rig, vr, scope, restored):
    """Intended: Scope 2 keeps the state across Reset Avatar, in VR and on desktop; Scope 1 and
    the default clear it, so Reset Avatar stays the escape unless the avatar gave it up. The
    restore after a Reset is a 1 alone: nothing is held."""
    r = rig(tree=True)
    watch_log(r)
    worn_in_vr(r, vr=vr, scope=scope, Word0=137.0)
    r.load(A, "GripSync", scope=scope, log="reset")
    if restored:
        assert r.completed("GripSync"), r.restores("GripSync")
        assert r.placed_from("GripSync") == {"Word0": 137.0}
        assert decided(r, "Scope 2: instance, keeps Reset")
        time.sleep(osc_persist.MARK_FLOOR_SECS + QUIET)
        assert r.restores("GripSync") == [1]
    else:
        assert decided(r, "nothing to restore: a reload of the worn avatar")
        time.sleep(QUIET)
        assert r.restores("GripSync") == []


@pytest.mark.parametrize("scope", [1, 2])
@pytest.mark.parametrize("instance, restored", [("1", True), ("2", False)],
                         ids=["same-instance", "new-instance"])
def test_scope_1_and_2_keep_the_instance_and_clear_on_a_new_one(rig, scope, instance, restored):
    """Intended: a rejoin of the same instance restores at Scope 1 and 2; a new instance, of the
    same world, clears."""
    r = rig(tree=True)
    watch_log(r)
    worn_in_vr(r, scope=scope, Word0=137.0)
    r.load(A, "GripSync", scope=scope, log="join", instance=instance)
    if restored:
        assert r.completed("GripSync"), r.restores("GripSync")
        assert r.placed_from("GripSync") == {"Word0": 137.0}
    else:
        assert decided(r, "falling back to Scope 0's rule, since the room")
        assert r.restores("GripSync") == []


def test_a_calibration_under_scope_1_still_holds_until_the_accept(rig):
    """Intended: Scope widens the lifetime and changes nothing about the hold."""
    r = rig(tree=True)
    worn_in_vr(r, scope=1, Word0=137.0)
    r.load(A, "GripSync", scope=1, log="calibration")
    assert wait_for(lambda: r.restores("GripSync") == [1, 3]), r.restores("GripSync")
    time.sleep(QUIET)
    assert r.restores("GripSync") == [1, 3]
    r.client_log.accept()
    assert wait_for(lambda: r.restores("GripSync") == [1, 3, 2])


def test_a_stale_scope_never_opts_in_an_avatar_that_sent_none(rig):
    """Intended: a Scope-0 avatar never sends Scope (0 onto a declared 0), so the value is only
    the one that arrived since this change. Scope-2 A, then B with the same prefab at Scope 0,
    then Reset Avatar on B: it clears, as Scope 0 does."""
    r = rig(tree=True)
    watch_log(r)
    worn_in_vr(r, scope=2, Word0=137.0)
    r.load(B, "GripSync", log="swap", before=A)
    assert r.completed("GripSync")
    time.sleep(REPEAT_GAP)
    r.vrc.messages.clear()
    r.m.log.reset_mock()
    r.load(B, "GripSync", log="reset")
    assert decided(r, "(Scope 0: swap); nothing to restore")
    assert r.restores("GripSync") == []


@pytest.mark.parametrize("streamed", [True, False], ids=["streamed", "tree-only"])
def test_scope_is_never_payload(rig, fast_reconcile, streamed):
    """Intended: Scope is reserved: never stored as payload from the stream, never folded from
    the tree when the stream missed it, never written back."""
    r = rig(tree=True)
    scope = 2 if streamed else None
    worn_in_vr(r, scope=scope, Word0=137.0)
    r.holds("GripSync", Word0=137.0, Scope=2)
    time.sleep(RECONCILED)
    r.load(A2, "GripSync", scope=scope, log="swap", before=A)
    assert r.completed("GripSync")
    assert r.placed_from("GripSync") == {"Word0": 137.0}
    assert r.written("GripSync", "Scope") == []


@pytest.mark.parametrize("how", ["pinned", "unbound"])
def test_scope_without_proof_from_the_log_falls_back_to_scope_0(rig, how):
    """Intended: Scope 1 and 2 stand on the log: chosen by service name and this change's
    switch bound. Without either, the decision runs Scope 0's rule, which forgets a reload, and
    says so."""
    r = rig(tree=how == "unbound")
    watch_log(r)
    worn_in_vr(r, scope=2, Word0=137.0)
    r.load(A, "GripSync", scope=2, log="reset" if how == "pinned" else None)
    assert decided(r, "falling back to Scope 0's rule")
    assert r.restores("GripSync") == []


def test_a_pinned_bridge_still_restores_one_swap_at_scope_2(rig):
    """Intended: falling back to Scope 0 is Scope 0's whole rule, so a single swap still
    restores without the log."""
    r = rig()
    watch_log(r)
    worn_in_vr(r, scope=2, Word0=137.0)
    r.load(A2, "GripSync", scope=2)
    assert r.completed("GripSync")
    assert said(r, "one swap; falling back to Scope 0's rule")


@pytest.mark.parametrize("value", [3, -1, True, 2.0, 0.0],
                         ids=["3", "-1", "bool", "float", "zero-float"])
def test_a_scope_that_is_not_0_1_or_2_as_an_int_is_0(rig, value):
    """Intended: only a true int 0, 1 or 2 is a scope; bool True is not 1, and a float is a
    mis-authored parameter. Anything else is 0, logged, and Reset Avatar clears."""
    r = rig(tree=True)
    watch_log(r)
    worn_in_vr(r, scope=value, Word0=137.0)
    r.load(A, "GripSync", scope=value, log="reset")
    assert decided(r, "(Scope 0: swap); nothing to restore")
    assert said(r, "counts as 0")
    assert r.restores("GripSync") == []


def test_a_trip_to_another_instance_on_an_avatar_without_the_namespace_still_clears(rig):
    """Intended: an instance change clears Scope 1 and 2 even when it happens on an avatar that
    carries no namespace, so nothing of this namespace boots there. A (Scope 2) in instance 1,
    B, a trip to instance 2 and back to instance 1 on B, then A again: it forgets."""
    r = rig(tree=True)
    watch_log(r)
    worn_in_vr(r, scope=2, Word0=137.0)
    r.change(B, log="swap", before=A)
    time.sleep(REPEAT_GAP)
    r.change(B, log="join", instance="2")
    time.sleep(REPEAT_GAP)
    r.change(B, log="join", instance="1")
    time.sleep(REPEAT_GAP)
    r.load(A2, "GripSync", scope=2, log="swap", before=B)
    assert decided(r, "falling back to Scope 0's rule, since the room is unknown")
    time.sleep(QUIET)
    assert r.restores("GripSync") == []


@pytest.mark.parametrize("scope, instance, restored", [
    (None, "1", False), (None, "2", False), (2, "1", True), (2, "2", False),
], ids=["scope0-same-instance", "scope0-new-instance", "scope2-same-instance",
        "scope2-new-instance"])
def test_a_join_that_changes_the_avatar_is_a_join_not_a_swap(rig, scope, instance, restored):
    """Intended: a join can load a different avatar with the same prefab (one id announced), and
    the log shows the room transition. At Scope 0 any join clears, so it forgets; at Scope 2 a
    rejoin of the same instance restores and a new instance forgets."""
    r = rig(tree=True)
    watch_log(r)
    worn_in_vr(r, scope=scope, Word0=137.0)
    r.load(A2, "GripSync", scope=scope, log="join", instance=instance)
    if restored:
        assert r.completed("GripSync"), r.restores("GripSync")
        assert r.placed_from("GripSync") == {"Word0": 137.0}
    else:
        assert decided(r, "a swap made by a world join")
        time.sleep(QUIET)
        assert r.restores("GripSync") == []
