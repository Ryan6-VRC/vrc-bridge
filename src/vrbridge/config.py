"""
SteamVR file helpers for vrbridge: the action manifest, the default controller
bindings, and the .vrmanifest that registers our background app identity.

The trackpad and press tunables that used to live here are now
`settings.ControllerSettings`, so they can be retuned without editing installed
source. The JSON below is *not* tunable: it is a hardware contract discovered
against SteamVR's binding UI, and every path and action name in it is load-bearing.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .settings import app_base_dir

# SteamVR application identity
APP_KEY:  str = "com.vrbridge.input"
APP_NAME: str = "VRBridge Controller Input"


# Where to write the SteamVR files (manifest + bindings + actions).
# Override with VRBRIDGE_FILES_DIR (absolute path).
def get_files_dir() -> str:
    env = os.environ.get("VRBRIDGE_FILES_DIR")
    if env:
        return str(Path(env).expanduser().resolve())
    return str((app_base_dir() / "steamvr_files").resolve())

@dataclass(frozen=True)
class SteamVRFiles:
    actions: str
    bindings_knuckles: str
    bindings_oculus: str
    vrmanifest: str

def ensure_steamvr_files(*, files_dir: str | None = None) -> SteamVRFiles:
    """
    Ensure SteamVR JSON files exist and return their paths.

    - actions.json          : Declares input actions.
    - bindings_*.json       : Default bindings for Knuckles and Oculus Touch.
    - vrbridge_input.vrmanifest : Registers our background app identity.

    If `files_dir` is None, uses get_files_dir().
    """
    d = Path(files_dir) if files_dir is not None else Path(get_files_dir())
    d.mkdir(parents=True, exist_ok=True)

    actions = d / "actions.json"
    bind_kn = d / "bindings_knuckles.json"
    bind_oc = d / "bindings_oculus.json"
    vrman   = d / "vrbridge_input.vrmanifest"

    ACTIONS_JSON = {
        "default_bindings": [
            {"controller_type": "knuckles",     "binding_url": "bindings_knuckles.json"},
            {"controller_type": "oculus_touch", "binding_url": "bindings_oculus.json"},
        ],
        "actions": [
            {"name": "/actions/main/in/track_click", "type": "boolean", "requirement": "suggested"},
            {"name": "/actions/main/in/track_touch", "type": "boolean", "requirement": "optional"},
            {"name": "/actions/main/in/track_pos",   "type": "vector2", "requirement": "suggested"},
            {"name": "/actions/main/in/joy_click",   "type": "boolean", "requirement": "suggested"},
            {"name": "/actions/main/in/joy_pos",     "type": "vector2", "requirement": "optional"},
        ],
        "action_sets": [ { "name": "/actions/main", "usage": "leftright" } ],
        "localization": [{
            "language_tag": "en_us",
            "/actions/main": "Main",
            "/actions/main/in/track_click": "Trackpad click",
            "/actions/main/in/track_touch": "Trackpad touch",
            "/actions/main/in/track_pos":   "Trackpad position",
            "/actions/main/in/joy_click":   "Thumbstick click",
            "/actions/main/in/joy_pos":     "Thumbstick position",
        }],
    }
    if not actions.exists():
        with actions.open("w", encoding="utf-8") as f:
            json.dump(ACTIONS_JSON, f, indent=2)

    BIND_KNU = {
        "action_manifest_version": 0,
        "controller_type": "knuckles",
        "name": "Default (VRBridge)",
        "bindings": {
            "/actions/main": {
                "sources": [
                    {
                        "path": "/user/hand/left/input/trackpad",
                        "mode": "trackpad",
                        "inputs": {
                            "position": {"output": "/actions/main/in/track_pos"},
                            "click":    {"output": "/actions/main/in/track_click"},
                            "touch":    {"output": "/actions/main/in/track_touch"},
                        },
                    },
                    {
                        "path": "/user/hand/right/input/trackpad",
                        "mode": "trackpad",
                        "inputs": {
                            "position": {"output": "/actions/main/in/track_pos"},
                            "click":    {"output": "/actions/main/in/track_click"},
                            "touch":    {"output": "/actions/main/in/track_touch"},
                        },
                    },
                    {
                        "path": "/user/hand/left/input/thumbstick",
                        "mode": "joystick",
                        "inputs": {
                            "position": {"output": "/actions/main/in/joy_pos"},
                            "click":    {"output": "/actions/main/in/joy_click"},
                        },
                    },
                    {
                        "path": "/user/hand/right/input/thumbstick",
                        "mode": "joystick",
                        "inputs": {
                            "position": {"output": "/actions/main/in/joy_pos"},
                            "click":    {"output": "/actions/main/in/joy_click"},
                        },
                    },
                ]
            }
        }
    }
    if not bind_kn.exists():
        with bind_kn.open("w", encoding="utf-8") as f:
            json.dump(BIND_KNU, f, indent=2)

    BIND_OCU = {
        "action_manifest_version": 0,
        "controller_type": "oculus_touch",
        "name": "Default (VRBridge)",
        "bindings": {
            "/actions/main": {
                "sources": [
                    {
                        "path": "/user/hand/left/input/joystick",
                        "mode": "joystick",
                        "inputs": {
                            "position": {"output": "/actions/main/in/joy_pos"},
                            "click":    {"output": "/actions/main/in/joy_click"},
                        },
                    },
                    {
                        "path": "/user/hand/right/input/joystick",
                        "mode": "joystick",
                        "inputs": {
                            "position": {"output": "/actions/main/in/joy_pos"},
                            "click":    {"output": "/actions/main/in/joy_click"},
                        },
                    },
                ]
            }
        }
    }
    if not bind_oc.exists():
        with bind_oc.open("w", encoding="utf-8") as f:
            json.dump(BIND_OCU, f, indent=2)

    # vrmanifest is small; rewrite every run so it always points at the current interpreter.
    # Launch the CLI module, not a file path: the only module that ever passed itself
    # here was controller_manager, which has no __main__, so a SteamVR auto-launch
    # started the interpreter and exited. "-m vrbridge.cli" resolves for both an
    # editable checkout and an installed package, and needs no quoting.
    vrman_json = {
        "source": "user",
        "applications": [{
            "app_key": APP_KEY,
            "launch_type": "binary",
            "binary_path_windows": sys.executable,
            "arguments": "-m vrbridge.cli",
            "working_directory": str(d.resolve()),
            "is_background_process": True,
            "strings": {"en_us": {"name": APP_NAME}},
            "action_manifest_path": str(actions.resolve()),
        }]
    }
    with vrman.open("w", encoding="utf-8") as f:
        json.dump(vrman_json, f, indent=2)

    return SteamVRFiles(actions=str(actions),
                        bindings_knuckles=str(bind_kn),
                        bindings_oculus=str(bind_oc),
                        vrmanifest=str(vrman))
