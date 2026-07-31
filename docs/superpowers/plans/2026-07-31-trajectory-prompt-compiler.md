# Trajectory-Derived Prompt Compiler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace required human K-frame motion text with one deterministic, source-bound prompt compiled from actor/camera trajectories and optional enumerated style/mood choices.

**Architecture:** A new pure Python module converts normalized structural keyframes into stable motion/camera facts and composes them with verified story/appearance text. Director annotation validation owns schema/provenance and calls that module; both the read-only preview endpoint and immutable iteration preparation call the same annotation compiler.

**Tech Stack:** Python 3.11, `unittest`, existing HTTP server, HTML/JavaScript.

---

### Task 1: Pure deterministic prompt compiler

**Files:**
- Create: `videoactagent/trajectory_prompt.py`
- Create: `tests/test_trajectory_prompt.py`

- [ ] **Step 1: Write failing compiler tests**

Test byte-identical repeated output, decreasing versus increasing pair distance, stationary versus moving camera, non-invention of gestures/contact, enum phrases, and rejection of empty source story/appearance inputs.

- [ ] **Step 2: Run RED**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_trajectory_prompt`

Expected: import failure because `videoactagent.trajectory_prompt` does not exist.

- [ ] **Step 3: Implement the pure compiler**

Create `PROMPT_COMPILER_VERSION = "trajectory-facts-v1"` and `compile_trajectory_prompt(story_prompt, appearance_instruction, duration_seconds, keyframes, visual_style, mood) -> str`. Use a fixed `0.01` normalized displacement/distance tolerance, stable actor ordering, stable number formatting, and fixed enum mappings. Emit only observed movement, spacing, and camera facts plus continuity constraints.

- [ ] **Step 4: Run GREEN and commit**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_trajectory_prompt`

Expected: all compiler tests pass.

Commit: `feat: compile prompts from trajectory facts`

### Task 2: Structural annotation schema and read-only preview

**Files:**
- Modify: `videoactagent/director_annotation.py`
- Modify: `videoactagent/director_loop.py`
- Modify: `tests/test_director_annotation.py`
- Modify: `tests/test_director_loop.py`

- [ ] **Step 1: Write failing schema and preview tests**

Remove `visible_state` from submitted/canonical keyframes, require top-level `visual_style` and `mood`, assert canonical `prompt_compiler_version`, and assert `preview_prompt()` returns the same compiled text as `prepare_iteration` without changing the workspace file inventory or state.

- [ ] **Step 2: Run RED**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_director_annotation tests.test_director_loop`

Expected: schema/preview failures because free text is still required and no preview function exists.

- [ ] **Step 3: Integrate the compiler**

Make inherited and submitted keyframes structural (`id`, `t`, `actors`, `camera`; submitted frames additionally contain `camera_source`). Add verified story prompt and appearance instruction to the server-only director contract. Store style/mood and compiler version in canonical annotation, and generate `compiled_prompt` only through `compile_trajectory_prompt`.

- [ ] **Step 4: Add preview endpoint**

Add pure `preview_prompt(manifest_path, payload)` and `POST /api/prompt-preview`. It verifies current workspace/version and returns prompt, SHA-256, and compiler version without writing files or starting a thread.

- [ ] **Step 5: Run GREEN and commit**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_trajectory_prompt tests.test_director_annotation tests.test_director_loop tests.test_vace_coded_draft`

Expected: all focused compiler/director/VACE tests pass.

Commit: `feat: preview source-bound trajectory prompts`

### Task 3: Prompt-free K-frame UI

**Files:**
- Modify: `static/director_panel.html`
- Modify: `tests/test_director_loop.py`
- Modify: `README.md`

- [ ] **Step 1: Write failing UI contract test**

Assert the old per-K free-text control is absent and the page contains `visual-style`, `mood`, `prompt-preview`, `预览自动 Prompt`, `/api/prompt-preview`, and a read-only preview textarea.

- [ ] **Step 2: Run RED**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_director_loop.DirectorLoopTests.test_panel_uses_trajectory_prompt_preview`

Expected: failure because the old required Prompt remains.

- [ ] **Step 3: Simplify authoring state**

Remove editable `visible_state`, semantic-copy button, and Prompt missing check. Keep actors/camera required. Add fixed style/mood selects and build the new exact payload schema.

- [ ] **Step 4: Add explicit preview action**

Post the validated candidate to `/api/prompt-preview` only when the user clicks `预览自动 Prompt`; render returned text in a read-only textarea and display its compiler version/hash. Generation continues to compile independently on the server.

- [ ] **Step 5: Verify and commit**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_trajectory_prompt tests.test_director_annotation tests.test_director_loop tests.test_cli tests.test_vace_coded_draft`

Restart port 8769 and verify in a real browser that K3 saves with actor/camera values but no motion text, preview creates no workspace files, and returned preview equals the prompt persisted by the same compiler fixture.

Commit: `feat: replace K-frame text with prompt preview`
