"""
VirtualLens2 controls via VRBridge (Index touchpads & presses)

- Touchpad short/long press:
    * LEFT  short -> Drop/Pickup via VirtualLens2_Control (uses PositionMode)
    * LEFT  long  -> AFMode toggle between 0 and 1 (else -> 0)
    * RIGHT short -> AutoLeveler toggle between 0 and 3 (else -> 0)
    * RIGHT long  -> RemotePlayerMask toggle 0/1
- Touchpad stepped scroll:
    * LEFT  VScroll -> Aperture +/- (discrete ladder)
    * LEFT  HScroll -> Exposure +/- (discrete ladder)
    * RIGHT HScroll -> Zoom +/- (discrete ladder)
- Touchpad raw scroll:
    * RIGHT VScroll (raw dy) -> Smooth Zoom (sensitivity and sticky-start
      threshold configurable; the raw-sample deadzone is a controller setting)
"""
from __future__ import annotations

import math
import time

from vrbridge.mappings.mapping_base import Mapping
from vrbridge.settings import (VirtualLensSettings, exposure_ev_rungs, filter_to_range,
                               log_unlerp, settings)
from vrbridge import ControllerEventType, VRBridge
from vrbridge.utils import ParamState, SmoothScroller, clamp01, step_param

# Tuning -- the optical ranges, the step ladders and the smooth-scroll feel --
# lives in settings.VirtualLensSettings. The ranges must match the VL2 prefab's
# own configuration: they are the domain of its parameter encoding, so a mismatch
# mis-encodes every value rather than merely feeling wrong.
#
# The ladders are derived at construction, not at import. Deriving them at import
# meant a bad range raised from inside math.log while the package was still being
# imported, taking down every router rather than the one mapping that owns it.

# ------------------------------ OSC paths ---------------------------------

# VirtualLens2 parameters. The underscores are part of VL2's own expression
# parameter names, which VRChat appends to /avatar/parameters/ verbatim.
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

def zoom_mm_to_x(value_mm: float, focal_min_mm: float, focal_max_mm: float) -> float:
    """Focal length in mm to VirtualLens2's log-encoded Zoom parameter in [0,1]."""
    return log_unlerp(value_mm, focal_min_mm, focal_max_mm)

def ev_map_x(E: float, E_range: float) -> float:
    """Exposure compensation in EV to VL2's linear Exposure parameter; 0 EV is 0.5."""
    return clamp01((E / E_range + 1.0) / 2.0)

def aperture_f_to_x(F: float, Fmin: float, Fmax: float, min_x: float) -> float:
    """
    VirtualLens2 aperture inverse mapping (empirical):
      - x==0.0 => Infinity (special)
      - For finite F in [Fmin, Fmax], x = ln(Fmax/F) / ln(Fmax/Fmin)
    Floor at min_x so the Fmax rung doesn't collide with the Infinity sentinel.

    Descending by design: higher x is a wider aperture (more blur), so +1 step
    stops down, matching index_usercamera's ascending f-number ladder in the
    direction the operator feels.
    """
    F = max(min(F, Fmax), Fmin)
    x = math.log(Fmax / F) / math.log(Fmax / Fmin)
    return max(min_x, clamp01(x))

def zoom_ladder(steps_mm, focal_min_mm, focal_max_mm) -> tuple[list[float], list[float]]:
    """Encoded zoom rungs, plus the mm rungs dropped for falling outside the range."""
    kept, dropped = filter_to_range(steps_mm, focal_min_mm, focal_max_mm)
    return [zoom_mm_to_x(f, focal_min_mm, focal_max_mm) for f in kept], dropped

def aperture_ladder(steps_f, fnumber_min, fnumber_max, min_x) -> tuple[list[float], list[float]]:
    """Encoded aperture rungs with the Infinity sentinel appended, plus dropped f-numbers."""
    kept, dropped = filter_to_range(steps_f, fnumber_min, fnumber_max)
    return [aperture_f_to_x(F, fnumber_min, fnumber_max, min_x) for F in kept] + [0.0], dropped

def exposure_ladder(steps_ev, e_range) -> tuple[list[float], list[float]]:
    """Encoded exposure rungs, plus the EV rungs dropped for falling outside +/- e_range."""
    kept, dropped = filter_to_range(steps_ev, -e_range, e_range)
    return [ev_map_x(E, e_range) for E in kept], dropped

# -------------------------- Mapping ---------------------------------------

class VirtualLensMapping(Mapping):
    name = "index_virtuallens"

    def __init__(self, bridge: VRBridge, tuning: VirtualLensSettings | None = None):
        super().__init__(bridge)
        t = self._tune = tuning if tuning is not None else settings().virtuallens

        # Derive the ladders here, and say which rungs the configured ranges
        # excluded. Dropping them silently is how a range edit shortens a ladder
        # under the operator with no way to notice.
        self.zoom_steps_x, zoom_dropped = zoom_ladder(t.zoom_steps_mm, t.focal_min_mm, t.focal_max_mm)
        self.aperture_steps_x, aperture_dropped = aperture_ladder(
            t.aperture_steps_f, t.fnumber_min, t.fnumber_max, t.aperture_min_x)
        self.exposure_steps_x, exposure_dropped = exposure_ladder(
            exposure_ev_rungs(-t.exposure_range_ev, t.exposure_range_ev, t.exposure_step_ev),
            t.exposure_range_ev)
        for label, dropped, unit, lo, hi in (
            ("zoom", zoom_dropped, "mm", t.focal_min_mm, t.focal_max_mm),
            ("aperture", aperture_dropped, "f", t.fnumber_min, t.fnumber_max),
            ("exposure", exposure_dropped, "EV", -t.exposure_range_ev, t.exposure_range_ev),
        ):
            if dropped:
                bridge.log.warning(
                    "index_virtuallens: %d %s rung(s) fall outside the configured %s..%s %s range "
                    "and will not be reachable: %s",
                    len(dropped), label, lo, hi, unit,
                    ", ".join(str(v) for v in dropped))

        # Param tracking. Startup values name an optical value, then encode it --
        # an index into a derived ladder silently re-points when a range changes.
        self.zoom_state     =    ParamState(VL2_ZOOM,     bridge=bridge,
                                            default=zoom_mm_to_x(t.default_zoom_mm,
                                                                 t.focal_min_mm, t.focal_max_mm))
        self.aperture_state =    ParamState(VL2_APERTURE, bridge=bridge,
                                            default=aperture_f_to_x(t.default_aperture_f,
                                                                    t.fnumber_min, t.fnumber_max,
                                                                    t.aperture_min_x))
        self.exposure_state =    ParamState(VL2_EXPOSURE, default=ev_map_x(0.0, t.exposure_range_ev),
                                            bridge=bridge)
        self.autoleveler_state = ParamState(VL2_AUTOLEVELER,   bridge=bridge)
        self.remotemask_state  = ParamState(VL2_REMOTE_MASK,   bridge=bridge)
        self.position_state    = ParamState(VL2_POSITION_MODE, bridge=bridge)
        self.autofocus_state   = ParamState(VL2_AF_MODE,       bridge=bridge)

        if VL2_SCROLL == VL2_ZOOM:
            self.scroll_state = self.zoom_state
        else:
            self.scroll_state = ParamState(VL2_SCROLL, default=0.5, bridge=bridge)

        ss = t.smooth_scroll
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
        """Aperture step +/-"""
        step_param(ctx, self.aperture_state, self.aperture_steps_x, evt.steps)

    def step_exposure(self, ctx, evt):
        """Exposure step +/-"""
        step_param(ctx, self.exposure_state, self.exposure_steps_x, evt.steps)

    def step_zoom(self, ctx, evt):
        """Zoom step +/-"""
        if self._tune.smooth_scroll.reset_sticky_on_step:
            self._smoother.reset()
        step_param(ctx, self.zoom_state, self.zoom_steps_x, evt.steps)

    def toggle_autolevel(self, ctx, evt):
        """AutoLeveler: toggle 0 <-> 3; else -> 0"""
        cur = self.autoleveler_state.get()
        self.autoleveler_state.set(ctx, 3 if cur == 0 else 0)

    def toggle_remotemask(self, ctx, evt):
        """RemotePlayerMask toggle 0/1"""
        cur = self.remotemask_state.get()
        self.remotemask_state.set(ctx, 1 if cur == 0 else 0)

    def toggle_drop(self, ctx, evt):
        """Drop (13) if PositionMode==0 else Pickup (12).

        UNSETTLED, and deliberately left as-is: this writes Control and leaves it
        latched at the command value, where index_vrclens drives its structurally
        identical command channel with press_pulse and returns it to 0. The two
        cannot both be right.

        Latching appears to work because the two commands alternate, so a press
        always changes the value. It would stop working the moment the same command
        is sent twice in a row -- which the optimistic position_state.ingest() below
        makes reachable if the mirror ever desyncs from VL2's own echo.

        Settle it against VirtualLens2's prefab (does its FX read a transition into
        the value, or the value itself?), not by pattern-matching the sibling. Until
        then there is no press_duration in VirtualLensSettings, because a config key
        that exists only to serve an unmade change is a key that does nothing.
        """
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

    def _attach(self) -> None:
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
