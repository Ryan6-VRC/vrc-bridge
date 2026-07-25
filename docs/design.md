# vrc-bridge — design

Primary reader: agent.

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

- **Proven:** the mDNS browser finds VRChat and other `_oscjson._tcp` services; the HOST_INFO *client* parses VRChat's `OSC_PORT`; VRChat accepts our advertisement and sends avatar params.
- **Never exercised:** any client reading *our* tree. VRChat is its only consumer and tolerates every non-conformance below.

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

`watch()` does not reach the served tree. The tree is a hardcoded two-node constant, so the docstring on `watch()` and the comment in `engine.on_osc` that claim otherwise are false.

## The warrant criterion

The repo holds **~242 externally-sourced facts** — 52 distinct OSC addresses, 66 module-level tuned constants, 124 lines of SteamVR action-manifest and binding JSON. None is derivable from first principles: addresses come from those products' documentation, the binding JSON is a hardware contract discovered against SteamVR's binding UI, the constants are feel-tuned against real hardware and fall under `runtime.md`'s 90% rule.

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
| `index_usercamera`, `index_virtuallens`, `index_vrclens` | 21 addresses | **Unaudited.** Audit first; preserve addresses, question logic |
| `osc_vrcft`, `osc_muteproxy` | 7 addresses | Small |
| `index_remy` | 2 addresses | Keep, label, lazy-import; four defects below |

## Audit before plan

This document's findings are **seed evidence that an audit pays, not an inventory.** They came from reading with a verdict in mind. `index_usercamera`, `index_virtuallens`, and `index_vrclens` — 562 lines, 21 of the 52 addresses, three mappings in daily use — have never been read line by line. Auditing them is the highest information-per-hour work in the repo and precedes any decision about restructuring them.

**Intent before test, on every step.** The suite is thin enough that tests written against observed behavior would freeze known defects as expected. For each defect an audit surfaces, state intended behavior here first, then assert intent (`CLAUDE.md` rule 5). This is the one discipline the audit-and-refactor path needs that a from-scratch rewrite would get for free — skipping it is how the refactor silently becomes a bug-preservation exercise.

## Sequenced plan

Each step is independently dispatchable. Ordering constraints are real; the numbering is not.

1. **Audit the three unread mappings.** Emits its own findings; blocks any restructuring of them.
2. **Config file.** The largest product gap: today retuning scroll inversion or a press threshold means editing installed source. Values move unchanged.
3. **Remy lazy-import + optional extra.** `mappings/__init__.py` imports it unconditionally, so every launch of every router loads `httpx` and `PIL` today.
4. **Extension seam.** Document the library path; add the entry-point router group.
5. **Test harness.** A fake-VRChat double — OSCQuery HTTP plus an OSC receiver on loopback, in the shape of `vrc-mcp-proxy/tests/fake_upstream.py` — then the pure core: `_quant_encode_unit`, `_derive_bool_addrs`, `FloatAxisSmoother`, `SmoothScroller`, `step_param`, `ParamState`. Delete the two tests that assert a removed mapping stayed removed. **Gates 6 and 7.**
6. **OSCQuery interop fixes**, per the table above — or skip entirely in favour of 7.
7. **Replace `osc_manager` with `python-oscquery`.** Preferred over 6: it deletes untested networking rather than patching it. Behind 5, and behind one operator smoke test that params still arrive.
8. **Fail loud.** `MappingRouter.run_forever` swallows every `update()` exception with a bare `pass`, so a mapping throwing each tick is invisible (`CLAUDE.md` rule 7).
9. **Truth pass** on the false comments: the two OSCQuery-tree claims above, `FullRouter`'s docstring crediting itself with a registration its base performs, `DefaultRouter`'s "MuteProxy remains always-on" when Leash and VRCFT are too, `index_remy`'s "if Pillow is available" against a hard top-level import, and its never-read `RESIZE_ON_UPLOAD`.
10. **Ancestor links.**
11. **Packaging.** `get_files_dir()` resolves inside the Python tree on a non-editable install; the generated `.vrmanifest` names `controller_manager.py`, which has no `__main__`, so a SteamVR auto-launch runs nothing; `arguments` double-escapes its quotes.
12. **Grammar.** `osc_muteproxy` declares `name = "index_muteproxy"`, the one mapping whose name disagrees with its file; the README omits that `--router camera` drops Leash and VRCFT.

## Findings the audit inherits — defects, and one settled non-defect

`index_remy`: `_set_audio_mode` opens with `if mode == self._audio_mode: pass`, a no-op where a return was meant; touchpad handlers write `audio_0` without updating `_last_audio0`, so a later mode change can skip a needed PUT; `Image.open(path).convert("RGB")` leaks the opened handle; `_do_request` logs any completed request as success, so HTTP 500 reads as fine.

`osc_muteproxy`'s pulse on every change, falling edge included, is **its contract, not a defect**: it requires a **latching** source. The reference driving parameter is an unsynced bool the avatar flips both directions — a menu Toggle plus `VRCAvatarParameterDriver` behaviours that set it high in some states and low in others, gated on gesture, contact, and `IsLocal` — so one pulse per transition keeps VRChat's mute state mirrored by the parameter. A **momentary** source is what breaks it: a source that returns to zero on its own double-pulses and nets no change. Say so if the mapping is ever documented for third-party use.

The narrow residual: a contact-flippable latch can chatter, and `press_pulse` sleeps on a per-datagram thread, so two flips inside one pulse duration interleave their sends as 1,1,0,0.

Blocking `time.sleep` inside an OSC callback is **not** a defect — dispatch is per-datagram threaded (measured), so `press_pulse` and `osc_vrcft`'s load delay stall nothing. Those comments are correct.

## Provenance

`osc_leash` was a literal port of OSCLeash (MIT, © 2022 ZenithVal) carrying no notice: same movement formula, same `Y_Combined` up/down deadzone, same divide-by-`Y_Modifier` compensation, same three `/input/` outputs, and two of three tuning constants identical. Deleting it ends the obligation forward; the history that carried it is scrubbable and not preserved for attribution's sake.

Its replacement is a `vrc-patterns` entry rebuilding the leash on face-proximity box receivers rather than OSCLeash's six-sphere direction cage — `box-tracker` establishes the mechanism, and the design question is how far below six contacts an axis-separable readout gets. Until that ships there is no leash in this repo.

Every other named project here is an interface, not an ancestor. Of them only Voicemeeter carries a link today; the rest are step 10.
