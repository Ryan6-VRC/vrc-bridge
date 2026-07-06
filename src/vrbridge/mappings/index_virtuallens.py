"""
VirtualLens2 controls via VRBridge (Index touchpads & presses)

- TOUCHPAD_PRESS:
    * LEFT short -> Drop/Pickup via VirtualLens2_Control (uses PositionMode)
    * LEFT long  -> RemotePlayerMask toggle 0/1
    * RIGHT  short -> AFMode toggle between 0 and 1 (else -> 0)
    * RIGHT  long  -> AutoLeveler toggle between 0 and 3 (else -> 0)
- TOUCHPAD_SCROLL (stepped):
    * LEFT  VScroll -> Aperture +/- (discrete ladder)
    * LEFT  HScroll -> Exposure +/- (discrete ladder)
    * RIGHT HScroll -> Zoom +/- (discrete ladder)
- TOUCHPAD_SCROLL_RAW:
    * RIGHT VScroll (raw dy) -> Smooth Zoom (sensitivity/deadzone configurable)

Requires the vrbridge project to be importable.
"""
from __future__ import annotations

import math
import time
from typing import Iterable

from vrbridge.mappings.mapping_base import Mapping
from vrbridge import ControllerEventType, VRBridge
from vrbridge.utils import ParamState, SmoothScroller, clamp01, step_param

# ------------------------------ Config ------------------------------------

# Shared pulse duration for control pulses
PRESS_DURATION: float = 0.1

# Smooth scroll tuning
SMOOTH_SCROLL_SENSITIVITY:      float = 0.15  # How much x changes per unit dy
SMOOTH_SCROLL_MAX_DELTA:        float = 0.10  # Clamp per raw event
SMOOTH_SCROLL_STICKY_ABS:       float = 0.06  # Cumulative |dy| to unstick
SMOOTH_SCROLL_STICKY_RESET_GAP: float = 0.20  # Seconds without raw -> treat as new touch
SMOOTH_SCROLL_RESET_STICKY_ON_RHSCROLL: bool = True  # Reset whenever a step zoom occurs

# User-configurable optical ranges (should match your VL2 setup)
FOCAL_MIN_MM: float = 12.0
FOCAL_MAX_MM: float = 300.0
FNUMBER_MIN:  float = 1.0
FNUMBER_MAX:  float = 22.0
EXPOSURE_RANGE_EV: float = 3.0  # +/- range

# Natural step ladders that define the increments in which Zoom, Aperture, and Exposure will change
ZOOM_STEPS_MM: list[float] = [12, 16, 20, 24, 28, 35, 50, 70, 85, 105, 135, 200, 300]
APERTURE_STEPS: list[float] = [1.0, 1.4, 1.8, 2.2, 2.8, 4.0, 5.6, 8.0, 11.0, 16.0, 22.0]
APERTURE_MIN_X: float = 0.0001  # keep x==0.0 for "Infinity"; smallest finite aperture should be >= this
# Exposure: -3..+3 in 1/3 EV steps
EXPOSURE_STEPS_EV: list[float] = [round(-EXPOSURE_RANGE_EV + i*(1/3), 6) for i in range(int((2*EXPOSURE_RANGE_EV)/(1/3)) + 1)]

# ------------------------------ OSC paths ---------------------------------

# VirtualLens2 parameters (OSC uses '_' instead of spaces)
VL2_ZOOM          = "/avatar/parameters/VirtualLens2_Zoom"
VL2_SCROLL        = "/avatar/parameters/VirtualLens2_Zoom" # Or use VirtualLens2_Distance for manual focus
VL2_APERTURE      = "/avatar/parameters/VirtualLens2_Aperture"
VL2_EXPOSURE      = "/avatar/parameters/VirtualLens2_Exposure"
VL2_CONTROL       = "/avatar/parameters/VirtualLens2_Control"      # write-only
VL2_POSITION_MODE = "/avatar/parameters/VirtualLens2_PositionMode" # read-only

VL2_AUTOLEVELER  = "/avatar/parameters/VirtualLens2_AutoLeveler"
VL2_REMOTE_MASK  = "/avatar/parameters/VirtualLens2_RemotePlayerMask"
VL2_AF_MODE      = "/avatar/parameters/VirtualLens2_AFMode"

# Control command values used here
CMD_PICKUP = 12
CMD_DROP   = 13

# ------------------------------ Helpers -----------------------------------

def exp_map_x(value: float, vmin: float, vmax: float) -> float:
    """Inverse of y = vmin * exp(x * ln(vmax/vmin)) to obtain x in [0,1]."""
    value = max(min(value, vmax), vmin)
    r = vmax / vmin
    if r <= 0.0:
        return 0.0
    return clamp01(math.log(value / vmin) / math.log(r))

def ev_map_x(E: float, E_range: float) -> float:
    return clamp01((E / E_range + 1.0) / 2.0)

def zoom_mm_to_x(steps_mm: Iterable[float]) -> list[float]:
    return [exp_map_x(f, FOCAL_MIN_MM, FOCAL_MAX_MM) for f in steps_mm if FOCAL_MIN_MM <= f <= FOCAL_MAX_MM]

def aperture_f_to_x(F: float, Fmin: float = FNUMBER_MIN, Fmax: float = FNUMBER_MAX) -> float:
    """
    VirtualLens2 aperture inverse mapping (empirical):
      - x==0.0 => Infinity (special)
      - For finite F in [Fmin, Fmax], x = ln(Fmax/F) / ln(Fmax/Fmin)
    Floor at APERTURE_MIN_X so Fmax step doesn't hit Infinity.
    """
    F = max(min(F, Fmax), Fmin)
    r = Fmax / Fmin
    x = math.log(Fmax / F) / math.log(r)
    return max(APERTURE_MIN_X, clamp01(x))

def exposure_ex_to_x(steps_ev: Iterable[float]) -> list[float]:
    return [ev_map_x(E, EXPOSURE_RANGE_EV) for E in steps_ev if -EXPOSURE_RANGE_EV <= E <= EXPOSURE_RANGE_EV]

ZOOM_STEPS_X     = zoom_mm_to_x(ZOOM_STEPS_MM)
APERTURE_STEPS_X = [aperture_f_to_x(F) for F in APERTURE_STEPS] + [0.0]
EXPOSURE_STEPS_X = exposure_ex_to_x(EXPOSURE_STEPS_EV)

# -------------------------- Mapping ---------------------------------------

class VirtualLensMapping(Mapping):
    name = "index_virtuallens"

    def __init__(self, bridge: VRBridge):
        super().__init__(bridge)
        # Param tracking
        self.zoom_state     =    ParamState(VL2_ZOOM,     default=ZOOM_STEPS_X[5],     bridge=bridge)
        self.aperture_state =    ParamState(VL2_APERTURE, default=APERTURE_STEPS_X[7], bridge=bridge)
        self.exposure_state =    ParamState(VL2_EXPOSURE, default=0.5,                 bridge=bridge)
        self.autoleveler_state = ParamState(VL2_AUTOLEVELER,   bridge=bridge)
        self.remotemask_state  = ParamState(VL2_REMOTE_MASK,   bridge=bridge)
        self.position_state    = ParamState(VL2_POSITION_MODE, bridge=bridge)
        self.autofocus_state   = ParamState(VL2_AF_MODE,       bridge=bridge)

        if VL2_SCROLL == VL2_ZOOM:
            self.scroll_state = self.zoom_state
        else:
            self.scroll_state = ParamState(VL2_SCROLL, default=0.5, bridge=bridge)

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
        """Aperture step +/-"""
        step_param(ctx, self.aperture_state, APERTURE_STEPS_X, evt.steps)

    def step_exposure(self, ctx, evt):
        """Exposure step +/-"""
        step_param(ctx, self.exposure_state, EXPOSURE_STEPS_X, evt.steps)

    def step_zoom(self, ctx, evt):
        """Zoom step +/-"""
        if SMOOTH_SCROLL_RESET_STICKY_ON_RHSCROLL:
            self._smoother.reset()
        step_param(ctx, self.zoom_state, ZOOM_STEPS_X, evt.steps)

    def toggle_autolevel(self, ctx, evt):
        """AutoLeveler: toggle 0 <-> 3; else -> 0"""
        cur = self.autoleveler_state.get()
        self.autoleveler_state.set(ctx, 3 if cur == 0 else 0)

    def toggle_remotemask(self, ctx, evt):
        """RemotePlayerMask toggle 0/1"""
        cur = self.remotemask_state.get()
        self.remotemask_state.set(ctx, 1 if cur == 0 else 0)

    def toggle_drop(self, ctx, evt):
        """Drop (13) if PositionMode==0 else Pickup (12)"""
        cur = self.position_state.get()
        if cur == 0:
            # Pickedup -> Drop
            ctx.send(VL2_CONTROL, CMD_DROP)
            self.position_state.ingest(1)
        else:
            # Dropped -> Pickup
            ctx.send(VL2_CONTROL, CMD_PICKUP)
            self.position_state.ingest(0)

    def toggle_autofocus(self, ctx, evt):
        """AFMode: toggle 0/1; else -> 0"""
        cur = self.autofocus_state.get()
        self.autofocus_state.set(ctx, 1 if cur == 0 else 0)

    # ---- lifecycle ----

    def register(self) -> None:
        super().register()
        # Smooth zoom
        self.bridge.on_controller(ControllerEventType.TOUCHPAD_SCROLL_RAW, hand="right",
                                  callback=self._gate(self.smooth_scroll),
                                  watch=[VL2_SCROLL])

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
                                  callback=self._gate(self.toggle_autolevel))
        self.bridge.on_controller(ControllerEventType.TOUCHPAD_LONG_PRESS,  hand="right",
                                  callback=self._gate(self.toggle_remotemask))
