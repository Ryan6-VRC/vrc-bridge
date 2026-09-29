"""Bridge persistence: carry an avatar-published state across one avatar swap.

An avatar publishes state under `/avatar/parameters/BridgePersist/<Name>/`, one namespace per
composition, and this mapping writes it back into the next avatar when that avatar carries the
same namespace with the same identity. It holds no manifest and enumerates nothing: the
namespace is the whole contract, and only names that arrive on the wire are ever known.

Four direct children of a namespace are reserved; every other address under it, at any depth, is
**payload**:

* `Id` -- the per-prefab identity, carried as the declared default and so never on the wire. 0
  means persistence is off. Ignored here if it ever arrives.
* `Announce` (int, avatar -> bridge) -- a driver copy of `Id`, written at boot a frame before `Boot`.
* `Boot` (float, avatar -> bridge) -- a driver `random`, once per animator load. Random so that a
  re-wear of the same build still gets past the change filter.
* `Restore` (int, both ways) -- the handshake. Rests at 0.

**The handshake.** Bridge writes 1; the avatar stops measuring, resets its payload to declared
defaults and answers 2; the bridge writes every snapshot value, waits `WRITE_SETTLE_SECS`, and
writes 3; the avatar reads the payload, places the prop and answers 0. Each side acts on the value
*becoming* what it waits for, so a repeated delivery does nothing. The client echoes every inbound
write back to us, so our own 1 and 3 arrive here too; only 2 and 0 advance anything. Each wait for
the avatar is bounded by `ACK_WAIT_SECS`, and a timeout abandons the handshake and drops its
snapshot -- never a retry. Payload the snapshot does not name stays at the default the avatar reset
it to, which is what makes a snapshot complete without holding values that never changed.

**What is kept per namespace, and when a snapshot is valid.** The live payload values, each as the
Python value python-osc parsed, so it is replayed with the wire type it arrived with (an int sent
to a declared float writes garbage, and a bool must stay a bool); the last `Announce`; a
checkpoint; and the distinct avatar ids announced since the namespace last booted.

* Every `/avatar/change`, echo or not, re-takes the checkpoint from the live values and the last
  `Announce`, and adds the id to the list unless it is the id worn when the namespace booted.
  Nothing from an incoming avatar arrives before its announcement at apply, so the last checkpoint
  before a `Boot` is the outgoing avatar's final state -- including after an OSC swap, whose
  request-time echo is followed by a second announcement at apply.
* `Boot` makes the checkpoint the snapshot, valid only when the namespace is baselined, exactly one
  id was announced since its last boot, the checkpoint's `Announce` equals the current one, and
  that value is not 0. A valid snapshot starts the handshake. Valid or not, the live values,
  checkpoint and list are then emptied, the last announced id is recorded as the one worn at boot,
  and the namespace is baselined.
* An empty list at `Boot` is a reload of the avatar already worn -- a world join, a rejoin and Reset
  Avatar are one event on the wire -- and restores nothing. Two or more ids is not one swap
  (A -> B -> A, or a failed OSC swap before a real one) and restores nothing.
* **Baselined** means this bridge has seen the namespace boot. A bridge started after the avatar
  loaded is not, and restores nothing until the next boot. The contract also allows baselining by
  an OSCQuery read of the namespace subtree; that is not built, because `OSCManager.fetch` reads a
  single parameter node and answers FETCH_MALFORMED for a container.

**Two jobs the change filter would otherwise undo.** `_update_cache_and_fire` suppresses a value
equal to the last one seen, and its cache outlives every avatar. So at `Boot` the namespace's known
addresses are `forget()`-ed: otherwise the new avatar's first value for a name, or our own restored
value echoed back, is dropped whenever it equals what the outgoing avatar last sent, and the next
swap's snapshot silently lacks it. And `Restore` is forgotten before each 1 is written, so an
avatar's 2 cannot be eaten by a 2 left cached from an earlier handshake that never completed.

**Which thread waits.** No handler blocks. The settle wait buys ordering (step 3 in a later client
frame than the payload writes, since the client applies only the latest value per parameter per
frame) and consumes no result, so by `docs/design.md`'s rule it does not belong on the datagram
thread; the tick is where that rule sends it, but this mapping's door is an embedder registering it
beside `bridge.start()`, and nothing ticks there. So the settle wait and both ack deadlines run on
`threading.Timer` threads, and a per-handshake token makes a timer that lost the race a no-op.

**Reordering.** A payload handler stores the value the manager's cache now holds rather than the one
it was handed: two datagrams for one address can reach this mapping in reverse arrival order
(`docs/design.md` §Inbound delivery semantics), and the cache is last-arrival-wins under its lock.
`Restore` is the exception and uses the delivered value, because a step is an edge, not a level --
reading the cache there could replace the avatar's 2 with our own echoed 1 arriving just after it.

**`enabled` belongs to the router.** Observation is ungated, so a mapping switched off and on again
never restores from state it failed to watch; only starting a handshake checks `enabled`.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from vrbridge import VRBridge
from vrbridge.mappings.mapping_base import Mapping

# ------------------------------ Config ------------------------------------

# The namespace root and the reserved names are the wire contract with the avatar side, not
# settings -- a typo here is a diff rather than a silent runtime miss (settings.py's header rule).
NAMESPACE_ROOT = "/avatar/parameters/BridgePersist/"
NAMESPACE_PATTERN = NAMESPACE_ROOT + "*"
AVATAR_CHANGE_ADDR = "/avatar/change"

ID, ANNOUNCE, BOOT, RESTORE = "Id", "Announce", "Boot", "Restore"

# Values of `Restore`. The bridge writes REQUEST and WRITTEN; the avatar writes READY and REST.
REST, REQUEST, READY, WRITTEN = 0, 1, 2, 3

#: How long the bridge waits for the avatar's READY after REQUEST, and again for REST after
#: WRITTEN. A contract constant shared with the avatar side's clip lengths, not a feel value.
ACK_WAIT_SECS = 3.0

#: Between the last payload write and WRITTEN. The client applies the latest value per parameter
#: per frame, so this is what puts WRITTEN in a later frame than the payload. Contract, not feel.
WRITE_SETTLE_SECS = 0.05

# Handshake state that is neither "idle" nor a Restore value awaited.
_SETTLING = "settling"


@dataclass
class _Namespace:
    name: str
    live: Dict[str, Any] = field(default_factory=dict)
    announce: Any = None
    checkpoint: Optional[Dict[str, Any]] = None
    checkpoint_announce: Any = None
    ids: List[str] = field(default_factory=list)
    worn_at_boot: Optional[str] = None
    baselined: bool = False
    last_boot: Any = None
    # The handshake in flight, if any. `awaiting` is READY, _SETTLING, REST, or None when idle.
    snapshot: Optional[Dict[str, Any]] = None
    awaiting: Any = None
    token: int = 0
    timer: Optional[threading.Timer] = None

    @property
    def restore_addr(self) -> str:
        return f"{NAMESPACE_ROOT}{self.name}/{RESTORE}"


def _split(address: str) -> Optional[tuple[str, str]]:
    """`/avatar/parameters/BridgePersist/<Name>/<rest>` -> (Name, rest), else None."""
    if not address.startswith(NAMESPACE_ROOT):
        return None
    name, sep, rest = address[len(NAMESPACE_ROOT):].partition("/")
    if not name or not sep or not rest:
        return None
    return name, rest


def _is_int(value) -> bool:
    # bool is an int subclass; a T on an int address is a mis-authored parameter, not a 1.
    return isinstance(value, int) and not isinstance(value, bool)


# ----------------------------- Mapping ------------------------------------

class BridgePersistMapping(Mapping):
    """Restores `BridgePersist/<Name>/` namespaces across one avatar swap."""
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
        # fetch) so a handshake's state is set before the write whose answer it awaits, and
        # across forget(), which nests _lock -> _cache_lock; nothing takes them the other way,
        # because _update_cache_and_fire releases _cache_lock before firing a listener.
        self._lock = threading.Lock()
        self._ns: Dict[str, _Namespace] = {}
        # The last id announced on /avatar/change, echo or not: what a namespace records as
        # worn when it boots, including one first seen at that boot.
        self._last_announced: Optional[str] = None
        self._tokens = 0

    # ---- lifecycle -------------------------------------------------------

    def _attach(self) -> None:
        self.bridge.on_osc(AVATAR_CHANGE_ADDR, self._on_avatar_change)
        self.bridge.on_osc_pattern(NAMESPACE_PATTERN, self._on_namespace)

    # ---- events ----------------------------------------------------------

    def _on_avatar_change(self, ctx, address: str, value) -> None:
        with self._lock:
            self._last_announced = value
            for ns in self._ns.values():
                ns.checkpoint = dict(ns.live)
                ns.checkpoint_announce = ns.announce
                if value in ns.ids:
                    continue
                if value != ns.worn_at_boot or self._reload_as_swap:
                    ns.ids.append(value)

    def _on_namespace(self, ctx, address: str, value) -> None:
        parsed = _split(address)
        if parsed is None:
            return
        name, rest = parsed
        if rest == ID:
            return
        if rest == RESTORE:
            self._on_restore(name, value)
            return
        if rest not in (ANNOUNCE, BOOT):
            # Payload: take the cache's value, which is the latest arrival even when this
            # listener fires after a newer one for the same address (module docstring).
            value = ctx.get(address, value)
        with self._lock:
            ns = self._ns.get(name)
            if ns is None:
                ns = self._ns[name] = _Namespace(name)
            if rest == ANNOUNCE:
                ns.announce = value
            elif rest == BOOT:
                self._on_boot_locked(ns, value)
            else:
                ns.live[address] = value

    def _on_boot_locked(self, ns: _Namespace, value) -> None:
        if value == ns.last_boot:
            # The same draw again is a repeated delivery of one boot, not a second load.
            return
        ns.last_boot = value
        reason = self._invalid_reason(ns)
        snapshot = ns.checkpoint if reason is None else None

        # A boot means the avatar reloaded, so a handshake still in flight was talking to an
        # animator that no longer exists.
        if ns.awaiting is not None:
            self.log.info("BridgePersist/%s booted again mid-restore; abandoning that restore.",
                          ns.name)
            self._clear_handshake_locked(ns)

        for addr in set(ns.live) | set(ns.checkpoint or ()):
            self.bridge.osc.forget(addr)
        ns.live = {}
        ns.checkpoint = None
        ns.checkpoint_announce = None
        ns.ids = []
        ns.worn_at_boot = self._last_announced
        ns.baselined = True

        if snapshot is None:
            self.log.info("BridgePersist/%s booted; nothing to restore: %s.", ns.name, reason)
            return
        if not self.enabled:
            self.log.info("BridgePersist/%s booted with a valid snapshot, but the mapping is "
                          "disabled; not restoring.", ns.name)
            return
        self._tokens += 1
        ns.token = self._tokens
        ns.snapshot = snapshot
        ns.awaiting = READY
        self.bridge.osc.forget(ns.restore_addr)
        if not self.bridge.osc.send(ns.restore_addr, REQUEST):
            self.log.warning("BridgePersist/%s: could not write Restore=1 (no OSC target); "
                             "dropping the snapshot.", ns.name)
            self._clear_handshake_locked(ns)
            return
        self.log.info("BridgePersist/%s: restoring %d value(s) from the outgoing avatar.",
                      ns.name, len(snapshot))
        self._arm_locked(ns, ACK_WAIT_SECS, self._expire, READY)

    def _invalid_reason(self, ns: _Namespace) -> Optional[str]:
        if not ns.baselined:
            return ("this bridge has not seen the namespace boot before, so it cannot tell what "
                    "its values belong to (started after the avatar loaded?)")
        if not ns.ids:
            return "no avatar change since its last boot, so this is a reload of the worn avatar"
        if len(ns.ids) > 1:
            return (f"{len(ns.ids)} avatars were announced since its last boot "
                    f"({', '.join(map(str, ns.ids))}), which is not a single swap")
        if ns.checkpoint_announce != ns.announce:
            return (f"Announce changed from {ns.checkpoint_announce!r} to {ns.announce!r}, so "
                    f"the incoming prefab is not the outgoing one")
        if not _is_int(ns.announce) or ns.announce == 0:
            return f"Announce is {ns.announce!r}, and only a non-zero int enables persistence"
        return None

    def _on_restore(self, name: str, value) -> None:
        if not _is_int(value):
            self.log.warning("BridgePersist/%s/Restore is %r, which is not an int; ignored.",
                             name, value)
            return
        with self._lock:
            ns = self._ns.get(name)
            if ns is None:
                return
            if value == READY and ns.awaiting == READY:
                self._cancel_timer_locked(ns)
                # All or nothing: a write that fails means the avatar would place from a
                # partial payload, so WRITTEN is never sent after one. The avatar's own wait
                # then expires and it resets to defaults.
                ok = True
                for addr, v in ns.snapshot.items():
                    ok = self.bridge.osc.send(addr, v) and ok
                if not ok:
                    self.log.warning("BridgePersist/%s: a payload write was dropped; "
                                     "abandoning the restore.", name)
                    self._clear_handshake_locked(ns)
                    return
                ns.awaiting = _SETTLING
                self._arm_locked(ns, WRITE_SETTLE_SECS, self._after_settle, _SETTLING)
            elif value == REST and ns.awaiting == REST:
                self.log.info("BridgePersist/%s restored.", name)
                self._clear_handshake_locked(ns)
            # Anything else is our own echoed 1 or 3, a repeat, or a value no step awaits.

    # ---- timers ----------------------------------------------------------

    def _after_settle(self, name: str, token: int, step) -> None:
        with self._lock:
            ns = self._ns.get(name)
            if ns is None or ns.token != token or ns.awaiting != step:
                return
            ns.awaiting = REST
            if not self.bridge.osc.send(ns.restore_addr, WRITTEN):
                self.log.warning("BridgePersist/%s: could not write Restore=3; abandoning the "
                                 "restore.", name)
                self._clear_handshake_locked(ns)
                return
            self._arm_locked(ns, ACK_WAIT_SECS, self._expire, REST)

    def _expire(self, name: str, token: int, step) -> None:
        with self._lock:
            ns = self._ns.get(name)
            if ns is None or ns.token != token or ns.awaiting != step:
                return
            self.log.warning("BridgePersist/%s: no Restore=%d from the avatar within %.1f s; "
                             "abandoning the restore and dropping its snapshot.",
                             name, step, ACK_WAIT_SECS)
            self._clear_handshake_locked(ns)

    def _arm_locked(self, ns: _Namespace, delay: float, fn, step) -> None:
        self._cancel_timer_locked(ns)
        t = threading.Timer(delay, fn, args=(ns.name, ns.token, step))
        t.daemon = True
        t.name = f"BridgePersist-{ns.name}"
        ns.timer = t
        t.start()

    def _cancel_timer_locked(self, ns: _Namespace) -> None:
        if ns.timer is not None:
            ns.timer.cancel()
            ns.timer = None

    def _clear_handshake_locked(self, ns: _Namespace) -> None:
        self._cancel_timer_locked(ns)
        ns.snapshot = None
        ns.awaiting = None
