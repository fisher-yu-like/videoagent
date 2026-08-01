# Trajectory Undo and Continuous Editing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users continuously edit K0–K4 in the multicamera page and locally undo or clear unsaved drawing without refreshing or deleting immutable S evidence.

**Architecture:** Keep all new behavior inside the existing vanilla-JavaScript multicamera page. Maintain a saved server snapshot, a mutable working copy, and an in-memory undo stack; every mutating UI action records one snapshot before changing the working copy. No backend endpoint or persistence format changes.

**Tech Stack:** HTML, CSS, vanilla JavaScript, Python 3.12 `unittest`, local standard-library HTTP server.

---

### Task 1: Add the editing-history contract

**Files:**
- Modify: `tests/test_director_multicam_panel.py`
- Modify: `static/director_multicam_panel.html`

- [ ] **Step 1: Write the failing static contract test**

Extend `test_independent_multicam_page_contract` with these exact requirements:

```python
for element_id in ("undo-staging", "clear-staging-drawing", "staging-dirty"):
    self.assertIn(f'id="{element_id}"', html)
for javascript_contract in (
    "savedTrajectory", "editHistory", "pushHistory",
    "undoStaging", "clearSelectedDrawing", "advanceStagingKeyframe",
):
    self.assertIn(javascript_contract, html)
self.assertIn("本次编辑：无未保存修改", html)
self.assertIn("本次编辑：有未保存修改", html)
```

- [ ] **Step 2: Run the panel test and verify RED**

Run:

```powershell
py -3.12 -m unittest tests.test_director_multicam_panel -v
```

Expected: FAIL because `undo-staging`, `clear-staging-drawing`, and the local history functions do not exist.

- [ ] **Step 3: Add controls and local state**

Add the following controls next to the existing staging buttons:

```html
<button id="undo-staging">撤销上一步</button>
<button id="clear-staging-drawing">清除所选目标的本次绘制</button>
<span id="staging-dirty" class="hint">本次编辑：无未保存修改</span>
```

Initialize local state beside the existing `session`, `trajectory`, and `bundle` variables:

```javascript
let savedTrajectory=null,editHistory=[];
function pushHistory(){editHistory.push(clone(trajectory));updateStagingEditState()}
function isDirty(){return JSON.stringify(trajectory)!==JSON.stringify(savedTrajectory)}
```

Implement `updateStagingEditState()` so undo is disabled when the stack is empty, clear is disabled when the selected target matches its saved baseline, and the hint reports the dirty state.

- [ ] **Step 4: Implement undo and clear semantics**

Implement `undoStaging()` by popping one complete trajectory snapshot, restoring it, rebuilding the target selector, preserving the selected target when it still exists, and redrawing.

Implement `clearSelectedDrawing()` with these exact branches:

```javascript
const saved=savedTrajectory.tracks.find(track=>track.target.id===selectedId);
pushHistory();
if(saved){
  const index=trajectory.tracks.findIndex(track=>track.target.id===selectedId);
  trajectory.tracks[index]=clone(saved);
}else{
  trajectory.tracks=trajectory.tracks.filter(track=>track.target.id!==selectedId);
}
```

Every canvas mutation and `add-object` operation must call `pushHistory()` immediately before changing `trajectory`. Button handlers call the two named functions without an API request.

- [ ] **Step 5: Implement continuous K advancement and save reset**

Add:

```javascript
function advanceStagingKeyframe(){
  const current=Number($("staging-keyframe").value.slice(1));
  if(current<4)$("staging-keyframe").value="K"+(current+1);
}
```

Call it after a successful canvas point mutation, followed by redraw. In `refresh()`, set `savedTrajectory=clone(session.staging.trajectory)` and `trajectory=clone(savedTrajectory)`, then clear `editHistory`; this makes a successful save the new undo baseline.

- [ ] **Step 6: Run the panel and backend regression tests**

Run:

```powershell
py -3.12 -m unittest tests.test_director_multicam_panel tests.test_director_multicam -v
```

Expected: all tests PASS.

- [ ] **Step 7: Commit**

```powershell
git add static/director_multicam_panel.html tests/test_director_multicam_panel.py
git commit -m "feat: add trajectory undo controls"
```

### Task 2: Verify real browser behavior and deploy locally

**Files:**
- Modify only if verification finds a tested defect: `static/director_multicam_panel.html`

- [ ] **Step 1: Run the full targeted suite**

Run:

```powershell
py -3.12 -m unittest tests.test_director_multicam tests.test_director_multicam_compat tests.test_director_multicam_panel tests.test_deepseek_planner tests.test_multicam_plan tests.test_multicam_rig tests.test_multicam_prompt tests.test_multicam_eval tests.test_multicam_suite tests.test_multicam_blender_integration -q
git diff --check
```

Expected: 28 or more tests PASS, including the real Blender integration; no API call occurs.

- [ ] **Step 2: Verify the local interaction**

Restart `director-multicam` on port 8770 with the existing station manifest. In the browser:

1. record actor_a K2 coordinates;
2. click a different canvas position and verify the selector becomes K3;
3. click `撤销上一步` and verify actor_a K2 returns to its original coordinates;
4. add one unsaved object, click `清除所选目标的本次绘制`, and verify the object disappears;
5. verify no new `staging/S*` directory exists because none of these local actions calls the API.

- [ ] **Step 3: Verify the service and repository state**

Request `http://127.0.0.1:8770/api/session` and confirm HTTP 200, `schema_version=multicam-director-1.0`, and the same current staging ID as before the browser test. Confirm `git status --short` is clean.

- [ ] **Step 4: Integrate the verified feature**

Merge the feature branch into `stage0-api-baseline`, rerun `tests.test_director_multicam_panel`, restart 8770 from the merged checkout, and preserve the immutable run directory.
