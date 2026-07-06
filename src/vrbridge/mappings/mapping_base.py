from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Callable, Dict

from vrbridge import VRBridge


class Mapping(ABC):
    """
    Base class for a controller-to-OSC mapping.

    Lifecycle:
      * register(bridge): attach all callbacks (only once).
      * activate(): enable this mapping's behavior.
      * deactivate(): disable this mapping's behavior.
      * update(now): optional periodic tick (router will call if defined).

    Notes:
      - We don't require VRBridge to support callback removal; instead, each
        mapping "gates" its callbacks behind self.enabled.
    """
    name: str = "mapping"

    def __init__(self, bridge: VRBridge):
        self.bridge = bridge
        self.enabled: bool = False
        self._registered: bool = False

    # ---- lifecycle --------------------------------------------------------

    def register(self) -> None:
        """Attach callbacks (idempotent). Subclasses should call super()."""
        if self._registered:
            return
        self._registered = True

    def activate(self) -> None:
        """Enable behavior."""
        self.enabled = True
        self.bridge.log.info("Activated mapping: %s", self.name)

    def deactivate(self) -> None:
        """Disable behavior."""
        if self.enabled:
            self.bridge.log.info("Deactivated mapping: %s", self.name)
        self.enabled = False

    def update(self, now: float) -> None:
        """Optional periodic tick."""
        # Subclasses may implement.
        return

    # ---- gating helpers ---------------------------------------------------

    def _gate(self, fn: Callable):
        """Wrap a callback; skip when mapping is disabled."""
        def wrapped(*args, **kwargs):
            if not self.enabled:
                return
            return fn(*args, **kwargs)
        return wrapped


class MappingRouter(ABC):
    """
    Abstract base for mapping routers.

    A router wires up mappings, decides which ones should be enabled/disabled
    based on external state, and drives their update() calls.
    """
    def __init__(self, bridge: VRBridge):
        self.bridge = bridge
        # Central registry for all mappings this router owns.
        self._mappings: Dict[str, Mapping] = {}

    def register(self, mapping: "Mapping") -> None:
        """Register (and .register()) a mapping with the router."""
        mapping.register()
        self._mappings[mapping.name] = mapping

    def run_forever(self, *, update_hz: float = 10.0):
        """Start I/O and enter the routing loop."""
        self.bridge.start()
        try:
            # Ensure a valid starting state before entering the loop
            self.evaluate()

            tick = 1.0 / max(1.0, float(update_hz))
            while True:
                if self.bridge.controllers and self.bridge.controllers.quit_requested():
                    break

                # Tick all mappings; each mapping decides whether to act.
                now = time.time()
                for m in list(self._mappings.values()):
                    try:
                        m.update(now)
                    except Exception:
                        # Don't crash the loop if a mapping throws in update()
                        pass

                time.sleep(tick)
        except KeyboardInterrupt:
            pass
        finally:
            self.bridge.stop()

    @abstractmethod
    def evaluate(self) -> None:
        """
        Decide which mappings should be enabled/disabled and apply it.
        Subclasses implement this policy and typically use self._mappings.
        """
        ...
