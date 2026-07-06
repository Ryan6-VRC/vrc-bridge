"""
MuteProxy mapping: /avatar/parameters/GestureControl/MuteProxy -> pulse /input/Voice
"""

from __future__ import annotations

from vrbridge.mappings.mapping_base import Mapping
from vrbridge import VRBridge
from vrbridge.utils import press_pulse

# ------------------------------ Config ------------------------------------

# --- OSC Parameter Names ---
MUTE_PROXY_ADDR  = "/avatar/parameters/GestureControl/MuteProxy"
VOICE_INPUT_ADDR = "/input/Voice"
PRESS_DURATION   = 1.0 / 30  # Seconds

# ----------------------------- Mapping ------------------------------------

class MuteProxyMapping(Mapping):
    """Always-on friendly: handler stays registered; gated by self.enabled."""
    name = "index_muteproxy"

    def __init__(self, bridge: VRBridge,
                 *, mute_addr: str = MUTE_PROXY_ADDR,
                 voice_addr: str = VOICE_INPUT_ADDR,
                 duration: float = PRESS_DURATION):
        super().__init__(bridge)
        self.mute_addr = str(mute_addr)
        self.voice_addr = str(voice_addr)
        self.duration = float(duration)
        self._handler = None

    def register(self) -> None:
        """Attach OSC callback once, like other mappings."""
        super().register()

        def _on_mute_proxy_change(ctx, address, value):
            press_pulse(ctx, self.voice_addr, 1, self.duration)

        self.bridge.on_osc(self.mute_addr, self._gate(_on_mute_proxy_change))

    def update(self, now: float) -> None:
        return
