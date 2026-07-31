# Seedance White-Model Human Restyle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn one human-approved, full-length Blender proxy into a neutral articulated-humanoid conditioning video, compile its actual K0-K4 actor/camera motion into a conflict-free restyle instruction, and perform at most one real Seedance reference-video generation with complete provenance and real-media evaluation.

**Architecture:** Keep the current `Story -> ShotScript -> Director Loop -> Blender` control plane, but separate three artifacts that are currently conflated: trajectory facts, photoreal restyle instructions, and backend request capability evidence. Borrow VideoCoCo's neutral-proxy and structured edit-instruction ideas without importing OmniWeaving or its training stack. The director approves a diagnostic video and the exact neutral clay video; only the clay video may become the Seedance reference. A capability gate blocks all network submission until the model name, reference-video request shape, public media URL, approval hash, and one-call budget are bound in a candidate bundle.

**Tech Stack:** Python 3.10+, `unittest`, Blender 4.x at `D:\blender\blender.exe`, Pillow, NumPy, imageio-ffmpeg/ffprobe, existing JD task transport, HTML/CSS/vanilla JavaScript.

---

## Non-negotiable experiment rules

- Unit fixtures prove code behavior only. They are never reported as model-quality evidence.
- The first real round uses the complete approximately five-second station scene, not separate shots.
- The diagnostic proxy may contain colors, labels, and trajectory overlays. The conditioning proxy must be neutral grayscale and contain none of those overlays.
- Existing approved D1 inputs may be reused as human-authored source data, but the new humanoid render must receive a new approval record before any video-generation call.
- Model submission count is zero through Tasks 1-7. Task 8 permits at most one generation submission and has no automatic retry or seed search.
- Provider status polling does not create new generations, but every query count and elapsed time is still logged.
- After each task, report changed artifacts, real test evidence, generation-call count, and any server/GPU use before moving to the next task. Local Blender work is reported separately from paid model generation.
- A rejected reference-video request is recorded as a real capability failure. Do not silently fall back to prompt-only generation.
- If a public HTTPS source-video URL cannot be produced, stop with `blocked_missing_remote_asset_transport`; local paths and `asset://` are not backend evidence.

## Task 1: Compile K0-K4 motion facts segment by segment

**Files:**

- Modify: `videoactagent/trajectory_prompt.py`
- Modify: `tests/test_trajectory_prompt.py`
- Modify: `tests/test_director_annotation.py`

- [ ] **Step 1: Add failing tests for non-monotonic actor and camera motion**

Add a trajectory in which `actor_a` approaches during K0-K2 and separates during K2-K4. Assert that both phases appear, in order, instead of a single endpoint summary. Add per-segment camera position, look-at, focal-length, shot-size, and roll changes.

```python
def test_compiles_each_segment_instead_of_hiding_reversal(self) -> None:
    frames = keyframes()
    xs = [0.10, 0.35, 0.65, 0.45, 0.20]
    for frame, x in zip(frames, xs):
        frame["actors"]["actor_a"]["x"] = x
    prompt = compile_prompt(frames)
    self.assertLess(prompt.index("K1 to K2"), prompt.index("K2 to K3"))
    self.assertIn("K1 to K2, actor_a moves right", prompt)
    self.assertIn("K2 to K3, actor_a moves left", prompt)
    self.assertIn("K1 to K2, actor_a and actor_b move closer", prompt)
    self.assertIn("K2 to K3, actor_a and actor_b move farther apart", prompt)
```

- [ ] **Step 2: Add a failing regression test for stale source-camera text**

The source story remains provenance input but must not be copied into the trajectory facts. This specifically prevents the old station sentence `locked static camera` from overriding a newly authored moving camera.

```python
def test_source_story_is_validated_but_not_copied_into_motion_facts(self) -> None:
    frames = keyframes()
    frames[2]["camera"]["position"][0] = 0.5
    prompt = compile_prompt(
        frames, story_prompt="A locked static camera watches a traveler."
    )
    self.assertNotIn("locked static camera", prompt.lower())
    self.assertIn("K1 to K2, camera moves along the approved path", prompt)
```

- [ ] **Step 3: Run the focused tests and confirm intended failures**

```powershell
python -m unittest tests.test_trajectory_prompt tests.test_director_annotation -v
```

Expected: new segment assertions fail because `trajectory-facts-v1` emits only whole-path/endpoint summaries.

- [ ] **Step 4: Implement deterministic segment facts and bump the version**

Set `PROMPT_COMPILER_VERSION = "trajectory-facts-v2"`. Add pure `_screen_motion`, `_spacing_change`, and `_camera_segment` helpers. Screen x increases right and y increases down; changes within tolerance mean hold. Describe both axes when both materially change. Emit segments K0-K1 through K3-K4, actors sorted by ID, pair spacing, camera path/look-at, focal length, shot size, and roll in stable order.

Validate `story_prompt` for source integrity but do not append its prose. Keep `appearance_instruction`, visual style, and mood because these are not motion claims.

- [ ] **Step 5: Run focused tests**

```powershell
python -m unittest tests.test_trajectory_prompt tests.test_director_annotation -v
```

Expected: all pass; repeated output is byte-identical and reports version `trajectory-facts-v2`.

- [ ] **Step 6: Commit**

```powershell
git add videoactagent/trajectory_prompt.py tests/test_trajectory_prompt.py tests/test_director_annotation.py
git commit -m "feat: compile segment-aware trajectory facts"
```

## Task 2: Add a VideoCoCo-style human restyle instruction

**Files:**

- Create: `videoactagent/restyle_prompt.py`
- Create: `tests/test_restyle_prompt.py`
- Create: `configs/restyle/station_reunion.json`

- [ ] **Step 1: Add failing schema and output-order tests**

Use a structured profile rather than parsing the old prose prompt. The station profile is real source input:

```json
{
  "schema_version": "1.0",
  "scene_id": "station_reunion",
  "subjects": [
    {"actor_id": "actor_a", "description": "an adult traveler wearing an orange coat and subdued travel clothes"},
    {"actor_id": "actor_b", "description": "an adult friend wearing a blue coat and subdued travel clothes"}
  ],
  "environment": "a grounded railway platform with stable station architecture",
  "lighting": "consistent natural station lighting",
  "quality": "photoreal live-action people, natural anatomy, realistic skin and cloth"
}
```

Assert exact section order: subjects/wardrobe; driving motion; environment; lighting; camera/framing; photoreal quality; preserve/replace/avoid. Require the text to preserve blocking, timing, occlusion, composition, and camera motion; replace every clay body with a complete person; and forbid cylinders, mannequins, plastic, clay, labels, path lines, and CG residue.

- [ ] **Step 2: Run the new test and confirm import failure**

```powershell
python -m unittest tests.test_restyle_prompt -v
```

Expected: `videoactagent.restyle_prompt` does not exist.

- [ ] **Step 3: Implement a strict deterministic compiler**

Implement `RESTYLE_COMPILER_VERSION = "human-restyle-v1"`, immutable `SubjectProfile`/`RestyleProfile`, `load_restyle_profile`, and:

```python
def compile_restyle_prompt(
    *, profile: RestyleProfile,
    trajectory_prompt: str,
    duration_seconds: float,
) -> str:
    """Return the seven ordered, source-bound restyle sections."""
```

Place actor/spacing facts under driving motion and camera/framing facts under camera. Never consume `story_prompt`.

- [ ] **Step 4: Run tests and commit**

```powershell
python -m unittest tests.test_restyle_prompt -v
git add videoactagent/restyle_prompt.py tests/test_restyle_prompt.py configs/restyle/station_reunion.json
git commit -m "feat: add structured human restyle prompt"
```

## Task 3: Replace cylinder actors with neutral articulated humanoids

**Files:**

- Create: `videoactagent/humanoid_proxy.py`
- Create: `tests/test_humanoid_proxy.py`
- Modify: `videoactagent/blender_proxy.py`
- Modify: `tests/test_blender_render_profiles.py`
- Modify: `tests/test_blender_proxy_integration.py`

- [ ] **Step 1: Add pure failing anatomy and heading tests**

The Blender-independent part spec must include head, torso, pelvis, bilateral upper/lower arms, and bilateral upper/lower legs. Test path heading uses the next non-stationary segment, then the previous segment, then zero degrees.

- [ ] **Step 2: Confirm the pure module is missing**

```powershell
python -m unittest tests.test_humanoid_proxy -v
```

- [ ] **Step 3: Implement immutable humanoid specs**

Add `HumanoidPartSpec` records containing name, primitive, local center/scale, and parent anchor. Keep stable, natural proportions and deterministic part ordering.

- [ ] **Step 4: Add failing real-Blender probes**

Extend integration tests to require root names `actor_a`/`actor_b`; parts such as `actor_a__head`; correct parentage; neutral clay materials; identical diagnostic/clay actor and camera animation; and `actor_geometry_profile: "humanoid_v1"` plus sorted part names in reports.

- [ ] **Step 5: Implement geometry, facing, and minimal walk motion**

Update `create_actor` to build low-poly head/torso/pelvis/limbs. Preserve existing root-location trajectories. Keyframe root Z rotation from authored path tangents. Add a small alternating arm/leg swing only while moving and a neutral pose while stationary. Keep labels/trajectory overlays diagnostic-only and clay grayscale with slight actor luminance contrast. Do not add fingers, face rigs, cloth, IK, or imported assets.

- [ ] **Step 6: Run pure and real Blender tests**

```powershell
python -m unittest tests.test_humanoid_proxy tests.test_blender_render_profiles tests.test_blender_proxy_integration -v
```

Expected: real renders decode and geometry/provenance probes pass. This is not a photoreal-quality claim.

- [ ] **Step 7: Commit**

```powershell
git add videoactagent/humanoid_proxy.py videoactagent/blender_proxy.py tests/test_humanoid_proxy.py tests/test_blender_render_profiles.py tests/test_blender_proxy_integration.py
git commit -m "feat: render neutral humanoid proxy actors"
```

## Task 4: Bind both prompts and proxy views into director approval

**Files:**

- Modify: `videoactagent/director_annotation.py`
- Modify: `videoactagent/director_loop.py`
- Modify: `static/director_panel.html`
- Modify: `tests/test_director_loop.py`
- Modify: `tests/test_director_annotation.py`

- [ ] **Step 1: Add failing artifact/provenance tests**

Require `trajectory_prompt.txt`, `restyle_prompt.txt`, both compiler versions, and `restyle_prompt` in approved export. Keep `compiled_prompt.txt` as a byte-identical compatibility copy of `trajectory_prompt.txt` for VACE. Approval/export must hash-bind all prompt files and the profile source.

- [ ] **Step 2: Add failing UI tests**

Require a diagnostic/clay selector, a Chinese warning that only clay is sent to generation, two separate read-only prompts, and no editable free-text prompt.

- [ ] **Step 3: Confirm focused failures**

```powershell
python -m unittest tests.test_director_annotation tests.test_director_loop -v
```

- [ ] **Step 4: Extend workspace preparation and compilation**

Add optional `restyle_profile_path` to `prepare_workspace` and `director-loop prepare`. Snapshot/hash it when supplied. For backward compatibility, derive a generic profile only from `appearance_instruction` and `environment_preset`, never story motion prose. Extend `CompiledDirectorAnnotation` with `trajectory_prompt` and `restyle_prompt`; keep `compiled_prompt` as a trajectory alias.

Return this preview shape:

```json
{
  "trajectory_compiler_version": "trajectory-facts-v2",
  "restyle_compiler_version": "human-restyle-v1",
  "trajectory_prompt": "segment-aware actor and camera facts",
  "restyle_prompt": "structured photoreal human restyle instruction"
}
```

- [ ] **Step 5: Update the browser panel**

Default to diagnostic while editing. A selector swaps `src` between `diagnostic_url` and `clay_url` without changing iteration. Put both read-only prompts below K0-K4. Preserve auto-initialization and frozen-boundary behavior.

- [ ] **Step 6: Run tests and commit**

```powershell
python -m unittest tests.test_director_annotation tests.test_director_loop tests.test_vace_coded_draft -v
git add videoactagent/director_annotation.py videoactagent/director_loop.py static/director_panel.html tests/test_director_annotation.py tests/test_director_loop.py
git commit -m "feat: approve humanoid proxy and restyle instruction"
```

## Task 5: Build a zero-network Seedance reference-video gate

**Files:**

- Create: `videoactagent/seedance_reference.py`
- Create: `tests/test_seedance_reference.py`
- Modify: `videoactagent/backends/capabilities.py`
- Modify: `videoactagent/backends/jd.py`
- Modify: `videoactagent/backend_prepare.py`
- Modify: `tests/test_backend_contracts.py`
- Modify: `tests/test_backend_prepare.py`

- [ ] **Step 1: Add failing request and rejection tests**

Test the expected content item, without transport:

```python
{
    "type": "video_url",
    "video_url": {"url": "https://portal.volccdn.com/controlled/proxy.mp4"},
    "role": "reference_video",
}
```

Prove local paths, HTTP, private IPs, placeholder hosts, and `asset://` are rejected for real submission. Prove a 2.5 model string alone does not establish gateway support; candidate preparation calls no opener/transport; no reference image is included; missing approval/hashes/public URL/capability blocks the candidate.

- [ ] **Step 2: Confirm focused failures**

```powershell
python -m unittest tests.test_seedance_reference tests.test_backend_contracts tests.test_backend_prepare -v
```

- [ ] **Step 3: Add strict capability evidence**

Add `gateway_verified` to `EvidenceState` but keep Seedance's static reference-video declaration `gateway_unverified`. Parse evidence requiring source URL or captured-response path, UTC time, response SHA-256, exact model, exact content type/URL field/role, and evidence state. Only a captured provider response may yield `gateway_verified`; an official product page yields at most `model_supported`.

- [ ] **Step 4: Implement pure request/candidate builders**

Implement `build_seedance_reference_video` and `prepare_reference_candidate`. The payload is 16:9, 720p, five seconds, no watermark, one text item, one reference-video item, no image. Bind approval, proxy, prompts/profile, capability hashes, `network_called: false`, submit limit one, and retry limit zero.

Candidate states are `ready` for verified gateway evidence, `ready_for_single_combined_probe` for model support plus unverified gateway, and `blocked` otherwise. There is no prompt-only fallback.

- [ ] **Step 5: Run tests and commit**

```powershell
python -m unittest tests.test_seedance_reference tests.test_backend_contracts tests.test_backend_prepare -v
git add videoactagent/seedance_reference.py videoactagent/backends/capabilities.py videoactagent/backends/jd.py videoactagent/backend_prepare.py tests/test_seedance_reference.py tests/test_backend_contracts.py tests/test_backend_prepare.py
git commit -m "feat: gate Seedance reference-video requests"
```

## Task 6: Add single-submit execution and real-result validation

**Files:**

- Modify: `videoactagent/jd_smoke.py`
- Create: `videoactagent/reference_result.py`
- Create: `tests/test_reference_result.py`
- Modify: `tests/test_jd_smoke.py`
- Modify: `videoactagent/manual_annotation.py`
- Modify: `tests/test_manual_annotation.py`

- [ ] **Step 1: Add failing budget/provenance tests**

Add `submit-seedance-reference`, accepting only a prepared candidate. Credential access follows all validation. A second submit for the same candidate/run is rejected. Metadata records submit limit/count, zero retry, reference-video mode, and proxy/restyle/approval hashes. Mock transport tests must label themselves code-only.

- [ ] **Step 2: Add failing media-validator tests**

Fixture tests reject short duration, undecodable K frames, wrong aspect ratio, and hash mismatch. The contract separates `technical_media_pass` from `human_restyle_pass`.

- [ ] **Step 3: Confirm focused failures**

```powershell
python -m unittest tests.test_jd_smoke tests.test_reference_result tests.test_manual_annotation -v
```

- [ ] **Step 4: Implement single-submit execution**

Reuse existing submit/query/download transport. Re-hash the candidate and sources before run creation. Write request/response before interpreting them. Never catch a submit error and resubmit. Each GET query receives an immutable timestamped response hash and increments `query_count`; a transient query error stops and is resumed only explicitly, never by another generation.

- [ ] **Step 5: Implement media inspection and manual schema**

Save exact duration, fps, dimensions, frame count, codec, decode result, and SHA-256. Decode K0-K4 at normalized times `[0.0, 0.2, 0.5, 0.8, 1.0]`, clamping K4 to the final decodable frame. Compare full duration to the proxy with only normal container rounding allowed.

Manual annotation records both actor centers, subject completeness/humanness, identity distinction, and background/camera motion at every K. Unknown stays `unknown`; no human labels are auto-filled.

- [ ] **Step 6: Run tests and commit**

```powershell
python -m unittest tests.test_jd_smoke tests.test_reference_result tests.test_manual_annotation -v
git add videoactagent/jd_smoke.py videoactagent/reference_result.py videoactagent/manual_annotation.py tests/test_jd_smoke.py tests/test_reference_result.py tests/test_manual_annotation.py
git commit -m "feat: run and validate one reference-video generation"
```

## Task 7: Produce and approve one real full-length humanoid proxy

**Runtime inputs/outputs:**

- Read: `runs/work/director_loop_v1/station_reunion/iterations/D1/input/annotation.json`
- Read: `runs/work/director_loop_v1/station_reunion/iterations/D1/approval.json`
- Create: `runs/work/director_loop_humanoid_v1/station_reunion/`
- Create: `runs/results/seedance_humanoid_v1/station_reunion/proxy_evaluation.json`

- [ ] **Step 1: Verify previous D1 is genuinely human-authored**

Use the existing verifier and inspect author, `auto_filled_values`, approval hash, actor paths, and camera states. If missing, hash-invalid, or auto-filled, stop rather than synthesize it.

- [ ] **Step 2: Prepare a new workspace**

```powershell
python -m videoactagent.cli director-loop prepare --bundle runs/work/coded_draft_v1/station_reunion/bundle.json --blender D:\blender\blender.exe --restyle-profile configs/restyle/station_reunion.json --output-dir runs/work/director_loop_humanoid_v1/station_reunion
```

Copy only the real D1 keyframe values and author identity into the new D1 payload; update inheritance/compiler fields from the new workspace. Record old annotation/approval hashes as provenance. Do not copy approval because it covered different geometry.

- [ ] **Step 3: Render one real diagnostic/clay pair**

Render 24 fps, 960x540, approximately five seconds. Save MP4/blend/report/manifest and hashes. Confirm 120 decodable frames, K0-K4 coverage, humanoid parts, and identical diagnostic/clay transforms. API generation calls remain zero.

- [ ] **Step 4: Present the complete proxy for human approval**

Watch both full videos and inspect both prompts. Approve only if the two articulated figures, full paths, and camera match K0-K4. Otherwise record the defect and return to the loop; do not call Seedance.

- [ ] **Step 5: Export real approval evidence**

Only after an actual approval click, export and write `proxy_evaluation.json` with media metadata, source hashes, approval hash, and decision. Do not prepopulate qualitative fields.

## Task 8: Resolve provider capability and make at most one call

**Runtime outputs:**

- `runs/results/seedance_humanoid_v1/station_reunion/capability_evidence.json`
- `runs/results/seedance_humanoid_v1/station_reunion/reference_candidate.json`
- `runs/results/seedance_humanoid_v1/station_reunion/api/`
- `runs/results/seedance_humanoid_v1/station_reunion/result_validation.json`

- [ ] **Step 1: Query only non-generation capability surfaces if available**

Capture model-list/schema URL, UTC time, raw response, and hash. Current public JD JoyBuilder video docs list Kling, Vidu, and Hailuo rather than Seedance, so they cannot verify this generic gateway's Seedance contract: <https://docs.jdcloud.com/cn/jdaip/chat>. If there is no non-generation schema endpoint, record `gateway_unverified`; do not submit a disposable probe.

Choose the model in this fixed order: verified Seedance 2.5 white-model/reference-video contract; otherwise a verified `Doubao-Seedance-2.0` reference-video contract; otherwise stop. Never replace a missing reference-video contract with prompt-only generation.

- [ ] **Step 2: Establish a backend-readable HTTPS proxy URL**

Use a provider-documented upload endpoint or already-authorized signed/object URL. Verify unauthenticated HTTPS GET bytes hash-identically to the approved clay MP4 and log the result. If unavailable, write `blocked_missing_remote_asset_transport` and stop at zero calls. Never substitute an old output URL, local URL, GitHub blob page, or `asset://`.

- [ ] **Step 3: Build and inspect the zero-network candidate**

Bind clay video, restyle instruction, capability evidence, model, duration, aspect ratio, resolution, approval, and source hashes. Confirm one `reference_video`, no image, `network_called=false`, submit limit one, retry zero.

- [ ] **Step 4: Submit once**

For `gateway_verified`, submit normally. For `ready_for_single_combined_probe`, the sole real generation also tests gateway acceptance. Before submission, persist an API-use record with one planned generation call, model, duration/resolution, input hashes, and no retry. On rejection, save the raw response and stop; do not alter model/role and try again.

- [ ] **Step 5: Poll, download, and validate original media**

Poll without resubmitting, download the original MP4, and record query count, elapsed time, request/response/task IDs, hash, exact media metadata, and K0-K4 decoding.

- [ ] **Step 6: Perform real manual K0-K4 annotation**

Watch full proxy and result. Label actor visibility/completeness, photoreal human versus clay/CG, orange/blue distinction and identity stability, centers/pair distance, camera/background direction, explicit failures, and unknowns. Never copy proxy labels into result labels.

- [ ] **Step 7: Apply the acceptance decision**

Pass only for a complete approximately five-second video with all K frames, two complete recognizable humans, distinguishable identities, visible approved actor-relation sequence, and matching camera/background motion. If technically valid but low-poly, report `api_chain_success_human_restyle_failure`. If only identity/wardrobe drifts after successful humanization, a later plan may add reference images; not this round.

## Task 9: Keep usage concise and deliver a real report

**Files:**

- Modify: `README.md`
- Modify: `docs/USAGE.md`
- Modify: `docs/ARCHITECTURE.md`
- Create runtime: `runs/results/seedance_humanoid_v1/station_reunion/REPORT.md`

- [ ] **Step 1: Reduce README to one short workflow**

Show only: prepare/open director loop; approve complete neutral proxy; run one approved Seedance reference candidate; annotate result. Put recovery submit/query/download details in `docs/USAGE.md`. Label real Blender, network, and paid-generation commands.

- [ ] **Step 2: Document architecture and VideoCoCo boundary**

```text
Prompt -> K0-K4 actor/camera plan -> humanoid clay proxy -> human approval
       -> segment-aware restyle instruction -> Seedance reference-video gate
       -> one real result -> media check + human annotation
```

State that VideoCoCo contributed neutral-proxy/edit-triplet ideas; this repo uses its own human/camera director and Seedance adapter, not OmniWeaving training/weights.

- [ ] **Step 3: Write only observed experiment facts**

Report exact commit/dirty status; paths/hashes/media; model/request/task ID/API counts/time/failure; K0-K4 annotations and deltas; separate technical/human pass; next change based on observed failure. If blocked before generation, report zero calls and the blocker, with no fabricated result table.

- [ ] **Step 4: Run final focused and full verification**

```powershell
python -m unittest tests.test_trajectory_prompt tests.test_restyle_prompt tests.test_humanoid_proxy tests.test_blender_render_profiles tests.test_blender_proxy_integration tests.test_director_annotation tests.test_director_loop tests.test_seedance_reference tests.test_backend_contracts tests.test_backend_prepare tests.test_jd_smoke tests.test_reference_result tests.test_manual_annotation -v
python -m unittest discover -s tests -v
```

Expected: all listed tests pass; real Blender tests are actual renders; mocked API tests remain code-only. Report exact names/output for any failure rather than claiming a clean suite.

- [ ] **Step 5: Inspect diff and secrets**

```powershell
git status --short
git diff --check
git log --oneline -10
```

Confirm no API key, password, signed URL, bulky MP4, `.blend`, or temporary provider response is staged.

- [ ] **Step 6: Commit docs**

```powershell
git add README.md docs/USAGE.md docs/ARCHITECTURE.md
git commit -m "docs: explain approved humanoid restyle workflow"
```

## Final evidence checklist

- [ ] Segment-aware K0-K4 prompt contains no stale story-camera directive.
- [ ] Station profile explicitly distinguishes actor_a orange and actor_b blue.
- [ ] Real Blender proxy has articulated humanoids and a five-second decodable clay MP4.
- [ ] Human approval covers that exact humanoid render.
- [ ] Candidate hashes bind approval, proxy, prompt/profile, capability, and payload.
- [ ] At most one real generation was submitted with zero automatic retries.
- [ ] Original result metadata/hash and K0-K4 decode evidence are saved.
- [ ] Human labels came from watching real media, not copying or synthesis.
- [ ] Report separates technical chain success from human-restyle/control success.
- [ ] No prompt-only, VACE, fixture, or old output is presented as this experiment.
