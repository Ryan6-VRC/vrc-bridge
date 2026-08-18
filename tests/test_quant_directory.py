"""The quant-channel directory: arming, invalidation, and the token discipline.

This file is also **probe 4** of the quant-channel arc (multi-manifest switching on
`/avatar/change`, headless, fake fetch responses) -- the probe lives here rather than in a
script because its whole content is assertions.

Intent before test, per `docs/design.md`. The trigger design is the part a naive test suite
would freeze wrong, so its three intents are stated: invalidation handlers only clear (a
fetch on the change would read a cold-loading avatar's *outgoing* tree); every fetch runs on
the one worker thread, and a fetch completing after a later invalidation must not arm
(sequence token, the wardrobe's press-token discipline at avatar-change scale); and while
unarmed, consumer *use* is the retry trigger, floored -- not a timer.

Driven through a fake bridge object rather than the FakeVRChat harness: the directory
touches exactly `bridge.log` / `bridge.on_osc` / `bridge.on_target_selected` /
`bridge.osc.fetch` / `bridge.osc.target_is_pinned`, and faking at that seam lets a test
park a fetch or serve any FETCH_* outcome without a socket in sight.
"""
import logging
import threading
import time
from typing import Optional

import pytest

import vrbridge.mappings.osc_quant as osc_quant
from vrbridge.mappings.osc_quant import (AVATAR_CHANGE_ADDR, MARKER_ADDR,
                                         QuantChannelDirectory)
from vrbridge.osc_manager import (FETCH_NO_PEER, FETCH_NOT_FOUND, FETCH_OK,
                                  FETCH_TRANSPORT, FetchResult, PeerIdentity)
from vrbridge.quant_channel import ChannelSpec
from vrbridge.quant_manifest import GateSpec, QuantManifest
from vrbridge.settings import PuppetSettings, QuantChannelSettings

VRCHAT = PeerIdentity(name="VRChat-Client-1._oscjson._tcp.local.", is_vrchat=True)
STRANGER = PeerIdentity(name="VRCFT-NHM5HG._oscjson._tcp.local.", is_vrchat=False)


def manifest(mid: int, *, name="QDemo/LX", bits=3, float_tau=0.12,
             revision=1) -> QuantManifest:
    return QuantManifest(
        id=mid, revision=revision,
        channels=(ChannelSpec(name=name, address=f"/avatar/parameters/{name}",
                              bits=bits, signed=True, float_tau=float_tau),),
        gates=(GateSpec(name="QDemo/Enable",
                        address="/avatar/parameters/QDemo/Enable"),),
        source=None)


def puppet_manifest(mid: int, *, bits=3, float_tau=0.12) -> QuantManifest:
    """A manifest declaring a channel at index_puppet's own Left_X address."""
    return QuantManifest(
        id=mid, revision=1,
        channels=(ChannelSpec(name="IndexPuppet/Left_X",
                              address="/avatar/parameters/IndexPuppet/Left_X",
                              bits=bits, signed=True, float_tau=float_tau),),
        gates=(), source=None)


class FakeOsc:
    """Serves scripted FetchResults, one per fetch, and can park a fetch mid-flight."""

    def __init__(self):
        self.results: list[FetchResult] = []
        self.fetches: list[str] = []
        self.target_is_pinned = False
        self._lock = threading.Lock()
        # When set, the next fetch parks: it signals `parked` and blocks on `release`.
        self.hold_next = False
        self.parked = threading.Event()
        self.release = threading.Event()

    def script(self, *results: FetchResult) -> None:
        with self._lock:
            self.results.extend(results)

    def fetch(self, address: str, timeout: float = 2.0) -> FetchResult:
        with self._lock:
            self.fetches.append(address)
            hold = self.hold_next
            self.hold_next = False
        if hold:
            self.parked.set()
            assert self.release.wait(5.0), "a parked fetch was never released"
            self.release.clear()
        with self._lock:
            if self.results:
                return self.results.pop(0)
        return FetchResult(FETCH_NOT_FOUND, peer=VRCHAT)


class FakeBridge:
    log = logging.getLogger("vrbridge")

    def __init__(self):
        self.osc = FakeOsc()
        self._osc_cbs: dict[str, list] = {}
        self._target_cbs: list = []

    def on_osc(self, address, callback, **kw):
        self._osc_cbs.setdefault(address, []).append(callback)

    def on_target_selected(self, callback):
        self._target_cbs.append(callback)

    # -- drivers, the way the real bridge would fire them --

    def change(self, avatar_id="avtr_x"):
        for cb in self._osc_cbs.get(AVATAR_CHANGE_ADDR, []):
            cb(None, AVATAR_CHANGE_ADDR, avatar_id)

    def select_target(self):
        for cb in self._target_cbs:
            cb(None, ("127.0.0.1", 9000))


def rig(manifests=None, **kw) -> tuple[FakeBridge, QuantChannelDirectory]:
    bridge = FakeBridge()
    d = QuantChannelDirectory(
        bridge, manifests,
        tuning=kw.pop("tuning", QuantChannelSettings()),
        puppet_tuning=kw.pop("puppet_tuning", PuppetSettings()),
        **kw)
    d.register()
    d.activate()
    return bridge, d


def wait_for(cond, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.005)
    return False


def armed_id(d: QuantChannelDirectory) -> Optional[int]:
    m = d.active_manifest()
    return None if m is None else m.id


# --------------------------------------------------------------------------
# Arming
# --------------------------------------------------------------------------

def test_nothing_is_fetched_until_something_asks(monkeypatch):
    """Intended: an untouched bridge fetches nothing. There is no timer and no startup
    read -- invalidation events and consumer use are the only triggers."""
    bridge, d = rig({1: manifest(1)})
    time.sleep(0.1)
    assert bridge.osc.fetches == []


def test_a_use_kicks_a_fetch_and_arms_the_manifest():
    """Intended: read-on-use. The first consumer ask returns None (the fetch is async by
    design -- a consumer at controller rate must never block), kicks the worker, and a
    later ask finds the latch armed."""
    bridge, d = rig({1: manifest(1)})
    bridge.osc.script(FetchResult(FETCH_OK, value=1, peer=VRCHAT))
    assert d.active_manifest() is None
    assert wait_for(lambda: armed_id(d) == 1), "the use-kicked fetch never armed"
    assert bridge.osc.fetches == [MARKER_ADDR]


def test_an_avatar_change_fires_no_fetch_of_its_own():
    """Intended: the change only invalidates. A fetch kicked on the change races the cold
    load and can read the OUTGOING avatar's tree -- and a wrong manifest armed there
    latches, because no later event corrects it. The next consumer use is the read, made
    prompt by the invalidation zeroing the retry floor."""
    bridge, d = rig({1: manifest(1)})
    bridge.osc.script(FetchResult(FETCH_OK, value=1, peer=VRCHAT))
    bridge.change()
    time.sleep(0.1)
    assert bridge.osc.fetches == [], "the change itself fetched"
    d.active_manifest()
    assert wait_for(lambda: armed_id(d) == 1)
    assert bridge.osc.fetches == [MARKER_ADDR]


def test_an_armed_latch_is_not_re_fetched_per_use():
    """Intended: the latch is the answer between invalidations. The wardrobe re-reads per
    press because presses are rare; a quant consumer asks at controller rate, and per-use
    reads would be a 90 Hz HTTP poll."""
    bridge, d = rig({1: manifest(1)})
    bridge.osc.script(FetchResult(FETCH_OK, value=1, peer=VRCHAT))
    assert d.active_manifest() is None
    assert wait_for(lambda: armed_id(d) == 1)
    for _ in range(50):
        assert d.active_manifest().id == 1
    assert len(bridge.osc.fetches) == 1, "an armed directory kept fetching"


# --------------------------------------------------------------------------
# Probe 4: manifest switching across /avatar/change
# --------------------------------------------------------------------------

def test_probe4_switching_avatars_switches_manifests():
    """Intended: two loaded manifests, two avatars; each change disarms and the next read
    arms the table the *new* avatar's sentinel names."""
    bridge, d = rig({1: manifest(1), 2: manifest(2, name="QOther/RX")})
    bridge.osc.script(FetchResult(FETCH_OK, value=1, peer=VRCHAT))
    d.active_manifest()
    assert wait_for(lambda: armed_id(d) == 1)

    bridge.osc.script(FetchResult(FETCH_OK, value=2, peer=VRCHAT))
    bridge.change("avtr_second")
    assert wait_for(lambda: armed_id(d) == 2), "the change did not re-arm to the new table"
    assert d.active_manifest().channel("QOther/RX") is not None


def test_a_change_to_an_avatar_without_channels_disarms():
    """Intended: disarm-on-change is immediate and synchronous -- the old table must not
    answer for the new avatar even for the moment the fetch is in flight."""
    bridge, d = rig({1: manifest(1)})
    bridge.osc.script(FetchResult(FETCH_OK, value=1, peer=VRCHAT))
    d.active_manifest()
    assert wait_for(lambda: armed_id(d) == 1)

    bridge.osc.script(FetchResult(FETCH_NOT_FOUND, peer=VRCHAT))
    bridge.change("avtr_plain")
    assert d.active_manifest() is None, "the old manifest answered past its avatar"
    time.sleep(0.1)
    assert d.active_manifest() is None


def test_a_target_change_disarms_like_an_avatar_change():
    """Intended: a different client means a different worn avatar; the target-selected leg
    runs on zeroconf's dispatch thread, so all it may do is clear."""
    bridge, d = rig({1: manifest(1)})
    bridge.osc.script(FetchResult(FETCH_OK, value=1, peer=VRCHAT))
    d.active_manifest()
    assert wait_for(lambda: armed_id(d) == 1)

    bridge.osc.script(FetchResult(FETCH_OK, value=1, peer=VRCHAT))
    bridge.select_target()
    assert d.active_manifest() is None, "the old client's manifest answered past it"
    # Re-arms, but only through a fresh use-triggered read of the new client.
    assert wait_for(lambda: len(bridge.osc.fetches) == 2)
    assert wait_for(lambda: armed_id(d) == 1)


# --------------------------------------------------------------------------
# The token discipline
# --------------------------------------------------------------------------

def test_a_fetch_completing_after_a_later_invalidation_must_not_arm():
    """Intended: the sequence token. A cold load means a fetch can be in flight for
    seconds; if a second change lands meanwhile, the older fetch's answer describes an
    avatar that is no longer worn, and letting it latch is the wrong-manifest failure the
    redesign exists to prevent."""
    bridge, d = rig({1: manifest(1), 2: manifest(2)})
    bridge.osc.hold_next = True
    bridge.osc.script(FetchResult(FETCH_OK, value=1, peer=VRCHAT))   # the stale answer

    bridge.change("avtr_first")
    d.active_manifest()                           # the use that kicks fetch 1
    assert bridge.osc.parked.wait(3.0), "the first fetch never started"

    bridge.change("avtr_second")                  # invalidates while fetch 1 is in flight
    bridge.osc.script(FetchResult(FETCH_OK, value=2, peer=VRCHAT))
    bridge.osc.release.set()                      # fetch 1 completes late, carrying id 1

    assert wait_for(lambda: armed_id(d) == 2), "the fresh fetch should arm manifest 2"
    assert armed_id(d) == 2
    time.sleep(0.1)
    assert armed_id(d) == 2, "the stale fetch's manifest latched over the current one"


# --------------------------------------------------------------------------
# Retry semantics
# --------------------------------------------------------------------------

def test_use_retries_are_floored_not_per_call():
    """Intended: while unarmed, use re-kicks the worker at most once per
    REARM_FLOOR_SECS. A consumer polling at controller rate must not translate into a
    controller-rate fetch stream against an avatar that has no channels."""
    bridge, d = rig({1: manifest(1)})
    # Every fetch 404s: an avatar without quant channels.
    for _ in range(20):
        d.active_manifest()
        time.sleep(0.005)
    assert wait_for(lambda: len(bridge.osc.fetches) == 1)
    time.sleep(0.2)
    assert len(bridge.osc.fetches) == 1, \
        f"{len(bridge.osc.fetches)} fetches inside the floor window"


def test_a_404_answers_now_not_forever(monkeypatch):
    """Intended: the cold-load fix. A 404 read mid-download is true *right now*; once the
    avatar finishes loading, the next use past the floor asks again and arms. Latching the
    404 would leave a cold-loaded avatar dark until some future avatar change."""
    monkeypatch.setattr(osc_quant, "REARM_FLOOR_SECS", 0.05)
    bridge, d = rig({1: manifest(1)})
    bridge.osc.script(FetchResult(FETCH_NOT_FOUND, peer=VRCHAT))   # mid-download
    d.active_manifest()
    assert wait_for(lambda: len(bridge.osc.fetches) == 1)
    assert d.active_manifest() is None

    time.sleep(0.1)                                # past the (shrunk) floor; avatar loaded
    bridge.osc.script(FetchResult(FETCH_OK, value=1, peer=VRCHAT))
    d.active_manifest()
    assert wait_for(lambda: armed_id(d) == 1), "the 404 was latched and killed the arm"


def test_a_foreign_peers_404_stays_retryable(monkeypatch, caplog):
    """Intended: a 404 from a peer that does not identify itself as VRChat says nothing
    about the worn avatar (VRCFT/VRCOSC can hold an empty target slot). It must be named
    as the peer's, and the directory must keep asking once a real client can answer."""
    monkeypatch.setattr(osc_quant, "REARM_FLOOR_SECS", 0.05)
    bridge, d = rig({1: manifest(1)})
    bridge.osc.script(FetchResult(FETCH_NOT_FOUND, peer=STRANGER))
    with caplog.at_level("INFO"):
        d.active_manifest()
        assert wait_for(lambda: len(bridge.osc.fetches) == 1)
    assert "does not identify itself as VRChat" in caplog.text
    assert "declares no quant-channel sentinel" not in caplog.text, \
        "a stranger's 404 was reported as a fact about the worn avatar"

    time.sleep(0.1)
    bridge.osc.script(FetchResult(FETCH_OK, value=1, peer=VRCHAT))
    d.active_manifest()
    assert wait_for(lambda: armed_id(d) == 1), "the foreign-peer 404 latched the directory dark"


def test_a_transport_failure_is_retried_past_the_floor(monkeypatch):
    """Intended: a failed read says nothing about the avatar and must not settle."""
    monkeypatch.setattr(osc_quant, "REARM_FLOOR_SECS", 0.05)
    bridge, d = rig({1: manifest(1)})
    bridge.osc.script(FetchResult(FETCH_TRANSPORT, detail="timed out", peer=VRCHAT))
    d.active_manifest()
    assert wait_for(lambda: len(bridge.osc.fetches) == 1)
    time.sleep(0.1)
    bridge.osc.script(FetchResult(FETCH_OK, value=1, peer=VRCHAT))
    d.active_manifest()
    assert wait_for(lambda: armed_id(d) == 1)


def test_outcomes_are_reported_once_not_once_per_attempt(monkeypatch, caplog):
    """Intended: the `_report` dedupe. A pinned-target session with no named manifest
    would otherwise log FETCH_NO_PEER on every floored retry, forever."""
    monkeypatch.setattr(osc_quant, "REARM_FLOOR_SECS", 0.02)
    bridge, d = rig({1: manifest(1)})
    bridge.osc.target_is_pinned = True
    bridge.osc.script(*[FetchResult(FETCH_NO_PEER)] * 5)
    with caplog.at_level("WARNING"):
        for _ in range(5):
            d.active_manifest()
            time.sleep(0.04)
    assert caplog.text.count("pinned_manifest_id") == 1
    assert "pinned_manifest_id" in caplog.text, "the dead end must name its escape hatch"


def test_a_non_integer_marker_is_refused():
    """Intended: bool is an int subclass and int() truncates, so a T or a 1.9 would arm
    manifest 1. Test the type instead of coercing -- the slot/marker rule."""
    bridge, d = rig({1: manifest(1)})
    bridge.osc.script(FetchResult(FETCH_OK, value=True, peer=VRCHAT))
    d.active_manifest()
    assert wait_for(lambda: len(bridge.osc.fetches) == 1)
    time.sleep(0.05)
    assert d.active_manifest() is None, "a boolean marker armed a manifest"


def test_an_unknown_marker_names_the_id_and_the_loaded_set(caplog):
    bridge, d = rig({1: manifest(1)})
    bridge.osc.script(FetchResult(FETCH_OK, value=42, peer=VRCHAT))
    with caplog.at_level("WARNING"):
        d.active_manifest()
        assert wait_for(lambda: "sentinel is 42" in caplog.text)
    assert "loaded: 1" in caplog.text
    assert d.active_manifest() is None


# --------------------------------------------------------------------------
# Pinning
# --------------------------------------------------------------------------

def test_a_pinned_manifest_arms_without_any_fetch():
    """Intended: a pinned send target (the Av3Emulator, --osc-port) serves no OSCQuery
    tree, so naming the manifest takes the question away -- design.md's named-peer rule.
    The kwarg is the whole interface: there is deliberately no CLI flag, because the CLI
    constructs routers with nothing else and no shipped router registers this mapping."""
    bridge, d = rig({7: manifest(7)}, pinned_manifest_id=7)
    d.active_manifest()
    assert wait_for(lambda: armed_id(d) == 7)
    assert bridge.osc.fetches == [], "a named manifest must not be read over HTTP"


def test_a_pinned_id_no_manifest_claims_says_so(caplog):
    bridge, d = rig({1: manifest(1)}, pinned_manifest_id=9)
    with caplog.at_level("WARNING"):
        d.active_manifest()
        assert wait_for(lambda: "sentinel is 9" in caplog.text)
    assert d.active_manifest() is None


# --------------------------------------------------------------------------
# The puppet cross-check
# --------------------------------------------------------------------------

def test_a_manifest_disagreeing_with_puppet_settings_is_refused_naming_both(caplog):
    """Intended: "manifest is authoritative" has to be enforced for the one rig it exists
    to describe. A manifest declaring IndexPuppet/* channels at widths the [puppet]
    settings do not drive would arm a table that mis-describes every datagram; the arm is
    refused with both numbers in the message, so the fix is a diff on whichever is stale."""
    bridge, d = rig({1: puppet_manifest(1, bits=4)},
                    puppet_tuning=PuppetSettings(quant_level=3))
    bridge.osc.script(FetchResult(FETCH_OK, value=1, peer=VRCHAT))
    with caplog.at_level("ERROR"):
        d.active_manifest()
        assert wait_for(lambda: "Refusing to arm" in caplog.text)
    assert d.active_manifest() is None
    assert "bits=4" in caplog.text and "quant_level is 3" in caplog.text, \
        "the refusal must name both values"


def test_the_cross_check_also_covers_float_tau(caplog):
    bridge, d = rig({1: puppet_manifest(1, float_tau=0.5)},
                    puppet_tuning=PuppetSettings(float_smooth_tau_secs=0.12))
    bridge.osc.script(FetchResult(FETCH_OK, value=1, peer=VRCHAT))
    with caplog.at_level("ERROR"):
        d.active_manifest()
        assert wait_for(lambda: "Refusing to arm" in caplog.text)
    assert "floatTau=0.5" in caplog.text and "0.12" in caplog.text


def test_the_cross_check_also_covers_signedness(caplog):
    """Intended: index_puppet hardcodes signed=True, so a puppet-address manifest saying
    signed=false describes a wire without the Negative address the mapping drives."""
    m = QuantManifest(
        id=1, revision=1,
        channels=(ChannelSpec(name="IndexPuppet/Left_X",
                              address="/avatar/parameters/IndexPuppet/Left_X",
                              bits=3, signed=False, float_tau=0.12),),
        gates=(), source=None)
    bridge, d = rig({1: m}, puppet_tuning=PuppetSettings(quant_level=3,
                                                         float_smooth_tau_secs=0.12))
    bridge.osc.script(FetchResult(FETCH_OK, value=1, peer=VRCHAT))
    with caplog.at_level("ERROR"):
        d.active_manifest()
        assert wait_for(lambda: "Refusing to arm" in caplog.text)
    assert "signed=false" in caplog.text
    assert d.active_manifest() is None


def test_a_matching_puppet_manifest_arms():
    """The cross-check must pass the true configuration, or the flagship consumer can
    never arm at all."""
    bridge, d = rig({1: puppet_manifest(1, bits=3, float_tau=0.12)},
                    puppet_tuning=PuppetSettings(quant_level=3,
                                                 float_smooth_tau_secs=0.12))
    bridge.osc.script(FetchResult(FETCH_OK, value=1, peer=VRCHAT))
    d.active_manifest()
    assert wait_for(lambda: armed_id(d) == 1)


def test_a_manifest_with_no_puppet_addresses_skips_the_cross_check():
    """Intended: the check binds only where the manifest and the puppet share addresses;
    a face-tracking manifest must not be hostage to [puppet] settings it never touches."""
    bridge, d = rig({1: manifest(1)},
                    puppet_tuning=PuppetSettings(quant_level=8))
    bridge.osc.script(FetchResult(FETCH_OK, value=1, peer=VRCHAT))
    d.active_manifest()
    assert wait_for(lambda: armed_id(d) == 1)


# --------------------------------------------------------------------------
# Router discipline
# --------------------------------------------------------------------------

def test_a_disabled_directory_does_not_fetch_and_does_not_resurrect(caplog):
    """Intended: `enabled` is the router's. The ungated invalidate still clears state on a
    disabled directory (so re-enabling does not resume stale), but the worker declines to
    fetch and use returns None without kicking."""
    bridge, d = rig({1: manifest(1)})
    bridge.osc.script(FetchResult(FETCH_OK, value=1, peer=VRCHAT))
    d.active_manifest()
    assert wait_for(lambda: armed_id(d) == 1)

    d.deactivate()
    bridge.change()                      # ungated: runs even while disabled, clears
    assert not d.enabled
    assert d.active_manifest() is None
    time.sleep(0.1)
    assert len(bridge.osc.fetches) == 1, "a disabled directory fetched"

    d.activate()
    bridge.osc.script(FetchResult(FETCH_OK, value=1, peer=VRCHAT))
    d.active_manifest()
    assert wait_for(lambda: armed_id(d) == 1), "re-enabling did not re-arm through a fresh read"
