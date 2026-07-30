# Full-Chain 24-Video Experiment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and validate a story-level experiment runner that produces 24 immutable Kling/Seedance/VACE jobs, executes six approved canaries first, and accepts only real complete videos for manual actor/camera evaluation.

**Architecture:** Keep `videoactagent.whole_story` as the immutable source layer. Add a small matrix compiler, a closed-API adapter that wraps the existing JD transport without `shot_id`, a result receiver that gates media before observation, and a VACE job exporter/server script. The runner is stateful and fail-closed: prepare is offline, canary and remainder releases are separate, every network/inference attempt is counted, and no operation retries automatically.

**Tech Stack:** Python 3.12, unittest, existing JD gateway transport, imageio-ffmpeg, Pillow, NumPy camera evaluator, Blender evidence, VACE/Wan2.1 on one A100 40GB.

---

## File map

- `configs/full_chain_matrix.json`: approved matrix, budgets, release gates and source paths.
- `videoactagent/full_chain.py`: strict config parsing, 24-job compilation, source snapshots and summary state.
- `videoactagent/whole_story_gateway.py`: Kling/Seedance story-level payload, submit, bounded query and download.
- `videoactagent/generated_result.py`: real decode, duration gate, five-frame extraction and observation preparation.
- `videoactagent/vace_full_chain.py`: whole-story VACE input/job exporter and returned-server-evidence validator.
- `scripts/run_vace_full_chain.sh`: one-job, zero-retry VACE server runner with CUDA metrics.
- `experiment.py`: one local entry with `prepare`, `canary-status`, and `remainder-status`; submission remains an explicit code path rather than the default.
- `tests/test_full_chain.py`: matrix and release-gate tests.
- `tests/test_whole_story_gateway.py`: API adapter/attempt-count tests using controlled transport only.
- `tests/test_generated_result.py`: real local MP4 completeness and observation-session tests.
- `tests/test_vace_full_chain.py`: VACE job/script/provenance tests without inference.
- `docs/EXPERIMENTS.md`: append actual canary and matrix results only after they exist.

### Task 1: Compile one immutable 24-job matrix

**Files:**
- Create: `configs/full_chain_matrix.json`
- Create: `videoactagent/full_chain.py`
- Create: `tests/test_full_chain.py`

- [ ] **Step 1: Write the failing matrix contract test**

```python
def test_matrix_has_24_story_level_jobs_and_exact_release_budgets():
    matrix = compile_matrix(Path("configs/full_chain_matrix.json"))
    assert len(matrix.jobs) == 24
    assert {job.backend for job in matrix.jobs} == {"kling", "seedance", "vace"}
    assert all("shot_id" not in json.dumps(job.document) for job in matrix.jobs)
    assert matrix.canary_story_ids == ("station_reunion", "studio_formation")
    assert len([j for j in matrix.jobs if j.release == "canary"]) == 6
    assert matrix.budgets == {
        "generation_submissions": 16,
        "status_queries": 64,
        "downloads": 16,
        "vace_inferences": 8,
        "automatic_retries": 0,
    }
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python -m unittest tests.test_full_chain -v`

Expected: import failure for `videoactagent.full_chain`.

- [ ] **Step 3: Add the exact approved config**

The JSON must bind `runs/work/whole_story_v4/summary.json`, list the eight frozen story IDs, set the two canaries, declare request duration 5.0, query limit 4, VACE frame count 81/fps 16/seed 2026, and set `submit=false` by default. It declares `release_policy="explicit_matrix_bound_token"`; no release token exists after prepare.

- [ ] **Step 4: Implement strict matrix parsing and compilation**

Implement frozen `ExperimentBudget`, `ExperimentJob`, `ExperimentConfig`, and `ExperimentMatrix` dataclasses. `load_experiment_config(path)` returns the validated config, `compile_matrix(path)` returns the three-backend Cartesian product with releases assigned from the canary IDs, and `write_prepared_matrix(matrix, output_dir)` snapshots all sources and returns the persisted summary object.

Validate schema version, safe story IDs, all eight v4 manifests and source hashes, unique `story_id/backend`, conditioning mode, adapter status, exact canary count and exact budgets. Snapshot the config, v4 summary, each case manifest, prompt, ShotScript and backend input file before publishing `matrix.json`. Publish to a new directory only and record every snapshot SHA-256.

- [ ] **Step 5: Add mutation and path-escape tests**

Tests must reject altered v4 proxy hash, changed submitted prompt hash, missing backend manifest, duplicate job, `../escape`, nonzero automatic retries, query limit other than four, and any config that enables remainder before canary.

- [ ] **Step 6: Run tests and commit**

Run: `python -m unittest tests.test_full_chain -v`

Commit: `feat: compile immutable full-chain matrix`

### Task 2: Add story-level Kling/Seedance adapter

**Files:**
- Create: `videoactagent/whole_story_gateway.py`
- Create: `tests/test_whole_story_gateway.py`
- Reuse: `videoactagent/backends/jd.py`
- Reuse: `videoactagent/run_record.py`

- [ ] **Step 1: Write failing payload tests**

```python
def test_story_payload_uses_root_duration_and_no_shot_id():
    prepared = prepare_api_job(matrix_job, output_dir)
    assert prepared.payload["parameters"]["duration"] == 5
    assert prepared.conditioning_mode == "prompt_only"
    assert "shot_id" not in json.dumps(prepared.payload)
    assert prepared.payload["content"] == [{"type": "text", "text": matrix_job.prompt}]
```

Add one assertion for the exact Kling model/parameters and one for Seedance 720p/no-watermark parameters.

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_whole_story_gateway -v`

Expected: import failure for `videoactagent.whole_story_gateway`.

- [ ] **Step 3: Implement offline preparation**

Implement `prepare_api_job(job: ExperimentJob, output_dir: Path) -> PreparedApiJob`. `PreparedApiJob` contains the immutable job directory, backend, conditioning mode, payload and request SHA-256.

It reads only snapshotted matrix inputs, builds with existing `build_kling_t2v`/`build_seedance_t2v`, writes `request.json`, `metadata.json`, `source_snapshot.json`, and `state.json` with counts all zero. It records `conditioning_mode=prompt_only`, `submit_retry_limit=0`, `query_limit=4`, and `download_limit=1`. It never reads credentials and never calls transport.

- [ ] **Step 4: Write failing single-attempt release tests**

Use controlled transport functions and assert:

```python
self.assertRaisesRegex(
    ReleaseError,
    "release is not approved",
    submit_prepared_job,
    prepared.path,
    "remainder",
    fake_transport,
)
assert fake_transport.calls == 0

task_id = submit_prepared_job(prepared, release="canary", transport=fake_transport)
assert task_id == "real-shape-task-id"
assert fake_transport.calls == 1
assert state["generation_submission_count"] == 1
```

Also assert a second submission is rejected before transport, a changed request/source is rejected, and credentials never appear in files.

- [ ] **Step 5: Implement submit/query/download wrappers**

Implement `submit_prepared_job(job_dir, release, transport=submit_once) -> str`, `query_prepared_job(job_dir, transport=query_once) -> dict`, and `download_prepared_job(job_dir, transport=download_once) -> Path` with the counters and limits below. The optional transport parameters exist only for controlled tests; production uses the existing JD functions.

Wrap existing `submit_once`, `query_once`, and `download_once`; do not add retry loops. Recheck all snapshots immediately before transport. Persist attempt records even on failure. Reject query count five and download count two. Separate submit/query/download counts.

- [ ] **Step 6: Run tests and commit**

Run: `python -m unittest tests.test_whole_story_gateway tests.test_jd_smoke -v`

Commit: `feat: add whole-story gateway adapter`

### Task 3: Gate real generated video before observation

**Files:**
- Create: `videoactagent/generated_result.py`
- Create: `tests/test_generated_result.py`
- Reuse: `videoactagent/module_io.py`
- Reuse: `videoactagent/trajectory_observe.py`
- Reuse: `videoactagent/camera_eval.py`

- [ ] **Step 1: Write failing real-media tests**

Use checked-in/ignored real local videos, never fabricated MP4 bytes:

```python
def test_five_second_api_video_is_complete_for_five_second_story():
    report = inspect_generated_video(real_5_04_second_mp4, requested_duration=5.0,
                                     expected_resolution=(1280, 720))
    assert report["status"] == "media_complete"

def test_same_video_is_incomplete_for_fifteen_second_request():
    report = inspect_generated_video(real_5_04_second_mp4, requested_duration=15.0,
                                     expected_resolution=(1280, 720))
    assert report["status"] == "incomplete"
    assert report["movement_scoring_allowed"] is False
```

Add a real v4 proxy test showing 5.0/5.0 passes its 960×540 profile and an unannotated complete video remains `complete_unscored`.

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_generated_result -v`

- [ ] **Step 3: Implement result inspection and atomic report**

Implement `inspect_generated_video(video, requested_duration, expected_resolution) -> dict` and `publish_result_report(job_dir, video, expected_profile) -> dict`. `ExpectedProfile` is a frozen dataclass containing duration, resolution and coverage bounds.

Use `module_io.inspect_video` and `whole_story.evaluate_output_completeness`. Record real frame count, counted/stream durations, codec, size, MP4 SHA and key-time decodability. Never allow scoring solely because the file exists.

- [ ] **Step 4: Prepare five real observation frames**

Select frame indices `[0, round(.25*(N-1)), round(.5*(N-1)), round(.75*(N-1)), N-1]` and call `trajectory_observe.prepare_session` on a private video snapshot. Create one manual actor session per expected actor ID. For camera, evaluate first-to-middle and middle-to-last background translation with `camera_eval.evaluate_camera_motion`; label it heuristic and require manual review.

- [ ] **Step 5: Add fail-closed observation tests**

Reject observation preparation for `incomplete`, changed MP4 hash, zero decoded frames, wrong resolution, missing key frame, and any attempt to report interpolated points as manual observation count.

- [ ] **Step 6: Run tests and commit**

Run: `python -W error::ResourceWarning -m unittest tests.test_generated_result tests.test_trajectory_eval tests.test_camera_eval -v`

Commit: `feat: gate generated results before observation`

### Task 4: Export whole-story VACE jobs and one-job server runner

**Files:**
- Create: `videoactagent/vace_full_chain.py`
- Create: `scripts/run_vace_full_chain.sh`
- Create: `tests/test_vace_full_chain.py`
- Reuse: `videoactagent/vace_inputs.py`
- Reuse: pinned commits in `runs/vace_matrix_20260730/preflight.json`

- [ ] **Step 1: Write failing VACE job tests**

```python
def test_vace_job_consumes_case_proxy_and_declares_five_seconds():
    job = export_vace_job(matrix_job, output_dir)
    assert job["story_id"] == matrix_job.story_id
    assert "shot_id" not in json.dumps(job)
    assert job["conditioning_mode"] == "source_video"
    assert job["inference"] == {
        "model_name": "vace-1.3B", "size": "480p", "frame_num": 81,
        "fps": 16, "seed": 2026, "sample_steps": 20,
    }
    assert hashlib.sha256(job_proxy.read_bytes()).hexdigest() == job["source_video"]["sha256"]
```

- [ ] **Step 2: Verify RED**

Run: `python -m unittest tests.test_vace_full_chain -v`

- [ ] **Step 3: Implement offline VACE input export**

Implement `export_vace_job(job, output_dir) -> dict` and `validate_vace_job(job_dir) -> dict`. Export returns the exact JSON written to disk; validation returns a source/hash/media report and rejects any mismatch.

Snapshot the v4 proxy/prompt/first frame, create and decode the VACE mask through the existing VACE input utilities, bind every file hash, and keep `seed=2026`. Validate pinned VACE commit `48eb44f1c4be87cc65a98bff985a26976841e9f3` and Wan commit `9737cba9c1c3c4d04b33fcad41c111989865d315` in the server contract.

- [ ] **Step 4: Write the zero-retry server script**

`scripts/run_vace_full_chain.sh <job.json> <output-dir>` must:

1. reject an existing output directory;
2. verify source hashes and pinned commits;
3. verify no existing GPU compute process;
4. launch exactly one `vace_wan_inference.py` process with frame_num 81, seed 2026, 20 steps;
5. poll `nvidia-smi` only for local GPU metrics;
6. record command, stdout, stderr, exit code, wall seconds and peak memory;
7. never run a retry command;
8. decode the returned MP4 and write its SHA/report.

- [ ] **Step 5: Add script/source audit tests**

Tests statically and behaviorally assert one inference invocation, no loop around inference, no `retry`, exact 81/2026/20 arguments, failure record on nonzero exit, and no success record without a decodable MP4.

- [ ] **Step 6: Run tests and commit**

Run: `python -m unittest tests.test_vace_full_chain tests.test_vace_inputs -v`

Commit: `feat: export whole-story VACE jobs`

### Task 5: Add the offline experiment entry and prepare all jobs

**Files:**
- Create: `experiment.py`
- Modify: `videoactagent/full_chain.py`
- Modify: `README.md`
- Test: `tests/test_full_chain.py`
- Runtime: `runs/work/full_chain_24_v1/`

- [ ] **Step 1: Write failing entry-point safety test**

`python experiment.py configs/full_chain_matrix.json prepare` must succeed offline. Running without an action or with `submit` must not submit. The only submission-capable library calls must require both an immutable, matrix-bound release token and an explicit release name.

- [ ] **Step 2: Implement the three-action entry**

Supported actions:

- `prepare`: compile/snapshot 24 jobs and write offline payload/VACE inputs;
- `canary-status`: summarize existing canary attempt/result directories without network;
- `remainder-status`: summarize existing remainder attempt/result directories without network.

Actual submit/query/download functions remain library calls invoked only after the user approval checkpoint; the user-facing default cannot call them accidentally. After approval, create an immutable `release_canary.json` containing the matrix SHA, release name and approved budgets. Remainder execution requires a separate `release_remainder.json`; neither token is created by `prepare`.

- [ ] **Step 3: Prepare the real 24-job offline run**

Run: `python experiment.py configs/full_chain_matrix.json prepare`

Verify 24 immutable jobs, 16 exact API request JSONs, 8 VACE jobs, six canary labels, source/hash consistency, no credential access, and summary counts of zero attempts.

- [ ] **Step 4: Run full local verification**

Run:

```powershell
python -W error::ResourceWarning -m unittest tests.test_full_chain `
  tests.test_whole_story_gateway tests.test_generated_result `
  tests.test_vace_full_chain -v
python -W error::ResourceWarning -m unittest discover -s tests -v
git diff --check
```

- [ ] **Step 5: Independent review and commit**

Review source snapshots, all 24 prepared jobs, attempt counters, release-token state and API/server call logs. Commit: `feat: prepare gated full-chain experiment`.

- [ ] **Step 6: Mandatory user checkpoint**

Report local files, test counts, prepared job hashes and exact planned next usage: four generation submissions, at most sixteen query requests, four downloads, two VACE inferences on one A100. Wait for explicit user permission before Task 6.

### Task 6: Execute and evaluate six real canaries

**Prerequisite:** Explicit user approval after Task 5. No approval means no API/server action.

**Runtime:** `runs/work/full_chain_24_v1/jobs/<story>/<backend>/`

- [ ] **Step 1: Recheck credentials, release and server preflight without submitting**

Create and hash `release_canary.json`, then report whether the JD key exists without printing it. Report server identity/GPU/model/commit checks. If server access or A100 is absent, stop and request the server endpoint/credentials; do not rent or infer automatically.

- [ ] **Step 2: Submit exactly four canary API jobs**

Submit Kling/Seedance for `station_reunion` and `studio_formation`. Persist one attempt each. No automatic retry.

- [ ] **Step 3: Run exactly two VACE canaries**

Run 81-frame jobs sequentially. If the first OOMs or fails completeness, stop before the second and report the real failure.

- [ ] **Step 4: Query/download within approved caps**

Use at most four status queries per API job and one download per completed task. Record every count and response. A pending fourth query becomes `unknown`, not failed/success.

- [ ] **Step 5: Apply real completeness gate and prepare observations**

Decode all available MP4s; incomplete results stop before movement metrics. Extract the five real frames for complete videos. Save manual actor annotations and camera heuristic/manual review separately.

- [ ] **Step 6: Report and stop**

Report six job statuses, video links/hashes, exact API request counts, VACE wall time/peak memory, completeness, visible control issues and failure evidence. Wait for explicit permission before Task 7.

### Task 7: Execute remaining 18 jobs and final report

**Prerequisite:** Explicit user approval after canary report.

- [ ] Execute exactly 12 remaining API submissions and 6 remaining VACE jobs with the same no-retry rules.
- [ ] Query/download only within the remaining global budgets.
- [ ] Gate, extract and manually annotate every complete result at five real times.
- [ ] Generate a 24-row result JSON/Markdown table and 24×3 frame contact sheet.
- [ ] Report per-backend completion, actor movement, camera evidence, failures and unknowns without combining them into a misleading score.
- [ ] Run the full test suite, hash audit and independent evidence review.
- [ ] Commit final local evidence summaries; do not commit MP4s or credentials and do not push without separate approval.
