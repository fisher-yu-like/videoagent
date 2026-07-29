# Stage 4 Camera Motion Evaluator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Quantify whether the real Kling prompt-only result exhibits the requested truck-right camera motion using a lightweight, reproducible background-translation baseline.

**Architecture:** The evaluator consumes actual first, middle, and last PNGs extracted from a generated video. It estimates pairwise background translation with FFT phase correlation on a configurable upper-frame crop, maps expected ShotScript camera motion to expected background direction, and emits a heuristic JSON verdict with raw shifts and confidence. This is an evaluation baseline, not ground-truth camera pose recovery.

**Tech Stack:** Python 3.12; NumPy FFT; Pillow image decoding; existing ShotScript parser; unittest.

---

## Evidence boundary

- Synthetic shifted patterns are used only to prove the estimator's sign and magnitude convention.
- Stage acceptance uses the actual Kling frames from `runs/stage3_api/20260729T012120Z_kling_d8bde2ef/frames/`.
- The verdict must be labelled `heuristic`; it cannot be reported as calibrated optical flow, SLAM, or ground-truth camera pose.
- Actor motion can contaminate global translation. The first version limits analysis to the upper 42% background crop and reports peak confidence.
- No API, server, or rented GPU is used.

## Files

- Create: `videoactagent/camera_eval.py`
- Create: `tests/test_camera_eval.py`
- Modify: `pyproject.toml`
- Create at runtime: `runs/stage4_camera_eval/kling_s01_camera_eval.json`
- Create: `docs/reports/2026-07-29-stage-4-camera-motion-evaluator.md`

## Task 1: Phase-correlation translation baseline

- [ ] Write `tests/test_camera_eval.py` first. Create a deterministic patterned array, translate it with `numpy.roll`, and assert `estimate_translation(reference, shifted)` returns the measured content displacement with the documented sign.
- [ ] Add a second test for identical images returning near-zero displacement and a finite confidence value.
- [ ] Run the focused test and observe import failure because `videoactagent.camera_eval` does not exist.
- [ ] Implement `Translation(dx, dy, confidence)` and `estimate_translation(reference, observed)` using mean removal, a 2D Hann window, normalized cross-power spectrum, inverse FFT peak location, and wraparound correction.
- [ ] Run focused tests and commit the estimator.

## Task 2: Motion verdict and CLI

- [ ] Extend the test first with the wished-for verdict API:

```python
result = evaluate_camera_motion(
    first_path,
    middle_path,
    last_path,
    expected_motion="truck_right",
    crop_fraction=0.42,
)
self.assertEqual(result["expected_background_dx"], "negative")
self.assertIn(result["verdict"], {"matched", "insufficient", "opposite"})
self.assertEqual(result["evidence_type"], "heuristic_phase_correlation")
```

- [ ] Add deterministic verdict tests: background `dx <= -5` is `matched` for truck right, `abs(dx) < 5` is `insufficient`, and `dx >= 5` is `opposite`.
- [ ] Run RED because verdict functions are missing.
- [ ] Implement pairwise first→middle, middle→last, and first→last estimates. Use first→last `dx` for the verdict, retain all raw measurements, input dimensions, crop bounds, and hashes in output.
- [ ] Implement CLI arguments `--first`, `--middle`, `--last`, `--shotscript`, `--shot`, and `--output`. Write JSON atomically and print `CAMERA_EVAL_OK` only after rereading it.
- [ ] Declare NumPy and Pillow runtime dependencies in `pyproject.toml`.
- [ ] Run focused and full tests, then commit.

## Task 3: Real Kling evaluation and report

- [ ] Run the CLI on the actual Stage 3 extracted frames and s01 ShotScript.
- [ ] Independently inspect the JSON, recompute input hashes, and compare its verdict with prior human visual inspection.
- [ ] Report raw `dx/dy`, confidence, threshold, verdict, limitations, and the exact source video hash.
- [ ] State explicitly that a failed/insufficient prompt-only camera verdict motivates first/last or proxy control but does not prove those controls will work.
- [ ] Run the full suite, `git diff --check`, and secret scan; commit the report without touching `.idea/`.
