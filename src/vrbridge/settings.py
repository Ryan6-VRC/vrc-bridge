"""User-tunable settings, resolved from a TOML file over built-in defaults.

Retuning must not require editing installed source. Every default here is the
literal that used to live at the top of the module that consumed it, unchanged
-- `tests/test_settings.py` pins that, so "no config file present" is provably
the behavior this repo shipped before the file existed.

Two rules the layout enforces rather than documents:

* **Addresses are not settings.** OSC addresses and the SteamVR binding JSON are
  contracts with other products, not taste. They stay in source, where a typo is
  a diff rather than a silent runtime miss.
* **Smooth-scroll tuning is per-mapping, even though the three sets of numbers
  are identical.** They do not mean the same thing: the lens mappings add the
  emitted delta straight to a 0..1 parameter, while `index_usercamera` multiplies
  it by a log-range width first. Collapsing them into one shared table would
  preserve the numbers and silently change the feel of two of them.

Consumers must read settings at construction time, never at import time. A value
read into a module constant at import cannot be reconfigured, and a ladder
derived at import crashes the whole package on a bad range instead of the one
mapping that owns it.
"""
from __future__ import annotations

import math
import os
from dataclasses import MISSING, dataclass, fields
from pathlib import Path
from typing import Any

try:  # 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib  # type: ignore[no-redef]


class ConfigError(Exception):
    """A settings file is unusable. Always names the offending key and the file."""


# --------------------------- Section defaults -------------------------------

@dataclass(frozen=True)
class ControllerSettings:
    """SteamVR polling and trackpad feel. Feel-tuned against Index controllers."""
    poll_interval: float = 0.02          # seconds between input polls
    v_scroll_step: float = 0.35          # y travel accumulated per stepped event
    h_scroll_step: float = 0.70          # x travel accumulated per stepped event
    deadzone: float = 0.01               # ignore trackpad jitter below this
    max_steps_per_frame: int = 2         # clamp burstiness
    invert_vscroll: int = 1              # -1 to invert
    invert_hscroll: int = 1
    raw_scroll_min_delta: float = 0.0005  # below this, emit no raw sample
    long_press_threshold: float = 0.40   # seconds held to count as a long press

    def validate(self, at: str) -> None:
        _positive(self.poll_interval, f"{at}.poll_interval")
        _positive(self.v_scroll_step, f"{at}.v_scroll_step")
        _positive(self.h_scroll_step, f"{at}.h_scroll_step")
        _non_negative(self.deadzone, f"{at}.deadzone")
        _positive(self.max_steps_per_frame, f"{at}.max_steps_per_frame")
        _one_of(self.invert_vscroll, (-1, 1), f"{at}.invert_vscroll")
        _one_of(self.invert_hscroll, (-1, 1), f"{at}.invert_hscroll")
        _non_negative(self.raw_scroll_min_delta, f"{at}.raw_scroll_min_delta")
        _positive(self.long_press_threshold, f"{at}.long_press_threshold")


@dataclass(frozen=True)
class SmoothScrollSettings:
    """Feeds utils.SmoothScroller. Units are mapping-specific -- see module docstring."""
    sensitivity: float = 0.15
    max_delta: float = 0.10
    sticky_abs: float = 0.06
    sticky_reset_gap: float = 0.20
    reset_sticky_on_step: bool = True

    def validate(self, at: str) -> None:
        _positive(self.sensitivity, f"{at}.sensitivity")
        _positive(self.max_delta, f"{at}.max_delta")
        _non_negative(self.sticky_abs, f"{at}.sticky_abs")
        _positive(self.sticky_reset_gap, f"{at}.sticky_reset_gap")


@dataclass(frozen=True)
class PuppetSettings:
    quant_level: int = 3                 # magnitude bits for the OSCmooth-style codec
    touch_active_idle_secs: float = 0.5
    single_touch_mode: str = "together"  # "together" mirrors one pad to both sides
    invert_x: int = 1
    invert_y: int = 1
    float_smooth_tau_secs: float = 0.12  # <= 0 disables float smoothing

    def validate(self, at: str) -> None:
        _non_negative(self.quant_level, f"{at}.quant_level")
        _non_negative(self.touch_active_idle_secs, f"{at}.touch_active_idle_secs")
        _one_of(self.single_touch_mode, ("together", "separate"), f"{at}.single_touch_mode")
        _one_of(self.invert_x, (-1, 1), f"{at}.invert_x")
        _one_of(self.invert_y, (-1, 1), f"{at}.invert_y")


@dataclass(frozen=True)
class UserCameraSettings:
    """VRChat's built-in camera. The four ranges are VRChat's published slider
    bounds, not taste; the exposure pair is this mapping's chosen working range
    and is deliberately narrower than VRChat's -10..4."""
    zoom_min_mm: float = 20.0
    zoom_max_mm: float = 150.0
    focaldist_min: float = 0.0
    focaldist_max: float = 10.0
    aperture_min_f: float = 1.4
    aperture_max_f: float = 32.0
    exposure_min_ev: float = -3.0
    exposure_max_ev: float = 3.0
    focaldist_log_eps: float = 0.10      # metres; widens/narrows feel near zero
    zoom_steps_mm: tuple[float, ...] = (20, 22, 26, 30, 35, 45, 55, 70, 85, 105, 135, 150)
    aperture_steps_f: tuple[float, ...] = (1.4, 1.8, 2.2, 2.8, 4.0, 5.6, 8.0, 11.0, 16.0, 22.0, 32.0)
    exposure_step_ev: float = 1.0 / 3.0
    smooth_scroll: SmoothScrollSettings = SmoothScrollSettings()

    def validate(self, at: str) -> None:
        _log_safe_range(self.zoom_min_mm, self.zoom_max_mm, f"{at}.zoom_min_mm", f"{at}.zoom_max_mm")
        _ordered(self.focaldist_min, self.focaldist_max, f"{at}.focaldist_min", f"{at}.focaldist_max")
        _ordered(self.aperture_min_f, self.aperture_max_f, f"{at}.aperture_min_f", f"{at}.aperture_max_f")
        _ordered(self.exposure_min_ev, self.exposure_max_ev, f"{at}.exposure_min_ev", f"{at}.exposure_max_ev")
        _positive(self.focaldist_log_eps, f"{at}.focaldist_log_eps")
        # index_usercamera takes log(focaldist_min + focaldist_log_eps) as the floor of
        # its focus mapping. _ordered alone would accept a negative min, which loads
        # clean and then raises a math domain error inside a controller callback --
        # swallowed to DEBUG, so focus scroll just stops working.
        if self.focaldist_min + self.focaldist_log_eps <= 0:
            raise ConfigError(
                f"{at}.focaldist_min ({self.focaldist_min!r}) + {at}.focaldist_log_eps "
                f"({self.focaldist_log_eps!r}) must be greater than 0; the focus mapping "
                "takes the log of their sum")
        _non_empty(self.zoom_steps_mm, f"{at}.zoom_steps_mm")
        _non_empty(self.aperture_steps_f, f"{at}.aperture_steps_f")
        _positive(self.exposure_step_ev, f"{at}.exposure_step_ev")
        self.smooth_scroll.validate(f"{at}.smooth_scroll")


@dataclass(frozen=True)
class VirtualLensSettings:
    """VirtualLens2. The optical ranges must match the VL2 prefab's own
    configuration -- they are the domain of its parameter encoding, so a
    mismatch mis-encodes every value rather than merely feeling wrong."""
    focal_min_mm: float = 12.0
    focal_max_mm: float = 300.0
    fnumber_min: float = 1.0
    fnumber_max: float = 22.0
    exposure_range_ev: float = 3.0
    aperture_min_x: float = 0.0001       # keeps the f-max rung clear of the x==0 Infinity sentinel
    # No press_duration here on purpose: this mapping drives VirtualLens2_Control
    # with a latched write, not a pulse. See index_virtuallens.toggle_drop -- the
    # key belongs here only once that question is settled.
    zoom_steps_mm: tuple[float, ...] = (12, 16, 20, 24, 28, 35, 50, 70, 85, 105, 135, 200, 300)
    aperture_steps_f: tuple[float, ...] = (1.0, 1.4, 1.8, 2.2, 2.8, 4.0, 5.6, 8.0, 11.0, 16.0, 22.0)
    exposure_step_ev: float = 1.0 / 3.0
    # Startup mirrors, named as the optical value rather than as an index into a
    # ladder: an index silently re-points when the ladder is retuned.
    default_zoom_mm: float = 35.0
    default_aperture_f: float = 8.0
    smooth_scroll: SmoothScrollSettings = SmoothScrollSettings()

    def validate(self, at: str) -> None:
        _log_safe_range(self.focal_min_mm, self.focal_max_mm, f"{at}.focal_min_mm", f"{at}.focal_max_mm")
        _log_safe_range(self.fnumber_min, self.fnumber_max, f"{at}.fnumber_min", f"{at}.fnumber_max")
        _positive(self.exposure_range_ev, f"{at}.exposure_range_ev")
        # Not merely positive: at >= 1 the floor in aperture_f_to_x returns the same
        # value for every finite f-number (one flat ladder), and above 1 it leaves
        # VL2's 0..1 parameter domain entirely.
        if not 0.0 < self.aperture_min_x < 1.0:
            raise ConfigError(
                f"{at}.aperture_min_x is {self.aperture_min_x!r}; expected a value "
                "between 0 and 1, exclusive")
        _non_empty(self.zoom_steps_mm, f"{at}.zoom_steps_mm")
        _non_empty(self.aperture_steps_f, f"{at}.aperture_steps_f")
        _positive(self.exposure_step_ev, f"{at}.exposure_step_ev")
        _in_range(self.default_zoom_mm, self.focal_min_mm, self.focal_max_mm, f"{at}.default_zoom_mm")
        _in_range(self.default_aperture_f, self.fnumber_min, self.fnumber_max, f"{at}.default_aperture_f")
        self.smooth_scroll.validate(f"{at}.smooth_scroll")


@dataclass(frozen=True)
class VRCLensSettings:
    """VRCLens. Its feature codes are opaque command identifiers from that
    product and stay in source -- they are not tuning."""
    press_duration: float = 0.1
    zoom_steps: tuple[float, ...] = (0.00, 0.12, 0.25, 0.38, 0.50, 0.60, 0.65, 0.75, 0.82, 0.90, 1.00)
    default_zoom: float = 0.12           # a value on the ladder, not an index into it
    smooth_scroll: SmoothScrollSettings = SmoothScrollSettings()

    def validate(self, at: str) -> None:
        _positive(self.press_duration, f"{at}.press_duration")
        _non_empty(self.zoom_steps, f"{at}.zoom_steps")
        for i, v in enumerate(self.zoom_steps):
            if not 0.0 <= v <= 1.0:
                raise ConfigError(f"{at}.zoom_steps[{i}] is {v!r}; VRCLens radials take 0.0..1.0")
        if not 0.0 <= self.default_zoom <= 1.0:
            raise ConfigError(f"{at}.default_zoom is {self.default_zoom!r}; expected 0.0..1.0")
        self.smooth_scroll.validate(f"{at}.smooth_scroll")


@dataclass(frozen=True)
class MuteProxySettings:
    press_duration: float = 1.0 / 30

    def validate(self, at: str) -> None:
        _positive(self.press_duration, f"{at}.press_duration")


@dataclass(frozen=True)
class VRCFTSettings:
    service_name: str = "VRCFT"          # substring matched against discovered mDNS names
    avatar_load_delay_secs: float = 1.0

    def validate(self, at: str) -> None:
        if not self.service_name:
            raise ConfigError(f"{at}.service_name is empty; nothing would ever match")
        _non_negative(self.avatar_load_delay_secs, f"{at}.avatar_load_delay_secs")


@dataclass(frozen=True)
class RemySettings:
    base_url: str = "http://127.0.0.1:8000"
    http_timeout_sec: float = 1.0
    work_queue_maxsize: int = 8
    max_retries: int = 1                 # total attempts = max_retries + 1
    watch_dir: str = ""                  # empty -> ~/Pictures/VRChat
    resize_on_upload: bool = True
    target_height: int = 480

    def validate(self, at: str) -> None:
        if not self.base_url:
            raise ConfigError(f"{at}.base_url is empty; set it to your Remy host")
        _positive(self.http_timeout_sec, f"{at}.http_timeout_sec")
        _positive(self.work_queue_maxsize, f"{at}.work_queue_maxsize")
        _non_negative(self.max_retries, f"{at}.max_retries")
        _positive(self.target_height, f"{at}.target_height")

    def resolved_watch_dir(self) -> Path:
        if self.watch_dir:
            return Path(self.watch_dir).expanduser()
        return Path.home() / "Pictures" / "VRChat"


@dataclass(frozen=True)
class Settings:
    controller: ControllerSettings = ControllerSettings()
    puppet: PuppetSettings = PuppetSettings()
    usercamera: UserCameraSettings = UserCameraSettings()
    virtuallens: VirtualLensSettings = VirtualLensSettings()
    vrclens: VRCLensSettings = VRCLensSettings()
    muteproxy: MuteProxySettings = MuteProxySettings()
    vrcft: VRCFTSettings = VRCFTSettings()
    remy: RemySettings = RemySettings()

    def validate(self) -> None:
        for f in fields(self):
            getattr(self, f.name).validate(f.name)


# ------------------------------ Validators ----------------------------------

def _positive(v: float, key: str) -> None:
    if not v > 0:
        raise ConfigError(f"{key} is {v!r}; expected a value greater than 0")


def _non_negative(v: float, key: str) -> None:
    if v < 0:
        raise ConfigError(f"{key} is {v!r}; expected 0 or greater")


def _one_of(v: Any, allowed: tuple, key: str) -> None:
    if v not in allowed:
        raise ConfigError(f"{key} is {v!r}; expected one of {', '.join(repr(a) for a in allowed)}")


def _non_empty(v: tuple, key: str) -> None:
    if not v:
        raise ConfigError(f"{key} is empty; a step ladder needs at least one rung")


def _in_range(v: float, lo: float, hi: float, key: str) -> None:
    if not lo <= v <= hi:
        raise ConfigError(f"{key} is {v!r}; expected a value within {lo!r}..{hi!r}")


def _ordered(lo: float, hi: float, lo_key: str, hi_key: str) -> None:
    if not lo < hi:
        raise ConfigError(f"{lo_key} ({lo!r}) must be less than {hi_key} ({hi!r})")


def _log_safe_range(lo: float, hi: float, lo_key: str, hi_key: str) -> None:
    """A range fed to a log-space encoder. Both ends must be positive and distinct.

    Guarding here rather than at the call site is deliberate: the encoders run
    `log(hi/lo)` as a divisor, so lo == hi is a ZeroDivisionError and lo <= 0 is a
    math domain error -- both raised from inside math, naming neither key.
    """
    if lo <= 0:
        raise ConfigError(f"{lo_key} is {lo!r}; a log-scaled range needs a value greater than 0")
    if hi <= 0:
        raise ConfigError(f"{hi_key} is {hi!r}; a log-scaled range needs a value greater than 0")
    if lo == hi:
        raise ConfigError(f"{lo_key} and {hi_key} are both {lo!r}; a log-scaled range needs two distinct ends")
    _ordered(lo, hi, lo_key, hi_key)


# ------------------------------- Loading ------------------------------------

def _user_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "vrbridge"
    return Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "vrbridge"


def app_base_dir() -> Path:
    """Root for anything generated or hand-edited at runtime.

    A source checkout uses the checkout root, where .gitignore already covers
    these. An installed package cannot: two levels up from site-packages/vrbridge
    is inside the Python tree, which is the wrong place to write runtime state and
    may not be writable at all.
    """
    here = Path(__file__).resolve().parent
    repo_root = here.parent.parent  # src/vrbridge -> src -> checkout root
    if (repo_root / "pyproject.toml").is_file():
        return repo_root
    return _user_data_dir()


def get_config_path() -> Path:
    """Where the settings file lives. Override with VRBRIDGE_CONFIG."""
    env = os.environ.get("VRBRIDGE_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    return app_base_dir() / "vrbridge.toml"


def _coerce(value: Any, default: Any, key: str) -> Any:
    """Fit a TOML value to the shape of the default it replaces."""
    if isinstance(default, tuple):
        if not isinstance(value, list):
            raise ConfigError(f"{key} is {value!r}; expected an array")
        out = []
        for i, v in enumerate(value):
            # float(v) on its own raises a bare ValueError naming neither the key
            # nor the file, and the path re-wrap at load_settings only catches
            # ConfigError -- so the message would name nothing at all.
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ConfigError(f"{key}[{i}] is {v!r}; expected a number")
            out.append(float(v))
        return tuple(out)
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ConfigError(f"{key} is {value!r}; expected true or false")
        return value
    if isinstance(default, int) and not isinstance(default, bool):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{key} is {value!r}; expected a whole number")
        return value
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{key} is {value!r}; expected a number")
        return float(value)
    if isinstance(default, str):
        if not isinstance(value, str):
            raise ConfigError(f"{key} is {value!r}; expected a string")
        return value
    raise ConfigError(f"{key} cannot be set from a config file")


def _build(cls, raw: dict, at: str):
    """Instantiate a settings dataclass from a raw TOML table.

    An unrecognised key is an error, not a shrug. A silently ignored setting is
    indistinguishable from one that does nothing, which is how three dead knobs
    survived in this repo unnoticed.
    """
    if not isinstance(raw, dict):
        raise ConfigError(f"[{at}] is {raw!r}; expected a table")
    known = {f.name: f for f in fields(cls)}
    unknown = sorted(set(raw) - set(known))
    if unknown:
        raise ConfigError(
            f"[{at}] has no setting named {', '.join(unknown)}. "
            f"Valid keys: {', '.join(sorted(known))}"
        )
    kwargs = {}
    for name, f in known.items():
        if name not in raw:
            continue
        default = f.default if f.default is not MISSING else None
        if isinstance(default, SmoothScrollSettings):
            kwargs[name] = _build(SmoothScrollSettings, raw[name], f"{at}.{name}")
        else:
            kwargs[name] = _coerce(raw[name], default, f"{at}.{name}")
    return cls(**kwargs)


def load_settings(path: Path | None = None) -> Settings:
    """Read the settings file if it exists, else return the built-in defaults.

    A missing file is normal and silent. A malformed or unreadable one is not:
    it raises, because a user who wrote a config file and got default behavior
    has no way to tell that their file was skipped.
    """
    path = path if path is not None else get_config_path()
    if not path.is_file():
        settings = Settings()
        settings.validate()
        return settings

    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except OSError as exc:
        raise ConfigError(f"cannot read settings file {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"settings file {path} is not valid TOML: {exc}") from exc

    known = {f.name for f in fields(Settings)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ConfigError(
            f"{path} has no section named {', '.join('[' + u + ']' for u in unknown)}. "
            f"Valid sections: {', '.join('[' + k + ']' for k in sorted(known))}"
        )

    # `from __future__ import annotations` makes f.type a string, so take the
    # class off the default instance instead.
    try:
        kwargs = {f.name: _build(type(f.default), raw[f.name], f.name)
                  for f in fields(Settings) if f.name in raw}
        settings = Settings(**kwargs)
        settings.validate()
    except ConfigError as exc:
        # _build and the constructor must be inside this too, not just validate():
        # ConfigError promises to name the key *and* the file, and a coercion error
        # raised above the wrapper reported only the key.
        raise ConfigError(f"{path}: {exc}") from None
    return settings


_cached: Settings | None = None


def settings() -> Settings:
    """Process-wide settings, loaded once on first use."""
    global _cached
    if _cached is None:
        _cached = load_settings()
    return _cached


def set_settings(value: Settings | None) -> None:
    """Replace (or clear, with None) the cached settings. For tests and embedders."""
    global _cached
    _cached = value


# ---------------------------- Derived ladders -------------------------------
# Pure functions over settings, called at mapping construction rather than at
# import. Each returns the rungs that survive its range filter, plus the rungs it
# dropped, so a caller can say which ones went missing instead of silently
# shortening the ladder under the user.

def exposure_ev_rungs(lo_ev: float, hi_ev: float, step_ev: float) -> list[float]:
    """Inclusive EV ladder from lo to hi. Counts in whole steps so the top rung
    cannot be lost to a float division landing just under an integer."""
    n = int(round((hi_ev - lo_ev) / step_ev))
    return [round(lo_ev + i * step_ev, 6) for i in range(n + 1)]


def filter_to_range(rungs, lo: float, hi: float) -> tuple[list[float], list[float]]:
    """Split rungs into (kept, dropped) against an inclusive range."""
    kept = [float(v) for v in rungs if lo <= v <= hi]
    dropped = [float(v) for v in rungs if not lo <= v <= hi]
    return kept, dropped


def log_unlerp(value: float, vmin: float, vmax: float) -> float:
    """Position of `value` in [vmin, vmax] on a log scale, clamped to 0..1.

    Callers must have validated the range (see _log_safe_range); this does no
    guarding of its own so that a bad range fails at config load with a named
    key rather than here with a math domain error.
    """
    value = max(min(value, vmax), vmin)
    return max(0.0, min(1.0, math.log(value / vmin) / math.log(vmax / vmin)))
