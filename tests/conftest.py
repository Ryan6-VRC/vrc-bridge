"""Keep the suite off the real network.

`OSCManager` browses mDNS for a VRChat to talk to. That browse used to share the Zeroconf
instance pinned to the serve interface, which made it deaf -- so the suite's isolation from a
real client was an accident of that bug rather than anything a test asked for. Once the
browser got its own unpinned instance, a VRChat running on the developer's machine started
winning the target away from `FakeVRChat` mid-test: the run retargeted to 127.0.0.1:9000, read
the *live* avatar's wardrobe marker instead of the fixture's, and sent test datagrams at a real
client. Five tests failed and the cause was in none of them.

No test needs real discovery -- every target-selection test drives `_consider_service` directly
with a `ServiceInfo` it built -- so discovery is off by default here and opted into by name.

Mark a test `@pytest.mark.real_mdns` to keep the browser. Only do that with `Zeroconf` itself
monkeypatched, or the test reaches the LAN.
"""

import pytest

import vrbridge.osc_manager as om


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_mdns: let this test construct a real mDNS browser (patch Zeroconf yourself)")


@pytest.fixture(autouse=True)
def no_real_mdns_discovery(request, monkeypatch):
    if request.node.get_closest_marker("real_mdns"):
        return

    real_init = om.OSCManager.__init__

    def init(self, *args, **kwargs):
        # Forced, not defaulted: VRBridge passes discover= explicitly, so a setdefault here
        # would sail straight past every bridge-level test.
        kwargs["discover"] = False
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(om.OSCManager, "__init__", init)
