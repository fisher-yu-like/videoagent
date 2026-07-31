# Camera Auto-Initialization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make editable K-frame cameras initialize from the current Proxy without requiring an inherit-then-draw order, while preserving exact provenance and reporting specific missing fields.

**Architecture:** The browser initializes only editable camera objects from the hash-bound `session.inherited_keyframes`, leaves actors and Prompt blank, and derives `camera_source` by comparing the submitted camera with its inherited source. The Python compiler normalizes and verifies that provenance before persisting the canonical annotation.

**Tech Stack:** Python 3.11, HTML canvas/JavaScript, `unittest`.

---

### Task 1: Validate camera provenance

**Files:**
- Modify: `videoactagent/director_annotation.py`
- Modify: `tests/test_director_annotation.py`

- [ ] **Step 1: Write failing provenance tests**

Add `camera_source` to every annotation keyframe. Assert that an unchanged editable camera accepts `inherited`, a changed camera accepts `human_modified`, and either false claim raises `DirectorAnnotationError` containing `camera_source`.

- [ ] **Step 2: Run RED**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_director_annotation`

Expected: failure because `camera_source` is not an accepted keyframe field.

- [ ] **Step 3: Implement minimal provenance validation**

Normalize the camera first, compare it with the matching normalized inherited camera, require `inherited` when equal and `human_modified` when different, and preserve the value in `canonical_annotation`.

- [ ] **Step 4: Run GREEN and commit**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_director_annotation tests.test_director_loop tests.test_vace_coded_draft`

Expected: all focused tests pass.

Commit: `feat: validate inherited camera provenance`

### Task 2: Initialize editable cameras and explain missing fields

**Files:**
- Modify: `static/director_panel.html`
- Modify: `tests/test_director_loop.py`
- Modify: `README.md`

- [ ] **Step 1: Write failing UI contract tests**

Assert the page includes `camera_source`, the visible labels `来自当前 Proxy，可修改` and `人工修改`, a `missingFields` helper, and the exact prefix `还缺少：`.

- [ ] **Step 2: Run RED**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_director_loop.DirectorLoopTests.test_panel_auto_initializes_camera_with_provenance`

Expected: failure because the old page exposes none of these behaviors.

- [ ] **Step 3: Implement automatic initialization**

When a boundary is selected, copy each editable keyframe's inherited camera into form state but leave actors and `visible_state` empty. Drawing camera position/look-at changes only X/Y. Replace the inherit button behavior with a merge that fills missing camera fields without overwriting non-null X/Y.

- [ ] **Step 4: Implement provenance and missing-field reporting**

Compare each outgoing camera to `session.inherited_keyframes[i].camera` to emit `camera_source`. Add `missingFields(i)` covering every actor, Prompt, camera position/look-at, focal length, shot size, interpolation, and roll; use its Chinese labels in Save and Generate errors.

- [ ] **Step 5: Run focused tests and real browser check**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_director_annotation tests.test_director_loop tests.test_cli tests.test_vace_coded_draft`

Expected: all focused tests pass. Restart port 8769, select K2, and verify K3 has a complete inherited camera, blank actors/Prompt, and switches its source label after a camera edit.

- [ ] **Step 6: Commit**

Commit: `fix: remove camera initialization order dependency`
