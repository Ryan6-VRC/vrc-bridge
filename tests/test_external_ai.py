"""The external AI socket, end to end over loopback: a real TCP client against the mapping, the
fake as the VRChat client.

Intent before test, per `docs/design.md`. The intents the suite holds:

* **The stream is the change-filtered one, per subscription.** A doubled delivery is one event, a
  change is another, and an address outside the subscription never arrives.
* **Types ride the JSON type, and writes are avatar parameters and the worn avatar only.**
  Anything else is an `error` echoing the request's `id`, and nothing is sent.
* **Initial state is read by name**, off the worker thread, answering `found` per address.
* **The last will fires in order, once,** on any close and on the bridge stopping.
* **A malformed request never closes the connection, and a slow client loses its oldest
  events, not its newest,** and is told how many.
* **The roster and avatar changes reach only connections that asked for them.**

Timing: thread-per-datagram dispatch means two datagrams sent back to back can arrive in either
order, so emits whose order matters are spaced by `STEP`. A `ping` answered with `pong` proves
every earlier request on that connection was handled, since one thread reads them in order.
"""
import json
import socket
import time

import pytest
from zeroconf import ServiceInfo

from vrbridge.controller_manager import ControllerEvent
from vrbridge.engine import ControllerEventType as CET
from vrbridge.engine import VRBridge
from vrbridge.mappings.external_ai import ExternalAIMapping
from vrbridge.osc_manager import REFIRE_FOLD_WINDOW_SECS
from vrbridge.settings import ExternalAISettings, Settings, set_settings

from .fake_vrchat import FakeVRChat

P = "/avatar/parameters/"
STEP = 0.05
ALICE = "usr_00000000-0000-4000-8000-000000000001"
BOB = "usr_00000000-0000-4000-8000-000000000002"
WORLD = "wrld_00000000-0000-4000-8000-0000000000aa"
SERVICE = "VRChat-Client-ABC123"


def log_line(msg):
    return f"2026.09.30 12:34:56 Debug      -  {msg}\n"


class Client:
    """A loopback NDJSON client with its own line buffer, so a read timeout loses nothing."""

    def __init__(self, port, rcvbuf=None):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if rcvbuf:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        self.sock.connect(("127.0.0.1", port))
        self._buf = b""
        self._n = 0

    def send(self, obj):
        self.raw(json.dumps(obj))

    def raw(self, line):
        self.sock.sendall((line + "\n").encode("utf-8"))

    def recv(self, timeout=2.0):
        """The next event, or None on timeout or EOF."""
        deadline = time.monotonic() + timeout
        while b"\n" not in self._buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                return None
            if not chunk:
                return None
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line)

    def sync(self, timeout=2.0):
        """Ping and return every event before the pong."""
        self._n += 1
        tag = f"sync-{self._n}"
        self.send({"op": "ping", "id": tag})
        seen = []
        while True:
            ev = self.recv(timeout)
            assert ev is not None, f"no pong for {tag}; saw {seen}"
            if ev["ev"] == "pong" and ev.get("id") == tag:
                return seen
            seen.append(ev)

    def until(self, pred, timeout=2.0):
        """Events up to and including the first that matches."""
        seen = []
        deadline = time.monotonic() + timeout
        while True:
            ev = self.recv(max(0.0, deadline - time.monotonic()))
            assert ev is not None, f"timed out; saw {seen}"
            seen.append(ev)
            if pred(ev):
                return seen

    def eof(self, timeout=2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.sock.settimeout(max(0.01, deadline - time.monotonic()))
            try:
                if self.sock.recv(65536) == b"":
                    return True
            except socket.timeout:
                return False
            except OSError:
                return True
        return False

    def close(self):
        self.sock.close()


class Rig:
    """A bridge on the fake, pinned by default or discovered through `_consider_service`."""

    def __init__(self, vrc, *, log_dir="", discovered=False):
        self.vrc = vrc
        if discovered:
            self.bridge = VRBridge(enable_steamvr=False, advertise=False, discover=False)
        else:
            self.bridge = VRBridge(enable_steamvr=False, advertise=False, discover=False,
                                   target=(vrc.host, vrc.osc_port))
        self.bridge.osc.start()
        vrc.out_port = self.bridge.osc.osc_port
        self.m = ExternalAIMapping(self.bridge, tuning=ExternalAISettings(
            enabled=True, bind="127.0.0.1", port=0, log_dir=str(log_dir)))
        self.m.register()
        self.m.activate()
        self._stopped = False
        self.clients = []

    def discover(self):
        name = f"{SERVICE}._oscjson._tcp.local."
        info = ServiceInfo("_oscjson._tcp.local.", name, addresses=[bytes([127, 0, 0, 1])],
                           port=self.vrc.http_port, properties={}, server="h.local.")
        self.bridge.osc._consider_service(name, info)

    def client(self, **kw):
        c = Client(self.m.port, **kw)
        welcome = c.recv()
        assert welcome is not None and welcome["ev"] == "welcome"
        self.clients.append(c)
        return c, welcome

    def stop_bridge(self):
        self._stopped = True
        self.bridge.stop()

    def close(self):
        for c in self.clients:
            c.close()
        self.m.close()
        if not self._stopped:
            self.bridge.osc.stop()


@pytest.fixture
def vrc():
    with FakeVRChat() as v:
        yield v


@pytest.fixture
def rig(vrc, tmp_path):
    made = []

    def make(**kw):
        kw.setdefault("log_dir", tmp_path)   # never the machine's real client log
        r = Rig(vrc, **kw)
        made.append(r)
        return r
    yield make
    for r in made:
        r.close()


def errors(events):
    return [e for e in events if e["ev"] == "error"]


# --------------------------------------------------------------------------

def test_welcome_comes_first_with_the_target(rig, vrc):
    """Intended: a client learns before anything else whom the bridge sends to, and whether that
    target was named, so `seq` starts at 1 on the welcome."""
    r = rig()
    _, w = r.client()
    assert w["seq"] == 1 and w["version"] == 1
    assert w["target"] == {"host": vrc.host, "port": vrc.osc_port}
    assert w["pinned"] is True
    assert isinstance(w["t"], float)


def test_the_param_stream_is_change_filtered_and_per_subscription(rig, vrc):
    """Intended: a doubled delivery is one event and a change another; nothing outside the
    subscription arrives; `name` is the bare parameter name; `seq` rises by one."""
    r = rig()
    c, _ = r.client()
    c.send({"op": "subscribe", "params": ["Foo/*"]})
    c.sync()
    vrc.copies = 2
    vrc.emit(P + "Foo/Bar", 1)
    vrc.emit(P + "Other", 5)
    time.sleep(STEP)
    vrc.emit(P + "Foo/Bar", 1)
    time.sleep(STEP)
    vrc.emit(P + "Foo/Bar", 2)
    time.sleep(STEP)
    seen = c.sync()
    params = [e for e in seen if e["ev"] == "param"]
    assert [(e["address"], e["name"], e["value"]) for e in params] == [
        (P + "Foo/Bar", "Foo/Bar", 1), (P + "Foo/Bar", "Foo/Bar", 2)]
    seqs = [e["seq"] for e in seen]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))


def test_set_carries_the_json_type_and_refuses_everything_else(rig, vrc):
    """Intended: true/false, an integer and a fractional number land as bool, int and float;
    a string value, a wildcard, `/input/` and any other address family are refused with the
    request's id, and none of them is sent."""
    r = rig()
    c, _ = r.client()
    c.send({"op": "set", "address": "B", "value": True})
    c.send({"op": "set", "address": P + "I", "value": 3})
    c.send({"op": "set", "address": "F", "value": 0.5})
    assert vrc.wait_for_count(3)
    got = {a: v for a, v in vrc.messages}
    assert got[P + "B"] is True
    assert type(got[P + "I"]) is int and got[P + "I"] == 3
    assert type(got[P + "F"]) is float and got[P + "F"] == 0.5

    bad = [("s", {"address": "S", "value": "text"}), ("w", {"address": "Foo/*", "value": 1}),
           ("j", {"address": "/input/Jump", "value": 1}),
           ("t", {"address": "/tracking/x", "value": 1.0})]
    for rid, body in bad:
        c.send({"op": "set", "id": rid, **body})
    errs = errors(c.sync())
    assert [(e["op"], e["id"]) for e in errs] == [("set", rid) for rid, _ in bad]
    time.sleep(STEP)
    assert len(vrc.messages) == 3


def test_change_sends_an_avatar_id_and_refuses_anything_else(rig, vrc):
    r = rig()
    c, _ = r.client()
    avatar = "avtr_aaaaaaaa-0000-0000-0000-000000000001"
    c.send({"op": "change", "avatar": avatar})
    c.send({"op": "change", "id": 7, "avatar": "usr_nope"})
    errs = errors(c.sync())
    assert vrc.wait_for_count(1)
    assert vrc.values_for("/avatar/change") == [avatar]
    assert [(e["op"], e["id"]) for e in errs] == [("change", 7)]


def test_get_reads_each_node_by_name(rig, vrc):
    """Intended: a served node answers found with its value, an undeclared one found false
    with no error, since a 404 is an answer about the avatar. A discovered target, because a
    pinned one serves no tree; its selection is also announced as a `target` event."""
    r = rig(discovered=True)
    c, w = r.client()
    assert w["target"] is None and w["pinned"] is False
    r.discover()
    target = c.until(lambda e: e["ev"] == "target")[-1]
    assert (target["host"], target["port"]) == ("127.0.0.1", vrc.osc_port)

    vrc.set_node(P + "Known", 7)
    c.send({"op": "get", "id": "g", "addresses": ["Known", "Unknown", "/input/Jump"]})
    seen = []
    while sum(e["ev"] == "value" for e in seen) < 2:
        seen.append(c.until(lambda e: True)[-1])
    values = {e["address"]: e for e in seen if e["ev"] == "value"}
    assert values[P + "Known"]["found"] is True and values[P + "Known"]["value"] == 7
    assert values[P + "Unknown"]["found"] is False and "error" not in values[P + "Unknown"]
    assert all(v["id"] == "g" for v in values.values())
    assert [e["op"] for e in errors(seen)] == ["get"]


def test_a_get_against_a_pinned_target_names_why(rig):
    r = rig()
    c, _ = r.client()
    c.send({"op": "get", "addresses": ["X"]})
    v = c.until(lambda e: e["ev"] == "value")[-1]
    assert v["found"] is False and v["error"] == "no-peer"


def test_the_will_fires_in_order_once_on_close_and_on_bridge_stop(rig, vrc):
    """Intended: a closed connection's will lands in order; a live one's lands when the bridge
    stops, while OSC can still send; neither fires twice."""
    r = rig()
    c1, _ = r.client()
    c2, _ = r.client()
    c1.send({"op": "will", "set": [{"address": "A", "value": 1}, {"address": "B", "value": 2}]})
    c2.send({"op": "will", "set": [{"address": "C", "value": True}]})
    c1.sync()
    c2.sync()
    c1.close()
    assert vrc.wait_for_count(2)
    assert vrc.addresses() == [P + "A", P + "B"]

    r.stop_bridge()
    assert vrc.wait_for_count(3)
    assert c2.eof()
    time.sleep(STEP * 2)
    assert vrc.addresses() == [P + "A", P + "B", P + "C"]


def test_a_bad_will_entry_leaves_the_old_will_standing(rig, vrc):
    r = rig()
    c, _ = r.client()
    c.send({"op": "will", "set": [{"address": "A", "value": 1}]})
    c.send({"op": "will", "id": 1, "set": [{"address": "B", "value": 1},
                                           {"address": "/input/Jump", "value": 1}]})
    assert [e["id"] for e in errors(c.sync())] == [1]
    c.close()
    assert vrc.wait_for_count(1)
    time.sleep(STEP)
    assert vrc.addresses() == [P + "A"]


def test_controller_events_reach_only_the_types_named(rig):
    """Intended: a named type arrives with its fields; an unnamed one, `scroll_raw` included,
    does not; an unknown name is an error naming it while the known ones still subscribe."""
    r = rig()
    c, _ = r.client()
    c.send({"op": "subscribe", "id": "s", "controller": [CET.TOUCHPAD_SHORT_PRESS, "nope.thing"]})
    errs = errors(c.sync())
    assert len(errs) == 1 and "nope.thing" in errs[0]["message"] and errs[0]["id"] == "s"

    fire = r.bridge._on_controller_event
    fire(ControllerEvent(CET.TOUCHPAD_SHORT_PRESS, "left", when=1.5))
    fire(ControllerEvent(CET.TOUCHPAD_LONG_PRESS, "left"))
    fire(ControllerEvent(CET.TOUCHPAD_SCROLL_RAW, "right", dx=0.1, dy=-0.2))
    got = [e for e in c.sync() if e["ev"] == "controller"]
    assert got == [{**got[0], "type": CET.TOUCHPAD_SHORT_PRESS, "hand": "left", "steps": None,
                    "dx": None, "dy": None, "ax": None, "ay": None, "when": 1.5}]
    assert len(got) == 1

    c.send({"op": "subscribe", "controller": [CET.TOUCHPAD_SCROLL_RAW]})
    c.sync()
    fire(ControllerEvent(CET.TOUCHPAD_SCROLL_RAW, "right", dx=0.1, dy=-0.2))
    fire(ControllerEvent(CET.TOUCHPAD_SHORT_PRESS, "left"))
    got = [e for e in c.sync() if e["ev"] == "controller"]
    assert [(e["type"], e["dx"], e["dy"]) for e in got] == [(CET.TOUCHPAD_SCROLL_RAW, 0.1, -0.2)]


def test_the_roster_is_read_from_the_log_and_sent_as_snapshot_and_deltas(rig, tmp_path):
    """Intended: subscribing answers with the roster as the log has it; a join is a `join`
    naming the player; leaving the room is a fresh snapshot with nobody in it."""
    log = tmp_path / "output_log_2026-09-30_12-00-00.txt"
    log.write_text("".join(log_line(m) for m in (
        f"Advertising Service {SERVICE} of type OSCQuery on 54321",
        "[Behaviour] Entering Room: Example World",
        f"[Behaviour] Joining {WORLD}:12345",
        "[Behaviour] Successfully joined room",
        f"[Behaviour] OnPlayerJoined Alice Example ({ALICE})")), encoding="utf-8")
    r = rig(log_dir=tmp_path)
    deadline = time.monotonic() + 2
    while r.m._tailer.snapshot()["players"] == [] and time.monotonic() < deadline:
        time.sleep(0.02)
    c, _ = r.client()
    quiet, _ = r.client()
    c.send({"op": "subscribe", "roster": True, "id": "sub-1"})
    snap = c.until(lambda e: e["ev"] == "roster")[-1]
    assert snap["players"] == [{"id": ALICE, "name": "Alice Example"}]
    assert snap["world"]["id"] == WORLD and snap["joined"] is True
    assert snap["id"] == "sub-1", "the immediate roster answer is a reply, so it echoes the id"
    # A string where a bool belongs is refused, not read as truthy.
    c.send({"op": "subscribe", "roster": "false", "id": "sub-2"})
    err = c.until(lambda e: e["ev"] == "error")[-1]
    assert err["id"] == "sub-2" and "roster" in err["message"]

    with log.open("a", encoding="utf-8") as fh:
        fh.write(log_line(f"[Behaviour] OnPlayerJoined Bob ({BOB})"))
    join = c.until(lambda e: e["ev"] == "join")[-1]
    assert join["player"] == {"id": BOB, "name": "Bob"}

    with log.open("a", encoding="utf-8") as fh:
        fh.write(log_line("[Behaviour] OnLeftRoom"))
    left = c.until(lambda e: e["ev"] == "roster")[-1]
    assert left["players"] == [] and left["joined"] is False and left["world"] is None
    assert [e for e in quiet.sync() if e["ev"] in ("roster", "join", "leave")] == []


def test_avatar_changes_reach_only_subscribed_connections(rig, vrc):
    """Intended: every delivered `/avatar/change` is an event, to those who asked."""
    r = rig()
    c, _ = r.client()
    other, _ = r.client()
    c.send({"op": "subscribe", "avatar": True})
    c.sync()
    a, b = "avtr_aaaaaaaa-0000-0000-0000-000000000001", "avtr_bbbbbbbb-0000-0000-0000-000000000001"
    vrc.emit("/avatar/change", a)
    time.sleep(REFIRE_FOLD_WINDOW_SECS + 0.1)
    vrc.emit("/avatar/change", b)
    time.sleep(STEP)
    got = [e["id"] for e in c.sync() if e["ev"] == "avatar"]
    assert got == [a, b]
    assert [e for e in other.sync() if e["ev"] == "avatar"] == []


def test_a_malformed_request_is_an_error_and_the_connection_survives(rig):
    r = rig()
    c, _ = r.client()
    c.raw("this is not json")
    c.raw("[1, 2]")
    c.raw("\xff\xfe garbage")
    c.send({"op": "dance", "id": 5})
    errs = errors(c.sync())
    assert len(errs) == 4
    assert errs[-1]["op"] == "dance" and errs[-1]["id"] == 5
    assert "id" not in errs[0]


def test_a_slow_client_loses_the_oldest_events_and_is_told(rig):
    """Intended: a client that stops reading never blocks the bridge; its queue drops the
    oldest, and once it reads again an `error` carrying `dropped` precedes the newer events
    and the newest value is there. Values are large so the socket buffers fill quickly."""
    r = rig()
    r.m.QUEUE_DEPTH = 8
    c, _ = r.client(rcvbuf=4096)
    c.send({"op": "subscribe", "params": ["Big"]})
    c.sync()
    pad = "x" * 8000
    started = time.monotonic()
    for i in range(1000):
        r.bridge._on_osc_event(P + "Big", f"{i}{pad}")
    r.bridge._on_osc_event(P + "Big", "last")
    assert time.monotonic() - started < 5, "producing blocked on a stalled client"
    seen = c.until(lambda e: e["ev"] == "param" and e["value"] == "last", timeout=5)
    drops = [i for i, e in enumerate(seen) if e["ev"] == "error" and e.get("dropped")]
    assert drops, "no dropped report"
    assert sum(seen[i]["dropped"] for i in drops) + sum(e["ev"] == "param" for e in seen) == 1001
    assert seen[drops[-1] + 1]["ev"] == "param"
    seqs = [e["seq"] for e in seen]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))


def test_deactivate_closes_the_listener_and_every_connection(rig, vrc):
    r = rig()
    c1, _ = r.client()
    c2, _ = r.client()
    c1.send({"op": "will", "set": [{"address": "Rest", "value": 0}]})
    c1.sync()
    port = r.m.port
    r.m.deactivate()
    assert c1.eof() and c2.eof()
    assert vrc.wait_for_count(1) and vrc.addresses() == [P + "Rest"]
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", port))   # binds only once the listener is really gone
    finally:
        probe.close()


def test_a_port_in_use_is_logged_naming_it_not_raised(vrc, caplog):
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen()
    port = holder.getsockname()[1]
    bridge = VRBridge(enable_steamvr=False, advertise=False, discover=False,
                      target=(vrc.host, vrc.osc_port))
    try:
        m = ExternalAIMapping(bridge, tuning=ExternalAISettings(enabled=True, port=port))
        m.register()
        m.activate()
        assert m.port is None
        assert any(r.levelname == "ERROR" and f"127.0.0.1:{port}" in r.getMessage()
                   for r in caplog.records)
        m.close()
    finally:
        holder.close()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("router_name", ["default", "camera"])
def test_shipped_routers_register_it_only_when_enabled(router_name, enabled, tmp_path):
    """Intended: off by default, because it opens a socket; `[external_ai] enabled` registers it
    active in every shipped router, outside mode switching."""
    from vrbridge.cli import ROUTERS
    set_settings(Settings(external_ai=ExternalAISettings(enabled=enabled, port=0,
                                                         log_dir=str(tmp_path))))
    ext = None
    try:
        bridge = VRBridge(enable_steamvr=False, advertise=False, discover=False)
        router = ROUTERS[router_name](bridge)
        ext = router._mappings.get("external_ai")
        if not enabled:
            assert ext is None
            return
        assert isinstance(ext, ExternalAIMapping) and ext.enabled and ext.port
        router.evaluate()
        assert ext.enabled
    finally:
        if ext is not None:
            ext.close()
        set_settings(None)
