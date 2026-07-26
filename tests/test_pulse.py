"""press_pulse must not stall its caller, and a train must stay N distinct edges.

Intended behavior, stated before the assertions because the observed behavior was
the defect: a caller queues a pulse and returns immediately; the value goes out,
is held, and is returned to 0 on a worker; consecutive pulses on one address are
separated by a real gap so an edge-triggered consumer counts N transitions.
"""
import threading
import time

import pytest

from vrbridge.utils import PULSE_QUEUE_DEPTH, drain_pulses, press_pulse


class Recorder:
    def __init__(self):
        self.events = []
        self._lock = threading.Lock()

    def send(self, addr, value):
        with self._lock:
            self.events.append((time.monotonic(), addr, value))
        return True

    def pairs(self):
        return [(a, v) for _, a, v in self.events]


@pytest.fixture(autouse=True)
def drained():
    yield
    assert drain_pulses(timeout=5.0)


def test_press_pulse_returns_before_the_hold_elapses():
    """The defect this replaces: the caller slept for the whole hold, on the one
    thread that polls controller input."""
    rec = Recorder()
    t0 = time.monotonic()
    press_pulse(rec, "/input/Voice", 1, 0.20)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.05, f"press_pulse blocked its caller for {elapsed:.3f}s"


def test_a_pulse_sends_the_value_then_zero():
    rec = Recorder()
    press_pulse(rec, "/input/Voice", 1, 0.02)
    assert drain_pulses()
    assert rec.pairs() == [("/input/Voice", 1), ("/input/Voice", 0)]


def test_the_hold_is_actually_held():
    """Intended: VRChat must observe the value for at least one frame."""
    rec = Recorder()
    press_pulse(rec, "/p", 1, 0.05)
    assert drain_pulses()
    held = rec.events[1][0] - rec.events[0][0]
    assert held >= 0.045, f"held only {held:.3f}s"


def test_a_train_stays_distinct_rather_than_coalescing():
    """Intended: two scroll steps are two increments. Sent back to back, the
    trailing 0 and the next value landed in one VRChat frame and the second
    increment was swallowed."""
    rec = Recorder()
    for _ in range(3):
        press_pulse(rec, "/avatar/parameters/VRCLFeatureToggle", 193, 0.02, gap=0.02)
    assert drain_pulses()
    assert rec.pairs() == [("/avatar/parameters/VRCLFeatureToggle", 193),
                           ("/avatar/parameters/VRCLFeatureToggle", 0)] * 3

    # every falling edge is separated from the next rising edge by the gap
    for i in range(1, len(rec.events) - 1, 2):
        assert rec.events[i + 1][0] - rec.events[i][0] >= 0.015


def test_overlapping_pulses_serialize_rather_than_interleave():
    """Intended: 1,0,1,0. The design record records the old failure as 1,1,0,0 --
    two flips inside one pulse duration racing each other's sends."""
    rec = Recorder()
    press_pulse(rec, "/p", 1, 0.05, gap=0.0)
    press_pulse(rec, "/p", 1, 0.05, gap=0.0)
    assert drain_pulses()
    assert [v for _, v in rec.pairs()] == [1, 0, 1, 0]


def test_a_failing_send_is_reported_not_swallowed():
    """Intended: off the caller's thread, nothing else would ever surface this.

    Captures on the 'vrbridge' logger directly rather than via caplog --
    setup_logging() sets propagate=False, so whether caplog sees the record
    depends on which other test ran first.
    """
    import logging

    seen = []

    class Capture(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    class Broken:
        def send(self, addr, value):
            raise RuntimeError("socket gone")

    handler = Capture()
    log = logging.getLogger("vrbridge")
    log.addHandler(handler)
    try:
        press_pulse(Broken(), "/p", 1, 0.0)
        assert drain_pulses()
    finally:
        log.removeHandler(handler)
    assert any("Pulse on /p failed" in m for m in seen), seen


def test_pulses_to_different_addresses_do_not_block_each_other():
    """A mute pulse must not queue behind a camera scroll burst.

    One shared worker made them serialize: under CameraPrefabRouter, MuteProxy and
    VRCLens are both registered, so a 1/30 s /input/Voice pulse waited out every
    queued VRCLens pulse. Before pulses were asynchronous the two ran on
    independent threads and could not interact at all.
    """
    rec = Recorder()
    for _ in range(3):
        press_pulse(rec, "/avatar/parameters/VRCLFeatureToggle", 193, 0.10, gap=0.10)
    t0 = time.monotonic()
    press_pulse(rec, "/input/Voice", 1, 0.01, gap=0.0)

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if ("/input/Voice", 0) in rec.pairs():
            break
        time.sleep(0.005)
    waited = time.monotonic() - t0
    assert ("/input/Voice", 0) in rec.pairs(), "mute pulse never completed"
    assert waited < 0.25, f"mute pulse waited {waited:.2f}s behind the camera burst"
    assert drain_pulses()


def test_the_trailing_zero_is_sent_even_if_the_rising_edge_fails():
    """A latched /input/Voice keys the mic open. The zero is the whole contract."""
    class FailsFirstSend:
        def __init__(self):
            self.sent, self.first = [], True

        def send(self, addr, value):
            if self.first:
                self.first = False
                raise RuntimeError("dropped")
            self.sent.append((addr, value))

    ctx = FailsFirstSend()
    press_pulse(ctx, "/input/Voice", 1, 0.0)
    assert drain_pulses()
    assert ctx.sent == [("/input/Voice", 0)]


def test_a_full_queue_drops_and_says_so():
    """The old synchronous press_pulse could not build a backlog -- the sleep was
    the backpressure. Dropping beats a pulse landing a second after the finger stops."""
    import logging

    seen = []

    class Capture(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    rec = Recorder()
    log = logging.getLogger("vrbridge")
    handler = Capture()
    log.addHandler(handler)
    try:
        accepted = [press_pulse(rec, "/p", 1, 0.05, gap=0.05)
                    for _ in range(PULSE_QUEUE_DEPTH + 6)]
        assert drain_pulses()
    finally:
        log.removeHandler(handler)

    assert accepted.count(False) >= 1, "queue never applied backpressure"
    assert any("Pulse queue for /p is full" in m for m in seen), seen
