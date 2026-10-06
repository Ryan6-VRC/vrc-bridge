"""Bridge persistence: carry an avatar-published state across avatar swaps and calibrations.

An avatar publishes state under `/avatar/parameters/BridgePersist/<Name>/`, one namespace per
composition, and this mapping writes it back into the next avatar when that avatar carries the
same namespace with the same identity. It holds no manifest and enumerates nothing: the
namespace is the whole contract, and only names that arrive on the wire are ever known.

Five direct children of a namespace are reserved (`RESERVED`); every other address under it, at
any depth, is **payload**:

* `Id` -- the per-prefab identity, carried as the declared default and so never on the wire. 0
  means persistence is off. Ignored here if it ever arrives.
* `Announce` (int, avatar -> bridge) -- a driver copy of `Id`, written in the same state as `Boot`,
  so it can reach us before or after it.
* `Boot` (float, avatar -> bridge) -- a driver `random` in (0, 1], once per animator load. Anything
  else, the emulator's re-send of the declared 0.0 included, is not a boot and is ignored.
* `Restore` (int) -- the bridge writes 1 once every payload value of the snapshot is written, and
  on a calibration restore then 3 and, at the accept, 2; the avatar writes it back to 0. That 0
  is logged and nothing waits for it.
* `Scope` (int, avatar -> bridge) -- a driver `set` in the `Boot` state: how long the state lives
  (below). Absent, 0.

**Scope.** 0 (`swap`, the default): one swap to the same prefab restores, A -> B -> A' forgets,
a calibration reload restores, and Reset Avatar and any join forget. 1 (`instance`): any chain of
swaps and calibrations inside one instance restores; Reset Avatar and an instance change, a new
instance of the same world included, forget. 2 (`instance-keep-reset`): as 1, and Reset Avatar
restores too, so leaving the instance is the namespace's only escape from a bad state. A newly
selected send target forgets at every scope. The value is delivered fresh per load: every
`/avatar/change` drops the stored one and forgets its cache entry, so only a value arriving since
that change counts. A Scope-0 avatar never sends it (a 0 set onto a declared 0 is no change on
the wire), and a stale 2 would otherwise opt it in. Anything but a true int 0, 1 or 2 is 0,
logged. Every decision logs the scope it applied, so an avatar running on a bridge that predates
`Scope` -- where `Scope` is payload and the lifetime is 0's -- shows in a newer bridge's log.

**The exchange.** Nothing is acknowledged. At `Boot` the bookkeeping below happens at once; the
validity decision happens `ANNOUNCE_SETTLE_SECS` later, against the state captured at the `Boot`,
which is what lets an `Announce` arriving after its `Boot` count. A decision that needs the client
log and has not yet seen this change's switch in it re-arms every `LOG_POLL_SECS` until
`LOG_DEADLINE_SECS` after the `Boot`, then decides without it. A valid snapshot is written, each
value with the wire type it arrived with, then `WRITE_SETTLE_SECS` later `Restore` 1 (the client
applies the latest value per parameter per frame, so the wait puts the 1 in a later frame than the
payload), and, but for a calibration restore's release (below), the mapping is finished with
it. The avatar waits for the 1 in a window of its own and boots from defaults without it, so a
payload write the bridge fails to send withholds the 1, and nothing is retried. A write lost after
it leaves is invisible here: UDP reports no receive. A decision running more than
`LATE_LIMIT_SECS` after its `Boot` arrived (a stalled bridge) writes nothing: payload landing
after the window closes lands on a running prop, which a watching remote sees thrown out of
place. A `/avatar/change`, a further `Boot` for the namespace, or a newly selected target during
either wait abandons the exchange; a per-exchange token makes the timer that lost the race a
no-op. Payload the snapshot does not name stays at the default the avatar reset it to, which is
what makes a snapshot complete without holding unchanged values.

**What is kept per namespace.** The live payload values, each as the Python value python-osc
parsed, so it replays with its wire type (an int sent to a declared float writes garbage, and a
bool must stay a bool); the last `Announce` and whether one arrived since the last
`/avatar/change`; the `Scope` that arrived since then; a checkpoint; the avatar ids announced
since the namespace last booted; and the client's room at its last decision.

* Every `/avatar/change`, echo or not, re-takes the checkpoint from the live values and the last
  `Announce`, then marks `Announce` not arrived while leaving its value standing for the next
  checkpoint, and drops `Scope`. The id is appended unless it repeats the list's last entry (an
  OSC swap's echo and its announcement at apply are one change) or the list is empty and it names
  the avatar worn at boot (an echo that reloads nothing). The list is a sequence: away and back
  to the same avatar is two changes. Nothing from an incoming avatar arrives before its
  announcement at apply, so the last checkpoint before a `Boot` is the outgoing avatar's final
  state.
* `Boot` captures the checkpoint, the list and the baselined flag. Then, valid or not, the live
  values become only what arrived since the last announcement -- the incoming avatar's own
  traffic -- the checkpoint and list empty, the last announced id is recorded as worn at boot, and
  the namespace is baselined. Emptying the live values outright loses a pose: the walks commit the
  home pose before `Boot`, and a value that never changes afterwards is never re-sent.
* Every snapshot needs the namespace baselined, an `Announce` arrived since the last
  `/avatar/change` equal to the checkpoint's, and that value a non-zero int. The arrival mark is
  what stops an avatar that sends no `Announce` from being matched against the outgoing avatar's
  value, which is still standing.
* **Scope 0** then needs exactly one id announced (a swap), or none and a calibration's switch in
  the log (below). An empty list is otherwise a reload of the worn avatar -- a world join, a
  rejoin and Reset Avatar are one event on the wire -- and two or more entries is not one swap.
* **Scope 1 and 2** need the log chosen by service name, this change's switch bound in it, and
  the client's room at this decision equal to the room recorded at the namespace's previous one
  (an unknown room equals nothing). With those, any id list restores: a swap chain, a rejoin of
  the same instance, and a calibration, which holds as at 0. Any other reload of the worn avatar
  -- Reset Avatar, or a reload the log cannot name -- restores at 2 and forgets at 1. Missing any
  of the three, the decision falls back to scope 0's rule, logged, which forgets in every case
  the log was needed for.
* A newly selected send target is a join -- a client that started or restarted -- and deletes
  every namespace. Merely emptying the lists would not do: a restarted client back on a different
  avatar announces an id that is not the one worn at boot, which would read as one swap.
* **Baselined** means this bridge has seen the namespace boot. A bridge started after the avatar
  loaded restores nothing until the next boot; the reconcile below corrects a baselined
  namespace and never baselines one.

**The client log.** Entering FBT calibration reloads the worn avatar, on the wire the same event
as Reset Avatar and a join, but the client then holds the avatar until the user accepts: its
animator runs and its constraints do not solve. Only the client's log tells the three apart, so
the mapping tails it with a `roster.LogTailer` of its own, bound to the client by OSCQuery service
name like `external_ai`'s, from `[external_ai] log_dir`. After the local player's `Switching <name>
to avatar` line a calibration writes `Saving Avatar Data:<worn id>` before the loading
placeholder's local `Initialize` and loads nothing; Reset Avatar writes `Loading Avatar Data:<worn
id>` before the avatar's own; a join follows a room transition, or meets one while its switch is
current. A save after the placeholder is the client's periodic one and counts for nothing, and
Loading is checked first, so it can never turn a Reset into a calibration. A switch is **bound**
to a change when the avatar's own `Initialize ... Avatar VRCPlayer[Local]` line, the second local
one after the switch, was read within `LOG_BIND_SECS` of the change, either side: that line is
written at apply, beside `Boot`, and the previous load's is a second or more older. A bound switch
is also what proves the log is live. The room is `world:instance` from the latest `Joining` line,
seeded from the replay, and unknown after `OnLeftRoom` or `Entering Room` until the next `Joining`.
So under scope 0 an empty list is a calibration's when the namespace saw a change since its last
boot, `VRMode` is 1 (desktop has no calibration, at any scope), the log was chosen by service
name, and this change's bound switch saved the worn id, loaded none of it and met no room
transition. Everything else forgets, which is the safe side: no switch in the log, a log chosen
any other way, lines a client update reworded. A VR reload with no bound switch, or with a bound
switch that neither saved nor loaded the worn id, warns once per log replay, since that is how a
reworded line shows; a switch that settles after its decision logs how late, once.

**The release.** A calibration restore writes the payload and 1 as a swap does, then
`MARK_FLOOR_SECS` later 3 (hold until released); a swap never writes 3. The accept is the first
`Measure Human Avatar Avatar isRemeasure:True` read after the switch settled, a local-only line
(the placeholder's remeasure comes before and is not it): once it is read and the 3 is out, 2
follows `WRITE_SETTLE_SECS` later, so 1, 3 and 2 land in separate frames. The release keeps its
own token and timer rather than the exchange's, because the reconcile must keep reading through a
wait that can last a minute and is then the only source of restored values whose echo never
came; whatever abandons an exchange abandons it, and it gives up, logged, `RELEASE_LIMIT_SECS`
after the 3, below the avatar's own timeout. A log replay during the wait loses the switch the
accept would be read against, and says so.

**Reconciling against the client's tree.** The stream alone cannot carry the snapshot: the client
sends a value only when it changes, so one that never reaches us is never re-sent while it stands,
and neither its out-port stream nor its echo of our writes is delivered reliably under load. A
snapshot lacking a placed prop's word restores the prop to that word's default, out of reach.
So every `RECONCILE_SECS` the mapping reads each baselined namespace's subtree from the
client's OSCQuery server (`OSCManager.fetch_tree`, typed from each node's TYPE tag) and folds in
every payload value that differs from `live`, priming the manager's cache so the change filter
agrees. A read is applied only if the tree's `Boot` is the one this namespace last booted with,
no exchange is in flight, and no `/avatar/change`, `Boot` or target selection landed while it
was out -- the tree switches avatars at apply, before the announcement reaches us, and a read
straddling that must not fold the incoming avatar's reset into the outgoing one's checkpoint.
An address the stream wrote while the read was out keeps the stream's value: the manager's cached
object is snapshotted before the GET and must be the same object at apply, so a read never
overwrites a newer delivered value. Floats compare as float32, since the tree's JSON and
python-osc render one float32 differently. A value the stream missed is therefore in `live` within
one period of coming to rest. Under a pinned target there is no tree to read and the stream is all
there is.

**The change filter.** `_update_cache_and_fire` suppresses a value equal to the last one seen, and
its cache outlives every avatar. So every announcement, and a target selection, `forget()`s the
namespace's payload addresses, its `Announce` and its `Scope`: otherwise an incoming payload value
equal to the outgoing avatar's last -- its home-pose commit, or our restore echoed back -- is
dropped and the next snapshot lacks it, and an equal incoming `Announce` or `Scope` is dropped and
the avatar reads as sending none. `Restore` is forgotten before each 1, so the avatar's 0 reaches
the log. `/avatar/change` is never forgotten: it is `REFIRE_ON_REPEAT`, and forgetting it breaks
the fold of its twin copies.

**Which thread waits.** No handler blocks. Every wait buys ordering or bounds the wait for the
log or an accept, none consumes a result, and the mapping runs them under a router, which ticks,
and beside `bridge.start()` on the library path, where nothing does, so they run on
`threading.Timer` threads, identical in both homes.

**Order across addresses.** Dispatch is thread-per-datagram, so nothing orders one address's
handler against another's. `Announce` and `Scope` against `Boot` are ordered by the settle wait.
The checkpoint at `/avatar/change` against the outgoing avatar's last payload and the incoming
avatar's first still rests on the wire's own spacing: the outgoing avatar falls silent about a
second before the announcement at apply, and the incoming one first writes payload a fifth of a
second after it. The Av3Emulator is the narrow case: its re-send of declared defaults follows its
announcement inside a few milliseconds. The log's lines reach the tailer's thread unordered
against the wire; `LOG_BIND_SECS` is the slack for that.

**Reordering within one address.** A payload handler stores the value the manager's cache
holds rather than the one it was handed: two datagrams for one address can reach this mapping in
reverse arrival order (`docs/design.md` §Inbound delivery semantics), and the cache is
last-arrival-wins under its lock. It reads the cache under the mapping's lock, in the same hold
as the store, so a handler cannot read, be overtaken by the newer datagram's handler, and then
store the older value over it.

**Every shipped router registers it always-on** (`routers._register_persist`), outside mode
switching, because a swap can happen in any mode. **`enabled` belongs to the router.**
Observation is ungated, so a mapping switched off and on again never restores from state it failed
to watch; only writing a snapshot checks `enabled`.
"""

from __future__ import annotations

import struct
import threading
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from vrbridge import VRBridge
from vrbridge.mappings.mapping_base import Mapping
from vrbridge.roster import (
    AvatarDataLoaded, AvatarDataSaved, AvatarInitialized, AvatarRemeasured, AvatarSwitch,
    EnteringRoom, JoiningWorld, LeftRoom, LogTailer, Roster, SelfIdentity, log_service_name)
from vrbridge.settings import settings

# ------------------------------ Config ------------------------------------

# The namespace root, the reserved names and both waits are the wire contract with the avatar
# side, not settings -- a typo here is a diff rather than a silent runtime miss (settings.py's
# header rule). The avatar's window has to outlast LATE_LIMIT_SECS plus WRITE_SETTLE_SECS.
NAMESPACE_ROOT = "/avatar/parameters/BridgePersist/"
NAMESPACE_PATTERN = NAMESPACE_ROOT + "*"
AVATAR_CHANGE_ADDR = "/avatar/change"
VRMODE_ADDR = "/avatar/parameters/VRMode"

ID, ANNOUNCE, BOOT, RESTORE, SCOPE = "Id", "Announce", "Boot", "Restore", "Scope"
#: Never payload: not stored, not restored, not folded from the tree.
RESERVED = (ID, ANNOUNCE, BOOT, RESTORE, SCOPE)

#: What the bridge writes to `Restore`: RESTORED once every payload value is written, then on a
#: calibration restore only HOLD and, at the accept, RELEASED. The avatar writes it back to REST.
REST, RESTORED, RELEASED, HOLD = 0, 1, 2, 3

#: `Scope` values, as the avatar-side generator's `scope:` names them.
SCOPE_SWAP, SCOPE_INSTANCE, SCOPE_KEEP_RESET = 0, 1, 2
SCOPE_NAMES = {SCOPE_SWAP: "Scope 0: swap", SCOPE_INSTANCE: "Scope 1: instance",
               SCOPE_KEEP_RESET: "Scope 2: instance, keeps Reset"}

#: From `Boot` arriving to the validity decision. Covers an `Announce` that arrives after its
#: `Boot`, which the avatar writes in the same state.
ANNOUNCE_SETTLE_SECS = 0.2

#: From the last payload write to `Restore` 1, so the 1 lands in a later client frame.
WRITE_SETTLE_SECS = 0.05

#: The longest after a `Boot` arrived that a snapshot may still begin writing, inside the window.
LATE_LIMIT_SECS = 0.6

#: How often the client log is read, and how often a decision waiting on it re-checks. A desktop
#: swap's avatar `Initialize` line was read about 1 ms after its `Boot` (2026-10-05).
LOG_POLL_SECS = 0.05

#: The longest after a `Boot` arrived that a decision waits for this change's switch; below
#: LATE_LIMIT_SECS, so a decision that then restores still writes inside it.
LOG_DEADLINE_SECS = 0.45

#: How far from the `/avatar/change`, either side, a switch's avatar `Initialize` line may have
#: been read and still be that change's.
LOG_BIND_SECS = 0.5

#: From `Restore` 1 to `Restore` 3.
MARK_FLOOR_SECS = 0.2

#: From `Restore` 3 to giving up on the accept; below the avatar's own timeout on the release.
RELEASE_LIMIT_SECS = 90.0

#: How often each baselined namespace is read back from the client's tree. A value the stream
#: missed is in the next snapshot once a read lands after it came to rest.
RECONCILE_SECS = 1.0


def _default_log_dir() -> Path:
    """The client's log directory: `[external_ai] log_dir`, which persistence reads too."""
    return settings().external_ai.resolved_log_dir()


@dataclass
class _Namespace:
    name: str
    live: Dict[str, Any] = field(default_factory=dict)
    # The payload values that arrived since the last announcement: what `live` becomes at Boot.
    since_announce: Dict[str, Any] = field(default_factory=dict)
    announce: Any = None
    # Whether `announce` arrived since the last /avatar/change. Its value outlives the change.
    announce_arrived: bool = False
    # The Scope that arrived since the last /avatar/change, as it arrived; None when none did.
    scope: Any = None
    checkpoint: Optional[Dict[str, Any]] = None
    checkpoint_announce: Any = None
    ids: List[str] = field(default_factory=list)
    worn_at_boot: Optional[str] = None
    baselined: bool = False
    last_boot: Any = None
    # The client's room at the last decision, when that decision had a bound switch.
    room: Optional[str] = None
    # The exchange in flight: its token (0 when none) and its pending timer.
    token: int = 0
    timer: Optional[threading.Timer] = None
    # A calibration restore's release in flight, kept off `token` so the reconcile reads through
    # it: its token (0 when none), its pending timer, and whether its 3 is out.
    release: int = 0
    release_timer: Optional[threading.Timer] = None
    held: bool = False
    # The log's switch a calibration restore was decided on; its accept is the release.
    reload: Optional["_Switch"] = None

    def addr(self, leaf: str) -> str:
        return f"{NAMESPACE_ROOT}{self.name}/{leaf}"


@dataclass
class _Switch:
    """The local player's latest avatar load, as the client log tells it."""
    moved: bool                 # a room transition came since the previous switch, or during this
    saved: Set[str] = field(default_factory=set)   # saved before the placeholder initialised
    loaded: Set[str] = field(default_factory=set)  # loaded before the avatar initialised
    inits: int = 0              # local `Initialize ... Avatar` lines: the placeholder's, the avatar's
    settled_at: Optional[float] = None  # when the avatar's own line was read
    accepted: bool = False      # a remeasure read after it

    def kind(self, worn: Optional[str]) -> str:
        """`join`, `reset`, `calibration`, or `none` when the switch shows none of them."""
        if self.moved:
            return "join"
        if worn in self.loaded:
            return "reset"
        if worn in self.saved:
            return "calibration"
        return "none"

    def verdict(self, worn: Optional[str]) -> Optional[str]:
        """Why this switch is not a calibration of `worn`, or None when it is."""
        return _NOT_CALIBRATION[self.kind(worn)]


_NOT_CALIBRATION = {
    "join": "the client log shows a world join",
    "reset": "the client log shows Reset Avatar: it loaded the avatar's saved data",
    "none": "the client log shows no calibration: no save of the worn avatar's data at its switch",
    "calibration": None,
}


def _room_key(world_id: Optional[str], instance: Optional[str]) -> Optional[str]:
    return f"{world_id}:{instance}" if world_id and instance else None


class _ClientLog:
    """What the client log has said that classifying a reload needs. Not locked itself; the
    mapping feeds it under its lock."""

    def __init__(self) -> None:
        self.reset(None, None, None)

    def reset(self, rule: Optional[str], self_name: Optional[str], room: Optional[str]) -> None:
        self.rule = rule                     # how the tailer chose the file
        self.self_name = self_name
        self.room = room                     # `world:instance`, None while unknown
        self.switch: Optional[_Switch] = None
        self.moved = False                   # a room transition since the latest switch
        self.warned = False                  # a signature warning given since this replay

    def feed(self, event, now: float) -> Optional[Tuple[str, _Switch]]:
        """Apply one tailed event; return ("settled", switch) when it settles the latest switch
        and ("accepted", switch) when it is that switch's accept."""
        sw = self.switch
        if isinstance(event, SelfIdentity):
            self.self_name = event.name
        elif isinstance(event, (EnteringRoom, JoiningWorld, LeftRoom)):
            self.room = (_room_key(event.world_id, event.instance)
                         if isinstance(event, JoiningWorld) else None)
            self.moved = True
            if sw is not None:
                sw.moved = True
        elif isinstance(event, AvatarSwitch):
            # Every player's switch is logged; only the local player's is ours.
            if self.self_name is not None and event.player == self.self_name:
                self.switch, self.moved = _Switch(moved=self.moved), False
        elif sw is None:
            pass
        elif isinstance(event, AvatarDataSaved):
            # A save after the placeholder is the client's own periodic one, not the switch's.
            if sw.inits == 0:
                sw.saved.add(event.avatar_id)
        elif isinstance(event, AvatarDataLoaded):
            if sw.inits < 2:
                sw.loaded.add(event.avatar_id)
        elif isinstance(event, AvatarInitialized):
            if event.local:
                sw.inits += 1
                if sw.inits == 2:
                    sw.settled_at = now
                    return "settled", sw
        elif isinstance(event, AvatarRemeasured):
            if sw.settled_at is not None and not sw.accepted:
                sw.accepted = True
                return "accepted", sw
        return None


@dataclass(frozen=True)
class _AtBoot:
    """What validity is decided against, captured when the Boot arrived."""
    baselined: bool
    ids: Tuple[str, ...]
    checkpoint: Optional[Dict[str, Any]]
    checkpoint_announce: Any
    arrived: float


def _split(address: str) -> Optional[tuple[str, str]]:
    """`/avatar/parameters/BridgePersist/<Name>/<rest>` -> (Name, rest), else None."""
    if not address.startswith(NAMESPACE_ROOT):
        return None
    name, sep, rest = address[len(NAMESPACE_ROOT):].partition("/")
    if not name or not sep or not rest:
        return None
    return name, rest


def _same_f32(a, b) -> bool:
    """Whether two floats are one float32: python-osc and the OSCQuery JSON round differently."""
    if not isinstance(a, float) or not isinstance(b, float):
        return False
    return struct.pack("<f", a) == struct.pack("<f", b)


def _is_int(value) -> bool:
    # bool is an int subclass; a T on an int address is a mis-authored parameter, not a 1.
    return isinstance(value, int) and not isinstance(value, bool)


def _applied_scope(value) -> int:
    return value if _is_int(value) and value in SCOPE_NAMES else SCOPE_SWAP


# ----------------------------- Mapping ------------------------------------

class BridgePersistMapping(Mapping):
    """Restores `BridgePersist/<Name>/` namespaces across swaps and calibrations, per `Scope`."""
    name = "osc_persist"

    def __init__(self, bridge: VRBridge, *, treat_reload_as_swap: bool = False,
                 log_dir: Optional[Path] = None):
        super().__init__(bridge)
        self.log = bridge.log
        # Test-only, off by default. Exists for a peer that cannot change avatars -- the
        # Av3Emulator re-announces the same avatar on each play entry -- so that a play, stop,
        # play reads as a swap rather than as the reload of the worn avatar it is on the wire.
        # On a live client it would make Reset Avatar and a world join restore, which the
        # lifetime rule forbids.
        self._reload_as_swap = treat_reload_as_swap
        # One plain Lock over all namespace state. Held across UDP sends (sendto, never a
        # fetch) and across forget() and a payload's cache read, which nest _lock ->
        # _cache_lock; nothing takes them the other way, because _update_cache_and_fire
        # releases _cache_lock before firing.
        self._lock = threading.Lock()
        self._ns: Dict[str, _Namespace] = {}
        # The last id announced on /avatar/change, echo or not: what a namespace records as
        # worn when it boots, including one first seen at that boot.
        self._last_announced: Optional[str] = None
        # Global, so a timer from a deleted namespace never matches a new one of the same name.
        self._tokens = 0
        # Bumped by every event that can move the worn avatar under a reconcile read in flight.
        self._epoch = 0
        # When the last /avatar/change was handled.
        self._change_at = float("-inf")
        # Set when a decision went without the switch it waited for, so the switch's late
        # settling is logged; cleared by the next change.
        self._late_watch = False
        # The client log, as its tailer last reported it.
        self._client_log = _ClientLog()
        self._tailer = LogTailer(
            self._on_log_change, on_event=self._on_log_event, poll_secs=LOG_POLL_SECS,
            log_dir=log_dir if log_dir is not None else _default_log_dir(), logger=bridge.log)

    # ---- lifecycle -------------------------------------------------------

    def _attach(self) -> None:
        self.bridge.on_osc(AVATAR_CHANGE_ADDR, self._on_avatar_change)
        self.bridge.osc.watch(VRMODE_ADDR)
        self.bridge.on_osc_pattern(NAMESPACE_PATTERN, self._on_namespace)
        # Runs on zeroconf's single dispatch thread, so it only clears. It fires on a real
        # change of target -- a new client, or one back on a fresh port after a restart -- and
        # on the first resolve after our own OSCManager.stop()/start(); an unchanged mDNS
        # republication returns early in _consider_service and never reaches it. Every one of
        # those is a join or a gap in what we watched, so each clears.
        self.bridge.on_target_selected(self._on_target_selected)
        self.bridge.on_stop(lambda ctx: self.close())
        self._tailer.retarget(log_service_name(self.bridge.osc.current_service_name))
        self._tailer.start()
        threading.Thread(target=_reconcile_loop, args=(weakref.ref(self),), daemon=True,
                         name="BridgePersist-reconcile").start()

    def close(self) -> None:
        """Stop tailing the client log. For tests and embedders; the bridge's stop calls it."""
        self._tailer.stop()

    # ---- events ----------------------------------------------------------

    def _on_avatar_change(self, ctx, address: str, value) -> None:
        with self._lock:
            self._last_announced = value
            self._epoch += 1
            self._change_at = time.monotonic()
            self._late_watch = False
            for ns in self._ns.values():
                self._abandon_locked(ns, "an avatar change")
                ns.checkpoint = dict(ns.live)
                ns.checkpoint_announce = ns.announce
                ns.announce_arrived = False
                ns.scope = None
                ns.since_announce = {}
                self._forget_locked(ns)
                if ns.ids:
                    if ns.ids[-1] != value:
                        ns.ids.append(value)
                elif value != ns.worn_at_boot or self._reload_as_swap:
                    ns.ids.append(value)

    # Both log callbacks run on the tailer's thread with its lock held, which nests the tailer's
    # lock -> _lock; nothing here takes the tailer's lock (retarget takes only its flag lock).

    def _on_log_change(self, roster: Roster, change: str) -> None:
        if change != "snapshot":
            return
        # A replay, of a newly chosen file or the same one: whatever switch was open is unknown.
        with self._lock:
            for ns in self._ns.values():
                if ns.release:
                    self.log.warning("BridgePersist/%s: the client log was re-read during a "
                                     "calibration hold, so its accept can no longer be matched; "
                                     "the release falls to the give-up and the avatar's own "
                                     "timeout.", ns.name)
            self._client_log.reset(self._tailer.rule, roster.self_name,
                                   _room_key(roster.world_id, roster.instance))

    def _on_log_event(self, event) -> None:
        now = time.monotonic()
        with self._lock:
            got = self._client_log.feed(event, now)
            if got is None:
                return
            what, sw = got
            if what == "settled":
                if self._late_watch:
                    self._late_watch = False
                    self.log.info("BridgePersist: the client log's switch settled %+.0f ms from "
                                  "the change, after the decision went without it.",
                                  (now - self._change_at) * 1000)
                return
            # The accept. A release whose 3 is not out yet is released as the 3 goes. A fresh
            # token, so a give-up already waiting on the lock cannot cancel the 2.
            for ns in self._ns.values():
                if ns.release and ns.held and ns.reload is sw:
                    ns.release_timer.cancel()
                    self._tokens += 1
                    ns.release = self._tokens
                    self._arm_locked(ns, WRITE_SETTLE_SECS, self._write_released, None,
                                     release=True)

    def _on_target_selected(self, ctx, target) -> None:
        with self._lock:
            self._epoch += 1
            for ns in self._ns.values():
                self._abandon_locked(ns, "a newly selected OSC target")
                self._forget_locked(ns)
            if self._ns:
                self.log.info("OSC target selected (%s:%d): a join, so every BridgePersist "
                              "namespace is cleared.", target[0], target[1])
            self._ns.clear()
            # The previous client's headset and log say nothing about this one's.
            self.bridge.osc.forget(VRMODE_ADDR)
            self._client_log.reset(None, None, None)
        self._tailer.retarget(log_service_name(self.bridge.osc.current_service_name))

    def _on_namespace(self, ctx, address: str, value) -> None:
        parsed = _split(address)
        if parsed is None:
            return
        name, rest = parsed
        if rest == ID:
            return
        if rest == RESTORE:
            if _is_int(value) and value == REST:
                self.log.info("BridgePersist/%s: Restore is back at 0 (the avatar ending a "
                              "restore, or resetting its namespace).", name)
            return
        with self._lock:
            ns = self._ns.get(name)
            if ns is None:
                ns = self._ns[name] = _Namespace(name)
            if rest == ANNOUNCE:
                ns.announce = value
                ns.announce_arrived = True
            elif rest == SCOPE:
                ns.scope = value
                if _applied_scope(value) != value or isinstance(value, bool):
                    self.log.info("BridgePersist/%s: Scope %r is not 0, 1 or 2 as an int; it "
                                  "counts as 0.", name, value)
            elif rest == BOOT:
                self._on_boot_locked(ns, value)
            else:
                # Payload: take the cache's value, read under our lock, which is the latest
                # arrival even when this listener fires after a newer one for the same address
                # or waits here while one lands (module docstring).
                value = ctx.get(address, value)
                ns.live[address] = value
                ns.since_announce[address] = value

    def _on_boot_locked(self, ns: _Namespace, value) -> None:
        if isinstance(value, bool) or not isinstance(value, float) or not 0.0 < value <= 1.0:
            # Not a boot. Nothing is recorded, not even as the last Boot, so the real draw
            # that follows is never taken for a repeat.
            return
        if value == ns.last_boot:
            # The same draw again is a repeated delivery of one boot, not a second load.
            return
        ns.last_boot = value
        self._epoch += 1
        arrived = time.monotonic()
        # The avatar reloaded, so an exchange still in flight was for an animator that is gone.
        self._abandon_locked(ns, "a further Boot")
        at_boot = _AtBoot(ns.baselined, tuple(ns.ids), ns.checkpoint, ns.checkpoint_announce,
                          arrived)

        ns.live = dict(ns.since_announce)
        ns.checkpoint = None
        ns.checkpoint_announce = None
        ns.ids = []
        ns.worn_at_boot = self._last_announced
        ns.baselined = True

        self._tokens += 1
        ns.token = self._tokens
        self._arm_locked(ns, ANNOUNCE_SETTLE_SECS, self._decide, at_boot)

    # ---- the decision ----------------------------------------------------

    def _bound_switch_locked(self) -> Optional[_Switch]:
        """The latest local switch, when it settled within LOG_BIND_SECS of the last change."""
        sw = self._client_log.switch
        if sw is None or sw.settled_at is None:
            return None
        return sw if abs(sw.settled_at - self._change_at) <= LOG_BIND_SECS else None

    def _announce_invalid(self, ns: _Namespace, at: _AtBoot) -> Optional[str]:
        if not ns.announce_arrived:
            return (f"no Announce arrived from the incoming avatar within "
                    f"{ANNOUNCE_SETTLE_SECS:.2f} s of its Boot")
        if at.checkpoint_announce != ns.announce:
            return (f"Announce changed from {at.checkpoint_announce!r} to {ns.announce!r}, so "
                    f"the incoming prefab is not the outgoing one")
        if not _is_int(ns.announce) or ns.announce == 0:
            return f"Announce is {ns.announce!r}, and only a non-zero int enables persistence"
        return None

    def _judge_locked(self, ns: _Namespace, at: _AtBoot, final: bool):
        """(how, why): how is SWAP or CALIBRATION to restore, None to forget, and why names the
        rule that decided. _PENDING, unless final, while the log may yet show this change's
        switch and the decision needs it. A first boot waits too, so the room it records is
        proven."""
        scope = _applied_scope(ns.scope)
        log = self._client_log
        sw = self._bound_switch_locked()
        reload = not at.ids
        vr = self.bridge.osc.get_cached(VRMODE_ADDR)
        in_vr = _is_int(vr) and vr == 1
        needs_log = log.rule == "service" and (
            scope != SCOPE_SWAP or (reload and at.checkpoint is not None and in_vr))
        if sw is None and needs_log and not final:
            return _PENDING
        if not at.baselined:
            return None, ("this bridge has not seen the namespace boot before, so it cannot tell "
                          "what its values belong to (started after the avatar loaded?)")
        bad_announce = self._announce_invalid(ns, at)
        if bad_announce is not None:
            return None, bad_announce

        fallback = ""
        if scope != SCOPE_SWAP:
            room = log.room if sw is not None else None
            if at.checkpoint is None:
                why = "no avatar change since its last boot"
            elif log.rule != "service":
                why = (f"the client log was not chosen by this client's OSCQuery service name "
                       f"(chosen by {log.rule or 'nothing yet'})")
            elif sw is None:
                why = "the client log showed no switch for this change"
            elif room is None or room != ns.room:
                why = (f"the room ({room or 'unknown'}) is not the one at this namespace's last "
                       f"decision ({ns.room or 'unknown'})")
            else:
                why = None
            if why is None:
                if at.ids:
                    return SWAP, (f"{len(at.ids)} avatar change(s) since its last boot, inside "
                                  f"one instance")
                kind = sw.kind(ns.worn_at_boot)
                if kind == "calibration" and in_vr:
                    return CALIBRATION, "a calibration reload"
                if kind == "join":
                    return SWAP, "a rejoin of the same instance"
                if scope == SCOPE_KEEP_RESET:
                    return SWAP, "a reload of the worn avatar inside one instance (Reset Avatar)"
                return None, "a reload of the worn avatar that is not a calibration (Reset Avatar)"
            fallback = f"; falling back to Scope 0's rule, since {why}"

        if reload:
            if at.checkpoint is None:
                why = "no avatar change since its last boot"
            elif not in_vr:
                why = f"VRMode is {vr!r}, not 1"
            elif log.rule != "service":
                why = (f"the client log was not chosen by this client's OSCQuery service name "
                       f"(chosen by {log.rule or 'nothing yet'})")
            elif sw is None:
                why = "the client log showed no switch for this change"
            else:
                why = sw.verdict(ns.worn_at_boot)
            if why is not None:
                return None, f"a reload of the worn avatar, and not a calibration's ({why}){fallback}"
            return CALIBRATION, f"a calibration reload{fallback}"
        if len(at.ids) > 1:
            return None, (f"{len(at.ids)} avatars were announced since its last boot "
                          f"({', '.join(map(str, at.ids))}), which is not a single swap{fallback}")
        return SWAP, f"one swap{fallback}"

    def _decide(self, name: str, token: int, at_boot: _AtBoot) -> None:
        with self._lock:
            ns = self._ns.get(name)
            if ns is None or ns.token != token:
                return
            final = time.monotonic() - at_boot.arrived + LOG_POLL_SECS > LOG_DEADLINE_SECS
            verdict = self._judge_locked(ns, at_boot, final)
            if verdict is _PENDING:
                self._arm_locked(ns, LOG_POLL_SECS, self._decide, at_boot)
                return
            ns.token, ns.timer = 0, None
            how, why = verdict
            scope = SCOPE_NAMES[_applied_scope(ns.scope)]
            sw = self._bound_switch_locked()
            self._note_log_locked(ns, at_boot, sw)
            ns.room = self._client_log.room if sw is not None else None
            if how is None:
                self.log.info("BridgePersist/%s booted (%s); nothing to restore: %s.", name,
                              scope, why)
                return
            if not self.enabled:
                self.log.info("BridgePersist/%s booted (%s) with a valid snapshot, but the "
                              "mapping is disabled; not restoring.", name, scope)
                return
            elapsed = time.monotonic() - at_boot.arrived
            if elapsed > LATE_LIMIT_SECS:
                self.log.warning("BridgePersist/%s: the decision ran %.3f s after Boot, past the "
                                 "%.3f s limit; dropping the snapshot unwritten, so the avatar "
                                 "boots from defaults.", name, elapsed, LATE_LIMIT_SECS)
                return
            snapshot = at_boot.checkpoint
            ok = True
            for addr, v in snapshot.items():
                ok = self.bridge.osc.send(addr, v) and ok
            if not ok:
                self.log.warning("BridgePersist/%s: a payload write was dropped; withholding "
                                 "Restore=1, so the avatar boots from defaults.", name)
                return
            self.log.info("BridgePersist/%s booted (%s); restoring %d value(s): %s.", name, scope,
                          len(snapshot), why)
            calibration = how is CALIBRATION
            if calibration:
                ns.reload = sw
            ns.token = token
            self._arm_locked(ns, WRITE_SETTLE_SECS, self._write_restored, calibration)

    def _note_log_locked(self, ns: _Namespace, at: _AtBoot, sw: Optional[_Switch]) -> None:
        """What the log showed for a reload: its timing always, and once per replay a VR reload
        whose switch is missing or names nothing, the trace a reworded client line leaves."""
        if at.ids or at.checkpoint is None:
            return
        log = self._client_log
        if sw is not None:
            self.log.info("BridgePersist/%s: the client log read the avatar's Initialize %+.0f ms "
                          "from the change.", ns.name, (sw.settled_at - self._change_at) * 1000)
        elif log.rule == "service":
            self.log.info("BridgePersist/%s: no bound switch in the client log by the decision.",
                          ns.name)
            self._late_watch = True
        vr = self.bridge.osc.get_cached(VRMODE_ADDR)
        if log.rule != "service" or not (_is_int(vr) and vr == 1) or log.warned:
            return
        if sw is None:
            what = (f"no switch settled within {LOG_BIND_SECS:.1f} s of the change by "
                    f"{LOG_DEADLINE_SECS:.2f} s after Boot")
        elif sw.kind(ns.worn_at_boot) == "none":
            what = "this change's switch neither saved nor loaded the worn avatar's data"
        else:
            return
        log.warned = True
        self.log.warning("BridgePersist/%s: a VR reload, and %s. If this repeats, the client may "
                         "have changed its log lines; calibrations will not restore until "
                         "osc_persist reads them again.", ns.name, what)

    # ---- reconcile -------------------------------------------------------

    def _reconcile(self) -> None:
        osc = self.bridge.osc
        with self._lock:
            epoch = self._epoch
            # The cached object per known address, before the GET: one the stream replaced
            # while the read was out is newer than the read, so the read must not touch it.
            due = [(ns.name, ns.last_boot, {a: osc.get_cached(a, _MISSING) for a in ns.live})
                   for ns in self._ns.values()
                   if ns.baselined and ns.last_boot is not None and not ns.token]
        for name, boot, cached in due:
            res = osc.fetch_tree(f"{NAMESPACE_ROOT}{name}")
            if not res.ok:
                self.log.debug("BridgePersist/%s: no reconcile read (%s: %s).", name,
                               res.reason, res.detail)
                continue
            tree = res.value
            with self._lock:
                ns = self._ns.get(name)
                if (ns is None or self._epoch != epoch or ns.token or ns.last_boot != boot
                        or not _same_f32(tree.get(ns.addr(BOOT)), boot)):
                    continue
                changed = []
                for address, value in tree.items():
                    parsed = _split(address)
                    if parsed is None or parsed[0] != name or parsed[1] in RESERVED:
                        continue
                    if osc.get_cached(address, _MISSING) is not cached.get(address, _MISSING):
                        continue
                    old = ns.live.get(address, _MISSING)
                    if old is not _MISSING and type(old) is type(value) and (
                            _same_f32(old, value) if isinstance(value, float) else old == value):
                        continue
                    osc.prime(address, value)
                    ns.live[address] = value
                    ns.since_announce[address] = value
                    changed.append(address)
                if changed:
                    self.log.debug("BridgePersist/%s: reconciled %d value(s) from the client's "
                                   "tree: %s", name, len(changed), ", ".join(changed[:8]))

    # ---- timers ----------------------------------------------------------

    def _write_restored(self, name: str, token: int, calibration: bool) -> None:
        with self._lock:
            ns = self._ns.get(name)
            if ns is None or ns.token != token:
                return
            ns.token, ns.timer = 0, None
            restore = ns.addr(RESTORE)
            self.bridge.osc.forget(restore)
            if not self.bridge.osc.send(restore, RESTORED):
                self.log.warning("BridgePersist/%s: could not write Restore=1; the avatar boots "
                                 "from defaults.", name)
                return
            if calibration:
                self._tokens += 1
                ns.release = self._tokens
                self._arm_locked(ns, MARK_FLOOR_SECS, self._write_hold, None, release=True)

    def _write_hold(self, name: str, release: int, _unused) -> None:
        with self._lock:
            ns = self._ns.get(name)
            if ns is None or ns.release != release:
                return
            if not self.bridge.osc.send(ns.addr(RESTORE), HOLD):
                self.log.warning("BridgePersist/%s: could not write Restore=3; the avatar ends "
                                 "its hold on its own timer, as after a swap.", name)
                ns.release, ns.release_timer = 0, None
                return
            ns.held = True
            if ns.reload is not None and ns.reload.accepted:
                self._arm_locked(ns, WRITE_SETTLE_SECS, self._write_released, None, release=True)
            else:
                self._arm_locked(ns, RELEASE_LIMIT_SECS, self._give_up, None, release=True)

    def _write_released(self, name: str, release: int, _unused) -> None:
        with self._lock:
            ns = self._ns.get(name)
            if ns is None or ns.release != release:
                return
            ns.release, ns.release_timer, ns.held = 0, None, False
            if not self.bridge.osc.send(ns.addr(RESTORE), RELEASED):
                self.log.warning("BridgePersist/%s: could not write Restore=2; the avatar ends "
                                 "its hold at its own timeout.", name)

    def _give_up(self, name: str, release: int, _unused) -> None:
        with self._lock:
            ns = self._ns.get(name)
            if ns is None or ns.release != release:
                return
            ns.release, ns.release_timer, ns.held = 0, None, False
            self.log.warning("BridgePersist/%s: the client log showed no accept within %.0f s of "
                             "Restore=3; not releasing, and the avatar ends its hold at its own "
                             "timeout.", name, RELEASE_LIMIT_SECS)

    def _arm_locked(self, ns: _Namespace, delay: float, fn, arg, *, release=False) -> None:
        t = threading.Timer(delay, fn, args=(ns.name, ns.release if release else ns.token, arg))
        t.daemon = True
        t.name = f"BridgePersist-{ns.name}"
        if release:
            ns.release_timer = t
        else:
            ns.timer = t
        t.start()

    def _abandon_locked(self, ns: _Namespace, why: str) -> None:
        if ns.token:
            self.log.info("BridgePersist/%s: %s during the exchange; abandoning it.", ns.name,
                          why)
            if ns.timer is not None:
                ns.timer.cancel()
            ns.token, ns.timer = 0, None
        if ns.release:
            self.log.info("BridgePersist/%s: %s before the release; abandoning it.", ns.name, why)
            if ns.release_timer is not None:
                ns.release_timer.cancel()
            ns.release, ns.release_timer, ns.held = 0, None, False

    def _forget_locked(self, ns: _Namespace) -> None:
        # Payload, Announce and Scope; /avatar/change is never here.
        for addr in ns.live:
            self.bridge.osc.forget(addr)
        self.bridge.osc.forget(ns.addr(ANNOUNCE))
        self.bridge.osc.forget(ns.addr(SCOPE))


_MISSING = object()
_PENDING = object()
SWAP, CALIBRATION = "swap", "calibration"


def _reconcile_loop(ref: "weakref.ref[BridgePersistMapping]") -> None:
    # Holds the mapping weakly, so a discarded bridge's mapping is collected and its loop ends.
    while True:
        time.sleep(RECONCILE_SECS)
        m = ref()
        if m is None:
            return
        try:
            m._reconcile()
        except Exception:
            m.log.exception("BridgePersist: reconcile failed")
        del m
