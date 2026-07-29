# Stage 6 VACE Input Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the existing real Stage 2 Blender control bundle into hash-addressed VACE Wan2.1 1.3B inputs, prove that the pinned VACE preprocessor accepts them on the rented A100 host, and only then run one separately reported short BF16 inference smoke.

**Architecture:** Keep the adapter model-independent: verify Stage 2 provenance, select one shot, materialize one deterministic full-generation mask from the real Blender frame count and dimensions, and emit a job manifest. A separate probe imports the pinned VACE preprocessor and validates real tensor shapes/ranges without loading model weights; model inference is a later evidence gate and can never be inferred from input-validation success.

**Tech Stack:** Python 3.12/3.10; Pillow; `imageio-ffmpeg`; SHA-256; pinned VACE commit `48eb44f1c4be87cc65a98bff985a26976841e9f3`; PyTorch 2.5.1/CUDA 12.4 on one A100 40 GB; unittest.

---

## Evidence boundary

- Acceptance inputs are the actual files under `runs/stage2_control_bridge/`; generated color bars, empty videos, copied sample media, and mocked VACE tensors are forbidden as acceptance evidence.
- Unit/TDD failures and passes prove adapter behavior only. Stage acceptance additionally requires a persistent run derived from the real `s01` Blender media and independently verified hashes.
- `source_validation_passed` means the pinned VACE video processor decoded and paired the real video/mask with valid shapes and ranges. It does **not** mean weights loaded, inference completed, camera control worked, or output quality is acceptable.
- `inference_success` may be set only after the one real BF16 process exits zero and its actual MP4 decodes. It still does not imply camera/actor adherence; that needs visual and metric evaluation in a later stage.
- No API call is part of Stage 6. Do not put SSH passwords, API keys, model tokens, or server credentials in commands, manifests, logs, reports, or Git.
- Plan writing and adapter/input validation download no model weights and perform no inference. The smoke task below is executed later, only after the input gate and server dependency report are archived.

## Exact Stage 2 → VACE mapping

| VACE input | Stage 2 source | Stage 6 policy |
| --- | --- | --- |
| `src_video` | `shots/<shot_id>/proxy.mp4` | Use the hash-verified real Blender proxy unchanged. For `s01` this is 15 frames at bundle FPS 3. |
| `src_mask` | Derived from real `frame_range`, bundle FPS, and actual `first.png` dimensions | Encode the same number of all-white frames. White means “generate” in VACE; this is the explicit equivalent of VACE's implicit all-one mask when a video is supplied without a mask. Do not claim it is actor segmentation. |
| `src_ref_images` | `shots/<shot_id>/first.png` | Pass only the real first frame as the identity/composition reference. Hash-check `last.png` as an endpoint provenance anchor, but do not misuse it as an unordered VACE reference image. |
| `prompt` | `prompts.cinematic` + `prompts.timed` | Join deterministically with one space; do not call an LLM or prompt-extension API. |

The first smoke uses `s01`, `vace-1.3B`, `480p`, base seed `2025`, and the source-derived 13-frame value (`4n+1`) produced by the pinned VACE processor from the 15-frame proxy. The short smoke is a compatibility test; its temporal duration and 4-step output are not a quality benchmark.

## Files

- Create: `videoactagent/vace_inputs.py` — verify provenance, create the real-data mask, and write the VACE job manifest.
- Create: `videoactagent/vace_preprocess_probe.py` — import the pinned upstream processor and emit source-validation evidence without weights.
- Create: `tests/test_vace_inputs.py` — TDD plus a real Stage 2 integration test.
- Modify: `pyproject.toml` — add the pinned-compatible `imageio-ffmpeg` runtime dependency.
- Create at runtime: `runs/stage6_vace_inputs/s01/src_mask.mp4`.
- Create at runtime: `runs/stage6_vace_inputs/s01/vace_job.json`.
- Create at runtime on the server: `runs/stage6_vace_inputs/s01/source_validation.json`.
- Create later on the server: `runs/stage6_vace_smoke/s01/` containing command, environment, logs, VRAM samples, output MP4, extracted frames, and hashes.
- Create after real execution: `docs/reports/2026-07-29-stage-6-vace-input-adapter.md`.

### Task 1: Lock the real-data adapter contract with TDD

**Files:**

- Create: `tests/test_vace_inputs.py`
- Create: `videoactagent/vace_inputs.py`

- [x] **Step 1: Write the failing provenance test against the real Stage 2 bundle**

```python
REAL_BUNDLE = Path("runs/stage2_control_bridge/control_bundle.json")

def test_builds_s01_job_only_from_hash_verified_stage2_media(self):
    self.assertTrue(REAL_BUNDLE.is_file(), "real Stage 2 bundle is required")
    with tempfile.TemporaryDirectory() as tmp:
        job_path = build_vace_inputs(REAL_BUNDLE, "s01", Path(tmp))
        job = json.loads(job_path.read_text(encoding="utf-8"))
    self.assertEqual(job["evidence_source"], "real_stage2_blender")
    self.assertEqual(job["vace"]["model_name"], "vace-1.3B")
    self.assertEqual(job["vace"]["size"], "480p")
    self.assertEqual(job["source"]["source_frame_count"], 15)
    self.assertEqual(job["source"]["expected_preprocessed_frames"], 13)
    self.assertEqual(job["mapping"]["src_ref_images"], ["source/shots/s01/first.png"])
    self.assertFalse(job["evidence"]["source_validation_passed"])
    self.assertFalse(job["evidence"]["inference_success"])
```

- [x] **Step 2: Add a failing tamper test**

Copy the real bundle and media to a temporary directory, change one byte in the copied `proxy.mp4`, and assert `build_vace_inputs()` raises `ProvenanceError` naming `proxy_video`. This is a negative integrity test, not acceptance media.

- [x] **Step 3: Run RED**

```powershell
& 'C:\Users\sy\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_vace_inputs -v
```

Expected: import failure because `videoactagent.vace_inputs` does not exist.

- [x] **Step 4: Implement the minimal provenance layer**

Implement `ProvenanceError`, `sha256_file(path)`, `load_verified_shot(bundle_path, shot_id)`, and `build_vace_inputs(bundle_path, shot_id, output_dir)`. Before creating output, recompute byte count and SHA-256 for `proxy_video`, `first_frame`, and `last_frame`; reject missing paths, traversal outside the bundle directory, mismatches, duplicate/missing shot IDs, non-positive FPS, and frame ranges whose inclusive length is not positive.

Use the actual first PNG for width/height, compute `source_frame_count = end - start + 1`, require `source_frame_count == round(duration * fps)`, and set `expected_preprocessed_frames = ((source_frame_count - 1) // 4) * 4 + 1`. Do not set either evidence flag to true.

- [x] **Step 5: Run the focused tests and commit**

```powershell
& 'C:\Users\sy\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_vace_inputs -v
git add -- videoactagent/vace_inputs.py tests/test_vace_inputs.py
git commit -m "feat: verify VACE input provenance"
```

Expected: the provenance tests pass using the real Stage 2 files.

### Task 2: Materialize and verify the deterministic VACE mask

**Files:**

- Modify: `pyproject.toml`
- Modify: `videoactagent/vace_inputs.py`
- Modify: `tests/test_vace_inputs.py`

- [x] **Step 1: Add the failing real-mask assertions**

Extend the real-data test to decode `src_mask.mp4` with `imageio_ffmpeg.read_frames()` and assert exactly 15 decoded frames, 960×540 dimensions taken from the actual `s01/first.png`, FPS 3, and every decoded RGB channel is at least 250 in the first, middle, and last frames. Independently recompute the mask SHA-256 and compare it with `vace_job.json`.

- [x] **Step 2: Run RED**

Expected: the mask file/mapping is absent.

- [x] **Step 3: Implement `write_full_generation_mask()`**

Add `imageio-ffmpeg` to `pyproject.toml`. Use its raw-frame writer with `pix_fmt_in="rgb24"`, the real PNG `(width, height)`, real bundle FPS, and exactly `source_frame_count` repetitions of `bytes([255]) * width * height * 3`. Close the generator in `finally`, decode the result once, verify the same count/dimensions and white threshold, and only then atomically publish `src_mask.mp4`.

Record this explicit policy in the manifest:

```json
{
  "mask_semantics": "white_generate_black_retain",
  "mask_policy": "full_frame_generate_from_real_stage2_dimensions",
  "actor_segmentation_claimed": false
}
```

- [x] **Step 4: Emit the deterministic job manifest**

`vace_job.json` must contain schema version, UTC creation time, source bundle path/hash, selected shot and source hashes, exact VACE commit, four-input mapping, prompt text and SHA-256, source/expected processed metadata, model/seed/size settings, and false evidence flags. Paths must be relative to the Stage 6 run directory or explicitly tagged as source-relative; never serialize a password or token.

- [x] **Step 5: Run focused/full tests and commit**

```powershell
& 'C:\Users\sy\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_vace_inputs -v
& 'C:\Users\sy\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest discover -s tests -v
git add -- pyproject.toml videoactagent/vace_inputs.py tests/test_vace_inputs.py
git commit -m "feat: materialize real VACE control mask"
```

### Task 3: Create persistent inputs from the real Blender shot

**Files:**

- Runtime: `runs/stage6_vace_inputs/s01/`

- [x] **Step 1: Run the adapter once on `s01`**

```powershell
& 'C:\Users\sy\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m videoactagent.vace_inputs prepare --bundle runs/stage2_control_bridge/control_bundle.json --shot s01 --output-dir runs/stage6_vace_inputs/s01
```

Expected terminal marker: `VACE_INPUTS_PREPARED` only after rereading the manifest and rechecking every hash.

- [x] **Step 2: Independently inspect persistent evidence**

Decode both proxy and mask; record actual frame counts, FPS, dimensions, and first/middle/last mask pixel ranges. Recompute all SHA-256 values outside the adapter and compare them with the manifest. Open the real first/last PNGs and three decoded proxy frames for visual inspection; record observations, not inferred success.

- [x] **Step 3: Stop on mismatch**

Any hash, count, dimension, FPS, or mask-value mismatch is a real failed experiment. Preserve logs and mark the run failed; do not substitute fixtures or manually edit the manifest.

### Task 4: Validate source tensors with the pinned VACE code on the A100 host

**Files:**

- Create: `videoactagent/vace_preprocess_probe.py`
- Create: `tests/test_vace_inputs.py`
- Runtime on server: `runs/stage6_vace_inputs/s01/source_validation.json`

- [ ] **Step 1: Write a failing manifest-state test**

Assert `merge_source_validation(job, report)` changes only `evidence.source_validation_passed` when the report has the exact VACE commit, matching input hashes, source shape `[3, 13, 480, 832]`, mask shape `[3, 13, 480, 832]`, first/last sampled frame IDs `0/14`, finite tensors, and binarized mask values `[1.0]`. It must leave `inference_success` false and reject hand-written/mismatched reports.

- [ ] **Step 2: Implement the probe without model construction**

Import `VaceVideoProcessor` from the pinned checkout and instantiate it with upstream 1.3B values: `downsample=(4,16,16)`, area `480*832`, FPS range `16`, `zero_start=True`, `seq_len=32760`, `keep_last=True`. Call upstream `load_video_pair(real_proxy, real_mask)`, reproduce only the upstream mask normalization `(mask[:1] + 1) / 2` and `>0.5` binarization, transfer tensors once to CUDA to prove the configured PyTorch/A100 path, and record actual shapes, frame IDs, spatial size, returned FPS, value ranges, finiteness, GPU identity, driver, torch/CUDA versions, exact Git HEAD, input hashes, elapsed time, and peak allocated/reserved bytes.

- [ ] **Step 3: Copy inputs without embedding credentials**

Use the configured SSH alias/key or an already authenticated session; do not place a password on the command line:

```powershell
scp -r runs/stage6_vace_inputs/s01 videoagent-a100:/root/videoactagent/runs/stage6_vace_inputs/
```

- [ ] **Step 4: Run the real preprocessing probe on the rented host**

```bash
cd /root/videoactagent
/root/venvs/vace/bin/python -m videoactagent.vace_preprocess_probe \
  --job runs/stage6_vace_inputs/s01/vace_job.json \
  --vace-root third_party/VACE \
  --output runs/stage6_vace_inputs/s01/source_validation.json
```

Expected: `VACE_SOURCE_VALIDATION_OK` and a real report. No checkpoint path is accepted by this command, so this task cannot silently load weights or run inference.

- [ ] **Step 5: Apply the gate and report immediately**

Copy the report back, verify its hash, and merge it into the job only through the validator. Report actual results and server/GPU usage. If import, decode, CUDA transfer, tensor shape, or mask checks fail, preserve the failure and stop before all model downloads/inference.

- [ ] **Step 6: Commit code after focused/full verification**

```powershell
git add -- videoactagent/vace_preprocess_probe.py videoactagent/vace_inputs.py tests/test_vace_inputs.py
git commit -m "feat: validate VACE source tensors"
```

### Task 5: Later one-shot BF16 inference gate (separate from Stage 6 input success)

This task is deliberately later. Execute it only after Task 4 passed and the exact VACE 1.3B weights are present and hash/size-audited. Do not retry with another seed or silently lower settings after failure.

- [ ] **Step 1: Archive environment and command before launch**

Write `environment.json` from actual `nvidia-smi`, Git HEAD, Python, torch, CUDA, package freeze, weight-file hashes/sizes, free disk, input hashes, and job settings. The command is one GPU, BF16, batch 1, `480p`, 13 frames, seed 2025, four sampling steps, model offload enabled, and prompt extension disabled:

```bash
python third_party/VACE/vace/vace_wan_inference.py \
  --model_name vace-1.3B --size 480p --frame_num 13 \
  --ckpt_dir /root/models/Wan2.1-VACE-1.3B \
  --src_video runs/stage2_control_bridge/shots/s01/proxy.mp4 \
  --src_mask runs/stage6_vace_inputs/s01/src_mask.mp4 \
  --src_ref_images runs/stage2_control_bridge/shots/s01/first.png \
  --prompt "<exact prompt from vace_job.json>" \
  --use_prompt_extend plain --base_seed 2025 --sample_steps 4 \
  --offload_model true --save_dir runs/stage6_vace_smoke/s01
```

- [ ] **Step 2: Capture real resource evidence**

Wrap the command with `/usr/bin/time -v`; concurrently sample `nvidia-smi --query-gpu=timestamp,memory.used,utilization.gpu --format=csv -lms 250` into `vram.csv`. Preserve stdout/stderr and exit code. Compute peak VRAM from the CSV and report wall time from the real logs.

- [ ] **Step 3: Validate output bytes, not terminal wording**

Require exit code 0, a non-empty `out_video.mp4`, successful independent decoding, exactly 13 frames, expected spatial dimensions, finite/non-constant pixel data, and saved first/middle/last PNGs. Record every output byte size and SHA-256. If OOM or another failure occurs, report that failure verbatim and do not claim pass.

- [ ] **Step 4: Keep claims separate**

Only then set `inference_success: true`. Record `control_adherence: unverified` until the real frames are visually inspected and evaluated. Four-step smoke frames cannot establish final quality, camera accuracy, multi-view consistency, or actor scheduling accuracy.

### Task 6: Final report and repository verification

**Files:**

- Create: `docs/reports/2026-07-29-stage-6-vace-input-adapter.md`

- [ ] **Step 1: Write the evidence report**

Include the exact mapping, real input/output hashes, local test commands/results, source-probe JSON hash, server GPU/time/VRAM usage, any failure, and visual observations. Use separate status fields for `adapter`, `source_validation`, `weight_load`, `inference`, and `control_adherence`; never collapse them into one “passed” label.

- [ ] **Step 2: Run final checks**

```powershell
& 'C:\Users\sy\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest discover -s tests -v
git diff --check
rg -n --hidden -g '!third_party/**' -g '!.git/**' '(password|api[_-]?key|secret|token)\s*[:=]'
```

Review every match manually. Preserve `.idea/` and unrelated user files.

- [ ] **Step 3: Commit only after evidence review**

```powershell
git add -- pyproject.toml videoactagent/vace_inputs.py videoactagent/vace_preprocess_probe.py tests/test_vace_inputs.py docs/reports/2026-07-29-stage-6-vace-input-adapter.md
git commit -m "docs: record Stage 6 VACE input evidence"
```
