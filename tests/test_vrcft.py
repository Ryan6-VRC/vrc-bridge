"""The VRCFT load delay: deferred to the tick, and off the datagram thread.

The delay itself is a preserved tuned value (`design.md` §The warrant criterion), so
nothing here re-derives 1.0 s -- these tests read whatever is configured and assert the
*shape*: the callback returns without waiting, the send lands once the delay has elapsed,
and the newest avatar change is the one answered.
"""
import time

import pytest

from vrbridge.engine import VRBridge
from vrbridge.mappings import VRCFTMapping
from vrbridge.mappings import osc_vrcft
from vrbridge.settings import VRCFTSettings

DELAY = 0.5
TUNE = VRCFTSettings(service_name="VRCFT", avatar_load_delay_secs=DELAY)


class _Osc:
    """Enough OSCManager for the mapping: a service answer and a send log."""

    def __init__(self, vrcft_running: bool = False):
        self.vrcft_running = vrcft_running
        self.sent: list[tuple[str, object]] = []

    def is_service_running(self, name: str) -> bool:
        return self.vrcft_running

    def send(self, address: str, value) -> bool:
        self.sent.append((address, value))
        return True

    def watch(self, address: str) -> None:
        pass


class _Bridge:
    class _Log:
        def info(self, *a): pass
        def warning(self, *a): pass
        def exception(self, *a): pass

    def __init__(self, vrcft_running: bool = False):
        self.log = self._Log()
        self.osc = _Osc(vrcft_running)
        self.handlers: list = []

    def on_osc(self, address, callback, *, watch=None):
        self.handlers.append((address, callback))


def _armed(vrcft_running: bool = False):
    """A registered, activated mapping with one avatar change already delivered.

    Returns the deadline the mapping actually stamped, not a wall-clock sample taken
    beside it: the mapping reads `time.time()` itself, so a sample from before the call
    is an unknown amount early and a tick placed off it can land short of the deadline
    if the process is descheduled in between.
    """
    bridge = _Bridge(vrcft_running)
    m = VRCFTMapping(bridge, tuning=TUNE)
    m.register()
    m.activate()
    m._on_avatar_change(None, "/avatar/change", "avtr_first")
    return bridge, m, m._due


# --------------------------------------------------------------------------
# The defect
# --------------------------------------------------------------------------

def test_a_handler_registered_behind_vrcft_is_not_delayed_by_its_load_delay(monkeypatch):
    """The reason this mapping has a tick at all.

    `engine._on_osc_event` runs one address's handlers serially on the datagram thread,
    so the old inline `time.sleep` was charged to everything registered behind this
    mapping -- measured at 1.001 s for a handler behind it, which once got read as that
    handler's own latency. Driven through the real fanout, because a stub of it could not
    fail the way production did.
    """
    # Two oracles, because each catches what the other cannot: the sleep ban fails
    # deterministically on a loaded machine where a wall-clock ceiling could flake, and
    # the wall-clock check still catches a handler that blocks on something other than
    # a sleep -- a fetch, a lock -- which a sleep ban would wave through.
    def _banned(_secs):
        raise AssertionError("osc_vrcft slept on the datagram dispatch thread")
    monkeypatch.setattr(osc_vrcft.time, "sleep", _banned)

    bridge = VRBridge(enable_steamvr=False, advertise=False, discover=False)
    try:
        vrcft = VRCFTMapping(bridge, tuning=TUNE)
        vrcft.register()
        vrcft.activate()

        ran_at: list[float] = []
        bridge.on_osc("/avatar/change", lambda ctx, a, v: ran_at.append(time.time()))

        t0 = time.time()
        bridge._on_osc_event("/avatar/change", "avtr_probe")
        elapsed = time.time() - t0

        assert ran_at, "the handler behind osc_vrcft never ran"
        assert ran_at[0] - t0 < DELAY / 2, (
            f"handler behind osc_vrcft ran {ran_at[0] - t0:.3f}s late; it is paying "
            "osc_vrcft's load delay again")
        assert elapsed < DELAY / 2, f"the fanout itself blocked for {elapsed:.3f}s"
    finally:
        bridge.osc.stop()


def test_the_callback_sends_nothing_of_its_own():
    """It arms and returns; every send belongs to the tick."""
    bridge, m, _ = _armed()
    assert bridge.osc.sent == []


# --------------------------------------------------------------------------
# The deferred send
# --------------------------------------------------------------------------

def test_nothing_is_sent_before_the_delay_has_elapsed():
    bridge, m, due = _armed()
    m.update(due - DELAY / 2)
    assert bridge.osc.sent == []


def test_the_inactive_set_is_sent_on_the_first_tick_past_the_delay():
    bridge, m, due = _armed(vrcft_running=False)
    m.update(due)
    assert dict(bridge.osc.sent) == osc_vrcft.INACTIVE_PARAMS


def test_the_active_set_is_sent_when_vrcft_is_running():
    bridge, m, due = _armed(vrcft_running=True)
    m.update(due)
    assert dict(bridge.osc.sent) == osc_vrcft.ACTIVE_PARAMS


def test_the_service_is_checked_at_fire_time_not_at_arm_time():
    """VRCFT can come up during the load delay; the whole point of waiting is to look
    at the world as it is once the avatar is there."""
    bridge, m, due = _armed(vrcft_running=False)
    bridge.osc.vrcft_running = True
    m.update(due)
    assert dict(bridge.osc.sent) == osc_vrcft.ACTIVE_PARAMS


def test_the_send_fires_once_and_not_on_every_later_tick():
    """An un-cleared deadline would re-send at update_hz -- 45 times a second."""
    bridge, m, due = _armed()
    for i in range(5):
        m.update(due + i)
    assert len(bridge.osc.sent) == len(osc_vrcft.INACTIVE_PARAMS)


def test_an_unarmed_mapping_never_sends():
    bridge = _Bridge()
    m = VRCFTMapping(bridge, tuning=TUNE)
    m.register()
    m.activate()
    for i in range(5):
        m.update(time.time() + i)
    assert bridge.osc.sent == []


# --------------------------------------------------------------------------
# Supersession and gating
# --------------------------------------------------------------------------

def test_a_second_change_supersedes_a_pending_one_rather_than_sending_twice(monkeypatch):
    """The inline sleep ran two independent waits and sent two sets; the pending send
    describes whichever avatar is arriving, so only the newest deserves an answer.

    The arming clock is patched rather than slept through: the mapping stamps `time.time()`
    itself, so the two deadlines have to be placed rather than raced.
    """
    clock = [1000.0]
    monkeypatch.setattr(osc_vrcft.time, "time", lambda: clock[0])

    bridge = _Bridge()
    m = VRCFTMapping(bridge, tuning=TUNE)
    m.register()
    m.activate()

    m._on_avatar_change(None, "/avatar/change", "avtr_first")      # due at 1000 + DELAY
    clock[0] += DELAY / 2
    m._on_avatar_change(None, "/avatar/change", "avtr_second")     # due at 1000.25 + DELAY

    # Past the first deadline, short of the second.
    m.update(1000.0 + DELAY + 0.01)
    assert bridge.osc.sent == [], "fired on the superseded deadline"

    m.update(1000.0 + DELAY / 2 + DELAY + 0.01)
    assert dict(bridge.osc.sent) == osc_vrcft.INACTIVE_PARAMS,         "the surviving deadline never fired"


def test_a_disabled_mapping_does_not_arm():
    """`_attach` registers the handler behind `_gate`, as it always did."""
    bridge = _Bridge()
    m = VRCFTMapping(bridge, tuning=TUNE)
    m.register()
    m.deactivate()
    _, gated = bridge.handlers[0]
    gated(None, "/avatar/change", "avtr_first")
    assert m._due is None, "a gated callback armed anyway"
    m.activate()
    m.update(time.time() + DELAY + 1.0)
    assert bridge.osc.sent == []


def test_deactivating_between_arming_and_firing_drops_the_send():
    bridge, m, due = _armed()
    m.deactivate()
    m.update(due)
    assert bridge.osc.sent == []
    # And the dropped deadline does not fire on re-activation.
    m.activate()
    m.update(due + 1.0)
    assert bridge.osc.sent == []


def test_a_change_arriving_during_the_fire_cancels_that_fire():
    """The supersession claim has to hold through the send, not just to the deadline.

    `_apply` checks the VRCFT service outside the lock, so an avatar change can land
    mid-fire; without the generation token the in-flight send wrote parameters during the
    new avatar's load, which is the thing the delay exists to prevent.
    """
    bridge = _Bridge()
    m = VRCFTMapping(bridge, tuning=TUNE)
    m.register()
    m.activate()
    m._on_avatar_change(None, "/avatar/change", "avtr_first")

    # Arm again from inside the service check -- where the real race lands.
    def _racing_check(name):
        m._on_avatar_change(None, "/avatar/change", "avtr_second")
        return False
    bridge.osc.is_service_running = _racing_check

    m.update(m._due)
    assert bridge.osc.sent == [], "sent for an avatar that had already been superseded"


def test_a_deactivation_arriving_during_the_fire_cancels_that_fire():
    bridge = _Bridge()
    m = VRCFTMapping(bridge, tuning=TUNE)
    m.register()
    m.activate()
    m._on_avatar_change(None, "/avatar/change", "avtr_first")

    def _racing_check(name):
        m.deactivate()
        return False
    bridge.osc.is_service_running = _racing_check

    m.update(m._due)
    assert bridge.osc.sent == []


# --------------------------------------------------------------------------
# Following VRCFT without an avatar change
# --------------------------------------------------------------------------
# The defect: the check ran only on /avatar/change, so a VRCFT started after the avatar
# loaded left both toggles at 0 until the next swap. `update()` now also polls the service
# every `service_poll_secs` and sends only on a transition.

POLL = 2.0
POLL_TUNE = VRCFTSettings(service_name="VRCFT", avatar_load_delay_secs=DELAY, service_poll_secs=POLL)


def _loaded(vrcft_running: bool = False):
    """A mapping whose avatar-change check has already fired, with `bridge.osc.sent` cleared.

    Returns the clock the fire happened at, so a later tick is placed relative to it.
    """
    bridge = _Bridge(vrcft_running)
    m = VRCFTMapping(bridge, tuning=POLL_TUNE)
    m.register()
    m.activate()
    m._on_avatar_change(None, "/avatar/change", "avtr_first")
    t = m._due
    m.update(t)
    assert dict(bridge.osc.sent) == (osc_vrcft.ACTIVE_PARAMS if vrcft_running
                                     else osc_vrcft.INACTIVE_PARAMS)
    bridge.osc.sent.clear()
    return bridge, m, t


def test_the_tested_module_is_this_checkouts():
    """An editable install records one absolute path, so a second checkout can import the
    first one's `src/` and pass whatever it changed (`docs/dispatched-work.md` Worktree
    mechanics). The printed path is what a run reports beside its pass count."""
    from pathlib import Path
    print("osc_vrcft.__file__ =", osc_vrcft.__file__)
    src = Path(__file__).resolve().parents[1] / "src"
    assert Path(osc_vrcft.__file__).resolve().is_relative_to(src), osc_vrcft.__file__


def test_both_toggles_go_active_when_vrcft_appears_after_the_avatar_loaded():
    bridge, m, t = _loaded(vrcft_running=False)
    bridge.osc.vrcft_running = True
    m.update(t + POLL)
    assert dict(bridge.osc.sent) == osc_vrcft.ACTIVE_PARAMS


def test_both_toggles_go_inactive_when_vrcft_disappears():
    bridge, m, t = _loaded(vrcft_running=True)
    bridge.osc.vrcft_running = False
    m.update(t + POLL)
    assert dict(bridge.osc.sent) == osc_vrcft.INACTIVE_PARAMS


def test_an_unchanged_service_is_not_resent_by_the_poll():
    bridge, m, t = _loaded(vrcft_running=True)
    for i in range(1, 6):
        m.update(t + i * POLL)
    assert bridge.osc.sent == []


def test_the_poll_waits_out_its_interval():
    """update() runs at update_hz; the service is read every `service_poll_secs`, not per tick."""
    bridge, m, t = _loaded(vrcft_running=False)
    m.update(t + POLL)                       # a poll: sees no change, schedules the next
    bridge.osc.vrcft_running = True
    m.update(t + POLL + POLL / 2)
    assert bridge.osc.sent == [], "polled again inside the interval"
    m.update(t + 2 * POLL)
    assert dict(bridge.osc.sent) == osc_vrcft.ACTIVE_PARAMS


def test_each_transition_sends_once():
    bridge, m, t = _loaded(vrcft_running=False)
    bridge.osc.vrcft_running = True
    m.update(t + POLL)
    m.update(t + 2 * POLL)
    bridge.osc.vrcft_running = False
    m.update(t + 3 * POLL)
    m.update(t + 4 * POLL)
    sent = bridge.osc.sent
    n = len(osc_vrcft.ACTIVE_PARAMS)
    assert dict(sent[:n]) == osc_vrcft.ACTIVE_PARAMS
    assert dict(sent[n:]) == osc_vrcft.INACTIVE_PARAMS
    assert len(sent) == 2 * n


def test_a_mapping_that_has_seen_no_avatar_change_follows_vrcft_after_its_first_look():
    """The first poll records the state without sending; a transition after it does send."""
    bridge = _Bridge(vrcft_running=False)
    m = VRCFTMapping(bridge, tuning=POLL_TUNE)
    m.register()
    m.activate()
    t = time.time()
    m.update(t)
    assert bridge.osc.sent == [], "the first look sent without a transition"
    bridge.osc.vrcft_running = True
    m.update(t + POLL)
    assert dict(bridge.osc.sent) == osc_vrcft.ACTIVE_PARAMS


def test_the_poll_stays_out_of_the_way_of_a_pending_avatar_change():
    """An armed deadline owns the next send: the avatar-change path looks at the world once
    the avatar is there, and a poll in the delay window would write during the load."""
    bridge, m, t = _loaded(vrcft_running=False)
    m._on_avatar_change(None, "/avatar/change", "avtr_second")
    bridge.osc.vrcft_running = True
    m.update(m._due - DELAY / 2)
    assert bridge.osc.sent == []
    m.update(m._due)
    assert dict(bridge.osc.sent) == osc_vrcft.ACTIVE_PARAMS


def test_a_disabled_mapping_does_not_poll():
    bridge, m, t = _loaded(vrcft_running=False)
    m.deactivate()
    bridge.osc.vrcft_running = True
    m.update(t + POLL)
    assert bridge.osc.sent == []


def test_an_avatar_change_arriving_during_a_poll_cancels_it():
    bridge, m, t = _loaded(vrcft_running=False)

    def _racing_check(name):
        m._on_avatar_change(None, "/avatar/change", "avtr_second")
        return True
    bridge.osc.is_service_running = _racing_check

    m.update(t + POLL)
    assert bridge.osc.sent == []


def test_the_toggles_are_sent_as_bools_not_ints():
    """Both are avatar Bool parameters; docs/osc.md has the type tag decide the dispatch, and
    `1 == True` in Python, so an equality assertion alone cannot see the difference."""
    for params in (osc_vrcft.ACTIVE_PARAMS, osc_vrcft.INACTIVE_PARAMS):
        assert all(type(v) is bool for v in params.values())


# Through the real OSCManager: its browser listener is what flips is_service_running in
# production, and a real datagram is what shows the wire type. Discovery itself stays a
# live-run concern (fake_vrchat's header).

def test_toggles_follow_the_services_browser_record_to_the_client():
    from .fake_vrchat import FakeVRChat
    from .test_target_selection import VRCFT, _ZcStub, service
    from vrbridge.osc_manager import OSCManager

    with FakeVRChat() as client, FakeVRChat() as vrcft_peer:
        mgr = OSCManager(advertise=False, target=("127.0.0.1", client.osc_port))
        listener = OSCManager._BrowserListener(mgr)
        bridge = _Bridge()
        bridge.osc = mgr
        m = VRCFTMapping(bridge, tuning=POLL_TUNE)
        m.register()
        m.activate()

        lip, eye = "/avatar/parameters/LipTrackingActive", "/avatar/parameters/EyeTrackingActive"
        m._on_avatar_change(None, "/avatar/change", "avtr_first")
        t = m._due
        m.update(t)
        assert client.wait_for_count(2)
        assert client.values_for(lip) == [False] and client.values_for(eye) == [False]

        listener.add_service(_ZcStub(service(VRCFT, vrcft_peer.http_port)),
                             "_oscjson._tcp.local.", VRCFT)
        m.update(t + POLL)
        assert client.wait_for_count(4)
        assert client.values_for(lip)[-1] is True and client.values_for(eye)[-1] is True

        listener.remove_service(None, "_oscjson._tcp.local.", VRCFT)
        m.update(t + 2 * POLL)
        assert client.wait_for_count(6)
        assert client.values_for(lip)[-1] is False and client.values_for(eye)[-1] is False
