"""The mDNS advertisement is pinned to the interface we actually serve on.

A bare Zeroconf() means InterfaceChoice.All: one announce socket per interface
that comes up. Measured on a host with loopback, a Hyper-V switch, Ethernet and
Tailscale up, that is 4 sockets carrying 4 copies of the same loopback-only
address record -- and a client opening one UDP sender per announcement is the
leading explanation for the doubled inbound in docs/design.md.

Intent, not observation: we announce on exactly the interface we serve, because
announcing a loopback address to the LAN advertises an endpoint no LAN peer can
reach. Asserted on the constructor argument rather than on
len(zeroconf.engine.senders), because the socket count is 1 on a
single-interface host either way -- a machine-shaped assertion would pass
vacuously in CI and prove nothing about the code.
"""
import pytest
from zeroconf import Zeroconf

import vrbridge.osc_manager as om
from vrbridge.osc_manager import OSCManager


def _recording_zeroconf(monkeypatch):
    """Record the `interfaces=` of every Zeroconf built, in construction order."""
    seen = []

    class RecordingZeroconf(Zeroconf):
        def __init__(self, *args, **kwargs):
            seen.append(kwargs.get("interfaces", "<default: InterfaceChoice.All>"))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(om, "Zeroconf", RecordingZeroconf)
    return seen


def test_announcement_is_pinned_to_the_served_interface(monkeypatch):
    seen = _recording_zeroconf(monkeypatch)

    mgr = OSCManager(advertise=False, discover=False)  # no register_service: costs ~1.7s
    mgr.start()
    try:
        assert seen == [[mgr.host]]
    finally:
        mgr.stop()


@pytest.mark.real_mdns
def test_the_browser_is_not_pinned_to_the_served_interface(monkeypatch):
    """Intended: browsing gets its own Zeroconf, on all interfaces.

    A Zeroconf pinned to the loopback interface receives no multicast, so sharing the
    announcement's instance made discovery impossible on any host. Measured with VRChat
    running: pinned to 127.0.0.1 saw nothing over 30 s, while InterfaceChoice.All saw
    VRChat-Client-<id>._oscjson._tcp, whose /?HOST_INFO then answered 200. That is what made
    the client look like it had never advertised OSCQuery.

    Asserted on the constructor arguments, so it stays honest on a single-interface CI host
    where a socket count would pass vacuously. `Zeroconf` is patched out above, so despite the
    real_mdns marker nothing here touches the network.
    """
    seen = _recording_zeroconf(monkeypatch)

    mgr = OSCManager(advertise=False, discover=True)
    mgr.start()
    try:
        announce, browse = seen
        assert announce == [mgr.host], "the announcement must stay pinned"
        assert browse == "<default: InterfaceChoice.All>", "a pinned browser is a deaf browser"
    finally:
        mgr.stop()


def test_discovery_off_builds_no_browser(monkeypatch):
    """Intended: `discover=False` is what keeps the suite off a developer's live VRChat."""
    seen = _recording_zeroconf(monkeypatch)

    mgr = OSCManager(advertise=False, discover=False)
    mgr.start()
    try:
        assert len(seen) == 1, "discovery was off, so only the announcement instance is built"
        assert mgr._browser is None
    finally:
        mgr.stop()


def test_the_advertised_address_record_follows_the_served_host(monkeypatch):
    """Intended: one host setting drives the bind, the announcement and the record.

    The address was a hardcoded inet_aton("127.0.0.1") while the HTTP and OSC
    servers bound self.host, so a non-loopback host advertised an address it was
    not serving on.

    A host of 127.0.0.2 rather than the 127.0.0.1 default is what makes this
    non-vacuous -- against the old hardcoded literal the two coincide and the
    assertion passes without testing anything. It stays inside loopback, so no
    test traffic leaves the machine.
    """
    import socket

    captured = {}
    real_service_info = om.ServiceInfo

    def recording_service_info(*args, **kwargs):
        captured.update(kwargs)
        return real_service_info(*args, **kwargs)

    class StubZeroconf:
        """Stands in for the whole mDNS stack: this test is about what we *build*."""

        def __init__(self, *a, **kw):
            pass

        def register_service(self, info, **kw):
            pass

        def unregister_service(self, info, **kw):
            pass

        def close(self):
            pass

    monkeypatch.setattr(om, "ServiceInfo", recording_service_info)
    monkeypatch.setattr(om, "Zeroconf", StubZeroconf)
    monkeypatch.setattr(om, "ServiceBrowser", lambda *a, **kw: None)
    # HTTPServer.server_bind calls socket.getfqdn(host), and a reverse lookup on
    # a loopback alias nobody has a PTR for blocks ~5s. The product only ever
    # binds 127.0.0.1, so this cost is the fixture's, not the code's.
    monkeypatch.setattr(socket, "getfqdn", lambda name="": name)

    mgr = OSCManager(host="127.0.0.2", advertise=True)
    mgr.start()
    try:
        assert captured["addresses"] == [socket.inet_aton("127.0.0.2")]
        assert captured["port"] == mgr.http_port
    finally:
        mgr.stop()
