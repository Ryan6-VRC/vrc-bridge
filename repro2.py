import sys, time, threading
sys.path.insert(0, "src"); sys.path.insert(0, ".")
from tests.fake_vrchat import FakeVRChat
from vrbridge.engine import VRBridge
import vrbridge.mappings.osc_wardrobe as wd
wd.REPEAT_GUARD_SECS = 0.0          # == the PR as diffed: no repeat guard exists there
from vrbridge.wardrobe import Manifest, Slot
from vrbridge.settings import WardrobeSettings
from pythonosc import udp_client

B="avtr_bbbbbbbb-0000-0000-0000-000000000002"
m7 = Manifest(id=7, slots={1: Slot(1, B, "")}, source="m7")
with FakeVRChat() as vrc:
    br = VRBridge(enable_steamvr=False, advertise=False, log_level="WARNING")
    br.osc.start()
    br.osc._client = udp_client.SimpleUDPClient("127.0.0.1", vrc.osc_port)
    br.osc._client_target = ("127.0.0.1", vrc.osc_port)
    br.osc._peer_http = ("127.0.0.1", vrc.http_port)
    m = wd.WardrobeMapping(br, {7: m7}, tuning=WardrobeSettings(fetch_timeout_secs=1.0))
    m.register(); m.activate()
    vrc.set_node(wd.MARKER_ADDR, 7)
    # design.md: "every inbound message is delivered twice, from two UDP sender sockets"
    # -> two datagram threads, ~1 ms apart, carrying the identical payload.
    t1 = threading.Thread(target=br.osc._update_cache_and_fire, args=(wd.SLOT_ADDR, 1))
    t1.start(); time.sleep(0.001)
    t2 = threading.Thread(target=br.osc._update_cache_and_fire, args=(wd.SLOT_ADDR, 1))
    t2.start(); t1.join(); t2.join()
    time.sleep(0.4)
    print("ONE press -> /avatar/change sends:", vrc.values_for("/avatar/change"))
    br.osc.stop()
