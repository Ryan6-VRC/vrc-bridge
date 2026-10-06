"""The client-log reload signature, re-derived from real log lines.

`fixtures/client_log_reloads.txt` is eight client launches' worth of real log lines, scrubbed, with
each local switch labelled from ground truth that is not the log, and each label naming its
source: `operator` (labelled live), `trackingtype` (`TrackingType` 6 -> 4 on the wire at the
accept), `upright-fast` (a first `Upright` tens of ms after the change, so not held),
`upright-run` (a slow first `Upright` in a calibration run: the weak kind, since an
operator-labelled Reset Avatar also had one), `structural` (a room transition or a different id)
and `none`. `osc_persist._ClientLog` reads the lines as the tailer hands them over, and at each
label the switch's verdict must agree with it. The tally is asserted per source, so a fixture
that silently lost its independently labelled cases fails here rather than passing on the weak
ones.

The synthetic cases below hold the guards the fixture cannot break: a fixture with no
spontaneous save inside a switch passes whether or not the saving window is checked.
"""
from datetime import datetime
from pathlib import Path

import pytest

from vrbridge.mappings.osc_persist import _ClientLog
from vrbridge.roster import parse_line

FIXTURE = Path(__file__).parent / "fixtures" / "client_log_reloads.txt"
ME = "Local Player"
WORN = "avtr_00000001-0000-0000-0000-000000000000"
OTHER = "avtr_00000002-0000-0000-0000-000000000000"


def _clock(raw: str, prev: float) -> float:
    """The line's own timestamp as the read time, a hair after the previous line's, so the
    accept's quiet window is measured on the client's clock rather than on line counts."""
    try:
        stamp = datetime.strptime(raw[:19], "%Y.%m.%d %H:%M:%S").timestamp()
    except ValueError:
        return prev
    return max(stamp, prev + 0.001)


def _replay():
    """(label, source, worn, kind) at every label line; accepts checked inline. Also the room
    key after each `Joining` line, per launch: {log: [key, ...]}."""
    log, seen, rooms, accepted, now, n = _ClientLog(), [], {}, None, 0.0, 0
    for raw in FIXTURE.read_text(encoding="utf-8").splitlines():
        now = _clock(raw, now)
        if raw.startswith("## log "):
            n = int(raw.split()[2])
            log.reset("service", None, None)      # a fresh launch is a replay
            continue
        if raw == "## accepted":
            assert accepted is log.switch and accepted is not None, f"no accept before {raw!r}"
            continue
        if raw.startswith("## "):
            worn, label, source = raw.split()[1:4]
            assert log.switch is not None and log.switch.settled_at is not None, raw
            seen.append((label, source, worn, log.switch.kind(worn)))
            continue
        if raw.startswith("#") or not raw:
            continue
        got = log.feed(parse_line(raw), now)
        if got is not None and got[0] == "accepted":
            accepted = got[1]
        if "] Joining wrld_" in raw:
            rooms.setdefault(n, []).append(log.room)
    return seen, rooms


def test_every_labelled_reload_gets_its_verdict_from_the_log():
    """Intended: a calibration is exactly the switch that saved the worn id before the
    placeholder and loaded none of it after no room transition. Every labelled calibration
    reads as one; every Reset Avatar reads as a reset and every join as a join, and no swap
    reads as a calibration."""
    want = {"calibration": "calibration", "reset": "reset", "join": "join"}
    wrong = [(label, source, worn, kind) for label, source, worn, kind in _replay()[0]
             if (label in want and kind != want[label])
             or (label == "swap" and kind == "calibration")]
    assert wrong == []


def test_the_fixture_still_holds_the_independently_labelled_cases():
    """Intended: the evidence the classifier was approved on, counted by source. Calibrations:
    6 operator-labelled, 6 marked by TrackingType at the accept, 10 resting on a slow Upright in
    a calibration run. Resets: 2 operator-labelled, 2 with a first Upright in tens of ms. Joins
    and swaps are structural regression rows; 4 same-id reloads have no ground truth."""
    counts = {}
    for label, source, _, _ in _replay()[0]:
        counts[(label, source)] = counts.get((label, source), 0) + 1
    assert counts == {
        ("calibration", "operator"): 6, ("calibration", "trackingtype"): 6,
        ("calibration", "upright-run"): 10,
        ("reset", "operator"): 2, ("reset", "upright-fast"): 2,
        ("join", "structural"): 11, ("swap", "structural"): 27, ("unlabelled", "none"): 4,
    }


def test_the_room_key_follows_the_instance_not_the_join():
    """Intended: a rejoin of the same instance keeps the room, and a move to another instance
    of the same world changes it. Log 4 rejoins instance 74105 at 13:54:40; log 7 moves from
    73101 to 45961 at 18:26:19."""
    rooms = _replay()[1]
    assert rooms[4][0] == rooms[4][1] and "74105" in rooms[4][0]
    assert rooms[7][0] != rooms[7][1]
    assert "73101" in rooms[7][0] and "45961" in rooms[7][1]
    assert rooms[7][0].split(":")[0] == rooms[7][1].split(":")[0], "the same world"


# --------------------------------------------------------------------------
# Synthetic guards
# --------------------------------------------------------------------------

def L(msg: str) -> str:
    return f"2026.10.05 12:00:00 Debug      -  {msg}"


SWITCH = L(f"[Behaviour] Switching {ME} to avatar Some Avatar")
PLACEHOLDER = L("[Behaviour] Initialize Limb Avatar VRCPlayer[Local] 2 True 1")
AVATAR = L("[Behaviour] Initialize ThreePoint Avatar VRCPlayer[Local] 2 True 8")
SAVE = L(f"Saving Avatar Data:{WORN}")
LOAD = L(f"Loading Avatar Data:{WORN}")
REMEASURE = L("Measure Human Avatar Avatar isRemeasure:True")


def feed(*lines, log=None):
    log = log or _ClientLog()
    if log.self_name is None:
        log.reset("service", ME, "wrld_x:1")
    got = [log.feed(parse_line(ln), float(i)) for i, ln in enumerate(lines)]
    return log, [g for g in got if g is not None]


@pytest.mark.parametrize("lines, kind", [
    ((SWITCH, SAVE, PLACEHOLDER, AVATAR), "calibration"),
    ((SWITCH, PLACEHOLDER, LOAD, LOAD, AVATAR), "reset"),
    ((SWITCH, SAVE, PLACEHOLDER, LOAD, AVATAR), "reset"),
    ((SWITCH, PLACEHOLDER, SAVE, AVATAR), "none"),
    ((SWITCH, PLACEHOLDER, AVATAR, SAVE), "none"),
    ((SWITCH, PLACEHOLDER, AVATAR), "none"),
], ids=["calibration", "reset", "save-then-load-is-reset", "save-after-placeholder",
        "save-after-avatar", "nothing"])
def test_only_a_save_before_the_placeholder_makes_a_calibration(lines, kind):
    """Intended: the client also saves spontaneously, seconds to a minute after a load, so only
    a save inside the switch's window -- before the placeholder initialises -- counts, and a
    Loading of the worn id wins over any save, so a spontaneous save can never turn a Reset
    Avatar into a calibration."""
    log, _ = feed(*lines)
    assert log.switch.kind(WORN) == kind


def test_a_load_after_the_avatar_initialised_is_not_the_switchs():
    """Intended: only Loading before the avatar's own Initialize is the switch's."""
    log, _ = feed(SWITCH, SAVE, PLACEHOLDER, AVATAR, LOAD)
    assert log.switch.kind(WORN) == "calibration"


def test_a_remote_players_switch_inside_a_local_block_is_ignored():
    """Intended: every player's switch is logged; a remote one inside ours must not replace it."""
    log, _ = feed(SWITCH, SAVE,
                  L("[Behaviour] Switching Someone Else to avatar Other Avatar"),
                  PLACEHOLDER,
                  L("[Behaviour] Initialize Limb Avatar VRCPlayer[Remote] 1 False 1"),
                  AVATAR)
    assert log.switch.kind(WORN) == "calibration"
    assert log.switch.settled_at is not None


@pytest.mark.parametrize("room", [
    L("[Behaviour] OnLeftRoom"),
    L("[Behaviour] Entering Room: Somewhere"),
    L("[Behaviour] Joining wrld_y:2~region(us)"),
], ids=["left", "entering", "joining"])
def test_a_room_event_before_or_during_a_switch_makes_it_a_join(room):
    """Intended: a join's room transition precedes its switch, but one landing while the switch
    is current marks it too, so a calibration-shaped switch with a room move is a join."""
    before, _ = feed(room, SWITCH, SAVE, PLACEHOLDER, AVATAR)
    during, _ = feed(SWITCH, SAVE, room, PLACEHOLDER, AVATAR)
    assert before.switch.kind(WORN) == "join"
    assert during.switch.kind(WORN) == "join"


def test_the_placeholders_remeasure_is_not_the_accept():
    """Intended: the placeholder remeasures before the avatar settles; only a remeasure after
    the avatar's own Initialize is the accept, and only the first."""
    log, got = feed(SWITCH, SAVE, PLACEHOLDER, REMEASURE, AVATAR)
    assert [g[0] for g in got] == ["settled"]
    _, got = feed(REMEASURE, REMEASURE, log=log)
    assert [g[0] for g in got] == ["accepted"]
    assert log.switch.accepted


def test_the_room_is_unknown_between_leaving_and_the_next_joining():
    """Intended: a room key stands only from a `Joining` line; leaving or entering makes it
    unknown, so a decision in between can match no namespace's room."""
    log, _ = feed(L("[Behaviour] Joining wrld_y:2~region(us)"))
    assert log.room == "wrld_y:2~region(us)"
    feed(L("[Behaviour] OnLeftRoom"), log=log)
    assert log.room is None
    feed(L("[Behaviour] Joining wrld_y:2~region(us)"),
         L("[Behaviour] Entering Room: Somewhere"), log=log)
    assert log.room is None


# --------------------------------------------------------------------------
# The accept against other players' remeasures (real lines)
# --------------------------------------------------------------------------

NOISE = Path(__file__).parent / "fixtures" / "client_log_remeasure_noise.txt"
NOISE_ME = "Local Player"


def _noise_blocks():
    """{block: [line, ...]}, the launch's identity under ""."""
    blocks, name = {"": []}, ""
    for raw in NOISE.read_text(encoding="utf-8").splitlines():
        if raw.startswith("## "):
            name = raw[3:]
            blocks[name] = []
        elif raw and not raw.startswith("#"):
            blocks[name].append(raw)
    return blocks


def _restamp(raw: str, hms: str) -> str:
    return raw[:11] + hms + raw[19:]


def _feed_real(lines):
    """Feed the launch head, then `lines`, on the log's clock; the events returned per line."""
    log, now = _ClientLog(), 0.0
    log.reset("service", None, None)
    got = []
    for raw in _noise_blocks()[""] + list(lines):
        now = _clock(raw, now)
        got.append((raw, log.feed(parse_line(raw), now)))
    return log, [(raw, g[0]) for raw, g in got if g is not None]


def test_the_local_accept_in_a_quiet_log_is_read():
    """Intended: the real calibration's accept, 2 s after the avatar settled and 27 s after the
    last other player's load line, is read as the accept, and nothing after it is."""
    log, got = _feed_real(_noise_blocks()["calibration"])
    assert log.switch.kind(WORN) == "calibration"
    assert [(raw[11:19], what) for raw, what in got] == [("21:46:17", "settled"),
                                                         ("21:46:19", "accepted")]


def test_another_players_load_during_a_held_calibration_does_not_release():
    """Intended: with the user still in calibration (the real accept line removed), the
    remeasure written for another player's load at 21:46:42 is not the accept, and an accept
    inside the quiet window after that load is missed rather than guessed: a missed accept
    falls to the give-up, an early one ends the hold before the constraints solve. The accept
    once the log has been quiet for ACCEPT_QUIET_SECS is read."""
    block = _noise_blocks()["calibration"]
    held = [ln for ln in block if not ln.startswith("2026.10.05 21:46:19")]
    upto = [ln for ln in held if ln[11:19] <= "21:46:42"]
    accept = next(ln for ln in block if ln.startswith("2026.10.05 21:46:19"))
    log, got = _feed_real(upto + [_restamp(accept, "21:46:50")])
    assert [what for _, what in got] == ["settled"]
    assert not log.switch.accepted
    log, got = _feed_real(upto + [_restamp(accept, "21:47:03")])
    assert [(raw[11:19], what) for raw, what in got] == [("21:46:17", "settled"),
                                                         ("21:47:03", "accepted")]


def test_a_burst_of_unattributed_remeasures_does_not_release_a_held_calibration():
    """Intended: the real calibration's switch, moved to 22:05:30 in the burst world and never
    accepted, reads none of the 27 remeasures that follow in the next minute as its accept,
    since each comes within ACCEPT_QUIET_SECS of another player's load or of the one before."""
    cal = [ln for ln in _noise_blocks()["calibration"] if "21:46:16" <= ln[11:19] <= "21:46:17"]
    burst = _noise_blocks()["burst"]
    lines = ([ln for ln in burst if ln[11:19] < "22:05:30"]
             + [_restamp(ln, "22:05:30") for ln in cal]
             + [ln for ln in burst if ln[11:19] >= "22:05:30"])
    log, got = _feed_real(lines)
    assert sum("isRemeasure:True" in ln and ln[11:19] > "22:05:30" for ln in lines) == 27
    assert [what for _, what in got] == ["settled"]
    assert log.switch.kind(WORN) == "calibration" and not log.switch.accepted
