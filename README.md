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

Options include `--router {name}`, `--log-level`, `--log-callbacks`, `--log-file PATH` / `--no-log-file` ([Logs](#logs)), and `--no-steamvr` (desktop mode without controller support). On first launch the SteamVR action manifest and default bindings are generated under `steamvr_files/` in a source checkout, or under your per-user data directory for an installed package. Set `VRBRIDGE_FILES_DIR` to put them somewhere else.

By default the bridge discovers VRChat over OSCQuery and sends to the port it advertises. To drive something that announces nothing — Lyuma's Av3Emulator in Unity play mode, for instance — name its ports instead. `--osc-port 9000` sends there and stops discovery from ever taking the target back; `--osc-bind-port 9001` listens on the port such a peer already sends to, since it has no way to learn the free port the bridge would otherwise pick. `--osc-host` sets the host for `--osc-port` and defaults to loopback; it aims sends only, since the bridge always listens on loopback, so a peer named on another machine can be sent to but cannot answer. Use both port flags together: a peer that cannot discover you needs to be told where to send as much as it needs to be sent to. A pinned run still reads OSCQuery from the VRChat client whose advertised OSC port is the pinned one, and still advertises itself, so a running VRChat can still find the bridge and push avatar parameters into it. `--no-advertise` stops that, for two VRChat clients on one PC: launch each with `--osc=inPort:ip:outPort` and give each its own bridge, pinned to that client's ports with `--osc-port`/`--osc-bind-port` and run with `--no-advertise`, so the other client's discovery does not also land here. `--no-advertise` is refused without `--osc-port`, since an unadvertised, unpinned bridge can neither find a client nor be found.

Settings come from `$VRBRIDGE_CONFIG` if it is set, else `vrbridge.toml` at the checkout root for a source run (gitignored, so `git status` never shows it) or in your per-user data directory for an installed package. A missing file means defaults; an unreadable or invalid one stops the bridge with an error naming it. To see which file and values are in force:

```
python -c "from vrbridge.settings import load_settings, get_config_path; print(get_config_path(), load_settings())"
```

## Logs

`vrbridge` writes what it prints to the console into a file as well, so something odd you notice mid-session can be read afterwards instead of reproduced — which matters most when another program launches the bridge and there is no console to scroll back in. Each run gets its own file, `logs/vrbridge_<date>_<time>_<pid>.log`, under the bridge's base directory: the checkout root in a source install, your per-user data directory for an installed package. Its first line names the arguments, the code and settings file in use, and the log file itself; a run that stops on an error, such as an invalid settings file, leaves the traceback there. Files older than 14 days are deleted at the next start.

`--log-file PATH` appends to a file you name instead, and prunes nothing beside it. `--no-log-file` keeps the console only. `vrbridge-paramlog` writes no log file, and neither does a `VRBridge` you build yourself: call `vrbridge.logfile.attach_log_file(path)` if you want one.

The file follows `--log-level`. At the default, INFO, a session is a few kilobytes: discovery, avatar changes, what persistence restored or why not, warnings and errors. It is not a record of OSC traffic. `--log-level DEBUG` adds every message the bridge sends and every one it ignores, which is most of what an avatar emits, so a run's file rolls over at 5 MiB and keeps one older copy (`.log.1`): at DEBUG you get the most recent traffic, not the session. The cap does not hold while another program has the log open: Windows will not let the bridge move a file something else is holding, so it keeps writing to the same file and rolls over once that program lets go. `--log-level WARNING` or higher leaves out the first line too.

**Read a log before you share it.** It names the avatar ids you wore, the world and instance you were in, addresses on your network and the OSCQuery services advertised there, the address of anything that connected to the [external socket](#external-ai-socket), and paths on your PC. Now and then it also quotes a line of VRChat's own log that the roster could not parse, which can carry another player's name.

## Routers and mappings

A **router** decides which mapping is active at any moment.

| Router    | Behavior |
|-----------|----------|
| `default` | Switches between `IndexPuppet` and `UserCamera` by the VRChat camera state; `MuteProxy`, `VRCFT` and `Persistence` stay on. |
| `camera`  | Switches between `IndexPuppet`, `VirtualLens2`, and `VRCLens` based on the lens system detected on the current avatar; `MuteProxy` and `Persistence` stay on, but `VRCFT` is not registered. |

**Core mappings**

- **Index Puppet** — two-axis avatar puppet control from absolute finger position on the touchpads; optional mirroring to both hands from a single controller.
- **User Camera** — full VRChat User Camera control (aperture, exposure, zoom, capture, modes).
- **VirtualLens2 / VRCLens** — dedicated control schemes for those camera prefabs; the `camera` router switches to them automatically when detected.
- **Mute Proxy** — toggles the VRChat microphone from a watched OSC parameter.
- **Wardrobe** — changes your worn avatar from a button on your own expression menu. Needs the [`osc-wardrobe`](#wardrobe) prefab on the avatar and a manifest listing the avatars each button means; it is opt-in, so register it from your own router. VRChat only accepts avatars in your favorites, recents, uploads or purchases.
- **Persistence** — carries a prop's placed position across one swap to an avatar carrying the same prop ([below](#persistence-across-an-avatar-swap)). On in every router; it does nothing on an avatar without the prop.
- **Leash** — a held or planted leash on your avatar pulls you toward its far end, over VRChat's `/input/` movement axes, taking only the axis it pulls on so you keep the other. Needs an avatar that publishes the five leash parameters listed in `vrbridge/mappings/osc_leash.py`; the avatar half is planned as a [vrc-patterns](https://github.com/Ryan6-VRC/vrc-patterns) entry. Off by default; `[leash] enabled = true` in `vrbridge.toml` turns it on in every router, and the same section holds its tuning. The idea comes from [OSCLeash](https://github.com/ZenithVal/OSCLeash) by ZenithVal; this is a rewrite that shares no code with it.
- **External AI socket** — one TCP socket another program connects to (an AI, a tool on another PC): it subscribes to avatar parameters, controller events, avatar changes and the instance roster, and writes avatar parameters and the worn avatar, as lines of JSON ([below](#external-ai-socket)). Off by default; `[external_ai] enabled = true` turns it on in every router.
- **Parameter logger** — records whitelisted avatar parameters (names or globs) to a timestamped CSV as they change; runs standalone as `vrbridge-paramlog --params "MyThing/*" [--file out.csv]`. The whitelist is required — full traffic is too noisy to log raw. For two VRChat clients on one PC (each launched with `--osc=inPort:ip:outPort`), run one logger per client with `--osc-port`/`--osc-bind-port` naming that client's ports and `--no-advertise` so the other client's discovery does not also land here.

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

**If you pin the send target** with `--osc-port`, the bridge reads the marker from the VRChat client advertising the pinned OSC port. A pinned peer that advertises nothing — the Av3Emulator, for instance — has no OSCQuery tree to read the marker from, so the wardrobe can never arm on its own. Name the manifest instead:

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

**Which manifest applies is the avatar's own statement**: the entry declares an unsynced Int `QuantChannel/Manifest` whose *default value* is the manifest id, and the directory reads it over OSCQuery. Like the wardrobe, it never reads on the avatar change itself — a swap announces `/avatar/change` before the new avatar is applied, so a read taken there answers for the outgoing one, and a cold download leaves nothing to read for tens of seconds. The directory clears on the change and re-reads when a consumer next asks, retrying at most every 2 s until the loaded avatar answers — the same retry that carries it through a client not yet discovered, each blocking state logged once.

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

**If you pin the send target** with `--osc-port`, the bridge reads the tree of the VRChat client advertising the pinned OSC port. For a pinned peer that advertises nothing (the Av3Emulator serves no tree), name the manifest instead: `QuantChannelDirectory.load_from_settings(bridge, pinned_manifest_id=1)`. There is deliberately no CLI flag for this — the mapping is only reachable from code that already holds the constructor.

One guard worth knowing: a manifest that declares channels at `index_puppet`'s own addresses must agree with your `[puppet]` settings (`quant_level`, `float_smooth_tau_secs`), or the directory refuses to arm it and the log names both values. The manifest and the settings describe the same wire; when they diverge, one of them is stale.

## Persistence across an avatar swap

Keep a world-placed prop where it was when you swap to another avatar carrying the same prop. The avatar publishes its state under `/avatar/parameters/BridgePersist/<Name>/`; the bridge remembers it through one swap and writes it back into the new avatar a short settle after that avatar reports it has booted (`BridgePersist/<Name>/Boot`), then, after a second and shorter wait, sets `BridgePersist/<Name>/Restore` to 1 to say the values are in place. The avatar never answers, and one that hears nothing within its own wait starts as it would with no bridge running. Nothing is configured on the bridge side: the avatar's namespace, and the identity it announces, are the whole contract. The avatar half comes from vrc-patterns: the [`bridge-persist`](https://github.com/Ryan6-VRC/vrc-patterns/tree/main/bridge-persist) entry is the layer to build into a gimmick of your own, and the [`compositions/grab-sync-persist`](https://github.com/Ryan6-VRC/vrc-patterns/tree/main/compositions/grab-sync-persist) composition is a ready prop that carries it.

By default it restores a single swap to an avatar carrying the same prefab, and a full-body calibration, which reloads your avatar. Swapping through a third avatar, Reset Avatar, joining any world (including a rejoin), and VRChat restarting all forget. An avatar can ask to keep its state for as long as you stay in the instance with the vrc-patterns entry's `scope` setting; at `instance-keep-reset` even Reset Avatar keeps it, so leaving the instance is the only way to clear it. Telling a calibration from Reset Avatar, and one instance from the next, takes VRChat's log, found the same way as [the roster](#the-roster) under `[external_ai] log_dir`; when the bridge cannot match the log to its client (a pinned peer that advertises nothing, the Av3Emulator, for one), calibrations forget and a longer `scope` falls back to the default. A bridge started after the avatar loaded restores nothing on the first swap, only once an avatar has loaded in front of it. Each time an avatar loads with persistence on, the bridge logs at the default log level either that it restored the namespace or why it did not.

Every shipped router runs it, in every mode, so `vrbridge` with any `--router` needs nothing extra. It does nothing on an avatar that publishes no `BridgePersist` namespace. On the library path, register it yourself:

```python
from vrbridge import VRBridge
from vrbridge.mappings import BridgePersistMapping

bridge = VRBridge()
persist = BridgePersistMapping(bridge)
persist.register()
persist.activate()
bridge.start()
```

Against the Av3Emulator (`VRBridge(target=("127.0.0.1", 9000), bind_port=9001)`), a play, stop, play re-announces the same avatar, which on a live client means a reload and restores nothing. `BridgePersistMapping(bridge, treat_reload_as_swap=True)` makes it restore there. It is for testing only, because on a live client it would restore across Reset Avatar and world joins, so no router sets it: register that instance yourself on the library path, instead of running a router.

## External AI socket

Let another program watch and drive your avatar through the bridge: an AI companion, a stream tool, a script on another PC. It connects to one TCP socket, says what it wants to hear about, and gets each change as a line of JSON; it writes avatar parameters, and changes the worn avatar, the same way. Any language that can open a socket and read lines can be a client — there is nothing to install on its side.

Turn it on in `vrbridge.toml`:

```toml
[external_ai]
enabled = true        # registered by every router, in every mode
bind = "127.0.0.1"    # loopback: only programs on this PC. "0.0.0.0" opens it to your LAN
port = 9002
log_dir = ""          # VRChat's log folder, which persistence reads too; empty means the default location
```

**There is no authentication.** On loopback, anything running on your PC can connect; bound to the LAN, anything on your network can. Only widen `bind` on a network you trust.

### The wire

UTF-8, one JSON object per line, each way. Everything the bridge sends carries `ev` (what it is), `seq` (1 on the first line of a connection, then rising by one per line) and `t` (the bridge's wall clock, seconds). Everything a client sends carries `op` and may carry `id`; a reply or error to that request echoes the `id`.

On connect, the bridge greets you with whom it is sending to (`null` until VRChat is found) and whether that target was set by hand:

```json
{"ev":"welcome","seq":1,"t":1790000000.1,"version":1,"target":{"host":"127.0.0.1","port":9000},"pinned":false}
```

Requests:

```json
{"op":"subscribe","params":["Ears/*","/avatar/parameters/Mood"],"controller":["touchpad.short_press"],"avatar":true,"roster":true}
{"op":"set","id":1,"address":"Mood","value":2}
{"op":"get","id":2,"addresses":["Mood","Ears/Wiggle"]}
{"op":"change","id":3,"avatar":"avtr_00000000-0000-0000-0000-000000000000"}
{"op":"will","set":[{"address":"Mood","value":0},{"address":"Talking","value":false}]}
{"op":"ping","id":4}
```

- `subscribe` replaces whatever the connection subscribed to before; a missing key means nothing of that kind. In `params` a bare name is an avatar parameter (`Mood` is `/avatar/parameters/Mood`) and `*`, `?` and `[...]` are wildcards (`*` also crosses `/`). `controller` names event types from `vrbridge.ControllerEventType`; `touchpad.scroll_raw` streams at the controller's poll rate, so ask for it only if you need it. An unknown type name is an error naming it, and the rest of the subscription still applies. `"roster": true` is answered at once with the current roster.
- `set` writes one avatar parameter. It is answered only if it fails.
- `get` reads each parameter's current value from VRChat, by name. Use it at startup: the stream only reports *changes*, so a value that has not changed since you connected is never streamed.
- `change` asks VRChat to wear an avatar. VRChat only accepts avatars in your favorites, recents, uploads or purchases, and never reports whether a change worked.
- `will` sets the connection's **last will**: writes the bridge sends, in order, when your connection closes for any reason — you disconnect, your program crashes, the bridge stops. Each will fires once. A new `will` replaces the old one; an empty list clears it.
- `ping` is answered with `pong`. Requests on one connection are handled in order, so a `pong` means everything you sent before it has been handled.

Events:

```json
{"ev":"param","seq":5,"t":1790000000.2,"address":"/avatar/parameters/Mood","name":"Mood","value":2}
{"ev":"value","seq":6,"t":1790000000.2,"address":"/avatar/parameters/Mood","found":true,"value":2,"id":2}
{"ev":"controller","seq":7,"t":1790000000.3,"type":"touchpad.short_press","hand":"left","steps":null,"dx":null,"dy":null,"ax":null,"ay":null,"when":12.5}
{"ev":"avatar","seq":8,"t":1790000000.4,"id":"avtr_00000000-0000-0000-0000-000000000000"}
{"ev":"target","seq":9,"t":1790000000.5,"host":"127.0.0.1","port":9000}
{"ev":"roster","seq":10,"t":1790000000.6,"self":{"id":"usr_...","name":"You"},"world":{"id":"wrld_...","instance":"12345~region(us)","name":"Example World"},"joined":true,"players":[{"id":"usr_...","name":"You"}]}
{"ev":"join","seq":11,"t":1790000000.7,"player":{"id":"usr_...","name":"A Friend"}}
{"ev":"leave","seq":12,"t":1790000000.8,"player":{"id":"usr_...","name":"A Friend"}}
{"ev":"error","seq":13,"t":1790000000.9,"op":"set","id":1,"message":"address '/input/Jump' is not an avatar parameter; ..."}
{"ev":"pong","seq":14,"t":1790000001.0,"id":4}
```

- `value` answers `get`: `found` is false with no `error` when the worn avatar has no such parameter, and false with an `error` (and a `detail`) when the bridge could not ask — no VRChat found yet, or a target set by hand that no VRChat client advertising its OSC port backs, which leaves no values to read.
- `target` goes to every connection when the bridge finds VRChat, or finds it again after a restart.
- `roster` is the whole roster: sent when you subscribe, and again whenever the room changes (you join or leave a world). Between those, `join` and `leave` name one player each.
- `error` with `"dropped": n` means your program read too slowly and the bridge discarded the `n` oldest events waiting for it, keeping the newest. Read faster, or re-`get` what you care about.

### Types

The JSON type decides the OSC type: `true`/`false` is a Bool, a whole number written without a decimal point is an Int (`2`), and a number with a decimal point or exponent is a Float (`2.0`, `0.5`). Match what the avatar declares: an Int sent to a Float parameter does not arrive as that number, so send `1.0`, not `1`, to a Float. Anything else — a string, `null`, a list — is refused.

### What is refused

Writes reach avatar parameters (`/avatar/parameters/...`) and the worn avatar (`change`), nothing else: `/input/*`, `/chatbox/*`, `/tracking/*` and other addresses are errors, and so is a wildcard in a write address. A line that is not a JSON object, or an unknown `op`, is an error too, and the connection stays open. A request line over 1 MiB is the one thing that closes the connection, after an error naming the limit.

### Order and repeats

The `param` stream is the bridge's change-filtered stream: one event per change of value, so VRChat sending a value twice is one event, and a value that has not changed is never re-sent. Your own `set` comes back as an ordinary `param` event once the avatar reports the new value. Events for different parameters can arrive slightly out of the order VRChat sent them; sort on `seq` if you log them, and treat each `param` as "this is the value now".

The bridge does not reset anything when the avatar changes; what your writes meant is yours to undo. Watch `avatar` events for that, and put whatever must never be left switched on in your `will`.

### The roster

VRChat sends no roster over OSC, so the bridge reads it from VRChat's own log file (`output_log_*.txt` under `log_dir`). It picks the log of the VRChat client the bridge is talking to, by matching the OSCQuery service name that client writes into its log at startup, and falls back to the newest log until a client is found. That is what keeps the roster right with two VRChat clients on one PC: each bridge follows the log of the client it found. A bridge whose target was set by hand (`--osc-port`) follows the newest log until the VRChat client advertising its pinned OSC port is found, then binds the roster and persistence's log to that client, and again if the client restarts, however the clients were started.

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

If `import vrbridge` fails after the checkout was moved, the editable install still points at the old path: run `pip install -e .` again from the new location.

If controller inputs don't register, check SteamVR → Settings → Controllers → Show Old Binding UI → VRBridge Controller Input, and ensure the default binding profile is active for your controller type.

## Bundled configs

The `assets/` folder ships example [Voicemeeter](https://vb-audio.com/Voicemeeter/) audio-routing configurations for the VRChat audio setup, provided as a starting point:

- `assets/VRChat-Potato.xml` — a Voicemeeter Potato preset.
- `assets/VRChat-VBAN.xml` — a VBAN (network audio) configuration.

Import them in Voicemeeter and adapt the device/channel assignments to your own machine.

## License

MIT — see [LICENSE](LICENSE).
