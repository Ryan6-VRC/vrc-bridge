"""Wardrobe mapping: a wardrobe button on the worn avatar -> /avatar/change.

The wearer presses a control on their own expression menu and the worn avatar changes.
Two avatar parameters carry it, both declared by the `osc-wardrobe` vrc-patterns entry:

* `OscWardrobe/Slot` -- the selector. A menu Button sets it to 1..8 and it returns to 0.
* `OscWardrobe/Manifest` -- the marker. Its *default value* is a manifest id, which is how
  the bridge learns which slot table this avatar's menu means. Read over OSCQuery rather
  than off the wire, because a value that never changes is never emitted.

**This mapping requires a momentary source, which is the opposite of osc_muteproxy.**
It acts only on a transition *to* a non-zero slot, so a Button -- held a minimum 0.2 s by
the SDK, then released to 0 -- gives exactly one swap per press. The release edge is not
incidental: it is what makes pressing the same slot twice two events rather than one.

**`_active` is the arm state; `enabled` belongs to the router.** This mapping never writes
its own `enabled`. A wardrobe with no adopted manifest declines a press and says so; a
wardrobe a router disabled stays disabled. Conflating the two let the ungated avatar-change
handler re-enable a mapping its router had deliberately switched off.

**Two things this deliberately does not do.** It does not verify that a swap happened: an
echo watchdog would fire on the most ordinary press, because the wearer's current avatar is
normally one of the eight slots and pressing that slot legitimately produces no echo. VRChat
accepts only ids in the player's favorites, recents, uploads or purchases, and what it does
with an ineligible one is **unmeasured** -- do not build a detector on an assumption of
silence. And it never enumerates your avatars: the manifest is authoritative about what is
swappable, and `design.md` descopes discovery.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional

from vrbridge import VRBridge
from vrbridge.mappings.mapping_base import Mapping
from vrbridge.osc_manager import (FETCH_NO_PEER, FETCH_NOT_FOUND, FETCH_OK,
                                  FETCH_TRANSPORT, FetchResult)
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

#: Why a marker read happens, strongest first. An avatar change demands the careful path
#: (the previous avatar's marker may still be served); a target selection cannot be
#: confused by a previous avatar, because none was worn under this target.
WHY_AVATAR_CHANGE = "avatar-change"
WHY_TARGET = "target-selected"
_WHY_RANK = {WHY_TARGET: 1, WHY_AVATAR_CHANGE: 2}


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

        # --- state shared across threads ---------------------------------------
        # Touched from the OSC datagram threads (thread-per-datagram), zeroconf's dispatch
        # thread, and the marker worker. One lock covers all of it and is never held across
        # a fetch -- the discipline _consider_service already follows.
        self._lock = threading.RLock()
        self._active: Optional[Manifest] = None
        self._worn_avatar_id: Optional[str] = None
        # Bumped on every distinct transition. A read job carries the generation it began
        # under and refuses to adopt if the world moved on; without it, a scroll through
        # two avatars inside one read window adopts the wrong table for the one now worn.
        self._generation = 0
        self._pending: Optional[str] = None
        self._no_peer_logged = False

        self._wake = threading.Event()
        # Set whenever no marker read is running or pending. A caller that needs to know the
        # wardrobe has finished settling has nothing else to wait on -- every other signal
        # (`_pending`, `_wake`) clears the moment the worker *picks up* a job rather than
        # when it finishes, so polling those reports idle through the whole read.
        self._idle = threading.Event()
        self._idle.set()
        self._stop = False
        self._thread: Optional[threading.Thread] = None

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

        # UNGATED, and that is load-bearing. This handler is the only trigger that re-reads
        # the marker, so gating it would make a single failed read terminal until the
        # process restarted -- and because a *successful* swap is what triggers the read,
        # the gated version would fail precisely on working. It only ever invalidates and
        # schedules; it never enables anything, so an ungated handler cannot resurrect a
        # mapping its router switched off.
        self.bridge.on_osc(AVATAR_CHANGE_ADDR, self._on_avatar_change)

        # Same reasoning; also runs on zeroconf's dispatch thread, so it only schedules.
        self.bridge.on_target_selected(self._on_target_selected)

    def close(self) -> None:
        """Stop the marker worker. Idempotent; safe to call on a never-started mapping."""
        with self._lock:
            self._stop = True
        self._wake.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)

    # ---- events ----------------------------------------------------------

    def _on_slot(self, ctx, address: str, value) -> None:
        try:
            slot = int(value)
        except (TypeError, ValueError):
            self.log.warning("Wardrobe slot %s is %r, which is not a whole number; ignored.",
                             address, value)
            return
        if slot == REST_SLOT:
            return

        with self._lock:
            active = self._active
            worn = self._worn_avatar_id
            gen = self._generation

        if active is None:
            self.log.warning(
                "Wardrobe slot %d pressed, but no manifest is active for the worn avatar "
                "(marker %s unread or unknown); ignoring the press.", slot, MARKER_ADDR)
            return

        row = active.avatar_for(slot)
        if row is None:
            # Gaps are legal and the menu always ships eight buttons, so a pressable slot
            # with no row is an ordinary authoring state. Warn and leave the wardrobe
            # working -- taking it down over one unused button would be far worse.
            self.log.warning(
                "Wardrobe slot %d has no entry in manifest %d (%s); nothing to swap to.",
                slot, active.id, active.source)
            return

        if worn is not None and row.avatar_id == worn:
            # A courtesy, not a correctness mechanism: the client no-ops a swap to the
            # avatar already worn. Skipping it keeps the log honest about what we asked for.
            self.log.info("Wardrobe slot %d is the avatar already worn; no send.", slot)
            return

        # Re-check under the lock immediately before sending. Selecting the row and sending
        # are separate steps, and an /avatar/change on another datagram thread can
        # invalidate in between -- which would put this send against the outgoing avatar's
        # table.
        with self._lock:
            if self._generation != gen or self._active is not active:
                self.log.info("Wardrobe slot %d dropped: the worn avatar changed while the "
                              "press was being handled.", slot)
                return

        label = f" ({row.label})" if row.label else ""
        self.log.info("Wardrobe slot %d -> %s%s", slot, row.avatar_id, label)
        if ctx.send(AVATAR_CHANGE_ADDR, row.avatar_id):
            # The watch layer suppresses a value equal to the last one seen, and a swap can
            # eat the Button's release-to-0 (the outgoing avatar stops emitting first). That
            # would leave the cache holding this slot, so pressing the same button again
            # would be filtered before it ever reached us -- a dead button until a different
            # slot is pressed. Forgetting the value makes the repeat a change again.
            self.bridge.osc.forget(SLOT_ADDR)

    def _on_avatar_change(self, ctx, address: str, value) -> None:
        """The worn avatar changed: the active manifest no longer describes it."""
        worn = value if isinstance(value, str) else None
        with self._lock:
            self._worn_avatar_id = worn
            # Invalidate first. Until a validated read lands, a press does nothing rather
            # than indexing the previous avatar's table and swapping somewhere unasked.
            self._active = None
        self._schedule(WHY_AVATAR_CHANGE)

    def _on_target_selected(self, ctx, target) -> None:
        """VRChat was discovered, or came back on a new port.

        Without this the mapping would wait for the wearer's first avatar change, because a
        marker's value never changes and so is never emitted -- the cache would stay empty
        for the whole session. It invalidates too: a different client is a different worn
        avatar, and keeping the old one's manifest live would index a stranger's table.
        """
        with self._lock:
            self._active = None
            self._worn_avatar_id = None
        self._schedule(WHY_TARGET)

    # ---- the marker worker ----------------------------------------------

    def _schedule(self, why: str) -> None:
        """Request a marker read, keeping the strongest reason and never losing a request.

        Coalescing is by *generation*, not by "one at a time": a request that arrives while
        a read is in flight supersedes it rather than being dropped. Dropping it was a
        defect -- two avatar changes inside one read window left the finishing read free to
        adopt a manifest for an avatar no longer worn.
        """
        with self._lock:
            self._generation += 1
            self._idle.clear()
            if self._pending is None or _WHY_RANK[why] > _WHY_RANK[self._pending]:
                self._pending = why
            if self._thread is None or not self._thread.is_alive():
                self._stop = False
                self._thread = threading.Thread(
                    target=self._run, name="OscWardrobeMarker", daemon=True)
                self._thread.start()
        self._wake.set()

    def _run(self) -> None:
        while True:
            self._wake.wait()
            with self._lock:
                if self._stop:
                    return
                self._wake.clear()
                why = self._pending
                self._pending = None
                gen = self._generation
            if why is None:
                with self._lock:
                    if self._pending is None:
                        self._idle.set()
                continue
            try:
                self._read_marker(why, gen)
            except Exception:
                # Off any caller's thread, so nothing else would surface this.
                self.log.exception("Wardrobe marker read failed (%s)", why)
            finally:
                with self._lock:
                    # Only idle if nothing arrived while we were reading; a superseding
                    # request has already cleared this and is about to be served.
                    if self._pending is None:
                        self._idle.set()

    def _superseded(self, gen: int) -> bool:
        with self._lock:
            return self._generation != gen or self._stop

    def _read_marker(self, why: str, gen: int) -> None:
        """Read the marker and adopt the manifest it names, or leave the mapping unarmed.

        After an avatar change a single read is not enough. The client's tree 404s an
        address no worn avatar declares, but a wardrobe's whole purpose is that *several*
        avatars declare this one at different values -- so a read taken during the
        transition can return the outgoing avatar's id and be indistinguishable from a
        correct answer.

        Two discriminators, in order of strength. **An observed 404 proves teardown**, so
        the first value after one is necessarily the new avatar's and is adopted at once.
        Failing that, two reads a gap apart that agree are accepted -- which establishes
        that the value is *stable*, not that it is *current*, and is therefore a timing bet
        rather than a proof. Which one was used is logged, because that is what a live
        measurement needs to know.

        On target selection there is no transition to be confused by, so one read settles
        it: nothing was worn before that could still be served.
        """
        tune = self.tuning

        if self._pinned_manifest_id is not None:
            # Named outright, so there is nothing to read and no staleness to defend
            # against -- design.md's rule that naming a peer takes the question away.
            self._adopt(self._pinned_manifest_id, gen, "pinned")
            return

        need_stable = (why == WHY_AVATAR_CHANGE)
        if need_stable and tune.settle_delay_secs:
            time.sleep(tune.settle_delay_secs)

        previous: Optional[int] = None
        saw_teardown = False
        transport_failures = 0

        for attempt in range(tune.max_reads):
            if attempt:
                time.sleep(tune.stable_gap_secs)
            if self._superseded(gen):
                return

            result: FetchResult = self.bridge.osc.fetch(
                MARKER_ADDR, timeout=tune.fetch_timeout_secs)

            if result.reason == FETCH_NOT_FOUND:
                # NOT the end of the schedule. During a swap the new avatar's node is not
                # published yet, so the first read of a perfectly good wardrobe 404s --
                # returning here left a wardrobed avatar permanently unarmed, recoverable
                # only through the very menu this feature replaces. A 404 is also the
                # teardown signal that makes the next value trustworthy.
                saw_teardown = True
                previous = None
                continue
            if result.reason == FETCH_TRANSPORT:
                transport_failures += 1
                previous = None      # a gap in the series breaks the stability claim
                continue
            if result.reason == FETCH_NO_PEER:
                # A pinned target advertises nothing and serves no tree. Say it once rather
                # than on every avatar change, and name the way out.
                if not self._no_peer_logged:
                    self._no_peer_logged = True
                    self.log.warning(
                        "No OSCQuery peer to read %s from -- the send target was pinned, and "
                        "a pinned peer serves no tree. Construct the wardrobe with "
                        "pinned_manifest_id= to name the manifest instead.", MARKER_ADDR)
                return
            if result.reason != FETCH_OK:
                self.log.warning("Cannot read the wardrobe marker: %s (%s)",
                                 result.reason, result.detail)
                return

            try:
                marker = int(result.value)
            except (TypeError, ValueError):
                self.log.warning("Wardrobe marker %s served %r, which is not a whole "
                                 "number; ignoring.", MARKER_ADDR, result.value)
                return

            if not need_stable:
                self._adopt(marker, gen, "first read")
                return
            if saw_teardown:
                self._adopt(marker, gen, "after an observed 404")
                return
            if marker == previous:
                self._adopt(marker, gen, "stable across two reads")
                return
            previous = marker

        if saw_teardown:
            # 404 for the whole schedule: the worn avatar declares no marker, so it carries
            # no wardrobe. Normal on any avatar without the prefab, and not an error.
            self.log.info("No wardrobe marker on the worn avatar (%s 404s); wardrobe idle "
                          "until the next avatar change.", MARKER_ADDR)
        elif transport_failures:
            self.log.warning(
                "Gave up reading the wardrobe marker after %d attempts (%d transport "
                "failures); the wardrobe stays idle until the next avatar change.",
                tune.max_reads, transport_failures)
        else:
            self.log.warning(
                "The wardrobe marker never held the same value across %d reads, so no "
                "manifest was adopted. The worn avatar may still have been loading.",
                tune.max_reads)

    def _adopt(self, marker: int, gen: int, how: str) -> None:
        manifest = self._manifests.get(marker)
        if manifest is None:
            known = ", ".join(str(k) for k in sorted(self._manifests)) or "none"
            self.log.warning(
                "The worn avatar's wardrobe marker is %d, which no loaded manifest claims "
                "(loaded: %s). Give a manifest id %d, or correct the avatar's %s default.",
                marker, known, marker, MARKER_ADDR)
            return
        with self._lock:
            if self._generation != gen:
                # The worn avatar changed while this read was in flight. Committing here
                # would arm the wardrobe with a table for an avatar nobody is wearing.
                self.log.info("Discarding a wardrobe marker read superseded by a later "
                              "avatar change.")
                return
            self._active = manifest
        self.log.info("Wardrobe manifest %d active (%d slot(s), from %s) -- %s.",
                      manifest.id, len(manifest.slots), manifest.source, how)

    def update(self, now: float) -> None:
        return
