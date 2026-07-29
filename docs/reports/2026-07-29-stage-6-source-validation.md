# Stage 6: Real VACE Source Validation on A100

Date: 2026-07-29

## Result

The pinned VACE source-preprocessing path passed on the rented A100 for the
historical job with SHA-256
`e1f4f1b13265db05ee6a145aeb6607f1963885ccb07d24b5b8a1882c15becd71`. This result
covers the upstream `VaceVideoProcessor.load_video_pair` call, exact source and
mask provenance, tensor compatibility, and one real CUDA transfer. It does not
cover checkpoint loading or video generation; `inference_success` remains
`false`.

Security audit status (2026-07-30): these successful artifacts are retained as
historical evidence only. The current job was rebuilt after a destructive-path
regression test reproduced the old CLI overwriting its `--job` with a failure
report. The old report and validated job do not satisfy the strengthened
report-binding contract and must not be treated as validation of the current
job.

Server environment:

- GPU: NVIDIA A100-PCIE-40GB
- driver: `580.126.20`
- PyTorch: `2.5.1+cu124`
- CUDA build: `12.4`
- VACE commit: `48eb44f1c4be87cc65a98bff985a26976841e9f3`
- processor Git blob: `a0788111a7b79fda3070a2ab8372956c0726af26`

## Preserved attempts

The first two real attempts stopped at their gates and were retained rather
than rewritten:

1. Platform-dependent processor byte SHA mismatch. The Windows and Linux
   checkout bytes differed because of CRLF/LF conversion, while both had the
   same pinned Git blob. Preserved report SHA-256:
   `d707e6d19045ab49f1071b328988f9053a3d55f5af4b4b046f282f8abbbb4c52`.
2. A hand-estimated frame-id sequence differed from the actual upstream Decord
   sampling. Preserved report SHA-256:
   `104eb5fe520b419cfdb90a237999e5a05c73d89ee7bc1a4924b23f58223fe03e`.

The source gate was corrected to verify both the pinned commit blob and the
normalized Git blob of the exact working-tree file being imported. The frame
contract was corrected from the real upstream output, without weakening exact
sequence comparison.

## Passing evidence

The passing report is
`runs/stage6_vace_inputs/s01/source_validation.historical-old-job.json`:

- SHA-256:
  `813046fc4441ed249f21e598d82f32e172dc3038401cdc4dd591edcf3c6d7dae`
- elapsed time: `3.127739293500781` seconds
- sampled frame IDs:
  `[0, 1, 2, 3, 4, 6, 7, 8, 9, 10, 12, 13, 14]`
- returned FPS: `2.6896553493141537`
- source tensor: `[3, 13, 480, 832]`, finite, CUDA float32
- mask tensor: `[3, 13, 480, 832]`, finite, all ones
- normalized binary mask: `[1, 13, 480, 832]`, unique values `[1.0]`
- peak CUDA allocated/reserved bytes: `277411840 / 287309824`
- model constructed: `false`
- checkpoint loaded: `false`

The independent validated job is
`runs/stage6_vace_inputs/s01/vace_job.validated.historical-old-job.json`, SHA-256
`fcf235e7b2c4aae02098ae0ca1ea326e1565b4b9bbc14c9feb88a48a072ebc5e`.
The historical implementation changed
`evidence.source_validation_passed` from `false` to `true` while leaving
`evidence.inference_success` false, but did not bind the validated job to the
report path, byte count, SHA-256, or validation summary. That omission is why
the file is archived rather than published under the canonical validated-job
name.

## Strengthened publication contract

The repaired probe now completes read-only path preflight before any write. It
requires report and validated outputs to remain inside the job directory and
rejects path, symlink, and hard-link aliases of the job, Stage 2 bundle, proxy,
mask, and first/last reference frames. A new attempt first replaces any
canonical validated result with an explicit false/running state. It then
atomically publishes the report, reads
the actual published byte count and SHA-256, and embeds those values plus a
validation summary in the validated job. Failures atomically publish a failed
report and a report-bound `source_validation_passed: false` job. If even the
initial false marker cannot replace an old canonical validated file, the old
file is renamed to a UUID `.stale` path with a failure record.

No server, model, API, checkpoint, or inference run was performed during this
security remediation. Therefore no canonical `source_validation.json` or
`vace_job.validated.json` currently exists for the rebuilt job.

## Command

```bash
cd /root/videoactagent
/root/venvs/vace/bin/python -m videoactagent.vace_preprocess_probe \
  --job runs/stage6_vace_inputs/s01/vace_job.json \
  --vace-root third_party/VACE \
  --output runs/stage6_vace_inputs/s01/source_validation.json \
  --validated-job-output runs/stage6_vace_inputs/s01/vace_job.validated.json
```

The server GPU returned to `0 MiB` used and `0%` utilization after the
historical probe.
