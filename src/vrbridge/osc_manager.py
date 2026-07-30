from __future__ import annotations

import http.server
import ipaddress
import json
import socket
import threading
from typing import Any, Callable, Dict, Optional, Set

from pythonosc import dispatcher, osc_server, udp_client
from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf


# serve_forever() only notices shutdown() between polls, so this interval *is* the
# teardown cost of each server it runs. Measured on one stop(): 0.437s HTTP plus
# 0.514s OSC at the 0.5s default, against 0.046s and 0.063s here -- 0.824s down to
# 0.106s for an embedder, and the round-trip suite from 19.6s to about 8s. The
# thread joins after shutdown() cost 0.000s either way, because shutdown() already
# blocks until the loop exits.
# Not a settings.py value: that file holds user-tunable mapping and hardware feel,
# and nothing about this number is a matter of taste.
_SERVE_POLL_SECS = 0.05


def _addr_to_ip(addr_bytes):
    try:
        return str(ipaddress.ip_address(addr_bytes))
    except Exception:
        # Fallback for older zeroconf
        if len(addr_bytes) == 4:
            return socket.inet_ntoa(addr_bytes)
        return "127.0.0.1"

class OSCManager:
    """OSC + OSCQuery with proper advertisement.
    - Binds OSC UDP on a free port; returns it from /?HOST_INFO (no hardcoded ports).
    - Advertises OSCQuery so VRChat can auto-send avatar params; CONTENTS contains
      only '/avatar' and '/usercamera'
    - Browses for VRChat's OSCQuery and selects VRChat as send target; ignores our own service.
    """
    def __init__(self, host: str = "127.0.0.1", logger=None, advertise: bool = True):
        self.host = host
        self.log = logger
        self._disp = dispatcher.Dispatcher()
        self._srv: Optional[osc_server.ThreadingOSCUDPServer] = None
        self._srv_thread: Optional[threading.Thread] = None
        self._listener: Optional[Callable[[str, Any], None]] = None
        self._client_lock = threading.Lock()
        self._client: Optional[udp_client.SimpleUDPClient] = None
        self._client_target: Optional[tuple[str,int]] = None
        self._cache: Dict[str, Any] = {}
        self._watched: Set[str] = set()
        self._cache_lock = threading.Lock()

        # All discovered services on the network
        self._discovered_services: Dict[str, ServiceInfo] = {}
        self._discovered_services_lock = threading.Lock()

        # OSCQuery
        self._advertise = advertise
        self._zeroconf: Optional[Zeroconf] = None
        self._browser: Optional[ServiceBrowser] = None
        self._service_info: Optional[ServiceInfo] = None
        self._current_service_name: Optional[str] = None
        # The rank the current target scored when it was chosen. Kept rather than
        # recomputed, because _service_rank also reads the mDNS server string and
        # that is not retained -- re-ranking from the name alone can score the
        # incumbent below the value that won it the slot.
        self._current_rank: int = -1
        self._httpd: Optional[http.server.ThreadingHTTPServer] = None
        self._http_thread: Optional[threading.Thread] = None
        self.http_port: Optional[int] = None
        self.osc_port: Optional[int] = None

        # default handler logs everything and updates cache for watched paths
        self._disp.set_default_handler(self._default_handler, needs_reply_address=False)

    # public API
    def set_listener(self, fn: Callable[[str, Any], None]): self._listener = fn

    def start(self):
        # Start HTTP first on a free port so we can advertise the correct port
        self._httpd = http.server.ThreadingHTTPServer((self.host, 0), self._make_http_handler())
        self.http_port = self._httpd.server_address[1]
        self._http_thread = threading.Thread(target=self._httpd.serve_forever,
                                             args=(_SERVE_POLL_SECS,),
                                             daemon=True, name="OSCQueryHTTP")
        self._http_thread.start()
        if self.log: self.log.info("OSCQuery HTTP on %s:%d", self.host, self.http_port)

        # Start OSC on a free port
        self._srv = osc_server.ThreadingOSCUDPServer((self.host, 0), self._disp)
        self.osc_port = self._srv.server_address[1]
        self._srv_thread = threading.Thread(target=self._srv.serve_forever,
                                            args=(_SERVE_POLL_SECS,),
                                            daemon=True, name="OSCServer")
        self._srv_thread.start()
        if self.log: self.log.info("OSC UDP server on %s:%d", self.host, self.osc_port)

        # mDNS. Pin the announcement to the one interface we actually serve on.
        #
        # A bare Zeroconf() means InterfaceChoice.All, which opens one announce
        # socket per interface that comes up -- measured 4 on a host with
        # loopback, a Hyper-V switch, Ethernet and Tailscale up, and 1 when
        # pinned. Every one of those announcements carries the same
        # loopback-only address record, so the extra copies advertise an
        # endpoint the receiving LAN cannot reach, and a client that opens a UDP
        # sender per announcement then sends us each message once per interface.
        # That is the leading explanation for the doubled inbound in docs/design.md.
        self._zeroconf = Zeroconf(interfaces=[self.host])
        if self._advertise:
            self._service_info = ServiceInfo(
                "_oscjson._tcp.local.",
                "VRBridge._oscjson._tcp.local.",
                addresses=[socket.inet_aton(self.host)],
                port=self.http_port,
                properties={},
                server="VRBridge.local."
            )
            self._zeroconf.register_service(self._service_info)
        self._browser = ServiceBrowser(self._zeroconf, "_oscjson._tcp.local.", self._BrowserListener(self))
        if self.log: self.log.info("mDNS service %s and browsing for VRChat...", "advertised" if self._advertise else "browsing")

    def stop(self):
        try:
            if self._zeroconf and self._service_info:
                self._zeroconf.unregister_service(self._service_info)
        except Exception as e:
            if self.log: self.log.debug("unregister_service failed: %s", e)
        finally:
            if self._zeroconf:
                try:
                    self._zeroconf.close()
                except Exception as e:
                    if self.log: self.log.debug("zeroconf.close failed: %s", e)
            self._zeroconf = None
        if self._httpd:
            try:
                self._httpd.shutdown()
            except Exception as e:
                if self.log: self.log.debug("HTTP shutdown failed: %s", e)
            finally:
                if self._http_thread:
                    self._http_thread.join(timeout=1.0)
            self._httpd = None
            self._http_thread = None
        if self._srv:
            try:
                self._srv.shutdown()
            except Exception as e:
                if self.log: self.log.debug("OSC shutdown failed: %s", e)
            finally:
                if self._srv_thread:
                    self._srv_thread.join(timeout=1.0)
            self._srv = None
            self._srv_thread = None

    def watch(self, address: str):
        """Track an OSC address: cache each value, and fire the listener on a change.

        This does **not** reach the served OSCQuery tree, whatever the address.
        That tree is the hardcoded two-node constant in _make_http_handler, and
        VRChat -- its only consumer -- does not read it to decide what to send us,
        so nothing turns on the difference. docs/design.md §OSCQuery interop gaps
        holds the measurement and the ruling that closing it buys nothing.
        """
        self._watched.add(address)
        def _handler(addr, *args):
            val = args[0] if args else None
            self._update_cache_and_fire(addr, val)
        self._disp.map(address, _handler)
        if self.log: self.log.debug("Watching OSC address %s", address)

    def get_cached(self, address: str, default=None):
        with self._cache_lock:
            return self._cache.get(address, default)

    def send(self, address: str, value):
        """Send a message to the selected VRChat OSC target (if any)."""
        with self._client_lock:
            client = self._client; target = self._client_target
        if not client:
            if self.log: self.log.warning("No VRChat OSC target yet; drop send %s=%s", address, value)
            return False
        try:
            client.send_message(address, value)
            if self.log: self.log.debug("Sent %s=%s to %s:%s", address, value, target[0], target[1])
            return True
        except Exception as e:
            if self.log: self.log.exception("OSC send failed for %s=%s: %s", address, value, e)
            return False

    def is_service_running(self, service_name_substring: str) -> bool:
        """Check if any discovered OSCQuery service name contains the given substring."""
        with self._discovered_services_lock:
            for name in self._discovered_services:
                if service_name_substring in name:
                    return True
        return False

    # internals
    def _default_handler(self, addr, *args):
        if addr in self._watched:
            val = args[0] if args else None
            self._update_cache_and_fire(addr, val)
        else:
            if self.log: self.log.debug("OSC recv (unwatched): %s %s", addr, args)

    def _update_cache_and_fire(self, addr, val):
        with self._cache_lock:
            old = self._cache.get(addr)
            self._cache[addr] = val
        if (old is None) or (val != old):
            if self._listener:
                try:
                    self._listener(addr, val)
                except Exception as e:
                    if self.log: self.log.exception("Listener error for %s: %s", addr, e)

    # Discovery
    class _BrowserListener:
        def __init__(self, outer: 'OSCManager'):
            self.outer = outer
        
        def add_service(self, zc, stype, name):
            info = zc.get_service_info(stype, name, timeout=2000)
            if self.outer.log: self.outer.log.info("Service added: %s", name)
            if info:
                with self.outer._discovered_services_lock:
                    self.outer._discovered_services[name] = info
                self.outer._consider_service(name, info)

        def update_service(self, zc, stype, name):
            info = zc.get_service_info(stype, name, timeout=2000)
            if info:
                with self.outer._discovered_services_lock:
                    self.outer._discovered_services[name] = info
                self.outer._consider_service(name, info)

        def remove_service(self, zc, stype, name):
            if self.outer.log: self.outer.log.info("Service removed: %s", name)
            with self.outer._discovered_services_lock:
                if name in self.outer._discovered_services:
                    del self.outer._discovered_services[name]

            # If current target removed, clear and wait for next best
            with self.outer._client_lock:
                if self.outer._current_service_name == name:
                    self.outer._client = None
                    self.outer._client_target = None
                    self.outer._current_service_name = None
                    # Kept consistent with the fields it describes rather than
                    # load-bearing: _consider_service reads the rank only while a
                    # client exists, so a stale value here is currently unreachable.
                    self.outer._current_rank = -1
                    if self.outer.log: self.outer.log.warning("Target %s removed; awaiting replacement...", name)

    def _service_rank(self, name: str, server: str | None) -> int:
        s = (name or "") + " " + (server or "")
        if self._service_info and name == self._service_info.name:
            return 0  # ourselves -> never target
        if "VRChat-Client" in s or "VRChat Client" in s or "VRChat" in s:
            return 3  # the one we want
        if "VRCFT" in s or "FaceTracking" in s:
            return 1  # not what we want for /input/*
        return 1       # generic other OSC apps

    def _consider_service(self, name, info):
        # Skip ourselves
        if self._service_info and name == self._service_info.name:
            return
        rank = self._service_rank(name, getattr(info, 'server', None))
        with self._client_lock:
            # News about the service we are already pointing at is an *update*, not
            # a rival bid, and must never be rank-compared: VRChat keeps its service
            # name across a restart and comes back on a fresh OSC port, so the ranks
            # tie, the tie is refused, and we go on sending into the dead port until
            # a remove_service happens to fire first. Following it is the whole
            # point of watching for updates.
            is_current = (self._current_service_name is not None
                          and name == self._current_service_name)
            # Only a strictly better rank unseats an incumbent, so VRCFT cannot take
            # the slot off VRChat.
            if self._client is not None and not is_current and self._current_rank >= rank:
                return
        # Query HOST_INFO
        host = _addr_to_ip(info.addresses[0]) if info.addresses else "127.0.0.1"
        hi = self._host_info(host, info.port)
        if not hi or "OSC_PORT" not in hi:
            return
        try:
            osc_port = int(hi["OSC_PORT"])
        except Exception:
            return
        with self._client_lock:
            # mDNS republishes a service on its own refresh schedule, and now that
            # every such refresh re-resolves the current target, an unchanged one
            # would otherwise rebuild the socket and re-log on each. The HTTP query
            # above is unavoidable -- the port is not knowable without asking.
            if self._client is not None and self._client_target == (host, osc_port) \
                    and self._current_service_name == name:
                return
            self._client = udp_client.SimpleUDPClient(host, osc_port)
            self._client_target = (host, osc_port)
            self._current_service_name = name
            self._current_rank = rank
        if self.log: self.log.info("VRChat target set to %s:%d (via %s)", host, osc_port, name)

    def _make_http_handler(self):
        outer = self

        def build_tree() -> dict:
            return {
                "avatar":     {"FULL_PATH": "/avatar",     "CONTENTS": {}},
                "usercamera": {"FULL_PATH": "/usercamera", "CONTENTS": {}},
            }

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/":
                    contents = build_tree()
                    self._send_json({"CONTENTS": contents})
                elif self.path == "/?HOST_INFO":
                    self._send_json({"OSC_PORT": outer.osc_port})
                else:
                    self.send_response(404); self.end_headers()
            def log_message(self, fmt, *args): return
            def _send_json(self, data):
                body = json.dumps(data).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
        return Handler

    @staticmethod
    def _host_info(host: str, http_port: int):
        import urllib.request
        url = f"http://[{host}]:{http_port}/?HOST_INFO" if ":" in host else f"http://{host}:{http_port}/?HOST_INFO"
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None
