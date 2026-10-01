"""The external AI socket: one TCP listener an external program connects to -- an AI, a tool on
another PC -- to watch the avatar and drive it.

A client subscribes to avatar parameter patterns, controller event types, avatar changes and the
instance roster, receives each as a change event, and writes avatar parameters and the worn
avatar. It is the product's worked integration example: everything it does goes through the same
`VRBridge` registrations and `OSCManager` calls a mapping author has.

**The wire.** UTF-8, newline-delimited JSON, one object per line each way. README.md §External AI
socket is the client's specification; this is the implementer's, and the two must agree.

Every bridge->client object carries `ev`, `seq` (per connection, 1 on the `welcome`, rising by
one per object written, no gaps) and `t` (`time.time()` when the event was queued). Every
client->bridge object carries `op` and may carry `id`, which every reply and error to it echoes.

Client -> bridge:

* `subscribe` -- `params` (list of specs: a bare name is an avatar parameter, a leading `/` a full
  address, fnmatch wildcards allowed, as `osc_paramlog.to_address`), `controller` (list of
  `ControllerEventType` values), `avatar` (bool), `roster` (bool). Replaces the connection's
  subscription; a missing key is empty or false. `roster` true answers at once with a `roster`
  snapshot. Unknown controller names are an `error` naming them; the rest still applies.
  `touchpad.scroll_raw` streams at the controller poll rate and, like every type, arrives only
  when named.
* `set` -- `address` (bare name or `/avatar/parameters/...`), `value`. Answered only on failure.
* `get` -- `addresses` (list, the `set` address rule). One `value` event per address.
* `change` -- `avatar` (an `avtr_` id), sent to `/avatar/change`. Answered only on failure.
* `will` -- `set` (list of `{address, value}`, each validated as `set`; one bad entry refuses the
  whole list and leaves the old will standing). Replaces the connection's last will.
* `ping` -> `pong`.

Bridge -> client: `welcome` (`version`, `target: {host, port} | null`, `pinned`), `param`
(`address`, `name` -- the part after `/avatar/parameters/`, else null -- and `value`), `value`
(`address`, `found`, and `value` when found or `error` and `detail` when the read failed other
than by a 404), `controller` (`type`, `hand`, `steps`, `dx`, `dy`, `ax`, `ay`, `when`, null where
the event carries none), `avatar` (`id`), `target` (`host`, `port`, to every connection), `roster`
(the `roster.Roster.snapshot()` fields: `self`, `world`, `joined`, `players`), `join` / `leave`
(`player: {id, name}`), `error` (`op`, `message`, `id` when the request carried one, `dropped`
for a queue overflow), `pong`.

**Rulings** (docs/design.md §The external AI socket holds the why):

* **Plain TCP NDJSON, no dependency.** Every language reads lines of JSON from a socket; a
  WebSocket or RPC framework would add a dependency for no capability the client lacks.
* **Loopback by default, LAN by config, no authentication.** `[external_ai] bind` widens it; the
  operator who does so owns the exposure.
* **Config-enabled, always on in every router, outside mode switching**, like the leash. Inert
  until a client connects.
* **Types ride the JSON type.** `true`/`false` is an OSC bool, an integer an int, a number with a
  fraction or exponent a float; anything else is refused. A bool is tested before an int,
  because Python's `bool` is an `int`.
* **Writes are two things: avatar parameters and the worn avatar.** `/input/*`, `/chatbox/*`,
  `/tracking/*` and every other family are refused, as is a wildcard in a write address.
* **Reset after an avatar change is the client's job.** The bridge cannot know what a client's
  writes meant; the last will is the one generic hook, fired in order when the connection closes
  for any reason and from `on_stop` while OSC can still send, at most once.
* **Initial state is read by name** (`get`, one OSCQuery node per address), never a subtree.
* **One wildcard watch, filtered per connection.** `VRBridge` callbacks cannot be removed, so a
  registration per subscription would accumulate across reconnects. A named shape of traffic is
  not enumeration, so the parameter-discovery descope holds.
* **The roster comes from the client's own log** (`roster.LogTailer`), bound to the discovered
  client by its `Advertising Service ... OSCQuery` line, the newest file as the fallback.

**Idempotence and order.** The `param` stream is `on_osc_pattern`'s change-filtered stream, which
already folds the doubled inbound delivery into one event per value; this mapping adds no dedupe
of its own. Order across addresses is not guaranteed -- two datagrams can reach their handlers in
reverse arrival order (docs/design.md §Inbound delivery semantics) -- and `seq` is the order the
bridge emitted, the key a client orders on. A client's own `set` comes back as an ordinary
`param` event, the client's echo of the write, once the avatar reports the value as changed.

**Threads.** A `ThreadingTCPServer` serves on its own thread. Per connection: the handler thread
reads lines and dispatches; a writer thread drains a bounded queue onto the socket; a single
worker thread runs `get`'s blocking fetches, so a read never runs on the handler, datagram or
controller thread. OSC, controller, target and roster callbacks only format and queue, under the
lock over the connection set. A full queue drops its OLDEST entry, so the newest state wins, and
the writer reports the run as one `error` carrying `dropped` ahead of the next event it writes.
The roster tailer's callback runs with the tailer's lock held, so nothing here calls back into
the tailer from it; `retarget` only flags, which is why the target callback may call it from
zeroconf's thread.
"""

from __future__ import annotations

import fnmatch
import json
import math
import queue
import socket
import socketserver
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Dict, Optional

from vrbridge import VRBridge
from vrbridge.engine import ControllerEventType
from vrbridge.mappings.mapping_base import Mapping
from vrbridge.mappings.osc_paramlog import PARAMS_PREFIX, to_address
from vrbridge.osc_manager import FETCH_NOT_FOUND, FETCH_OK
from vrbridge.roster import LogTailer, Roster
from vrbridge.settings import settings

# ------------------------------ Contract ----------------------------------

# Addresses and the wire's version are contracts, not settings (settings.py's header rule).
PARAMS_PATTERN = PARAMS_PREFIX + "*"
AVATAR_CHANGE_ADDR = "/avatar/change"
AVATAR_ID_PREFIX = "avtr_"
PROTOCOL_VERSION = 1

#: Every controller event type a client may name.
CONTROLLER_TYPES = tuple(v for k, v in vars(ControllerEventType).items() if k.isupper())

#: The zeroconf form of a service name carries the type; the client's log line does not.
OSCQUERY_SERVICE_SUFFIX = "._oscjson._tcp.local."

#: One OSCQuery node read for `get`. A loopback GET; the bound is for a peer that stops answering.
FETCH_TIMEOUT_SECS = 2.0

# Matches osc_manager's serve-poll interval, for the same reason: it is the server's teardown cost.
_SERVE_POLL_SECS = 0.05
_GLOB_CHARS = ("*", "?", "[")
#: The longest request line accepted. A peer that never sends a newline would otherwise grow
#: the bridge's memory without bound; a real request is a few hundred bytes.
MAX_LINE_BYTES = 1 << 20
_INT32_MIN, _INT32_MAX = -2 ** 31, 2 ** 31 - 1
_CLOSE = object()


class _Refused(Exception):
    """A request the bridge will not carry out; the message goes back to the client."""


def write_address(spec: Any) -> str:
    """A `set`/`get`/`will` address: a bare name or `/avatar/parameters/<name>`, no wildcard."""
    if not isinstance(spec, str) or not spec:
        raise _Refused(f"address must be a non-empty string, got {spec!r}")
    if any(ch in spec for ch in _GLOB_CHARS):
        raise _Refused(f"address {spec!r} contains a wildcard; name one parameter")
    address = to_address(spec)
    if not address.startswith(PARAMS_PREFIX) or address == PARAMS_PREFIX:
        raise _Refused(f"address {spec!r} is not an avatar parameter; only "
                       f"{PARAMS_PREFIX}<name> and the worn avatar can be written")
    return address


def osc_value(value: Any):
    """The OSC value a JSON value stands for: bool, int (32-bit) or float. Bool is tested first."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if not _INT32_MIN <= value <= _INT32_MAX:
            raise _Refused(f"value {value} is outside OSC's 32-bit int range")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _Refused(f"value {value} is not a finite number")
        return value
    raise _Refused(f"value {value!r} is not a bool, an integer or a number")


def _finite(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_finite(v) for v in value]
    return value


def _instance_name(service_name: Optional[str]) -> Optional[str]:
    """`VRChat-Client-XXXX._oscjson._tcp.local.` -> `VRChat-Client-XXXX`, as the log writes it."""
    if service_name and service_name.endswith(OSCQUERY_SERVICE_SUFFIX):
        return service_name[:-len(OSCQUERY_SERVICE_SUFFIX)]
    return service_name


def _string_list(msg: dict, key: str) -> list:
    v = msg.get(key)
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(s, str) for s in v):
        raise _Refused(f"{key} must be a list of strings")
    return v


@dataclass(frozen=True)
class _Subscription:
    params: tuple = ()
    controller: frozenset = frozenset()
    avatar: bool = False
    roster: bool = False

    def wants_param(self, address: str) -> bool:
        return any(address == p or fnmatch.fnmatchcase(address, p) for p in self.params)


# ----------------------------- Connection ----------------------------------

class _Connection:
    """One client: its subscription, its outbound queue and writer, its will and its worker."""

    def __init__(self, owner: "ExternalAIMapping", sock: socket.socket, peer):
        self.owner = owner
        self.sock = sock
        self.peer = peer
        self.sub = _Subscription()
        self._q: "queue.Queue" = queue.Queue(maxsize=owner.QUEUE_DEPTH)
        self._qlock = threading.Lock()
        self._dropped = 0
        self._seq = 0
        self._lock = threading.Lock()
        self._closed = False
        self._will: list = []
        self._will_fired = False
        self._worker: Optional[ThreadPoolExecutor] = None
        self._writer = threading.Thread(target=self._write_loop, daemon=True,
                                        name=f"vrbridge-external-ai-writer-{peer}")

    def start(self) -> None:
        self._writer.start()

    # ---- outbound --------------------------------------------------------

    def send(self, ev: str, fields: Optional[dict] = None) -> None:
        """Queue one event. Never blocks: a full queue loses its oldest entry instead."""
        self._put((ev, time.time(), fields or {}))

    def error(self, op, message: str, rid=None, has_id: bool = False) -> None:
        fields = {"op": op, "message": message}
        if has_id:
            fields["id"] = rid
        self.send("error", fields)

    def _put(self, item) -> None:
        with self._qlock:
            while True:
                try:
                    self._q.put_nowait(item)
                    return
                except queue.Full:
                    try:
                        self._q.get_nowait()
                        if item is not _CLOSE:
                            self._dropped += 1
                    except queue.Empty:
                        pass

    def _frame(self, ev: str, t: float, fields: dict) -> str:
        self._seq += 1
        obj = {"ev": ev, "seq": self._seq, "t": t}
        obj.update(fields)
        try:
            return json.dumps(obj, separators=(",", ":"), default=str, allow_nan=False) + "\n"
        except ValueError:
            # A NaN or infinite float off the wire: JSON has no spelling for it that a strict
            # parser accepts, so it travels as null.
            return json.dumps(_finite(obj), separators=(",", ":"), default=str) + "\n"

    def _write_loop(self) -> None:
        while True:
            item = self._q.get()
            if item is _CLOSE:
                return
            with self._qlock:
                dropped, self._dropped = self._dropped, 0
            out = ""
            if dropped:
                out += self._frame("error", time.time(), {
                    "op": None, "dropped": dropped,
                    "message": f"{dropped} event(s) dropped: the client read too slowly "
                               "and the oldest were discarded"})
            out += self._frame(*item)
            try:
                self.sock.sendall(out.encode("utf-8"))
            except OSError:
                return

    # ---- the get worker ----------------------------------------------------

    def has_worker(self) -> bool:
        with self._lock:
            return self._worker is not None

    def submit(self, fn, *args) -> None:
        with self._lock:
            if self._closed:
                return
            if self._worker is None:
                self._worker = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="vrbridge-external-ai-get")
            self._worker.submit(fn, *args)

    # ---- the will and closing --------------------------------------------

    def set_will(self, writes: list) -> None:
        with self._lock:
            self._will = writes

    def fire_will(self) -> None:
        """Send the will in order, once; later calls do nothing."""
        with self._lock:
            if self._will_fired:
                return
            self._will_fired = True
            writes = list(self._will)
        for address, value in writes:
            if not self.owner.bridge.osc.send(address, value):
                self.owner.bridge.log.warning("external_ai: %s's last will write %s=%r dropped",
                                              self.peer, address, value)

    def close(self) -> None:
        """Fire the will, stop the writer and the worker, and close the socket. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            worker = self._worker
        self.fire_will()
        self._put(_CLOSE)
        # Let the writer drain what was queued before the close -- the error that explains a
        # refused connection, for one -- bounded so a peer that stopped reading cannot hold
        # the close. The writer never calls close(), so this join is never a self-join.
        if self._writer.is_alive() and threading.current_thread() is not self._writer:
            self._writer.join(timeout=1.0)
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        if worker is not None:
            worker.shutdown(wait=False, cancel_futures=True)


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    mapping: "ExternalAIMapping"


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.server.mapping._serve(self.request, self.client_address)


# ------------------------------- Mapping -----------------------------------

class ExternalAIMapping(Mapping):
    """The external AI socket. The server and the roster tailer run between activate() and
    deactivate(); a bind failure is logged, not raised."""
    name = "external_ai"

    #: Events a connection may have queued before the oldest is dropped. Deep enough to ride out
    #: a client pausing for a second under a busy avatar's parameter stream (a few hundred
    #: changes a second); shallow enough that a stalled client costs a bounded amount of memory
    #: and catches up on recent state rather than replaying stale history.
    QUEUE_DEPTH = 2048

    def __init__(self, bridge: VRBridge, *, tuning=None):
        super().__init__(bridge)
        self._tune = tuning if tuning is not None else settings().external_ai
        self._lock = threading.Lock()
        self._conns: set = set()
        self._server: Optional[_Server] = None
        self._serve_thread: Optional[threading.Thread] = None
        self._tailer: Optional[LogTailer] = None
        # The roster's players as last reported, so a `join`/`leave` change can name its player.
        # Touched only on the tailer thread, inside its callback.
        self._players: Dict[str, str] = {}
        self.port: Optional[int] = None
        self._ops = {
            "subscribe": self._op_subscribe, "set": self._op_set, "get": self._op_get,
            "change": self._op_change, "will": self._op_will, "ping": self._op_ping,
        }

    # ---- lifecycle -------------------------------------------------------

    def _attach(self) -> None:
        self.bridge.on_osc_pattern(PARAMS_PATTERN, self._gate(self._on_param))
        self.bridge.on_osc(AVATAR_CHANGE_ADDR, self._gate(self._on_avatar))
        for event_type in CONTROLLER_TYPES:
            self.bridge.on_controller(event_type, "both", self._gate(self._on_controller))
        self.bridge.on_target_selected(self._gate(self._on_target))
        self.bridge.on_stop(self._on_stop)

    def activate(self) -> None:
        super().activate()
        self._start()

    def deactivate(self) -> None:
        super().deactivate()
        self._shutdown()

    def close(self) -> None:
        """Stop serving and close every connection, firing their wills. For tests and embedders."""
        self.deactivate()

    def _start(self) -> None:
        with self._lock:
            if self._server is not None:
                return
        bind, port = self._tune.bind, self._tune.port
        try:
            server = _Server((bind, port), _Handler)
        except OSError as e:
            self.bridge.log.error("external_ai: cannot listen on %s:%d (%s); the socket is off. "
                                  "Free the port or set [external_ai] port.", bind, port, e)
            return
        server.mapping = self
        self.port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, args=(_SERVE_POLL_SECS,),
                                  daemon=True, name="vrbridge-external-ai")
        with self._lock:
            self._server, self._serve_thread = server, thread
        thread.start()
        tailer = LogTailer(self._on_roster, log_dir=self._tune.resolved_log_dir(),
                           service_name=_instance_name(self.bridge.osc.current_service_name),
                           logger=self.bridge.log)
        self._tailer = tailer
        tailer.start()
        self.bridge.log.info("external_ai: listening on %s:%d", bind, self.port)

    def _shutdown(self) -> None:
        with self._lock:
            server, thread = self._server, self._serve_thread
            self._server = self._serve_thread = None
            conns = list(self._conns)
            self._conns.clear()
        if server is not None:
            server.shutdown()
            server.server_close()
            if thread is not None:
                thread.join(timeout=2.0)
        for conn in conns:
            conn.close()
        tailer, self._tailer = self._tailer, None
        if tailer is not None:
            tailer.stop()

    def _on_stop(self, ctx) -> None:
        # Wills first, while OSC can still send; then everything else goes down with the bridge.
        with self._lock:
            conns = list(self._conns)
        for conn in conns:
            conn.fire_will()
        self._shutdown()

    # ---- connections -----------------------------------------------------

    def _serve(self, sock: socket.socket, peer) -> None:
        """One connection, on its handler thread, until EOF, a read error or shutdown."""
        conn = _Connection(self, sock, peer)
        conn.start()
        with self._lock:
            # Target read, welcome queued and the set joined in one hold, so a `target` broadcast
            # cannot fall between the welcome's view and the connection's first event; `send`
            # never blocks, and the welcome is queued first so it is always seq 1.
            conn.send("welcome", {"version": PROTOCOL_VERSION, "target": self._target_dict(),
                                  "pinned": self.bridge.osc.target_is_pinned})
            live = self._server is not None
            if live:
                self._conns.add(conn)
        if not live:
            conn.close()
            return
        self.bridge.log.info("external_ai: client %s connected", peer)
        try:
            for line in self._lines(sock, conn):
                self._dispatch(conn, line)
        except (OSError, ValueError):
            pass  # a reset or a socket closed under us: the connection is over either way
        finally:
            with self._lock:
                self._conns.discard(conn)
            conn.close()
            self.bridge.log.info("external_ai: client %s disconnected", peer)

    @staticmethod
    def _lines(sock: socket.socket, conn: _Connection):
        """Request lines off the socket, each at most MAX_LINE_BYTES; a longer one ends the
        connection after one error naming the limit."""
        buf = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                return
            buf += chunk
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line = buf[:nl].decode("utf-8", errors="replace").strip()
                buf = buf[nl + 1:]
                if line:
                    yield line
            if len(buf) > MAX_LINE_BYTES:
                conn.error(None, f"request line longer than {MAX_LINE_BYTES} bytes; closing")
                return

    def _target_dict(self) -> Optional[dict]:
        target = self.bridge.osc.current_target
        return {"host": target[0], "port": target[1]} if target else None

    def _broadcast(self, ev: str, fields: dict, wants) -> None:
        with self._lock:
            for conn in self._conns:
                if wants(conn.sub):
                    conn.send(ev, fields)

    # ---- requests --------------------------------------------------------

    def _dispatch(self, conn: _Connection, line: str) -> None:
        try:
            msg = json.loads(line)
        except (ValueError, RecursionError) as e:
            conn.error(None, f"not JSON: {e}")
            return
        if not isinstance(msg, dict):
            conn.error(None, "a request must be a JSON object")
            return
        op, rid, has_id = msg.get("op"), msg.get("id"), "id" in msg
        handler = self._ops.get(op) if isinstance(op, str) else None
        if handler is None:
            conn.error(op, f"unknown op {op!r}; expected one of {', '.join(self._ops)}",
                       rid, has_id)
            return
        try:
            handler(conn, msg, rid, has_id)
        except _Refused as e:
            conn.error(op, str(e), rid, has_id)

    def _op_subscribe(self, conn, msg, rid, has_id) -> None:
        params = tuple(to_address(p) for p in _string_list(msg, "params"))
        named = _string_list(msg, "controller")
        for key in ("avatar", "roster"):
            if key in msg and not isinstance(msg[key], bool):
                raise _Refused(f"{key} must be true or false, got {msg[key]!r}")
        unknown = [c for c in named if c not in CONTROLLER_TYPES]
        sub = _Subscription(params=params,
                            controller=frozenset(c for c in named if c in CONTROLLER_TYPES),
                            avatar=msg.get("avatar", False),
                            roster=msg.get("roster", False))
        tailer = self._tailer
        reply = {"id": rid} if has_id else {}
        if tailer is None:
            conn.sub = sub
            if sub.roster:
                conn.send("roster", {**Roster().snapshot(), **reply})
        else:
            # Under the tailer's lock, so no roster change lands between the subscription taking
            # effect and the snapshot it is answered with.
            with tailer.lock:
                conn.sub = sub
                if sub.roster:
                    conn.send("roster", {**tailer.roster.snapshot(), **reply})
        if unknown:
            raise _Refused(f"unknown controller event type(s) {', '.join(unknown)}; the rest of "
                           f"the subscription applies. Known: {', '.join(CONTROLLER_TYPES)}")

    def _op_set(self, conn, msg, rid, has_id) -> None:
        address = write_address(msg.get("address"))
        value = osc_value(msg.get("value"))
        if not self.bridge.osc.send(address, value):
            raise _Refused(f"{address} not sent: no OSC target yet, or the send failed")

    def _op_get(self, conn, msg, rid, has_id) -> None:
        specs = msg.get("addresses")
        if not isinstance(specs, list):
            raise _Refused("addresses must be a list")
        for spec in specs:
            try:
                address = write_address(spec)
            except _Refused as e:
                conn.error("get", str(e), rid, has_id)
                continue
            conn.submit(self._fetch_one, conn, address, rid, has_id)

    def _fetch_one(self, conn, address: str, rid, has_id) -> None:
        """On the connection's worker thread: one node read, one `value` event."""
        result = self.bridge.osc.fetch(address, timeout=FETCH_TIMEOUT_SECS)
        fields: dict = {"address": address, "found": result.reason == FETCH_OK}
        if result.reason == FETCH_OK:
            fields["value"] = result.value
        elif result.reason != FETCH_NOT_FOUND:
            fields["error"] = result.reason
            fields["detail"] = result.detail
        if has_id:
            fields["id"] = rid
        conn.send("value", fields)

    def _op_change(self, conn, msg, rid, has_id) -> None:
        avatar = msg.get("avatar")
        if not isinstance(avatar, str) or not avatar.startswith(AVATAR_ID_PREFIX):
            raise _Refused(f"avatar must be an id starting {AVATAR_ID_PREFIX!r}, got {avatar!r}")
        if not self.bridge.osc.send(AVATAR_CHANGE_ADDR, avatar):
            raise _Refused(f"{AVATAR_CHANGE_ADDR} not sent: no OSC target yet, or the send failed")

    def _op_will(self, conn, msg, rid, has_id) -> None:
        entries = msg.get("set")
        if not isinstance(entries, list):
            raise _Refused("set must be a list of {address, value}")
        writes = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise _Refused(f"will entry {entry!r} is not an object; the will is unchanged")
            try:
                writes.append((write_address(entry.get("address")), osc_value(entry.get("value"))))
            except _Refused as e:
                raise _Refused(f"{e}; the will is unchanged") from None
        conn.set_will(writes)

    def _op_ping(self, conn, msg, rid, has_id) -> None:
        # Through the get worker when one exists, so the pong lands after every `value` the
        # connection's earlier gets owe: the README promises a pong is a barrier.
        fields = {"id": rid} if has_id else {}
        if conn.has_worker():
            conn.submit(conn.send, "pong", fields)
        else:
            conn.send("pong", fields)

    # ---- bridge events (format and queue only) ---------------------------

    def _on_param(self, ctx, address: str, value) -> None:
        name = address[len(PARAMS_PREFIX):] if address.startswith(PARAMS_PREFIX) else None
        self._broadcast("param", {"address": address, "name": name, "value": value},
                        lambda sub: sub.wants_param(address))

    def _on_avatar(self, ctx, address: str, value) -> None:
        self._broadcast("avatar", {"id": value}, lambda sub: sub.avatar)

    def _on_controller(self, ctx, evt) -> None:
        fields = {"type": evt.type, "hand": evt.hand, "steps": evt.steps, "dx": evt.dx,
                  "dy": evt.dy, "ax": evt.ax, "ay": evt.ay, "when": evt.when}
        self._broadcast("controller", fields, lambda sub: evt.type in sub.controller)

    def _on_target(self, ctx, target) -> None:
        # zeroconf's dispatch thread: queue and flag only. `retarget` only flags.
        self._broadcast("target", {"host": target[0], "port": target[1]}, lambda sub: True)
        tailer = self._tailer
        if tailer is not None:
            tailer.retarget(_instance_name(self.bridge.osc.current_service_name))

    def _on_roster(self, roster: Roster, change: str) -> None:
        # The tailer thread, with its lock held: read the roster handed in, never the tailer.
        if not self.enabled:
            return
        current = dict(roster.players)
        if change in ("join", "leave"):
            if change == "join":
                moved = [(pid, n) for pid, n in current.items() if pid not in self._players]
            else:
                moved = [(pid, n) for pid, n in self._players.items() if pid not in current]
            self._players = current
            for pid, name in moved:
                self._broadcast(change, {"player": {"id": pid, "name": name}},
                                lambda sub: sub.roster)
            return
        self._players = current
        self._broadcast("roster", roster.snapshot(), lambda sub: sub.roster)
