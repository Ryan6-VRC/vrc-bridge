"""A fake VRChat: an OSCQuery HTTP endpoint plus an OSC receiver, on loopback.

Enough of VRChat's OSC surface to prove our client half without a headset --
that HOST_INFO is parsed, that a selected target actually receives datagrams,
and that a mapping's sends arrive at the addresses it claims.

mDNS discovery is deliberately NOT modelled. Browsing a real network from a test
is flaky and proves nothing about our code that pointing the client at a known
port does not. Tests inject the target; discovery stays a live-run concern.

`hold_next_node_get` parks a node GET mid-flight, which is how the suite reaches an
interleaving at all: a mapping on the OSC datagram path blocks on exactly one thing,
this read, so holding it open stalls a dispatch thread where production stalls it.
docs/design.md holds why the rendezvous lives here rather than in the code under test.

The other direction is `emit`: the client's out-port stream, aimed at the bridge's bound port
through `out_port`, with `copies`, `echo_inbound` and `on_receive` reproducing the doubling, the
client's echo of every inbound write, and a scripted avatar answering a write.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pythonosc import dispatcher, osc_server

# Matches osc_manager._SERVE_POLL_SECS, and for the same measured reason: at the
# 0.5s default these two servers cost about 1.0s of every fixture teardown, on top
# of the manager's own. Six round-trip tests paid it, which was most of the suite.
_SERVE_POLL_SECS = 0.05


class _NodeGate:
    """One node GET, held mid-flight. `FakeVRChat.hold_next_node_get` hands one out.

    A context manager, because an assertion failing between the park and the release would
    otherwise leak a stuck handler thread for the whole park bound.
    """

    def __init__(self):
        self._parked = threading.Event()
        self._go = threading.Event()

    def wait_until_parked(self, timeout: float = 2.0) -> bool:
        """Block until the held GET has actually arrived, so a caller never races it.

        Returns False on timeout rather than raising: the caller asserts on it, which names
        the test that failed to reach the rendezvous instead of the fake's handler thread.
        """
        return self._parked.wait(timeout)

    def release(self) -> None:
        self._go.set()

    def __enter__(self) -> "_NodeGate":
        return self

    def __exit__(self, *exc) -> bool:
        self.release()
        return False

    def _park(self, timeout: float = 5.0) -> None:
        """Called on the fake's handler thread. Bounded, so a bug fails a test not the run.

        A caller's `fetch_timeout_secs` must exceed this bound, or the *fetch* unparks
        itself first, the rendezvous silently dissolves, and the abandoned handler writes to
        a closed socket -- which socketserver reports as a traceback against whichever test
        happens to run next.
        """
        self._parked.set()
        self._go.wait(timeout)


class FakeVRChat:
    """Context manager exposing .osc_port, .http_port and the received messages."""

    def __init__(self, host: str = "127.0.0.1", host_info: dict | None = None):
        self.host = host
        self._host_info_override = host_info
        self.messages: list[tuple[str, object]] = []
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        # Parameter nodes this fake serves, address -> VALUE. Anything absent 404s, which
        # is how the real client answers for a parameter the worn avatar does not declare
        # -- the state a wardrobe read has to tell apart from a failure.
        self.nodes: dict[str, object] = {}
        #: Set to make every node GET fail at the transport level instead of answering,
        #: so a caller's retry path is reachable without a real network fault.
        self.node_fault: bool = False
        #: Set to serve a body that is not a parameter node, for the malformed path.
        self.node_garbage: str | None = None
        #: Answer this many initial node GETs with 404 before serving normally. Reproduces
        #: the swap window -- the new avatar's node is not published the instant the change
        #: is announced -- deterministically, so a test need not race a sleep against the
        #: read schedule.
        self.node_404_first: int = 0
        self.node_gets: list[str] = []
        self._node_gate: _NodeGate | None = None
        #: The client's out-port stream: where `emit` sends, as VRChat sends to its configured
        #: out-port. None until a test points it at the bridge's bound OSC port.
        self.out_port: int | None = None
        #: Copies of every emitted message. 2 reproduces the client's doubled inbound delivery
        #: (docs/design.md §Inbound delivery semantics), which the emulator cannot.
        self.copies: int = 1
        #: Echo every received message back through `emit`, as the client echoes each inbound
        #: write on its out-port. Off by default so the existing tests see only what they send.
        self.echo_inbound: bool = False
        #: Called as `on_receive(address, value)` after the echo, on the fake's datagram thread:
        #: how a test scripts the worn avatar's reaction to a write.
        self.on_receive = None
        self._sender = None

    def hold_next_node_get(self) -> _NodeGate:
        """Park the next node GET mid-flight, so a caller's fetch() stalls there.

        Joins node_fault / node_garbage / node_404_first: a knob here makes a hard-to-reach
        path deterministic without a seam in the code under test. This one reaches the
        interleavings -- what a stalled read does to state a later caller established -- which
        no inline delivery can produce. docs/design.md holds the reasoning.
        """
        gate = _NodeGate()
        with self._lock:
            self._node_gate = gate
        return gate

    def set_node(self, address: str, value: object) -> None:
        """Serve `address` with this VALUE. VRChat wraps VALUE in an array; so do we."""
        with self._lock:
            self.nodes[address] = value

    def clear_node(self, address: str) -> None:
        """Stop serving `address`, so it 404s as an undeclared parameter does."""
        with self._lock:
            self.nodes.pop(address, None)

    # ---- lifecycle ----

    def __enter__(self) -> "FakeVRChat":
        disp = dispatcher.Dispatcher()
        disp.set_default_handler(self._record, needs_reply_address=False)
        self._osc = osc_server.ThreadingOSCUDPServer((self.host, 0), disp)
        self.osc_port = self._osc.server_address[1]
        self._osc_thread = threading.Thread(target=self._osc.serve_forever,
                                            args=(_SERVE_POLL_SECS,), daemon=True,
                                            name="FakeVRChatOSC")
        self._osc_thread.start()

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/?HOST_INFO":
                    body = outer._host_info_override
                    if body is None:
                        body = {"NAME": "FakeVRChat", "OSC_IP": outer.host,
                                "OSC_PORT": outer.osc_port, "OSC_TRANSPORT": "UDP"}
                    payload = json.dumps(body).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(payload)
                    return

                # A single-node GET, which is what OSCManager.fetch issues. The real
                # server answers one JSON node per parameter address.
                with outer._lock:
                    outer.node_gets.append(self.path)
                    known = dict(outer.nodes)
                    fault = outer.node_fault
                    garbage = outer.node_garbage
                    if outer.node_404_first > 0:
                        outer.node_404_first -= 1
                        known = {}
                    # Claimed here, in the same locked block that consumes node_404_first, so
                    # the parked request is provably the one that took the 404. Claimed the
                    # other way round, a *later* request takes it, a test aimed at one
                    # mechanism silently exercises another, and the fix under test stops
                    # curing it. One-shot: a second GET is served normally.
                    gate = outer._node_gate
                    outer._node_gate = None
                if gate is not None:
                    # Parked outside the lock: holding it would block every other GET,
                    # including the one a caller means to release this thread with. The answer
                    # served below was snapshotted above, before the park, so a stalled read
                    # describes the avatar it asked about rather than whatever the fake was
                    # retuned to while it waited.
                    gate._park()
                if fault:
                    # Close without answering: urllib raises, which is the transport case.
                    self.close_connection = True
                    return
                if garbage is not None:
                    payload = garbage.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if self.path in known:
                    node = {"FULL_PATH": self.path, "ACCESS": 3,
                            "VALUE": [known[self.path]]}
                    payload = json.dumps(node).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(payload)
                    return

                self.send_response(404)
                self.end_headers()

            def log_message(self, fmt, *args):
                return

        self._http = ThreadingHTTPServer((self.host, 0), Handler)
        self.http_port = self._http.server_address[1]
        self._http_thread = threading.Thread(target=self._http.serve_forever,
                                             args=(_SERVE_POLL_SECS,), daemon=True,
                                             name="FakeVRChatHTTP")
        self._http_thread.start()
        return self

    def __exit__(self, *exc):
        self._http.shutdown()
        self._http_thread.join(timeout=2)
        self._osc.shutdown()
        self._osc_thread.join(timeout=2)

    # ---- capture ----

    def _record(self, addr, *args):
        value = args[0] if args else None
        with self._cv:
            self.messages.append((addr, value))
            self._cv.notify_all()
        if self.echo_inbound:
            self.emit(addr, value)
        if self.on_receive is not None:
            self.on_receive(addr, value)

    def emit(self, address: str, value) -> None:
        """Send one message on the client's out-port stream, `copies` times.

        The Python type decides the OSC type tag (python-osc's inference: bool -> T/F, int ->
        ,i, float -> ,f), which is how a test states the wire type a value arrives with.
        """
        from pythonosc import udp_client
        if self.out_port is None:
            raise RuntimeError("FakeVRChat.out_port is not set; nothing to emit to")
        with self._lock:
            if self._sender is None:
                self._sender = udp_client.SimpleUDPClient(self.host, self.out_port)
            sender = self._sender
        for _ in range(self.copies):
            sender.send_message(address, value)

    def wait_for_count(self, n: int, timeout: float = 2.0) -> bool:
        """Block until at least n messages have arrived. UDP is asynchronous; a
        test that reads .messages straight after a send races the receiver."""
        deadline = time.monotonic() + timeout
        with self._cv:
            while len(self.messages) < n:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cv.wait(remaining)
        return True

    def addresses(self) -> list[str]:
        with self._lock:
            return [a for a, _ in self.messages]

    def values_for(self, address: str) -> list[object]:
        with self._lock:
            return [v for a, v in self.messages if a == address]
