# Multicam Feedback and Rerender Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a minimal three-camera review loop that can rerender exact camera inputs or revise all/one camera through one DeepSeek call, while restoring editable camera position and look-at trajectories.

**Architecture:** Keep the existing S/P/M workspace and Blender renderer. A revision creates a normal P version plus one compiled `camera_bundle.json`; unselected cameras are copied from the current successful M input. Rerendering snapshots the current M input into a new M, and the existing browser page gains the missing review controls and dual trajectory editor.

**Tech Stack:** Python 3.12 standard library HTTP server, DeepSeek JSON API, vanilla HTML/JavaScript Canvas, Blender CLI, `unittest`.

---

## File map

- Modify `videoactagent/deepseek_planner.py`: add validated multicamera revision context to the existing one-call planner.
- Modify `videoactagent/director_multicam.py`: revise P versions, preserve unselected cameras, rerender an M input, expose matching iteration state, and add routes.
- Modify `videoactagent/director_wizard.py`: delegate the two new pipeline routes.
- Modify `static/director_multicam_panel.html`: add result feedback controls and position/look-at editing.
- Modify `tests/test_deepseek_planner.py`, `tests/test_director_multicam.py`, `tests/test_director_multicam_panel.py`, and `tests/test_director_wizard_http.py`: focused contracts and regression coverage.

### Task 1: Extend the existing DeepSeek planner with revision context

**Files:**
- Modify: `videoactagent/deepseek_planner.py:129`
- Test: `tests/test_deepseek_planner.py`

- [ ] **Step 1: Write failing revision-request tests**

Add a transport-capture test that calls:

```python
request_multicam_plan(
    scene_context=scene_context,
    output_dir=output,
    environ=deepseek_environment,
    transport=fake_transport,
    previous_plan=VALID_PLAN,
    previous_camera_bundle=VALID_CAMERA_BUNDLE,
    revision_scope="camera_b",
    feedback="keep B static and looking at actor_b",
)
```

Assert that the request contains one `revision` object with exactly `scope`, `feedback`, `previous_plan`, and `previous_camera_bundle`; assert `api_call_count == 1` and `retry_count == 0`. Add validation tests rejecting empty feedback, invalid scope, or only some revision arguments before transport is called.

- [ ] **Step 2: Run the focused tests and confirm RED**

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_deepseek_planner -v
```

Expected: new tests fail because `request_multicam_plan` does not accept revision arguments.

- [ ] **Step 3: Add the minimal optional revision parameters**

Extend the signature with:

```python
previous_plan: Mapping[str, Any] | None = None,
previous_camera_bundle: Mapping[str, Any] | None = None,
revision_scope: str | None = None,
feedback: str | None = None,
```

Require all four values together, restrict scope to `all`, `camera_a`, `camera_b`, `camera_c`, and require non-empty feedback of at most 2000 characters. Add:

```python
user_payload["revision"] = {
    "scope": revision_scope,
    "feedback": feedback.strip(),
    "previous_plan": dict(previous_plan),
    "previous_camera_bundle": dict(previous_camera_bundle),
}
```

Append one system instruction saying that a single-camera revision must reproduce every unselected camera assignment exactly. Keep `stream=False`, one transport call, and zero retries.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run the Task 1 command. Expected: all `tests.test_deepseek_planner` tests pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add videoactagent/deepseek_planner.py tests/test_deepseek_planner.py
git commit -m "feat: add scoped multicam revision prompts"
```

### Task 2: Create a revised P and preserve unselected camera states

**Files:**
- Modify: `videoactagent/director_multicam.py:506`
- Test: `tests/test_director_multicam.py`

- [ ] **Step 1: Write failing backend tests**

Prepare and approve P1, create a successful M1 job whose `input/camera_bundle.json` contains a recognizable manual adjustment, then call:

```python
record = revise_plan(
    manifest,
    "P1",
    scope="camera_b",
    feedback="make B static",
    planner=fake_revision_planner,
)
```

Assert P2 is current and unapproved; its record contains `parent_plan_id`, `revision_scope`, `feedback`, and a hash record for `camera_bundle.json`. Assert Camera A/C states are exactly equal to M1 input while Camera B differs. Add rejection tests for invalid scope, blank feedback, missing successful M, and a planner that changes an unselected assignment.

- [ ] **Step 2: Run the backend tests and confirm RED**

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_director_multicam.DirectorMulticamTests -v
```

Expected: new tests fail because `revise_plan` does not exist.

- [ ] **Step 3: Implement `revise_plan` with existing helpers**

Add:

```python
def revise_plan(
    manifest_path: Path | str,
    plan_id: str,
    *,
    scope: str,
    feedback: str,
    planner: Callable[..., Mapping[str, Any]] = request_multicam_plan,
) -> dict[str, Any]:
```

Use `_scene_context`, approved P1 `plan.json`, and M1 `input/camera_bundle.json` as planner inputs. Validate P2 with `load_multicam_plan`. For a single scope, compare every unselected assignment as canonical dictionaries. Compile P2, replace unselected camera states with a deep JSON copy from M1, validate through `_validated_camera_bundle`, and write `plans/P2/camera_bundle.json`. Only after all files validate, set `current_plan=P2` and `approved_plan=None`; keep P1/M1 unchanged.

- [ ] **Step 4: Make session use the attached revised bundle**

When the current plan record has a `camera_bundle` record, verify and return it as `session["camera_bundle"]`; otherwise retain existing deterministic compilation. Apply the same rule inside `prepare_render` when no explicit override is supplied, so non-browser callers cannot silently discard the preserved A/C states.

- [ ] **Step 5: Run focused tests and confirm GREEN**

Run the Task 2 command. Expected: all director multicamera unit tests pass.

- [ ] **Step 6: Commit Task 2**

```powershell
git add videoactagent/director_multicam.py tests/test_director_multicam.py
git commit -m "feat: create scoped multicam plan revisions"
```

### Task 3: Rerender exact M input and expose safe workflow state

**Files:**
- Modify: `videoactagent/director_multicam.py:676`
- Modify: `videoactagent/director_wizard.py:850`
- Test: `tests/test_director_multicam.py`
- Test: `tests/test_director_wizard_http.py`

- [ ] **Step 1: Write failing rerender and HTTP tests**

Test `prepare_rerender(manifest, "M1")` after successful M1. Assert new M2 camera input SHA-256 equals M1 input SHA-256, plan/staging bindings remain valid, and no planner is called. Reject failed, stale, or plan-mismatched iterations.

Add HTTP tests for:

```text
POST /api/plans/P1/revise
POST /api/iterations/M1/rerender
```

The wizard must delegate both to the approved reference pipeline and return JSON.

- [ ] **Step 2: Run focused tests and confirm RED**

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_director_multicam tests.test_director_wizard_http -v
```

Expected: failures for missing functions and routes.

- [ ] **Step 3: Implement exact-input rerendering**

Add:

```python
def prepare_rerender(manifest_path: Path | str, iteration_id: str) -> Path:
    workspace = verify_workspace(manifest_path)
    root, state = workspace["root"], workspace["state"]
    if state.get("current_iteration") != iteration_id:
        raise DirectorMulticamError("only the current iteration can be rerendered")
    job = _read(root / "iterations" / iteration_id / "job.json", "rerender source")
    if job.get("status") != "succeeded":
        raise DirectorMulticamError("rerender source must have succeeded")
    plan_id = str(job.get("plan_id"))
    if state.get("approved_plan") != plan_id:
        raise DirectorMulticamError("rerender source plan is not currently approved")
    camera_path = _verify_record(
        root, job.get("inputs", {}).get("camera_bundle"), "rerender camera bundle"
    )
    return prepare_render(
        manifest_path,
        plan_id,
        camera_bundle_override=_read(camera_path, "rerender camera bundle"),
    )
```

Do not call DeepSeek. Add `plan_id` and the verified rendered bundle to `session["iteration"]`. Set `workflow_step="result"` only when current successful M belongs to current approved P; a new P without a matching M remains in camera review.

- [ ] **Step 4: Add the two routes**

Validate exact keys `{"scope", "feedback"}` for revision and an empty object for rerender. Start rerender with the existing `Thread(target=run_render_job, ...)` pattern. Add both regex paths to the wizard delegation list so the body is parsed once.

- [ ] **Step 5: Run focused tests and confirm GREEN**

Run the Task 3 command. Expected: all selected tests pass.

- [ ] **Step 6: Commit Task 3**

```powershell
git add videoactagent/director_multicam.py videoactagent/director_wizard.py tests/test_director_multicam.py tests/test_director_wizard_http.py
git commit -m "feat: rerender exact multicam inputs"
```

### Task 4: Restore position/look-at editing and result feedback controls

**Files:**
- Modify: `static/director_multicam_panel.html:135`
- Test: `tests/test_director_multicam_panel.py`

- [ ] **Step 1: Write failing page-contract tests**

Require IDs `camera-edit-position`, `camera-edit-look-at`, `look-height`, `revision-scope`, `camera-feedback`, `revise-camera-plan`, and `rerender-proxy`. Require `/revise` and `/rerender`. Extract the Canvas functions and assert Node can execute one position edit and one look-at edit without changing the other array.

- [ ] **Step 2: Run the panel tests and confirm RED**

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_director_multicam_panel -v
```

Expected: missing element and JavaScript contract failures.

- [ ] **Step 3: Add dual trajectory editing**

In stage 5, add position/look-at mode buttons and `look-height`. Update `drawCamera()` to draw cyan solid `position.xy` paths and green dashed `look_at.xy` paths with K0-K4 markers. Use mode-based editing:

```javascript
const field=cameraEditMode==='look_at'?'look_at':'position';
bundle.cameras[currentCamera].states[k][field][0]=p[0];
bundle.cameras[currentCamera].states[k][field][1]=p[1];
```

Synchronize `look_at[2]` through `look-height`; retain position height, focal length, and Roll.

- [ ] **Step 4: Add the result loop controls**

Below the videos, add scope, feedback, “按意见重新规划”, and “按当前参数重新渲染”. Revision posts to current approved P and refreshes into camera review. Rerender posts to current M and polls the existing job endpoint; it never calls `/api/plans`.

- [ ] **Step 5: Run panel tests and confirm GREEN**

Run the Task 4 command. Expected: all panel tests pass, including Node execution.

- [ ] **Step 6: Commit Task 4**

```powershell
git add static/director_multicam_panel.html tests/test_director_multicam_panel.py
git commit -m "feat: add multicam feedback and look-at editor"
```

### Task 5: Full regression and real minimal verification

**Files:**
- Verify only; do not add synthetic success artifacts.

- [ ] **Step 1: Run the complete relevant unit suite**

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_deepseek_planner tests.test_multicam_plan tests.test_multicam_rig tests.test_director_multicam tests.test_director_multicam_compat tests.test_director_multicam_panel tests.test_director_wizard tests.test_director_wizard_http -v
```

Expected: zero failures and zero errors.

- [ ] **Step 2: Verify old single-camera startup**

Start `director-loop serve` on port 8771 with `runs/work/director_loop_humanoid_v1/station_reunion/director_loop_manifest.json`; require HTTP 200 from `/` and `/api/session`, five keyframes, and non-null camera reference. Do not modify its workspace.

- [ ] **Step 3: Perform one real DeepSeek revision validation**

Using the approved `runs/work/my_story` pipeline, submit one narrow Camera B feedback. Confirm one API call, zero retries, a new P, and exact Camera A/C state equality. If the API fails, report the real failure and do not synthesize a plan.

- [ ] **Step 4: Perform one real Blender rerender**

Use `D:\blender\blender.exe` and current successful M input to create one new M. Require `MULTICAM_PROXY_OK`, three MP4 files, and camera bundle SHA-256 equality with its source. Do not infer visual quality from automated checks.

- [ ] **Step 5: Inspect final repository scope**

```powershell
git status --short
git diff --check
git log -6 --oneline
```

Expected: only pre-existing `README.md` and `videoactagent.pdf` remain outside feature commits. If verification needs a legitimate test correction, commit only those test files with `test: verify multicam feedback loop`.
