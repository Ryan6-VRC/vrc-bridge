"""Wardrobe mapping: a wardrobe button on the worn avatar -> /avatar/change.

The wearer presses a control on their own expression menu and the worn avatar changes.
Two avatar parameters carry it, both declared by the `osc-wardrobe` vrc-patterns entry:

* `OscWardrobe/Slot` -- the selector. A menu Button sets it to 1..8 and it returns to 0.
* `OscWardrobe/Manifest` -- the marker. Its *default value* is a manifest id, which is how
  the bridge learns which slot table this avatar's menu means. Read over OSCQuery rather
  than off the wire, because a value that never changes is never emitted.

**The marker is read on every press, and never cached.** A press can only reach us from a
fully loaded avatar: while an avatar is loading the wearer *is* the placeholder, which declares
no expression parameters and emits no OSC, so no `OscWardrobe/Slot` can arrive from a
half-swapped state. A read taken at a press therefore describes the avatar whose button was
pressed -- which is the only avatar whose table could be meant.

**Reading on the avatar *change* is what cannot work**, and no window can be sized for it: the
client acknowledges a change immediately while a cold download runs 30-60 s, so a scheduled
read interrogates an avatar that does not exist yet. An earlier design did exactly that and
concluded "no wardrobe here" mid-download, leaving a wardrobed avatar unarmed until the next
change. Do not reintroduce a schedule.

**Not caching the read is a simplicity choice, not a bug fix.** A cache would be *correct* only
as long as invalidation is provably complete -- every avatar change reaches `_on_invalidate`,
including one the wearer makes from VRChat's own menu, and including the case where the change
filter suppresses a repeated identical echo. Re-reading costs one loopback GET at human press
rates on a path `design.md` sanctions blocking, and needs none of that argument. Prefer the
version with less to get wrong.

**This mapping requires a momentary source, which is the opposite of osc_muteproxy.**
It acts only on a transition *to* a non-zero slot. Measured on a live client, a menu Button
holds for 200 ms and then returns to 0 -- matching the SDK's documented floor -- so one press
is exactly one swap, and the release edge is what makes a second press of the same slot a
second event rather than a repeat the change filter drops.

**`enabled` belongs to the router, and this mapping never writes it.** The ungated
avatar-change handler below only ever invalidates, so it cannot re-enable a mapping its router
had deliberately switched off -- which is the confusion this note exists to prevent.

**It cannot verify that a swap happened, and that is a property of the channel.** Measured:
VRChat echoes `/avatar/change` carrying the id we sent within 5 ms, does so identically for
an ineligible id and for a malformed one, and never emits again when the avatar really loads.
The echo acknowledges the request and never reports the outcome, so no watchdog built on it
could tell a working swap from a rejected one, and nothing here may treat it as evidence of
what is worn.

**It never enumerates your avatars.** The manifest is authoritative about what is swappable,
and `design.md` descopes discovery.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional

from vrbridge import VRBridge
from vrbridge.mappings.mapping_base import Mapping
from vrbridge.osc_manager import (FETCH_NO_PEER, FETCH_NOT_FOUND, FETCH_OK,
                                  FETCH_PEER_GONE, FetchResult)
from vrbridge.settings import settings
from vrbridge.wardrobe import Manifest

# ------------------------------ Config ------------------------------------

# --- OSC addresses ---
# Not settings: these are the contract with the vrc-patterns entry that declares them,
# and a typo here is a diff rather than a silent runtime miss (settings.py's header rule).
SLOT_ADDR = "/avatar/parameters/OscWardrobe/Slot"
MARKER_ADDR = "/avatar/parameters/OscWardrobe/Manifest"
AVATAR_CHANGE_ADDR = "/avatar/change"

#: The slot value a menu at rest sends. Never swaps -- a Button returns here on release,
#: and an avatar load initialises the parameter here, so acting on it would swap on every
#: avatar load.
REST_SLOT = 0

#: Ignore a repeat of the same slot arriving within this long, as a duplicate delivery
#: rather than a second press.
#:
#: Two facts make this a contract rather than a feel value, so it lives in source. First,
#: `design.md` records that every inbound message is delivered twice; measured live, the two
#: copies of one press arrived **1 ms** apart. Second, the SDK holds a menu Button active for
#: a minimum 200 ms (`menus.md`), measured live at exactly that -- so the wearer's next press
#: of the same slot cannot begin sooner than ~200 ms after this one did. The gap between
#: "duplicate" and "genuine repeat" is therefore two orders of magnitude, and anything in the
#: middle separates them. Raising this past the Button floor would start eating real presses.
#:
#: Not solvable by the change-filter alone: `forget()` below deliberately clears the cached
#: slot so a repeat press is deliverable at all, which is exactly what lets the duplicate
#: through. The two mechanisms answer different failures -- forget restores deliverability,
#: this restores idempotence -- and removing either brings back a dead button or a double swap.
REPEAT_GUARD_SECS = 0.15


# ----------------------------- Mapping ------------------------------------

class WardrobeMapping(Mapping):
    """Drives /avatar/change from the worn avatar's wardrobe menu."""
    name = "osc_wardrobe"

    def __init__(self, bridge: VRBridge,
                 manifests: Optional[Dict[int, Manifest]] = None,
                 *, tuning=None, pinned_manifest_id: Optional[int] = None):
        super().__init__(bridge)
        self.tuning = tuning if tuning is not None else settings().wardrobe
        self.log = bridge.log
        # Injected rather than loaded here so a caller can supply a fixture set, and so a
        # config error surfaces at the caller's construction site instead of inside a
        # mapping's __init__. `load_from_settings` is the convenience path.
        self._manifests: Dict[int, Manifest] = dict(manifests or {})
        # Names the manifest outright, for a peer that serves no OSCQuery at all -- a
        # pinned --osc-port target, the Av3Emulator. There is nothing to read there, so
        # without this such a session can never arm.
        self._pinned_manifest_id = pinned_manifest_id

        # The OSC server is thread-per-datagram, so two presses and an avatar change can be
        # in flight at once. One lock covers this state and is never held across a fetch --
        # the discipline _consider_service already follows.
        # A plain Lock, not an RLock: no path nests it, and the adjacent rule is that it is
        # never held across a fetch. A plain Lock deadlocks loudly the first time someone
        # breaks that; an RLock would quietly permit it.
        self._lock = threading.Lock()
        # What the last read reported, so an unchanged outcome is not re-logged per press.
        self._reported: Optional[tuple] = None
        # The last slot acted on and when, for REPEAT_GUARD_SECS.
        self._last_slot: Optional[int] = None
        self._last_slot_at: float = 0.0
        # Identity for the press that armed the guard. A press blocks on its marker read for up
        # to `fetch_timeout_secs`, an order of magnitude past REPEAT_GUARD_SECS, so a press
        # returning from that read cannot assume the state it left is still its own: the slot
        # and the timestamp describe whoever armed last, which may be a later press. Everything
        # a press does after its read is gated on this token still matching.
        self._press_seq = 0
        self._last_press: Optional[int] = None

    # ---- construction helpers --------------------------------------------

    @classmethod
    def load_from_settings(cls, bridge: VRBridge, *, tuning=None,
                           pinned_manifest_id: Optional[int] = None) -> "WardrobeMapping":
        """Build with the manifests found in the configured directory.

        A missing directory yields none, which is not an error here -- the mapping reports
        that it has nothing to work with when a press arrives, because only then has anyone
        asked it to do something.
        """
        from vrbridge.wardrobe import discover_manifests
        tune = tuning if tuning is not None else settings().wardrobe
        found = discover_manifests(tune.resolved_manifest_dir())
        return cls(bridge, found, tuning=tune, pinned_manifest_id=pinned_manifest_id)

    # ---- lifecycle -------------------------------------------------------

    def _attach(self) -> None:
        # Gated: a router that disabled this mapping must not have it swapping avatars.
        self.bridge.on_osc(SLOT_ADDR, self._gate(self._on_slot))

        # UNGATED, and the reason is narrower than it used to be. There is no cached table to
        # drop -- the marker is read on every press -- so all this resets is the once-per-
        # condition report dedupe, which is what lets a new avatar state its own problem
        # instead of inheriting the previous one's silence. Ungated because a router-disabled
        # wardrobe still passes through avatar changes, and re-enabling it should not begin
        # in stale silence. It only ever clears, so it cannot resurrect a mapping its router
        # switched off.
        #
        # Do not read a caching contract back into this. An earlier version of this comment
        # promised one, and the state behind the promise had already been deleted.
        self.bridge.on_osc(AVATAR_CHANGE_ADDR, self._on_invalidate)

        # A different client means a different worn avatar. Same reasoning; also runs on
        # zeroconf's dispatch thread, so it must stay cheap -- clearing is one field.
        self.bridge.on_target_selected(lambda ctx, target: self._on_invalidate(ctx, "", None))

    # ---- events ----------------------------------------------------------

    def _on_invalidate(self, ctx, address: str, value) -> None:
        """The worn avatar (or the client) changed, so let the next press speak for itself.

        Nothing is invalidated in the caching sense, because nothing is cached: this resets
        the report dedupe so the next press reports its own outcome rather than being
        suppressed as a repeat of the previous avatar's.

        The echoed avatar id is deliberately not retained: it acknowledges whatever was last
        requested rather than stating what is worn (see the module docstring), so keeping it
        would only invite a caller to trust it. The same is true of VRChat's OSCQuery node of
        this name, measured -- it adopts an id no avatar owns, so it reports the request too.
        """
        with self._lock:
            self._reported = None
            # `_last_slot` is deliberately NOT cleared here. This handler is bound to
            # /avatar/change, and the echo of our own swap arrives within 5 ms (measured) --
            # inside the 150 ms duplicate window the swap itself just armed. Clearing it here
            # destroyed the guard from within: once ctx.send's forget() has reopened the
            # change filter, a second copy of the press whose dispatch thread starts late
            # meets an empty guard and swaps again -- the doubled swap REPEAT_GUARD_SECS and
            # `_release_guard`'s token exist to prevent. `design.md` rules cite by path, not SHA.
            # Nothing is lost by keeping it: a genuine same-slot press on the new avatar
            # cannot arrive inside 150 ms, because a Button holds for 200 ms (measured) and
            # the loading wearer is the placeholder, which emits no OSC at all.

    def _on_slot(self, ctx, address: str, value) -> None:
        # Same guard as the marker read below, and for the same reason: bool is an int
        # subclass and int() truncates, so a T or a 1.9 would otherwise become slot 1 and
        # swap the avatar. int() also raises OverflowError on an infinity, which the old
        # (TypeError, ValueError) clause did not catch. Test the type instead of coercing.
        if isinstance(value, bool) or not isinstance(value, int):
            self.log.warning("Wardrobe slot %s is %r, which is not a whole number; ignored.",
                             address, value)
            return
        slot = value
        if slot == REST_SLOT:
            return

        # Reject the second copy of one press before doing anything else. Acting twice is
        # not merely redundant: the client answers the duplicate with a visible "you are
        # already using this avatar" error and then completes the swap, so the wearer sees a
        # failure on every successful press.
        now = time.monotonic()
        with self._lock:
            if (self._last_slot == slot
                    and (now - self._last_slot_at) < REPEAT_GUARD_SECS):
                self.log.debug(
                    "Ignoring a repeat of wardrobe slot %d %.0f ms after the first: inbound "
                    "is delivered twice, and a real second press cannot be this fast.",
                    slot, (now - self._last_slot_at) * 1000)
                return
            self._press_seq += 1
            token = self._press_seq
            self._last_slot = slot
            self._last_slot_at = now
            self._last_press = token

        manifest = self._read_manifest()
        if manifest is None:
            # Nothing was sent, so release the guard: the wearer pressing again is the retry
            # for a transport failure, and holding a guard set by an attempt that did nothing
            # would make that retry look like a duplicate and swallow it.
            self._release_guard(token)
            return

        row = manifest.avatar_for(slot)
        if row is None:
            # Gaps are legal and the menu always ships eight buttons, so a pressable slot
            # with no row is an ordinary authoring state. Warn and leave the wardrobe
            # working -- taking it down over one unused button would be far worse.
            self.log.warning(
                "Wardrobe slot %d has no entry in manifest %d (%s); nothing to swap to.",
                slot, manifest.id, manifest.source)
            return

        # There is deliberately no "already wearing this one, skip it" check: the only thing
        # that could say what is worn is the /avatar/change echo, and it is an
        # acknowledgement of our own request. Suppressing on it would mean that after a swap
        # the client declined, the wearer's retry of that very slot is swallowed in silence.

        # A marker read blocks, so the wearer can press a different slot while this one is
        # still reading -- and that press is the live one: it arrived later, and its own swap
        # has already gone out. Sending ours now would land the wearer on the avatar they
        # pressed *first*, which is what they will read as the wardrobe picking at random.
        # Abandon instead. Last press wins, which is the only rule a menu can express.
        #
        # The check and the send are one critical section. Testing the token, releasing, and
        # then sending only narrows the race from the length of a read to the length of a log
        # call: a later press can still arm, read and send inside that gap, and ours would
        # land after it. Holding across the send is what actually orders them.
        #
        # `ctx.send` is a UDP sendto and is safe to hold this across; the adjacent rule is that
        # the lock is never held across a *fetch*, which this is not. `forget()` is called after
        # releasing, so the only nesting would be _lock -> _cache_lock, and nothing acquires
        # them the other way round: `_update_cache_and_fire` releases `_cache_lock` before
        # firing a listener, and `_consider_service` fires target listeners outside its own.
        label = f" ({row.label})" if row.label else ""
        with self._lock:
            still_ours = self._last_press == token
            if still_ours:
                self.log.info("Wardrobe slot %d -> %s%s", slot, row.avatar_id, label)
                sent = ctx.send(AVATAR_CHANGE_ADDR, row.avatar_id)
        if not still_ours:
            self.log.info(
                "Wardrobe slot %d was superseded by a later press while its marker was being "
                "read; not swapping to %s.", slot, row.avatar_id)
            return

        if sent:
            # The watch layer suppresses a value equal to the last one seen, and a swap can
            # eat the Button's release-to-0 (the outgoing avatar stops emitting first). That
            # would leave the cache holding this slot, so pressing the same button again
            # would be filtered before it ever reached us -- a dead button until a different
            # slot is pressed. Forgetting the value makes the repeat a change again.
            self.bridge.osc.forget(SLOT_ADDR)
        else:
            # Nothing reached the wire, so release the guard for the same reason an unreadable
            # manifest does. Deliberately no matching forget(): forget answers a *successful*
            # swap eating the Button's release-to-0, because the outgoing avatar stops emitting
            # first. Here the avatar did not change, so the release arrives normally and the
            # change filter needs no help. `send` has already logged the drop at WARNING.
            self._release_guard(token)

    def _release_guard(self, token: int) -> None:
        """Disarm the duplicate guard, but only if this press is still the one holding it.

        Matching on the slot alone is not enough. This runs after a read that can block for
        `fetch_timeout_secs`, an order of magnitude past REPEAT_GUARD_SECS, so by the time a
        stalled press arrives here a *later* press of the same slot may have armed the guard
        and swapped. Clearing that one lets its duplicate copy through as a second swap -- one
        press, two swaps, and the client answers the second with the "you are already using
        this avatar" error the guard exists to prevent the wearer ever seeing.
        """
        with self._lock:
            if self._last_press == token:
                self._last_slot = None
                self._last_press = None

    # ---- arming ----------------------------------------------------------

    def _read_manifest(self) -> Optional[Manifest]:
        """Read the worn avatar's marker and return its manifest. **Every press.**

        A press can only come from a loaded avatar -- the loading placeholder declares no
        parameters and emits no OSC -- so this read always describes the avatar whose button
        was pressed. Not cached because a cache would be correct only while invalidation is
        provably complete, and one loopback GET per press buys not needing that proof.
        """
        if self._pinned_manifest_id is not None:
            # Named outright, so there is nothing to read -- design.md's rule that naming a
            # peer takes the question away rather than entering it as a bid.
            return self._lookup(self._pinned_manifest_id, "named by pinned_manifest_id")

        result: FetchResult = self.bridge.osc.fetch(
            MARKER_ADDR, timeout=self.tuning.fetch_timeout_secs)

        if result.reason == FETCH_NOT_FOUND:
            if result.peer is not None and not result.peer.is_vrchat:
                # The 404 is about the peer, not the avatar. Ranking fills an empty target
                # slot with the best peer advertising, so before a VRChat client is
                # discovered that slot can hold VRCFaceTracking or VRCOSC -- both advertise
                # themselves -- and their trees carry no avatar parameters at all. Blaming
                # the worn avatar here sends the wearer to check a marker that is set
                # correctly. The hedged wording is deliberate: PeerIdentity.is_vrchat is
                # what the advertisement claims, so this says what the peer identifies
                # itself as and never what it is -- which is also why the remedy names the
                # other way this can read: an unrecognised real client scores as a stranger
                # too, and "wait for discovery" is useless advice to someone already running
                # one.
                self._report(("foreign-peer", result.peer.name), "warning",
                             "%s was read from %s, which does not identify itself as "
                             "VRChat, so its 404 says nothing about the worn avatar; press "
                             "again once a VRChat client is discovered and takes the "
                             "target. If one is already running, its service is not being "
                             "recognised -- docs/design.md, Target selection.",
                             MARKER_ADDR, result.peer.name)
                return None
            # No marker on whatever is worn right now. Normal on any avatar without the
            # prefab, and normal *transiently* in the gap between avatars -- which is why it
            # only suppresses the log line and never suppresses the next read.
            self._report(("no-marker",), "info",
                         "The worn avatar declares no wardrobe marker (%s 404s), so its "
                         "menu is not a wardrobe.", MARKER_ADDR)
            return None
        if result.reason == FETCH_NO_PEER:
            # Three states hide behind a missing peer and they have different fixes; only a
            # pin is answered by naming the manifest.
            if self.bridge.osc.target_is_pinned:
                self._report(("no-peer", "pinned"), "warning",
                             "The send target was pinned, so no OSCQuery tree exists to read "
                             "%s from. Construct the wardrobe with pinned_manifest_id= to "
                             "name the manifest instead.", MARKER_ADDR)
            else:
                self._report(("no-peer", "undiscovered"), "warning",
                             "No OSCQuery peer discovered yet, so %s cannot be read; press "
                             "again once VRChat has been found.", MARKER_ADDR)
            return None
        if result.reason == FETCH_PEER_GONE:
            # Distinct from never having found one: waiting on discovery to finish is the
            # wrong advice here, because it finished and the client then went away.
            self._report(("peer-gone",), "warning",
                         "The client we were reading %s from withdrew; press again once "
                         "VRChat is running and rediscovered.", MARKER_ADDR)
            return None
        if result.reason != FETCH_OK:
            self._report(("fetch", result.reason), "warning",
                         "Cannot read the wardrobe marker, so this press does nothing: "
                         "%s (%s)", result.reason, result.detail)
            return None

        value = result.value
        # bool is an int subclass and int() truncates, so a T or a 1.9 would otherwise
        # become slot 1. The guard claims to reject a non-whole number; make it true.
        if isinstance(value, bool) or not isinstance(value, int):
            self._report(("bad-marker", repr(value)), "warning",
                         "Wardrobe marker %s served %r, which is not a whole number.",
                         MARKER_ADDR, value)
            return None
        return self._lookup(value, "read from the worn avatar")

    def _lookup(self, marker: int, how: str) -> Optional[Manifest]:
        manifest = self._manifests.get(marker)
        if manifest is None:
            known = ", ".join(str(k) for k in sorted(self._manifests)) or "none"
            self._report(("unknown", marker), "warning",
                         "The worn avatar's wardrobe marker is %d, which no loaded manifest "
                         "claims (loaded: %s). Give a manifest id %d, or correct the "
                         "avatar's %s default.", marker, known, marker, MARKER_ADDR)
            return None
        self._report(("active", manifest.id), "info",
                     "Wardrobe manifest %d active (%d slot(s), from %s) -- %s.",
                     manifest.id, len(manifest.slots), manifest.source, how)
        return manifest

    def _report(self, key: tuple, level: str, msg: str, *args) -> None:
        """Log an outcome once, not once per press.

        Reading every press means the same answer arrives repeatedly; saying it repeatedly
        would bury everything else. Suppressing the *message* is safe in a way that
        suppressing the *read* was not.
        """
        with self._lock:
            if self._reported == key:
                return
            self._reported = key
        getattr(self.log, level)(msg, *args)

    def update(self, now: float) -> None:
        return
