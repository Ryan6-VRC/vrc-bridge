from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Literal, Optional

from .controller_manager import ControllerEvent, ControllerManager
from .osc_manager import FetchResult, OSCManager
from .utils import drain_pulses, setup_logging

Hand = Literal["left", "right", "both"]

class ControllerEventType:
    # Touchpad (Index Knuckles trackpad)
    TOUCHPAD_VSCROLL       = "touchpad.vscroll"
    TOUCHPAD_HSCROLL       = "touchpad.hscroll"
    TOUCHPAD_PRESS         = "touchpad.press"
    TOUCHPAD_RELEASE       = "touchpad.release"
    TOUCHPAD_SHORT_PRESS   = "touchpad.short_press"
    TOUCHPAD_LONG_PRESS    = "touchpad.long_press"
    TOUCHPAD_SCROLL_RAW    = "touchpad.scroll_raw"
    TOUCHPAD_TOUCH         = "touchpad.touch"
    TOUCHPAD_LIFT          = "touchpad.lift"

    # Thumbstick / Joystick (Knuckles thumbstick, Quest/Oculus joystick)
    THUMBSTICK_PRESS       = "thumbstick.press"
    THUMBSTICK_RELEASE     = "thumbstick.release"
    THUMBSTICK_SHORT_PRESS = "thumbstick.short_press"
    THUMBSTICK_LONG_PRESS  = "thumbstick.long_press"

@dataclass
class CallbackContext:
    """Minimal surface exposed to callbacks.
    Runtime mutation of watched addresses is intentionally not supported.
    """
    osc: OSCManager
    def get(self, address: str, default=None): return self.osc.get_cached(address, default)
    def send(self, address: str, value) -> bool:
        """Send, returning False if it was dropped (no target yet, or a socket error).

        A caller that mirrors what it sends needs this to decide whether to
        advance that mirror; discarding it is how ParamState drifted from the avatar.
        """
        return self.osc.send(address, value)
    def fetch(self, address: str, timeout: float = 2.0) -> FetchResult:
        """Read one parameter node's live VALUE off the peer's OSCQuery server.

        Blocking, and OSCManager.fetch's docstring owns which threads may pay for it.
        Unlike `get`, this asks the avatar rather than reading what we happen to have
        seen -- which is the difference that matters at startup, when the cache is empty
        because no value has changed since we began listening.
        """
        return self.osc.fetch(address, timeout=timeout)

class VRBridge:
    """Orchestrates OSC I/O and SteamVR controller events.
    Users register all callbacks up-front with `on_osc`/`on_controller` and then call `start()`.

    `target=` and `bind_port=` pass straight to OSCManager, and are how a peer that
    advertises nothing -- the Av3Emulator -- is addressed; its docstring holds the rules.
    """
    def __init__(self, *, log_level: str = "INFO", vr_background=True, enable_steamvr: bool = True,
                 advertise=True, log_callbacks: bool = False,
                 target: tuple[str, int] | None = None, bind_port: int = 0,
                 discover: bool = True):
        import logging as _logging
        self.log = setup_logging(level=getattr(_logging, log_level))
        self.osc = OSCManager(logger=self.log, advertise=advertise,
                              target=target, bind_port=bind_port, discover=discover)

        self.controllers: Optional[ControllerManager] = None
        if enable_steamvr:
            self.controllers = ControllerManager(logger=self.log, vr_background=vr_background)
            self.controllers.set_listener(self._on_controller_event)
        else:
            self.log.info("SteamVR is disabled.")

        self._osc_callbacks: Dict[str, list[Callable[[CallbackContext, str, Any], None]]] = {}
        self._ctl_callbacks: Dict[tuple[str, Hand], list[Callable[[CallbackContext, ControllerEvent], None]]] = {}
        self._target_callbacks: list[Callable[[CallbackContext, tuple[str, int]], None]] = []
        self._lock = threading.RLock()
        self.osc.set_listener(self._on_osc_event)
        self.osc.set_target_listener(self._on_target_selected)
        self._ctx = CallbackContext(osc=self.osc)
        self._log_callbacks = log_callbacks

    # --- small helpers for pretty logging ---
    @staticmethod
    def _cb_name(cb) -> str:
        mod = getattr(cb, "__module__", "")
        name = getattr(cb, "__qualname__", getattr(cb, "__name__", repr(cb)))
        return f"{mod}.{name}" if mod else str(name)

    @staticmethod
    def _short(val, maxlen: int = 120) -> str:
        s = repr(val)
        return s if len(s) <= maxlen else (s[:maxlen - 1] + "…")

    def on_target_selected(self, callback: Callable[[CallbackContext, tuple[str, int]], None]):
        """Register for "discovery just chose a send target".

        Multiplexed here because OSCManager holds a single listener slot while several
        mappings may each need the event -- the same reason on_osc fans out.

        The callback runs on zeroconf's dispatch thread and fires again on a re-resolve,
        so keep it short and make it idempotent; OSCManager.set_target_listener owns those
        rules in full.
        """
        with self._lock:
            self._target_callbacks.append(callback)

    def on_osc(self, address: str, callback: Callable[[CallbackContext, str, Any], None], *, watch: Iterable[str] | None = None):
        with self._lock:
            self._osc_callbacks.setdefault(address, []).append(callback)
        # Watch the primary address so its value is cached. Note this does NOT put
        # it in the served OSCQuery tree -- that tree is a hardcoded two-node
        # constant in osc_manager. VRChat is its only consumer and does not read it.
        self.osc.watch(address)
        if watch:
            for addr in watch:
                self.osc.watch(addr)

    def on_controller(self, event_type: str, hand: Hand, callback: Callable[[CallbackContext, ControllerEvent], None], *, watch: Iterable[str] | None = None):
        with self._lock:
            self._ctl_callbacks.setdefault((event_type, hand), []).append(callback)
        if watch:
            for addr in watch:
                self.osc.watch(addr)

    def start(self):
        self.osc.start()
        if self.controllers:
            self.controllers.start()
        self.log.info("VRBridge started (OSC in: %s, HTTP: %s).", self.osc.osc_port, self.osc.http_port)

    def stop(self):
        # Order matters. Stop the controller thread first so no new pulses can be
        # produced, then drain the ones in flight, then take OSC down -- the sink has
        # to outlive the drain or the trailing zeros go nowhere.
        #
        # Draining in MappingRouter.run_forever instead was wrong four ways: the
        # controller loop was still producing, osc.stop() ran before the 1.5s
        # controller join so callbacks fired into a dead OSCManager, and a library
        # embedder calling stop() directly never drained at all.
        if self.controllers:
            self.controllers.stop()
        if not drain_pulses(timeout=1.0):
            self.log.warning(
                "Timed out draining pending OSC pulses; a parameter may be left latched.")
        self.osc.stop()
        self.log.info("VRBridge stopped.")

    # ---------------------- Internal dispatch -----------------------------
    def _on_osc_event(self, address: str, value):
        with self._lock:
            cbs = list(self._osc_callbacks.get(address, ()))
        for cb in cbs:
            if self._log_callbacks and self.log.isEnabledFor(logging.INFO):
                self.log.info("VRChat %s -> %s(%s)", address, self._cb_name(cb), self._short(value))
            try:
                cb(self._ctx, address, value)
            except Exception as e:
                self.log.exception("VRChat callback error for %s: %s", address, e)

    def _on_target_selected(self, target: tuple[str, int]):
        with self._lock:
            cbs = list(self._target_callbacks)
        for cb in cbs:
            if self._log_callbacks and self.log.isEnabledFor(logging.INFO):
                self.log.info("Target %s:%d -> %s", target[0], target[1], self._cb_name(cb))
            try:
                cb(self._ctx, target)
            except Exception as e:
                # One mapping's handler must not cost the others theirs, and this runs on
                # zeroconf's dispatch thread where an escape would take out every later
                # service callback.
                self.log.exception("Target callback error for %s:%d: %s", target[0], target[1], e)

    def _on_controller_event(self, evt: ControllerEvent):
        key_variants = [(evt.type, evt.hand), (evt.type, "both")]
        with self._lock:
            cbs = []
            for key in key_variants:
                cbs.extend(self._ctl_callbacks.get(key, ()))
        for cb in cbs:
            if self._log_callbacks:
                # Avoid spamming logs with raw samples unless DEBUG is enabled
                if evt.type == ControllerEventType.TOUCHPAD_SCROLL_RAW:
                    if self.log.isEnabledFor(logging.DEBUG):
                        self.log.debug(
                            "Controller %s/%s dx=%.5f dy=%.5f -> %s",
                            evt.type, evt.hand, evt.dx or 0.0, evt.dy or 0.0, self._cb_name(cb)
                        )
                else:
                    if self.log.isEnabledFor(logging.INFO):
                        self.log.info(
                            "Controller %s/%s steps=%s -> %s",
                            evt.type, evt.hand, getattr(evt, "steps", None), self._cb_name(cb)
                        )
            try:
                cb(self._ctx, evt)
            except Exception as e:
                self.log.exception("Controller callback error for %s/%s: %s", evt.type, evt.hand, e)
