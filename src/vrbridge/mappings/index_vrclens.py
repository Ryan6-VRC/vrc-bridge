"""
VRCLens controls via VRBridge (Index touchpads & presses)

- Right touchpad RAW vertical -> smooth zoom (continuous).
- Right touchpad horizontal   -> stepped zoom using ZOOM_STEPS.
- Left touchpad vertical      -> Aperture +/- (VRCL feature toggles 193/192).
- Left touchpad horizontal    -> Exposure +/- (VRCL feature toggles 110/108).
- Short/Long presses (TOUCHPAD):
    * LEFT  short -> Drop      (251)
    * LEFT  long  -> AutoFocus (13)
    * RIGHT short -> Stabilize (14)
    * RIGHT long  -> Portrait  (222)

Requires the vrbridge project to be importable.
"""
from __future__ import annotations

import time

from vrbridge.mappings.mapping_base import Mapping
from vrbridge import ControllerEventType, VRBridge
from vrbridge.utils import (ParamState, SmoothScroller, clamp01, press_pulse,
                            step_param)

# ------------------------------ Config ------------------------------------

# General button pulse
PRESS_DURATION: float = 0.1

# Smooth scroll tuning
SMOOTH_SCROLL_SENSITIVITY:      float = 0.15  # How much x changes per unit dy
SMOOTH_SCROLL_MAX_DELTA:        float = 0.10  # Clamp per raw event
SMOOTH_SCROLL_STICKY_ABS:       float = 0.06  # Cumulative |dy| to unstick
SMOOTH_SCROLL_STICKY_RESET_GAP: float = 0.20  # Seconds without raw -> treat as new touch
SMOOTH_SCROLL_RESET_STICKY_ON_RHSCROLL: bool = True  # Reset whenever a step zoom occurs

# ------------------------------ OSC paths ---------------------------------

VRCL_ZOOM   = "/avatar/parameters/VRCLZoomRadial"
VRCL_SCROLL = "/avatar/parameters/VRCLZoomRadial" # Or use VRCLFocusRadial for manual focus
VRCL_TOGGLE = "/avatar/parameters/VRCLFeatureToggle"

# User-configurable zoom steps (0..1 range in VRCLens)
ZOOM_STEPS: list[float] = [0.00, 0.12, 0.25, 0.38, 0.50, 0.60, 0.65, 0.75, 0.82, 0.90, 1.00]

# VRCL feature codes
FEATURE_DROP:      int = 251
FEATURE_AUTOFOCUS: int = 13
FEATURE_STABILIZE: int = 14
FEATURE_PORTRAIT:  int = 222

# Increase/Decrease Aperture
FEATURE_APERTURE_MINUS: int = 192
FEATURE_APERTURE_PLUS:  int = 193

# Increase/Decrease Exposure
FEATURE_EXPOSURE_MINUS: int = 108
FEATURE_EXPOSURE_PLUS:  int = 110

# -------------------------- Mapping ---------------------------------------

class VRCLensMapping(Mapping):
    name = "index_vrclens"

    def __init__(self, bridge: VRBridge):
        super().__init__(bridge)
        # Param tracking
        self.zoom_state = ParamState(VRCL_ZOOM, default=ZOOM_STEPS[1], bridge=bridge)

        if VRCL_SCROLL == VRCL_ZOOM:
            self.scroll_state = self.zoom_state
        else:
            self.scroll_state = ParamState(VRCL_SCROLL, default=0.5, bridge=bridge)

        self._smoother = SmoothScroller(
            sensitivity=SMOOTH_SCROLL_SENSITIVITY,
            max_delta=SMOOTH_SCROLL_MAX_DELTA,
            sticky_abs=SMOOTH_SCROLL_STICKY_ABS,
            reset_gap=SMOOTH_SCROLL_STICKY_RESET_GAP,
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
            press_pulse(ctx, VRCL_TOGGLE, int(code), PRESS_DURATION)

    def step_exposure(self, ctx, evt):
        """Exposure +/- (feature presses per step)."""
        steps = int(getattr(evt, "steps", 0) or 0)
        code = FEATURE_EXPOSURE_PLUS if steps > 0 else FEATURE_EXPOSURE_MINUS
        for _ in range(abs(steps)):
            press_pulse(ctx, VRCL_TOGGLE, int(code), PRESS_DURATION)

    def step_zoom(self, ctx, evt):
        """Zoom step +/-"""
        if SMOOTH_SCROLL_RESET_STICKY_ON_RHSCROLL:
            self._smoother.reset()
        steps = int(getattr(evt, "steps", 0) or 0)
        step_param(ctx, self.zoom_state, ZOOM_STEPS, steps)

    def toggle_stabilize(self, ctx, evt):
        press_pulse(ctx, VRCL_TOGGLE, FEATURE_STABILIZE, PRESS_DURATION)

    def toggle_portrait(self, ctx, evt):
        press_pulse(ctx, VRCL_TOGGLE, FEATURE_PORTRAIT, PRESS_DURATION)

    def toggle_drop(self, ctx, evt):
        press_pulse(ctx, VRCL_TOGGLE, FEATURE_DROP, PRESS_DURATION)

    def toggle_autofocus(self, ctx, evt):
        press_pulse(ctx, VRCL_TOGGLE, FEATURE_AUTOFOCUS, PRESS_DURATION)

    # ---- lifecycle ----

    def register(self) -> None:
        super().register()
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
