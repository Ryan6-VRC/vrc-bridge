"""A fake VRChat: an OSCQuery HTTP endpoint plus an OSC receiver, on loopback.

Enough of VRChat's OSC surface to prove our client half without a headset --
that HOST_INFO is parsed, that a selected target actually receives datagrams,
and that a mapping's sends arrive at the addresses it claims.

mDNS discovery is deliberately NOT modelled. Browsing a real network from a test
is flaky and proves nothing about our code that pointing the client at a known
port does not. Tests inject the target; discovery stays a live-run concern.
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


class FakeVRChat:
    """Context manager exposing .osc_port, .http_port and the received messages."""

    def __init__(self, host: str = "127.0.0.1", host_info: dict | None = None):
        self.host = host
        self._host_info_override = host_info
        self.messages: list[tuple[str, object]] = []
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)

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
                else:
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
        with self._cv:
            self.messages.append((addr, args[0] if args else None))
            self._cv.notify_all()

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
