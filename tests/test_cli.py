"""The CLI grammar for addressing a peer that does not advertise.

`build_parser` and `osc_target` are separated from `main` so that parsing can be tested
without starting a bridge and entering a router's forever-loop. The port types are
tested rather than trusted because their whole job is to convert a silent failure into a
loud one: a mistyped *send* port has nobody downstream to refuse it, so UDP swallows the
whole run.
"""
import argparse

import pytest

from vrbridge.cli import ROUTERS, build_parser, osc_target


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
