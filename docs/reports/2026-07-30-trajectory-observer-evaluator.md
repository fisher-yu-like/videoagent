# Task 7 — Manual Trajectory Observer and Evaluator

## Real pilot evaluation

Both API pilot MP4s were decoded at frames 0, 30, 60, 90, and 120. The moving
person was visually located at each frame through the module's validated
manual-annotation interface; no detector or synthetic coordinates were used.
For Seedance, the moving woman is not visible in frame 0, so that point is
explicitly marked occluded rather than interpolated.

The evaluator then independently re-decoded each original MP4, checked every
selected PNG hash, checked trajectory/session/annotation identity and timing,
and recomputed the metrics from those source bytes.

| Metric | Kling | Seedance |
|---|---:|---:|
| compared samples | 121 | 91 |
| occluded samples | 0 | 30 |
| direction cosine | 0.876756 | 0.637167 |
| direction match | true | true |
| mean distance | 0.042759 | 0.096872 |
| normalized endpoint error | 0.005170 | 0.112788 |
| normalized DTW cost | 0.004883 | 0.059630 |
| observed arrival | 0.658333 | unavailable |
| arrival error | 0.175000 | unavailable |

Kling follows the requested left-to-centre actor path closely, but reaches the
endpoint region earlier than requested. Seedance preserves the overall
direction but has a larger endpoint/path error and never enters the requested
endpoint tolerance. These are actor-image trajectory measurements; they are
not camera-pose ground truth.

## Hash-bound evidence

Kling:

- video SHA-256: `5cfce7a0a887bad62904dd05f9934f647770defa11b9c066be5636f1c34bd180`
- session manifest: `b3a372513015ed99b543c3a6b790f7b467db1c56df2f0ce2371a3af1454c3d45`
- manual annotation: `1af2a09cc4139df994422207dc1819d33f5292bd6c9a15effc522a0e6b2d4239`
- evaluation: `a0be8fc5f2caf3e089f18e9adada0d808b645dd0dc7317307f3861720b2513bf`

Seedance:

- video SHA-256: `0cc51800c70f45f94314210ac590041a1e3f1ce3a913a375c4f4d24ce960bae2`
- session manifest: `a8b539d2bb91c860fa50156625d1199d7f4508a053818eb7cbd65f8f96d58cc0`
- manual annotation: `38ea7217f6f0d05df5ca6583aa0a694f7a76c76b59d232edba9f21b77c714790`
- evaluation: `4281bf331e18089dc63b2673aef8ff2266f4ea75516c9c52b298db5e92b99d7b`

## Mechanism verification

Fresh strict Task 7 verification: 28 tests passed, one Windows symlink test
was skipped because the current account lacks symlink privilege, and there
were zero failures or errors. The independent review approved source snapshot
handling, frame timing, Host/Origin checks, UI loading gates, occlusion,
partial endpoints, single-sample static tracks, and direct/hardlink output
collision protection.
