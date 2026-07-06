"""
VRBridge entry point.

Starts the bridge, selects a mapping or router, and runs the event loop.

Usage:
    python main.py [--log-level INFO] [--log-callbacks] [--router {name}]

This simply wires up VRBridge + a MappingRouter and hands off to the
router's main loop. DefaultRouter is responsible for choosing among the
Puppet and UserCamera mappings and keeping MuteProxy always-on.
"""

from __future__ import annotations

import argparse
from typing import Dict, Type

from vrbridge.routers import CameraPrefabRouter, DefaultRouter, FullRouter, MappingRouter
from vrbridge import VRBridge

# Registry of available MappingRouter classes. Extend here to add new routers.
ROUTERS: Dict[str, MappingRouter] = {
    "default": DefaultRouter,
    "camera": CameraPrefabRouter,
    "remy": FullRouter,
}

DEFAULT_ROUTER = "default"

def _format_options(options: list[str]) -> str:
    """Return a human-friendly, quoted list like: 'a', 'b', or 'c'."""
    q = [f"'{o}'" for o in options]
    if not q:
        return ""
    if len(q) == 1:
        return q[0]
    return ", ".join(q[:-1]) + f", or {q[-1]}"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python main.py",
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

    router_choices = sorted(ROUTERS.keys())

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

    args = parser.parse_args(argv)

    bridge = VRBridge(
        log_level=args.log_level,
        enable_steamvr=not args.no_steamvr,
        log_callbacks=args.log_callbacks,
    )

    # Instantiate via registry
    router_cls = ROUTERS.get(args.router, DefaultRouter)
    router = router_cls(bridge)
    router.run_forever(update_hz=45)


if __name__ == "__main__":
    main()
