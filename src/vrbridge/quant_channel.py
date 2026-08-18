"""Quant-channel codec: the sender half of a smoothed, binary-synced OSC channel.

One channel is one continuous value in [-1, 1] (or [0, 1] unsigned) carried two ways at
once: a full-precision float to `<address>` for the wearer's own client, and OSCmooth-shaped
quantized booleans (`<Name>1/2/4...` + `<Name>Negative`) that are the only part remotes see.
The avatar side decodes `x_hat = +-k/(2^n - 1)` and smooths it; `vrc-patterns/quant-channel`
generates that half and the manifest JSON `quant_manifest` loads.

The measured split this module preserves (promoted from `index_puppet`, which is its first
consumer): the **float is smoothed sender-side** (first-order low-pass at `float_tau`) and the
**bits are raw and immediate** -- quantized output snaps, analog output eases. Smoothing runs
on the caller's clock (`now`), never wall clock: controller events carry their own timestamps
(`evt.when`), and re-basing onto `time.time()` would silently change the feel the tau values
were tuned against.

Wire typing, deliberate on both counts: bits are sent as **ints** (0/1), which is a valid
write to a declared bool parameter on the client and the emulator alike -- VRCFT sends true
OSC bools on the same wire and both are correct, so neither end should be "fixed". The float
is coerced through `float()` at the boundary, because an int reaching a declared **float**
parameter is dropped silently on both venues (`docs/osc.md`), and Python's int `0` slips
through arithmetic that looks float-typed (`_clamp_unit(0)` is type-preserving).

Encoding is `k = round(|x| * (2^n - 1))` with no negative zero (`Negative` is suppressed at
zero magnitude). VRCFT's own encoder floors instead of rounding; compatibility is the names
plus the decode codebook, not sender rounding, and round is the more accurate encoder --
`docs/local` design record and the entry README carry the comparison.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

#: Widest supported code. The avatar generator refuses past 8 too; a wider channel should be
#: a plain synced float (8 wire bits) rather than nine bools.
MAX_BITS = 8


def encode_unit(x: float, n: int) -> Tuple[bool, int]:
    """Encode x in [-1,1] as (negative_flag, integer code k in [0, 2^n - 1]).

    Promoted verbatim from `index_puppet._quant_encode_unit` (pinned by
    `tests/test_quant_channel.py` before the move): clamp to [-1,1], round the magnitude,
    and never emit a negative zero -- `neg` is true only when the code itself is non-zero,
    so a decoder need not special-case sign-with-zero-magnitude from this sender.
    """
    if n <= 0:
        return (False, 0)
    if x < -1.0:
        x = -1.0
    if x > 1.0:
        x = 1.0
    d = (1 << n) - 1
    k = int(round(abs(x) * d))
    neg = (x < 0.0) and (k > 0)
    return neg, k


def derive_bool_addrs(base_addr: str, n: int) -> dict:
    """Bool addresses for a float address: '/.../X' -> '/.../XNegative', '/.../X1', ...

    Promoted from `index_puppet._derive_bool_addrs`, shape unchanged: n <= 0 yields no
    bool addresses at all (the float-only mode).
    """
    if n <= 0:
        return {"neg": None, "bits": []}
    root, name = base_addr.rsplit("/", 1)
    root = root + "/"
    return {
        "neg": f"{root}{name}Negative",
        "bits": [f"{root}{name}{1 << i}" for i in range(n)],
    }


def _clamp_unit(v: float) -> float:
    return -1.0 if v < -1.0 else 1.0 if v > 1.0 else v


@dataclass(frozen=True)
class ChannelSpec:
    """One channel's wire contract, as the manifest declares it.

    `bits == 0` is the plain-synced-float mode: no bool addresses, the float itself is the
    synced carrier. `signed` is meaningless there and the manifest loader refuses it.
    """
    name: str
    address: str
    bits: int
    signed: bool = True
    float_tau: float = 0.0


class QuantChannel:
    """Sender state for one channel: the float smoother plus the bit encoder.

    One instance per **address**, deliberately: smoother state is per-address in the
    behavior this preserves, so a mapping that mirrors one raw value to two addresses
    (index_puppet's `single_touch_mode: "together"`) holds two instances and writes both --
    sharing one instance across the mirror would couple the two filters and change the feel.
    """

    def __init__(self, spec: ChannelSpec):
        if spec.bits < 0 or spec.bits > MAX_BITS:
            raise ValueError(f"channel {spec.name}: bits is {spec.bits}; expected 0-{MAX_BITS}")
        self.spec = spec
        addrs = derive_bool_addrs(spec.address, spec.bits)
        self._neg_addr: Optional[str] = addrs["neg"] if spec.signed else None
        self._bit_addrs: List[str] = addrs["bits"]
        self._value: float = 0.0
        self._last_ts: float = 0.0
        self._initialized: bool = False

    # -- float smoothing (FloatAxisSmoother's semantics, per-instance) --------

    def _reset(self, value: float, now: float) -> float:
        self._value = _clamp_unit(value)
        self._last_ts = now
        self._initialized = True
        return self._value

    def _filter(self, target: float, now: float) -> float:
        target = _clamp_unit(target)
        if self.spec.float_tau <= 0.0:
            return self._reset(target, now)
        if not self._initialized:
            return self._reset(target, now)
        import math
        dt = now - self._last_ts
        alpha = 1.0 if dt <= 0.0 else (1.0 - math.exp(-dt / self.spec.float_tau))
        self._value = _clamp_unit(self._value + (target - self._value) * alpha)
        self._last_ts = now
        return self._value

    def reset(self, value: float, now: float) -> None:
        """Seat the smoother at `value` without emitting -- an avatar-change reset seeds
        the next send's starting point rather than easing from stale state."""
        self._reset(value, now)

    # -- sending ---------------------------------------------------------------

    def send(self, send: Callable[[str, object], object], x: float, now: float) -> None:
        """Emit one sample: raw/immediate bits, then the smoothed float.

        `send` is `ctx.send` / `OSCManager.send`; `now` is the caller's clock -- pass the
        event's own timestamp, not `time.time()` (module docstring).
        """
        if self._bit_addrs:
            neg, k = encode_unit(x, self.spec.bits)
            if self._neg_addr is not None:
                send(self._neg_addr, 1 if neg else 0)
            for i, addr in enumerate(self._bit_addrs):
                send(addr, (k >> i) & 1)
        smoothed = self._filter(x, now)
        # float() is the boundary coercion the module docstring argues for; do not remove
        # it on the observation that every current caller already passes a float.
        send(self.spec.address, float(smoothed))
