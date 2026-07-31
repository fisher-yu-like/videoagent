# Human Director Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a browser-guided K0–K4 actor/camera/prompt editor that repeatedly rerenders a complete Blender proxy and gates VACE job creation on explicit human approval.

**Architecture:** Add a focused director-annotation compiler and a local director-loop service rather than expanding the legacy actor-only author. The compiler emits the existing actor trajectory plus a separate camera trajectory; Blender consumes both for diagnostic/clay renders. Immutable loop manifests bind every iteration, and coded-draft/VACE adapters accept only the exact approved iteration.

**Tech Stack:** Python 3.11 standard library, existing ShotScript/TrajectoryInstruction models, Blender 4 Python API, HTML/CSS/JavaScript, `unittest`, FFmpeg media probes already used by `coded_draft.py`.

---

## File map

- Create `videoactagent/director_annotation.py`: strict annotation validation and actor/camera/prompt compilation.
- Create `videoactagent/director_loop.py`: immutable iteration workspace, real Blender job lifecycle, approval gate, localhost HTTP API.
- Create `static/director_panel.html`: complete video review and guided K0–K4 editor.
- Modify `videoactagent/blender_runner.py`: forward a verified camera trajectory to Blender.
- Modify `videoactagent/blender_proxy.py`: apply K0–K4 camera location/look-at/focal-length keyframes and report them.
- Modify `videoactagent/coded_draft.py`: snapshot director evidence and pass actor/camera trajectories to both profiles.
- Modify `videoactagent/vace_coded_draft.py`: reject an unapproved/stale director iteration and bind approval hashes.
- Modify `videoactagent/cli.py`: register `director-loop`.
- Create `tests/test_director_annotation.py` and `tests/test_director_loop.py`.
- Modify `tests/test_blender_proxy_integration.py`, `tests/test_coded_draft.py`, and `tests/test_vace_coded_draft.py`.

### Task 1: Strict director annotation and compilation

**Files:**
- Create: `videoactagent/director_annotation.py`
- Create: `tests/test_director_annotation.py`

- [ ] **Step 1: Write failing schema tests**

Create tests whose complete station payload contains five keys, ten actor points, five camera states, and five prompts. Assert that missing camera states, empty prompts, non-finite coordinates, camera/look-at equality, unknown keys, and auto-filled values raise `DirectorAnnotationError`.

```python
def test_complete_annotation_compiles_actor_camera_and_prompt(station_payload):
    result = compile_director_annotation(station_payload, station_contract())
    assert len(result.actor_trajectory.tracks) == 2
    assert [state.keyframe_id for state in result.camera_trajectory.states] == [f"K{i}" for i in range(5)]
    assert "K0" in result.compiled_prompt and "K4" in result.compiled_prompt

def test_annotation_rejects_missing_camera_state(station_payload):
    station_payload["keyframes"][2].pop("camera")
    with pytest.raises(DirectorAnnotationError, match="camera"):
        compile_director_annotation(station_payload, station_contract())
```

- [ ] **Step 2: Run the tests and confirm RED**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_director_annotation`

Expected: import failure because `videoactagent.director_annotation` does not exist.

- [ ] **Step 3: Implement immutable model and compiler**

Implement these public boundaries:

```python
@dataclass(frozen=True)
class CameraKeyframe:
    keyframe_id: str
    t: float
    position: tuple[float, float, float]
    look_at: tuple[float, float, float]
    focal_length_mm: float
    shot_size: str

@dataclass(frozen=True)
class CompiledDirectorAnnotation:
    actor_trajectory: TrajectoryInstruction
    camera_trajectory: Sequence[CameraKeyframe]
    compiled_prompt: str
    canonical_annotation: bytes

def compile_director_annotation(payload: Mapping[str, Any], contract: Mapping[str, Any]) -> CompiledDirectorAnnotation:
    validated = _validate_annotation(payload, contract)
    return CompiledDirectorAnnotation(
        actor_trajectory=_compile_actor_trajectory(validated),
        camera_trajectory=tuple(_compile_camera_states(validated)),
        compiled_prompt=_compile_prompt(validated),
        canonical_annotation=canonical_json_bytes(validated),
    )
```

Require exact K IDs/times and exact actor IDs from the contract. Build the actor `TrajectoryInstruction` from explicitly supplied normalized points. Compile text chronologically as `K{index} (t={normalized_time}): visible_state. Camera: shot_size, focal length, position, look-at.`

- [ ] **Step 4: Run focused tests and commit**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_director_annotation`

Expected: all tests pass.

Commit: `feat: compile human director annotations`

### Task 2: Apply explicit camera trajectory in Blender

**Files:**
- Modify: `videoactagent/blender_runner.py`
- Modify: `videoactagent/blender_proxy.py`
- Modify: `tests/test_blender_proxy_integration.py`

- [ ] **Step 1: Write failing command and real-render assertions**

Assert that `--camera-trajectory <path>` is forwarded after `--trajectory`, and add a short real Blender integration that inspects the resulting manifest:

```python
camera = manifest["applied_camera_trajectory"]
self.assertEqual([item["keyframe_id"] for item in camera], ["K0", "K1", "K2", "K3", "K4"])
self.assertNotEqual(camera[0]["position"], camera[-1]["position"])
self.assertEqual(camera[2]["focal_length_mm"], 50.0)
```

- [ ] **Step 2: Confirm RED**

Run the new runner unit and one short Blender integration test. Expected: parser rejects `--camera-trajectory`.

- [ ] **Step 3: Implement camera application**

Add `--camera-trajectory` to both parsers. Load the strict canonical camera document, convert normalized `t` to inclusive scene frames, set camera location and lens keyframes, compute `rotation_euler` from `(look_at - position).to_track_quat('-Z', 'Y')`, and set linear interpolation for location/rotation/lens curves. Report every applied camera state and source SHA-256.

- [ ] **Step 4: Run the focused real integration and commit**

Run only the new camera test with `D:\blender\blender.exe`; verify the MP4 is non-empty and manifest camera states match the authored states.

Commit: `feat: render authored camera trajectories`

### Task 3: Immutable complete-video review loop and browser UI

**Files:**
- Create: `videoactagent/director_loop.py`
- Create: `static/director_panel.html`
- Create: `tests/test_director_loop.py`
- Modify: `videoactagent/cli.py`

- [ ] **Step 1: Write failing workspace/API tests**

Test preparation from a verified coded-draft bundle, `D0` video serving with byte ranges, exact save payloads, monotonic `D1`/`D2` allocation, stale-render rejection, failure evidence, and approval rejection before complete-video media validation.

```python
def test_vace_export_requires_exact_approved_iteration(workspace):
    with self.assertRaisesRegex(DirectorLoopError, "not approved"):
        export_approved_iteration(workspace)
    approval = approve_iteration(workspace, "D1", author_id="sy")
    exported = export_approved_iteration(workspace)
    self.assertEqual(exported["approval_sha256"], sha256(approval))
```

- [ ] **Step 2: Confirm RED**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_director_loop`

Expected: import failure because `videoactagent.director_loop` does not exist.

- [ ] **Step 3: Implement the loop service**

Expose the following localhost-only API:

```text
GET  /api/session
GET  /media/<iteration>/diagnostic.mp4   (supports Range)
GET  /media/<iteration>/clay.mp4         (supports Range)
POST /api/iterations                     (save annotation and start real Blender render)
GET  /api/jobs/<job_id>                  (real queued/running/succeeded/failed state)
POST /api/iterations/<id>/approve        (hash-bound human approval)
```

Use one guarded background render at a time. Render into a staging directory, validate both MP4s with the existing coded-draft media probe, then atomically publish the new immutable iteration. Never overwrite the last valid iteration on failure.

- [ ] **Step 4: Implement the guided panel**

Add a complete HTML5 video player, K0–K4 seek buttons, actor/camera click modes, camera position/look-at/focal/shot-size fields, editable visible-state prompt, path drawing, completed-state indicators, real job polling, and separate **Generate next proxy** and **Approve current proxy** controls. Do not prefill human points or approval.

- [ ] **Step 5: Run API/UI contract tests and commit**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_director_annotation tests.test_director_loop tests.test_cli`

Expected: all pass.

Commit: `feat: add iterative proxy director loop`

### Task 4: Bind approved director evidence through coded draft and VACE

**Files:**
- Modify: `videoactagent/coded_draft.py`
- Modify: `videoactagent/vace_coded_draft.py`
- Modify: `tests/test_coded_draft.py`
- Modify: `tests/test_vace_coded_draft.py`

- [ ] **Step 1: Write stale/unapproved attack tests**

Add cases proving coded draft rejects an unapproved annotation, approval for another iteration, replaced diagnostic/clay videos, replaced camera trajectory, and changed prompt. Prove VACE rejects a coded bundle whose approval snapshot or approved proxy hash has changed.

- [ ] **Step 2: Confirm RED**

Run only the new coded-draft and VACE test methods. Expected: new CLI arguments are rejected or the attack is incorrectly accepted.

- [ ] **Step 3: Add approval-bound adapters**

Add paired coded-draft arguments `--director-iteration` and `--director-approval`. Verify all hashes before Blender starts, snapshot annotation/actor trajectory/camera trajectory/compiled prompt/approval, and record `human_director_binding` in bundle and inventory. VACE copies and re-verifies this binding; a missing or `approved != true` binding is rejected for director-loop jobs.

- [ ] **Step 4: Run focused adapter tests and commit**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_coded_draft tests.test_vace_coded_draft`

Expected: all focused tests pass.

Commit: `feat: gate VACE on approved proxy iterations`

### Task 5: Real station loop handoff

**Files:**
- Create at runtime: `runs/work/director_loop_v1/station_reunion/`
- Modify: `README.md`

- [ ] **Step 1: Prepare the real D0 workspace**

Prepare from `runs/work/coded_draft_v1/station_reunion/bundle.json`, verify the existing full 120-frame diagnostic/clay videos, and start the localhost panel. Do not create human points.

- [ ] **Step 2: Human creates D1**

The user watches the full D0 video, authors actor/camera K0–K4 states and prompts, and selects **Generate next proxy**. The service runs real Blender and returns the full D1 video. No VACE call occurs.

- [ ] **Step 3: Human approves an iteration**

Repeat only if requested. Approval must be a real browser action after complete-video review. Record its hashes and lock the chosen iteration.

- [ ] **Step 4: Run one real VACE inference**

Build the approval-bound VACE job, upload it to the existing A100 server, run exactly once with no automatic retry, download the real MP4/log/job report, and validate media metadata and SHA-256.

- [ ] **Step 5: Update concise usage and commit**

Document one browser-first command for starting the director loop and the exact evidence/result locations. Do not add a long CLI tutorial.

Commit: `docs: document browser-first director loop`
