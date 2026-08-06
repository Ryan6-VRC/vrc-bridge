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
import time

from vrbridge import VRBridge
from vrbridge.cli import DEFAULT_OSC_HOST, _listen_port, _port, osc_target
from vrbridge.mappings.osc_paramlog import ParamLogMapping


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vrbridge-paramlog",
        description="Record whitelisted VRChat OSC parameters to a timestamped CSV.",
    )
    parser.add_argument(
        "--params", action="append", required=True, metavar="NAME_OR_GLOB",
        help=("Parameter to record; repeatable, at least one required. A bare name or "
              "glob names an avatar parameter (SyncProbe/*); a leading / names a full "
              "address (/avatar/change). Full traffic is deliberately not recordable."))
    parser.add_argument(
        "--file", default=None, metavar="CSV",
        help="Output CSV path. Default: paramlog_<timestamp>.csv in the working directory.")
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
    path = args.file or time.strftime("paramlog_%Y%m%d_%H%M%S.csv")

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

    bridge.start()
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop()
        mapping.close()
        bridge.log.info("osc_paramlog wrote %d rows to %s", mapping.rows, path)


if __name__ == "__main__":
    main()
