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
from vrbridge.settings import UserCameraSettings, exposure_ev_rungs, filter_to_range, settings
from vrbridge import ControllerEvent, ControllerEventType, VRBridge
from vrbridge.utils import ParamState, SmoothScroller, clamp, step_param

# ------------------------------ Config ------------------------------------

# Tuning lives in settings.UserCameraSettings and is read at construction.
#
# Two kinds of number live there and they are not interchangeable. zoom_*,
# focaldist_* and aperture_* are VRChat's published slider bounds -- protocol
# facts, and sending outside them just gets clamped. exposure_min_ev/max_ev are
# this mapping's chosen working range, deliberately narrower than VRChat's
# -10..4, because a third-of-a-stop ladder across the full range would be 43 rungs.
#
# The smooth-scroll numbers match the two lens mappings' but do not mean the same
# thing: here a delta is a fraction of a *log range* and gets multiplied by its
# width before use, so the two sets must stay separately settable.

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

    def __init__(self, bridge: VRBridge, tuning: UserCameraSettings | None = None):
        super().__init__(bridge)
        t = self._tune = tuning if tuning is not None else settings().usercamera

        # Step ladders, derived here rather than at import so a retuned range is
        # actually consulted, and so an out-of-range rung is named rather than dropped.
        self.zoom_steps_mm, zoom_dropped = filter_to_range(
            t.zoom_steps_mm, t.zoom_min_mm, t.zoom_max_mm)
        self.aperture_steps_f, aperture_dropped = filter_to_range(
            t.aperture_steps_f, t.aperture_min_f, t.aperture_max_f)
        self.exposure_steps_ev, exposure_dropped = filter_to_range(
            exposure_ev_rungs(t.exposure_min_ev, t.exposure_max_ev, t.exposure_step_ev),
            t.exposure_min_ev, t.exposure_max_ev)
        for label, dropped, unit, lo, hi in (
            ("zoom", zoom_dropped, "mm", t.zoom_min_mm, t.zoom_max_mm),
            ("aperture", aperture_dropped, "f", t.aperture_min_f, t.aperture_max_f),
            ("exposure", exposure_dropped, "EV", t.exposure_min_ev, t.exposure_max_ev),
        ):
            if dropped:
                bridge.log.warning(
                    "index_usercamera: %d %s rung(s) fall outside VRChat's %s..%s %s range "
                    "and would only be clamped: %s",
                    len(dropped), label, lo, hi, unit,
                    ", ".join(str(v) for v in dropped))

        # Param mirrors. These defaults are VRChat's own documented startup values,
        # not tuning, so they stay in source.
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
            sensitivity=t.smooth_scroll.sensitivity,
            max_delta=t.smooth_scroll.max_delta,
            sticky_abs=t.smooth_scroll.sticky_abs,
            reset_gap=t.smooth_scroll.sticky_reset_gap,
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
            eps   = self._tune.focaldist_log_eps
            lo_x  = self._tune.focaldist_min
            hi_x  = self._tune.focaldist_max
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
            cur_mm = clamp(self.zoom_state.get(ctx), self._tune.zoom_min_mm, self._tune.zoom_max_mm)
            ln_min = math.log(self._tune.zoom_min_mm)
            ln_max = math.log(self._tune.zoom_max_mm)
            ln_rng = ln_max - ln_min
            cur_ln = math.log(cur_mm)
            new_ln = clamp(cur_ln + d_frac * ln_rng, ln_min, ln_max)
            new_mm = math.exp(new_ln)
            self.zoom_state.set(ctx, new_mm)

    def step_aperture(self, ctx, evt: ControllerEvent):
        """Aperture step +/- (f-number list)"""
        step_param(ctx, self.aperture_state, self.aperture_steps_f, evt.steps)

    def step_exposure(self, ctx, evt: ControllerEvent):
        """Exposure step +/- (EV list)"""
        step_param(ctx, self.exposure_state, self.exposure_steps_ev, evt.steps)

    def step_zoom(self, ctx, evt: ControllerEvent):
        """Zoom step +/- (mm list)"""
        if self._tune.smooth_scroll.reset_sticky_on_step:
            self._smoother.reset()
        step_param(ctx, self.zoom_state, self.zoom_steps_mm, evt.steps)

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
