# Repository Pipeline Cleanup Implementation Plan

> **For agentic workers:** Execute this plan inline with explicit verification before publishing.

**Goal:** Publish a coherent, reproducible VideoActAgent repository centered on the complete Prompt → Director → Blender Proxy → review → appearance/backend pipeline.

**Architecture:** Keep the existing runtime contracts and separate the semantic planning layer (`videoactagent`) from the auditable dynamic Blender path (`pipeline_v2`). Publish source, tests, examples and concise docs; exclude generated runs, media, weights, secrets and one-off patch scripts.

**Tech Stack:** Python 3.12, Blender 5.1, `pytest`, `json-repair`, optional TOS upload dependency, Seedance/Kling adapters.

---

### Task 1: Audit publishable files

- Keep `videoactagent/`, `pipeline_v2/`, `tests/`, `examples/`, `configs/`, `stories/`, `prompts/`, `static/`, and canonical scripts.
- Exclude `runs/`, media, model weights, caches, credentials, and one-off revision patch scripts.
- Confirm the GitHub remote and current branch before staging.

### Task 2: Rewrite project documentation

- Replace the root README with the overall pipeline, module map, contracts, quick start, review gate and reproducibility rules.
- Link detailed architecture, usage and experiment evidence under `docs/`.
- Do not advertise rejected Proxy or unfinished backend runs as completed results.

### Task 3: Make repository hygiene explicit

- Extend `.gitignore` for temporary test directories, local Blender files and generated proxy bundles.
- Add a small source-layout note so future experiments do not create root-level debug clutter.

### Task 4: Verify before publish

- Run targeted pipeline tests and `py_compile` on canonical scripts.
- Run `git diff --check` and inspect the staged file list.
- Confirm no API, video or secret files are staged.

### Task 5: Publish

- Create one cleanup commit on the current branch.
- Push the branch to `origin` and report commit, branch, files and test evidence.
