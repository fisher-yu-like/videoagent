# Frozen-Boundary Director UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a source-bound frozen K-frame boundary, automatic actor/camera boundary points, and a plain-Chinese usage guide to the existing director loop.

**Architecture:** The server derives one exact inheritance contract from D0 ShotScript/semantic inputs or the current human annotation. The browser submits the selected frozen boundary plus an exact inherited prefix; the compiler rejects any changed locked value and accepts newly authored values only after the boundary. The UI displays locked and editable paths separately and keeps technical camera controls in a Chinese advanced section.

**Tech Stack:** Python 3.11, existing `DirectorAnnotation` compiler, ShotScript JSON, HTML canvas/JavaScript, `unittest`.

---

### Task 1: Source-bound inherited prefix

**Files:**
- Modify: `videoactagent/director_loop.py`
- Modify: `videoactagent/director_annotation.py`
- Modify: `tests/test_director_annotation.py`
- Modify: `tests/test_director_loop.py`

- [ ] **Step 1: Write failing inheritance tests**

Add tests proving D0 derives actor/camera K0–K4 values from ShotScript, D1 inherits the saved annotation, and changing any value through the selected boundary is rejected.

```python
def test_k2_boundary_rejects_changed_locked_actor(director_contract, director_payload):
    director_payload["frozen_through_keyframe"] = "K2"
    director_payload["inherited_locked_values"] = director_contract["inherited_keyframes"][:3]
    director_payload["keyframes"][1]["actors"]["actor_a"]["x"] += 0.01
    with pytest.raises(DirectorAnnotationError, match="locked inherited"):
        compile_director_annotation(director_payload, director_contract)
```

- [ ] **Step 2: Run RED**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_director_annotation tests.test_director_loop`

Expected: failures because boundary and inheritance fields are not accepted or exposed.

- [ ] **Step 3: Implement inheritance derivation and validation**

Add `derive_inherited_keyframes(workspace)` in `director_loop.py`. For D0, linearly evaluate ShotScript actor/camera starts and ends at each semantic `t`, calculate the configured look-at target, and reuse semantic visible-state text. For later versions, read the current hash-verified annotation. Return exact keyframes plus a canonical `inheritance_sha256`.

Extend the payload with:

```json
{
  "frozen_through_keyframe": "K2",
  "inheritance_sha256": "64 lowercase hex characters",
  "inherited_locked_values": ["exact K0 object", "exact K1 object", "exact K2 object"]
}
```

The compiler must require K0 through the selected boundary to equal the inheritance contract byte-for-byte after canonical numeric normalization. It must reject K4 as a boundary because no editable suffix would remain.

- [ ] **Step 4: Run GREEN and commit**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_director_annotation tests.test_director_loop tests.test_vace_coded_draft`

Expected: all focused tests pass.

Commit: `feat: freeze source-bound director prefixes`

### Task 2: Boundary-first Chinese UI

**Files:**
- Modify: `static/director_panel.html`
- Modify: `tests/test_director_loop.py`
- Modify: `README.md`

- [ ] **Step 1: Write failing UI contract test**

Assert that the page contains a required boundary selector, locked-start legend, Chinese labels for every camera term, an advanced-details section, and a Chinese step-by-step guide.

```python
assert 'id="frozen-boundary"' in html
assert "锁定起点" in html
assert "什么是景别" in html
assert "什么是插值" in html
assert "什么是画面倾斜" in html
```

- [ ] **Step 2: Run RED**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_director_loop.DirectorLoopTests.test_panel_explains_boundary_and_camera_terms_in_chinese`

Expected: failure because the new controls and guide are absent.

- [ ] **Step 3: Implement locked-prefix interaction**

Populate the canvas from `session.inherited_keyframes`. Selecting K2 copies K0–K2 into immutable form state, shows their actor/camera/look-at points and dashed paths, and clears editable K3–K4 values. Disable canvas and form mutation for locked keys. Render the boundary marker as `锁定起点 K2` and connect the first new point from it.

Use Chinese labels in ordinary mode. Put exact XYZ, interpolation, and roll inside `<details><summary>高级镜头参数</summary>`. Default recommendations remain visible as explanatory text rather than silently authored values.

- [ ] **Step 4: Add the Chinese guide**

Add a bottom section explaining the full workflow, boundary semantics, look-at, shot size, focal length, interpolation, and roll with recommended values. State explicitly that changing K2 requires selecting K1 as the boundary.

- [ ] **Step 5: Run focused tests, restart the page, and commit**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_director_annotation tests.test_director_loop tests.test_cli`

Expected: all pass. Restart the localhost service and confirm `/api/session` contains five inherited keyframes and the updated page returns HTTP 200.

Commit: `feat: guide frozen-boundary proxy revisions`
