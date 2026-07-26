"""
UserCamera controls via VRBridge (Index touchpads & presses)

- TOUCHPAD_PRESS:
    * LEFT  short -> Toggle /usercamera/AutoLevelRoll
    * LEFT  long  -> Toggle /usercamera/ShowFocus
    * RIGHT short -> Capture Photo via /usercamera/Capture
    * RIGHT long  -> Switch between Photo and Print Mode

- TOUCHPAD_SCROLL (stepped):
    * LEFT  VScroll -> Aperture +/- (discrete ladder in f-numbers)
    * LEFT  HScroll -> Exposure +/- (discrete ladder in EV)
    * RIGHT HScroll -> Zoom +/- (discrete ladder in millimeters)

- TOUCHPAD_SCROLL_RAW:
    * RIGHT VScroll (raw dy) -> Smooth control:
        - When ShowFocus == 1: adjust FocalDistance
        - When ShowFocus == 0: adjust Zoom

Requires the vrbridge project to be importable.
"""

from __future__ import annotations

import math
import time

from vrbridge.mappings.mapping_base import Mapping
from vrbridge import ControllerEvent, ControllerEventType, VRBridge
from vrbridge.utils import ParamState, SmoothScroller, clamp, step_param

# ------------------------------ Config ------------------------------------

# Smooth scroll tuning (dimensionless deltas; we scale to range below)
SMOOTH_SCROLL_SENSITIVITY:      float = 0.15  # How much 'fraction of range' changes per unit dy
SMOOTH_SCROLL_MAX_DELTA:        float = 0.10  # Clamp per raw event (fraction of range)
SMOOTH_SCROLL_STICKY_ABS:       float = 0.06  # Cumulative |dy| to unstick
SMOOTH_SCROLL_STICKY_RESET_GAP: float = 0.20  # Seconds without raw -> treat as new touch
SMOOTH_SCROLL_RESET_STICKY_ON_RHSCROLL: bool = True  # Reset whenever a step zoom occurs

# UserCamera slider ranges (from VRChat docs)
ZOOM_MIN_MM:       float = 20.0
ZOOM_MAX_MM:       float = 150.0
EXPOSURE_MIN_EV:   float = -3.0
EXPOSURE_MAX_EV:   float =  3.0
FOCALDIST_MIN:     float = 0.0
FOCALDIST_MAX:     float = 10.0
APERTURE_MIN_F:    float = 1.4
APERTURE_MAX_F:    float = 32.0

# Focus smooth-scroll mapping:
# Use shifted log so the slider can reach EXACT 0.0 m while staying numerically stable.
# We transform with ln(x + ε) in [ln(ε), ln(FOCALDIST_MAX + ε)], then invert via exp(t) - ε.
FOCALDIST_LOG_EPS: float = 0.10  # metres; tweak if you want more/less sensitivity near zero

# Natural step ladders (kept compact and within allowed ranges)
ZOOM_STEPS_MM: list[float] = [20, 22, 26, 30, 35, 45, 55, 70, 85, 105, 135, 150]
APERTURE_STEPS: list[float] = [1.4, 1.8, 2.2, 2.8, 4.0, 5.6, 8.0, 11.0, 16.0, 22.0, 32.0]
# Exposure: -3..+3 in 1/3 EV steps
EXPOSURE_STEPS_EV: list[float] = [round(EXPOSURE_MIN_EV + i*(1/3), 6)
                                  for i in range(int((EXPOSURE_MAX_EV - EXPOSURE_MIN_EV)/(1/3)) + 1)]

# ------------------------------ OSC paths ---------------------------------

USERC_MODE          = "/usercamera/Mode"
USERC_REMOTE_MASK   = "/usercamera/RemotePlayer"
USERC_AUTOLEVELROLL = "/usercamera/AutoLevelRoll"
USERC_SHOWFOCUS     = "/usercamera/ShowFocus"
USERC_CAPTURE       = "/usercamera/Capture"

USERC_ZOOM          = "/usercamera/Zoom"           # float mm, 20..150
USERC_EXPOSURE      = "/usercamera/Exposure"       # float EV, -3..+3
USERC_FOCALDIST     = "/usercamera/FocalDistance"  # float metres, 0..10
USERC_APERTURE      = "/usercamera/Aperture"       # float f-number, 1.4..32

# RAW smooth scroll watches Zoom, not FocalDistance.
# (ParamState registers OSC listeners itself, so both Zoom and FocalDistance are mirrored anyway.)
USERC_SCROLL = USERC_ZOOM

# ------------------------------ Mapping -----------------------------------

class UserCameraMapping(Mapping):
    name = "index_usercamera"

    def __init__(self, bridge: VRBridge):
        super().__init__(bridge)
        # Param mirrors (defaults match VRChat table)
        self.mode_state         = ParamState(USERC_MODE,          default=0,    bridge=bridge) # 0 Off
        self.remote_state       = ParamState(USERC_REMOTE_MASK,   default=1,    bridge=bridge)
        self.autoroll_state     = ParamState(USERC_AUTOLEVELROLL, default=0,    bridge=bridge)
        self.showfocus_state    = ParamState(USERC_SHOWFOCUS,     default=0,    bridge=bridge)

        self.zoom_state         = ParamState(USERC_ZOOM,          default=45.0, bridge=bridge)
        self.exposure_state     = ParamState(USERC_EXPOSURE,      default=0.0,  bridge=bridge)
        self.focaldist_state    = ParamState(USERC_FOCALDIST,     default=1.5,  bridge=bridge)
        self.aperture_state     = ParamState(USERC_APERTURE,      default=15.0, bridge=bridge)

        # Smooth scroller for raw vertical (right pad)
        self._smoother = SmoothScroller(
            sensitivity=SMOOTH_SCROLL_SENSITIVITY,
            max_delta=SMOOTH_SCROLL_MAX_DELTA,
            sticky_abs=SMOOTH_SCROLL_STICKY_ABS,
            reset_gap=SMOOTH_SCROLL_STICKY_RESET_GAP,
        )

    # ---- callbacks ----

    # RAW smooth scroll:
    # - ShowFocus == 1 => FocalDistance via shifted log(m) (invert sign to match Zoom feel; can reach 0.0 m)
    # - ShowFocus == 0 => Zoom via log(mm)
    def smooth_scroll(self, ctx, evt: ControllerEvent):
        dy   = evt.dy
        when = evt.when
        d_frac = self._smoother.on_sample(dy, when=when)  # fraction of range (dimensionless)
        if d_frac == 0.0:
            return

        if self.showfocus_state.get():
            # ---- Focus: shifted-log mapping ln(x + ε) so 0.0 m is reachable ----
            eps   = max(1e-6, FOCALDIST_LOG_EPS)
            lo_x  = 0.0
            hi_x  = FOCALDIST_MAX
            cur_x = clamp(self.focaldist_state.get(ctx), lo_x, hi_x)

            ln_min = math.log(eps)
            ln_max = math.log(hi_x + eps)
            ln_rng = ln_max - ln_min
            cur_ln = math.log(cur_x + eps)
            new_ln = clamp(cur_ln + d_frac * ln_rng, ln_min, ln_max)
            new_x  = math.exp(new_ln) - eps
            new_x  = clamp(new_x, lo_x, hi_x)
            self.focaldist_state.set(ctx, new_x)
        else:
            # ---- Zoom: work in log(mm) so equal scroll yields equal zoom ratios ----
            cur_mm = clamp(self.zoom_state.get(ctx), ZOOM_MIN_MM, ZOOM_MAX_MM)
            ln_min = math.log(ZOOM_MIN_MM)
            ln_max = math.log(ZOOM_MAX_MM)
            ln_rng = ln_max - ln_min
            cur_ln = math.log(cur_mm)
            new_ln = clamp(cur_ln + d_frac * ln_rng, ln_min, ln_max)
            new_mm = math.exp(new_ln)
            self.zoom_state.set(ctx, new_mm)

    def step_aperture(self, ctx, evt: ControllerEvent):
        """Aperture step +/- (f-number list)"""
        step_param(ctx, self.aperture_state, APERTURE_STEPS, evt.steps)

    def step_exposure(self, ctx, evt: ControllerEvent):
        """Exposure step +/- (EV list)"""
        step_param(ctx, self.exposure_state, EXPOSURE_STEPS_EV, evt.steps)

    def step_zoom(self, ctx, evt: ControllerEvent):
        """Zoom step +/- (mm list)"""
        if SMOOTH_SCROLL_RESET_STICKY_ON_RHSCROLL:
            self._smoother.reset()
        step_param(ctx, self.zoom_state, ZOOM_STEPS_MM, evt.steps)

    # --- presses (short/long) ---

    def toggle_showfocus(self, ctx, evt):
        cur = self.showfocus_state.get()
        self.showfocus_state.set(ctx, 0 if cur else 1)

    def toggle_remote_mask(self, ctx, evt):
        cur = self.remote_state.get()
        self.remote_state.set(ctx, 0 if cur else 1)

    def toggle_autolevelroll(self, ctx, evt):
        cur = self.autoroll_state.get()
        self.autoroll_state.set(ctx, 0 if cur else 1)

    def switch_mode_photo_print(self, ctx, evt):
        cur = self.mode_state.get()
        # If currently 1 (Photo) -> switch to 5 (Print); otherwise set to 1.
        self.mode_state.set(ctx, 5 if cur == 1 else 1)

    def capture_photo(self, ctx, evt):
        ctx.send(USERC_CAPTURE, 1)

    # ---- lifecycle ----

    def register(self) -> None:
        super().register()
        # Smooth control (right pad raw vertical)
        self.bridge.on_controller(
            ControllerEventType.TOUCHPAD_SCROLL_RAW, hand="right",
            callback=self._gate(self.smooth_scroll),
            watch=[USERC_SCROLL],
        )

        # Discrete ladders
        self.bridge.on_controller(ControllerEventType.TOUCHPAD_VSCROLL, hand="left",
                                  callback=self._gate(self.step_aperture))
        self.bridge.on_controller(ControllerEventType.TOUCHPAD_HSCROLL, hand="left",
                                  callback=self._gate(self.step_exposure))
        self.bridge.on_controller(ControllerEventType.TOUCHPAD_HSCROLL, hand="right",
                                  callback=self._gate(self.step_zoom))

        # Press mappings (short/long only)
        self.bridge.on_controller(ControllerEventType.TOUCHPAD_SHORT_PRESS, hand="left",
                                  callback=self._gate(self.toggle_autolevelroll))
        self.bridge.on_controller(ControllerEventType.TOUCHPAD_LONG_PRESS,  hand="left",
                                  callback=self._gate(self.toggle_showfocus))
        self.bridge.on_controller(ControllerEventType.TOUCHPAD_SHORT_PRESS, hand="right",
                                  callback=self._gate(self.capture_photo))
        self.bridge.on_controller(ControllerEventType.TOUCHPAD_LONG_PRESS,  hand="right",
                                  callback=self._gate(self.switch_mode_photo_print))
