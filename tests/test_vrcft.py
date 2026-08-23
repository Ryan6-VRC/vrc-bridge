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
    """A registered, activated mapping with one avatar change already delivered."""
    bridge = _Bridge(vrcft_running)
    m = VRCFTMapping(bridge, tuning=TUNE)
    m.register()
    m.activate()
    t0 = time.time()
    m._on_avatar_change(None, "/avatar/change", "avtr_first")
    return bridge, m, t0


# --------------------------------------------------------------------------
# The defect
# --------------------------------------------------------------------------

def test_a_handler_registered_behind_vrcft_is_not_delayed_by_its_load_delay():
    """The reason this mapping has a tick at all.

    `engine._on_osc_event` runs one address's handlers serially on the datagram thread,
    so the old inline `time.sleep` was charged to everything registered behind this
    mapping -- measured at 1.001 s for a handler behind it, which once got read as that
    handler's own latency. Driven through the real fanout, because a stub of it could not
    fail the way production did.
    """
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
        assert ran_at[0] - t0 < DELAY / 10, (
            f"handler behind osc_vrcft ran {ran_at[0] - t0:.3f}s late; it is paying "
            "osc_vrcft's load delay again")
        assert elapsed < DELAY / 10, f"the fanout itself blocked for {elapsed:.3f}s"
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
    bridge, m, t0 = _armed()
    m.update(t0 + DELAY / 2)
    assert bridge.osc.sent == []


def test_the_inactive_set_is_sent_on_the_first_tick_past_the_delay():
    bridge, m, t0 = _armed(vrcft_running=False)
    m.update(t0 + DELAY + 0.01)
    assert dict(bridge.osc.sent) == osc_vrcft.INACTIVE_PARAMS


def test_the_active_set_is_sent_when_vrcft_is_running():
    bridge, m, t0 = _armed(vrcft_running=True)
    m.update(t0 + DELAY + 0.01)
    assert dict(bridge.osc.sent) == osc_vrcft.ACTIVE_PARAMS


def test_the_service_is_checked_at_fire_time_not_at_arm_time():
    """VRCFT can come up during the load delay; the whole point of waiting is to look
    at the world as it is once the avatar is there."""
    bridge, m, t0 = _armed(vrcft_running=False)
    bridge.osc.vrcft_running = True
    m.update(t0 + DELAY + 0.01)
    assert dict(bridge.osc.sent) == osc_vrcft.ACTIVE_PARAMS


def test_the_send_fires_once_and_not_on_every_later_tick():
    """An un-cleared deadline would re-send at update_hz -- 45 times a second."""
    bridge, m, t0 = _armed()
    for i in range(5):
        m.update(t0 + DELAY + 0.01 + i)
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
    t0 = time.time()
    gated(None, "/avatar/change", "avtr_first")
    m.activate()
    m.update(t0 + DELAY + 0.01)
    assert bridge.osc.sent == []


def test_deactivating_between_arming_and_firing_drops_the_send():
    bridge, m, t0 = _armed()
    m.deactivate()
    m.update(t0 + DELAY + 0.01)
    assert bridge.osc.sent == []
    # And the dropped deadline does not fire on re-activation.
    m.activate()
    m.update(t0 + DELAY + 1.0)
    assert bridge.osc.sent == []
