from __future__ import annotations

import fnmatch
import http.server
import ipaddress
import json
import socket
import threading
import time
from dataclasses import dataclass, replace
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


#: Addresses where the value repeating does not make the message redundant, so the
#: change filter's value-equality test is the wrong instrument. On `/avatar/change` the
#: repeat is the whole event: the in-client Reset Avatar and a world rejoin each reload
#: the worn avatar while announcing the id already cached. An OSC change naming the worn
#: avatar repeats the id too but reloads nothing (the echo alone, measured); it is
#: delivered anyway, because the fold is by time and cannot tell that echo from the
#: repeat a reload makes. Kept per address rather than lifted to a setting -- which
#: addresses carry that meaning is a property of VRChat's wire, not a matter of taste.
#: `docs/design.md` §Inbound delivery semantics holds the measurements and what earns an
#: address a place here.
REFIRE_ON_REPEAT: Set[str] = {"/avatar/change"}

#: How long an exempt address folds a repeat for: long enough to swallow the client's twin
#: copies, which arrive within a millisecond of each other where it doubles at all. Do not
#: widen it toward seconds -- that is where a deliberate re-wear starts being eaten, the
#: failure the carve-out exists to prevent.
REFIRE_FOLD_WINDOW_SECS = 0.25

#: Before a VRChat rival displaces an incumbent VRChat that did not answer its OSCQuery, the
#: incumbent is asked once more after this long: one hitch on a live client's HTTP must not
#: hand the slot to a second client for good.
INCUMBENT_RETRY_SECS = 0.5


#: The scores _service_rank hands out. Named because the VRChat score is no longer only an
#: input to target selection: fetch() reports it out as PeerIdentity.is_vrchat, so
#: `_current_rank == _RANK_VRCHAT` has to read as an identity test rather than arithmetic.
_RANK_SELF = 0
_RANK_OTHER = 1
_RANK_VRCHAT = 3


#: OSCQuery TYPE tag -> the Python type python-osc hands a handler for that tag, so a value
#: read from the tree replays with the wire type it would have arrived with.
_OSCQUERY_TYPES: Dict[Optional[str], Callable[[Any], Any]] = {
    "T": bool, "F": bool, "i": int, "f": float}


#: Why every fetch() outcome is named rather than collapsed to None: the caller has to
#: act differently on each. A 404 means the worn avatar does not declare the node, which
#: is a normal state and not an error; a transport failure means we learned nothing and
#: should ask again; malformed means the peer answered something we cannot use, which is
#: worth reporting once rather than retrying. _host_info swallows all three into None,
#: and a caller inheriting that cannot keep CLAUDE.md rule 7's named-offender promise.
FETCH_OK = "ok"
FETCH_NO_PEER = "no-peer"        # nothing resolved (pinned: no client advertises the pinned port)
FETCH_PEER_GONE = "peer-gone"    # a peer was resolved, then withdrew its service
FETCH_NOT_FOUND = "not-found"    # 404: the peer serves no such node
FETCH_TRANSPORT = "transport"    # timeout, refused, or a non-404 HTTP status
FETCH_MALFORMED = "malformed"    # answered 200, but not a JSON node carrying VALUE


@dataclass(frozen=True)
class PeerIdentity:
    """Which peer answered a fetch, by the mDNS service that holds the target slot.

    Exists because a 404 is ambiguous without it. `FETCH_NOT_FOUND` from VRChat says the
    worn avatar does not declare the node; the same 404 from any other OSCQuery app says
    only that we asked a tree with no avatar parameters in it. A caller that phrases a
    message about the avatar needs to know which of those it is holding.

    `is_vrchat` is what the advertisement *claims*, not what the peer is. It comes from the
    rank the service scored, and `_service_rank` reads the instance name **and the mDNS
    server string** -- so a service whose own name says nothing, advertised from a host
    called VRChat-Client, reads as VRChat here. Whoever registers a service chooses both.
    A browse offers no better signal, so anything written from this says what the peer
    identifies itself as, never what it is.

    Do not re-derive `is_vrchat` from `name` alone. The rank is stored rather than
    recomputed precisely because the server string is not retained, and a fresh derivation
    would disagree with the selection that actually happened.
    """
    name: str
    is_vrchat: bool


@dataclass(frozen=True)
class FetchResult:
    """One OSCQuery single-node read. `reason` is one of the FETCH_* constants above.

    `peer` names the target whose endpoint was queried -- not necessarily one that answered,
    since a refusal or a timeout reports FETCH_TRANSPORT against a peer that said nothing.
    It is None only where there was no peer to ask: a pin no VRChat client's HOST_INFO
    matches, discovery that has not resolved, and a peer that withdrew.
    """
    reason: str
    value: Any = None
    detail: str = ""
    peer: Optional[PeerIdentity] = None

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


def _own_addresses() -> Set[str]:
    """This machine's addresses by its hostname; empty if the lookup fails.

    UnicodeError too: getaddrinfo runs an odd hostname through the IDNA codec."""
    try:
        return {ai[4][0] for ai in socket.getaddrinfo(socket.gethostname(), None)}
    except (OSError, UnicodeError):
        return set()

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
        # Under a pinned target it is the VRChat client advertising the pinned host and OSC
        # port, and stays None while none does -- so fetch() answers FETCH_NO_PEER against
        # a peer that advertises nothing (the Av3Emulator) rather than appearing to work.
        self._peer_http: Optional[tuple[str, int]] = None
        # Whether the peer above was resolved and then withdrew, as against never having been
        # resolved at all. Both leave _peer_http None, and collapsing them cost fetch() the
        # named-failure vocabulary it otherwise keeps: "press again once VRChat is found" is
        # wrong advice for a client that was found and crashed. Guarded by _client_lock, with
        # _peer_http, so a reader sees the pair consistently.
        self._peer_lost = False
        # Fired once a discovered send target is chosen. See add_target_listener.
        self._target_listeners: list[Callable[[tuple[str, int]], None]] = []
        # Fired when a pinned target's readable peer is found or renamed. See add_peer_listener.
        self._peer_listeners: list[Callable[[str], None]] = []
        self._cache: Dict[str, Any] = {}
        # monotonic stamp of the last fire per REFIRE_ON_REPEAT address; under _cache_lock.
        self._last_fired: Dict[str, float] = {}
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
        # Read once: a pin on loopback has to match a client whose mDNS address record is
        # this machine's LAN address. See _same_host.
        self._own_addrs = _own_addresses() if target is not None else set()
        if target is not None:
            # Built here and not in start(), so that no window exists in which the
            # browser could fill the slot first: a SimpleUDPClient is a connectionless
            # sender and needs no server of ours running. remove_service checks the pin
            # before clearing anything, so discovery never clears a target it did not set.
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

    def add_peer_listener(self, fn: Callable[[str], None]):
        """Call `fn(service_name)` when a pinned target's readable peer is found, or is
        found again under a new name (a restarted client).

        For what binds to the client rather than to the send target -- the client-log
        tailers. A pin never fires the target listeners, and those clear state on what they
        read as a join, so this is its own event: nothing was selected and the target has
        not moved. Same thread and rules as add_target_listener.
        """
        self._peer_listeners.append(fn)

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
        # of the announce half is the error that survives daily use. Neither the doubled
        # inbound nor the announce-socket count is a license to pin -- docs/design.md
        # SecInbound delivery semantics holds the measurements. All's cost is cosmetic:
        # one announce socket per interface, each carrying this loopback-only address
        # record onto a LAN that cannot reach it.
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

        Never call it on a `REFIRE_ON_REPEAT` address: the filter already delivers repeats
        there, and forgetting drops the cached value the fold's short-circuit reads, so the
        next twin copy is delivered twice.
        """
        with self._cache_lock:
            self._cache.pop(address, None)

    def prime(self, address: str, value) -> None:
        """Set a watched address's cached value without firing anything.

        For a consumer that learned the current value some other way than the stream -- an
        OSCQuery read -- and folds it into its own state itself. Priming keeps the change
        filter honest afterwards: with the stale value left cached, the client's next send of
        the value just read could equal it and be suppressed, and the consumer would never
        hear the parameter move back. Never on a `REFIRE_ON_REPEAT` address, as forget().
        """
        with self._cache_lock:
            self._cache[address] = value

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
        now = time.monotonic()
        with self._cache_lock:
            old = self._cache.get(addr)
            self._cache[addr] = val
            fire = (old is None) or (val != old)
            if addr in REFIRE_ON_REPEAT:
                if not fire:
                    # Deliver the repeat unless it is the twin of the one just delivered.
                    fire = (now - self._last_fired.get(addr, float("-inf"))
                            >= REFIRE_FOLD_WINDOW_SECS)
                if fire:
                    # Stamped on a value *change* too, which is what arms the fold against
                    # that change's own twin: the twin is a repeat, and without this stamp
                    # the window has nothing to measure from. It dates the decision to
                    # deliver, not the delivery -- the listener has not run yet.
                    self._last_fired[addr] = now
        if fire:
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

            # If current target removed, clear and wait for next best. Under a pin the
            # service named here is only the readable peer, so the send target stands:
            # discovery never set it and may never clear it.
            with self.outer._client_lock:
                if self.outer._current_service_name == name:
                    pinned = self.outer._pinned_target is not None
                    if not pinned:
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
                    # load-bearing. Two readers now: _consider_service reads the rank only
                    # while a client exists, and fetch() reads it only while _peer_http is
                    # set -- which this same block clears. A stale value is unreachable
                    # from either, so keep the census current if a third reader appears.
                    self.outer._current_rank = -1
                    if self.outer.log:
                        self.outer.log.warning(
                            "OSCQuery peer %s removed; sends stay on the pinned target" if pinned
                            else "Target %s removed; awaiting replacement...", name)

    def _same_host(self, a: str, b: str) -> bool:
        """Literally equal, or both this machine: loopback, `localhost`, or an own address."""
        def own(h: str) -> bool:
            try:
                if ipaddress.ip_address(h).is_loopback:
                    return True
            except ValueError:
                pass
            return h == "localhost" or h in self._own_addrs
        return a == b or (own(a) and own(b))

    def _service_rank(self, name: str, server: str | None) -> int:
        s = (name or "") + " " + (server or "")
        if self._service_info and name == self._service_info.name:
            return _RANK_SELF  # ourselves -> never target
        if "VRChat-Client" in s or "VRChat Client" in s or "VRChat" in s:
            return _RANK_VRCHAT  # the one we want
        if "VRCFT" in s or "FaceTracking" in s:
            return _RANK_OTHER  # not what we want for /input/*
        return _RANK_OTHER      # generic other OSC apps

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
        #
        # A pin still gets a *readable* peer: the VRChat client whose HOST_INFO names the
        # pinned host and OSC port is the one we are sending to, so its tree is the worn
        # avatar's. Only the peer fields are set -- never `_client`/`_client_target`, and no
        # target listener fires, because nothing was selected; a new peer name fires the
        # peer listeners instead. The host matches when it is the pinned one or both are
        # this machine's (_same_host). VRChat's `_oscjson._tcp` record and HOST_INFO carry
        # 127.0.0.1 (measured); only its `_osc._udp` record carries the LAN address, so the
        # own-address half is for a client that advertises otherwise.
        if self._pinned_target is not None:
            if self._service_rank(name, getattr(info, 'server', None)) != _RANK_VRCHAT:
                return
            host = _addr_to_ip(info.addresses[0]) if info.addresses else "127.0.0.1"
            if not self._same_host(host, self._pinned_target[0]):
                # Named, so a VPN or virtual adapter advertising an address we do not hold
                # reads as a host mismatch rather than as no client on the pinned port.
                if self.log:
                    self.log.debug("Pinned target: %s advertises %s, not the pinned host %s "
                                   "or one of this machine's addresses; not read",
                                   name, host, self._pinned_target[0])
                return
            try:
                osc_port = int(self._host_info(host, info.port)["OSC_PORT"])
            except Exception:
                return  # unresolved says nothing about the port; leave any peer standing
            with self._client_lock:
                if osc_port != self._pinned_target[1]:
                    if self._current_service_name == name:
                        # Our peer moved off the pinned port, so its tree is no longer
                        # the one we send to.
                        self._peer_http = None
                        self._peer_lost = True
                        self._current_service_name = None
                        self._current_rank = -1
                    return
                if self._current_service_name == name and self._peer_http == (host, info.port):
                    return
                renamed = self._current_service_name != name
                self._peer_http = (host, info.port)
                self._peer_lost = False
                self._current_service_name = name
                self._current_rank = _RANK_VRCHAT
            if self.log:
                self.log.info("OSCQuery peer for the pinned target %s:%d is %s",
                              host, osc_port, name)
            # Outside the lock, per listener, for the reasons the target listeners are.
            if renamed:
                for fn in list(self._peer_listeners):
                    try:
                        fn(name)
                    except Exception:
                        if self.log:
                            self.log.exception("Peer listener raised for %s", name)
            return
        # Skip ourselves
        if self._service_info and name == self._service_info.name:
            return
        rank = self._service_rank(name, getattr(info, 'server', None))
        incumbent = None
        with self._client_lock:
            # News about the service we are already pointing at is an *update*, not
            # a rival bid, and must never be rank-compared: a republication under the
            # same name on a fresh OSC port ties, the tie is refused, and we go on
            # sending into the dead port until a remove_service happens to fire first.
            # Following it is the whole point of watching for updates.
            is_current = (self._current_service_name is not None
                          and name == self._current_service_name)
            # Only a strictly better rank unseats an incumbent, so VRCFT cannot take
            # the slot off VRChat -- with one exception below.
            if self._client is not None and not is_current and self._current_rank >= rank:
                if not (rank == self._current_rank == _RANK_VRCHAT and self._peer_http):
                    return
                incumbent = self._peer_http
                # Its latest advertisement, if it republished on a new port and that update
                # is still queued behind this one.
                with self._discovered_services_lock:
                    latest = self._discovered_services.get(self._current_service_name)
                if latest is not None and latest.addresses:
                    incumbent = (_addr_to_ip(latest.addresses[0]), latest.port)
        if incumbent is not None:
            alive = self._host_info(*incumbent) is not None
            if not alive:
                time.sleep(INCUMBENT_RETRY_SECS)
                alive = self._host_info(*incumbent) is not None
        if incumbent is not None and alive:
            # Two live VRChat clients: the first keeps the slot. A restarted client is the
            # other case -- each launch advertises a new service name, and a killed one
            # sends no mDNS goodbye -- so a VRChat rival takes the slot from an incumbent
            # whose OSCQuery no longer answers. Without that the bridge stays on the dead
            # client: no target selection fires, so nothing keyed on one (persistence's
            # clear, the client-log binding) learns of the restart. The probes block
            # zeroconf's dispatch thread for at most two _host_info timeouts and the retry
            # wait, as an incumbent's refresh already blocks it below.
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
        if self.log:
            # Says "OSC", not "VRChat": ranking fills an empty slot with the best peer on
            # offer, and until a VRChat client is discovered that is whatever else advertises
            # -- VRCFaceTracking and VRCOSC both do. Asserting VRChat here made the one log
            # line that could explain a stranger holding the slot claim the opposite.
            self.log.info("OSC target set to %s:%d (via %s)%s", host, osc_port, name,
                          "" if rank == _RANK_VRCHAT else
                          "; this peer does not identify itself as VRChat, so sends and "
                          "OSCQuery reads go to it until a VRChat client is discovered")
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
    def current_target(self) -> Optional[tuple[str, int]]:
        """The `(host, port)` sends go to, pinned or discovered, or None before one exists."""
        with self._client_lock:
            return self._client_target

    @property
    def current_service_name(self) -> Optional[str]:
        """The mDNS service name of the peer we read from, or None (nothing found yet).

        Discovered, it is the send target's; pinned, the VRChat client advertising the
        pinned port, which a client that advertises nothing never has.
        """
        with self._client_lock:
            return self._current_service_name

    @property
    def target_is_pinned(self) -> bool:
        """True when the send target was named rather than discovered.

        Exists so a caller can tell the three states behind a missing OSCQuery peer apart:
        a pin no advertising VRChat client matches (the Av3Emulator serves no tree and never
        will), discovery that has not resolved yet (normal for the first seconds of any run),
        and a peer that went away. They need different messages, and only the first has
        `pinned_manifest_id` as its answer.
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
        res = self._fetch_json(address, timeout)
        if not res.ok:
            return res
        node = res.value
        if "VALUE" not in node:
            return replace(res, reason=FETCH_MALFORMED, value=None,
                           detail=f"{res.detail} -> no VALUE attribute")
        value = node["VALUE"]
        # OSCQuery types VALUE as an array -- one entry per type tag -- and VRChat's
        # parameter nodes carry exactly one. Unwrap a single-element list so callers
        # compare against a scalar; leave anything else alone rather than guessing, since
        # a multi-tag node is not a parameter and the caller should see that it isn't.
        if isinstance(value, list) and len(value) == 1:
            value = value[0]
        return replace(res, value=value, detail="")

    def fetch_tree(self, address: str, timeout: float = 2.0) -> FetchResult:
        """Read every parameter at or under `address`, as `{full_path: value}`, in one GET.

        For a consumer that owns a namespace and needs the worn avatar's whole state under
        it -- `osc_persist` reconciling what the datagram stream never delivered. It is not
        discovery: the caller names the subtree, and docs/design.md §Settled decisions draws
        the line there. Each value is typed from the node's OSCQuery TYPE tag (`T`/`F` bool,
        `i` int, `f` float), because the JSON number alone cannot tell a whole-number float
        from an int and the two are not interchangeable on the wire. A node of any other
        type is left out. Same outcomes and thread rules as fetch().
        """
        res = self._fetch_json(address, timeout)
        if not res.ok:
            return res
        out: Dict[str, Any] = {}
        stack = [res.value]
        while stack:
            node = stack.pop()
            if not isinstance(node, dict):
                continue
            value, kind, path = node.get("VALUE"), node.get("TYPE"), node.get("FULL_PATH")
            if isinstance(value, list) and len(value) == 1 and isinstance(path, str):
                cast = _OSCQUERY_TYPES.get(kind)
                if cast is not None:
                    try:
                        out[path] = cast(value[0])
                    except (TypeError, ValueError):
                        pass
            contents = node.get("CONTENTS")
            if isinstance(contents, dict):
                stack.extend(contents.values())
        return replace(res, value=out, detail="")

    def _fetch_json(self, address: str, timeout: float) -> FetchResult:
        """One GET of `address` on the peer's OSCQuery server; FETCH_OK carries the JSON
        node as `value` and the URL as `detail`. fetch() and fetch_tree() read it."""
        with self._client_lock:
            # `endpoint`, not `peer`: this is where to ask. Who is answering is `identity`,
            # and both appear below.
            endpoint = self._peer_http
            lost = self._peer_lost
            # Read here, with the endpoint, and carried on the result rather than offered as
            # a property to ask afterwards. The lock is dropped for the whole GET below, and
            # a rank-3 VRChat displacing a rank-1 stranger mid-read is exactly the transient
            # this identity exists to describe -- so a caller asking after the fact could be
            # told about a peer that did not answer it.
            identity = (PeerIdentity(self._current_service_name,
                                     self._current_rank == _RANK_VRCHAT)
                        if endpoint is not None and self._current_service_name is not None
                        else None)

        if endpoint is None:
            # These three name nobody, and `identity` is None here by construction: there is
            # no endpoint to have asked. They build a FetchResult directly, which is why
            # `_result` is defined below them rather than above -- its promise is about the
            # returns that follow it, and a helper whose scope overshot its docstring would
            # be the same trap it exists to close.
            if lost:
                # Separated from "never discovered" because the remedies differ: this one
                # needs the client to come back, and no amount of waiting on discovery to
                # finish will help. target_is_pinned's docstring promises a caller can tell
                # these three states apart; without this it could tell two.
                return FetchResult(
                    FETCH_PEER_GONE,
                    detail="the OSCQuery peer we were reading from withdrew its service")
            if self.target_is_pinned:
                return FetchResult(
                    FETCH_NO_PEER,
                    detail="the send target was pinned, and no VRChat client advertising "
                           "its OSC port has been discovered")
            return FetchResult(FETCH_NO_PEER,
                               detail="no OSCQuery peer has been discovered yet")

        def _result(reason, **kw) -> FetchResult:
            """Every outcome from here on names the target it queried, without each site
            remembering to.

            A branch added later that forgot `peer=` would silently restore the defect this
            field was added to fix, and no test would go red.
            """
            return FetchResult(reason, peer=identity, **kw)

        import urllib.error
        import urllib.parse
        import urllib.request
        host, http_port = endpoint
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
                return _result(FETCH_NOT_FOUND, detail=f"{url} -> 404")
            return _result(FETCH_TRANSPORT, detail=f"{url} -> HTTP {e.code}")
        except Exception as e:
            return _result(FETCH_TRANSPORT, detail=f"{url} -> {type(e).__name__}: {e}")

        try:
            node = json.loads(body)
        except Exception as e:
            return _result(FETCH_MALFORMED, detail=f"{url} -> not JSON: {e}")
        if not isinstance(node, dict):
            return _result(FETCH_MALFORMED, detail=f"{url} -> not a JSON node")
        return _result(FETCH_OK, value=node, detail=url)

    @staticmethod
    def _host_info(host: str, http_port: int):
        import urllib.request
        url = f"http://[{host}]:{http_port}/?HOST_INFO" if ":" in host else f"http://{host}:{http_port}/?HOST_INFO"
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None
