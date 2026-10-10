"""
VRBridge entry point.

Starts the bridge, selects a mapping or router, and runs the event loop.

Usage:
    vrbridge [--log-level INFO] [--log-callbacks] [--router {name}] [--no-steamvr]
             [--osc-port PORT [--osc-host HOST]] [--osc-bind-port PORT] [--no-advertise]
             [--log-file PATH | --no-log-file]

The three OSC flags take their host/port/bind-port shape from the standalone OSC probe
this repo is developed alongside, deliberately: both do the same job -- name the ports
of a peer that announces nothing -- and one grammar for it beats a shorter flag here.

This simply wires up VRBridge + a MappingRouter and hands off to the
router's main loop. DefaultRouter is responsible for choosing among the
Puppet and UserCamera mappings and keeping MuteProxy always-on.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from importlib.metadata import entry_points
from pathlib import Path
from typing import Dict, Type

import vrbridge
from vrbridge.logfile import attach_log_file, default_log_path, prune_logs
from vrbridge.routers import CameraPrefabRouter, DefaultRouter, MappingRouter
from vrbridge import VRBridge
from vrbridge.settings import get_config_path
from vrbridge.utils import setup_logging

#: Installed packages advertise routers under this entry-point group.
#: See README "Extending vrc-bridge".
ROUTER_ENTRY_POINT_GROUP = "vrbridge.routers"

# Routers shipped with the package. These hold classes, not instances.
ROUTERS: Dict[str, Type[MappingRouter]] = {
    "default": DefaultRouter,
    "camera": CameraPrefabRouter,
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
        help=("Logging verbosity for VRBridge and mappings, on the console and in the "
              "log file alike. Default: INFO"),
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

    parser.add_argument(
        "--no-advertise",
        action="store_true",
        help=(
            "Do not advertise over mDNS. Requires --osc-port. Needed when two clients run "
            "on one PC, or the other client's discovery also lands here."
        ),
    )

    log_dest = parser.add_mutually_exclusive_group()

    log_dest.add_argument(
        "--log-file",
        default=None,
        metavar="PATH",
        help=(
            "Append this run's log to PATH. Default: a new file per run under logs/ "
            "beside vrbridge.toml, deleted after 14 days."
        ),
    )

    log_dest.add_argument(
        "--no-log-file",
        action="store_true",
        help="Log to the console only.",
    )

    return parser


def log_file_path(args) -> Path | None:
    """Where this run logs: the file named, a new per-run file, or None for the console only."""
    if args.no_log_file:
        return None
    if args.log_file is not None:
        return Path(args.log_file).expanduser()
    return default_log_path()


def _log_thread_exception(args) -> None:
    """A `threading.excepthook` that goes through the logger, so an exception that kills a
    worker thread reaches the log file and not only a console nobody may be watching."""
    if args.exc_type is SystemExit:
        return
    name = args.thread.name if args.thread is not None else "?"
    # getLogger, not setup_logging: that one sets the level, and would take a DEBUG run
    # back to INFO the first time a thread died.
    logging.getLogger("vrbridge").error(
        "Unhandled exception in thread %s", name,
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def osc_target(args, parser: argparse.ArgumentParser) -> tuple[str, int] | None:
    """The pinned send target, or None to discover one.

    --osc-host alone is refused rather than ignored: it reads like it aimed the bridge
    somewhere, and silently discovering a different target instead is the failure that
    would take longest to see. The flag defaults to None and not to the host it resolves
    to, so that "given" is what is tested -- comparing against the default instead made
    `--osc-host 127.0.0.1` the one spelling that slipped through.

    --no-advertise alone is refused too: unadvertised and unpinned, the bridge sends
    nowhere until something is discovered, and no VRChat can discover it in turn.
    """
    if args.osc_port is None:
        if args.osc_host is not None:
            parser.error("--osc-host sets the host for --osc-port, which was not given; "
                         "without --osc-port the send target is discovered and "
                         "--osc-host has no effect.")
        if args.no_advertise:
            parser.error("--no-advertise needs --osc-port: unadvertised and unpinned, "
                         "VRChat cannot find the bridge to send to it, and the bridge "
                         "has no named port to send to.")
        return None
    host = DEFAULT_OSC_HOST if args.osc_host is None else args.osc_host
    return (host, args.osc_port)


def main(argv: list[str] | None = None) -> None:
    available = discover_routers()
    parser = build_parser(available)
    args = parser.parse_args(argv)
    # Resolved before the log file opens, so a refused flag leaves no empty file behind.
    target = osc_target(args, parser)

    log = setup_logging()
    path = log_file_path(args)
    if path is not None:
        try:
            if args.log_file is None:
                # Only our own directory: a path the user named is theirs to keep tidy.
                prune_logs(path.parent)
            attach_log_file(path)
        except OSError as exc:
            # Not fatal. The file is how a run is read afterwards; refusing to start over
            # it would trade a missing log for a missing bridge.
            log.warning("Cannot write the log file %s (%s); this run logs to the console "
                        "only.", path, exc)
            path = None
    # What ran, for whoever reads the file later: none of it is in any other line.
    config = get_config_path()
    log.info("vrbridge %s | code: %s | settings: %s%s | log file: %s",
             " ".join(sys.argv[1:] if argv is None else argv) or "(no arguments)",
             Path(vrbridge.__file__).resolve().parent, config,
             "" if config.is_file() else " (absent, so defaults)",
             path if path is not None else "none")
    threading.excepthook = _log_thread_exception

    try:
        bridge = VRBridge(
            log_level=args.log_level,
            enable_steamvr=not args.no_steamvr,
            log_callbacks=args.log_callbacks,
            advertise=not args.no_advertise,
            target=target,
            bind_port=args.osc_bind_port,
        )

        # Instantiate via registry
        router_cls = available[args.router]
        router = router_cls(bridge)
        router.run_forever(update_hz=45)
    except Exception:
        # An invalid settings file or an occupied bind port ends the run here, and on a
        # launch with no console to read, the log file is the only place that can say so.
        # Exit rather than re-raise: the traceback has already gone to both.
        log.exception("vrbridge stopped on an unhandled error")
        sys.exit(1)


if __name__ == "__main__":
    main()
