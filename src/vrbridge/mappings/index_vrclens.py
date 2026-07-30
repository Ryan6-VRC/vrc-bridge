"""
VRCLens controls via VRBridge (Index touchpads & presses)

- Right touchpad RAW vertical -> smooth zoom (continuous).
- Right touchpad horizontal   -> stepped zoom along the configured zoom ladder.
- Left touchpad vertical      -> Aperture +/- (VRCL feature toggles 193/192).
- Left touchpad horizontal    -> Exposure +/- (VRCL feature toggles 110/108).
- Short/Long presses (TOUCHPAD):
    * LEFT  short -> Drop      (251)
    * LEFT  long  -> AutoFocus (13)
    * RIGHT short -> Stabilize (14)
    * RIGHT long  -> Portrait  (222)
"""
from __future__ import annotations

import time

from vrbridge.mappings.mapping_base import Mapping
from vrbridge.settings import VRCLensSettings, settings
from vrbridge import ControllerEventType, VRBridge
from vrbridge.utils import (ParamState, SmoothScroller, clamp01, press_pulse,
                            step_param)

# Tuning (press duration, zoom ladder, smooth-scroll feel) lives in
# settings.VRCLensSettings and is read at construction.

# ------------------------------ OSC paths ---------------------------------

VRCL_ZOOM   = "/avatar/parameters/VRCLZoomRadial"
VRCL_SCROLL = "/avatar/parameters/VRCLZoomRadial" # Or use VRCLFocusRadial for manual focus
VRCL_TOGGLE = "/avatar/parameters/VRCLFeatureToggle"

# VRCL feature codes. Opaque command identifiers from VRCLens, not tuning:
# they name features, so they stay in source rather than in the settings file.
# Each was checked against VRCLens's own expression menus; test_addresses.py pins
# them and records what the one surprising value means.
FEATURE_DROP:      int = 251
FEATURE_AUTOFOCUS: int = 13   # avatar-tracking AF, not the plain AF entry
FEATURE_STABILIZE: int = 14
FEATURE_PORTRAIT:  int = 222

# Increase/Decrease Aperture
FEATURE_APERTURE_MINUS: int = 192
FEATURE_APERTURE_PLUS:  int = 193

# Increase/Decrease Exposure. Not adjacent: the value between them is Exposure Reset.
FEATURE_EXPOSURE_MINUS: int = 108
FEATURE_EXPOSURE_PLUS:  int = 110

# -------------------------- Mapping ---------------------------------------

class VRCLensMapping(Mapping):
    name = "index_vrclens"

    def __init__(self, bridge: VRBridge, tuning: VRCLensSettings | None = None):
        super().__init__(bridge)
        self._tune = tuning if tuning is not None else settings().vrclens
        self.zoom_steps = list(self._tune.zoom_steps)

        # Param tracking. The default names a value on the ladder rather than an
        # index into it, so retuning the ladder cannot silently re-point it.
        self.zoom_state = ParamState(VRCL_ZOOM, default=self._tune.default_zoom, bridge=bridge)

        if VRCL_SCROLL == VRCL_ZOOM:
            self.scroll_state = self.zoom_state
        else:
            self.scroll_state = ParamState(VRCL_SCROLL, default=0.5, bridge=bridge)

        ss = self._tune.smooth_scroll
        self._smoother = SmoothScroller(
            sensitivity=ss.sensitivity,
            max_delta=ss.max_delta,
            sticky_abs=ss.sticky_abs,
            reset_gap=ss.sticky_reset_gap,
        )

    # ---- callbacks ----

    def smooth_scroll(self, ctx, evt):
        delta = self._smoother.on_sample(evt.dy, evt.when)
        if delta != 0.0:
            self.scroll_state.set(ctx, clamp01(self.scroll_state.get(ctx) + delta))

    def step_aperture(self, ctx, evt):
        """Aperture +/- (feature presses per step)."""
        steps = int(getattr(evt, "steps", 0) or 0)
        code = FEATURE_APERTURE_PLUS if steps > 0 else FEATURE_APERTURE_MINUS
        for _ in range(abs(steps)):
            press_pulse(ctx, VRCL_TOGGLE, int(code), self._tune.press_duration)

    def step_exposure(self, ctx, evt):
        """Exposure +/- (feature presses per step)."""
        steps = int(getattr(evt, "steps", 0) or 0)
        code = FEATURE_EXPOSURE_PLUS if steps > 0 else FEATURE_EXPOSURE_MINUS
        for _ in range(abs(steps)):
            press_pulse(ctx, VRCL_TOGGLE, int(code), self._tune.press_duration)

    def step_zoom(self, ctx, evt):
        """Zoom step +/-"""
        if self._tune.smooth_scroll.reset_sticky_on_step:
            self._smoother.reset()
        steps = int(getattr(evt, "steps", 0) or 0)
        step_param(ctx, self.zoom_state, self.zoom_steps, steps)

    def toggle_stabilize(self, ctx, evt):
        press_pulse(ctx, VRCL_TOGGLE, FEATURE_STABILIZE, self._tune.press_duration)

    def toggle_portrait(self, ctx, evt):
        press_pulse(ctx, VRCL_TOGGLE, FEATURE_PORTRAIT, self._tune.press_duration)

    def toggle_drop(self, ctx, evt):
        press_pulse(ctx, VRCL_TOGGLE, FEATURE_DROP, self._tune.press_duration)

    def toggle_autofocus(self, ctx, evt):
        press_pulse(ctx, VRCL_TOGGLE, FEATURE_AUTOFOCUS, self._tune.press_duration)

    # ---- lifecycle ----

    def _attach(self) -> None:
        # Smooth zoom (right pad raw vertical)
        self.bridge.on_controller(ControllerEventType.TOUCHPAD_SCROLL_RAW, hand="right",
                                  callback=self._gate(self.smooth_scroll),
                                  watch=[VRCL_SCROLL])

        # Aperture and Exposure adjustments
        self.bridge.on_controller(ControllerEventType.TOUCHPAD_VSCROLL, hand="left",
                                  callback=self._gate(self.step_aperture))
        self.bridge.on_controller(ControllerEventType.TOUCHPAD_HSCROLL, hand="left",
                                  callback=self._gate(self.step_exposure))
        self.bridge.on_controller(ControllerEventType.TOUCHPAD_HSCROLL, hand="right",
                                  callback=self._gate(self.step_zoom))

        # Touchpad Short/Long presses only
        self.bridge.on_controller(ControllerEventType.TOUCHPAD_SHORT_PRESS, hand="left",
                                  callback=self._gate(self.toggle_drop))
        self.bridge.on_controller(ControllerEventType.TOUCHPAD_LONG_PRESS,  hand="left",
                                  callback=self._gate(self.toggle_autofocus))
        self.bridge.on_controller(ControllerEventType.TOUCHPAD_SHORT_PRESS, hand="right",
                                  callback=self._gate(self.toggle_stabilize))
        self.bridge.on_controller(ControllerEventType.TOUCHPAD_LONG_PRESS,  hand="right",
                                  callback=self._gate(self.toggle_portrait))
