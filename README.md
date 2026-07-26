# vrc-bridge

> Developed in the [Atelier](https://github.com/Ryan6-VRC/atelier) workspace.

vrc-bridge connects SteamVR controller inputs and VRChat OSC parameters to enable control over avatar features, camera systems, and external tools that standard bindings can't reach. It runs as a background application, listening to both your controllers and VRChat at once.

It exposes a small interface for attaching callbacks to OSC addresses, caching the latest value seen for any watched address, and sending OSC values back to VRChat. The bundled mappings target SteamVR users on Valve Index (Knuckles) controllers, but thumbstick inputs from Oculus/Quest controllers via SteamVR are also supported.

## Features

- **Controller input processing** — touchpad and thumbstick events: short/long presses, stepped and raw scrolling, and touch position.
- **OSC integration** — reacts to VRChat OSC parameters (avatar toggles, physbone values) and sends OSC back to control movement, avatar parameters, and more.
- **Automatic discovery** — OSCQuery + mDNS find and connect to VRChat with no manual IP/port setup.
- **Modular mappings** — functionality is organized into "mappings": rule sets that translate inputs to outputs.
- **Mapping routers** — automatically switch the active mapping based on in-game state (camera enabled, avatar changed, lens prefab detected).

## Install

Requires Python 3.10+ and SteamVR.

```
git clone https://github.com/Ryan6-VRC/vrc-bridge
cd vrc-bridge
pip install -e .
```

## Run

Start SteamVR first, then launch the bridge:

```
vrbridge                  # default router
vrbridge --router camera  # camera-prefab router
vrbridge --help           # all options
```

Options include `--router {name}`, `--log-level`, `--log-callbacks`, and `--no-steamvr` (desktop mode without controller support). On first launch the SteamVR action manifest and default bindings are generated under `steamvr_files/` in a source checkout, or under your per-user data directory for an installed package. Set `VRBRIDGE_FILES_DIR` to put them somewhere else.

## Routers and mappings

A **router** decides which mapping is active at any moment.

| Router    | Behavior |
|-----------|----------|
| `default` | Switches between `IndexPuppet` and `UserCamera` by the VRChat camera state; `MuteProxy` and `VRCFT` stay on. |
| `camera`  | Switches between `IndexPuppet`, `VirtualLens2`, and `VRCLens` based on the lens system detected on the current avatar. |
| `remy`    | The `default` router plus the Remy AI integration (see below). |

**Core mappings**

- **Index Puppet** — two-axis avatar puppet control from absolute finger position on the touchpads; optional mirroring to both hands from a single controller.
- **User Camera** — full VRChat User Camera control (aperture, exposure, zoom, capture, modes).
- **VirtualLens2 / VRCLens** — dedicated control schemes for those camera prefabs; the `camera` router switches to them automatically when detected.
- **Mute Proxy** — toggles the VRChat microphone from a watched OSC parameter.
- **Remy AI integration** — triggers actions on an external AI service. Point it at your host with `VRBRIDGE_REMY_URL` (e.g. `http://192.168.1.100:8000`) and `VRBRIDGE_REMY_WATCH_DIR` for the screenshot folder.

## Interoperates with

vrc-bridge speaks to these projects over OSC. None of their code is vendored here — the parameter names their mappings drive are each project's own contract, and their documentation is the authority on them.

- [VirtualLens2](https://vlens2.logilabo.dev/) by ろじらぼ / logilabo — the camera prefab `index_virtuallens` drives ([BOOTH](https://logilabo.booth.pm/items/2280136)).
- [VRCLens](https://hirabiki.booth.pm/) by ひらびき / hirabiki — the camera prefab `index_vrclens` drives.
- [OSCmooth](https://github.com/regzo2/OSCmooth) by regzo2 — the float-to-boolean quantization convention `index_puppet` follows.
- [VRCFaceTracking](https://github.com/benaclejames/VRCFaceTracking) by benaclejames — detected over mDNS by `osc_vrcft`, which sets the matching avatar parameters.

## How it works

vrc-bridge registers itself with SteamVR as a background application to receive low-level controller input. It simultaneously runs an OSC server (in) and client (out) for VRChat. The core engine processes inputs, and the active router directs them to the correct mapping, which emits the appropriate OSC commands.

See [`docs/design.md`](docs/design.md) for what this project is, the decisions behind it, and the sequenced plan for where it is going.

## Development

```
pip install -e ".[dev]"
pytest
```

## Troubleshooting

If controller inputs don't register, check SteamVR → Settings → Controllers → Show Old Binding UI → VRBridge Controller Input, and ensure the default binding profile is active for your controller type.

## Bundled configs

The `assets/` folder ships example [Voicemeeter](https://vb-audio.com/Voicemeeter/) audio-routing configurations for the VRChat audio setup, provided as a starting point:

- `assets/VRChat-Potato.xml` — a Voicemeeter Potato preset.
- `assets/VRChat-VBAN.xml` — a VBAN (network audio) configuration.

Import them in Voicemeeter and adapt the device/channel assignments to your own machine.

## License

MIT — see [LICENSE](LICENSE).
