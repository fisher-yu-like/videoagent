# Stage 7 Closed-Loop Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn shot-level camera, actor, and continuity constraints into hash-linked evaluation feedback and one bounded revision proposal for each real generated video.

**Architecture:** Compile the existing ShotScript into an immutable expectation contract, combine automatic camera measurements with structured inspection of actual output frames, and apply a small deterministic revision policy. The first loop reuses the existing real Kling `s01` result; the same contract then evaluates the first real VACE output when Stage 6 produces it. No model training or extra LLM planner is introduced initially.

**Tech Stack:** Python 3.12/3.10; dataclasses; JSON; SHA-256; Pillow; NumPy; existing `videoactagent.shotscript` and `videoactagent.camera_eval`; unittest.

---

## File map

- Create: `videoactagent/closed_loop.py` — compile expectations, validate real observations, merge feedback, and propose bounded revisions.
- Create: `tests/test_closed_loop.py` — unit tests plus an integration test using the existing real Kling video and Stage 4 camera report.
- Create: `examples/stage7_s01_inspection.json` — human inspection of named, hash-addressed frames from the real `s01` output.
- Create at runtime: `runs/stage7_closed_loop/kling_s01/expectation.json`.
- Create at runtime: `runs/stage7_closed_loop/kling_s01/feedback.json`.
- Create at runtime: `runs/stage7_closed_loop/kling_s01/revision.json`.
- Create after execution: `docs/reports/2026-07-29-stage-7-closed-loop-evaluation.md`.

## Contract and experiment shape

Each shot produces three linked records:

1. `expectation.json`: expected camera motion, camera start/end/look-at, actor start/end/action/facing, and continuity constraints copied from ShotScript.
2. `feedback.json`: generated-video hash, inspected-frame hashes, automatic camera result, structured actor/continuity observations, and per-constraint verdicts.
3. `revision.json`: only the permitted changes needed for the next run, the source feedback hash, and the next backend job inputs.

The initial accepted observation sources are the existing automatic phase-correlation camera report and a human inspection record bound to the exact video/frame hashes. Actor identity tracking or learned pose estimation can be added later; Stage 7 does not add an unvalidated vision model merely to create a numeric score.

### Task 1: Compile the shot-level expectation contract

**Files:**

- Create: `videoactagent/closed_loop.py`
- Create: `tests/test_closed_loop.py`

- [x] **Step 1: Write the failing expectation test**

```python
def test_compiles_s01_camera_actor_and_continuity_constraints():
    script = ShotScript.from_path("examples/station_shotscript.json")
    contract = compile_expectation(script, "s01")

    assert contract["shot_id"] == "s01"
    assert contract["camera"]["motion"] == "truck_right"
    assert contract["camera"]["start"] == [-1.0, -10.0, 6.0]
    assert contract["camera"]["end"] == [1.0, -10.0, 6.0]
    assert contract["actors"][0]["actor_id"] == "actor_a"
    assert contract["actors"][0]["action"] == "walk"
    assert contract["continuity"]["screen_direction"] == "left_to_right"
```

- [x] **Step 2: Run the focused test and verify RED**

```powershell
& 'C:\Users\sy\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_closed_loop -v
```

Expected: import failure because `videoactagent.closed_loop` does not exist.

- [x] **Step 3: Implement the minimal compiler**

Implement `compile_expectation(script, shot_id)` and `write_json_atomic(path, value)`. Reject missing/duplicate shots and unsupported camera motions. Preserve numeric camera/actor coordinates exactly as lists and include `schema_version`, `scene_id`, `shot_id`, `duration`, and ShotScript file SHA-256.

- [x] **Step 4: Run GREEN and commit**

```powershell
& 'C:\Users\sy\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_closed_loop -v
git add -- videoactagent/closed_loop.py tests/test_closed_loop.py
git commit -m "feat: compile closed-loop shot expectations"
```

### Task 2: Bind measured feedback to an actual generated video

**Files:**

- Modify: `videoactagent/closed_loop.py`
- Modify: `tests/test_closed_loop.py`
- Create: `examples/stage7_s01_inspection.json`

- [x] **Step 1: Write failing provenance and feedback tests**

Use these real repository inputs in the integration test:

```python
REAL_VIDEO = Path("runs/stage3_api/20260729T012120Z_kling_d8bde2ef/result.mp4")
REAL_CAMERA = Path("runs/stage4_camera_eval/kling_s01_camera_eval.json")
REAL_FRAMES = Path("runs/stage3_api/20260729T012120Z_kling_d8bde2ef/frames")
```

Assert `build_feedback()` recomputes the video, camera-report, and first/middle/last-frame hashes; requires the camera report input hashes to match those same frame files; keeps automatic and human observations separate; emits an individual verdict for camera motion, actor actions, actor facing, and screen direction; and rejects a one-byte-tampered copy of the video or inspection JSON.

- [x] **Step 2: Create the real inspection record**

Open the actual first/middle/last PNG files and record only visible observations with this schema:

```python
inspection = {
    "schema_version": "0.1",
    "shot_id": "s01",
    "video_sha256": sha256_file(video_path),
    "frames": {
        name: {
            "sha256": sha256_file(frame_paths[name]),
            "actor_a_visible": inspect_frame(frame_paths[name], actor_id="A"),
            "actor_b_visible": inspect_frame(frame_paths[name], actor_id="B"),
        }
        for name in ("first", "middle", "last")
    },
    "observations": {
        "actor_a_action": "matched",       # or "mismatched"/"uncertain"
        "actor_b_action": "matched",       # or "mismatched"/"uncertain"
        "actors_facing": "matched",        # or "mismatched"/"uncertain"
        "screen_direction": "matched",     # or "mismatched"/"uncertain"
    },
    "notes": ["literal observations tied to visible frames"],
}
```

The inspection writer must replace each unresolved frame-inspection expression and observation value with the result of looking at the named real frames before saving. The validator rejects unresolved expressions, unknown verdicts, and hashes that do not match the input bytes.

- [x] **Step 3: Implement validation and feedback merging**

Implement:

```python
build_feedback(
    expectation_path: Path,
    video_path: Path,
    camera_report_path: Path,
    inspection_path: Path,
) -> dict
```

Camera control is `matched` only when the Stage 4 report verdict is `matched`; `inconclusive` remains `inconclusive`. Human observations are copied as `manual_visual_inspection`, never relabeled as automatic measurements. Every input receives byte size and SHA-256.

- [x] **Step 4: Run focused and full tests, then commit**

```powershell
& 'C:\Users\sy\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_closed_loop -v
& 'C:\Users\sy\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest discover -s tests -v
git add -- videoactagent/closed_loop.py tests/test_closed_loop.py examples/stage7_s01_inspection.json
git commit -m "feat: bind real video feedback to shot constraints"
```

### Task 3: Produce a bounded revision instead of a new planner

**Files:**

- Modify: `videoactagent/closed_loop.py`
- Modify: `tests/test_closed_loop.py`

- [x] **Step 1: Write failing revision-policy tests**

Cover these exact rules:

```python
def test_inconclusive_camera_uses_structure_before_prompt_rewrite():
    revision = propose_revision(feedback_with(camera="inconclusive"))
    assert revision["operations"] == [
        {"op": "enable_structural_proxy", "channel": "vace_src_video"},
        {"op": "preserve_camera_trajectory", "source": "shotscript"},
    ]

def test_actor_mismatch_tightens_only_actor_timing():
    revision = propose_revision(feedback_with(actor_a_action="mismatched"))
    assert revision["operations"] == [
        {"op": "strengthen_timed_action", "actor_id": "A"}
    ]

def test_all_matched_produces_no_revision():
    assert propose_revision(feedback_all_matched())["operations"] == []
```

Add cases for uncertain observations, screen-direction mismatch, stable operation ordering, and duplicate-operation removal.

- [x] **Step 2: Implement only the fixed operation vocabulary**

Allowed operations are:

- `enable_structural_proxy`;
- `preserve_camera_trajectory`;
- `strengthen_timed_action`;
- `bind_previous_last_frame`;
- `preserve_action_axis`.

`propose_revision()` must include the source feedback SHA-256 and may not modify seed, model, resolution, duration, or unrelated actors. Unknown verdicts fail closed.

- [x] **Step 3: Run tests and commit**

```powershell
& 'C:\Users\sy\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_closed_loop -v
& 'C:\Users\sy\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest discover -s tests -v
git add -- videoactagent/closed_loop.py tests/test_closed_loop.py
git commit -m "feat: add bounded shot revision policy"
```

### Task 4: Run the first real closed loop and archive the comparison

**Files:**

- Runtime: `runs/stage7_closed_loop/kling_s01/`
- Create: `docs/reports/2026-07-29-stage-7-closed-loop-evaluation.md`

- [x] **Step 1: Generate expectations and feedback from existing real data**

```powershell
& 'C:\Users\sy\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m videoactagent.closed_loop evaluate `
  --shotscript examples/station_shotscript.json `
  --shot s01 `
  --video runs/stage3_api/20260729T012120Z_kling_d8bde2ef/result.mp4 `
  --camera-report runs/stage4_camera_eval/kling_s01_camera_eval.json `
  --inspection examples/stage7_s01_inspection.json `
  --output-dir runs/stage7_closed_loop/kling_s01
```

Expected: `CLOSED_LOOP_EVALUATED` only after all input hashes have been reread and checked. The command creates the three linked JSON files and makes no API call.

- [ ] **Step 2: Prepare one controlled next-run job**

Compile `revision.json` into one backend job while holding story, shot, model, seed, duration, and resolution constant. Change only the listed operations. For the open-source path, use the real Stage 6 VACE input mapping; do not substitute synthetic proxy media.

- [ ] **Step 3: Evaluate one actual revised output**

After Stage 6 has produced a decodable real VACE MP4, extract first/middle/last frames, run `camera_eval`, visually inspect the named frames, and invoke the same `closed_loop evaluate` command into `runs/stage7_closed_loop/vace_s01/`. Record actual runtime, GPU memory, file sizes, and hashes. If generation fails, archive that failure and do not create a success feedback record.

- [ ] **Step 4: Report the shot-level comparison**

Write the report as a constraint table with columns `constraint`, `expected`, `baseline observation`, `revised observation`, `measurement source`, and `verdict`. Keep automatic camera evidence and manual actor/continuity inspection labeled separately. Do not replace a missing revised output with unit-test data.

- [ ] **Step 5: Final verification and commit**

```powershell
& 'C:\Users\sy\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest discover -s tests -v
git diff --check
git add -- videoactagent/closed_loop.py tests/test_closed_loop.py examples/stage7_s01_inspection.json docs/reports/2026-07-29-stage-7-closed-loop-evaluation.md
git commit -m "docs: record Stage 7 closed-loop experiment"
```

## Real-data acceptance gates

- Unit tests establish software behavior; Stage 7 acceptance additionally requires the existing real Kling video or a newly decoded real backend output.
- Every observation must be linked to the exact MP4 and frame hashes. A hash mismatch invalidates the feedback run.
- Camera verdicts come from the persisted evaluator output; actor and continuity verdicts state whether they came from manual inspection or a named detector.
- One baseline and one controlled revision form the smallest comparison. No silent retries, seed search, or selective replacement of failed outputs.
- Training remains outside Stage 7. Consider a lightweight adapter only after repeated real closed-loop runs identify a stable residual error that prompt, keyframe, proxy, and stock VACE controls cannot remove.
