from __future__ import annotations

import threading
import time

from vrbridge.mappings.mapping_base import Mapping
from vrbridge import VRBridge
from vrbridge.settings import settings

# ------------------------------ Config ------------------------------------

# Avatar parameters to set when VRCFT is detected.
# VRCFT may also control these, but setting them helps with avatar logic.
# Real bools, not 0/1: both are avatar Bool parameters, and an OSC argument's type has to
# match the declared one (the workspace's docs/osc.md, "Argument types are exact").
ACTIVE_PARAMS: dict[str, bool] = {
    "/avatar/parameters/LipTrackingActive": True,
    "/avatar/parameters/EyeTrackingActive": True,
}

# Avatar parameters to set when VRCFT is NOT detected.
INACTIVE_PARAMS: dict[str, bool] = {
    "/avatar/parameters/LipTrackingActive": False,
    "/avatar/parameters/EyeTrackingActive": False,
}

# The mDNS service-name substring, the post-avatar-change delay and the service poll
# interval are settings.VRCFTSettings.service_name / .avatar_load_delay_secs /
# .service_poll_secs.

# ----------------------------- Mapping ------------------------------------

class VRCFTMapping(Mapping):
    """
    Detects if VRChat Face Tracking (VRCFT) is running and sets avatar
    parameters accordingly: after an avatar change, and again whenever the VRCFT service
    appears or disappears.

    The load delay is served by `update()`, not by sleeping in the callback.

    *Why the delay moved.* `avatar_load_delay_secs` used to be a `time.sleep` inside
    `_on_avatar_change`. `design.md` rules that the OSC datagram path tolerates a
    blocking handler, and it does -- dispatch is thread-per-datagram, so the sleep cost
    the server nothing. But handlers for one address run serially, in registration
    order, on that one datagram thread, so the wait was charged to every handler behind
    this one: four others share `/avatar/change`, and `index_puppet`'s reset ran a
    measured 1.001 s late, which once got read as the reset's own latency rather than a
    borrowed delay. The criterion that makes this dead time rather than work: the sleep
    consumed no result. Nothing is read from it, so only the *ordering* matters to us
    and which thread holds the wait does not -- unlike `osc_wardrobe._on_slot`, which
    blocks on a fetch whose answer it needs and is right to.

    *Why a tick and not a worker thread.* `press_pulse` (`utils.py`) is the other shipped
    answer to a blocking delay, but its shape is press/hold/release keyed per address and
    there is no trailing release here; reusing it would mean a general deferred-callable
    worker, plus cancellation and a shutdown drain, for one caller. The router already
    ticks every registered mapping (`MappingRouter.run_forever`), so the deadline costs
    no thread and supersession is a field overwrite.

    *Following VRCFT without an avatar change.* The avatar-change check alone left the
    toggles at whatever VRCFT's state was at the last swap, so a VRCFT started after the
    avatar loaded read 0/0 until the next swap. `update()` therefore also looks at the
    service every `service_poll_secs` and sends only when the answer differs from the last
    one this mapping sent. It polls because no `OSCManager` hook reports an arbitrary
    service arriving or leaving: target listeners report send-target selections, peer
    listeners a pinned peer resolving, and a new service-listener hook would be manager
    API for one caller where `is_service_running` is already a cheap read. An armed
    avatar-change deadline owns the next send, so polling pauses while one is pending.

    *The first look records and sends nothing.* Sending on it would zero the toggles at
    every startup, because discovery fills `_discovered_services` only after
    `get_service_info` (2 s timeout) returns, so the first look usually reads False even
    with VRCFT running. In production an already-running VRCFT therefore shows up as a
    transition a few seconds in and is sent then. The test fake has no discovery latency,
    so there the first look reads the true state and a running VRCFT sends nothing until a
    swap or a transition; that difference is the fake's, not a second behaviour.

    *A send that did not land is not recorded.* `_last_running` and the checkpoint move
    only when every `send` returned True, and a failed one sets `_resend` so the next poll
    sends again whether or not the state changed. Two cases need it: no target yet, and
    VRCFT holding an empty target slot (design.md, Target selection) so the set went to
    VRCFT's port while a VRChat that was already loaded sends no `/avatar/change` to
    repair it. A target selection (`on_target_selected`) bumps `_gen` so an apply in
    flight for the old target cannot record, rewinds the poll clock, and sets `_resend`
    once a send has landed, so the next tick sends to the new target. The send that went
    to VRCFT's own port is recorded as landed, which is why the selection, not the send, is
    the repair. Before any send has landed there is nothing to repair, and forcing one
    would break the first-look rule: VRChat resolving before VRCFT's own `add_service`
    reads False and zeroes a running VRCFT's toggles. The handler is not gated, so a
    selection while the router has the mapping switched off is still owed on reactivation,
    as a service transition is. A retried send logs at DEBUG, since `OSCManager.send`
    already warned once for the outage (design.md, "A dropped send warns once per outage").

    *The cost of that choice, stated because it is not obvious at the call site:* this
    mapping now needs its `update()` driven. Under any shipped router it is; a host that
    drives VRBridge without `MappingRouter.run_forever` gets no VRCFT parameter set at all.
    """
    name = "osc_vrcft"

    def __init__(self, bridge: VRBridge, tuning=None):
        super().__init__(bridge)
        self._tune = tuning if tuning is not None else settings().vrcft
        # Wall clock, matching what run_forever hands update() -- index_puppet's idle
        # timer stamps the same clock from a callback. Mixing in monotonic here would
        # compare two unrelated epochs. None means nothing pending.
        self._due: float | None = None
        # Bumped on every arm so a fire in flight can tell it has been superseded --
        # osc_quant's `_seq` is the same idiom for the same reason.
        self._gen: int = 0
        # What the last send (or the first poll) saw, so a poll sends only on a transition.
        # None until either has run. Under _lock.
        self._last_running: bool | None = None
        # The next poll sends whether or not the state changed: the last send failed or the
        # send target moved. Under _lock.
        self._resend: bool = False
        # Some send has landed, so a target selection has something to repair. Under _lock.
        self._landed: bool = False
        # Wall-clock time of the next service poll, same clock as _due. Under _lock.
        self._next_poll: float = 0.0
        # Armed on a datagram thread, read and cleared on the router loop thread.
        self._lock = threading.Lock()

    def _attach(self) -> None:
        """Register a callback for avatar changes."""
        self.bridge.on_osc("/avatar/change", self._gate(self._on_avatar_change))
        # Ungated: flipping fields while disabled is harmless (update() and _apply both
        # check enabled), and a selection missed while disabled would never be resent.
        self.bridge.on_target_selected(self._on_target_selected)

    def _on_target_selected(self, ctx, target) -> None:
        """A send target was chosen or re-resolved: what we last sent may have gone elsewhere.

        Runs on zeroconf's dispatch thread, so it only flips fields; the poll does the send.
        """
        with self._lock:
            self._gen += 1
            self._next_poll = 0.0
            if self._landed:
                self._resend = True

    def _on_avatar_change(self, ctx, address: str, avatar_id: str):
        """Arm the deferred check. Returns immediately; `update()` does the work.

        A second change before the first is due overwrites the deadline rather than
        queueing behind it: the pending send describes whichever avatar is arriving, so
        the newest event is the only one worth answering. The inline sleep had no such
        guard -- two changes a second apart ran two independent waits and sent twice.
        """
        with self._lock:
            self._gen += 1
            self._due = time.time() + self._tune.avatar_load_delay_secs

    def update(self, now: float) -> None:
        """Fire the armed check once its delay has elapsed; with none armed, poll the service."""
        with self._lock:
            due = self._due
            if due is None:
                if not self.enabled or now < self._next_poll:
                    return
                self._next_poll = now + self._tune.service_poll_secs
                gen = self._gen
                on_change_only = True
            else:
                # Drop rather than fire on a mapping the router switched off: the callback
                # is gated, so arming already required being enabled, and a deactivation
                # between arming and firing is a decision not to send.
                if not self.enabled:
                    self._due = None
                    return
                if now < due:
                    return
                self._due = None
                gen = self._gen
                on_change_only = False

        self._apply(gen, on_change_only)

    def _apply(self, gen: int, on_change_only: bool = False) -> None:
        """Check for VRCFT and send the corresponding parameter set.

        `gen` is the arm this fire belongs to. The service check runs unlocked -- it takes
        OSCManager's own lock, and nesting ours around a foreign component's is what the
        wardrobe's "never across a fetch" rule generalizes to -- so an avatar change can
        land while we are in it. Re-checking under the lock before sending is what makes
        the supersession claim hold through the send rather than only through the deadline:
        parameters written during the *next* avatar's load are exactly what the delay
        exists to prevent. The lock is held across the sends deliberately; two `sendto`
        calls are the bounded cost of an exact claim, and design.md's rule bars holding a
        lock across a *fetch*, which this is not.

        `on_change_only` is the service poll: it sends nothing unless the answer differs
        from the last one acted on, and its first look only records the state. A poll
        leaves `_gen` alone, so an avatar change landing mid-poll still cancels it.
        """
        is_vrcft_running = self.bridge.osc.is_service_running(self._tune.service_name)

        params_to_set = ACTIVE_PARAMS if is_vrcft_running else INACTIVE_PARAMS

        with self._lock:
            if gen != self._gen or not self.enabled:
                return
            if on_change_only and not self._resend:
                previous = self._last_running
                if previous is None:
                    self._last_running = is_vrcft_running
                    return
                if previous == is_vrcft_running:
                    return
            if self._send(params_to_set, is_vrcft_running):
                self._last_running = is_vrcft_running
                self._resend = False
                self._landed = True
            else:
                self._resend = True

    def _send(self, params_to_set: dict[str, bool], is_vrcft_running: bool) -> bool:
        """Send and log; True only if every send landed. Called with `_lock` held."""
        # osc.send directly rather than through a stashed CallbackContext: ctx.send is a
        # passthrough to it, and index_puppet's update() already sends this way. Holding a
        # ctx from the arming callback would outlive the event it came from for no gain.
        results = [self.bridge.osc.send(addr, val) for addr, val in params_to_set.items()]
        msg = ("VRCFT detected. Activating face tracking parameters." if is_vrcft_running
               else "VRCFT not detected. Deactivating face tracking parameters.")
        if all(results):
            self.bridge.log.info(msg)
            return True
        # A failed send retries every poll for as long as the outage lasts; INFO here would
        # repeat at that rate where OSCManager.send has already warned once.
        self.bridge.log.debug("%s Not every send landed; the next poll retries.", msg)
        return False
