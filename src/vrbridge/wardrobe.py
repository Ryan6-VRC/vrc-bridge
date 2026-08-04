"""Wardrobe manifests: which avatar each menu slot swaps to.

A manifest is the table `osc_wardrobe` indexes when the wearer presses a wardrobe button.
It is **user data, not tuning**, which is why it does not live in `settings.py`: the values
are avatar ids belonging to whoever installed the bridge, and `settings.py`'s loader cannot
hold them anyway -- its tuple branch coerces every element through `float`.

**Which manifest applies is decided by the avatar, not by this file.** Each manifest carries
an `id`, and the avatar carries that same number as the default value of an unsynced Int
parameter its menu declares. The bridge reads that marker off the worn avatar over OSCQuery
and looks the manifest up here, which is what lets two avatars carry different wardrobes.
Ids are identity: a manifest keeps its id when its rows change.

Ids are constrained to **1-255** by the substrate that authors them, not by anything here.
Modular Avatar's parameter inspector clamps an Int default to 0-255 and re-clamps whenever
the sync type is touched, so a larger number cannot survive being typed into the prefab.
This is a different constraint from the SDK inspector's truncation that `runtime.md`
§Parameters records, and it is why the vrc-patterns id range convention stops at 255.

Every rejection names the offending key and the file, following `ConfigError`'s promise in
`settings.py` -- a manifest is hand-edited, and a loader that merely says "invalid" leaves
the author hunting a typo across several files.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable

from vrbridge.settings import ConfigError

try:  # 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib  # type: ignore[no-redef]

#: The lowest and highest manifest id, bounded by MA's inspector clamp (module docstring).
MANIFEST_ID_MIN = 1
MANIFEST_ID_MAX = 255

#: Slots a shipped wardrobe menu can produce. The prefab carries eight buttons and a
#: VRChat menu page holds eight controls, so a ninth is not merely unused -- nothing can
#: ever send it, which makes it an authoring mistake rather than a spare row.
SLOT_MIN = 1
SLOT_MAX = 8

#: An avatar id as VRChat writes it: `avtr_` and a UUID. Checked because a typo's only
#: symptom at runtime is a swap that silently does nothing -- the client accepts the
#: datagram and declines to act -- so this is the difference between a load-time error
#: naming the row and an unexplained dead button.
_AVATAR_ID_RE = re.compile(
    r"^avtr_[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


@dataclass(frozen=True)
class Slot:
    """One wardrobe button: the value the menu sends, and the avatar it means."""
    slot: int
    avatar_id: str
    label: str = ""


@dataclass(frozen=True)
class Manifest:
    """One avatar's wardrobe: the id its menu marker carries, and its slot table."""
    id: int
    slots: Dict[int, Slot]
    source: Path | None = None

    def avatar_for(self, slot: int) -> Slot | None:
        """The row a slot value selects, or None -- a gap is legal, so this is not an error."""
        return self.slots.get(slot)


def load_manifest(path: Path) -> Manifest:
    """Read and validate one manifest file."""
    try:
        with Path(path).open("rb") as fh:
            raw = tomllib.load(fh)
    except OSError as exc:
        raise ConfigError(f"cannot read wardrobe manifest {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"wardrobe manifest {path} is not valid TOML: {exc}") from exc

    known = {"id", "slots"}
    unknown = sorted(set(raw) - known)
    if unknown:
        # Same reasoning as settings.py's _build: a silently ignored key is
        # indistinguishable from one that does nothing.
        raise ConfigError(
            f"{path}: unknown key(s) {', '.join(unknown)}. Valid keys: id, slots")

    manifest_id = raw.get("id")
    if manifest_id is None:
        raise ConfigError(f"{path}: no 'id'. It must match the wardrobe marker "
                          f"parameter's default value on the avatar.")
    if isinstance(manifest_id, bool) or not isinstance(manifest_id, int):
        raise ConfigError(f"{path}: id is {manifest_id!r}; expected a whole number")
    if not MANIFEST_ID_MIN <= manifest_id <= MANIFEST_ID_MAX:
        raise ConfigError(
            f"{path}: id is {manifest_id}; expected {MANIFEST_ID_MIN}-{MANIFEST_ID_MAX}. "
            f"Modular Avatar's inspector clamps an Int default to that range, so an id "
            f"outside it cannot be authored onto the avatar that would have to carry it.")

    rows = raw.get("slots")
    if rows is None:
        raise ConfigError(f"{path}: no [[slots]] entries; a manifest with no slots "
                          f"matches an avatar and can never swap it")
    if not isinstance(rows, list) or not rows:
        raise ConfigError(f"{path}: slots is {rows!r}; expected one or more [[slots]] tables")

    slots: Dict[int, Slot] = {}
    for i, row in enumerate(rows):
        at = f"{path}: [[slots]] #{i + 1}"
        if not isinstance(row, dict):
            raise ConfigError(f"{at} is {row!r}; expected a table")
        row_unknown = sorted(set(row) - {"slot", "label", "id"})
        if row_unknown:
            raise ConfigError(f"{at} has unknown key(s) {', '.join(row_unknown)}. "
                              f"Valid keys: slot, label, id")

        slot = row.get("slot")
        if isinstance(slot, bool) or not isinstance(slot, int):
            raise ConfigError(f"{at}: slot is {slot!r}; expected a whole number")
        if not SLOT_MIN <= slot <= SLOT_MAX:
            raise ConfigError(
                f"{at}: slot is {slot}; expected {SLOT_MIN}-{SLOT_MAX}. The wardrobe menu "
                f"carries {SLOT_MAX} buttons, so nothing can ever send this value.")
        if slot in slots:
            raise ConfigError(f"{at}: slot {slot} is already used by another entry; "
                              f"one button cannot mean two avatars")

        avatar_id = row.get("id")
        if not isinstance(avatar_id, str):
            raise ConfigError(f"{at}: id is {avatar_id!r}; expected an avatar id string")
        if not _AVATAR_ID_RE.match(avatar_id):
            raise ConfigError(
                f"{at}: id is {avatar_id!r}, which is not an avatar id "
                f"(expected avtr_ and a UUID, e.g. "
                f"avtr_26187637-0c30-4a09-86e1-bc928c07309e). VRChat ignores a malformed "
                f"id silently, so this would read as a dead button rather than an error.")

        label = row.get("label", "")
        if not isinstance(label, str):
            raise ConfigError(f"{at}: label is {label!r}; expected a string")

        slots[slot] = Slot(slot=slot, avatar_id=avatar_id, label=label)

    return Manifest(id=manifest_id, slots=slots, source=Path(path))


def load_manifests(paths: Iterable[Path]) -> Dict[int, Manifest]:
    """Load several manifests, indexed by id.

    A duplicate id is an error naming both files: the id is how the worn avatar selects
    its table, so two manifests claiming one id make the selection arbitrary. Note the
    asymmetry -- several *avatars* sharing one manifest id is legal and useful, because
    that is how two avatars share a wardrobe. Only a collision inside the loaded set is
    incoherent.
    """
    out: Dict[int, Manifest] = {}
    for path in paths:
        manifest = load_manifest(path)
        if manifest.id in out:
            first = out[manifest.id].source
            raise ConfigError(
                f"{path}: id {manifest.id} is already claimed by {first}. Two manifests "
                f"with one id leave the worn avatar's marker ambiguous; give one a "
                f"different id and update that avatar's marker parameter to match.")
        out[manifest.id] = manifest
    return out


def discover_manifests(directory: Path) -> Dict[int, Manifest]:
    """Load every `*.toml` in `directory`, indexed by id. A missing directory is empty.

    Missing is normal -- a user who has not written a wardrobe has none -- and is not an
    error here. `osc_wardrobe` is what reports that it has nothing to work with, because
    only the mapping knows whether anyone asked it to.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return {}
    return load_manifests(sorted(directory.glob("*.toml")))
