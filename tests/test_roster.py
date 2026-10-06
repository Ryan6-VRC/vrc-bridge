"""The log-read instance roster: line parsing, roster rules, file selection, the tailer.

Intent first (module docstring of `vrbridge.roster`): players are keyed on the
`usr_` id with the name matched from the end of the line; a join/leave line in a
shape we do not know is reported rather than dropped; a room start clears the
roster; the tailer replays silently, announces one snapshot, then delivers
deltas as lines complete. Every name and id here is synthetic.
"""

import logging
import os
import threading
import time

import pytest

from vrbridge.roster import (
    AvatarDataLoaded, AvatarDataSaved, AvatarInitialized, AvatarRemeasured, AvatarSwitch,
    EnteringRoom, JoinedRoom, JoiningWorld, LeftRoom, LogTailer, PlayerJoined, PlayerLeft,
    SERVICE_NOT_FOUND, Roster, SelfIdentity, ServiceAdvertised, Unparsed, parse_line,
    select_log_file,
)

ALICE = "usr_00000000-0000-4000-8000-000000000001"
BOB = "usr_00000000-0000-4000-8000-000000000002"
WORLD = "wrld_00000000-0000-4000-8000-0000000000aa"


def line(msg, level="Debug"):
    return f"2026.09.30 12:34:56 {level:<10} -  {msg}"


def _file(dir_, name, *msgs, mtime=None):
    p = dir_ / name
    p.write_text("".join(line(m) + "\n" for m in msgs), encoding="utf-8")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


# --- parsing ----------------------------------------------------------------

@pytest.mark.parametrize("msg, expected", [
    (f"User Authenticated: Alice Example ({ALICE})", SelfIdentity(ALICE, "Alice Example")),
    ("Advertising Service VRChat-Client-ABC123 of type OSCQuery on 54321",
     ServiceAdvertised("VRChat-Client-ABC123", 54321)),
    ("[Behaviour] Entering Room: Example World", EnteringRoom("Example World")),
    (f"[Behaviour] Joining {WORLD}:12345~hidden({ALICE})~region(us)",
     JoiningWorld(WORLD, f"12345~hidden({ALICE})~region(us)")),
    ("[Behaviour] Successfully joined room", JoinedRoom()),
    (f"[Behaviour] OnPlayerJoined Alice Example ({ALICE})", PlayerJoined(ALICE, "Alice Example")),
    (f"[Behaviour] OnPlayerLeft Bob ({BOB})", PlayerLeft(BOB, "Bob")),
    ("[Behaviour] OnLeftRoom", LeftRoom()),
])
def test_each_line_shape_parses_with_the_prefix_stripped(msg, expected):
    """Intended: the timestamp/level prefix is not part of any field, at any level."""
    assert parse_line(line(msg)) == expected
    assert parse_line(line(msg, level="Warning")) == expected
    assert parse_line(line(msg) + "\r\n") == expected


def test_a_name_with_spaces_and_parentheses_keeps_everything_before_the_id():
    """Intended: the id is matched from the end, so a name's own parentheses stay in the name."""
    ev = parse_line(line(f"[Behaviour] OnPlayerJoined Bob (the) Builder (x) ({BOB})"))
    assert ev == PlayerJoined(BOB, "Bob (the) Builder (x)")


@pytest.mark.parametrize("msg", [
    "[Behaviour] OnPlayerJoined Alice Example",  # the pre-id shape
    "[Behaviour] OnPlayerLeft",
])
def test_a_join_or_leave_in_an_unknown_shape_is_reported_not_dropped(msg):
    """Intended: a format change surfaces as Unparsed, never as an exception or a None."""
    ev = parse_line(line(msg))
    assert isinstance(ev, Unparsed)
    assert ev.verb in ("OnPlayerJoined", "OnPlayerLeft")


@pytest.mark.parametrize("msg", [
    "[Behaviour] OnPlayerLeftRoom",
    "[Behaviour] Destroying Alice Example",
    "[Behaviour] Joining or Creating Room: Example World",
    "[Behaviour] Switching to network region usw (current state: ConnectedToNameServer)",
    "Measure Human Avatar Avatar isRemeasure:False",
    "Saving Avatar Data:",
    "Something else entirely",
    "",
])
def test_neighbouring_lines_are_ignored(msg):
    """Intended: only the listed shapes are events; OnPlayerLeftRoom is not OnPlayerLeft."""
    assert parse_line(line(msg)) is None


AVTR = "avtr_00000000-0000-4000-8000-0000000000bb"


@pytest.mark.parametrize("msg, expected", [
    ("[Behaviour] Switching Alice Example to avatar Some Avatar",
     AvatarSwitch("Alice Example", "Some Avatar")),
    (f"Saving Avatar Data:{AVTR}", AvatarDataSaved(AVTR)),
    (f"Loading Avatar Data:{AVTR}", AvatarDataLoaded(AVTR)),
    ("[Behaviour] Initialize Limb Avatar VRCPlayer[Local] 2 True 1",
     AvatarInitialized("Limb", True)),
    ("[Behaviour] Initialize SixPoint Avatar VRCPlayer[Local] 2 True 8",
     AvatarInitialized("SixPoint", True)),
    ("[Behaviour] Initialize ThreePoint Avatar VRCPlayer[Remote] 1 False 8",
     AvatarInitialized("ThreePoint", False)),
    ("Measure Human Avatar Avatar isRemeasure:True", AvatarRemeasured()),
])
def test_each_avatar_load_line_parses(msg, expected):
    """Intended: the local avatar's load lines osc_persist classifies a reload by, with Local
    told from Remote; the roster itself ignores them."""
    assert parse_line(line(msg)) == expected
    assert Roster().apply(expected) is None


def test_a_switch_splits_the_player_at_the_first_to_avatar():
    """Intended: a player name containing " to avatar " splits early, so a consumer comparing
    the player to its own name fails closed rather than matching someone else."""
    ev = parse_line(line("[Behaviour] Switching Mr to avatar Man to avatar Some Avatar"))
    assert ev == AvatarSwitch("Mr", "Man to avatar Some Avatar")


# --- roster rules -----------------------------------------------------------

def _in_room(r):
    r.apply(EnteringRoom("Example World"))
    r.apply(JoiningWorld(WORLD, "1~region(us)"))
    r.apply(JoinedRoom())
    r.apply(PlayerJoined(ALICE, "Alice Example"))
    return r


def test_a_room_start_clears_the_players_and_either_order_composes():
    """Intended: Entering Room and Joining wrld_ both start a room; the pair, in either
    order, leaves both the name and the world id set, and a new pair replaces the old."""
    r = _in_room(Roster())
    assert r.apply(JoiningWorld("wrld_other", "2")) == "room"
    assert r.players == {} and r.joined is False
    assert (r.world_id, r.room_name) == ("wrld_other", None)  # old room's name not carried
    r.apply(EnteringRoom("Other World"))
    assert (r.world_id, r.instance, r.room_name) == ("wrld_other", "2", "Other World")


def test_join_is_idempotent_and_leave_of_an_absent_id_is_no_change():
    """Intended: a repeated join of one id, or a leave of an unknown id, is not a change."""
    r = _in_room(Roster())
    assert r.apply(PlayerJoined(ALICE, "Alice Example")) is None
    assert r.apply(PlayerJoined(BOB, "Bob")) == "join"
    assert r.apply(PlayerLeft("usr_absent", "Nobody")) is None
    assert r.apply(PlayerLeft(BOB, "Bob")) == "leave"
    assert list(r.players) == [ALICE]


def test_left_room_clears_players_and_joined():
    """Intended: OnLeftRoom empties the roster; a second one is not a change."""
    r = _in_room(Roster())
    assert r.apply(LeftRoom()) == "left"
    assert r.players == {} and r.joined is False
    # The world identity goes too, so the next room's first start line cannot be snapshotted
    # beside the old world's id or name.
    assert r.snapshot()["world"] is None
    assert r.apply(LeftRoom()) is None


def test_snapshot_is_json_ready_and_ordered_by_arrival():
    """Intended: the snapshot shape the external client reads, players in join order."""
    r = Roster()
    assert r.snapshot() == {"self": None, "world": None, "joined": False, "players": []}
    r.apply(SelfIdentity(ALICE, "Alice Example"))
    _in_room(r)
    r.apply(PlayerJoined(BOB, "Bob"))
    assert r.snapshot() == {
        "self": {"id": ALICE, "name": "Alice Example"},
        "world": {"id": WORLD, "instance": "1~region(us)", "name": "Example World"},
        "joined": True,
        "players": [{"id": ALICE, "name": "Alice Example"}, {"id": BOB, "name": "Bob"}],
    }


# --- file selection ---------------------------------------------------------

def test_selection_binds_by_service_name_and_falls_back_to_newest(tmp_path):
    """Intended: the advertised service name picks its own log even when it is not the
    newest; without a name, or with a name nobody advertised, the newest wins."""
    adv = "Advertising Service VRChat-Client-{} of type OSCQuery on 5000"
    old = _file(tmp_path, "output_log_2026-09-30_10-00-00.txt", adv.format("AAAAAA"), mtime=1000)
    new = _file(tmp_path, "output_log_2026-09-30_11-00-00.txt", adv.format("BBBBBB"), mtime=2000)
    assert select_log_file(tmp_path, "VRChat-Client-AAAAAA") == (old, "service")
    assert select_log_file(tmp_path, None) == (new, "newest")
    assert select_log_file(tmp_path, "VRChat-Client-ZZZZZZ") == (new, "newest (service not found)")
    assert select_log_file(tmp_path / "missing", None) == (None, "none")


# --- tailer -----------------------------------------------------------------

class _Recorder:
    def __init__(self):
        self.calls = []
        self.cond = threading.Condition()

    def __call__(self, roster, change):
        with self.cond:
            self.calls.append((change, roster.snapshot()))
            self.cond.notify_all()

    def wait_for(self, n, timeout=1.0):
        with self.cond:
            self.cond.wait_for(lambda: len(self.calls) >= n, timeout)
        return [c for c, _ in self.calls]


def _append(path, text):
    with open(path, "a", encoding="utf-8", newline="") as fh:
        fh.write(text)


def test_tailer_replays_silently_then_delivers_appended_lines(tmp_path):
    """Intended: the existing file produces exactly one "snapshot" call carrying the
    replayed roster; a line appended later arrives as its own delta; a line written
    in two pieces is applied only once its newline arrives."""
    p = _file(tmp_path, "output_log_2026-09-30_10-00-00.txt",
              "[Behaviour] Entering Room: Example World",
              f"[Behaviour] Joining {WORLD}:1",
              "[Behaviour] Successfully joined room",
              f"[Behaviour] OnPlayerJoined Alice Example ({ALICE})")
    rec = _Recorder()
    t = LogTailer(rec, log_dir=tmp_path, poll_secs=0.02)
    t.start()
    try:
        assert rec.wait_for(1) == ["snapshot"]
        assert [pl["id"] for pl in rec.calls[0][1]["players"]] == [ALICE]
        assert t.rule == "newest"

        _append(p, line(f"[Behaviour] OnPlayerJoined Bob ({BOB})") + "\n")
        assert rec.wait_for(2) == ["snapshot", "join"]

        _append(p, line(f"[Behaviour] OnPlayerLeft Bob ({BOB}"))
        time.sleep(0.1)
        assert len(rec.calls) == 2, "a partial line must not be applied"
        _append(p, ")\n")
        assert rec.wait_for(3) == ["snapshot", "join", "leave"]
        assert [pl["id"] for pl in t.snapshot()["players"]] == [ALICE]
    finally:
        t.stop()
    assert not t._thread.is_alive()


def test_tailer_idles_without_files_and_picks_one_up_on_retarget(tmp_path, caplog):
    """Intended: a missing directory is not an error — the tailer logs once, idles,
    and a retarget after the log appears selects it by service name and replays."""
    rec = _Recorder()
    # retry_secs short enough that several attempts fall in the idle window: the test is that
    # the idle line is logged once across attempts, not that only one attempt happened.
    t = LogTailer(rec, log_dir=tmp_path, poll_secs=0.02, retry_secs=0.02,
                  logger=logging.getLogger("roster-test"))
    with caplog.at_level(logging.INFO, logger="roster-test"):
        t.start()
        try:
            time.sleep(0.1)
            assert rec.calls == [] and t.path is None
            _file(tmp_path, "output_log_2026-09-30_10-00-00.txt",
                  "Advertising Service VRChat-Client-ABC123 of type OSCQuery on 5000")
            t.retarget("VRChat-Client-ABC123")
            assert rec.wait_for(1) == ["snapshot"]
            assert t.rule == "service"
        finally:
            t.stop()
    assert sum("idle" in r.getMessage() for r in caplog.records) == 1


def test_a_tailer_that_has_not_found_its_client_keeps_looking_without_replaying(tmp_path):
    """Intended: the client can be selected before its log names it, so a log chosen as
    "newest (service not found)" is re-checked on the retry interval. A retry that finds
    nothing new replays nothing; once the log names the client it is followed by service."""
    _file(tmp_path, "output_log_2026-09-30_10-00-00.txt",
          "[Behaviour] Entering Room: Example World", mtime=time.time() - 60)
    rec = _Recorder()
    t = LogTailer(rec, log_dir=tmp_path, service_name="VRChat-Client-ABC123", poll_secs=0.02,
                  retry_secs=0.05)
    t.start()
    try:
        assert rec.wait_for(1) == ["snapshot"]
        assert t.rule == SERVICE_NOT_FOUND
        time.sleep(0.3)
        assert len(rec.calls) == 1, "a retry that found nothing new replayed"
        # The client's own log, appearing after the bridge chose: newer, and naming it.
        named = _file(tmp_path, "output_log_2026-09-30_10-05-00.txt",
                      "Advertising Service VRChat-Client-ABC123 of type OSCQuery on 5000")
        assert rec.wait_for(2) == ["snapshot", "snapshot"]
        assert (t.path, t.rule) == (named, "service")
    finally:
        t.stop()


def test_tailer_logs_an_unparsed_join_once(tmp_path, caplog):
    """Intended: an unknown join shape is surfaced to the operator once, not per line."""
    _file(tmp_path, "output_log_2026-09-30_10-00-00.txt",
          "[Behaviour] OnPlayerJoined Alice Example",
          "[Behaviour] OnPlayerJoined Bob")
    rec = _Recorder()
    t = LogTailer(rec, log_dir=tmp_path, poll_secs=0.02, logger=logging.getLogger("roster-test"))
    with caplog.at_level(logging.WARNING, logger="roster-test"):
        t.start()
        try:
            rec.wait_for(1)
        finally:
            t.stop()
    assert sum("OnPlayerJoined" in r.getMessage() for r in caplog.records) == 1
    assert rec.calls[0][1]["players"] == []
