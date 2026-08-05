"""Wardrobe mapping: a wardrobe button on the worn avatar -> /avatar/change.

The wearer presses a control on their own expression menu and the worn avatar changes.
Two avatar parameters carry it, both declared by the `osc-wardrobe` vrc-patterns entry:

* `OscWardrobe/Slot` -- the selector. A menu Button sets it to 1..8 and it returns to 0.
* `OscWardrobe/Manifest` -- the marker. Its *default value* is a manifest id, which is how
  the bridge learns which slot table this avatar's menu means. Read over OSCQuery rather
  than off the wire, because a value that never changes is never emitted.

**The marker is read on every press, and never cached.** A press proves that whatever is
emitting it is loaded, so a read taken then describes the avatar the press came from -- which
is the only avatar whose table could be meant. Reading on the avatar *change* instead needs a
settling window, and none can be sized: the client acknowledges a change immediately while a
cold download runs 30-60 s, so a scheduled read polls an avatar that does not exist yet. An
earlier design did exactly that and concluded "no wardrobe here" mid-download.

**Caching that read was the subtler version of the same bug.** The outgoing avatar stays worn
and emitting for the whole download, so a press in that window is genuinely *its* press and
the read genuinely describes *it* -- correct at that instant, and wrong forever after if
kept, because nothing invalidates when the incoming avatar finishes loading: the client emits
no `/avatar/change` on load (measured). The cached table then survives into an avatar it does
not describe and the next press swaps somewhere unasked, silently. An epoch counter does not
help, because the stale read is not racing anything; it is a correct read of the wrong
avatar. Do not reintroduce a schedule, and do not reintroduce a cache.

**This mapping requires a momentary source, which is the opposite of osc_muteproxy.**
It acts only on a transition *to* a non-zero slot. Measured on a live client, a menu Button
holds for 200 ms and then returns to 0 -- matching the SDK's documented floor -- so one press
is exactly one swap, and the release edge is what makes a second press of the same slot a
second event rather than a repeat the change filter drops.

**`_active` is the arm state; `enabled` belongs to the router.** This mapping never writes
its own `enabled`. Conflating them let the ungated avatar-change handler re-enable a mapping
its router had deliberately switched off.

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
                                  FetchResult)
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
        # The last manifest a read produced. **Not a cache** -- it is never consulted to
        # skip a read, only to keep the log from repeating itself. Caching it was a real
        # defect: see the module docstring on the download window.
        self._last_manifest: Optional[Manifest] = None
        # What the last read reported, so an unchanged outcome is not re-logged per press.
        self._reported: Optional[tuple] = None
        # The last slot acted on and when, for REPEAT_GUARD_SECS.
        self._last_slot: Optional[int] = None
        self._last_slot_at: float = 0.0

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

        # UNGATED, and that is load-bearing: this is what drops a manifest that no longer
        # describes the worn avatar. Gated, a router-disabled wardrobe would keep a stale
        # table across every avatar change and act on it the moment it was re-enabled. It
        # only ever invalidates -- it never enables anything, so an ungated handler cannot
        # resurrect a mapping its router switched off.
        self.bridge.on_osc(AVATAR_CHANGE_ADDR, self._on_invalidate)

        # A different client means a different worn avatar. Same reasoning; also runs on
        # zeroconf's dispatch thread, so it must stay cheap -- invalidating is one field.
        self.bridge.on_target_selected(lambda ctx, target: self._on_invalidate(ctx, "", None))

    # ---- events ----------------------------------------------------------

    def _on_invalidate(self, ctx, address: str, value) -> None:
        """The worn avatar (or the client) changed, so the active manifest is stale.

        The echoed avatar id is deliberately not retained: it acknowledges whatever was last
        requested rather than stating what is worn (see the module docstring), so keeping it
        would only invite a caller to trust it.
        """
        with self._lock:
            self._last_manifest = None
            self._reported = None
            # The duplicate-delivery window belongs to one press on one avatar. Clearing it
            # means a press of the same slot on the *new* avatar is never mistaken for the
            # previous avatar's echo.
            self._last_slot = None

    def _on_slot(self, ctx, address: str, value) -> None:
        try:
            slot = int(value)
        except (TypeError, ValueError):
            self.log.warning("Wardrobe slot %s is %r, which is not a whole number; ignored.",
                             address, value)
            return
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
            self._last_slot = slot
            self._last_slot_at = now

        manifest = self._read_manifest()
        if manifest is None:
            # Nothing was sent, so release the guard: the wearer pressing again is the retry
            # for a transport failure, and holding a guard set by an attempt that did nothing
            # would make that retry look like a duplicate and swallow it.
            with self._lock:
                if self._last_slot == slot:
                    self._last_slot = None
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

        label = f" ({row.label})" if row.label else ""
        self.log.info("Wardrobe slot %d -> %s%s", slot, row.avatar_id, label)
        if ctx.send(AVATAR_CHANGE_ADDR, row.avatar_id):
            # The watch layer suppresses a value equal to the last one seen, and a swap can
            # eat the Button's release-to-0 (the outgoing avatar stops emitting first). That
            # would leave the cache holding this slot, so pressing the same button again
            # would be filtered before it ever reached us -- a dead button until a different
            # slot is pressed. Forgetting the value makes the repeat a change again.
            self.bridge.osc.forget(SLOT_ADDR)

    # ---- arming ----------------------------------------------------------

    def _read_manifest(self) -> Optional[Manifest]:
        """Read the worn avatar's marker and return its manifest. **Every press.**

        Not cached, and that is the whole correctness argument. The premise this design
        rests on is that a press proves the avatar sending it is loaded -- but the *outgoing*
        avatar stays worn and emitting for the entire 30-60 s while the incoming one
        downloads, so a press during that window is genuinely the outgoing avatar's, and a
        read then genuinely describes the outgoing avatar. Correct at that instant, and
        wrong forever after if kept: nothing invalidates when the new avatar finishes
        loading, because the client emits no `/avatar/change` on load (measured). A cached
        answer therefore survives into an avatar it does not describe, and the next press
        indexes the wrong table with no diagnostic.

        Re-reading per press makes the answer always as current as the press that asked. It
        costs one loopback GET at human press rates -- about a millisecond, on the OSC
        datagram path where `design.md` sanctions blocking -- and it is less machinery than
        the cache plus the epoch counter that would be needed to make a cache safe.
        """
        if self._pinned_manifest_id is not None:
            # Named outright, so there is nothing to read -- design.md's rule that naming a
            # peer takes the question away rather than entering it as a bid.
            return self._lookup(self._pinned_manifest_id, "named by pinned_manifest_id")

        result: FetchResult = self.bridge.osc.fetch(
            MARKER_ADDR, timeout=self.tuning.fetch_timeout_secs)

        if result.reason == FETCH_NOT_FOUND:
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
        with self._lock:
            self._last_manifest = manifest
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
