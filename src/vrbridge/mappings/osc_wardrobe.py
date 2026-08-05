"""Wardrobe mapping: a wardrobe button on the worn avatar -> /avatar/change.

The wearer presses a control on their own expression menu and the worn avatar changes.
Two avatar parameters carry it, both declared by the `osc-wardrobe` vrc-patterns entry:

* `OscWardrobe/Slot` -- the selector. A menu Button sets it to 1..8 and it returns to 0.
* `OscWardrobe/Manifest` -- the marker. Its *default value* is a manifest id, which is how
  the bridge learns which slot table this avatar's menu means. Read over OSCQuery rather
  than off the wire, because a value that never changes is never emitted.

**The marker is read on the first press after an avatar change, not on the change itself.**
The press is the readiness signal, and using it is what makes this simple: a Slot datagram
can only arrive if the new avatar is loaded and emitting, so a marker read at that instant
necessarily describes the avatar being worn. Reading on the change instead means reading
*during* a transition, which needs a settling window -- and no window can be sized, because
a cold avatar download runs 30-60 s while the client acknowledges the change immediately. An
earlier design polled the marker on a ~6.5 s budget and concluded "this avatar has no
wardrobe" while the avatar was still downloading. Do not reintroduce a schedule here.

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
        self._lock = threading.RLock()
        self._active: Optional[Manifest] = None
        # Set once a read has established that the worn avatar carries no usable marker, so
        # a wardrobe-less avatar is not re-queried on every press. Cleared by any avatar
        # change, which is the only thing that can make the answer different.
        self._marker_settled = False

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
            self._active = None
            self._marker_settled = False

    def _on_slot(self, ctx, address: str, value) -> None:
        try:
            slot = int(value)
        except (TypeError, ValueError):
            self.log.warning("Wardrobe slot %s is %r, which is not a whole number; ignored.",
                             address, value)
            return
        if slot == REST_SLOT:
            return

        manifest = self._arm()
        if manifest is None:
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

    def _arm(self) -> Optional[Manifest]:
        """The manifest for the worn avatar, reading its marker if that is not yet known.

        Called from a press, on an OSC datagram thread, where `design.md` sanctions a
        blocking call. The read is one HTTP GET against loopback.
        """
        with self._lock:
            if self._active is not None:
                return self._active
            if self._marker_settled:
                # A previous press already established there is nothing to arm with. Say
                # nothing further: repeating it on every press would bury the first message.
                return None

        if self._pinned_manifest_id is not None:
            # Named outright, so there is nothing to read -- design.md's rule that naming a
            # peer takes the question away rather than entering it as a bid.
            return self._adopt(self._pinned_manifest_id, "named by pinned_manifest_id")

        result: FetchResult = self.bridge.osc.fetch(
            MARKER_ADDR, timeout=self.tuning.fetch_timeout_secs)

        if result.reason == FETCH_NOT_FOUND:
            # The worn avatar declares no marker, so it carries no wardrobe. Normal on any
            # avatar without the prefab, and settled until the next avatar change.
            with self._lock:
                self._marker_settled = True
            self.log.info("The worn avatar declares no wardrobe marker (%s 404s), so its "
                          "menu is not a wardrobe.", MARKER_ADDR)
            return None
        if result.reason == FETCH_NO_PEER:
            # A pinned target advertises nothing and serves no tree. Settle so this is said
            # once per avatar rather than once per press, and name the way out.
            with self._lock:
                self._marker_settled = True
            self.log.warning(
                "No OSCQuery peer to read %s from -- the send target was pinned, and a "
                "pinned peer serves no tree. Construct the wardrobe with "
                "pinned_manifest_id= to name the manifest instead.", MARKER_ADDR)
            return None
        if result.reason != FETCH_OK:
            # Deliberately NOT settled: a transport failure or an unusable answer says
            # nothing about the avatar, so the next press should try again.
            self.log.warning("Cannot read the wardrobe marker, so this press does nothing: "
                             "%s (%s)", result.reason, result.detail)
            return None

        try:
            marker = int(result.value)
        except (TypeError, ValueError):
            with self._lock:
                self._marker_settled = True
            self.log.warning("Wardrobe marker %s served %r, which is not a whole number.",
                             MARKER_ADDR, result.value)
            return None

        return self._adopt(marker, "read from the worn avatar")

    def _adopt(self, marker: int, how: str) -> Optional[Manifest]:
        manifest = self._manifests.get(marker)
        if manifest is None:
            known = ", ".join(str(k) for k in sorted(self._manifests)) or "none"
            with self._lock:
                self._marker_settled = True
            self.log.warning(
                "The worn avatar's wardrobe marker is %d, which no loaded manifest claims "
                "(loaded: %s). Give a manifest id %d, or correct the avatar's %s default.",
                marker, known, marker, MARKER_ADDR)
            return None
        with self._lock:
            self._active = manifest
        self.log.info("Wardrobe manifest %d active (%d slot(s), from %s) -- %s.",
                      manifest.id, len(manifest.slots), manifest.source, how)
        return manifest

    def update(self, now: float) -> None:
        return
