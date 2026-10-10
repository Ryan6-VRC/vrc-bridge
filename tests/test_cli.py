"""The CLI grammar for addressing a peer that does not advertise.

`build_parser` and `osc_target` are separated from `main` so that parsing can be tested
without starting a bridge and entering a router's forever-loop. The port types are
tested rather than trusted because their whole job is to convert a silent failure into a
loud one: a mistyped *send* port has nobody downstream to refuse it, so UDP swallows the
whole run.
"""
import argparse
import logging
import os
import threading
import time

import pytest

import vrbridge.cli as cli
import vrbridge.logfile as logfile
from vrbridge.cli import ROUTERS, build_parser, log_file_path, osc_target


@pytest.fixture
def parser() -> argparse.ArgumentParser:
    """The real grammar over the built-in routers; plugin discovery is tested in
    test_extension_seam.py and would only add entry-point scanning here."""
    return build_parser(dict(ROUTERS))


def test_no_osc_flags_means_discover_a_target(parser):
    """Intended: the flags are an override. Absent them, nothing about the discovery
    path changes -- which is what every existing user gets."""
    args = parser.parse_args([])
    assert osc_target(args, parser) is None
    assert args.osc_bind_port == 0


def test_a_port_alone_pins_loopback(parser):
    """Intended: the emulator is on loopback, so the common case is one flag."""
    args = parser.parse_args(["--osc-port", "9000"])
    assert osc_target(args, parser) == ("127.0.0.1", 9000)


def test_a_host_and_port_pin_that_peer(parser):
    args = parser.parse_args(["--osc-host", "192.168.1.5", "--osc-port", "9000"])
    assert osc_target(args, parser) == ("192.168.1.5", 9000)


@pytest.mark.parametrize("host", ["192.168.1.5", "127.0.0.1"])
def test_a_host_without_a_port_is_refused_rather_than_ignored(parser, host):
    """Intended: fail loud. `--osc-host` alone reads like the bridge was aimed
    somewhere; discovering a different target instead would be the slowest possible
    failure to see, because everything keeps working against the wrong peer.

    Loopback is in the parametrization because it is the spelling that hid the bug: a
    guard comparing the value against the default cannot see the flag that was given
    the default, and every host a test would reach for is the one kind that works.
    """
    args = parser.parse_args(["--osc-host", host])
    with pytest.raises(SystemExit):
        osc_target(args, parser)


@pytest.mark.parametrize("value", ["0", "70000", "-1", "nine"])
def test_an_impossible_send_port_is_refused_at_parse(parser, value):
    """Intended: reject at the boundary. Port 0 is included deliberately -- it is
    meaningful for a bind and meaningless as a destination."""
    with pytest.raises(SystemExit):
        parser.parse_args(["--osc-port", value])


def test_a_bind_port_of_zero_is_kept_as_any_free_port(parser):
    """Intended: 0 means what it has always meant on the listening side, so the
    stricter send-port rule must not leak into it."""
    assert parser.parse_args(["--osc-bind-port", "0"]).osc_bind_port == 0
    assert parser.parse_args(["--osc-bind-port", "9001"]).osc_bind_port == 9001


@pytest.mark.parametrize("value", ["70000", "-1", "nine"])
def test_an_impossible_bind_port_is_refused_at_parse(parser, value):
    with pytest.raises(SystemExit):
        parser.parse_args(["--osc-bind-port", value])


def test_the_options_reach_the_osc_manager_through_vrbridge():
    """Intended: the `VRBridge` -> `OSCManager` link, which is pure delegation.

    Named narrowly on purpose. Everything above tests the parse and
    `tests/test_target_selection.py` tests the behavior; the remaining link, `main()`
    handing its parsed args to `VRBridge`, is *not* covered here -- reaching it means
    driving `main()`, which ends in `router.run_forever()`, and the harness to stop that
    costs more than the two keyword arguments it would guard.
    """
    from vrbridge import VRBridge

    bridge = VRBridge(enable_steamvr=False, advertise=False,
                      target=("127.0.0.1", 9000), bind_port=9001)

    assert bridge.osc._client_target == ("127.0.0.1", 9000)
    assert bridge.osc._bind_port == 9001


def test_the_suite_imports_the_checkout_it_lives_in():
    """Not a product rule -- the executable half of pyproject.toml's `pythonpath` note.

    That setting is the defence against a worktree importing another checkout's `src/`
    and running green on changes it never loaded; a comment cannot notice when it stops
    working, and the resolved path is the only thing that shows it.
    """
    from pathlib import Path

    import vrbridge.osc_manager as under_test

    resolved = Path(under_test.__file__).resolve()
    print(f"vrbridge.osc_manager under test: {resolved}")
    assert resolved.is_relative_to(Path(__file__).resolve().parents[1]), (
        f"the suite imported {resolved}, which is outside this checkout")


def test_no_advertise_reaches_the_bridge_flag_with_a_pin(parser):
    """Intended: the two-clients-one-PC run -- each bridge pinned to its client's ports and
    unadvertised, so the other client's discovery does not also land here."""
    args = parser.parse_args(["--no-advertise", "--osc-port", "9000", "--osc-bind-port", "9001"])
    assert args.no_advertise is True
    assert osc_target(args, parser) == ("127.0.0.1", 9000)
    assert parser.parse_args([]).no_advertise is False


def test_no_advertise_without_a_port_is_refused(parser):
    """Intended: fail loud. Unadvertised and unpinned, VRChat cannot find the bridge and
    the bridge has nowhere named to send, so the run would do nothing and say nothing."""
    args = parser.parse_args(["--no-advertise"])
    with pytest.raises(SystemExit):
        osc_target(args, parser)


def test_a_run_logs_to_a_new_file_unless_told_otherwise(parser, tmp_path):
    """Intended: on by default from the CLI, because the incident comes before anyone
    decides to record it; a named file and no file are the two ways out."""
    default = log_file_path(parser.parse_args([]))
    assert default.parent.name == "logs" and default.name.startswith("vrbridge_")
    named = tmp_path / "bridge.log"
    assert log_file_path(parser.parse_args(["--log-file", str(named)])) == named
    assert log_file_path(parser.parse_args(["--no-log-file"])) is None


class _Boom(Exception):
    pass


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def messages(self):
        return [r.getMessage() for r in self.records]


def _stale(path):
    path.write_text("x", encoding="utf-8")
    then = time.time() - 30 * 86400
    os.utime(path, (then, then))
    return path


@pytest.fixture
def main_that_dies_at_construction(monkeypatch, bridge_logger):
    """`main()` up to the bridge, which raises: the harness that stops `run_forever` is
    still not worth building, and everything the log file needs happens before it."""
    def boom(**kwargs):
        raise _Boom("the settings file is invalid")

    monkeypatch.setattr(cli, "discover_routers", lambda: dict(ROUTERS))
    monkeypatch.setattr(cli, "VRBridge", boom)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)   # main() replaces it
    return cli.main


def test_a_run_that_dies_at_startup_says_why_in_its_log_file(tmp_path, monkeypatch,
                                                             main_that_dies_at_construction):
    """Intended: a bridge launched with no console that stops on a bad settings file or an
    occupied port leaves the reason in the file, under a line saying what was run; and the
    default directory is pruned on the way in."""
    monkeypatch.setattr(logfile, "app_base_dir", lambda: tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    stale = _stale(logs / "vrbridge_20260101_000000_1.log")

    with pytest.raises(SystemExit) as exit_:
        main_that_dies_at_construction(["--no-steamvr"])

    assert exit_.value.code == 1
    assert not stale.exists()
    (written,) = logs.glob("vrbridge_*.log")
    text = written.read_text(encoding="utf-8")
    assert "vrbridge --no-steamvr | code: " in text and str(written) in text
    assert "the settings file is invalid" in text and "Traceback" in text


def test_the_log_level_holds_from_the_first_line(tmp_path, main_that_dies_at_construction):
    """Intended: the file follows `--log-level`, with no line exempt. The startup line is
    INFO and is written before the bridge exists, so the level has to be set before it."""
    path = tmp_path / "bridge.log"
    with pytest.raises(SystemExit):
        main_that_dies_at_construction(["--log-level", "WARNING", "--log-file", str(path)])
    text = path.read_text(encoding="utf-8")
    assert " INFO " not in text, text
    assert "stopped on an unhandled error" in text


def test_a_named_log_file_prunes_nothing_beside_it(tmp_path, main_that_dies_at_construction):
    """Intended: pruning is for the directory the bridge made. A path the user named may
    sit among files that only look like ours."""
    stale = _stale(tmp_path / "vrbridge_20260101_000000_1.log")
    with pytest.raises(SystemExit):
        main_that_dies_at_construction(["--log-file", str(tmp_path / "bridge.log")])
    assert stale.exists()
    assert "the settings file is invalid" in (tmp_path / "bridge.log").read_text(encoding="utf-8")


def test_a_log_file_that_cannot_be_opened_does_not_stop_the_bridge(
        tmp_path, main_that_dies_at_construction, bridge_logger):
    """Intended: a missing log is better than a missing bridge. The run says so and goes
    on -- here, as far as the construction this harness stops at."""
    seen = _Capture()
    bridge_logger.addHandler(seen)
    in_the_way = tmp_path / "not-a-directory"
    in_the_way.write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit):
        main_that_dies_at_construction(["--log-file", str(in_the_way / "bridge.log")])
    messages = seen.messages()
    assert any("Cannot write the log file" in m for m in messages), messages
    assert any("log file: none" in m for m in messages), messages
    assert any("stopped on an unhandled error" in m for m in messages), messages


def test_a_refused_flag_leaves_no_log_file_behind(tmp_path, main_that_dies_at_construction):
    """Intended: a usage error is argparse's to report, and it is reported before the file
    opens -- a shortcut with a bad flag must not mint an empty log on every launch."""
    with pytest.raises(SystemExit) as exit_:
        main_that_dies_at_construction(["--no-advertise", "--log-file", str(tmp_path / "b.log")])
    assert exit_.value.code == 2
    assert not (tmp_path / "b.log").exists()


def test_an_exception_on_a_worker_thread_reaches_the_logger(monkeypatch, bridge_logger):
    """Intended: a timer or pulse thread that dies takes a feature with it silently; the
    default hook prints to a console nobody may be watching."""
    seen = _Capture()
    bridge_logger.addHandler(seen)
    bridge_logger.setLevel(logging.DEBUG)
    earlier = []
    monkeypatch.setattr(threading, "excepthook", cli._thread_excepthook(earlier.append))

    def die():
        raise RuntimeError("worker gone")

    t = threading.Thread(target=die, name="Doomed")
    t.start()
    t.join()
    (record,) = seen.records
    assert record.getMessage() == "Unhandled exception in thread Doomed"
    assert record.exc_info[0] is RuntimeError
    assert bridge_logger.level == logging.DEBUG, "reporting it reset the run's log level"
    assert [a.exc_type for a in earlier] == [RuntimeError], \
        "a hook installed before ours was discarded instead of chained"


def test_the_interpreters_own_thread_hook_is_not_run_a_second_time(monkeypatch, capsys,
                                                                   bridge_logger):
    """Intended: chaining is for a hook somebody installed. The default one prints the
    traceback to stderr, which the console handler has already done."""
    monkeypatch.setattr(threading, "excepthook",
                        cli._thread_excepthook(threading.__excepthook__))

    def die():
        raise RuntimeError("worker gone")

    t = threading.Thread(target=die, name="Doomed")
    t.start()
    t.join()
    assert "Exception in thread Doomed" not in capsys.readouterr().err
