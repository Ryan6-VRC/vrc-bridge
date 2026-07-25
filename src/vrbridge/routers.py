from __future__ import annotations

from typing import Optional

from vrbridge.mappings import MappingRouter
from vrbridge import VRBridge
from vrbridge.utils import ParamState

VIRTUALLENS_ENABLE_ADDR = "/avatar/parameters/VirtualLens2_Enable"
VRCL_FEATURE_TOGGLE_ADDR = "/avatar/parameters/VRCLFeatureToggle"
USERCAMERA_MODE_ADDR = "/usercamera/Mode"
AVATAR_CHANGE_ADDR = "/avatar/change"


class DefaultRouter(MappingRouter):
    """
    Switch between:
      - index_puppet       when /usercamera/Mode == 0
      - index_usercamera   when /usercamera/Mode != 0

    MuteProxy remains always-on for convenience.
    """

    def __init__(self, bridge: VRBridge):
        super().__init__(bridge)
        self.log = bridge.log

        # Mirror the camera mode param so we can query its last value at any time.
        # Default to 0.0 so the initial state is "puppet" until a non-zero update arrives.
        self._cam_mode = ParamState(USERCAMERA_MODE_ADDR, default=0.0, bridge=bridge)

        # Re-evaluate immediately whenever /usercamera/Mode changes.
        self.bridge.on_osc(USERCAMERA_MODE_ADDR, self._on_mode_change)

        # We manage exactly these two mappings.
        self._managed_names = {"index_puppet", "index_usercamera"}

        # Lazy imports to avoid circulars when running single-mapping scripts.
        from vrbridge.mappings import (IndexPuppetMapping, MuteProxyMapping,
                              UserCameraMapping, VRCFTMapping)

        # Register MuteProxy (always on)
        mute = MuteProxyMapping(bridge)
        self.register(mute)
        mute.activate()

        # Register VRCFTMapping (always on)
        vrcft = VRCFTMapping(bridge)
        self.register(vrcft)
        vrcft.activate()

        # Register the managed mappings (router will activate exactly one)
        self.register(IndexPuppetMapping(bridge))
        self.register(UserCameraMapping(bridge))

        # Track what we last enabled to avoid redundant (de)activations.
        self._current: Optional[str] = None

    # ---- events -----------------------------------------------------------

    def _on_mode_change(self, ctx, address: str, value):
        self.evaluate()

    # ---- selection logic --------------------------------------------------

    def _desired(self) -> str:
        """
        Decide which mapping should be active *right now* based only on /usercamera/Mode.
        Non-zero => index_usercamera; zero/None => index_puppet.
        """
        mode_val = self._cam_mode.get()
        # Treat None the same as 0 (default/unknown -> puppet)
        if (mode_val or 0) != 0:
            return "index_usercamera"
        return "index_puppet"

    def evaluate(self) -> None:
        desired = self._desired()
        if desired == self._current:
            return

        # Only touch our managed mappings; leave others (e.g., muteproxy) alone.
        managed_present = self._managed_names & set(self._mappings.keys())
        for name in managed_present:
            m = self._mappings[name]
            if name == desired:
                if not m.enabled:
                    m.activate()
            else:
                if m.enabled:
                    m.deactivate()

        if desired != self._current:
            self.log.info("DefaultRouter selected mapping: %s", desired)
            self._current = desired


class CameraPrefabRouter(MappingRouter):
    """
    Chooses and toggles mappings based on VRChat state.

    Selection policy (managed set):
      - index_puppet: default when neither VirtualLens nor VRCLens is active.
        (Does not latch; can be replaced any time.)
      - index_virtuallens: Enable if VirtualLens_Enable != 0; remain enabled
        until avatar changes (latching).
      - index_vrclens: Enable if VRCLFeatureToggle != 0; remain enabled
        until avatar changes (latching).

    Always on:
      - muteproxy: Convert MuteProxy changes to /input/voice presses.
    """
    def __init__(self, bridge: VRBridge):
        super().__init__(bridge)
        self.log = bridge.log

        # Track decisive VRChat parameters
        self.vl_enable = ParamState(VIRTUALLENS_ENABLE_ADDR, default=0.0, bridge=bridge)
        self.vrcl_toggle = ParamState(VRCL_FEATURE_TOGGLE_ADDR, default=0.0, bridge=bridge)

        # Observe param changes so we can re-evaluate immediately
        self.bridge.on_osc(VIRTUALLENS_ENABLE_ADDR, self._on_param_change)
        self.bridge.on_osc(VRCL_FEATURE_TOGGLE_ADDR, self._on_param_change)

        # Avatar changes
        self.bridge.on_osc(AVATAR_CHANGE_ADDR, self._on_avatar_change)

        # Latch for VL/VRCL until avatar changes
        self._latched: Optional[str] = None

        # Which mapping names are managed by this router (enable exactly one)
        self._managed_names = {"index_puppet", "index_virtuallens", "index_vrclens"}

        # ---- instantiate & register the default set here ----
        # (lazy imports to avoid any accidental circularity with standalone scripts)
        from vrbridge.mappings import (IndexPuppetMapping, MuteProxyMapping,
                              VirtualLensMapping, VRCLensMapping)

        # Register MuteProxy (always on)
        mute = MuteProxyMapping(bridge)
        self.register(mute)
        mute.activate()

        # Managed group
        self.register(IndexPuppetMapping(bridge))
        self.register(VirtualLensMapping(bridge))
        self.register(VRCLensMapping(bridge))

    # ---- events -----------------------------------------------------------

    def _on_param_change(self, ctx, address: str, value):
        # Re-evaluate mapping choice promptly
        self.evaluate()

    def _on_avatar_change(self, ctx, address: str, value):
        avatar_id = value
        self.log.info("Avatar changed: %s", avatar_id)

        # Reset latching and param mirrors
        self._latched = None
        self.vl_enable.reset(send=False)
        self.vrcl_toggle.reset(send=False)
        self.evaluate()

    # ---- selection logic --------------------------------------------------

    def _choose(self) -> str:
        """
        Return mapping name that should be enabled now (managed group only),
        respecting latching rules. If both toggles are non-zero (undefined),
        prefer VRCLens.
        """
        if self._latched:
            return self._latched

        vrcl = self.vrcl_toggle.get()
        vl = self.vl_enable.get()

        if (vrcl or 0) != 0:
            self._latched = "index_vrclens"
            return "index_vrclens"
        if (vl or 0) != 0:
            self._latched = "index_virtuallens"
            return "index_virtuallens"

        return "index_puppet"

    def evaluate(self) -> None:
        desired = self._choose()
        # Only touch managed mappings; leave others alone
        managed_present = self._managed_names & set(self._mappings.keys())
        for name in managed_present:
            m = self._mappings[name]
            if name == desired:
                if not m.enabled:
                    m.activate()
            else:
                if m.enabled:
                    m.deactivate()


class FullRouter(DefaultRouter):
    """
    Extends DefaultRouter by registering RemyMapping, which is enabled
    *only* when IndexPuppet is active.

    - When index_puppet is active: RemyMapping.activate()
      (touchpad press/release bound by _gate become live).
    - When index_usercamera is active: RemyMapping.deactivate()
      (touchpad press/release gated off).

    Note: RemyMapping thumbstick callbacks are registered without gating,
    so they remain active regardless of .enabled state.
    """
    def __init__(self, bridge: VRBridge):
        super().__init__(bridge)
        # Lazy import
        from vrbridge.mappings import RemyMapping

        # Register RemyMapping (managed in evaluate)
        self.remy_mapping = RemyMapping(bridge)
        self.register(self.remy_mapping)

    def evaluate(self) -> None:
        # Let the base router pick & toggle the primary managed mapping
        super().evaluate()

        # Mirror puppet activation onto RemyMapping for touchpad gating
        puppet = self._mappings.get("index_puppet")
        if not puppet:
            return

        if puppet.enabled:
            if not self.remy_mapping.enabled:
                self.remy_mapping.activate()
        else:
            if self.remy_mapping.enabled:
                self.remy_mapping.deactivate()
