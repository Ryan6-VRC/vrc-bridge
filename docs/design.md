# vrc-bridge — design

Primary reader: agent.

**Cite by path, never by SHA.** This repo's history is slated for re-creation when it is packaged as a product, so a commit reference recorded anywhere — here, a PR body, a handoff — rots. Name the file and the symbol.

vrc-bridge is a **product**: the SteamVR-input to VRChat-OSC substrate for people building OSC-driven gimmicks. The controller layer is the load-bearing half nothing else supplies, and the camera mappings are both shipped features and the worked examples that teach the substrate. It is not a personal input rig to be minimized, and it is **not** the workspace's internal verification surface.

The contract is with third-party mapping authors, not with this workspace's gates. `docs/verify.md` governs claims about *avatars*; this repo's claims are about software, gated by ordinary tests. "Only provable in a live client" describes the whole VRChat tooling ecosystem, not a defect in this repo.

## Settled decisions

Do not relitigate these; they are the operator's.

| Decision | Consequence |
|---|---|
| Library **and** application, with a declared seam | The library path is the documented extension route; a `[project.entry-points]` router group makes third-party routers reachable from the CLI. |
| Operator-tunable values live in a config file | Retuning must not require editing installed source. A constant that is a *contract* rather than a feel setting stays in source and says so — `osc_wardrobe.REPEAT_GUARD_SECS` and `osc_manager._SERVE_POLL_SECS` are both deliberate, and `settings.py`'s header rule draws the line. |
| Parameter discovery is **descoped** | Discovery serves an observer poking an avatar they did not author; a user here owns both ends and already knows the names. Never build it standalone, and do not accept a dependency that carries it in. |
| `index_remy` is a labelled personal-integration example | Lazy-imported, behind an optional extra, kept as the worked example of an integration mapping. |
| Named ancestors get **links, not notices** | We interface with OSCmooth, VirtualLens2, VRCLens, VRCFaceTracking; we borrowed code from none of them. |
| `osc_leash` is **deleted** and does not return | It was the sole code borrow (§Provenance). Its replacement is a `vrc-patterns` entry on face-proximity boxes, not a mapping rewrite. |
| Test-determinism machinery lives in the fake, never as a seam in the code under test | `FakeVRChat`'s knobs — `node_fault`, `node_garbage`, `node_404_first`, `hold_next_node_get` — make a hard-to-reach path reachable. A seam in the code under test would instead encode the interleaving its author already knew about, and its placement would be chosen by whoever already knew the bug. |
| Design record lives here; `gimmicks.md` carries the only route in | A product's design record travels with the product. No new first-hop doc, no `docs/` owner in the meta-repo. |

## Inbound delivery semantics

Four facts about what arrives. Together they are why a mapping must be idempotent per *value* rather than per delivery.

**Every inbound message is delivered twice, from two UDP sender sockets** — measured on one client build (`VRChat-Client-161618`) on one machine, so treat it as the working assumption rather than a law. Every inbound-triggered callback fires twice, so a non-idempotent listener double-applies; outbound sends appear once. Whether the cause is VRChat's or our own mDNS configuration is unsettled, and the leading suspect is a client opening one sender per announcement, since `start()` once announced on every interface while the record carried a single loopback address. **Do not reach for a dedupe window** — it masks the signal the open measurement below reads.

Two attributions are already ruled out, so do not re-investigate them: the service record is not itself duplicated, and there is only one candidate destination — one service, one address, `VRBridge.local.` resolving to `127.0.0.1` alone — so the `OSC_IP` missing from our `?HOST_INFO` in §OSCQuery interop gaps is **not** implicated here despite looking like it should be.

**A repeated identical value never reaches a mapping at all.** `_update_cache_and_fire` compares and writes under one lock and suppresses a value equal to the last seen, so the second copy of one press is eaten until something calls `forget()`. The consequence for anything guarding against doubled delivery: the guard's live window *opens* at `forget()`, roughly 1 ms after arrival, rather than at arrival. A test that redelivers a value without first driving the source's return to rest is exercising the change filter and nothing else.

**Delivery order is not preserved, and that is accepted rather than fixed.** The listener fires after `_cache_lock` is released, so under thread-per-datagram two datagrams for one address can reach a mapping in reverse arrival order — a `ParamState` mirror can ingest the older value last and drive its next step from a value the avatar no longer holds. Both obvious fixes cost more than the defect: holding the lock across the listener would have `osc_wardrobe`'s marker read hold it for `fetch_timeout_secs`, and dropping the stale fire would eat that mapping's return-to-rest. A mapping that cannot tolerate reordering carries its own sequence check, as `osc_wardrobe`'s press token does.

**The thread decides what may block: the OSC datagram path tolerates a sleep, the controller path does not.** Datagram dispatch is thread-per-datagram, so `osc_vrcft`'s load delay is free. Controller callbacks run synchronously on the single `ControllerLoop` thread — `_poll_once` → `_emit` → the listener, no handoff — so a sleep there freezes input polling, and a 0.4 s freeze is exactly `long_press_threshold`: long enough to reclassify the following tap and fire the wrong feature. This is why `press_pulse` is asynchronous, queueing onto one worker thread per address.

**The one measurement still owed:** re-run a source-endpoint probe against a live client now that the announcement is pinned, with a third-party OSCQuery receiver as a control. Two senders still arriving makes the doubling a client behaviour to tolerate rather than our own misconfiguration. Reach for the **source endpoint**, never a duplicate count — distinct source ports are what rule out socket-layer duplication and a doubled log handler alike. Where sender counts are the evidence, measure `len(Zeroconf().engine.senders)` and never a count of reachable interfaces.

## What is proven, and what has never been exercised

The handshake is solid in daily use, but the proven set is narrower than "it works" suggests, and knowing where the edge sits keeps a change from being aimed at the wrong half. `tests/` is the enumeration; this is the shape of it.

- **Proven in daily use, not headlessly:** the mDNS browser finds VRChat and other `_oscjson._tcp` services, and VRChat accepts our advertisement and sends avatar params.
- **Proven headlessly**, against a fake VRChat on loopback: the client half — HOST_INFO parsing, that a selected target receives datagrams, that a mapping's sends land at the addresses it declares; target *selection*, which separates from discovery because `_consider_service` takes a name and a `ServiceInfo` and decides, so handing it one needs no network; and interleaved delivery, because `FakeVRChat.hold_next_node_get` parks a read where production blocks and a second thread runs against it.
- **Reachable only with a live client:** whether the announcement pin collapses the doubled inbound, and what the client does with an ineligible avatar id.
- **Still never exercised:** any client reading *our* tree, and mDNS discovery itself — the `_BrowserListener` wiring, meaning whether zeroconf calls those methods when we expect. Deliberately not faked: browsing a real network from a test is flaky and proves nothing that pointing at a known port does not. VRChat is the tree's only consumer and tolerates every non-conformance in §OSCQuery interop gaps.
- **Out of the interleaving harness's reach:** it parks *after* the change filter, so two copies of one value racing `_update_cache_and_fire` with both reading `old is None` cannot be staged. Per §Inbound delivery semantics that race is unreachable in production too.

**Trap:** running alongside VRCFaceTracking proves nothing about our server. `osc_vrcft` consumes VRCFT's mDNS *advertisement* via `is_service_running`, and VRCFT never reads our tree. `_service_rank` scores it below VRChat so it **cannot unseat** VRChat — but it can take an *empty* slot, and then `fetch` 404s against a tree holding no avatar parameters and the wardrobe reports the worn avatar as declaring no marker. Naming the real offender there is open work.

**Intent before test, on every step.** The suite is thin enough that a test written against observed behavior freezes a known defect as expected. State intended behavior first, then assert intent (`CLAUDE.md` rule 5). Where intent cannot be settled from the source the test **pins** the value and says so: pinning preserves and claims nothing about correctness. `index_vrclens`'s `FEATURE_EXPOSURE_PLUS = 110` is the live example — it breaks the pattern its neighbouring pair sets, and is kept verbatim as tested working code.

## Target selection, and peers that do not advertise

**A candidate that fails to resolve must leave the current target standing.** Dropping a live client over one malformed reply is worse than the staleness it avoids; `tests/test_target_selection.py` covers this from both the incumbent and the rival direction.

**A republication of the current target is an update and is always re-resolved; a rival still needs a strictly better rank.** VRChat keeps its service name across a restart and returns on a fresh OSC port, so refusing that tie left datagrams going to a dead port. Three consequences before touching `_consider_service` again: the incumbent's rank is stored rather than recomputed, because `_service_rank` also reads the mDNS server string and that is not retained; an unchanged republication is detected and dropped, or every mDNS refresh rebuilds the socket; and following the incumbent puts a blocking `_host_info` on zeroconf's single dispatch thread once per record refresh. That cost is accepted rather than moved off-thread, because `ServiceBrowser` serialises callbacks through one queue and that serialisation is what lets `_consider_service` read and write target state without holding the lock across the HTTP query.

**A peer that does neither half of the handshake is reached by naming its ports.** Lyuma's Av3Emulator carries VRChat's OSC surface with no service discovery of any kind, listens on 9000, and sends to a fixed `127.0.0.1:9001`. `target=(host, port)` for the send side and `bind_port=` for the receive side, from `OSCManager` through `VRBridge` out to `--osc-host`/`--osc-port`/`--osc-bind-port`. **Both, or the loop is one-directional:** a peer that cannot read `/?HOST_INFO` cannot learn a floating inbound port, so it goes on sending to the one it was built with and every reply lands nowhere. The receive half introduces the only failure the port-0 bind never had, an occupied port, which `start()` reports naming both the port and the option that asked for it. The host names a send destination and nothing else — `OSCManager.host`, which the listener, the served tree and the mDNS interface pin all follow, stays at loopback — so a peer named off this machine can be sent to and cannot answer.

**Naming a target takes the question away rather than entering it as a bid.** `_consider_service` returns at its top, so nothing found revises a given target, while `_BrowserListener` still records what it sees and `is_service_running` answers exactly as before. Letting a pin be an initial pick that ranking may unseat fails on the case it exists for: VRChat scores 3 and would take the slot from an emulator pinned beside it on loopback, mid-session, on mDNS callback timing. A pin also leaves `_current_service_name` at `None`, which is what makes `remove_service` structurally unable to clear a target no service backs.

**A pin governs the send side only.** It does not touch `advertise`, so a pinned session still registers its mDNS service and still answers `/?HOST_INFO` — with the fixed bind port, if one was given. A live VRChat therefore finds it and pushes avatar params into the same cache and listener the named peer writes to, and an inbound value is whatever reached the bind port rather than necessarily the peer we aimed at. `advertise=False` closes it, which is why the two stay separate arguments and the CLI deliberately has no flag for it.

Neither is a `settings.py` value: that file holds mapping and hardware feel, and which peer to address is runtime wiring, like `--no-steamvr`.

**`stop()`'s cost is the serve-poll interval, not thread joins.** `serve_forever()` notices `shutdown()` only between polls, so the interval *is* each server's teardown cost, and the joins measure zero because `shutdown()` already blocks until the loop exits. `_SERVE_POLL_SECS` is the lever; at a 0.5 s interval a non-advertising `stop()` costs 0.8 s against 0.1 s tuned down. A `discover=False` option was proposed as the remedy instead and declined: the Zeroconf share of that cost is paid only when advertising, and the tests already run `advertise=False`, so it buys them nothing.

## OSCQuery interop gaps

**Characterized and deliberately tolerated — not a defect list, and not pending work.** Read the ruling below before proposing a fix, or the table reads as a to-do and the work gets done twice. Latent, not live: VRChat tolerates all of these, and anything that is not VRChat will not.

| Request | Here | `vrchat-community/vrc-oscquery-lib` |
|---|---|---|
| `/` | no root `FULL_PATH`; `CONTENTS` only | full node with `FULL_PATH`/`TYPE`/`ACCESS` |
| `/?HOST_INFO` | `OSC_PORT` alone | plus `OSC_IP`, `OSC_TRANSPORT`, `NAME`, `EXTENSIONS` |
| `/?HOST_INFO=1` | `404` — the path is matched by string equality | served; matched by substring |
| `/avatar` — a path we advertise ourselves | `404`, empty body | serves any registered node |
| `/#fragment` | `404` | resolved via `Url.LocalPath` |
| 404 body, `pragma: no-cache` | empty, absent | `"OSC Path not found"`, set |

Two claims about that reference are wrong in the *other* direction and should not be repeated: its `GetAvailableTcpPort()` binds port 0, reads the number and then **closes** the socket, leaving a TOCTOU window — so binding port 0 on the real server socket and keeping it, as here, is the more correct of the two. And it *does* implement `VALUE` as a node attribute and advertise it in `EXTENSIONS`; what it lacks is a `?VALUE` query **filter**.

**The ruling: close none of them, and do not adopt a library to close them.** The tree has exactly one consumer. VRChat reads `/?HOST_INFO` for our OSC port and then sends; it does not read `CONTENTS` to decide what to send, which is why every row above has been survivable for the product's whole life. Third-party mapping authors — the audience §Settled decisions commits to — consume the Python API, not the served tree. Conformance here is work against a hypothetical consumer, and `CLAUDE.md` rule 2 disposes of it.

**Adoption was priced rather than dismissed, and the asymmetry holds under any re-examination: it never shrinks the surface that needs testing.** Measured against `python-oscquery` 0.4.0, its low-level server closes five of the six rows for free, and its own client cannot read ours — the hard evidence that a conformant client fails against us rather than degrading. Against that, it supplies no counterpart for `_consider_service`, `_service_rank` or `is_service_running`; its browser is poll-only and builds a bare `Zeroconf()` that would undo our interface pin; its service wrapper binds HTTP to every interface and advertises the port literally, so port 0 announces `0` and forces a pre-bind TOCTOU; its `map_node` type-checks by exact identity, so an `int` arriving at a float-typed node is dropped, which would silently break every Int avatar parameter we watch; and its handler inherits `do_HEAD` from `SimpleHTTPRequestHandler` and serves the process working directory, unfixable without subclassing around the library. The half a library covers is the half VRChat exercises daily; the half we must own stays ours either way.

`watch()` does not reach the served tree either, whatever address it is given — the tree is a hardcoded two-node constant, and nothing turns on the difference for the same one-consumer reason.

## The warrant criterion

The repo holds a few hundred externally-sourced facts, none derivable from first principles: the OSC addresses come from those products' documentation, the SteamVR action-manifest and binding JSON is a hardware contract discovered against SteamVR's binding UI, and the tuned constants are feel-tuned against real hardware under `runtime.md`'s 90% rule. Do not count them by hand: `tests/test_addresses.py` is the census and pins every address verbatim, and `tests/test_settings.py` pins every tuned default.

So: **refactor where the code is the value; preserve where the fact is the value.** Regenerating a tuned constant or an address table spends operator headset time re-proving something that already works — the scarcest resource here, and the reason a whole-repo rewrite loses to a scoped one.

`osc_manager` holds no facts and is nonetheless **kept and fixed in place**, because §OSCQuery interop gaps prices its replacement. `config` is the opposite and every value in it is **preserved**. `utils` is pure math and the headlessly-provable core, so test it hard. Everything else — `engine`, `mapping_base`, `routers`, the `index_*` mappings — is free to restructure, provided the addresses and tuned values move unchanged.

**"Values move unchanged" is not satisfied by copying them.** A derived ladder, a startup mirror that indexes into one, and an encoder that freezes its range as `def`-time defaults all read as unchanged values and are not: each makes a config file half-effective or crashes the package import on a plausible value.

## The camera facts, checked against the vendor packages

Pinning is not verification: a wrong address stays wrong and stays pinned. These were read out of VirtualLens2's and VRCLens's own shipped assets, which is the only thing that licenses changing — or keeping — one of them. All 19 camera addresses are correct; VRChat's `/usercamera/*` nine are live-verified, the rest against the prefabs.

- **A latched write and a pulsed write are both right, because the two products have opposite contracts.** VirtualLens2 fires on the *transition into* a `Control` value and its own state driver returns the parameter to 0, so the same command twice running is still an edge. VRCLens leaves its states on `!= code`, so there the host must clear the parameter. This is why `index_virtuallens` latches, `index_vrclens` pulses, and `VirtualLensSettings` has no `press_duration`.
- **VL2 parameter names carry a space** (`VirtualLens2 Zoom`) and VRChat's OSC interface replaces spaces with underscores. The underscore form in the census is right; matching the prefab's spelling literally would break every one of them.
- **VL2's encoders are not fits — they are that product's inverses.** Its zoom parameter reduces to log-unlerp over the focal range because its internal zoom factor is proportional to focal length; aperture is `ln(Fmax/F)/ln(Fmax/Fmin)`; exposure is linear in EV. The three optical ranges in `VirtualLensSettings` match VL2's shipped defaults, but they stay *per-install* on VL2's side — and VL2 publishes its configured values as parameters, so a mismatch is checkable at runtime rather than only in a prefab.
- **VL2's aperture parameter has no Infinity sentinel.** `x == 0` is Fmax with the depth-of-field pass disabled, because the f-number and the blur-enable flag blend along the same 0..1 parameter. The ladder's floor still earns its keep — it separates "DoF on, minimum blur" from "DoF off" — and the 8-bit question is moot: aperture is unsynced by default, and the compression is on the remote replication path, not the locally driven value.
- **A code that breaks its neighbours' pattern is not thereby wrong.** VRCLens's exposure pair is 108/110 because the value between them is Exposure Reset. Only the vendor's own table settles such a value; the pattern never does.
- **`/usercamera/Zoom` reaches 300, and the published 20–150 range is wrong.** Measured against the live client: the in-client slider echoes up to 300, and clamping to the published ceiling is what produces a reported "does not let me zoom all the way". **Do not re-apply a 150 clamp on the strength of the published range.** Its neighbour `EXPOSURE_MIN_EV`/`MAX_EV` is *not* a protocol range — ±3 EV is this mapping's chosen working range against VRChat's −10…4, and is labelled as the choice it is.
- **The three sets of smooth-scroll constants are identical and not interchangeable.** The lens mappings add the emitted delta straight to a 0–1 parameter; `index_usercamera` multiplies it by a log-range width first. They stay three separate tables for that reason.

**Pulse spacing is bounded at both ends, and the floor is one VRChat frame.** VRChat applies the latest value per parameter per frame rather than queueing — a queue would fall permanently behind under face-tracking volume — so two writes inside one frame collapse to the later, and a trailing 0 sharing a frame with the next value is lost silently. Driving a stepped scroll through a real socket into a timestamped receiver, the separation tracks the configured gap to within half a millisecond: **33.45–33.77 ms at `1/30`, 50.14–50.45 ms at `1/20`**. So `1/30` clears a 30 fps frame by under a millisecond and `1/20` clears it by half again, which is why `PULSE_GAP_SECS` is the latter. The ceiling is feel: a queued train drains at `press_duration` plus the gap.

## The muteproxy contract

`osc_muteproxy`'s pulse on every change, falling edge included, is **its contract, not a defect**: it requires a **latching** source. The reference driving parameter is an unsynced bool the avatar flips both directions — a menu Toggle plus `VRCAvatarParameterDriver` behaviours that set it high in some states and low in others, gated on gesture, contact, and `IsLocal` — so one pulse per transition keeps VRChat's mute state mirrored by the parameter. A **momentary** source is what breaks it: a source that returns to zero on its own double-pulses and nets no change. Say so if the mapping is ever documented for third-party use.

A contact-flippable latch can chatter, and two flips inside one pulse duration would interleave their sends as 1,1,0,0. They serialize as 1,0,1,0 instead, because `press_pulse` queues onto one worker thread per address.

## The wardrobe: the rulings, not the mechanism

`osc_wardrobe` swaps the worn avatar from a button on the wearer's own menu. Only the decisions live here. The wire facts are `osc.md`'s (both directions of `/avatar/change`, the eligibility set, what the echo does and does not carry, the emulator's outbound-only handling); the avatar-side authoring traps are the `osc-wardrobe` entry README's (Modular Avatar's 0–255 clamp on an Int default, the `NotSynced` trap, the saved/synced OR-merge, the label mechanism); and how the mapping works is its own module docstring and comments, which are where someone changing it will be standing.

**It is a wearer-facing feature, and additions are priced against that alone.** This workspace's own probe-swapping loop is not the warrant and needs none of this machinery — one `send("/avatar/change", "avtr_…")` on the library path does it, and §Settled decisions rules that this repo is not the workspace's verification surface. If a component earns nothing from the menu feature, cut it.

**A marker parameter, not a configured list.** The slot→avatar table cannot live on the avatar (the ids are a user's own) and the bridge cannot otherwise know which table a given avatar's menu means, so each manifest carries an `id` that the avatar echoes as an unsynced Int parameter's *default*. That is what lets two avatars carry different wardrobes, and it is the whole justification — do not add "so we can tell a wardrobe-less avatar apart", because an avatar without the menu never sends a non-zero slot and zero is ignored anyway.

**Do not reintroduce a scheduled marker read.** The marker is read on the first press after an avatar change, never on the change itself, because no settling window can be sized: the client acknowledges a change immediately while a cold avatar download runs 30–60 s, so a scheduled read interrogates an avatar that does not exist yet and concludes the wearer has no wardrobe mid-download.

**Do not cache that read either, though for a weaker reason than the schedule.** A press can only arrive from a loaded avatar — while one is loading the wearer is the placeholder, which declares no parameters and emits no OSC — so a cached answer would in fact stay correct as long as every avatar change reaches the invalidation path, including a change the wearer makes from VRChat's own menu and the case where the change filter suppresses a repeated identical echo. Re-reading per press needs none of that reasoning and costs one loopback GET at human press rates. The rule is prefer the version with less to get wrong, not that the cache was broken. A 404 is likewise never remembered, only its log line suppressed.

**Do not re-propose an echo watchdog.** Measured against a live client: the `/avatar/change` echo carries the id we *sent*, arrives within 5 ms, fires identically for an ineligible and for a malformed id, and never fires again on the real load. It acknowledges the request and never reports the outcome, so nothing built on it can separate a working swap from a rejected one, and nothing may treat it as evidence of what is worn. A swap's success is observable on the account's profile page and nowhere on this wire.

**The duplicate guard belongs to a press, not to a slot, because a marker read can outlast the guard's own window.** A read blocks for up to `fetch_timeout_secs`, an order of magnitude past it, so everything a press does after its read is gated on a token proving that press still holds the guard. Matching on the slot alone lets a stalled press release a guard a *later* press of the same slot armed, and that press's duplicate copy then swaps a second time.

**Last press wins: a press superseded while its marker was being read abandons instead of sending.** Sending anyway lands the wearer on the avatar they pressed *first* — press slot 1, press slot 3 while slot 1 is still reading, and the swaps arrive in the order the reads finished rather than the order the buttons were pressed. This is the only place the mapping discards a press it had already accepted, and the same token is what detects it.

**A successful swap immediately un-arms the wardrobe, and that is correct.** Our own send is echoed, the echo is indistinguishable from any other avatar change, and the avatar really did change. Do not "fix" it by filtering our own id out of the echo: the same address carries a change made from VRChat's own menu, and telling them apart would mean trusting the id we sent, which the ruling above forbids.

## Provenance

`osc_leash` was a literal port of OSCLeash (MIT, © 2022 ZenithVal) carrying no notice. The evidence is the finding, so it is recorded rather than summarised: the same movement formula, the same `Y_Combined` up/down deadzone, the same divide-by-`Y_Modifier` compensation, the same three `/input/` outputs, and two of three tuning constants identical. Deleting it ends the obligation forward, and the history that carried it is scrubbable and not preserved for attribution's sake. Its replacement is a `vrc-patterns` entry rebuilding the leash on face-proximity box receivers rather than OSCLeash's six-sphere direction cage: `box-tracker` establishes the mechanism, and the open design question is how far below six contacts an axis-separable readout gets. Until that ships there is no leash in this repo.

Every other named project here is an interface, not an ancestor, and each carries a link from the README's §Interoperates-with rather than a notice: VirtualLens2, VRCLens, OSCmooth, VRCFaceTracking, and Voicemeeter.
