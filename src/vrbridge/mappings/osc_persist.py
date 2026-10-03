"""Bridge persistence: carry an avatar-published state across one avatar swap or a calibration.

An avatar publishes state under `/avatar/parameters/BridgePersist/<Name>/`, one namespace per
composition, and this mapping writes it back into the next avatar when that avatar carries the
same namespace with the same identity. It holds no manifest and enumerates nothing: the
namespace is the whole contract, and only names that arrive on the wire are ever known.

Four direct children of a namespace are reserved; every other address under it, at any depth, is
**payload**:

* `Id` -- the per-prefab identity, carried as the declared default and so never on the wire. 0
  means persistence is off. Ignored here if it ever arrives.
* `Announce` (int, avatar -> bridge) -- a driver copy of `Id`, written in the same state as `Boot`,
  so it can reach us before or after it.
* `Boot` (float, avatar -> bridge) -- a driver `random` in (0, 1], once per animator load. Anything
  else, the emulator's re-send of the declared 0.0 included, is not a boot and is ignored.
* `Restore` (int) -- the bridge writes 1 once every payload value of the snapshot is written, and
  on a calibration restore then 3 and, at the accept, 2; the avatar writes it back to 0. That 0
  is logged and nothing waits for it.

**The exchange.** Nothing is acknowledged. At `Boot` the bookkeeping below happens at once; the
validity decision happens `ANNOUNCE_SETTLE_SECS` later, against the state captured at the `Boot`,
which is what lets an `Announce` arriving after its `Boot` count. A valid snapshot is written, each
value with the wire type it arrived with, then `WRITE_SETTLE_SECS` later `Restore` 1 (the client
applies the latest value per parameter per frame, so the wait puts the 1 in a later frame than the
payload), and, but for a calibration restore's release (below), the mapping is finished with
it. The avatar waits for the 1 in a window of its own
and boots from defaults without it, so a payload write the bridge fails to send withholds the 1,
and nothing is retried. A write lost after it leaves is invisible here: UDP reports no receive. A decision running more than `LATE_LIMIT_SECS` after its
`Boot` arrived (a stalled bridge) writes nothing: payload landing after the window closes lands on
a running prop, which a watching remote sees thrown out of place. A `/avatar/change`, a further
`Boot` for the namespace, or a newly selected target during either wait abandons the exchange; a
per-exchange token makes the timer that lost the race a no-op. Payload the snapshot does not name
stays at the default the avatar reset it to, which is what makes a snapshot complete without holding
unchanged values.

**What is kept per namespace.** The live payload values, each as the Python value python-osc
parsed, so it replays with its wire type (an int sent to a declared float writes garbage, and a
bool must stay a bool); the last `Announce` and whether one arrived since the last
`/avatar/change`; a checkpoint; and the avatar ids announced since the namespace last booted.

* Every `/avatar/change`, echo or not, re-takes the checkpoint from the live values and the last
  `Announce`, then marks `Announce` not arrived while leaving its value standing for the next
  checkpoint. The id is appended unless it repeats the list's last entry (an OSC swap's echo and
  its announcement at apply are one change) or the list is empty and it names the avatar worn at
  boot (an echo that reloads nothing). The list is a sequence: away and back to the same avatar is
  two changes. Nothing from an incoming avatar arrives before its announcement at apply, so the
  last checkpoint before a `Boot` is the outgoing avatar's final state.
* `Boot` captures the checkpoint, the list and the baselined flag. Then, valid or not, the live
  values become only what arrived since the last announcement -- the incoming avatar's own
  traffic -- the checkpoint and list empty, the last announced id is recorded as worn at boot, and
  the namespace is baselined. Emptying the live values outright loses a pose: the walks commit the
  home pose before `Boot`, and a value that never changes afterwards is never re-sent.
* The snapshot is valid when the namespace was baselined, exactly one id was announced, an
  `Announce` arrived since the last `/avatar/change` and equals the checkpoint's, and that value
  is a non-zero int. The arrival mark is what stops an avatar that sends no `Announce` from being
  matched against the outgoing avatar's value, which is still standing.
* An empty list at `Boot` is a reload of the worn avatar -- a world join, a rejoin and Reset
  Avatar are one event on the wire -- and restores nothing unless it is a calibration's (below);
  two or more entries is not one swap.
* A newly selected send target is a join -- a client that started or restarted -- and deletes
  every namespace. Merely emptying the lists would not do: a restarted client back on a different
  avatar announces an id that is not the one worn at boot, which would read as one swap.
* **Baselined** means this bridge has seen the namespace boot. A bridge started after the avatar
  loaded restores nothing until the next boot; the reconcile below corrects a baselined
  namespace and never baselines one.

**A calibration reload.** Entering FBT calibration reloads the worn avatar, on the wire the same
event as Reset Avatar and a join, but the client then holds the avatar until the user accepts:
its animator runs, its constraints do not solve, and the client sends no `Upright`. So an empty
list is a calibration's when the namespace saw a change since its last boot, `VRMode` (watched
beside `Upright`) is 1, an `Upright` arrived within `LIVE_SECS` before that change, and none has
arrived since it; the rest of the validity rule then runs against that change's checkpoint. The
liveness term is what keeps an idle headset, which can send no `Upright` for minutes, from
reading as held. The decision for an empty list runs `RELOAD_SETTLE_SECS` after `Boot`, where a
swap's keeps `ANNOUNCE_SETTLE_SECS`: Reset Avatar's and a join's first `Upright` arrives within
25 ms of `Boot`, a calibration's 400 ms or more after it. A calibration restore writes the
payload and 1 as a swap does, then `MARK_FLOOR_SECS` later 3 (hold until released); a swap never
writes 3. The first `Upright` since the change is the accept: once it has arrived and the 3 is
out, 2 follows `WRITE_SETTLE_SECS` later, so 1, 3 and 2 land in separate frames. The release
keeps its own token and timer rather than the exchange's, because the reconcile must keep
reading through a wait that can last a minute and is then the only source of restored values
whose echo never came; whatever abandons an exchange abandons it, and it gives up, logged,
`RELEASE_LIMIT_SECS` after the 3, below the avatar's own timeout.

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
namespace's payload addresses and its `Announce`: otherwise an incoming payload value equal to the
outgoing avatar's last -- its home-pose commit, or our restore echoed back -- is dropped and the
next snapshot lacks it, and an equal incoming `Announce` is dropped and the avatar reads as
sending none. `Restore` is forgotten before each 1, so the avatar's 0 reaches the log. `Upright`
is forgotten at each arrival, so every one fires, the first after a reload included when it
equals the last before it, as it does when saturated at exactly 1.0, and whichever of it and the
`/avatar/change` reaches the manager first.
`/avatar/change` is never forgotten: it is `REFIRE_ON_REPEAT`, and forgetting it breaks the fold of
its twin copies.

**Which thread waits.** No handler blocks. Every wait buys ordering or bounds the wait for an
accept, none consumes a result, and the mapping runs them under a router, which ticks, and beside `bridge.start()` on the library path,
where nothing does, so they run on `threading.Timer` threads, identical in both homes.

**Order across addresses.** Dispatch is thread-per-datagram, so nothing orders one address's
handler against another's. `Announce` against `Boot` is ordered by the settle wait. The checkpoint
at `/avatar/change` against the outgoing avatar's last payload and the incoming avatar's first
still rests on the wire's own spacing: the outgoing avatar falls silent about a second before the
announcement at apply, and the incoming one first writes payload a fifth of a second after it.
The Av3Emulator is the narrow case: its re-send of declared defaults follows its announcement
inside a few milliseconds. `Upright` against `/avatar/change` is the other: Reset Avatar's first
`Upright` follows its change by about 30 ms, so its handler can run first. An `Upright` handled
within `UPRIGHT_ORDER_SECS` before the change's handler therefore counts as after the change,
which only errs toward forgetting: the outgoing avatar falls silent about 600 ms before a
calibration's change.

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
from typing import Any, Dict, List, Optional, Tuple

from vrbridge import VRBridge
from vrbridge.mappings.mapping_base import Mapping

# ------------------------------ Config ------------------------------------

# The namespace root, the reserved names and both waits are the wire contract with the avatar
# side, not settings -- a typo here is a diff rather than a silent runtime miss (settings.py's
# header rule). The avatar's window has to outlast LATE_LIMIT_SECS plus WRITE_SETTLE_SECS.
NAMESPACE_ROOT = "/avatar/parameters/BridgePersist/"
NAMESPACE_PATTERN = NAMESPACE_ROOT + "*"
AVATAR_CHANGE_ADDR = "/avatar/change"
UPRIGHT_ADDR = "/avatar/parameters/Upright"
VRMODE_ADDR = "/avatar/parameters/VRMode"

ID, ANNOUNCE, BOOT, RESTORE = "Id", "Announce", "Boot", "Restore"

#: What the bridge writes to `Restore`: RESTORED once every payload value is written, then on a
#: calibration restore only HOLD and, at the accept, RELEASED. The avatar writes it back to REST.
REST, RESTORED, RELEASED, HOLD = 0, 1, 2, 3

#: From `Boot` arriving to the validity decision. Covers an `Announce` that arrives after its
#: `Boot`, which the avatar writes in the same state.
ANNOUNCE_SETTLE_SECS = 0.2

#: From the last payload write to `Restore` 1, so the 1 lands in a later client frame.
WRITE_SETTLE_SECS = 0.05

#: From `Boot` arriving to the decision for a reload of the worn avatar: between Reset Avatar's and
#: a join's first `Upright`, at most 25 ms after `Boot`, and a calibration's, at least 400 ms.
RELOAD_SETTLE_SECS = 0.3

#: The longest after a `Boot` arrived that a snapshot may still begin writing, inside the window.
LATE_LIMIT_SECS = 0.6

#: How recent an `Upright` must be at the `/avatar/change` for the reload to be a calibration's.
LIVE_SECS = 5.0

#: An `Upright` handled this soon before the `/avatar/change`'s handler counts as after the change.
UPRIGHT_ORDER_SECS = 0.3

#: From `Restore` 1 to `Restore` 3.
MARK_FLOOR_SECS = 0.2

#: From `Restore` 3 to giving up on the accept; below the avatar's own timeout on the release.
RELEASE_LIMIT_SECS = 90.0

#: How often each baselined namespace is read back from the client's tree. A value the stream
#: missed is in the next snapshot once a read lands after it came to rest.
RECONCILE_SECS = 1.0


@dataclass
class _Namespace:
    name: str
    live: Dict[str, Any] = field(default_factory=dict)
    # The payload values that arrived since the last announcement: what `live` becomes at Boot.
    since_announce: Dict[str, Any] = field(default_factory=dict)
    announce: Any = None
    # Whether `announce` arrived since the last /avatar/change. Its value outlives the change.
    announce_arrived: bool = False
    checkpoint: Optional[Dict[str, Any]] = None
    checkpoint_announce: Any = None
    ids: List[str] = field(default_factory=list)
    worn_at_boot: Optional[str] = None
    baselined: bool = False
    last_boot: Any = None
    # The exchange in flight: its token (0 when none) and its pending timer.
    token: int = 0
    timer: Optional[threading.Timer] = None
    # A calibration restore's release in flight, kept off `token` so the reconcile reads through
    # it: its token (0 when none), its pending timer, and whether its 3 is out.
    release: int = 0
    release_timer: Optional[threading.Timer] = None
    held: bool = False

    def addr(self, leaf: str) -> str:
        return f"{NAMESPACE_ROOT}{self.name}/{leaf}"


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


# ----------------------------- Mapping ------------------------------------

class BridgePersistMapping(Mapping):
    """Restores `BridgePersist/<Name>/` namespaces across one avatar swap or a calibration."""
    name = "osc_persist"

    def __init__(self, bridge: VRBridge, *, treat_reload_as_swap: bool = False):
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
        # When the last Upright arrived; whether one has since the last /avatar/change; and
        # whether one had in the LIVE_SECS before that change.
        self._upright_at: Optional[float] = None
        self._upright_since_change = False
        self._live_at_change = False

    # ---- lifecycle -------------------------------------------------------

    def _attach(self) -> None:
        self.bridge.on_osc(AVATAR_CHANGE_ADDR, self._on_avatar_change)
        self.bridge.on_osc(UPRIGHT_ADDR, self._on_upright, watch=[VRMODE_ADDR])
        self.bridge.on_osc_pattern(NAMESPACE_PATTERN, self._on_namespace)
        # Runs on zeroconf's single dispatch thread, so it only clears. It fires on a real
        # change of target -- a new client, or one back on a fresh port after a restart -- and
        # on the first resolve after our own OSCManager.stop()/start(); an unchanged mDNS
        # republication returns early in _consider_service and never reaches it. Every one of
        # those is a join or a gap in what we watched, so each clears.
        self.bridge.on_target_selected(self._on_target_selected)
        threading.Thread(target=_reconcile_loop, args=(weakref.ref(self),), daemon=True,
                         name="BridgePersist-reconcile").start()

    # ---- events ----------------------------------------------------------

    def _on_avatar_change(self, ctx, address: str, value) -> None:
        with self._lock:
            self._last_announced = value
            self._epoch += 1
            since = (None if self._upright_at is None
                     else time.monotonic() - self._upright_at)
            self._live_at_change = since is not None and since <= LIVE_SECS
            # One handled just before this one may have arrived after it (module docstring).
            self._upright_since_change = since is not None and since <= UPRIGHT_ORDER_SECS
            for ns in self._ns.values():
                self._abandon_locked(ns, "an avatar change")
                ns.checkpoint = dict(ns.live)
                ns.checkpoint_announce = ns.announce
                ns.announce_arrived = False
                ns.since_announce = {}
                self._forget_locked(ns)
                if ns.ids:
                    if ns.ids[-1] != value:
                        ns.ids.append(value)
                elif value != ns.worn_at_boot or self._reload_as_swap:
                    ns.ids.append(value)

    def _on_upright(self, ctx, address: str, value) -> None:
        with self._lock:
            self.bridge.osc.forget(UPRIGHT_ADDR)
            self._upright_at = time.monotonic()
            if self._upright_since_change:
                return
            self._upright_since_change = True
            # The accept. A release whose 3 is not out yet is released as the 3 goes. A fresh
            # token, so a give-up already waiting on the lock cannot cancel the 2.
            for ns in self._ns.values():
                if ns.release and ns.held:
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
            # The previous client's headset says nothing about this one's.
            self._upright_at, self._upright_since_change, self._live_at_change = None, False, False
            self.bridge.osc.forget(VRMODE_ADDR)

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
        self._arm_locked(ns, ANNOUNCE_SETTLE_SECS if at_boot.ids else RELOAD_SETTLE_SECS,
                         self._decide, at_boot)

    def _invalid_reason(self, ns: _Namespace, at: _AtBoot) -> Optional[str]:
        if not at.baselined:
            return ("this bridge has not seen the namespace boot before, so it cannot tell what "
                    "its values belong to (started after the avatar loaded?)")
        if not at.ids:
            why = self._not_calibration(at)
            if why is not None:
                return f"a reload of the worn avatar, and not a calibration's ({why})"
        if len(at.ids) > 1:
            return (f"{len(at.ids)} avatars were announced since its last boot "
                    f"({', '.join(map(str, at.ids))}), which is not a single swap")
        if not ns.announce_arrived:
            settle = ANNOUNCE_SETTLE_SECS if at.ids else RELOAD_SETTLE_SECS
            return (f"no Announce arrived from the incoming avatar within {settle:.2f} s of "
                    f"its Boot")
        if at.checkpoint_announce != ns.announce:
            return (f"Announce changed from {at.checkpoint_announce!r} to {ns.announce!r}, so "
                    f"the incoming prefab is not the outgoing one")
        if not _is_int(ns.announce) or ns.announce == 0:
            return f"Announce is {ns.announce!r}, and only a non-zero int enables persistence"
        return None

    def _not_calibration(self, at: _AtBoot) -> Optional[str]:
        """Why a reload of the worn avatar is not a calibration's, or None when it is."""
        if at.checkpoint is None:
            return "no avatar change since its last boot"
        vr = self.bridge.osc.get_cached(VRMODE_ADDR)
        if not _is_int(vr) or vr != 1:
            return f"VRMode is {vr!r}, not 1"
        if not self._live_at_change:
            return f"no Upright in the {LIVE_SECS:.0f} s before the change"
        if self._upright_since_change:
            return "an Upright arrived since the change, so the avatar is not held"
        return None

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
                    if (parsed is None or parsed[0] != name
                            or parsed[1] in (ID, ANNOUNCE, BOOT, RESTORE)):
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

    def _decide(self, name: str, token: int, at_boot: _AtBoot) -> None:
        with self._lock:
            ns = self._ns.get(name)
            if ns is None or ns.token != token:
                return
            ns.token, ns.timer = 0, None
            reason = self._invalid_reason(ns, at_boot)
            if reason is not None:
                self.log.info("BridgePersist/%s booted; nothing to restore: %s.", name, reason)
                return
            if not self.enabled:
                self.log.info("BridgePersist/%s booted with a valid snapshot, but the mapping "
                              "is disabled; not restoring.", name)
                return
            elapsed = time.monotonic() - at_boot.arrived
            if elapsed > LATE_LIMIT_SECS:
                self.log.warning("BridgePersist/%s: the decision ran %.3f s after Boot, past the "
                                 "%.3f s limit; dropping the snapshot unwritten, so the avatar "
                                 "boots from defaults.", name, elapsed, LATE_LIMIT_SECS)
                return
            snapshot = at_boot.checkpoint
            calibration = not at_boot.ids
            ok = True
            for addr, v in snapshot.items():
                ok = self.bridge.osc.send(addr, v) and ok
            if not ok:
                self.log.warning("BridgePersist/%s: a payload write was dropped; withholding "
                                 "Restore=1, so the avatar boots from defaults.", name)
                return
            self.log.info("BridgePersist/%s: restoring %d value(s) %s.", name, len(snapshot),
                          "across a calibration reload" if calibration
                          else "from the outgoing avatar")
            ns.token = token
            self._arm_locked(ns, WRITE_SETTLE_SECS, self._write_restored, calibration)

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
            if self._upright_since_change:
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
            self.log.warning("BridgePersist/%s: no Upright within %.0f s of Restore=3, so no "
                             "accept was seen; not releasing, and the avatar ends its hold at "
                             "its own timeout.", name, RELEASE_LIMIT_SECS)

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
        # Payload and Announce; /avatar/change is never here.
        for addr in ns.live:
            self.bridge.osc.forget(addr)
        self.bridge.osc.forget(ns.addr(ANNOUNCE))


_MISSING = object()


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
