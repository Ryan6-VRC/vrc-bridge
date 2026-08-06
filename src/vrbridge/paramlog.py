"""`vrbridge-paramlog`: run the parameter logger standalone.

A thin second console script rather than a router: the logger is a recording
instrument with no mapping selection to do, and a measurement run wants the
smallest possible process around it. SteamVR never starts here.

Two-clients-one-PC runs (each VRChat launched with `--osc=inPort:ip:outPort`)
run one logger per client: `--osc-port <inPort> --osc-bind-port <outPort>`
names that client's fixed ports, and `--no-advertise` keeps the *other*
client's discovery from also pushing its parameters into this log — an
advertised listener receives from whoever finds it (docs/design.md §Target
selection: a pin governs the send side only).
"""

from __future__ import annotations

import argparse
import os
import time

from vrbridge import VRBridge
from vrbridge.cli import DEFAULT_OSC_HOST, _listen_port, _port, osc_target
from vrbridge.mappings.osc_paramlog import ParamLogMapping


def default_path() -> str:
    """Second resolution alone collides when two loggers launch together —
    the documented two-clients-one-PC shape — and "w" mode makes the loser
    silent. The pid keeps them apart."""
    return time.strftime(f"paramlog_%Y%m%d_%H%M%S_{os.getpid()}.csv")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vrbridge-paramlog",
        description="Record whitelisted VRChat OSC parameters to a timestamped CSV.",
    )
    parser.add_argument(
        "--params", action="append", required=True, metavar="NAME_OR_GLOB",
        help=("Parameter to record; repeatable, at least one required. A bare name or "
              "glob names an avatar parameter (SyncProbe/*); a leading / names a full "
              "address (/avatar/change). Globs are fnmatch, so * crosses / segments, "
              "and a name containing ? or [ is still recorded under its literal "
              "spelling as well. There is no log-everything default: you name the "
              "shapes you record — /* is how you spell all of them."))
    parser.add_argument(
        "--file", default=None, metavar="CSV",
        help="Output CSV path. Default: paramlog_<timestamp>_<pid>.csv in the working "
             "directory (the pid keeps two same-second launches apart).")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    parser.add_argument("--osc-port", type=_port, default=None, metavar="PORT",
                        help="Send to this OSC port instead of discovering a target "
                             "(sends matter only for OSC-driven resets).")
    parser.add_argument("--osc-host", default=None, metavar="HOST",
                        help=f"Host for --osc-port. Default: {DEFAULT_OSC_HOST}")
    parser.add_argument("--osc-bind-port", type=_listen_port, default=0, metavar="PORT",
                        help="Bind the listener to this port instead of a free one — the "
                             "port a --osc-launched client already sends to. Default: 0.")
    parser.add_argument("--no-advertise", action="store_true",
                        help="Do not advertise over mDNS. Required when two clients run "
                             "on one PC, or the other client's discovery also lands here.")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    path = args.file or default_path()

    bridge = VRBridge(
        log_level=args.log_level,
        enable_steamvr=False,
        advertise=not args.no_advertise,
        target=osc_target(args, parser),
        bind_port=args.osc_bind_port,
    )
    mapping = ParamLogMapping(bridge, params=args.params, path=path)
    mapping.register()
    mapping.activate()

    try:
        try:
            # Inside the try: an occupied --osc-bind-port raises the named OSError
            # here (the likeliest two-client-run failure), and the header-only CSV
            # still gets closed and the row count still gets reported.
            #
            # stop() unconditionally, including after a failed start(): start() brings
            # the HTTP server up before the OSC bind that raises, and OSCManager's
            # contract is that stop() walks each block independently and takes down
            # whichever ones came up. Guarding on a `started` flag instead leaked the
            # HTTP thread and its socket for anything calling main() in-process.
            bridge.start()
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            bridge.stop()
    finally:
        mapping.close()
        bridge.log.info("osc_paramlog wrote %d rows to %s", mapping.rows, path)


if __name__ == "__main__":
    main()
