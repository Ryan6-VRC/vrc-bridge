"""The quant-channel manifest loader: the third-party extension surface's gatekeeper.

Intent before test, per `docs/design.md`. Three intents shape this loader and are stated
here rather than left implicit: the `address` field is a **checked echo** of the name (kept
for the reader, verified so it can never become a second source of truth); ids have **no 255
ceiling** (the wardrobe's 1-255 bound is Modular Avatar's inspector clamp on the substrate
that authors *that* marker, deliberately not inherited -- `quant_manifest`'s docstring owns
the argument); and every refusal names the offending key and the file, because these files
are copied around by hand.
"""
import json
from pathlib import Path

import pytest

from vrbridge.quant_manifest import (discover_manifests, load_manifest,
                                     load_manifests)
from vrbridge.settings import ConfigError

GOOD = {
    "schema": 1, "id": 1, "revision": 1,
    "channels": [
        {"name": "QDemo/LX", "address": "/avatar/parameters/QDemo/LX",
         "bits": 3, "signed": True, "floatTau": 0.12,
         "declaredWidths": {"bools": 4}},
        {"name": "QDemo/LY", "address": "/avatar/parameters/QDemo/LY",
         "bits": 3, "signed": True, "floatTau": 0.12},
    ],
    "gates": [{"name": "QDemo/Enable", "address": "/avatar/parameters/QDemo/Enable"}],
}


def write(tmp_path: Path, body: dict, name: str = "m.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(body))
    return p


def variant(**kw) -> dict:
    out = {**GOOD, **kw}
    return out


def test_a_manifest_loads_its_channels_and_gates(tmp_path):
    m = load_manifest(write(tmp_path, GOOD))
    assert m.id == 1 and m.revision == 1
    lx = m.channel("QDemo/LX")
    assert lx.address == "/avatar/parameters/QDemo/LX"
    assert lx.bits == 3 and lx.signed and lx.float_tau == 0.12
    assert m.channel("QDemo/Nope") is None
    assert [g.name for g in m.gates] == ["QDemo/Enable"]
    assert m.source == tmp_path / "m.json"


def test_ids_from_1_up_with_no_ceiling(tmp_path):
    """Intended: the 1000+ third-party range is real. The sentinel is emitted straight into
    the params asset and live-validated at 1000 (`docs/osc.md`); a 255 ceiling here would
    close the extension surface the schema exists for. The wardrobe's bound is MA's
    inspector clamp on *its* marker's authoring substrate, deliberately not inherited."""
    assert load_manifest(write(tmp_path, variant(id=1000))).id == 1000
    with pytest.raises(ConfigError, match="id is 0"):
        load_manifest(write(tmp_path, variant(id=0)))


@pytest.mark.parametrize("mutate, expect", [
    (lambda d: d.update(schema=2), "schema is 2"),
    (lambda d: d.pop("schema"), "schema"),
    (lambda d: d.update(extra=1), "unknown key"),
    (lambda d: d["channels"][0].update(surprise=1), "unknown key"),
    (lambda d: d["gates"][0].update(surprise=1), "unknown key"),
    (lambda d: d.update(id=True), "whole number"),
    (lambda d: d.pop("revision"), "revision"),
    (lambda d: d.update(channels=[]), "one or more"),
    (lambda d: d.pop("channels"), "one or more"),
    (lambda d: d["channels"][0].update(bits=9), "bits is 9"),
    (lambda d: d["channels"][0].update(bits=-1), "bits is -1"),
    (lambda d: d["channels"][0].update(signed=1), "true or false"),
    (lambda d: d["channels"][0].update(floatTau=-0.1), "floatTau"),
    (lambda d: d["channels"][1].update(name="QDemo/LX",
                                       address="/avatar/parameters/QDemo/LX"),
     "appears twice"),
])
def test_a_bad_manifest_is_refused_and_says_why(tmp_path, mutate, expect):
    """Intended: refuse loudly, naming key and file. A silently half-read manifest encodes
    to wrong addresses, which surfaces only in-headset."""
    body = json.loads(json.dumps(GOOD))     # deep copy so mutations do not leak
    mutate(body)
    p = write(tmp_path, body)
    with pytest.raises(ConfigError) as exc:
        load_manifest(p)
    assert expect in str(exc.value)
    assert p.name in str(exc.value), "the message must name the file"


def test_a_future_schema_is_refused_not_half_read(tmp_path):
    """Intended: an unknown `schema` value means unknown semantics for every other key;
    guessing would encode to whatever the old reading happens to produce."""
    with pytest.raises(ConfigError, match="Refusing"):
        load_manifest(write(tmp_path, variant(schema=3)))


def test_the_address_echo_is_checked_on_channels_and_gates(tmp_path):
    """Intended: `address` spares consumers the derivation but may never disagree with the
    name -- a drifted echo is two sources of truth for the wire."""
    body = json.loads(json.dumps(GOOD))
    body["channels"][0]["address"] = "/avatar/parameters/Elsewhere"
    p = write(tmp_path, body)
    with pytest.raises(ConfigError, match="checked echo"):
        load_manifest(p)

    body = json.loads(json.dumps(GOOD))
    body["gates"][0]["address"] = "/avatar/parameters/QDemo/enable"   # case drift
    with pytest.raises(ConfigError, match="checked echo"):
        load_manifest(write(tmp_path, body))


def test_signed_with_zero_bits_is_refused(tmp_path):
    """Intended: bits 0 is the plain-synced-float mode, where the float carries its own
    sign; a `signed` flag there describes nothing and marks a confused generator."""
    body = json.loads(json.dumps(GOOD))
    body["channels"][0].update(bits=0, signed=True)
    with pytest.raises(ConfigError, match="signed with bits 0"):
        load_manifest(write(tmp_path, body))


def test_invalid_json_is_refused_naming_the_file(tmp_path):
    p = tmp_path / "broken.json"
    p.write_text("{not json")
    with pytest.raises(ConfigError, match="not valid JSON") as exc:
        load_manifest(p)
    assert "broken.json" in str(exc.value)


def test_two_manifests_claiming_one_id_are_refused_naming_both(tmp_path):
    """Intended: the id is how the worn avatar's sentinel selects its table; a collision in
    the loaded set makes that selection arbitrary. The wardrobe loader's rule, restated for
    the same reason."""
    a = write(tmp_path, GOOD, "a.json")
    b = write(tmp_path, GOOD, "b.json")
    with pytest.raises(ConfigError) as exc:
        load_manifests([a, b])
    assert "a.json" in str(exc.value) and "b.json" in str(exc.value)


def test_discover_loads_a_directory_indexed_by_id(tmp_path):
    write(tmp_path, GOOD, "one.json")
    write(tmp_path, variant(id=1000), "two.json")
    found = discover_manifests(tmp_path)
    assert sorted(found) == [1, 1000]
    assert found[1].channel("QDemo/LX") is not None


def test_a_missing_manifest_directory_is_empty_not_an_error(tmp_path):
    """Intended: a user with no quant-channel avatars has no manifests. The directory
    reports having nothing when something actually asks it to arm, because only then has
    anyone asked."""
    assert discover_manifests(tmp_path / "absent") == {}
