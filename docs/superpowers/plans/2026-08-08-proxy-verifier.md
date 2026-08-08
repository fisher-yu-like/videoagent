# ProxyVerifier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with checkpoints.

**Goal:** Add a deterministic ProxyVerifier and immutable revision manager, then run them against the real station Proxy without making a VLM/API call.

**Architecture:** `proxy_verifier.py` reads canonical WorldState, optional DirectorPlan, CodeAgent evidence, render manifest, logs and real MP4s. It emits a structured report with passed/failed/unknown checks. `revision_manager.py` allocates `revision_NNN` directories and persists feedback/handoff metadata without overwriting any existing revision.

**Tech Stack:** Python standard library, existing `pipeline_v2.state.WorldState`, SHA-256, JSON, optional injected ffprobe probe function.

---

### Task 1: Add RED tests for verifier contracts

**Files:**
- Create: `tests/test_pipeline_v2_proxy_verifier.py`

- [ ] **Step 1: Write tests first**

  Cover the real station bundle at `pipeline_v2/runs/module3_station_20260808_019/blender_run_001/`: report schema, WorldState hash, 120 state frames, three camera logs, three videos, and non-failing deterministic checks. Add one test that invalidates a copied manifest hash and expects a failed provenance/media check. Add feedback category validation tests.

- [ ] **Step 2: Run RED**

  Run `python -m pytest -q tests/test_pipeline_v2_proxy_verifier.py`. Expected result: collection fails because `pipeline_v2.proxy_verifier` and `pipeline_v2.revision_manager` do not exist.

### Task 2: Implement deterministic ProxyVerifier

**Files:**
- Create: `pipeline_v2/proxy_verifier.py`
- Modify: `pipeline_v2/__init__.py`

- [ ] **Step 1: Implement the public contract**

  Add `verify_proxy(world_state_path, render_output_dir, director_plan_path=None, code_agent_evidence_path=None, probe_video=None) -> dict`. Return `proxy-verifier-1.0`, `verdict`, `checks`, `feedback_categories`, `required_human_review`, and source hashes. Use `passed`, `failed`, and `unknown`; never convert unavailable visual/physical evidence into pass.

- [ ] **Step 2: Implement plan checks**

  Compare entity/camera ids and frame metadata with WorldState, validate event frame/participant references, compare trajectory keyframes against `state_log.json`, and compare camera authored/applied records against CameraTrajectoryPlan. Record max errors and exact frame evidence.

- [ ] **Step 3: Implement media/provenance checks**

  Validate manifest schema, WorldState hash, relative video paths, file bytes and SHA-256. If `probe_video` is supplied, compare its real metadata; otherwise record media metadata as manifest-verified and visual duration as `unknown`.

- [ ] **Step 4: Run GREEN**

  Run the focused verifier test and expect all tests to pass on the real station bundle.

### Task 3: Add immutable RevisionManager

**Files:**
- Create: `pipeline_v2/revision_manager.py`
- Modify: `tests/test_pipeline_v2_proxy_verifier.py`

- [ ] **Step 1: Add failing tests**

  Test allocation of `revision_000`, then `revision_001`, rejection when a requested directory already exists, and feedback routing so only `appearance_only` produces an appearance route.

- [ ] **Step 2: Implement minimal manager**

  Add `allocate_revision(root, parent_revision=None)`, `record_feedback(revision_dir, feedback)`, and `build_handoff(feedback)`. Use exclusive directory creation and JSON written with a trailing newline. Never delete or replace old revisions.

- [ ] **Step 3: Run GREEN**

  Run focused tests and the existing 27 `pipeline_v2` tests.

### Task 4: Real station verification and documentation

**Files:**
- Create: `pipeline_v2/runs/module3_station_20260808_019/proxy_verifier_report.json`
- Modify: `README.md`

- [ ] **Step 1: Run verifier on real outputs**

  Use the existing real station WorldState and Blender bundle. Do not call GPT/VLM. Persist the report beside the real run and retain any `unknown` visual judgments.

- [ ] **Step 2: Update README**

  Document deterministic checks, human-first station approval, dataset VLM adapter routing, feedback categories, and revision directories.

- [ ] **Step 3: Final verification**

  Run the focused tests, inspect the real report, and report API/server usage and any unresolved `unknown` checks before asking for the human approval decision.
