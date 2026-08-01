# Locked-Prefix Render Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow an approved multicamera plan with a locked K0--K3 prefix to pass the render worker's source-bound validation.

**Architecture:** Preserve the existing immutable approval binding and strict plan loader. The worker will pass the lock value read from the already verified approval file into the same validator used during render preparation.

**Tech Stack:** Python 3.12 standard library, `unittest`, Blender 4.x command-line rendering.

---

### Task 1: Reproduce the worker-only lock mismatch

**Files:**
- Modify: `tests/test_director_multicam.py`

- [ ] **Step 1: Add a failing regression test**

Add `test_locked_suffix_plan_reaches_blender_worker_boundary`. Prepare and approve a K1-locked plan, create its render job, patch `videoactagent.director_multicam.subprocess.run` to return a controlled nonzero Blender result, run the job, then assert the patched process was called once and the recorded error is the controlled Blender failure rather than a lock mismatch:

```python
with patch("videoactagent.director_multicam.subprocess.run") as launch:
    launch.return_value.returncode = 1
    launch.return_value.stdout = ""
    launch.return_value.stderr = "controlled test stop"
    run_render_job(manifest, job)
self.assertEqual(launch.call_count, 1)
job_value = json.loads(job.read_text(encoding="utf-8"))
self.assertIn("real Blender multicamera render failed", job_value["error"])
```

- [ ] **Step 2: Verify the regression test fails for the original reason**

Run:

```powershell
& 'C:\Users\sy\AppData\Local\Programs\Python\Python312\python.exe' -m unittest tests.test_director_multicam.DirectorMulticamTests.test_locked_suffix_plan_reaches_blender_worker_boundary -v
```

Expected: FAIL because `launch.call_count` is zero and the job reports `locked prefix differs from the planning request`.

### Task 2: Propagate the approved lock into worker validation

**Files:**
- Modify: `videoactagent/director_multicam.py`

- [ ] **Step 1: Apply the minimal fix**

Change the worker's plan load to:

```python
plan = load_multicam_plan(
    _read(plan_path, "approved plan"),
    scene_id=document["story_id"],
    actors=_trajectory_targets(trajectory),
    locked_through_keyframe=plan_approval.get("locked_through_keyframe"),
)
```

- [ ] **Step 2: Verify the focused test passes**

Run the exact Task 1 command. Expected: one test passes.

- [ ] **Step 3: Run related regressions**

Run:

```powershell
& 'C:\Users\sy\AppData\Local\Programs\Python\Python312\python.exe' -m unittest tests.test_director_multicam tests.test_multicam_plan tests.test_multicam_blender_integration -v
```

Expected: all tests pass, including source-binding tamper rejection.

- [ ] **Step 4: Commit code and test**

Commit message: `fix: preserve locked prefix in render worker`

### Task 3: Run one real M2 render

**Files:**
- Create through the existing workflow: `runs/work/agent_multicam_suite_20260801/station_reunion/iterations/M2/`

- [ ] **Step 1: Restart the existing port 8770 service with Python 3.12**

Use the same station manifest and configured `D:\blender\blender.exe`; do not call any model API.

- [ ] **Step 2: Submit one P3 render operation**

Create M2 through the existing `/api/plans/P3/render` operation. Do not automatically retry.

- [ ] **Step 3: Inspect measured outputs**

Read M2 `job.json`, `render.log`, `render/multicam_manifest.json`, `evaluation.json`, and each recorded MP4. Report their actual status, sizes, hashes and duration. If Blender or automatic checks fail, preserve the failure and report it without claiming success.
