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
| Parameter discovery is **descoped** | Discovery serves an observer poking an avatar they did not author. A user here owns both ends and already knows the names. It arrives free if `python-oscquery` is adopted; never build it standalone. |
| `index_remy` keeps its name, labeled as a personal-integration example | Lazy-imported, `httpx`/`pillow` moved to an optional extra. |
| Named ancestors get **links, not notices** | We interface with OSCmooth, VirtualLens2, VRCLens, VRCFaceTracking; we borrowed code from none of them. |
| `osc_leash` is **deleted** | It was the sole code borrow (below). Its replacement is designed as a `vrc-patterns` entry on face-proximity boxes, not as a mapping rewrite. |
| Design record lives here; `gimmicks.md` carries the only route in | A product's design record travels with the product. No new first-hop doc, no `docs/` owner in the meta-repo. |

## What is proven, and what has never been exercised

The handshake is solid in daily use, but the proven set is narrower than "it works" suggests, and the gap is the whole case for replacing `osc_manager`:

- **Proven in daily use:** the mDNS browser finds VRChat and other `_oscjson._tcp` services; VRChat accepts our advertisement and sends avatar params.
- **Proven headlessly** (`tests/test_osc_roundtrip.py`, against a fake VRChat on loopback): the HOST_INFO client parses `OSC_PORT` and returns `None` rather than raising when nothing answers; a selected target actually receives datagrams; a mapping's sends land at the addresses it declares; an inbound value updates a watched mirror and only a *changed* value wakes the listener.
- **Still never exercised:** any client reading *our* tree, and mDNS discovery itself — deliberately not faked, since browsing a real network from a test is flaky and proves nothing that pointing at a known port does not. VRChat is the tree's only consumer and tolerates every non-conformance below.

**Trap:** running alongside VRCFaceTracking proves nothing about our server. `osc_vrcft` consumes VRCFT's mDNS *advertisement* via `is_service_running`; VRCFT never reads our tree, and `_service_rank` scores it below VRChat so it is never a send target.

## OSCQuery interop gaps (measured black-box against the running server)

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

`watch()` does not reach the served tree. The tree is a hardcoded two-node constant, so `watch()`'s own docstring still claims otherwise — left alone, because step 7 deletes the file that holds it. `engine.on_osc`'s matching comment is corrected, and `tests/test_osc_roundtrip.py` pins the two-node shape so step 7 has something concrete to change.

## The warrant criterion

The repo holds a few hundred externally-sourced facts: **48 distinct OSC addresses** (32 written as literals, 16 more derived by the puppet codec), the tuned values now gathered in `settings.py`, and 124 lines of SteamVR action-manifest and binding JSON in `config.py`. None is derivable from first principles: addresses come from those products' documentation, the binding JSON is a hardware contract discovered against SteamVR's binding UI, the constants are feel-tuned against real hardware and fall under `runtime.md`'s 90% rule.

Do not maintain those counts by hand — an earlier revision's figures were taken before `osc_leash` was deleted and were wrong within a day. `tests/test_addresses.py` is the census and pins every address verbatim; `tests/test_settings.py` pins every tuned default against the value it had before the config file existed.

So: **refactor where the code is the value; preserve where the fact is the value.** Regenerating a tuned constant or an address table spends operator headset time re-proving something that already works — the scarcest resource here, and the reason a whole-repo rewrite loses to a scoped one.

| Module | Facts held | Warrant |
|---|---|---|
| `osc_manager` | none | **Replace** with `python-oscquery` (MIT, `>=3.10`; adds only `requests`, and covers the client half we lack) |
| `engine`, `mapping_base` | none | Restructure freely — config and entry-point seams land here |
| `utils` | none; pure math | Restructure, and test hard: this is the headlessly-provable core |
| `index_puppet` | 4 bases plus a derivation algorithm | Logic-heavy, so free to restructure; `_quant_encode_unit` is the float→bool codec `gimmicks.md` documents and the first thing to pin with tests |
| `routers` | 4 addresses | Light: policy code with stale docstrings |
| `config` | 124 JSON lines plus tuning | **Preserve every value**; relocate to the config file |
| `controller_manager` | consumes `config`'s hardware contract | Preserve behavior; cover with tests |
| `index_usercamera`, `index_virtuallens`, `index_vrclens` | 19 addresses | Audited (step 1); addresses pinned, logic questioned — findings folded into the steps below |
| `osc_vrcft`, `osc_muteproxy` | 7 addresses | Small |
| `index_remy` | 2 addresses | Keep, label, lazy-import; four defects below |

## What the audit changed

The three camera mappings have now been read line by line. The audit paid, and four of its findings changed a later step rather than merely adding to it — recorded here because each is the kind of thing a reader would otherwise re-derive or, worse, undo:

- **A wrong externally-sourced fact.** `ZOOM_MAX_MM` was 300 under a "from VRChat docs" header; the published range is 20–150. The ladder's top two rungs were unreachable, smooth zoom ran ~34% hot, and at the ceiling the optimistic mirror fought VRChat's clamp. Corrected from the vendor table, which costs no headset time. Its neighbour `EXPOSURE_MIN_EV`/`MAX_EV` is *not* a protocol range — ±3 EV is this mapping's chosen working range against VRChat's −10…4, and is now labelled as the choice it is.
- **"Values move unchanged" was not sufficient for the config file.** Two ladders were derived at import, two startup mirrors were indexes into a derived ladder, and one encoder froze its range as `def`-time defaults. Each would have made a config file half-effective or crashed the package import on a plausible value.
- **The three sets of smooth-scroll constants are identical and not interchangeable.** The lens mappings add the emitted delta straight to a 0–1 parameter; `index_usercamera` multiplies it by a log-range width first. They stay three separate tables for that reason.
- **Both lens mappings' docstrings named the wrong hands.** `index_vrclens` had all four press rows transposed left↔right, `index_virtuallens` three of four rotated, while `index_usercamera`'s matched. A test author trusting either would have pinned the wrong button — which is exactly the failure the next paragraph exists to prevent.

**Intent before test, on every step.** The suite is thin enough that tests written against observed behavior would freeze known defects as expected. For each defect an audit surfaces, state intended behavior first, then assert intent (`CLAUDE.md` rule 5). This is the one discipline the audit-and-refactor path needs that a from-scratch rewrite would get for free — skipping it is how the refactor silently becomes a bug-preservation exercise. Where intent could not be settled from the source, the test **pins** the value and says so: pinning preserves, and claims nothing about correctness. `index_vrclens`'s `FEATURE_EXPOSURE_PLUS = 110` is the live example — it breaks the pattern its neighbouring pair sets, and is kept verbatim as tested working code.

## Sequenced plan

**Landed.** The audit (1) and, on its findings, the config file (2), the Remy lazy-import and optional extra (3), the extension seam (4), the test harness (5), fail-loud (8), the truth pass (9), ancestor links (10), packaging (11) and grammar (12). Two additions the audit argued for and the operator approved: correcting VRChat's Zoom range, and moving `press_pulse` off the controller thread. The four `index_remy` defects below are fixed.

**Remaining, and why.** Both touch `osc_manager`, and both sit behind an operator smoke test that avatar *and* `/usercamera/*` params still arrive — the router cannot activate `index_usercamera` at all without inbound `/usercamera/Mode`.

6. **OSCQuery interop fixes**, per the table above — or skip entirely in favour of 7.
7. **Replace `osc_manager` with `python-oscquery`.** Preferred over 6: it deletes untested networking rather than patching it.

Three things step 7 inherits, none of them blocking:

- `watch()`'s docstring still claims it reaches the served tree. Left false on purpose: correcting a docstring in a file that is about to be deleted is wasted work, and `tests/test_osc_roundtrip.py` pins the real two-node shape.
- `OSCManager.stop()` costs ~2s (Zeroconf close plus two 1s thread joins), which the round-trip tests pay per test. A `discover=False` option would remove it for embedders as well as tests.
- `_consider_service` compares a candidate's rank against the *current* target's and returns on a tie, so a VRChat that restarts on a new OSC port under the same service name is ignored unless `remove_service` fires first.

## The muteproxy contract, and a ruling that needed narrowing

`osc_muteproxy`'s pulse on every change, falling edge included, is **its contract, not a defect**: it requires a **latching** source. The reference driving parameter is an unsynced bool the avatar flips both directions — a menu Toggle plus `VRCAvatarParameterDriver` behaviours that set it high in some states and low in others, gated on gesture, contact, and `IsLocal` — so one pulse per transition keeps VRChat's mute state mirrored by the parameter. A **momentary** source is what breaks it: a source that returns to zero on its own double-pulses and nets no change. Say so if the mapping is ever documented for third-party use.

The narrow residual is closed: a contact-flippable latch could chatter, and two flips inside one pulse duration used to interleave their sends as 1,1,0,0. `press_pulse` now queues onto one worker thread, so overlapping pulses serialize as 1,0,1,0.

**The blocking-sleep ruling was right about the case it examined and wrong as stated.** It read: a blocking `time.sleep` inside an OSC callback is not a defect, because dispatch is per-datagram threaded. That measurement holds, and `osc_vrcft`'s load delay is still free. But the axis that matters is *which* thread, not which kind of callback, and **controller** callbacks run synchronously on the single `ControllerLoop` thread: `_poll_once` → `_emit` → the listener, no handoff. Every caller of `press_pulse` was on that path, so each sleep froze input polling — up to 0.4 s for a diagonal left-pad drag, which is exactly `long_press_threshold`, long enough for a following tap to be reclassified and fire the wrong VRCLens feature. Fixed by making `press_pulse` asynchronous. State the rule by thread: **the OSC datagram path tolerates a sleep; the controller path does not.**

## Provenance

`osc_leash` was a literal port of OSCLeash (MIT, © 2022 ZenithVal) carrying no notice: same movement formula, same `Y_Combined` up/down deadzone, same divide-by-`Y_Modifier` compensation, same three `/input/` outputs, and two of three tuning constants identical. Deleting it ends the obligation forward; the history that carried it is scrubbable and not preserved for attribution's sake.

Its replacement is a `vrc-patterns` entry rebuilding the leash on face-proximity box receivers rather than OSCLeash's six-sphere direction cage — `box-tracker` establishes the mechanism, and the design question is how far below six contacts an axis-separable readout gets. Until that ships there is no leash in this repo.

Every other named project here is an interface, not an ancestor, and each carries a link from the README's §Interoperates-with rather than a notice: VirtualLens2, VRCLens, OSCmooth, VRCFaceTracking, and Voicemeeter.
