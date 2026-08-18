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

By default the bridge discovers VRChat over OSCQuery and sends to the port it advertises. To drive something that announces nothing — Lyuma's Av3Emulator in Unity play mode, for instance — name its ports instead. `--osc-port 9000` sends there and stops discovery from ever taking the target back; `--osc-bind-port 9001` listens on the port such a peer already sends to, since it has no way to learn the free port the bridge would otherwise pick. `--osc-host` sets the host for `--osc-port` and defaults to loopback; it aims sends only, since the bridge always listens on loopback, so a peer named on another machine can be sent to but cannot answer. Use both port flags together: a peer that cannot discover you needs to be told where to send as much as it needs to be sent to. Note that a pinned run still advertises itself, so a running VRChat can still find the bridge and push avatar parameters into it.

## Routers and mappings

A **router** decides which mapping is active at any moment.

| Router    | Behavior |
|-----------|----------|
| `default` | Switches between `IndexPuppet` and `UserCamera` by the VRChat camera state; `MuteProxy` and `VRCFT` stay on. |
| `camera`  | Switches between `IndexPuppet`, `VirtualLens2`, and `VRCLens` based on the lens system detected on the current avatar; `MuteProxy` stays on, but `VRCFT` is not registered. |
| `remy`    | The `default` router plus the Remy AI integration (see below). |

**Core mappings**

- **Index Puppet** — two-axis avatar puppet control from absolute finger position on the touchpads; optional mirroring to both hands from a single controller.
- **User Camera** — full VRChat User Camera control (aperture, exposure, zoom, capture, modes).
- **VirtualLens2 / VRCLens** — dedicated control schemes for those camera prefabs; the `camera` router switches to them automatically when detected.
- **Mute Proxy** — toggles the VRChat microphone from a watched OSC parameter.
- **Wardrobe** — changes your worn avatar from a button on your own expression menu. Needs the [`osc-wardrobe`](#wardrobe) prefab on the avatar and a manifest listing the avatars each button means; it is opt-in, so register it from your own router. VRChat only accepts avatars in your favorites, recents, uploads or purchases.
- **Parameter logger** — records whitelisted avatar parameters (names or globs) to a timestamped CSV as they change; runs standalone as `vrbridge-paramlog --params "MyThing/*" [--file out.csv]`. The whitelist is required — full traffic is too noisy to log raw. For two VRChat clients on one PC (each launched with `--osc=inPort:ip:outPort`), run one logger per client with `--osc-port`/`--osc-bind-port` naming that client's ports and `--no-advertise` so the other client's discovery does not also land here.
- **Remy AI integration** — triggers actions on an external AI service. Point it at your host with `VRBRIDGE_REMY_URL` (defaults to `http://127.0.0.1:8000`) and `VRBRIDGE_REMY_WATCH_DIR` for the screenshot folder.

## Extending vrc-bridge

There are two supported routes, and they answer different questions.

**Use it as a library** when you want the input and OSC plumbing but your own control flow. Build a `VRBridge`, attach callbacks up front, then start it:

```python
from vrbridge import VRBridge, ControllerEventType

bridge = VRBridge()
bridge.on_osc("/avatar/parameters/MyThing", lambda ctx, addr, value: print(addr, value))
bridge.on_controller(ControllerEventType.TOUCHPAD_SHORT_PRESS, hand="left",
                     callback=lambda ctx, evt: ctx.send("/input/Jump", 1))
bridge.start()
```

`ctx.send` returns `False` if the message was dropped because VRChat has not been discovered yet — check it if you mirror what you send. To write a reusable mapping instead of loose callbacks, subclass `vrbridge.mappings.Mapping` and put your bindings in `_attach()`; the base calls it exactly once, so registering twice cannot double-bind your callbacks.

**Ship a router** when you want your mapping set selectable from the installed CLI. Advertise a `MappingRouter` subclass under the `vrbridge.routers` entry-point group and it appears in `vrbridge --router`:

```toml
[project.entry-points."vrbridge.routers"]
myrouter = "mypackage.routers:MyRouter"
```

A plugin that fails to import, is not a `MappingRouter`, or reuses a built-in name is skipped with a warning naming it — it is never silently missing.

Settings work the same way for both: `vrbridge.settings.settings()` returns the resolved configuration, and any mapping accepts a `tuning=` argument if you would rather pass your own.

## Wardrobe

Change your worn avatar from your own expression menu. Press a button, the bridge sends `/avatar/change`, VRChat swaps.

You need two things: the `osc-wardrobe` prefab on the avatar (from [vrc-patterns](https://github.com/Ryan6-VRC/vrc-patterns) — drop it in, no animator work), and a **manifest** saying which avatar each button means.

Manifests live in `wardrobe/` next to your `vrbridge.toml`, one `.toml` per wardrobe menu. That directory is gitignored, because an avatar id identifies real account content. Copy `wardrobe.example.toml` to start:

```toml
id = 1                    # must match the prefab's Manifest parameter default on the avatar

[[slots]]
slot  = 1                 # which button (1-8)
label = "streaming"       # appears in the log, nowhere else
id    = "avtr_26187637-0c30-4a09-86e1-bc928c07309e"
```

The `id` at the top is how one avatar picks its own wardrobe: the prefab declares a parameter whose default value is that number, the bridge reads it off whatever you are wearing, and looks up the matching manifest. So different avatars can carry different menus — give each its own manifest and set the prefab's default to match. Two avatars may share one manifest id if you want them to share a wardrobe; two manifests may not.

Valid ids are 1–255, because Modular Avatar's inspector clamps an Int parameter default to that range.

Three things worth knowing before you file a bug:

- **VRChat only accepts avatars in your favorites, recents, uploads, or purchases.** An ineligible id does not swap, and the bridge cannot tell you it failed: the client acknowledges every request the same way whether it can wear the avatar or not, and never reports the result. So if a button does nothing, check eligibility first — and check your profile page on vrchat.com, which shows what you are actually wearing.
- **The wardrobe goes quiet on an avatar without the prefab.** That is normal — there is no menu there to press. Swap back the usual way and it re-arms on the next avatar that has one.
- **Buttons, not toggles.** The mapping swaps on the press and ignores the release, so a toggle left switched on would swap again on your next avatar load.

The mapping is opt-in: no shipped router registers it, so add it to your own.

```python
from vrbridge import VRBridge
from vrbridge.mappings import WardrobeMapping

bridge = VRBridge()
wardrobe = WardrobeMapping.load_from_settings(bridge)   # or pass manifests= yourself
wardrobe.register()
wardrobe.activate()        # `enabled` is yours to own; the mapping never sets it itself
bridge.start()
```

`activate()` is not optional — a registered but inactive wardrobe ignores every press.

The manifest is read off the worn avatar **on every press**, not when the avatar changes. That is deliberate: a cold avatar download can take a minute, so anything reading on the change would be asking about an avatar that does not exist yet. Reading at the press costs about a millisecond and is always about the avatar whose button you pressed — while an avatar is loading you are the placeholder, which sends nothing, so a press can only ever come from an avatar that is fully there.

**If you pin the send target** with `--osc-port` — at the Av3Emulator, or anything else that advertises nothing — there is no OSCQuery tree to read the marker from, so the wardrobe can never arm on its own. Name the manifest instead:

```python
wardrobe = WardrobeMapping.load_from_settings(bridge, pinned_manifest_id=1)
```

`vrbridge.wardrobe` is the manifest loader if you would rather build the table in code: `load_manifest(path)`, `load_manifests(paths)` and `discover_manifests(dir)` all return validated `Manifest` objects and raise `ConfigError` naming the offending key and file.

## Quant channels

Send a continuous value to an avatar two ways at once: a full-precision float for the wearer's own client, plus OSCmooth-shaped quantized booleans (`<Name>1/2/4…` + `<Name>Negative`) that are the only part remote players see. The avatar-side decode/smoothing layers come from the `quant-channel` entry in [vrc-patterns](https://github.com/Ryan6-VRC/vrc-patterns); this repo owns the sender half: the codec (`vrbridge.quant_channel`), the manifest loader (`vrbridge.quant_manifest`), and the directory mapping that learns which manifest describes the worn avatar (`vrbridge.mappings.QuantChannelDirectory`). `index_puppet` is the shipped consumer.

**The manifest is the extension surface.** The generator emits one JSON manifest per avatar module; install it into `manifests/` next to your `vrbridge.toml` (or point `[quantchannel] manifest_dir` elsewhere). One file per module:

```json
{
  "schema": 1, "id": 1, "revision": 1,
  "channels": [
    {"name": "QDemo/LX", "address": "/avatar/parameters/QDemo/LX",
     "bits": 3, "signed": true, "floatTau": 0.12,
     "declaredWidths": {"bools": 4}}
  ],
  "gates": [{"name": "QDemo/Enable", "address": "/avatar/parameters/QDemo/Enable"}]
}
```

- `id` is identity and `revision` is content: a manifest keeps its id when its channels change, and `revision` bumps on any channel change so a stale installed copy is at least visible in the log line the directory prints when it arms. Valid ids are **1 and up — there is no 255 ceiling here** (this sentinel is emitted straight into the avatar's parameter asset, and ids above 255 are live-validated). The range convention: **1–999 belong to vrc-patterns entries, 1000+ to third parties**; the quant-channel entry README's registry table is the ledger.
- `address` is a checked echo of `name` (`/avatar/parameters/` + name): kept so your consumer never derives it, verified so it can never drift into a second source of truth.
- `bits: 0` declares a float-only channel (the float itself is synced); `signed` is illegal there. `floatTau` is the sender-side smoothing time constant for the float companion — bits are always raw and immediate.
- The loader refuses unknown keys, unknown `schema` values, and duplicate ids across the loaded set, always naming the offending key and file.

**Which manifest applies is the avatar's own statement**: the entry declares an unsynced Int `QuantChannel/Manifest` whose *default value* is the manifest id, and the directory reads it over OSCQuery. Like the wardrobe, it never reads on the avatar change itself — a cold avatar download runs 30–60 s while the client acknowledges the change immediately, so the directory clears on the change and re-reads when a consumer next asks, retrying at most every 2 s until the loaded avatar answers.

The directory is opt-in — no shipped router registers it:

```python
from vrbridge import VRBridge
from vrbridge.mappings import QuantChannelDirectory

bridge = VRBridge()
directory = QuantChannelDirectory.load_from_settings(bridge)
directory.register()
directory.activate()
bridge.start()
# a consumer asks:  table = directory.active_manifest()   # None until armed
```

**If you pin the send target** with `--osc-port` (the Av3Emulator advertises nothing and serves no tree), name the manifest instead: `QuantChannelDirectory.load_from_settings(bridge, pinned_manifest_id=1)`. There is deliberately no CLI flag for this — the mapping is only reachable from code that already holds the constructor.

One guard worth knowing: a manifest that declares channels at `index_puppet`'s own addresses must agree with your `[puppet]` settings (`quant_level`, `float_smooth_tau_secs`), or the directory refuses to arm it and the log names both values. The manifest and the settings describe the same wire; when they diverge, one of them is stale.

## Interoperates with

vrc-bridge speaks to these projects over OSC. None of their code is vendored here — the parameter names their mappings drive are each project's own contract, and their documentation is the authority on them.

- [VirtualLens2](https://vlens2.logilabo.dev/) by ろじらぼ / logilabo — the camera prefab `index_virtuallens` drives ([BOOTH](https://logilabo.booth.pm/items/2280136)).
- [VRCLens](https://hirabiki.booth.pm/) by ひらびき / hirabiki — the camera prefab `index_vrclens` drives.
- [OSCmooth](https://github.com/regzo2/OSCmooth) by regzo2 — the float-to-boolean quantization convention `index_puppet` follows.
- [VRCFaceTracking](https://github.com/benaclejames/VRCFaceTracking) by benaclejames — detected over mDNS by `osc_vrcft`, which sets the matching avatar parameters.

## How it works

vrc-bridge registers itself with SteamVR as a background application to receive low-level controller input. It simultaneously runs an OSC server (in) and client (out) for VRChat. The core engine processes inputs, and the active router directs them to the correct mapping, which emits the appropriate OSC commands.

See [`docs/design.md`](docs/design.md) for what this project is, the decisions behind it, and the measured behaviour a mapping author has to build around.

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
