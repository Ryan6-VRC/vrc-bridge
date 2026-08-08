from __future__ import annotations

import fnmatch
import http.server
import ipaddress
import json
import socket
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Set

from pythonosc import dispatcher, osc_server, udp_client
from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf


# serve_forever() only notices shutdown() between polls, so this interval *is* the
# teardown cost of each server it runs. Measured non-advertising at the 0.5s default:
# 0.310s HTTP plus 0.513s OSC, a 0.824s stop() against 0.106s here, and the full suite
# from 19.6s to 7.6s. An advertising stop() -- what an embedder pays -- adds ~0.27s in
# zeroconf.close() on top, which this lever does not touch. The thread joins after
# shutdown() cost 0.000s in every configuration, because shutdown() already blocks
# until the loop exits.
# Not a settings.py value: that file holds user-tunable mapping and hardware feel,
# and nothing about this number is a matter of taste.
_SERVE_POLL_SECS = 0.05


#: Why every fetch() outcome is named rather than collapsed to None: the caller has to
#: act differently on each. A 404 means the worn avatar does not declare the node, which
#: is a normal state and not an error; a transport failure means we learned nothing and
#: should ask again; malformed means the peer answered something we cannot use, which is
#: worth reporting once rather than retrying. _host_info swallows all three into None,
#: and a caller inheriting that cannot keep CLAUDE.md rule 7's named-offender promise.
FETCH_OK = "ok"
FETCH_NO_PEER = "no-peer"        # nothing has ever been resolved (a pinned target never will)
FETCH_PEER_GONE = "peer-gone"    # a peer was resolved, then withdrew its service
FETCH_NOT_FOUND = "not-found"    # 404: the peer serves no such node
FETCH_TRANSPORT = "transport"    # timeout, refused, or a non-404 HTTP status
FETCH_MALFORMED = "malformed"    # answered 200, but not a JSON node carrying VALUE


@dataclass(frozen=True)
class FetchResult:
    """One OSCQuery single-node read. `reason` is one of the FETCH_* constants above."""
    reason: str
    value: Any = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.reason == FETCH_OK


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

    Both halves of that handshake assume a peer that advertises itself and that reads
    our /?HOST_INFO. A peer doing neither -- the Av3Emulator, which carries OSC and no
    service discovery at all -- is addressed by naming its ports instead:
    `target=(host, port)` for the send side, `bind_port=` for the receive side. The two
    are independent, but an emulator loop wants both: it listens on 9000 and sends to a
    fixed 127.0.0.1:9001, which no floating port can satisfy.
    """
    def __init__(self, host: str = "127.0.0.1", logger=None, advertise: bool = True,
                 target: Optional[tuple[str, int]] = None, bind_port: int = 0,
                 discover: bool = True):
        self.host = host
        # Browsing reaches the real network, so a test that wants a fake peer has to be able
        # to switch it off. Until the browser was given its own unpinned Zeroconf it was
        # pinned to loopback and therefore deaf, and the suite's isolation was an accident of
        # that bug: with discovery actually working, a live VRChat on the same host wins the
        # target away from the fake mid-test.
        self._discover = discover
        self.log = logger
        self._disp = dispatcher.Dispatcher()
        self._srv: Optional[osc_server.ThreadingOSCUDPServer] = None
        self._srv_thread: Optional[threading.Thread] = None
        self._listener: Optional[Callable[[str, Any], None]] = None
        self._client_lock = threading.Lock()
        self._client: Optional[udp_client.SimpleUDPClient] = None
        self._client_target: Optional[tuple[str,int]] = None
        # The peer's OSCQuery *HTTP* endpoint, which is a different port from the OSC one
        # in _client_target and the only thing fetch() can ask. _consider_service already
        # learns it as info.port to read OSC_PORT and used to discard it afterwards.
        # Stays None under a pinned target, which advertises nothing and serves no tree --
        # so fetch() answers FETCH_NO_PEER there rather than appearing to work.
        self._peer_http: Optional[tuple[str, int]] = None
        # Whether the peer above was resolved and then withdrew, as against never having been
        # resolved at all. Both leave _peer_http None, and collapsing them cost fetch() the
        # named-failure vocabulary it otherwise keeps: "press again once VRChat is found" is
        # wrong advice for a client that was found and crashed. Guarded by _client_lock, with
        # _peer_http, so a reader sees the pair consistently.
        self._peer_lost = False
        # Fired once a discovered send target is chosen. See add_target_listener.
        self._target_listeners: list[Callable[[tuple[str, int]], None]] = []
        self._cache: Dict[str, Any] = {}
        self._watched: Set[str] = set()
        # fnmatch-style patterns admitted by _default_handler, which every datagram not
        # explicitly mapped already reaches. This admits named shapes of traffic; it
        # enumerates nothing, so the parameter-discovery descope (docs/design.md) holds.
        self._watched_patterns: Set[str] = set()
        self._cache_lock = threading.Lock()

        # A target we were told to use rather than one we found. Held so
        # _consider_service can refuse to revise it -- see the guard there.
        self._pinned_target = target
        self._bind_port = bind_port
        if target is not None:
            # Built here and not in start(), so that no window exists in which the
            # browser could fill the slot first: a SimpleUDPClient is a connectionless
            # sender and needs no server of ours running. _current_service_name stays
            # None, which is also what stops remove_service from ever clearing a
            # target no discovered service backs.
            self._client = udp_client.SimpleUDPClient(target[0], target[1])
            self._client_target = (target[0], target[1])
            if self.log:
                self.log.info("OSC target pinned to %s:%d; discovery will not revise it",
                              target[0], target[1])

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

    def add_target_listener(self, fn: Callable[[tuple[str, int]], None]):
        """Call `fn(target)` each time discovery selects or re-resolves a send target.

        Exists because nothing else announces it: _consider_service sets the target under
        the client lock and only logs, so a consumer that must act *when VRChat appears*
        -- read a sentinel node, prime a cache -- had no event to hang on and would have
        to poll a private field.

        Additive, and that is the point: `VRBridge.__init__` registers its own multiplexer
        here, so a single settable slot would let any embedder's direct call silently
        unregister every mapping's target callback -- including the wardrobe's invalidate --
        with nothing logged and no symptom until an avatar change went unnoticed. Embedders
        should still prefer `VRBridge.on_target_selected`, which delivers a CallbackContext;
        this is the layer beneath it.

        Two things the callback must respect. It runs on **zeroconf's single dispatch
        thread**, which serialises every service callback, and docs/design.md accepts one
        blocking _host_info there as a deliberate cost -- so do slow work on your own
        thread and return. And it fires on a *re-resolve* too (VRChat restarting onto a
        fresh OSC port is the case that exists for), so it is not once-per-process and the
        handler has to be idempotent.

        A pinned target never fires it: nothing was discovered, and per docs/design.md
        naming a target takes the question away rather than entering it as a bid.
        """
        self._target_listeners.append(fn)

    def start(self):
        # A fresh session has lost nothing. start()'s own bind_port failure invites an
        # embedder to stop() and start() again on another port, and without this reset that
        # second session would report a withdrawn peer it never had.
        with self._client_lock:
            self._peer_lost = False

        # Start HTTP first on a free port so we can advertise the correct port
        self._httpd = http.server.ThreadingHTTPServer((self.host, 0), self._make_http_handler())
        self.http_port = self._httpd.server_address[1]
        self._http_thread = threading.Thread(target=self._httpd.serve_forever,
                                             args=(_SERVE_POLL_SECS,),
                                             daemon=True, name="OSCQueryHTTP")
        self._http_thread.start()
        if self.log: self.log.info("OSCQuery HTTP on %s:%d", self.host, self.http_port)

        # Start OSC on a free port, or on the one we were told to take. Naming a port
        # is the only way a peer that cannot read our /?HOST_INFO can reach us, and it
        # introduces the one failure the floating bind never had: the port is occupied.
        # Say which port and which option asked for it -- a bare WinError 10048 names
        # neither. The HTTP server above is already running when this raises; an embedder
        # retrying a different port calls stop(), which walks each block independently
        # and takes it down, so unwinding it here would only duplicate stop().
        try:
            self._srv = osc_server.ThreadingOSCUDPServer((self.host, self._bind_port), self._disp)
        except OSError as e:
            if self._bind_port:
                raise OSError(
                    f"cannot bind the OSC listener to {self.host}:{self._bind_port} "
                    f"(asked for by bind_port= / --osc-bind-port): {e}") from e
            raise
        self.osc_port = self._srv.server_address[1]
        self._srv_thread = threading.Thread(target=self._srv.serve_forever,
                                            args=(_SERVE_POLL_SECS,),
                                            daemon=True, name="OSCServer")
        self._srv_thread.start()
        if self.log: self.log.info("OSC UDP server on %s:%d", self.host, self.osc_port)

        # mDNS. One bare Zeroconf() -- InterfaceChoice.All -- announcing AND browsing,
        # and neither half tolerates an interface pin. Multicast does not traverse the
        # loopback interface in either direction: a Zeroconf pinned to 127.0.0.1 browses
        # deaf, and a loopback-pinned *announcement* is one no client ever hears -- and
        # because browsing fails loud (no target, sends dropped) while an unheard
        # announcement fails silent (outbound healthy, inbound simply absent), a re-pin
        # of the announce half is the error that survives daily use. docs/design.md
        # SecInbound holds both live-client measurements, including why the doubled
        # inbound is no reason to pin: the client opens exactly two senders per
        # advertisement whether we announce on one interface or four. All's real cost is
        # cosmetic -- one announce socket per interface, each carrying this loopback-only
        # address record onto a LAN that cannot reach it.
        self._zeroconf = Zeroconf()
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

        if self._discover:
            self._browser = ServiceBrowser(self._zeroconf, "_oscjson._tcp.local.",
                                           self._BrowserListener(self))
        if self.log:
            self.log.info("mDNS service %s; %s",
                          "advertised" if self._advertise else "not advertised",
                          "browsing for VRChat" if self._discover else "discovery off")

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

        # Drop the readable peer, for the reason remove_service already states: fetch() must
        # not keep querying the HTTP endpoint of a peer we have stopped serving and report its
        # answers as the worn avatar's.
        #
        # `_peer_lost` is deliberately NOT set. It means the peer withdrew, which is a claim
        # about the network; tearing down our own end is not that, and asserting it would have
        # `fetch()` tell a caller to wait for a client that never left. Reserved for
        # remove_service, which is the only place something really went away.
        #
        # `_client` is also left alone. A pulse caught between its value and its trailing zero
        # needs a live sender or /input/Voice stays latched and keys the mic open;
        # VRBridge.stop() drains pulses before calling this, but a library embedder calling
        # stop() directly does not, and a dropped trailing zero is worse than a send into a
        # torn-down session.
        with self._client_lock:
            self._peer_http = None

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

    def watch_pattern(self, pattern: str) -> None:
        """Watch every address matching an fnmatch pattern (`*`, `?`, `[seq]`).

        Same cache, change filter, and listener as watch(); the concrete arriving
        address is what is cached and fired, never the pattern. Kept out of the
        dispatcher: python-osc compiles mapped addresses through its own OSC-pattern
        translation, and this repo's contract is fnmatch — one grammar, ours.

        A pattern also admits its own literal spelling. Nothing tells a name containing
        `?` or `[` apart from a pattern, and reading `Foo[1]` only as a pattern silently
        logged `Foo1` — an address the caller never named — while logging nothing for the
        one they did. Over-admitting is visible in the output; the loss was not.
        """
        self._watched_patterns.add(pattern)
        if self.log: self.log.debug("Watching OSC pattern %s", pattern)

    def get_cached(self, address: str, default=None):
        with self._cache_lock:
            return self._cache.get(address, default)

    def forget(self, address: str) -> None:
        """Drop a watched address's cached value, so the next arrival counts as a change.

        `_update_cache_and_fire` suppresses a value equal to the last one seen, which is
        what keeps a streaming parameter from waking every listener. That filter has one
        blind spot: a consumer whose *action* changed the world can need the same value
        delivered twice. Forgetting is how it says so, and is cheaper than teaching the
        filter about consumers.
        """
        with self._cache_lock:
            self._cache.pop(address, None)

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
        # Snapshot: watch_pattern may add on another thread, and a set cannot be
        # iterated across a mutation (_watched is only ever membership-tested, so it
        # never had this constraint). Equality admits a pattern entry that is really a
        # literal name -- see watch_pattern's contract.
        if addr in self._watched or any(
                addr == p or fnmatch.fnmatchcase(addr, p)
                for p in tuple(self._watched_patterns)):
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
                    # Cleared with the client, or fetch() would keep querying the HTTP
                    # endpoint of a peer we have stopped sending to and report its answers
                    # as current.
                    self.outer._peer_http = None
                    # Set here, inside the "this was *our* target" branch, and not merely
                    # under the lock: an unrelated service withdrawing -- VRCFaceTracking
                    # closing, say -- must not make fetch() report that the peer we are
                    # reading from went away while VRChat is still live.
                    self.outer._peer_lost = True
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
        # A pinned target is an instruction, not a bid. Ranking exists to choose among
        # peers we did not name, so nothing discovered may revise one we did -- not even
        # a rank-3 VRChat, which is exactly the case that makes this load-bearing: the
        # emulator sits on 127.0.0.1:9000 and a live client outranks it, so under a
        # rankable pin a run aimed at the emulator would retarget onto the real avatar
        # mid-session, on mDNS callback timing.
        #
        # Returning here rather than earlier leaves discovery *observing* while it stops
        # *deciding*: _BrowserListener has already recorded the service, so
        # is_service_running -- which osc_vrcft depends on -- answers as it always did.
        if self._pinned_target is not None:
            return
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
            # mDNS republishes on its own refresh schedule, and following the incumbent
            # means every refresh re-resolves it, so an unchanged one has to be caught
            # here or each would rebuild the socket and re-log. This check sits after
            # the query rather than before it because the OSC port is not knowable
            # without asking -- so an incumbent's refresh now costs a blocking
            # _host_info on zeroconf's single dispatch thread, stalling every service
            # callback for its duration. Bounded at a few refreshes per record TTL, and
            # accepted rather than resolved off-thread: concurrent callbacks would make
            # the two-phase read here racy, and their serialisation is exactly what
            # lets it skip holding the lock across the query.
            # Every piece of derived state must already match, not just the send target.
            # `stop()` drops `_peer_http` while leaving the client standing, so a target
            # check alone would treat the peer's return on an unchanged port -- the normal
            # case, since VRChat sits on 9000 -- as nothing to do, and `fetch()` would stay
            # peerless for the rest of the process while `send` kept working.
            if self._client is not None and self._client_target == (host, osc_port) \
                    and self._current_service_name == name \
                    and self._peer_http == (host, info.port):
                return
            self._client = udp_client.SimpleUDPClient(host, osc_port)
            self._client_target = (host, osc_port)
            self._peer_http = (host, info.port)
            # A peer is readable again, so a previous withdrawal is no longer the answer.
            self._peer_lost = False
            self._current_service_name = name
            self._current_rank = rank
        if self.log: self.log.info("VRChat target set to %s:%d (via %s)", host, osc_port, name)
        # Outside the lock deliberately: a listener that reaches back into OSCManager --
        # fetch() takes the same lock to read _peer_http -- would deadlock on a
        # non-reentrant Lock. The early-return above means this fires only on a real
        # change, so a listener sees one event per selection rather than per mDNS refresh.
        for fn in list(self._target_listeners):
            try:
                fn((host, osc_port))
            except Exception:
                # Caught per listener, not around the loop: one throwing consumer must not
                # cost us the target we just resolved, nor deprive the *other* listeners of
                # the event, nor kill zeroconf's dispatch thread and every later callback.
                if self.log:
                    self.log.exception("Target listener raised for %s:%d", host, osc_port)

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

    @property
    def target_is_pinned(self) -> bool:
        """True when the send target was named rather than discovered.

        Exists so a caller can tell the three states behind a missing OSCQuery peer apart:
        a pin (which serves no tree and never will), discovery that has not resolved yet
        (normal for the first seconds of any run), and a target that went away. They need
        different messages, and only the first has `pinned_manifest_id` as its answer.
        """
        return self._pinned_target is not None

    def fetch(self, address: str, timeout: float = 2.0) -> FetchResult:
        """Read one parameter node's live VALUE from the peer's OSCQuery server.

        A targeted single-node GET, never a tree walk: docs/design.md descopes parameter
        discovery, and this exists for the opposite case -- a consumer that already knows
        the address and wants the value the *worn avatar* currently holds. VRChat serves
        unsynced parameters here at full local precision, and 404s an address no worn
        avatar declares, so a 404 is a legitimate answer about the avatar rather than a
        failure. Every outcome is named; see the FETCH_* constants.

        Blocking, and the caller owns the thread choice. docs/design.md: the OSC datagram
        path tolerates a block, the controller path does not -- and a target-listener
        callback is on zeroconf's dispatch thread, which is a third case that already pays
        for one blocking query per record refresh.
        """
        with self._client_lock:
            peer = self._peer_http
            lost = self._peer_lost
        if peer is None:
            if self.target_is_pinned:
                return FetchResult(
                    FETCH_NO_PEER,
                    detail="the send target was pinned, and a pinned peer serves no tree")
            if lost:
                # Separated from "never discovered" because the remedies differ: this one
                # needs the client to come back, and no amount of waiting on discovery to
                # finish will help. target_is_pinned's docstring promises a caller can tell
                # these three states apart; without this it could tell two.
                return FetchResult(
                    FETCH_PEER_GONE,
                    detail="the OSCQuery peer we were reading from withdrew its service")
            return FetchResult(FETCH_NO_PEER,
                               detail="no OSCQuery peer has been discovered yet")

        import urllib.error
        import urllib.parse
        import urllib.request
        host, http_port = peer
        # Bracket an IPv6 literal, as _host_info does; a bare colon parses as a port.
        base = f"http://[{host}]:{http_port}" if ":" in host else f"http://{host}:{http_port}"
        # Percent-encode the address. Unencoded, a `#` in a parameter name is stripped as a
        # URL fragment and the GET returns 200 for a *different* node -- a silently wrong
        # answer, which is the one outcome this function's named-failure vocabulary has no
        # way to express. A space would likewise report FETCH_TRANSPORT ("ask again") for a
        # node that is there. `safe="/"` keeps the OSC path separators intact.
        url = base + urllib.parse.quote(address, safe="/")
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            # HTTPError subclasses URLError, so it has to be caught first or a 404 would
            # read as a transport failure and the caller would retry a settled answer.
            if e.code == 404:
                return FetchResult(FETCH_NOT_FOUND, detail=f"{url} -> 404")
            return FetchResult(FETCH_TRANSPORT, detail=f"{url} -> HTTP {e.code}")
        except Exception as e:
            return FetchResult(FETCH_TRANSPORT, detail=f"{url} -> {type(e).__name__}: {e}")

        try:
            node = json.loads(body)
        except Exception as e:
            return FetchResult(FETCH_MALFORMED, detail=f"{url} -> not JSON: {e}")
        if not isinstance(node, dict) or "VALUE" not in node:
            return FetchResult(FETCH_MALFORMED, detail=f"{url} -> no VALUE attribute")
        value = node["VALUE"]
        # OSCQuery types VALUE as an array -- one entry per type tag -- and VRChat's
        # parameter nodes carry exactly one. Unwrap a single-element list so callers
        # compare against a scalar; leave anything else alone rather than guessing, since
        # a multi-tag node is not a parameter and the caller should see that it isn't.
        if isinstance(value, list) and len(value) == 1:
            value = value[0]
        return FetchResult(FETCH_OK, value=value)

    @staticmethod
    def _host_info(host: str, http_port: int):
        import urllib.request
        url = f"http://[{host}]:{http_port}/?HOST_INFO" if ":" in host else f"http://{host}:{http_port}/?HOST_INFO"
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None
