"""
VRBridge entry point.

Starts the bridge, selects a mapping or router, and runs the event loop.

Usage:
    vrbridge [--log-level INFO] [--log-callbacks] [--router {name}] [--no-steamvr]

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

    available = discover_routers()
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

    args = parser.parse_args(argv)

    bridge = VRBridge(
        log_level=args.log_level,
        enable_steamvr=not args.no_steamvr,
        log_callbacks=args.log_callbacks,
    )

    # Instantiate via registry
    router_cls = available[args.router]
    router = router_cls(bridge)
    router.run_forever(update_hz=45)


if __name__ == "__main__":
    main()
