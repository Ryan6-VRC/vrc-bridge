from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Literal, Optional

from .controller_manager import ControllerEvent, ControllerManager
from .osc_manager import OSCManager
from .utils import setup_logging

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
    def send(self, address: str, value): self.osc.send(address, value)

class VRBridge:
    """Orchestrates OSC I/O and SteamVR controller events.
    Users register all callbacks up-front with `on_osc`/`on_controller` and then call `start()`.
    """
    def __init__(self, *, log_level: str = "INFO", vr_background=True, enable_steamvr: bool = True,
                 advertise=True, log_callbacks: bool = False):
        import logging as _logging
        self.log = setup_logging(level=getattr(_logging, log_level))
        self.osc = OSCManager(logger=self.log, advertise=advertise)

        self.controllers: Optional[ControllerManager] = None
        if enable_steamvr:
            self.controllers = ControllerManager(logger=self.log, vr_background=vr_background)
            self.controllers.set_listener(self._on_controller_event)
        else:
            self.log.info("SteamVR is disabled.")

        self._osc_callbacks: Dict[str, list[Callable[[CallbackContext, str, Any], None]]] = {}
        self._ctl_callbacks: Dict[tuple[str, Hand], list[Callable[[CallbackContext, ControllerEvent], None]]] = {}
        self._lock = threading.RLock()
        self.osc.set_listener(self._on_osc_event)
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

    def on_osc(self, address: str, callback: Callable[[CallbackContext, str, Any], None], *, watch: Iterable[str] | None = None):
        with self._lock:
            self._osc_callbacks.setdefault(address, []).append(callback)
        # Always watch the primary address so it shows up in the OSCQuery tree and is cached.
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
        self.osc.stop()
        if self.controllers:
            self.controllers.stop()
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
