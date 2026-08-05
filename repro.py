import sys, time
sys.path.insert(0, "src"); sys.path.insert(0, ".")
from tests.fake_vrchat import FakeVRChat
from vrbridge.engine import VRBridge
from vrbridge.mappings.osc_wardrobe import WardrobeMapping, SLOT_ADDR, MARKER_ADDR, AVATAR_CHANGE_ADDR
from vrbridge.wardrobe import Manifest, Slot
from vrbridge.settings import WardrobeSettings
from pythonosc import udp_client

A="avtr_aaaaaaaa-0000-0000-0000-000000000001"
B="avtr_bbbbbbbb-0000-0000-0000-000000000002"
C="avtr_cccccccc-0000-0000-0000-000000000003"
D="avtr_dddddddd-0000-0000-0000-000000000004"
# manifest 7 = avatar A's wardrobe; manifest 9 = avatar B's wardrobe
m7 = Manifest(id=7, slots={1: Slot(1, B, "goto-B"), 2: Slot(2, C, "goto-C")}, source="m7")
m9 = Manifest(id=9, slots={1: Slot(1, D, "B-slot1")}, source="m9")

with FakeVRChat() as vrc:
    br = VRBridge(enable_steamvr=False, advertise=False)
    br.osc.start()
    br.osc._client = udp_client.SimpleUDPClient("127.0.0.1", vrc.osc_port)
    br.osc._client_target = ("127.0.0.1", vrc.osc_port)
    br.osc._peer_http = ("127.0.0.1", vrc.http_port)
    m = WardrobeMapping(br, {7: m7, 9: m9}, tuning=WardrobeSettings(fetch_timeout_secs=1.0))
    m.register(); m.activate()

    def deliver(a, v): br.osc._update_cache_and_fire(a, v)

    # wearing avatar A, marker 7
    vrc.set_node(MARKER_ADDR, 7)
    print("--- t=0: press slot 1 on avatar A -> should send B")
    deliver(SLOT_ADDR, 1); deliver(SLOT_ADDR, 0)
    time.sleep(0.2)
    deliver(AVATAR_CHANGE_ADDR, B)          # the 5ms acknowledgement echo
    print("   active after echo:", None if m._active is None else m._active.id)

    print("--- t=2s: cold download; A still worn+emitting. Wearer retries slot 1")
    time.sleep(0.2)
    deliver(SLOT_ADDR, 1); deliver(SLOT_ADDR, 0)
    time.sleep(0.2)
    deliver(AVATAR_CHANGE_ADDR, B)          # same id -> change filter?
    print("   active after 2nd echo:", None if m._active is None else m._active.id)

    print("--- t=45s: avatar B finishes loading. No /avatar/change is emitted (measured).")
    vrc.set_node(MARKER_ADDR, 9)            # B's marker is now what the tree serves
    print("--- press slot 1 on avatar B -> SHOULD send D (manifest 9)")
    deliver(SLOT_ADDR, 1); deliver(SLOT_ADDR, 0)
    time.sleep(0.3)
    print("   active:", None if m._active is None else m._active.id)
    print("   sends:", vrc.values_for(AVATAR_CHANGE_ADDR))
    print("   marker HTTP reads:", vrc.node_gets)
    br.osc.stop()
