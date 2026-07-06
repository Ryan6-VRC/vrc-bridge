from __future__ import annotations

import time

from vrbridge.mappings.mapping_base import Mapping
from vrbridge import VRBridge

# ------------------------------ Config ------------------------------------

# The substring to search for in discovered OSCQuery service names.
VRCFT_SERVICE_NAME: str = "VRCFT"

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

# How long to wait after an avatar change before sending parameters.
# This gives the avatar time to fully load and initialize its parameters.
AVATAR_LOAD_DELAY_SECS: float = 1.0

# ----------------------------- Mapping ------------------------------------

class VRCFTMapping(Mapping):
    """
    Detects if VRChat Face Tracking (VRCFT) is running and sets avatar
    parameters accordingly after an avatar change.
    """
    name = "osc_vrcft"

    def register(self) -> None:
        """Register a callback for avatar changes."""
        super().register()
        self.bridge.on_osc("/avatar/change", self._gate(self._on_avatar_change))

    def _on_avatar_change(self, ctx, address: str, avatar_id: str):
        """
        Called on avatar change. Waits a bit, then checks for VRCFT
        and sends the corresponding parameter set.
        """
        # This callback can block; it only runs once per avatar change.
        time.sleep(AVATAR_LOAD_DELAY_SECS)

        # Use the new OSCManager method to check for the service.
        is_vrcft_running = self.bridge.osc.is_service_running(VRCFT_SERVICE_NAME)

        params_to_set = ACTIVE_PARAMS if is_vrcft_running else INACTIVE_PARAMS

        if is_vrcft_running:
            self.bridge.log.info("VRCFT detected. Activating face tracking parameters.")
        else:
            self.bridge.log.info("VRCFT not detected. Deactivating face tracking parameters.")

        for addr, val in params_to_set.items():
            ctx.send(addr, val)
