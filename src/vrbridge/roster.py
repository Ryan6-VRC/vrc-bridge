"""The instance roster — who is in the instance — read from the VRChat client's own log.

OSC carries no roster, so the log is the only source: the client writes
`output_log_YYYY-MM-DD_HH-MM-SS.txt` under `~/AppData/LocalLow/VRChat/VRChat`,
one file per client launch. Three layers, separable for testing:

- `parse_line` is pure: one log line in, one event or `None` out. The
  timestamp/level prefix is stripped by regex rather than fixed widths, because
  the level column is padded and the padding is the client's to change.
- `Roster` is the state those events build, and `apply` names what changed so a
  consumer can choose between sending a delta and sending a snapshot.
- `LogTailer` is the thread that replays a chosen file and then follows its tail.

Rulings:

- **Players are keyed on the `usr_` id**; display names ride along and are not
  unique. A name may contain spaces and parentheses, so the id group is matched
  from the *end* of the line and the name is everything before it.
- **A join/leave line that does not parse is reported, not dropped.** The format
  has changed before (ids were added to these lines), and a silently empty
  roster is indistinguishable from an empty instance. `parse_line` returns
  `Unparsed` for it; `LogTailer` logs the first one per verb.
- **The local player is a player.** The client writes its own `OnPlayerJoined`,
  and the roster keeps it like any other; `self` is reported separately.
- **`Entering Room` and `Joining wrld_` each start a new room**, and the client
  writes both for one transition, in an order this module does not depend on: a
  start event arriving while `joined` is true wipes all room fields first, while
  one arriving mid-transition (not yet joined) sets only its own fields, so the
  pair composes either way round.
- **The tailer does not follow a newer file on its own.** With no service name it
  takes the newest file once; a new client launch is picked up by `retarget`,
  which the bridge calls when discovery picks a client. Binding by service name
  is what ties a log to the client the bridge is actually talking to when two
  clients run on one machine.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NamedTuple, Optional, Union

DEFAULT_LOG_DIR = Path.home() / "AppData" / "LocalLow" / "VRChat" / "VRChat"
LOG_GLOB = "output_log_*.txt"
SERVICE_SCAN_LINES = 400
"""The `Advertising Service` line is written near the top of a launch's log."""
RETRY_SECS = 5.0

# --- events -----------------------------------------------------------------


@dataclass(frozen=True)
class SelfIdentity:
    id: str
    name: str


@dataclass(frozen=True)
class ServiceAdvertised:
    service_name: str
    port: int


@dataclass(frozen=True)
class EnteringRoom:
    name: str


@dataclass(frozen=True)
class JoiningWorld:
    world_id: str
    instance: Optional[str]


@dataclass(frozen=True)
class JoinedRoom:
    pass


@dataclass(frozen=True)
class PlayerJoined:
    id: str
    name: str


@dataclass(frozen=True)
class PlayerLeft:
    id: str
    name: str


@dataclass(frozen=True)
class LeftRoom:
    pass


@dataclass(frozen=True)
class Unparsed:
    """A line naming a roster verb whose body did not match the known shape."""
    verb: str
    line: str


RosterEvent = Union[SelfIdentity, ServiceAdvertised, EnteringRoom, JoiningWorld, JoinedRoom,
                    PlayerJoined, PlayerLeft, LeftRoom, Unparsed]

# --- parsing ----------------------------------------------------------------

_PREFIX = re.compile(r"^\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2}\s+\w+\s+-\s+")
_NAME_AND_ID = re.compile(r"^(?P<name>.+) \((?P<id>usr_[^\s()]+)\)$")
_SELF = re.compile(r"^User Authenticated: (?P<rest>.+)$")
_SERVICE = re.compile(r"^Advertising Service (?P<name>\S+) of type OSCQuery on (?P<port>\d+)\b")
_ENTERING = re.compile(r"^\[Behaviour\] Entering Room: (?P<name>.*)$")
_JOINING = re.compile(r"^\[Behaviour\] Joining (?P<world>wrld_[^\s:]+)(?::(?P<instance>.*))?$")
_JOINED = "[Behaviour] Successfully joined room"
_LEFT_ROOM = "[Behaviour] OnLeftRoom"


def strip_prefix(line: str) -> str:
    """The message part of a log line; a line without the prefix is returned as-is."""
    return _PREFIX.sub("", line.rstrip("\r\n"), count=1).rstrip()


def parse_line(line: str) -> Optional[RosterEvent]:
    """One log line to its roster event, `Unparsed` for a malformed join/leave, else None."""
    msg = strip_prefix(line)
    if not msg:
        return None
    for verb, cls in (("OnPlayerJoined", PlayerJoined), ("OnPlayerLeft", PlayerLeft)):
        head = f"[Behaviour] {verb}"
        # The trailing-space test is what keeps `OnPlayerLeftRoom` out of `OnPlayerLeft`.
        if msg == head or msg.startswith(head + " "):
            m = _NAME_AND_ID.match(msg[len(head) + 1:])
            return cls(m["id"], m["name"]) if m else Unparsed(verb, msg)
    if msg == _JOINED:
        return JoinedRoom()
    if msg == _LEFT_ROOM:
        return LeftRoom()
    m = _JOINING.match(msg)
    if m:
        return JoiningWorld(m["world"], m["instance"])
    m = _ENTERING.match(msg)
    if m:
        return EnteringRoom(m["name"])
    m = _SERVICE.match(msg)
    if m:
        return ServiceAdvertised(m["name"], int(m["port"]))
    m = _SELF.match(msg)
    if m:
        who = _NAME_AND_ID.match(m["rest"])
        return SelfIdentity(who["id"], who["name"]) if who else None
    return None

# --- state ------------------------------------------------------------------


class Roster:
    """Instance state built from events. Not locked itself; `LogTailer` holds the lock."""

    def __init__(self) -> None:
        self.self_id: Optional[str] = None
        self.self_name: Optional[str] = None
        self.service_name: Optional[str] = None
        self.oscquery_port: Optional[int] = None
        self.world_id: Optional[str] = None
        self.instance: Optional[str] = None
        self.room_name: Optional[str] = None
        self.joined = False
        self.players: "OrderedDict[str, str]" = OrderedDict()

    def _start_room(self) -> None:
        if self.joined:
            self.world_id = self.instance = self.room_name = None
        self.players.clear()
        self.joined = False

    def apply(self, event: Optional[RosterEvent]) -> Optional[str]:
        """Apply one event; return what changed (`"self"`, `"service"`, `"room"`,
        `"joined"`, `"join"`, `"leave"`, `"left"`) or None for no change."""
        if isinstance(event, PlayerJoined):
            if event.id in self.players:
                return None
            self.players[event.id] = event.name
            return "join"
        if isinstance(event, PlayerLeft):
            if self.players.pop(event.id, None) is None:
                return None
            return "leave"
        if isinstance(event, EnteringRoom):
            self._start_room()
            self.room_name = event.name
            return "room"
        if isinstance(event, JoiningWorld):
            self._start_room()
            self.world_id, self.instance = event.world_id, event.instance
            return "room"
        if isinstance(event, JoinedRoom):
            if self.joined:
                return None
            self.joined = True
            return "joined"
        if isinstance(event, LeftRoom):
            if not self.joined and not self.players and self.world_id is None:
                return None
            # The whole room identity goes: the next start sets its own field first, and a
            # kept world id or name would ride into that half-built snapshot.
            self.players.clear()
            self.joined = False
            self.world_id = self.instance = self.room_name = None
            return "left"
        if isinstance(event, SelfIdentity):
            if (self.self_id, self.self_name) == (event.id, event.name):
                return None
            self.self_id, self.self_name = event.id, event.name
            return "self"
        if isinstance(event, ServiceAdvertised):
            if (self.service_name, self.oscquery_port) == (event.service_name, event.port):
                return None
            self.service_name, self.oscquery_port = event.service_name, event.port
            return "service"
        return None

    def snapshot(self) -> dict:
        """JSON-ready: `{"self", "world", "joined", "players"}`; absent parts are None."""
        has_world = any(v is not None for v in (self.world_id, self.instance, self.room_name))
        return {
            "self": {"id": self.self_id, "name": self.self_name} if self.self_id else None,
            "world": ({"id": self.world_id, "instance": self.instance, "name": self.room_name}
                      if has_world else None),
            "joined": self.joined,
            "players": [{"id": pid, "name": name} for pid, name in self.players.items()],
        }

# --- file selection ---------------------------------------------------------


class Selection(NamedTuple):
    """`rule` is `"service"`, `"newest"`, `"newest (service not found)"`, or `"none"`."""
    path: Optional[Path]
    rule: str


def _advertised_service(path: Path) -> Optional[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= SERVICE_SCAN_LINES:
                    break
                event = parse_line(line)
                if isinstance(event, ServiceAdvertised):
                    return event.service_name
    except OSError:
        pass
    return None


def select_log_file(log_dir: Path, service_name: Optional[str]) -> Selection:
    """The log whose OSCQuery advertisement names `service_name`, else the newest by mtime."""
    try:
        files = [p for p in Path(log_dir).glob(LOG_GLOB) if p.is_file()]
    except OSError:
        files = []
    if not files:
        return Selection(None, "none")
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if service_name is None:
        return Selection(files[0], "newest")
    for p in files:
        if _advertised_service(p) == service_name:
            return Selection(p, "service")
    return Selection(files[0], "newest (service not found)")

# --- tailer -----------------------------------------------------------------


class LogTailer:
    """Replay a client log into a `Roster`, then follow its tail on a daemon thread.

    `on_change(roster, change)` runs on the tailer thread with `lock` held, so the
    roster it is handed is consistent for the length of the call — and a slow
    callback delays the next poll. It is suppressed during a replay, which ends
    in one `"snapshot"` call. Other threads read through `snapshot()`.
    """

    def __init__(self, on_change: Callable[[Roster, str], None], log_dir: Optional[Path] = None,
                 service_name: Optional[str] = None, poll_secs: float = 0.25,
                 logger: Optional[logging.Logger] = None, retry_secs: float = RETRY_SECS) -> None:
        self.on_change = on_change
        self.log_dir = Path(log_dir) if log_dir is not None else DEFAULT_LOG_DIR
        self.poll_secs = poll_secs
        self.retry_secs = retry_secs
        self.log = logger or logging.getLogger(__name__)
        self.lock = threading.RLock()
        # The retarget request is its own short lock, never `lock`: `retarget` is called from
        # zeroconf's dispatch thread, and `lock` is held for the length of a replay.
        self._flag_lock = threading.Lock()
        self.roster = Roster()
        self.path: Optional[Path] = None
        self.rule: Optional[str] = None
        self._service_name = service_name
        self._retarget_pending = True
        self._offset = 0
        self._partial = b""
        self._idle_logged = False
        self._last_attempt = float("-inf")
        self._unparsed_logged: set = set()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def select_file(self, service_name: Optional[str]) -> Selection:
        return select_log_file(self.log_dir, service_name)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="vrbridge-roster", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def retarget(self, service_name: Optional[str]) -> None:
        """Re-select the file for `service_name` and replay it, on the tailer thread."""
        with self._flag_lock:
            self._service_name = service_name
            self._retarget_pending = True
        self._wake.set()

    def snapshot(self) -> dict:
        with self.lock:
            return self.roster.snapshot()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with self._flag_lock:
                    due = self._retarget_pending or (
                        self.path is None
                        and time.monotonic() - self._last_attempt >= self.retry_secs)
                if due:
                    self._select_and_replay()
                elif self.path is not None:
                    self._poll()
            except Exception:  # a log we cannot read must not kill the thread
                self.log.exception("roster: tailing %s failed", self.path)
            self._wake.wait(self.poll_secs)
            self._wake.clear()

    def _select_and_replay(self) -> None:
        with self._flag_lock:
            self._retarget_pending = False
            self._last_attempt = time.monotonic()
            service = self._service_name
        sel = self.select_file(service)
        with self.lock:
            if sel.path is None:
                if not self._idle_logged:
                    self.log.info("roster: no %s in %s; idle, retrying every %.0f s",
                                  LOG_GLOB, self.log_dir, self.retry_secs)
                    self._idle_logged = True
                self.path = self.rule = None
                return
            self._idle_logged = False
            self.path, self.rule = sel.path, sel.rule
            self.log.info("roster: following %s (by %s%s)", sel.path.name, sel.rule,
                          f", service {service}" if service else "")
            self.roster = Roster()
            self._offset = 0
            self._partial = b""
            self._read_new(notify=False)
            self.on_change(self.roster, "snapshot")

    def _poll(self) -> None:
        with self.lock:
            try:
                size = self.path.stat().st_size
            except OSError:
                return
            if size < self._offset:  # truncated or rewritten in place: replay it
                with self._flag_lock:
                    self._retarget_pending = True
                return
            if size > self._offset:
                self._read_new(notify=True)

    def _read_new(self, notify: bool) -> None:
        with open(self.path, "rb") as fh:
            fh.seek(self._offset)
            data = fh.read()
        self._offset += len(data)
        # Decoding whole lines only also keeps a multi-byte character split across reads intact.
        *lines, self._partial = (self._partial + data).split(b"\n")
        for raw in lines:
            event = parse_line(raw.decode("utf-8", errors="replace"))
            if isinstance(event, Unparsed):
                if event.verb not in self._unparsed_logged:
                    self._unparsed_logged.add(event.verb)
                    self.log.warning("roster: unrecognised %s line, roster may be incomplete: %r",
                                     event.verb, event.line)
                continue
            change = self.roster.apply(event)
            if notify and change is not None:
                self.on_change(self.roster, change)
