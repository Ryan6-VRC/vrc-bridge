"""Quant-channel manifests: the sender-side contract a quant-channel avatar module emits.

A manifest is JSON emitted by `vrc-patterns/quant-channel`'s generator into its `built/`
(one per CONFIG) and installed beside the bridge; it declares each channel's name, address,
bit width, sign, and sender-side float smoothing. **The manifest is authoritative** -- the
avatar's parameter declarations cannot carry this (pairing, widths, taus), and the bridge
never adapts to what an avatar merely appears to declare (`docs/design.md` descopes
discovery). Which manifest applies is decided by the avatar: it carries the manifest id as
the default value of the unsynced Int `QuantChannel/Manifest`, read over OSCQuery by
`mappings.osc_quant`.

Ids are identity (a manifest keeps its id when its channels change); `revision` is content,
bumped on any channel change so a stale installed copy is at least visible in the logs.
Valid ids are **1 and up, with no 255 ceiling**: this module's sentinel is emitted by the
generator straight into the params asset and live-validated at 1000 (`docs/osc.md`). The
wardrobe manifest's 1-255 bound is Modular Avatar's inspector clamp on the substrate that
authors *that* marker, and is deliberately not inherited here -- the two schema docs state
their own ranges. Range convention: 1-999 vrc-patterns entries, 1000+ third parties (the
entry README's registry table is the ledger).

`address` is a **checked echo**: the loader verifies it equals
`/avatar/parameters/<name>` and refuses otherwise, so the field can never drift into a
second source of truth while still sparing every consumer the address derivation. (The
generator lints names -- no spaces, no trailing digits -- which is what makes that
derivation single-valued.)

Every rejection names the offending key and the file, following `ConfigError`'s promise --
these files are copied around by hand, and "invalid manifest" would leave the author
hunting across a directory.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from vrbridge.quant_channel import MAX_BITS, ChannelSpec
from vrbridge.settings import ConfigError

#: The one schema this loader understands; a different value is refused rather than
#: guessed at, because a silently half-read manifest encodes to wrong addresses.
SCHEMA = 1

MANIFEST_ID_MIN = 1


@dataclass(frozen=True)
class GateSpec:
    """A declared gate bool (synced, sender-driven) -- e.g. a puppet's Enable."""
    name: str
    address: str


@dataclass(frozen=True)
class QuantManifest:
    """One avatar module's channel table, as its emitted manifest declares it."""
    id: int
    revision: int
    channels: Tuple[ChannelSpec, ...]
    gates: Tuple[GateSpec, ...]
    source: Path | None = None

    def channel(self, name: str) -> ChannelSpec | None:
        for ch in self.channels:
            if ch.name == name:
                return ch
        return None


def _require_int(v, at: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ConfigError(f"{at} is {v!r}; expected a whole number")
    return v


def _require_str(v, at: str) -> str:
    if not isinstance(v, str) or not v:
        raise ConfigError(f"{at} is {v!r}; expected a non-empty string")
    return v


def _check_echo(name: str, address: str, at: str) -> None:
    want = "/avatar/parameters/" + name
    if address != want:
        raise ConfigError(
            f"{at}: address is {address!r} but name {name!r} derives {want!r}. The address "
            f"field is a checked echo of the name, not a place to point elsewhere -- fix "
            f"whichever of the two is wrong.")


def load_manifest(path: Path) -> QuantManifest:
    """Read and validate one manifest file."""
    path = Path(path)
    try:
        with path.open("rb") as fh:
            raw = json.load(fh)
    except OSError as exc:
        raise ConfigError(f"cannot read quant-channel manifest {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"quant-channel manifest {path} is not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level is {type(raw).__name__}; expected an object")
    known = {"schema", "id", "revision", "channels", "gates"}
    unknown = sorted(set(raw) - known)
    if unknown:
        # Same reasoning as settings.py's _build: a silently ignored key is
        # indistinguishable from one that does nothing.
        raise ConfigError(f"{path}: unknown key(s) {', '.join(unknown)}. "
                          f"Valid keys: {', '.join(sorted(known))}")

    schema = _require_int(raw.get("schema"), f"{path}: schema")
    if schema != SCHEMA:
        raise ConfigError(f"{path}: schema is {schema}; this bridge understands schema "
                          f"{SCHEMA}. Refusing rather than half-reading it.")

    manifest_id = _require_int(raw.get("id"), f"{path}: id")
    if manifest_id < MANIFEST_ID_MIN:
        raise ConfigError(f"{path}: id is {manifest_id}; expected {MANIFEST_ID_MIN} or "
                          f"greater (module docstring owns the range convention)")
    revision = _require_int(raw.get("revision"), f"{path}: revision")

    rows = raw.get("channels")
    if not isinstance(rows, list) or not rows:
        raise ConfigError(f"{path}: channels is {rows!r}; expected one or more channel objects")

    channels: List[ChannelSpec] = []
    seen = set()
    for i, row in enumerate(rows):
        at = f"{path}: channels[{i}]"
        if not isinstance(row, dict):
            raise ConfigError(f"{at} is {row!r}; expected an object")
        row_known = {"name", "address", "bits", "signed", "floatTau", "declaredWidths"}
        row_unknown = sorted(set(row) - row_known)
        if row_unknown:
            raise ConfigError(f"{at} has unknown key(s) {', '.join(row_unknown)}. "
                              f"Valid keys: {', '.join(sorted(row_known))}")
        name = _require_str(row.get("name"), f"{at}: name")
        if name in seen:
            raise ConfigError(f"{at}: name {name!r} appears twice in this manifest")
        seen.add(name)
        address = _require_str(row.get("address"), f"{at}: address")
        _check_echo(name, address, at)
        bits = _require_int(row.get("bits"), f"{at}: bits")
        if not 0 <= bits <= MAX_BITS:
            raise ConfigError(f"{at}: bits is {bits}; expected 0-{MAX_BITS}")
        signed = row.get("signed", False)
        if not isinstance(signed, bool):
            raise ConfigError(f"{at}: signed is {signed!r}; expected true or false")
        if signed and bits == 0:
            raise ConfigError(f"{at}: signed with bits 0 -- a plain synced float already "
                              f"carries sign; the generator refuses this too")
        tau = row.get("floatTau", 0.0)
        if isinstance(tau, bool) or not isinstance(tau, (int, float)) or tau < 0:
            raise ConfigError(f"{at}: floatTau is {tau!r}; expected a number >= 0")
        channels.append(ChannelSpec(name=name, address=address, bits=bits,
                                    signed=signed, float_tau=float(tau)))

    gates: List[GateSpec] = []
    grows = raw.get("gates", [])
    if not isinstance(grows, list):
        raise ConfigError(f"{path}: gates is {grows!r}; expected an array")
    for i, row in enumerate(grows):
        at = f"{path}: gates[{i}]"
        if not isinstance(row, dict):
            raise ConfigError(f"{at} is {row!r}; expected an object")
        row_unknown = sorted(set(row) - {"name", "address"})
        if row_unknown:
            raise ConfigError(f"{at} has unknown key(s) {', '.join(row_unknown)}. "
                              f"Valid keys: address, name")
        name = _require_str(row.get("name"), f"{at}: name")
        address = _require_str(row.get("address"), f"{at}: address")
        _check_echo(name, address, at)
        gates.append(GateSpec(name=name, address=address))

    return QuantManifest(id=manifest_id, revision=revision,
                         channels=tuple(channels), gates=tuple(gates), source=path)


def load_manifests(paths: Iterable[Path]) -> Dict[int, QuantManifest]:
    """Load several manifests, indexed by id. A duplicate id fails loud naming both files:
    the id is how the worn avatar selects its table, so a collision makes selection
    arbitrary -- the wardrobe loader's rule, for the same reason."""
    out: Dict[int, QuantManifest] = {}
    for path in paths:
        m = load_manifest(path)
        if m.id in out:
            first = out[m.id].source
            raise ConfigError(
                f"{path}: id {m.id} is already claimed by {first}. Two quant-channel "
                f"manifests with one id leave the worn avatar's sentinel ambiguous; "
                f"renumber one (and regenerate that avatar's module to match).")
        out[m.id] = m
    return out


def discover_manifests(directory: Path) -> Dict[int, QuantManifest]:
    """Load every `*.json` in `directory`, indexed by id. A missing directory is empty --
    normal for a user with no quant-channel avatars; the mapping reports having nothing
    to work with when something actually asks it to arm."""
    directory = Path(directory)
    if not directory.is_dir():
        return {}
    return load_manifests(sorted(directory.glob("*.json")))
