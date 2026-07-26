from __future__ import annotations

import time

from vrbridge.mappings.mapping_base import Mapping
from vrbridge import VRBridge
from vrbridge.settings import settings

# ------------------------------ Config ------------------------------------

# Avatar parameters to set when VRCFT is detected.
# VRCFT may also control these, but setting them helps with avatar logic.
ACTIVE_PARAMS: dict[str, int] = {
    "/avatar/parameters/LipTrackingActive": 1,
    "/avatar/parameters/EyeTrackingActive": 1,
}

# Avatar parameters to set when VRCFT is NOT detected.
INACTIVE_PARAMS: dict[str, int] = {
    "/avatar/parameters/LipTrackingActive": 0,
    "/avatar/parameters/EyeTrackingActive": 0,
}

# The mDNS service-name substring and the post-avatar-change delay are
# settings.VRCFTSettings.service_name / .avatar_load_delay_secs.

# ----------------------------- Mapping ------------------------------------

class VRCFTMapping(Mapping):
    """
    Detects if VRChat Face Tracking (VRCFT) is running and sets avatar
    parameters accordingly after an avatar change.
    """
    name = "osc_vrcft"

    def __init__(self, bridge: VRBridge, tuning=None):
        super().__init__(bridge)
        self._tune = tuning if tuning is not None else settings().vrcft

    def _attach(self) -> None:
        """Register a callback for avatar changes."""
        self.bridge.on_osc("/avatar/change", self._gate(self._on_avatar_change))

    def _on_avatar_change(self, ctx, address: str, avatar_id: str):
        """
        Called on avatar change. Waits a bit, then checks for VRCFT
        and sends the corresponding parameter set.
        """
        # This callback can block; it only runs once per avatar change.
        time.sleep(self._tune.avatar_load_delay_secs)

        # Use the new OSCManager method to check for the service.
        is_vrcft_running = self.bridge.osc.is_service_running(self._tune.service_name)

        params_to_set = ACTIVE_PARAMS if is_vrcft_running else INACTIVE_PARAMS

        if is_vrcft_running:
            self.bridge.log.info("VRCFT detected. Activating face tracking parameters.")
        else:
            self.bridge.log.info("VRCFT not detected. Deactivating face tracking parameters.")

        for addr, val in params_to_set.items():
            ctx.send(addr, val)
