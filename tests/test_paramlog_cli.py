"""The vrbridge-paramlog argument surface: the flags the design record makes claims about.

main() itself is a start-and-block loop over pieces tested elsewhere; what needs
pinning is the grammar — including that osc_target, borrowed from vrbridge.cli,
keeps its refusal semantics under this foreign parser.
"""

import re

import pytest

from vrbridge.paramlog import build_parser, default_path
from vrbridge.cli import osc_target


def test_params_is_required():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--file", "x.csv"])


def test_the_documented_two_client_invocation_parses():
    args = build_parser().parse_args(
        ["--no-advertise", "--osc-port", "9000", "--osc-bind-port", "9001",
         "--params", "SyncProbe/*", "--params", "/avatar/change",
         "--file", "a.csv"])
    assert args.params == ["SyncProbe/*", "/avatar/change"]
    assert args.no_advertise is True
    assert args.osc_port == 9000 and args.osc_bind_port == 9001


def test_osc_host_without_osc_port_is_refused_under_this_parser():
    """Intended: the borrowed osc_target keeps vrbridge.cli's ruling — a host with
    no port reads like it aimed the logger somewhere and must not silently discover."""
    parser = build_parser()
    args = parser.parse_args(["--params", "X", "--osc-host", "10.0.0.2"])
    with pytest.raises(SystemExit):
        osc_target(args, parser)


def test_osc_target_resolves_the_pinned_pair():
    parser = build_parser()
    args = parser.parse_args(["--params", "X", "--osc-port", "9010"])
    assert osc_target(args, parser) == ("127.0.0.1", 9010)
    args = parser.parse_args(["--params", "X"])
    assert osc_target(args, parser) is None


def test_default_path_is_unique_per_process():
    """Intended: two loggers launched in the same second must not share a file —
    the two-clients-one-PC run is the documented shape."""
    p = default_path()
    assert re.fullmatch(r"paramlog_\d{8}_\d{6}_\d+\.csv", p)
    assert p.rstrip(".csv").endswith(str(__import__("os").getpid()))
