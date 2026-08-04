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
incidental: it is what makes pressing the same slot twice two events rather than one, which
is how a retry works after a swap that did not take.

**Two things this deliberately does not do.**

It does not verify that a swap happened. VRChat only accepts ids in your favorites, recents,
uploads or purchases, and declines the rest in silence -- so an echo watchdog is the obvious
next step and is deliberately absent: the wearer's own worn avatar is usually one of the
eight slots, and pressing that slot legitimately produces no echo, so a watchdog would cry
wolf on the most ordinary press. It waits on a live measurement of what the client does with
an ineligible id.

And it never enumerates your avatars. The manifest is authoritative about what is swappable;
`design.md` descopes parameter discovery and the same reasoning holds here.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Dict, Optional

from vrbridge import VRBridge
from vrbridge.mappings.mapping_base import Mapping
from vrbridge.osc_manager import (FETCH_NOT_FOUND, FETCH_OK, FETCH_TRANSPORT,
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


# ----------------------------- Mapping ------------------------------------

class WardrobeMapping(Mapping):
    """Drives /avatar/change from the worn avatar's wardrobe menu."""
    name = "osc_wardrobe"

    def __init__(self, bridge: VRBridge,
                 manifests: Optional[Dict[int, Manifest]] = None,
                 *, tuning=None):
        super().__init__(bridge)
        self.tuning = tuning if tuning is not None else settings().wardrobe
        self.log = bridge.log
        # Injected rather than loaded here so a caller can supply a fixture set, and so a
        # config error surfaces at the caller's construction site instead of inside a
        # mapping's __init__. `load_from_settings` is the convenience path.
        self._manifests: Dict[int, Manifest] = dict(manifests or {})

        # --- state shared across datagrams -------------------------------------
        # The OSC server is thread-per-datagram, so a Slot handler can run while the
        # re-read worker is mid-flight. One lock covers all of it, and is never held
        # across a fetch -- the discipline _consider_service already follows.
        self._lock = threading.RLock()
        self._active: Optional[Manifest] = None
        self._worn_avatar_id: Optional[str] = None
        self._rearm_queued = False

        self._work: "queue.Queue[str]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None

    # ---- construction helpers --------------------------------------------

    @classmethod
    def load_from_settings(cls, bridge: VRBridge, *, tuning=None) -> "WardrobeMapping":
        """Build with the manifests found in the configured directory.

        A missing directory yields none, which is not an error here -- the mapping reports
        that it has nothing to work with when it is activated, because only then has anyone
        asked it to do something.
        """
        from vrbridge.wardrobe import discover_manifests
        tune = tuning if tuning is not None else settings().wardrobe
        found = discover_manifests(tune.resolved_manifest_dir())
        return cls(bridge, found, tuning=tune)

    # ---- lifecycle -------------------------------------------------------

    def _attach(self) -> None:
        # Gated: a disabled wardrobe must not swap the avatar.
        self.bridge.on_osc(SLOT_ADDR, self._gate(self._on_slot))

        # UNGATED, and that is load-bearing. This handler is both the only trigger that
        # re-reads the marker and the only path back from a deactivated state. Gating it
        # would make deactivation terminal: a 404 on one avatar change would silence the
        # mapping until the process restarted, and because a *successful* swap is what
        # triggers the read, the feature would break precisely on working.
        self.bridge.on_osc(AVATAR_CHANGE_ADDR, self._on_avatar_change)

        # Same reasoning as the avatar-change handler; also runs on zeroconf's dispatch
        # thread, so it only queues work.
        self.bridge.on_target_selected(self._on_target_selected)

    def activate(self) -> None:
        super().activate()
        if not self._manifests:
            self.log.warning(
                "%s is active with no wardrobe manifests loaded, so no button can swap "
                "anything. Put a manifest in %s -- see wardrobe.example.toml.",
                self.name, self.tuning.resolved_manifest_dir())

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

        if active is None:
            self.log.warning(
                "Wardrobe slot %d pressed, but no manifest is active for the worn avatar "
                "(marker %s unread or unknown); ignoring the press.", slot, MARKER_ADDR)
            return

        row = active.avatar_for(slot)
        if row is None:
            # Gaps are legal and the menu always ships eight buttons, so a pressable slot
            # with no row is an ordinary authoring state. Warn and stay active -- routing
            # this into the manifest-miss path would take the whole wardrobe down over one
            # unused button.
            self.log.warning(
                "Wardrobe slot %d has no entry in manifest %d (%s); nothing to swap to.",
                slot, active.id, active.source)
            return

        if worn is not None and row.avatar_id == worn:
            # A courtesy, not a correctness mechanism: the client no-ops a swap to the
            # avatar already worn. Skipping it keeps the log honest about what we asked for.
            self.log.info("Wardrobe slot %d is the avatar already worn (%s); no send.",
                          slot, row.avatar_id)
            return

        label = f" ({row.label})" if row.label else ""
        self.log.info("Wardrobe slot %d -> %s%s", slot, row.avatar_id, label)
        ctx.send(AVATAR_CHANGE_ADDR, row.avatar_id)

    def _on_avatar_change(self, ctx, address: str, value) -> None:
        """The worn avatar changed: the active manifest no longer describes it."""
        worn = value if isinstance(value, str) else None
        with self._lock:
            self._worn_avatar_id = worn
            # Invalidate first. Between here and a validated re-read the mapping is inert,
            # which is the point: a press during the transition does nothing rather than
            # indexing the previous avatar's table and swapping somewhere unasked.
            self._active = None
        self.deactivate()
        self._queue_rearm("avatar-change")

    def _on_target_selected(self, ctx, target) -> None:
        """VRChat was discovered (or came back on a new port): read the marker.

        Without this the mapping would wait for the wearer's first avatar change, because a
        marker's value never changes and so is never emitted -- the cache would stay empty
        for the whole session.
        """
        self._queue_rearm("target-selected")

    # ---- the re-read worker ---------------------------------------------

    def _queue_rearm(self, why: str) -> None:
        """Ask for a marker read, at most one outstanding at a time.

        Coalescing is not an optimisation. `design.md` rules that inbound is doubled until
        its open measurement lands, so every avatar change arrives twice; without this, one
        swap would start two read schedules racing to adopt a manifest.
        """
        with self._lock:
            if self._rearm_queued:
                return
            self._rearm_queued = True
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="OscWardrobeMarker", daemon=True)
                self._thread.start()
        self._work.put(why)

    def _run(self) -> None:
        while True:
            why = self._work.get()
            try:
                self._read_marker(why)
            except Exception:
                # Off any caller's thread, so nothing else would surface this.
                self.log.exception("Wardrobe marker read failed (%s)", why)
            finally:
                with self._lock:
                    self._rearm_queued = False
                self._work.task_done()

    def _read_marker(self, why: str) -> None:
        """Read the marker and adopt the manifest it names, or leave the mapping inert.

        After an avatar change a single read is not enough. The client's tree 404s an
        address no worn avatar declares, but a wardrobe's whole purpose is that *several*
        avatars declare this one at different values -- so a read taken during the
        transition can return the outgoing avatar's id and be indistinguishable from a
        correct answer. Requiring two reads a gap apart to agree is what makes adopting the
        wrong table unlikely rather than routine.

        On target selection there is no transition to be confused by, so one read settles
        it: nothing was worn before that could still be served.
        """
        tune = self.tuning
        need_stable = (why == "avatar-change")
        if need_stable and tune.settle_delay_secs:
            time.sleep(tune.settle_delay_secs)

        previous: Optional[int] = None
        transport_failures = 0
        for attempt in range(tune.max_reads):
            if attempt:
                time.sleep(tune.stable_gap_secs)
            result: FetchResult = self.bridge.osc.fetch(
                MARKER_ADDR, timeout=tune.fetch_timeout_secs)

            if result.reason == FETCH_NOT_FOUND:
                # The worn avatar declares no marker: it carries no wardrobe. Normal, and
                # the common case on any avatar without the prefab.
                self.log.info("No wardrobe marker on the worn avatar (%s 404s); "
                              "wardrobe idle until the next avatar change.", MARKER_ADDR)
                return
            if result.reason == FETCH_TRANSPORT:
                transport_failures += 1
                previous = None  # a gap in the series breaks the stability claim
                continue
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

            if not need_stable or marker == previous:
                self._adopt(marker)
                return
            previous = marker

        if transport_failures:
            self.log.warning(
                "Gave up reading the wardrobe marker after %d attempts (%d transport "
                "failures); the wardrobe stays idle until the next avatar change.",
                tune.max_reads, transport_failures)
        else:
            self.log.warning(
                "The wardrobe marker never held the same value across %d reads, so no "
                "manifest was adopted. The worn avatar may still have been loading.",
                tune.max_reads)

    def _adopt(self, marker: int) -> None:
        manifest = self._manifests.get(marker)
        if manifest is None:
            known = ", ".join(str(k) for k in sorted(self._manifests)) or "none"
            self.log.warning(
                "The worn avatar's wardrobe marker is %d, which no loaded manifest claims "
                "(loaded: %s). Give a manifest id %d, or correct the avatar's %s default.",
                marker, known, marker, MARKER_ADDR)
            return
        with self._lock:
            self._active = manifest
        self.activate()
        self.log.info("Wardrobe manifest %d active (%d slot(s), from %s).",
                      manifest.id, len(manifest.slots), manifest.source)

    def update(self, now: float) -> None:
        return
