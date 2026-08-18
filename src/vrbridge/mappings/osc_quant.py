"""Quant-channel directory: which manifest describes the worn avatar, read on use.

The worn avatar names its own channel table: the `quant-channel` vrc-patterns entry declares
an unsynced Int `QuantChannel/Manifest` whose *default value* is a manifest id, read over
OSCQuery -- a value that never changes is never emitted, so the wire cannot carry it. This
mapping owns that read and hands consumers the armed `QuantManifest`; it sends nothing
itself, and no shipped router registers it (opt-in, like the wardrobe).

**Why this is not the wardrobe's read-on-every-press, although it copies everything else.**
The wardrobe's marker read rides a human press: rare, and itself the proof the avatar is
loaded. A quant consumer asks at controller rate, so the read latches (`arm`) -- and the
whole trigger design is shaped by what a *cold avatar load* does to any read taken at the
change: the client acknowledges `/avatar/change` immediately while a cold download runs
30-60 s, so a fetch fired on the change either 404s (and a latch-on-change design stays dark
until some future change) or reads the *outgoing* avatar's tree and arms the wrong manifest.
Hence, in order:

* **Invalidation is inline and cheap.** `/avatar/change` and target-selected only clear the
  armed state, bump a sequence token, and wake the worker. The target-selected leg runs on
  zeroconf's single dispatch thread, which `docs/design.md` prices at one blocking query for
  target *selection* itself -- an inline fetch there is not ours to add.
* **All fetches run on one daemon worker thread** (the `press_pulse` single-worker shape).
  A completed fetch arms only if its token is still current: two avatar changes in flight
  must not let the older fetch latch the older avatar's manifest.
* **Re-arm on use, floored.** While unarmed, `active_manifest()` re-kicks the worker at most
  once per `REARM_FLOOR_SECS`. This is what closes the cold load: the 404 taken mid-download
  answers "right now", the next use after the avatar finishes loading asks again. It is not a
  timer -- an untouched bridge fetches nothing.

The armed manifest is trusted only after the **puppet cross-check**: where a manifest
declares channels at `index_puppet`'s own addresses, its `bits`/`floatTau` must match the
`[puppet]` settings that actually drive those addresses, or the arm is refused naming both
values. Without this the manifest is ceremonial for the one rig it exists to describe --
two silently divergent homes for the same numbers.

`enabled` belongs to the router (the wardrobe's rule): the ungated invalidation handlers
only ever clear, and the worker declines to fetch while the mapping is disabled.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional

from vrbridge import VRBridge
from vrbridge.mappings import index_puppet as puppet
from vrbridge.mappings.mapping_base import Mapping
from vrbridge.osc_manager import (FETCH_NO_PEER, FETCH_NOT_FOUND, FETCH_OK,
                                  FETCH_PEER_GONE, FetchResult)
from vrbridge.quant_manifest import QuantManifest
from vrbridge.settings import settings

# ------------------------------ Config ------------------------------------

# The sentinel is a contract with the `quant-channel` vrc-patterns entry (its generator
# emits the parameter), not a setting -- a typo here is a diff, not a silent runtime miss.
MARKER_ADDR = "/avatar/parameters/QuantChannel/Manifest"
AVATAR_CHANGE_ADDR = "/avatar/change"

#: Floor between use-triggered fetch attempts while unarmed. A consumer asks at controller
#: rate (~every 11 ms mid-gesture), and each attempt is a loopback GET with a 404 as its
#: steady-state answer on an avatar without channels -- unfloored, that is a 90 Hz poll of
#: the client's HTTP server. 2 s keeps the cold-load recovery prompt (the avatar takes tens
#: of seconds to load; one extra 2 s beat is invisible) at half the wardrobe's default fetch
#: timeout, so attempts cannot stack on their own trigger.
REARM_FLOOR_SECS = 2.0


class QuantChannelDirectory(Mapping):
    """Arms the worn avatar's quant-channel manifest; consumers read `active_manifest()`."""
    name = "osc_quant"

    def __init__(self, bridge: VRBridge,
                 manifests: Optional[Dict[int, QuantManifest]] = None,
                 *, tuning=None, puppet_tuning=None,
                 pinned_manifest_id: Optional[int] = None):
        super().__init__(bridge)
        self.tuning = tuning if tuning is not None else settings().quantchannel
        # The cross-check compares against the settings that actually drive the puppet
        # addresses; injected for tests exactly as the manifests are.
        self._puppet_tune = puppet_tuning if puppet_tuning is not None else settings().puppet
        self.log = bridge.log
        # Injected rather than loaded here, the wardrobe's reasoning: a caller can supply a
        # fixture set, and a config error surfaces at the caller's construction site.
        self._manifests: Dict[int, QuantManifest] = dict(manifests or {})
        # Names the manifest outright for a peer that serves no OSCQuery at all (a pinned
        # --osc-port target, the Av3Emulator). There is nothing to read there. Deliberately
        # a constructor kwarg and NOT a CLI flag: the CLI constructs routers with nothing
        # else, no shipped router registers this mapping, and the embedder who does register
        # it holds the constructor anyway.
        self._pinned_manifest_id = pinned_manifest_id

        # One lock over the latch state; never held across a fetch (the wardrobe's
        # discipline, and a plain Lock so a violation deadlocks loudly).
        self._lock = threading.Lock()
        self._armed: Optional[QuantManifest] = None
        # Bumped by every invalidation; a fetch arms only if the token it started under is
        # still current, so a fetch completing after a later change cannot latch stale.
        self._seq = 0
        self._last_attempt = 0.0        # monotonic; the REARM_FLOOR_SECS clock
        self._reported: Optional[tuple] = None
        self._wake = threading.Event()
        self._worker: Optional[threading.Thread] = None

    # ---- construction helpers --------------------------------------------

    @classmethod
    def load_from_settings(cls, bridge: VRBridge, *, tuning=None, puppet_tuning=None,
                           pinned_manifest_id: Optional[int] = None) -> "QuantChannelDirectory":
        """Build with the manifests found in the configured directory.

        A missing directory yields none, which is not an error here -- the mapping reports
        having nothing to work with when something actually asks it to arm.
        """
        from vrbridge.quant_manifest import discover_manifests
        tune = tuning if tuning is not None else settings().quantchannel
        found = discover_manifests(tune.resolved_manifest_dir())
        return cls(bridge, found, tuning=tune, puppet_tuning=puppet_tuning,
                   pinned_manifest_id=pinned_manifest_id)

    # ---- lifecycle -------------------------------------------------------

    def _attach(self) -> None:
        # UNGATED, like the wardrobe's invalidate: both handlers only ever clear, so they
        # cannot resurrect a mapping its router switched off -- and a disabled directory
        # passing through avatar changes must not re-arm with stale state on re-enable.
        self.bridge.on_osc(AVATAR_CHANGE_ADDR, self._on_invalidate)
        # Runs on zeroconf's dispatch thread; clearing plus an Event.set is the whole cost.
        self.bridge.on_target_selected(lambda ctx, target: self._on_invalidate(ctx, "", None))

    # ---- events ----------------------------------------------------------

    def _on_invalidate(self, ctx, address: str, value) -> None:
        """The worn avatar (or the client behind the target) changed: disarm, and let the
        worker ask again. Idempotent under the doubled inbound delivery -- clearing twice
        and bumping twice both leave the same state: unarmed, newest token wins."""
        with self._lock:
            self._armed = None
            self._seq += 1
            self._reported = None
            # Not floored: this is an event, not a use, and the cold-load recovery counts
            # on the change kicking a fresh read promptly.
            self._last_attempt = 0.0
        self._kick()

    # ---- the consumer's door ---------------------------------------------

    def active_manifest(self) -> Optional[QuantManifest]:
        """The armed manifest, or None -- in which case asking IS the retry trigger.

        Read-on-use: while unarmed, each call re-kicks the worker, floored at
        REARM_FLOOR_SECS between attempts. Never blocks; a consumer polls this at
        controller rate and gets the latch, not a fetch.
        """
        with self._lock:
            if self._armed is not None:
                return self._armed
            if not self.enabled:
                return None
            if time.monotonic() - self._last_attempt < REARM_FLOOR_SECS:
                return None
        self._kick()
        return None

    # ---- the worker ------------------------------------------------------

    def _kick(self) -> None:
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._run, name="QuantChannelFetch", daemon=True)
                self._worker.start()
        self._wake.set()

    def _run(self) -> None:
        while True:
            self._wake.wait()
            self._wake.clear()
            try:
                self._attempt()
            except Exception:
                # Off every caller's thread, so nothing else would surface this.
                self.log.exception("Quant-channel manifest fetch failed")

    def _attempt(self) -> None:
        with self._lock:
            if self._armed is not None or not self.enabled:
                return
            token = self._seq
            self._last_attempt = time.monotonic()

        if self._pinned_manifest_id is not None:
            # Named outright, so there is nothing to read -- design.md's rule that naming
            # a peer takes the question away rather than entering it as a bid.
            self._resolve(self._pinned_manifest_id, token, "named by pinned_manifest_id")
            return

        # The blocking read, on this thread only, with no lock held.
        result: FetchResult = self.bridge.osc.fetch(
            MARKER_ADDR, timeout=self.tuning.fetch_timeout_secs)

        if result.reason == FETCH_NOT_FOUND:
            if result.peer is not None and not result.peer.is_vrchat:
                # The 404 is about the peer, not the avatar (the wardrobe's finding, same
                # ranking mechanics): before a VRChat client is discovered the target slot
                # can hold VRCFaceTracking or VRCOSC, whose trees carry no avatar
                # parameters. Stays unarmed and retryable -- the next use after a real
                # client takes the slot asks again.
                self._report(("foreign-peer", result.peer.name), "warning",
                             "%s was read from %s, which does not identify itself as "
                             "VRChat, so its 404 says nothing about the worn avatar; "
                             "quant channels stay unarmed until a VRChat client is "
                             "discovered and takes the target. If one is already running, "
                             "its service is not being recognised -- docs/design.md, "
                             "Target selection.", MARKER_ADDR, result.peer.name)
                return
            # No sentinel on whatever is worn right now. Normal on any avatar without a
            # quant-channel module, and normal *transiently* through a cold load -- which is
            # why it suppresses only the log line, never the next attempt.
            self._report(("no-marker",), "info",
                         "The worn avatar declares no quant-channel sentinel (%s 404s); "
                         "no channels to arm.", MARKER_ADDR)
            return
        if result.reason == FETCH_NO_PEER:
            if self.bridge.osc.target_is_pinned:
                self._report(("no-peer", "pinned"), "warning",
                             "The send target was pinned, so no OSCQuery tree exists to "
                             "read %s from. Construct the directory with "
                             "pinned_manifest_id= to name the manifest instead.",
                             MARKER_ADDR)
            else:
                self._report(("no-peer", "undiscovered"), "warning",
                             "No OSCQuery peer discovered yet, so %s cannot be read; "
                             "quant channels arm once VRChat has been found.", MARKER_ADDR)
            return
        if result.reason == FETCH_PEER_GONE:
            self._report(("peer-gone",), "warning",
                         "The client we were reading %s from withdrew; quant channels "
                         "re-arm once VRChat is running and rediscovered.", MARKER_ADDR)
            return
        if result.reason != FETCH_OK:
            self._report(("fetch", result.reason), "warning",
                         "Cannot read the quant-channel sentinel, so nothing is armed: "
                         "%s (%s)", result.reason, result.detail)
            return

        value = result.value
        # bool is an int subclass and int() truncates, so a T or a 1.9 would otherwise
        # become manifest 1 and arm the wrong table. Test the type instead of coercing.
        if isinstance(value, bool) or not isinstance(value, int):
            self._report(("bad-marker", repr(value)), "warning",
                         "Quant-channel sentinel %s served %r, which is not a whole "
                         "number.", MARKER_ADDR, value)
            return
        self._resolve(value, token, "read from the worn avatar")

    # ---- arming ----------------------------------------------------------

    def _resolve(self, marker: int, token: int, how: str) -> None:
        manifest = self._manifests.get(marker)
        if manifest is None:
            known = ", ".join(str(k) for k in sorted(self._manifests)) or "none"
            self._report(("unknown", marker), "warning",
                         "The worn avatar's quant-channel sentinel is %d, which no loaded "
                         "manifest claims (loaded: %s). Install a manifest with id %d, or "
                         "correct the avatar's %s default.", marker, known, marker,
                         MARKER_ADDR)
            return
        problem = self._puppet_mismatch(manifest)
        if problem is not None:
            # Refused, not warned-and-armed: an armed manifest is a promise that its
            # numbers describe the wire, and for the puppet addresses the [puppet]
            # settings are what actually drive it.
            self._report(("cross-check", manifest.id, problem), "error",
                         "Refusing to arm quant-channel manifest %d (%s): %s. The manifest "
                         "and [puppet] settings describe the same wire and must agree -- "
                         "fix whichever is stale.", manifest.id, manifest.source, problem)
            return
        self._arm(manifest, token, how)

    def _puppet_mismatch(self, manifest: QuantManifest) -> Optional[str]:
        """The cross-check: a manifest channel at an `index_puppet` address must agree
        with the settings that drive that address. None means no conflict."""
        for ch in manifest.channels:
            if ch.address not in puppet.AXIS_ADDRS:
                continue
            if ch.bits != self._puppet_tune.quant_level:
                return (f"channel {ch.name} declares bits={ch.bits} but "
                        f"[puppet] quant_level is {self._puppet_tune.quant_level}")
            if ch.float_tau != self._puppet_tune.float_smooth_tau_secs:
                return (f"channel {ch.name} declares floatTau={ch.float_tau} but "
                        f"[puppet] float_smooth_tau_secs is "
                        f"{self._puppet_tune.float_smooth_tau_secs}")
        return None

    def _arm(self, manifest: QuantManifest, token: int, how: str) -> None:
        with self._lock:
            if token != self._seq:
                # A later invalidation happened while this fetch was in flight; what it
                # read describes an avatar that is no longer the question.
                self.log.info(
                    "Quant-channel manifest %d was read for a superseded avatar state; "
                    "not arming it.", manifest.id)
                return
            self._armed = manifest
        # revision is the only stale-copy detector there is until the discovery audit:
        # a regenerated avatar beside an old installed JSON diverges silently otherwise.
        self._report(("armed", manifest.id, manifest.revision), "info",
                     "Quant-channel manifest %d rev %d armed (%d channel(s), from %s) "
                     "-- %s.", manifest.id, manifest.revision, len(manifest.channels),
                     manifest.source, how)

    def _report(self, key: tuple, level: str, msg: str, *args) -> None:
        """Log an outcome once per state, not once per attempt -- the wardrobe's `_report`:
        a pinned session would otherwise say FETCH_NO_PEER every other second forever."""
        with self._lock:
            if self._reported == key:
                return
            self._reported = key
        getattr(self.log, level)(msg, *args)

    def update(self, now: float) -> None:
        return
