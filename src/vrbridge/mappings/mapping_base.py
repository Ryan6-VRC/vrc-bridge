from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Callable, Dict

from vrbridge import VRBridge


class Mapping(ABC):
    """
    Base class for a controller-to-OSC mapping.

    Lifecycle:
      * register(): attach all callbacks, exactly once. Subclasses override
        _attach(), not this.
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
        """Attach callbacks. Idempotent: a second call is a no-op.

        Do not override. Subclasses put their bindings in _attach(), which this
        calls exactly once.

        The guard used to live here while every subclass overrode register(),
        called super() -- which returned None whether or not it had already run --
        and then bound its callbacks unconditionally. VRBridge.on_controller
        appends without de-duplicating, so registering one mapping twice bound
        every callback twice and each press fired twice: two photos per capture,
        and a toggle that flipped and flipped back. No router does that today, but
        the entry-point seam hands register() to third parties, and a contract
        that says "idempotent" has to be one.
        """
        if self._registered:
            return
        self._registered = True
        self._attach()

    def __init_subclass__(cls, **kwargs):
        """Refuse a subclass that overrides register().

        The docstring above says "do not override"; the entry-point seam hands this
        base to third parties, so it has to be enforced rather than requested.
        """
        super().__init_subclass__(**kwargs)
        if "register" in cls.__dict__:
            raise TypeError(
                f"{cls.__name__} overrides Mapping.register(), which must stay idempotent. "
                "Put your bindings in _attach() instead -- register() calls it exactly once.")

    def _attach(self) -> None:
        """Subclass hook: attach callbacks. Called exactly once, from register()."""
        return

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
    #: Seconds between repeat reports of one mapping's failing update().
    UPDATE_FAILURE_REPORT_GAP: float = 10.0

    def __init__(self, bridge: VRBridge):
        self.bridge = bridge
        # Central registry for all mappings this router owns.
        self._mappings: Dict[str, Mapping] = {}
        # name -> [failure count, last time we reported it]
        self._update_failures: Dict[str, list] = {}

    def register(self, mapping: "Mapping") -> None:
        """Register (and .register()) a mapping with the router."""
        mapping.register()
        self._mappings[mapping.name] = mapping

    def _report_update_failure(self, name: str, now: float) -> None:
        """Report a throwing update() on the first failure, then at a bounded rate.

        Swallowing it hid a mapping that threw on every tick. Logging every tick
        would emit at update_hz and bury everything else, so repeats are rate
        limited and carry the running count.
        """
        st = self._update_failures.setdefault(name, [0, 0.0])
        st[0] += 1
        if st[0] == 1 or (now - st[1]) >= self.UPDATE_FAILURE_REPORT_GAP:
            self.bridge.log.exception(
                "Mapping %s raised in update() (%d time(s) so far). It stays registered "
                "and enabled; its periodic behavior is not running.", name, st[0])
            st[1] = now

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
                        # One mapping must not take down the loop -- but it must
                        # not vanish either (CLAUDE.md rule 7).
                        self._report_update_failure(m.name, now)

                time.sleep(tick)
        except KeyboardInterrupt:
            pass
        finally:
            # VRBridge.stop() owns the pulse drain -- it is the only place that can
            # order it between the controller thread stopping and OSC going down.
            self.bridge.stop()

    @abstractmethod
    def evaluate(self) -> None:
        """
        Decide which mappings should be enabled/disabled and apply it.
        Subclasses implement this policy and typically use self._mappings.
        """
        ...
