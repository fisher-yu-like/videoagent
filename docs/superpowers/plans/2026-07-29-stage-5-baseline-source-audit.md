# Stage 5 Baseline Source Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax.

**Goal:** Pin and locally audit the official VACE and ReCamMaster source baselines before any GPU rental or weight download.

**Architecture:** A checked-in manifest records official repository URLs, exact commits, expected entry points, and intended project role. Shallow third-party checkouts remain ignored runtime dependencies. A standard-library audit CLI verifies the actual Git HEAD and required files, then emits a machine-readable report.

**Tech Stack:** Git; Python 3.12 standard library; official GitHub repositories.

---

## Evidence boundary

- A successful clone proves source availability only.
- File and dependency audit does not prove model inference, output quality, CUDA compatibility, or A100 memory fit.
- No model weights, datasets, server, GPU, or external generation API are used.

## Files

- Modify: `.gitignore`
- Create: `baselines/manifest.json`
- Create: `videoactagent/baseline_audit.py`
- Create: `tests/test_baseline_audit.py`
- Runtime clone: `third_party/VACE`
- Runtime clone: `third_party/ReCamMaster`
- Runtime report: `runs/stage5_baselines/audit.json`
- Create: `docs/reports/2026-07-29-stage-5-baseline-source-audit.md`

## Tasks

- [ ] Write a failing test that loads the manifest, expects VACE commit `48eb44f1c4be87cc65a98bff985a26976841e9f3`, ReCamMaster commit `fcf98bc86e876bb534518cd99e8a65b282f0f16e`, and invokes `audit_baselines()` against `third_party/`.
- [ ] Observe RED because the manifest/module/checkouts are absent.
- [ ] Add `third_party/` to `.gitignore` and create the manifest with official URLs, required files, role, and evidence boundary.
- [ ] Implement `baseline_audit.py` to run `git rev-parse HEAD`, compare exact commits, verify required files, hash requirements/README/entry scripts, and record availability without importing or executing model code.
- [ ] Shallow clone VACE and ReCamMaster, checkout the pinned commits, and rerun the focused test.
- [ ] Parse actual requirements and READMEs to record Python/PyTorch/CUDA/model-weight instructions and the minimum inference commands. Do not invent missing values.
- [ ] Run the persistent audit to `runs/stage5_baselines/audit.json`.
- [ ] Write a report recommending VACE as the first structural-control baseline and ReCamMaster as the later camera-rerender comparator, with explicit A100-stage prerequisites.
- [ ] Run the full suite, `git diff --check`, and secret scan; commit checked-in files only.
