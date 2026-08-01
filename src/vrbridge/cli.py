"""
VRBridge entry point.

Starts the bridge, selects a mapping or router, and runs the event loop.

Usage:
    vrbridge [--log-level INFO] [--log-callbacks] [--router {name}] [--no-steamvr]
             [--osc-port PORT [--osc-host HOST]] [--osc-bind-port PORT]

The three OSC flags take their host/port/bind-port shape from the standalone OSC probe
this repo is developed alongside, deliberately: both do the same job -- name the ports
of a peer that announces nothing -- and one grammar for it beats a shorter flag here.

This simply wires up VRBridge + a MappingRouter and hands off to the
router's main loop. DefaultRouter is responsible for choosing among the
Puppet and UserCamera mappings and keeping MuteProxy always-on.
"""

from __future__ import annotations

import argparse
from importlib.metadata import entry_points
from typing import Dict, Type

from vrbridge.routers import CameraPrefabRouter, DefaultRouter, FullRouter, MappingRouter
from vrbridge import VRBridge
from vrbridge.utils import setup_logging

#: Installed packages advertise routers under this entry-point group.
#: See README "Extending vrc-bridge".
ROUTER_ENTRY_POINT_GROUP = "vrbridge.routers"

# Routers shipped with the package. These hold classes, not instances.
ROUTERS: Dict[str, Type[MappingRouter]] = {
    "default": DefaultRouter,
    "camera": CameraPrefabRouter,
    "remy": FullRouter,
}

DEFAULT_ROUTER = "default"

#: Where --osc-port sends when --osc-host is not given. Not argparse's default for that
#: flag: osc_target needs to tell "not given" from "given this value".
DEFAULT_OSC_HOST = "127.0.0.1"


def discover_routers() -> Dict[str, Type[MappingRouter]]:
    """The built-in routers plus any an installed package advertises.

    A plugin that fails to load is named and skipped rather than taking the CLI
    down with it -- but it is never silently absent, because "my router did not
    show up in --help", with nothing said, is not diagnosable from outside.
    A plugin may not shadow a built-in name.
    """
    log = setup_logging()
    found: Dict[str, Type[MappingRouter]] = dict(ROUTERS)
    for ep in entry_points(group=ROUTER_ENTRY_POINT_GROUP):
        if ep.name in ROUTERS:
            log.warning("Ignoring router plugin %r from %s: that name is built in.",
                        ep.name, ep.value)
            continue
        try:
            cls = ep.load()
        except Exception as exc:
            log.warning("Router plugin %r (%s) failed to import and was skipped: %s",
                        ep.name, ep.value, exc)
            continue
        if not (isinstance(cls, type) and issubclass(cls, MappingRouter)):
            log.warning("Router plugin %r (%s) is not a MappingRouter subclass; skipped.",
                        ep.name, ep.value)
            continue
        found[ep.name] = cls
    return found


def _port(value: str) -> int:
    """An argparse type rejecting a port nothing downstream will.

    A mistyped *send* port raises nowhere -- UDP has nobody to refuse it -- so it is
    either caught here or it is silence for the whole run.
    """
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a port number") from None
    if not 1 <= n <= 65535:
        raise argparse.ArgumentTypeError(f"port {n} is outside 1-65535")
    return n


def _listen_port(value: str) -> int:
    """Same, except that 0 is meaningful here: bind any free port, which is the default."""
    if value.strip() == "0":
        return 0
    return _port(value)


def _format_options(options: list[str]) -> str:
    """Return a human-friendly, quoted list like: 'a', 'b', or 'c'."""
    q = [f"'{o}'" for o in options]
    if not q:
        return ""
    if len(q) == 1:
        return q[0]
    return ", ".join(q[:-1]) + f", or {q[-1]}"


def build_parser(available: Dict[str, Type[MappingRouter]]) -> argparse.ArgumentParser:
    """The CLI grammar, built apart from main() so it can be parsed without running."""
    parser = argparse.ArgumentParser(
        prog="vrbridge",
        description="Run the VRBridge default mapping router.",
    )

    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity for VRBridge and mappings. Default: INFO",
    )

    parser.add_argument(
        "--log-callbacks",
        action="store_true",
        help="Log each callback invocation (verbose).",
    )

    router_choices = sorted(available)

    parser.add_argument(
        "--router",
        default=DEFAULT_ROUTER,
        choices=router_choices,
        help=(
            f"Select mapping router: {_format_options(router_choices)}. "
            f"Default: {DEFAULT_ROUTER}"
        ),
    )

    parser.add_argument(
        "--no-steamvr",
        action="store_true",
        help="Run in desktop mode without SteamVR controller support.",
    )

    parser.add_argument(
        "--osc-port",
        type=_port,
        default=None,
        metavar="PORT",
        help=(
            "Send to this OSC port instead of discovering a target, and stop discovery "
            "from ever revising it. The Av3Emulator listens on 9000 and announces "
            "nothing, so it is reachable no other way."
        ),
    )

    parser.add_argument(
        "--osc-host",
        default=None,
        metavar="HOST",
        help=(
            f"Host for --osc-port. Sends only: the listener and the served tree stay on "
            f"loopback, so a peer off this machine can be sent to and cannot answer. "
            f"Default: {DEFAULT_OSC_HOST}"
        ),
    )

    parser.add_argument(
        "--osc-bind-port",
        type=_listen_port,
        default=0,
        metavar="PORT",
        help=(
            "Bind the OSC listener to this port instead of a free one. A peer that "
            "cannot read our OSCQuery cannot learn a floating port; the emulator sends "
            "to 9001. Default: 0, any free port."
        ),
    )

    return parser


def osc_target(args, parser: argparse.ArgumentParser) -> tuple[str, int] | None:
    """The pinned send target, or None to discover one.

    --osc-host alone is refused rather than ignored: it reads like it aimed the bridge
    somewhere, and silently discovering a different target instead is the failure that
    would take longest to see. The flag defaults to None and not to the host it resolves
    to, so that "given" is what is tested -- comparing against the default instead made
    `--osc-host 127.0.0.1` the one spelling that slipped through.
    """
    if args.osc_port is None:
        if args.osc_host is not None:
            parser.error("--osc-host sets the host for --osc-port, which was not given; "
                         "without --osc-port the send target is discovered and "
                         "--osc-host has no effect.")
        return None
    host = DEFAULT_OSC_HOST if args.osc_host is None else args.osc_host
    return (host, args.osc_port)


def main(argv: list[str] | None = None) -> None:
    available = discover_routers()
    parser = build_parser(available)
    args = parser.parse_args(argv)

    bridge = VRBridge(
        log_level=args.log_level,
        enable_steamvr=not args.no_steamvr,
        log_callbacks=args.log_callbacks,
        target=osc_target(args, parser),
        bind_port=args.osc_bind_port,
    )

    # Instantiate via registry
    router_cls = available[args.router]
    router = router_cls(bridge)
    router.run_forever(update_hz=45)


if __name__ == "__main__":
    main()
