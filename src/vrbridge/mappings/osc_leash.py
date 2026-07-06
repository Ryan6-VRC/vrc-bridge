"""
OSCLeash mapping: translates VRChat Physbone and contact parameters from an 
avatar's "leash" into player movement OSC inputs.

This mapping ports the core movement logic from the OSCLeash project into
the vrbridge architecture. It listens for avatar parameters indicating a leash's
stretch and direction, and converts them into `/input/Vertical`, `/input/Horizontal`,
and `/input/Run` messages.

Features:
- Master enable/disable switch via OSC.
- Run/Walk deadzones based on leash stretch amount.
- Up/Down angle deadzone and movement compensation.
- Optional OSC proxy for the `/input/Jump` command.
"""

from __future__ import annotations

import time

from vrbridge.mappings.mapping_base import Mapping
from vrbridge import VRBridge
from vrbridge.utils import ParamState, clamp, press_pulse

# ------------------------------ Config ------------------------------------

# --- OSC Parameter Names ---
# The base name of the leash parameter group.
LEASH_BONENAME = "Leash"

# Master switch to enable or disable the entire mapping.
ENABLED_ADDR = f"/avatar/parameters/Leash/Enabled"

# Optional proxy to trigger a jump on a rising edge (False -> True).
JUMP_PROXY_ADDR = "/avatar/parameters/Leash/Jump"

# --- Movement Tuning ---
# How much to multiply the calculated movement vector.
STRENGTH_MULTIPLIER = 1.25

# The minimum leash stretch percentage (0.0 to 1.0) to trigger walking and running.
WALK_THRESHOLD = 0.15
RUN_THRESHOLD = 0.70

# --- Y-Axis (Up/Down) Tuning ---
# How much to compensate for the loss of speed that naturally occurs when you pull
# the leash at a vertical angle instead of straight forward.
UP_DOWN_COMPENSATION = 0.8

# If the leash is pulled up or down beyond this angle (represented as a
# combined Y-contact value from 0.0 to 1.0), movement will stop completely.
UP_DOWN_DEADZONE = 0.6

# --- OSC Input Addresses (from VRChat) ---
GRABBED_ADDR = f"/avatar/parameters/{LEASH_BONENAME}_IsGrabbed"
STRETCH_ADDR = f"/avatar/parameters/{LEASH_BONENAME}_Stretch"
Z_POS_ADDR = f"/avatar/parameters/Leash/Z+"  # Forward
Z_NEG_ADDR = f"/avatar/parameters/Leash/Z-"  # Backward
X_POS_ADDR = f"/avatar/parameters/Leash/X+"  # Right
X_NEG_ADDR = f"/avatar/parameters/Leash/X-"  # Left
Y_POS_ADDR = f"/avatar/parameters/Leash/Y+"  # Up
Y_NEG_ADDR = f"/avatar/parameters/Leash/Y-"  # Down

# --- OSC Output Addresses (to VRChat) ---
VERTICAL_INPUT_ADDR = "/input/Vertical"
HORIZONTAL_INPUT_ADDR = "/input/Horizontal"
RUN_INPUT_ADDR = "/input/Run"
JUMP_INPUT_ADDR = "/input/Jump"

# Cooldown period (in seconds) to prevent jump spam.
JUMP_COOLDOWN_SECS: float = 1.0
JUMP_PULSE_DURATION = 1.0 / 30  # Seconds

# ----------------------------- Mapping ------------------------------------

class LeashMapping(Mapping):
    name = "osc_leash"

    def __init__(self, bridge: VRBridge):
        super().__init__(bridge)

        # State trackers for incoming OSC parameters
        self.enabled_state = ParamState(ENABLED_ADDR, default=1, bridge=bridge)
        self.grabbed_state = ParamState(GRABBED_ADDR, default=0, bridge=bridge)
        self.stretch_state = ParamState(STRETCH_ADDR, default=0.0, bridge=bridge)
        
        # Directional States (6 axes)
        self.z_pos = ParamState(Z_POS_ADDR, bridge=bridge)
        self.z_neg = ParamState(Z_NEG_ADDR, bridge=bridge)
        self.x_pos = ParamState(X_POS_ADDR, bridge=bridge)
        self.x_neg = ParamState(X_NEG_ADDR, bridge=bridge)
        self.y_pos = ParamState(Y_POS_ADDR, bridge=bridge)
        self.y_neg = ParamState(Y_NEG_ADDR, bridge=bridge)

        # Helper list for bulk operations (like resets)
        self._physics_params = [
            self.grabbed_state, self.stretch_state,
            self.z_pos, self.z_neg, 
            self.x_pos, self.x_neg, 
            self.y_pos, self.y_neg
        ]

        # Internal state to manage transitions
        self.is_active = False
        self._was_active = False
        self._last_jump_state = False
        self._last_jump_timestamp: float = 0.0

    def register(self) -> None:
        """
        Register OSC listeners.
        """
        super().register()
        # Add a listener to the master enable switch for immediate response.
        self.bridge.on_osc(ENABLED_ADDR, self._on_enable_change)
        
        # Register a separate callback for the jump proxy feature.
        self.bridge.on_osc(JUMP_PROXY_ADDR, self._gate(self._on_jump_proxy_change))

        # Listen for avatar changes to reset the state cache
        self.bridge.on_osc("/avatar/change", self._on_avatar_change)

    def _on_avatar_change(self, ctx, address: str, value: str):
        """
        Reset all local state when switching avatars.
        """
        self.bridge.log.info(f"LeashMapping: Avatar changed to {value}. Resetting physics state.")
        for param in self._physics_params:
            param.reset(send=False)

    def _on_enable_change(self, ctx, address, value: bool | int) -> None:
        """
        When the mapping is disabled, send a stop command immediately.
        """
        if not value:
            self._send_movement(ctx, 0.0, 0.0, 0)
            self._was_active = False

    def _on_jump_proxy_change(self, ctx, address, value: bool | int) -> None:
        """
        Handles the jump proxy logic. Triggers a jump on the rising edge
        (False -> True) if enabled.
        """
        if not self.is_active:
            return
        
        new_state = bool(value)
        # Rising edge detection
        if new_state and not self._last_jump_state:
            now = time.time()
            if (now - self._last_jump_timestamp) >= JUMP_COOLDOWN_SECS:
                press_pulse(ctx, JUMP_INPUT_ADDR, 1, JUMP_PULSE_DURATION)
                self._last_jump_timestamp = now
        
        self._last_jump_state = new_state

    def update(self, now: float) -> None:
        """
        Called periodically by the mapping router to update movement state.
        """
        ctx = self.bridge.osc

        # --- Check Activation ---
        is_enabled = bool(self.enabled_state.get())
        is_grabbed = bool(self.grabbed_state.get())
        stretch = self.stretch_state.get() # ParamState defaults ensure float return

        # Determine if we should be actively calculating movement.
        # Check self.enabled (Mapping enabled) AND is_enabled (OSC toggle)
        self.is_active = self.enabled and is_enabled and is_grabbed and (stretch > WALK_THRESHOLD)

        # --- Inactive Logic ---
        if not self.is_active:
            # Falling edge: If we were active last frame, send one STOP packet.
            if self._was_active:
                self._send_movement(ctx, 0.0, 0.0, 0)
            
            self._was_active = False
            self._last_jump_state = False
            return

        # --- Active Movement Calculation ---
        self._was_active = True

        # Calculate raw directional vectors (Positive - Negative)
        vertical_raw = self._get_signed_axis(self.z_pos, self.z_neg)
        horizontal_raw = self._get_signed_axis(self.x_pos, self.x_neg)
        
        # Calculate Y-axis influence for deadzone and compensation
        y_raw_sum = self.y_pos.get() + self.y_neg.get()

        # Deadzone: If pulled too far vertically, stop movement
        if y_raw_sum >= UP_DOWN_DEADZONE:
            self._send_movement(ctx, 0.0, 0.0, 0)
            return

        # Apply multiplier logic
        output_multiplier = stretch * STRENGTH_MULTIPLIER
        vertical_output = vertical_raw * output_multiplier
        horizontal_output = horizontal_raw * output_multiplier

        # Apply Y-axis compensation (slow down if pulling upwards/downwards)
        if UP_DOWN_COMPENSATION > 0:
            y_modifier = clamp(1.0 - (y_raw_sum * UP_DOWN_COMPENSATION), 0.01, 1.0)
            vertical_output /= y_modifier
            horizontal_output /= y_modifier

        # Final clamping and sending
        run_type = 1 if stretch > RUN_THRESHOLD else 0
        self._send_movement(ctx, clamp(vertical_output), clamp(horizontal_output), run_type)

    def _get_signed_axis(self, pos_state: ParamState, neg_state: ParamState) -> float:
        """Helper to get the net value of an axis pair (Positive - Negative)."""
        return pos_state.get() - neg_state.get()

    def _send_movement(self, ctx, v: float, h: float, r: int) -> None:
        """Wrapper to send all three movement OSC messages to VRChat."""
        ctx.send(VERTICAL_INPUT_ADDR, v)
        ctx.send(HORIZONTAL_INPUT_ADDR, h)
        ctx.send(RUN_INPUT_ADDR, r)
