# Stage 5 — Official Baseline Source Audit

Date: 2026-07-29
Status: `verified_source_only`

## Outcome

The official VACE and ReCamMaster repositories were cloned into the ignored
`third_party/` runtime directory and checked out at pinned commits. The audit
verified each Git HEAD and the presence and SHA-256 hashes of the selected
README, requirement, and inference-entry files.

This stage proves source availability and reproducibility of the checkout. It
does **not** prove that model weights can be loaded, that inference succeeds,
that an A100 40 GB has enough memory, or that either model produces usable
video for this project.

## Verified source checkouts

| Baseline | Intended role | Official source | Pinned and actual commit | Result |
| --- | --- | --- | --- | --- |
| VACE | Primary structural-control baseline | <https://github.com/ali-vilab/VACE> | `48eb44f1c4be87cc65a98bff985a26976841e9f3` | Exact match |
| ReCamMaster | Later camera-rerender comparator | <https://github.com/KlingAIResearch/ReCamMaster> | `fcf98bc86e876bb534518cd99e8a65b282f0f16e` | Exact match |

The machine-readable record is
`runs/stage5_baselines/audit.json`. Its SHA-256 is
`11bfb5a20a918c898439e1019a6f1d9f845890eb267ff20758479db60d5a1017`.
Each requested hash path is now represented by a stable
`exists`/`is_file`/`bytes`/`sha256` record; a missing path or directory cannot
produce `verified_source_only`.

## Facts taken from the pinned source

### VACE

- The package declares Python `>=3.10,<4.0`, PyTorch `>=2.5.1`, and
  torchvision `>=0.20.1`.
- The official installation instructions use the PyTorch 2.5.1 CUDA 12.4
  wheels.
- The Wan inference entry point accepts `src_video`, `src_mask`,
  `src_ref_images`, and `prompt`. This maps directly to our Blender preview,
  object/region masks, reference frames, and compiled shot prompt.
- The documented single-GPU starting point is Wan2.1 VACE 1.3B at roughly
  81 frames and 480×832. The 14B/720p path is not the first target.
- Official weights: <https://huggingface.co/Wan-AI/Wan2.1-VACE-1.3B>.

Minimum upstream-style invocation to adapt on the GPU server:

```bash
python vace/vace_wan_inference.py \
  --ckpt_dir /path/to/Wan2.1-VACE-1.3B \
  --src_video /path/to/source.mp4 \
  --src_mask /path/to/mask.mp4 \
  --src_ref_images /path/to/reference.png \
  --prompt "..."
```

The exact argument formatting and memory options will be taken from the pinned
entry point during the server smoke test; the command above is not yet a passed
experiment.

### ReCamMaster

- The requirements include PyTorch, torchvision, and `cupy-cuda12x`.
- The open implementation targets Wan2.1 T2V 1.3B, BF16, CUDA, 81 frames,
  and 480×832 inference.
- It needs both the Wan2.1 base-model files and the separate ReCamMaster
  checkpoint: <https://huggingface.co/KwaiVGI/ReCamMaster-Wan2.1>.
- The repository states that its released Wan2.1 port is not the same internal
  text-to-video model used for the paper results, so matching paper quality
  must not be assumed.
- The documented training path uses eight GPUs and is outside the first
  three-day server scope.

## Architecture decision

Use VACE Wan2.1 1.3B first. It is the shortest path from the existing control
bundle to a real structure-conditioned output and keeps our innovation in the
agent layer: shot scheduling, camera/actor constraints, compilation of Blender
previews into control inputs, and measured feedback.

Keep ReCamMaster as a later comparator for explicit camera rerendering. Do not
merge both model graphs into one complex pipeline initially. First determine
whether VACE preserves the intended blocking and camera cues; then compare a
small number of the same shots with ReCamMaster.

## A100-stage prerequisites and evidence gates

1. Verify the rented host's actual GPU, driver, OS, disk, and network.
2. Create an isolated Python 3.10 environment with the pinned PyTorch
   2.5.1/CUDA 12.4 wheels and confirm `torch.cuda.is_available()` on the real
   A100.
3. Install the pinned VACE source before downloading weights. Record the exact
   resolved dependencies.
4. Download only VACE 1.3B first and record model-file sizes and disk usage.
5. Run one short BF16, batch-1 smoke at 480p (prefer 49 frames first, then 81
   only if memory permits). Record command, logs, peak VRAM, runtime, seed,
   input hashes, output hash, and frames.
6. If out of memory, record the real failure and try only upstream-supported
   tiled/offload settings. Do not report an inferred or mocked pass.
7. Install or download ReCamMaster only after the VACE smoke evidence is
   archived.

## Deferred claims

- A100 40 GB fit: unverified until a real load/inference run.
- VACE output quality and control adherence: unverified.
- ReCamMaster output quality and paper-level parity: unverified and upstream
  explicitly cautions against assuming parity.
- Training feasibility within three days: not evaluated; inference-first is
  the approved scope.
