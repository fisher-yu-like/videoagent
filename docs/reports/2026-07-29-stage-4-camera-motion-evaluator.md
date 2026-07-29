# Stage 4 — Camera Motion Evaluator

Date: 2026-07-29

## Result

Stage 4 estimates global background translation from the upper 42% of the
real Kling frames using FFT phase correlation. For `truck_right`, negative
background `dx` is the expected direction. The first-to-last measurement is
`dx=-59 px`, `dy=0 px`, confidence `1.0803985977360666`.

The user-approved relaxed gate (`1.05`) reports the direction as `matched` for
debugging sensitivity. The fixed strict reference (`1.25`) remains
`inconclusive`. Neither verdict is camera-pose ground truth, and the relaxed
result is not used as closed-loop acceptance evidence.

| Frame pair | dx | dy | confidence | relaxed 1.05 | strict 1.25 |
|---|---:|---:|---:|---|---|
| first to middle | -33 px | 4 px | 1.015842046904826 | inconclusive | inconclusive |
| middle to last | -26 px | -4 px | 1.1980625495656918 | matched | inconclusive |
| first to last | -59 px | 0 px | 1.0803985977360666 | matched | inconclusive |

## Real evidence

- Video: `runs/stage3_api/20260729T012120Z_kling_d8bde2ef/result.mp4`
- Video SHA-256: `f3a95d76e898858726be8651f0472bafd0ff6a33e6c0e6743f5e23730aae35f3`
- Media: 1280×720, 121 frames, 24 fps, about 5.042 seconds
- Report: `runs/stage4_camera_eval/kling_s01_camera_eval.json`
- Report bytes: 3408
- Report SHA-256: `f3b0e1d41e03affc04334b9c6c891b3f04ced8aea8951d09f626a88a1403c390`

The report was regenerated with the current canonical atomic writer. The CLI
also rejects an output path that aliases any input, including a hardlink, so a
failed invocation cannot overwrite source frames or the ShotScript.

## Verification

- Stage 4 focused suite: 13/13 passed.
- External API calls: 0.
- Server/GPU use: none.
