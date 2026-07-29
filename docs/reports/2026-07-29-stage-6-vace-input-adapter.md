# Stage 6 — VACE Input Adapter (Local Tasks 1–3)

Date: 2026-07-29

## Status

| Gate | Status | Evidence |
| --- | --- | --- |
| Adapter and provenance | `verified_local` | Real Stage 2 `s01` files, hashes, negative tamper tests |
| Mask materialization | `verified_local` | Real 15-frame 960×540 mask independently decoded |
| Persistent input inspection | `verified_local` | Blender 5.1.2 metadata, OS hashes, saved frames and `inspection.json` |
| Pinned VACE preprocessing | `not_run` | Requires the A100 server environment |
| Weight load | `not_run` | No weights downloaded or loaded |
| VACE inference | `not_run` | No GPU process launched |
| Control adherence | `unverified` | No VACE output exists yet |

No external generation API was called in this stage.

## Outcome

The real Blender control bundle can now be converted into a strict four-input
VACE job without calling an LLM or generation API. The adapter verifies the
Stage 2 bundle and all three media hashes before writing output. It rejects
missing files, tampering, path traversal, duplicate shot IDs, invalid timing,
manifest contract changes, fake evidence flags, false actor-segmentation
claims, and VACE configuration changes.

The selected first experiment remains deliberately small: `s01`, VACE
Wan2.1 1.3B, 480p, seed 2025. The 15-frame, 3 fps Blender proxy is expected to
be sampled by the pinned upstream processor to 13 frames (`4n+1`). This is an
input-contract claim derived from the pinned source; upstream preprocessing on
the A100 has not yet run.

## Exact mapping

| VACE input | Real project source |
| --- | --- |
| `src_video` | `runs/stage2_control_bridge/shots/s01/proxy.mp4` |
| `src_mask` | `runs/stage6_vace_inputs/s01/src_mask.mp4` |
| `src_ref_images` | `runs/stage2_control_bridge/shots/s01/first.png` |
| `prompt` | Exact deterministic join of the Stage 2 cinematic and timed prompts |

The mask policy is `white_generate_black_retain` with a full-white generated
frame. It preserves the real proxy as the structural control input. It is not
an actor segmentation mask, and the manifest fixes
`actor_segmentation_claimed` to `false`.

## Real persistent evidence

Run directory: `runs/stage6_vace_inputs/s01/`

| Artifact | Actual metadata | SHA-256 |
| --- | --- | --- |
| Stage 2 proxy | 92,783 bytes; 960×540; 15 frames; 3 fps; 5.0 s | `2fe40427bed79725cc18b0394d0c0fb2ab9943fe542d1196c0d2b0815c168b2e` |
| Generated mask | 2,273 bytes; 960×540; 15 frames; 3 fps; 5.0 s | `cbc0ef39ac29dc57dac06cb67111924c73f4ce1034bcbfda34cf16f79ab2029e` |
| VACE job | Four-input mapping and false evidence flags | `e1f4f1b13265db05ee6a145aeb6607f1963885ccb07d24b5b8a1882c15becd71` |
| Independent inspection | Blender metadata, hashes, mask samples, observations | `89388c900369664859b2a548b5d9472ead98e3db1bac7bc62725322e1ae1d6a1` |

Blender 5.1.2 independently reported both videos as 960×540, 15 frames, and
3 fps. Mask frames 1, 8, and 15 each had RGBA channel ranges `[1.0, 1.0]`.
The decoded sample PNGs have identical SHA-256 values because all three frames
are fully white.

Visual inspection of the real proxy frames shows actor A moving from the left
toward actor B, actor B remaining on the right, and the platform, tracks,
labels, and red action-axis guide remaining visible. Framing changes across
the samples. These observations prove that scheduled motion exists in the
control proxy; they do not prove VACE camera accuracy or output quality.

## TDD and debugging evidence

The implementation was not accepted on first green output. The recorded RED
cases included:

- missing `videoactagent.vace_inputs` module;
- absent mask output;
- a duplicate unselected shot ID being accepted;
- mask paths that were ambiguous or escaped the job directory;
- two reproducible FFmpeg pipe `ResourceWarning` instances;
- 17 of 18 execution-contract mutations initially being accepted;
- nine mask-metadata/time mutations initially being accepted;
- temporary mask files remaining after injected writer failures.

The FFmpeg warning root cause was the `imageio-ffmpeg` 0.6.0 generator leaving
stdin/stdout handles open when FFmpeg exited just before generator close. The
dependency is pinned to 0.6.0 and the exact race has a regression test. Failure
cleanup uses unique temporary paths and is unit-tested; those injected failure
tests are mechanics tests and are not acceptance evidence.

Final verification command:

```powershell
& 'C:\Users\sy\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -X tracemalloc=25 -W always::ResourceWarning -m unittest discover -s tests -v
```

Result: 45 tests run in 81.420 seconds, exit code 0, no failures and no
`ResourceWarning` output. Independent specification review and code-quality
review both approved local Tasks 1–3 after the contract fixes.

## Server boundary

The rented server is network-reachable, but none of the local SSH public keys
is authorized. Three key-only attempts returned exit code 255 with
`Permission denied (publickey,password)`. The Windows trusted interactive
control runtime is unavailable, so the supplied password was not placed in a
command, environment variable, script, or file. No remote diagnostics,
installation, download, GPU use, or inference has occurred.

The next automatic task is pinned VACE preprocessing on the A100. It remains
blocked until the project public key is added to the Matpool instance. Once
key access works, the first remote actions are read-only GPU/OS/disk checks,
followed by the pinned Python/PyTorch environment and source-only preprocessing
probe. Weight download and the one-shot BF16 inference remain separate gates.
