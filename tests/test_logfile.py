"""The log file: its name, its bounds, and that what the console shows lands in it.

Intent before test, per `docs/design.md`. The bounds are tested at their edges because each
has a wrong implementation that looks right: a cap that never rolls over still holds the
newest record, and a prune counting days as seconds still removes a month-old file.
"""
import logging
import os
import re
import time

import vrbridge.logfile as logfile
from vrbridge.logfile import attach_log_file, default_log_path, prune_logs

DAY = 86400


def aged(path, days: float):
    path.write_text("x", encoding="utf-8")
    then = time.time() - days * DAY
    os.utime(path, (then, then))
    return path


def test_the_default_file_is_per_run_beside_the_settings_file(tmp_path, monkeypatch):
    """Intended: `logs/` under the same root as `vrbridge.toml`, and a name carrying the pid
    so two bridges started in one second on one PC write two files."""
    monkeypatch.setattr(logfile, "app_base_dir", lambda: tmp_path)
    path = default_log_path()
    assert path.parent == tmp_path / "logs"
    assert re.fullmatch(rf"vrbridge_\d{{8}}_\d{{6}}_{os.getpid()}\.log", path.name), path.name


def test_the_file_holds_what_the_console_shows_in_the_same_shape(tmp_path, bridge_logger):
    """Intended: one format, so a line read in the file is the line a console would have
    shown; and written through as it is logged, with nothing waiting on a close."""
    path = tmp_path / "logs" / "run.log"
    attach_log_file(path)
    bridge_logger.info("OSC target set to %s", "127.0.0.1:9000")
    bridge_logger.debug("below the logger's level")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, lines
    assert re.fullmatch(r"\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}\] INFO vrbridge: "
                        r"OSC target set to 127\.0\.0\.1:9000", lines[0]), lines[0]


def test_a_second_run_opens_its_file_in_the_directory_the_first_made(tmp_path, bridge_logger):
    """Intended: every launch after the first finds `logs/` already there."""
    attach_log_file(tmp_path / "logs" / "first.log")
    attach_log_file(tmp_path / "logs" / "second.log")
    bridge_logger.info("both")
    assert "both" in (tmp_path / "logs" / "second.log").read_text(encoding="utf-8")


def test_a_named_file_is_appended_to_not_replaced(tmp_path, bridge_logger):
    """Intended: `--log-file PATH` across restarts keeps the session before this one, which
    is the session an incident is usually in."""
    path = tmp_path / "bridge.log"
    path.write_text("yesterday\n", encoding="utf-8")
    attach_log_file(path)
    bridge_logger.info("today")
    text = path.read_text(encoding="utf-8")
    assert text.startswith("yesterday\n") and "today" in text


def test_attaching_to_a_bare_logger_keeps_the_console(tmp_path, bridge_logger):
    """Intended: an embedder may attach the file before building a VRBridge. setup_logging
    adds the console handler only to a logger with none, so the file must not be the
    handler that makes the logger look configured."""
    for h in list(bridge_logger.handlers):
        bridge_logger.removeHandler(h)
    attach_log_file(tmp_path / "run.log")
    kinds = [type(h) for h in bridge_logger.handlers]
    assert kinds.count(logging.StreamHandler) == 1, kinds
    assert len(kinds) == 2, kinds


def test_a_run_past_the_cap_keeps_its_newest_lines_in_two_files(tmp_path, monkeypatch,
                                                                bridge_logger):
    """Intended: the cap bounds a run at the live file plus one rollover, and what is
    discarded is the oldest. Asserted on the rollover and the loss, not on the newest
    record -- an uncapped file holds the newest record too."""
    monkeypatch.setattr(logfile, "MAX_BYTES", 600)
    path = tmp_path / "run.log"
    attach_log_file(path)
    for i in range(60):
        bridge_logger.info("record %03d", i)
    live = path.read_text(encoding="utf-8")
    rolled = (tmp_path / "run.log.1").read_text(encoding="utf-8")
    assert not (tmp_path / "run.log.2").exists()
    assert "record 059" in live
    assert "record 000" not in live + rolled, "the oldest line survived the rollovers"
    assert len(live.encode()) <= 600 and len(rolled.encode()) <= 600


def test_a_rollover_the_os_refuses_loses_no_record(tmp_path, monkeypatch, bridge_logger):
    """Intended: Windows refuses the rollover's rename while another process holds the file
    open, which is exactly when someone is reading it. The stock handler then drops every
    record until the reader lets go; ours writes on past the cap instead."""
    monkeypatch.setattr(logfile, "MAX_BYTES", 600)
    path = tmp_path / "run.log"
    attach_log_file(path)

    def refused(src, dst):
        raise PermissionError(32, "The process cannot access the file", str(src))

    with monkeypatch.context() as m:
        m.setattr(os, "rename", refused)
        for i in range(60):
            bridge_logger.info("record %03d", i)
    live = path.read_text(encoding="utf-8")
    assert all(f"record {i:03d}" in live for i in range(60))


def test_prune_takes_only_our_own_old_files(tmp_path):
    """Intended: 14 days, measured in days, over the names a run produces and nothing else
    that happens to live in the directory."""
    old = aged(tmp_path / "vrbridge_20260101_000000_1.log", 15)
    old_rolled = aged(tmp_path / "vrbridge_20260101_000000_1.log.1", 15)
    recent = aged(tmp_path / "vrbridge_20260102_000000_2.log", 13)
    foreign = aged(tmp_path / "notes.log", 400)
    lookalike = aged(tmp_path / "vrbridge_notes.logbook", 400)
    assert prune_logs(tmp_path) == 2
    assert not old.exists() and not old_rolled.exists()
    assert recent.exists() and foreign.exists() and lookalike.exists()


def test_prune_of_a_directory_not_made_yet_is_nothing(tmp_path):
    """Intended: the first run prunes before `logs/` exists."""
    assert prune_logs(tmp_path / "logs") == 0


def test_prune_survives_a_file_it_cannot_remove(tmp_path, monkeypatch):
    """Intended: a file another running bridge holds open stays, and the start goes on."""
    held = aged(tmp_path / "vrbridge_20260101_000000_1.log", 15)

    def refused(self, *a, **k):
        raise PermissionError(32, "The process cannot access the file", str(self))

    monkeypatch.setattr(type(held), "unlink", refused)
    assert prune_logs(tmp_path) == 0
    assert held.exists()
