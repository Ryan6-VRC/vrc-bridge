"""The parameter logger: whitelist admission, CSV rows, and the pattern seam.

Intent first (design record §The parameter logger): the whitelist is required,
patterns admit named shapes of traffic without enumerating anything, and the
logged stream is the change-filtered stream — one row per change, repeats and
the doubled inbound delivery folded away.
"""

import csv
import time

import pytest

from vrbridge import VRBridge
from vrbridge.mappings.osc_paramlog import ParamLogMapping, to_address


def _bridge():
    return VRBridge(enable_steamvr=False, advertise=False, discover=False)


def _send(port, address, value):
    from pythonosc import udp_client
    udp_client.SimpleUDPClient("127.0.0.1", port).send_message(address, value)


def _wait_rows(mapping, n, timeout=2.0):
    deadline = time.monotonic() + timeout
    while mapping.rows < n and time.monotonic() < deadline:
        time.sleep(0.01)
    return mapping.rows


def _read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.reader(fh))


def test_an_empty_whitelist_is_refused():
    """Intended: the whitelist is required — log-everything is not offerable, both
    for noise and because parameter discovery is descoped (design record)."""
    with pytest.raises(ValueError):
        ParamLogMapping(_bridge(), params=[], path="never_created.csv")


def test_spec_forms_resolve_to_addresses():
    """Intended: bare names are avatar parameters; a leading / is a full address."""
    assert to_address("SyncProbe/R05") == "/avatar/parameters/SyncProbe/R05"
    assert to_address("/avatar/change") == "/avatar/change"


def test_exact_name_lands_as_a_csv_row(tmp_path):
    out = tmp_path / "log.csv"
    bridge = _bridge()
    m = ParamLogMapping(bridge, params=["Thing"], path=out)
    m.register()
    m.activate()
    bridge.osc.start()
    try:
        _send(bridge.osc.osc_port, "/avatar/parameters/Thing", 0.5)
        assert _wait_rows(m, 1) == 1
    finally:
        bridge.osc.stop()
        m.close()
    header, row = _read_csv(out)
    assert header == ["time", "address", "value", "type"]
    assert row[1] == "/avatar/parameters/Thing"
    assert float(row[2]) == pytest.approx(0.5)
    assert row[3] == "float"
    assert abs(float(row[0]) - time.time()) < 60


def test_a_glob_admits_matching_names_and_nothing_else(tmp_path):
    """Intended: the pattern seam admits the named shape of traffic; addresses
    outside every pattern never reach the file."""
    out = tmp_path / "log.csv"
    bridge = _bridge()
    m = ParamLogMapping(bridge, params=["SyncProbe/*", "/avatar/change"], path=out)
    m.register()
    m.activate()
    bridge.osc.start()
    try:
        _send(bridge.osc.osc_port, "/avatar/parameters/SyncProbe/Rx/R05", 0.25)
        _send(bridge.osc.osc_port, "/avatar/parameters/Unrelated", 1.0)
        _send(bridge.osc.osc_port, "/avatar/change", "avtr_x")
        assert _wait_rows(m, 2) == 2
    finally:
        bridge.osc.stop()
        m.close()
    rows = _read_csv(out)[1:]
    addresses = [r[1] for r in rows]
    assert "/avatar/parameters/SyncProbe/Rx/R05" in addresses
    assert "/avatar/change" in addresses
    assert not any("Unrelated" in a for a in addresses)


def test_a_repeated_identical_value_is_one_row(tmp_path):
    """Intended (a pin on the shared change filter, stated as logger behavior):
    only transitions are rows. Sends are sequenced through the row count because
    thread-per-datagram dispatch does not preserve processing order between
    unsynchronized sends; the fold of the doubled inbound delivery is the design
    record's inference from this same filter, not separately exercised here."""
    out = tmp_path / "log.csv"
    bridge = _bridge()
    m = ParamLogMapping(bridge, params=["Thing"], path=out)
    m.register()
    m.activate()
    bridge.osc.start()
    try:
        _send(bridge.osc.osc_port, "/avatar/parameters/Thing", 1.0)
        assert _wait_rows(m, 1) == 1
        _send(bridge.osc.osc_port, "/avatar/parameters/Thing", 1.0)
        time.sleep(0.1)  # the repeat would land within this window if it were coming
        assert m.rows == 1
        _send(bridge.osc.osc_port, "/avatar/parameters/Thing", 2.0)
        assert _wait_rows(m, 2) == 2
    finally:
        bridge.osc.stop()
        m.close()
    values = [float(r[2]) for r in _read_csv(out)[1:]]
    assert values == [pytest.approx(1.0), pytest.approx(2.0)]


def test_an_overlapping_whitelist_is_still_one_row_per_change(tmp_path):
    """Intended: the engine dedupes one callback object across exact and pattern
    registrations, so naming a channel AND one of its members — the most natural
    whitelist — does not double the apparent rate the log exists to measure."""
    out = tmp_path / "log.csv"
    bridge = _bridge()
    m = ParamLogMapping(bridge, params=["SyncProbe/*", "SyncProbe/Reset", "SyncProbe/Rx/*"],
                        path=out)
    m.register()
    m.activate()
    bridge.osc.start()
    try:
        _send(bridge.osc.osc_port, "/avatar/parameters/SyncProbe/Reset", True)
        assert _wait_rows(m, 1) == 1
        _send(bridge.osc.osc_port, "/avatar/parameters/SyncProbe/Rx/R05", 0.5)
        assert _wait_rows(m, 2) == 2
        time.sleep(0.1)  # duplicates would land within this window
        assert m.rows == 2
    finally:
        bridge.osc.stop()
        m.close()
    assert len(_read_csv(out)) == 3  # header + one row per change


def test_rows_after_close_are_dropped_not_raised(tmp_path):
    """Intended: teardown races (a datagram in flight at Ctrl-C) end quietly."""
    out = tmp_path / "log.csv"
    bridge = _bridge()
    m = ParamLogMapping(bridge, params=["Thing"], path=out)
    m.register()
    m.activate()
    m.close()
    m._on_value(None, "/avatar/parameters/Thing", 1.0)  # must not raise
    assert m.rows == 0
