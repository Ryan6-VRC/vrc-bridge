# vrc-bridge — design

Primary reader: agent.

**Cite by path, never by SHA.** This repo's history is slated for re-creation when it is packaged as a product, so a commit reference recorded anywhere — here, a PR body, a handoff — rots. Name the file and the symbol.

vrc-bridge is a **product**: the SteamVR-input to VRChat-OSC substrate for people building OSC-driven gimmicks. The controller layer is the load-bearing half nothing else supplies, and the camera mappings are both shipped features and the worked examples that teach the substrate. It is not a personal input rig to be minimized, and it is **not** the workspace's internal verification surface — that question is settled below, not open.

The contract is with third-party mapping authors, not with this workspace's gates. `docs/verify.md` governs claims about *avatars*; this repo's claims are about software, gated by ordinary tests. "Only provable in a live client" describes the whole VRChat tooling ecosystem, not a defect in this repo.

## Settled decisions

Do not relitigate these; they are the operator's, taken with the verdict.

| Decision | Consequence |
|---|---|
| Library **and** application, with a declared seam | The library path is the documented extension route; a `[project.entry-points]` router group makes third-party routers reachable from the CLI. |
| A config file replaces module-level constants | Retuning must not require editing installed source. Current values become the defaults, unchanged. |
| Parameter discovery is **descoped** | Discovery serves an observer poking an avatar they did not author. A user here owns both ends and already knows the names. Never build it standalone — and the dependency that would have carried it in free is declined below, so it is not arriving by that route either. |
| `index_remy` keeps its name, labeled as a personal-integration example | Lazy-imported, `httpx`/`pillow` moved to an optional extra. |
| Named ancestors get **links, not notices** | We interface with OSCmooth, VirtualLens2, VRCLens, VRCFaceTracking; we borrowed code from none of them. |
| `osc_leash` is **deleted** | It was the sole code borrow (below). Its replacement is designed as a `vrc-patterns` entry on face-proximity boxes, not as a mapping rewrite. |
| Design record lives here; `gimmicks.md` carries the only route in | A product's design record travels with the product. No new first-hop doc, no `docs/` owner in the meta-repo. |

## What is proven, and what has never been exercised

The handshake is solid in daily use, but the proven set is narrower than "it works" suggests, and knowing exactly where the edge sits is what keeps a change from being aimed at the wrong half:

- **Proven in daily use:** the mDNS browser finds VRChat and other `_oscjson._tcp` services; VRChat accepts our advertisement and sends avatar params.
- **Proven headlessly** (`tests/test_osc_roundtrip.py`, against a fake VRChat on loopback): the HOST_INFO client parses `OSC_PORT` and returns `None` rather than raising when nothing answers; a selected target actually receives datagrams; a mapping's sends land at the addresses it declares; an inbound value updates a watched mirror and only a *changed* value wakes the listener.
- **Also proven headlessly** (`tests/test_target_selection.py`): target *selection* — which discovered service wins the slot, that only a strictly better rank unseats an incumbent, that a republication of the current service is followed to its new port, and that our own advertisement is never targeted. Selection separates from discovery because `_consider_service` takes a name and a `ServiceInfo` and decides, so handing it one directly needs no network.
- **Still never exercised:** any client reading *our* tree, and mDNS discovery itself — the `_BrowserListener` wiring, meaning whether zeroconf calls those methods when we expect. Deliberately not faked, since browsing a real network from a test is flaky and proves nothing that pointing at a known port does not. VRChat is the tree's only consumer and tolerates every non-conformance below.

**Trap:** running alongside VRCFaceTracking proves nothing about our server. `osc_vrcft` consumes VRCFT's mDNS *advertisement* via `is_service_running`; VRCFT never reads our tree, and `_service_rank` scores it below VRChat so it is never a send target.

## OSCQuery interop gaps (measured black-box against the running server)

**Characterized and deliberately tolerated — not a defect list, and not pending work.** Each row below is real, and closing any of them buys nothing, for the reason stated after the table. Read that ruling before proposing a fix, or the table reads as a to-do and the work gets done twice.

Latent, not live: VRChat tolerates all of these, and anything that is not VRChat will not.

| Request | Here | `vrchat-community/vrc-oscquery-lib` |
|---|---|---|
| `/` | no root `FULL_PATH`; `CONTENTS` only | full node with `FULL_PATH`/`TYPE`/`ACCESS` |
| `/?HOST_INFO` | `OSC_PORT` alone | plus `OSC_IP`, `OSC_TRANSPORT`, `NAME`, `EXTENSIONS` |
| `/?HOST_INFO=1` | `404` — the path is matched by string equality | served; matched by substring |
| `/avatar` — a path we advertise ourselves | `404`, empty body | serves any registered node |
| `/#fragment` | `404` | resolved via `Url.LocalPath` |
| 404 body, `pragma: no-cache` | empty, absent | `"OSC Path not found"`, set |

Two claims about that reference are wrong in the other direction and should not be repeated: its `GetAvailableTcpPort()` binds port 0, reads the number, then **closes** the socket, leaving a TOCTOU window — binding port 0 on the real server socket and keeping it, as here, is the more correct of the two. And it does implement `VALUE` as a node attribute and advertises it in `EXTENSIONS`; what it lacks is a `?VALUE` query *filter*.

**The ruling: close none of them, and do not adopt a library to close them.** The tree has exactly one consumer. VRChat reads `/?HOST_INFO` for our OSC port and then sends; it does not read `CONTENTS` to decide what to send, which is why every row above has been survivable for the product's whole life. Third-party mapping authors — the audience §Settled decisions commits to — consume the Python API, not the served tree. So conformance here is work against a hypothetical consumer, and `CLAUDE.md` rule 2 disposes of it.

That ruling was reached by pricing the alternative rather than by asserting it, because the sequenced plan below once preferred adopting `python-oscquery` outright. Measured against 0.4.0: its low-level `OSCQueryHTTPServer` plus `OSCAddressSpace` does close five of the six rows for free — only the `pragma` header stays open — and `add_node` builds intermediate containers, which would make `watch()` feed the tree. Its own `OSCQueryClient` reads that server and **cannot read ours**, raising `KeyError: 'NAME'` on HOST_INFO and `ValueError` walking `/`, which is the first hard evidence that a conformant client fails against us rather than degrading. Against that: `OSCQueryHTTPHandler` extends `SimpleHTTPRequestHandler` and overrides only `do_GET`, so the inherited `do_HEAD` serves the process working directory — `HEAD /secret.txt` answers `200` with the real `Content-Length`, and under `OSCQueryService`'s all-interfaces bind that is a LAN-reachable file-existence-and-size oracle. Our own handler answers `501`. Adopting means importing that, unfixable without subclassing around the library, to use roughly a third of it while still writing the Zeroconf setup, the browser, the ranking and the HOST_INFO client ourselves.

**The asymmetry is what settles it, and it holds under any future re-examination:** adoption never shrinks the surface that needs testing. `OSCQueryBrowser` is poll-only, constructs a bare `Zeroconf()` that would undo the interface pin in `start()`, and has no counterpart for `_consider_service`, `_service_rank` or `is_service_running`. The half a library covers is the half VRChat exercises daily; the half we must own stays ours either way. Two further wrappers are actively worse than what we have: `OSCQueryService` binds HTTP to every interface and advertises `http_port` literally, so port 0 announces `0` and forces the pre-bind TOCTOU noted above, and `map_node` type-checks by exact identity — measured live, an `int` arriving at a float-typed node is dropped, which would silently break every Int avatar parameter we watch.

`watch()` does not reach the served tree, whatever address it is given; the tree is the hardcoded two-node constant in `_make_http_handler`. Its docstring says so, `engine.on_osc`'s comment matches, and `tests/test_osc_roundtrip.py` pins the two-node shape.

**Every inbound message is delivered twice, from two UDP sender sockets — and whether that is VRChat's doing or our own mDNS configuration is not yet settled.** The measurement is solid: a probe recording source endpoints saw 8,024 datagrams over 20 s from exactly two endpoints, 4,012 each, and of 1,107 distinct payloads every one arrived twice with **zero** duplicated from the same endpoint, the two sender ports sitting immediately above the listener's. Different source ports mean two genuine sends, which rules out socket-layer duplication and a doubled log handler alike — so reach for the source endpoint, not for duplicate counts, if this is re-tested.

The attribution's leading suspect was `start()`'s bare `Zeroconf()`, which defaults to `InterfaceChoice.All` and announced on every interface the host has while the record itself carried a single loopback address. A client that opens a sender per announcement would produce exactly two on a host whose loopback and LAN adapters both reach it — the observed signature. Ruled out already: the service record is not itself duplicated, and there is only one candidate destination (one service, one address, `VRBridge.local.` resolving to `127.0.0.1` alone), so the missing `OSC_IP` in `?HOST_INFO` above is not implicated here.

**The announcement half is fixed and proven; the client half is not.** `start()` now pins the announcement to the interface it serves on, and the effect is measurable with no client at all: `len(Zeroconf().engine.senders)` is 4 bare against 1 pinned on a host with loopback, a Hyper-V switch, Ethernet and Tailscale up. What that does **not** establish is whether the pin collapses the doubled *inbound*, which needs a signed-in client and has not been run. **Measure the sender count, never a count of reachable interfaces** — an earlier reading treated interface agreement as the evidence and got lucky, because `ifaddr` offered 8 addresses on the probe host while zeroconf bound only the 4 whose adapters were up.

What remains is one measurement against a live client: re-run the source-endpoint probe now that the announcement is pinned, with a third-party OSCQuery receiver against the same client as a control. Two senders still arriving would make this a client behaviour to tolerate rather than our own misconfiguration. **Until that is run, treat inbound as doubled:** every inbound-triggered callback fires twice, so a non-idempotent listener double-applies. Do not reach for a dedupe window first — it would mask the very signal the probe reads. Inbound only: outbound sends appear once. One client build (`VRChat-Client-161618`), one machine.

## The warrant criterion

The repo holds a few hundred externally-sourced facts: **48 distinct OSC addresses** (32 written as literals, 16 more derived by the puppet codec), the tuned values now gathered in `settings.py`, and 124 lines of SteamVR action-manifest and binding JSON in `config.py`. None is derivable from first principles: addresses come from those products' documentation, the binding JSON is a hardware contract discovered against SteamVR's binding UI, the constants are feel-tuned against real hardware and fall under `runtime.md`'s 90% rule.

Do not maintain those counts by hand — an earlier revision's figures were taken before `osc_leash` was deleted and were wrong within a day. `tests/test_addresses.py` is the census and pins every address verbatim; `tests/test_settings.py` pins every tuned default against the value it had before the config file existed.

So: **refactor where the code is the value; preserve where the fact is the value.** Regenerating a tuned constant or an address table spends operator headset time re-proving something that already works — the scarcest resource here, and the reason a whole-repo rewrite loses to a scoped one.

| Module | Facts held | Warrant |
|---|---|---|
| `osc_manager` | none | **Keep and fix in place.** Replacing it with `python-oscquery` was measured and declined — §OSCQuery interop gaps holds the evidence and the ruling |
| `engine`, `mapping_base` | none | Restructure freely — config and entry-point seams land here |
| `utils` | none; pure math | Restructure, and test hard: this is the headlessly-provable core |
| `index_puppet` | 4 bases plus a derivation algorithm | Logic-heavy, so free to restructure; `_quant_encode_unit` is the float→bool codec `gimmicks.md` documents and the first thing to pin with tests |
| `routers` | 4 addresses | Light: policy code with stale docstrings |
| `config` | 124 JSON lines plus tuning | **Preserve every value**; relocate to the config file |
| `controller_manager` | consumes `config`'s hardware contract | Preserve behavior; cover with tests |
| `index_usercamera`, `index_virtuallens`, `index_vrclens` | 19 addresses | Audited (step 1) and since **verified against the vendor packages** — see below; all 19 correct |
| `osc_vrcft`, `osc_muteproxy` | 7 addresses | Small |
| `index_remy` | 2 addresses | Keep, label, lazy-import; four defects below |

## The camera facts, checked against the vendor packages

Pinning is not verification: a wrong address stays wrong and stays pinned. These were read out of VirtualLens2's and VRCLens's own shipped assets, which is the only thing that licenses changing — or keeping — one of them. All 19 camera addresses are correct; VRChat's `/usercamera/*` nine are live-verified, the rest against the prefabs.

- **A latched write and a pulsed write are both right, because the two products have opposite contracts.** VirtualLens2 fires on the *transition into* a `Control` value and its own state driver returns the parameter to 0, so the same command twice running is still an edge. VRCLens leaves its states on `!= code`, so there the host must clear the parameter. This is why `index_virtuallens` latches, `index_vrclens` pulses, and `VirtualLensSettings` has no `press_duration`.
- **VL2 parameter names carry a space** (`VirtualLens2 Zoom`) and VRChat's OSC interface replaces spaces with underscores. The underscore form in the census is right; matching the prefab's spelling literally would break every one of them.
- **VL2's encoders are not fits — they are that product's inverses.** Its zoom parameter reduces to log-unlerp over the focal range because its internal zoom factor is proportional to focal length; aperture is `ln(Fmax/F)/ln(Fmax/Fmin)`; exposure is linear in EV. The three optical ranges in `VirtualLensSettings` match VL2's shipped defaults, but they stay *per-install* on VL2's side — and VL2 publishes its configured values as parameters, so a mismatch is checkable at runtime rather than only in a prefab.
- **VL2's aperture parameter has no Infinity sentinel.** `x == 0` is Fmax with the depth-of-field pass disabled, because the f-number and the blur-enable flag blend along the same 0..1 parameter. The ladder's floor still earns its keep — it separates "DoF on, minimum blur" from "DoF off" — and the 8-bit question is moot: aperture is unsynced by default, and the compression is on the remote replication path, not the locally driven value.
- **A code that breaks its neighbours' pattern is not thereby wrong.** VRCLens's exposure pair is 108/110 because the value between them is Exposure Reset. Only the vendor's own table settles such a value; the pattern never does.

**Pulse spacing is bounded at both ends, and the floor is one VRChat frame.** VRChat applies the latest value per parameter per frame rather than queueing — a queue would fall permanently behind under face-tracking volume — so two writes inside one frame collapse to the later, and a trailing 0 sharing a frame with the next value is lost silently. Driving a stepped scroll through a real socket into a timestamped receiver, the separation tracks the configured gap to within half a millisecond: **33.45–33.77 ms at `1/30`, 50.14–50.45 ms at `1/20`**. So `1/30` clears a 30 fps frame by under a millisecond and `1/20` clears it by half again, which is why `PULSE_GAP_SECS` is the latter. The ceiling is feel: a queued train drains at `press_duration` plus the gap.

## What the audit changed

The three camera mappings have now been read line by line. The audit paid, and four of its findings changed a later step rather than merely adding to it — recorded here because each is the kind of thing a reader would otherwise re-derive or, worse, undo:

- **A fact the published table gets wrong, and the audit's "correction" was the error.** `ZOOM_MAX_MM` sat at 300 under a "from VRChat docs" header, and the audit clamped it to 150 on the published 20–150 range. Measurement against the live client overturned that: the in-client slider echoes `/usercamera/Zoom` up to 300, the clamp was what produced the reported "does not let me zoom all the way", and `settings.py` carries `zoom_max_mm: float = 300.0` today with a docstring recording the measurement. **Do not re-apply the 150 clamp on the strength of the published range.** Its neighbour `EXPOSURE_MIN_EV`/`MAX_EV` is *not* a protocol range — ±3 EV is this mapping's chosen working range against VRChat's −10…4, and is now labelled as the choice it is.
- **"Values move unchanged" was not sufficient for the config file.** Two ladders were derived at import, two startup mirrors were indexes into a derived ladder, and one encoder froze its range as `def`-time defaults. Each would have made a config file half-effective or crashed the package import on a plausible value.
- **The three sets of smooth-scroll constants are identical and not interchangeable.** The lens mappings add the emitted delta straight to a 0–1 parameter; `index_usercamera` multiplies it by a log-range width first. They stay three separate tables for that reason.
- **Both lens mappings' docstrings named the wrong hands.** `index_vrclens` had all four press rows transposed left↔right, `index_virtuallens` three of four rotated, while `index_usercamera`'s matched. A test author trusting either would have pinned the wrong button — which is exactly the failure the next paragraph exists to prevent.

**Intent before test, on every step.** The suite is thin enough that tests written against observed behavior would freeze known defects as expected. For each defect an audit surfaces, state intended behavior first, then assert intent (`CLAUDE.md` rule 5). This is the one discipline the audit-and-refactor path needs that a from-scratch rewrite would get for free — skipping it is how the refactor silently becomes a bug-preservation exercise. Where intent could not be settled from the source, the test **pins** the value and says so: pinning preserves, and claims nothing about correctness. `index_vrclens`'s `FEATURE_EXPOSURE_PLUS = 110` is the live example — it breaks the pattern its neighbouring pair sets, and is kept verbatim as tested working code.

## Sequenced plan

**Landed.** The audit (1) and, on its findings, the config file (2), the Remy lazy-import and optional extra (3), the extension seam (4), the test harness (5), fail-loud (8), the truth pass (9), ancestor links (10), packaging (11) and grammar (12). Two additions the audit argued for and the operator approved: correcting VRChat's Zoom range, and moving `press_pulse` off the controller thread. The four `index_remy` defects below are fixed.

**Nothing remains. Steps 6 and 7 are both answered "no", and the three defects they were carrying are fixed.** Neither needed the operator smoke test they were held behind, because nothing that reaches VRChat changed shape.

6. **OSCQuery interop fixes** — declined. §OSCQuery interop gaps holds the ruling: one consumer, which does not read the part that is non-conformant.
7. **Replace `osc_manager` with `python-oscquery`** — declined, measured against 0.4.0. The premise was that it deletes untested networking; it deletes the *tested* half instead, and the same section prices what it would have bought against what it would have imported.

The three defects that were riding on those steps, and how each was settled:

- `watch()`'s docstring claimed it reaches the served tree. Corrected to state what it does, and to route to the ruling above rather than restate it.
- **`OSCManager.stop()`'s cost is `poll_interval`, not thread joins.** `serve_forever()` notices `shutdown()` only between polls, so the interval *is* each server's teardown cost; the joins measure 0.000s, because `shutdown()` already blocks until the loop exits. Priced at the 0.5s default: `unregister_service` 0.001s, `zeroconf.close()` 0.268s, HTTP 0.437s, OSC 0.514s. The Zeroconf share is paid only when advertising, so a `discover=False` option — once proposed as the remedy — buys the tests nothing, since they already run `advertise=False`. `_SERVE_POLL_SECS` is the lever, and takes a non-advertising `stop()` from 0.824s to 0.106s.
- **`_consider_service` refused a tie, so it could not follow its own target.** VRChat keeps its service name across a restart and returns on a fresh OSC port; the ranks tied, the tie was refused, and sends kept going to the dead port unless `remove_service` happened to fire first. A republication of the current target is now treated as an update and always re-resolved, while a rival still needs a strictly better rank. Three consequences worth knowing before touching it again: the incumbent's rank is stored rather than recomputed, because `_service_rank` also reads the mDNS server string and that is not retained; an unchanged republication is detected and dropped, or every mDNS refresh would rebuild the socket; and following the incumbent puts a blocking `_host_info` on zeroconf's single dispatch thread once per record refresh, stalling every service callback for its duration. That cost is accepted rather than resolved off-thread — `ServiceBrowser` serialises callbacks through one queue, and that serialisation is what lets `_consider_service` read and write its target state without holding the lock across the HTTP query. **A candidate that fails to resolve must leave the current target standing**, which is the case `tests/test_target_selection.py` covers from both the incumbent and the rival direction: dropping a live client over one malformed reply would be worse than the tie this replaced.

## The muteproxy contract, and a ruling that needed narrowing

`osc_muteproxy`'s pulse on every change, falling edge included, is **its contract, not a defect**: it requires a **latching** source. The reference driving parameter is an unsynced bool the avatar flips both directions — a menu Toggle plus `VRCAvatarParameterDriver` behaviours that set it high in some states and low in others, gated on gesture, contact, and `IsLocal` — so one pulse per transition keeps VRChat's mute state mirrored by the parameter. A **momentary** source is what breaks it: a source that returns to zero on its own double-pulses and nets no change. Say so if the mapping is ever documented for third-party use.

The narrow residual is closed: a contact-flippable latch could chatter, and two flips inside one pulse duration used to interleave their sends as 1,1,0,0. `press_pulse` now queues onto one worker thread, so overlapping pulses serialize as 1,0,1,0.

**The blocking-sleep ruling was right about the case it examined and wrong as stated.** It read: a blocking `time.sleep` inside an OSC callback is not a defect, because dispatch is per-datagram threaded. That measurement holds, and `osc_vrcft`'s load delay is still free. But the axis that matters is *which* thread, not which kind of callback, and **controller** callbacks run synchronously on the single `ControllerLoop` thread: `_poll_once` → `_emit` → the listener, no handoff. Every caller of `press_pulse` was on that path, so each sleep froze input polling — up to 0.4 s for a diagonal left-pad drag, which is exactly `long_press_threshold`, long enough for a following tap to be reclassified and fire the wrong VRCLens feature. Fixed by making `press_pulse` asynchronous. State the rule by thread: **the OSC datagram path tolerates a sleep; the controller path does not.**

## Provenance

`osc_leash` was a literal port of OSCLeash (MIT, © 2022 ZenithVal) carrying no notice: same movement formula, same `Y_Combined` up/down deadzone, same divide-by-`Y_Modifier` compensation, same three `/input/` outputs, and two of three tuning constants identical. Deleting it ends the obligation forward; the history that carried it is scrubbable and not preserved for attribution's sake.

Its replacement is a `vrc-patterns` entry rebuilding the leash on face-proximity box receivers rather than OSCLeash's six-sphere direction cage — `box-tracker` establishes the mechanism, and the design question is how far below six contacts an axis-separable readout gets. Until that ships there is no leash in this repo.

Every other named project here is an interface, not an ancestor, and each carries a link from the README's §Interoperates-with rather than a notice: VirtualLens2, VRCLens, OSCmooth, VRCFaceTracking, and Voicemeeter.
