# Trajectory API Pilot Review Contract

## Evidence status

This file defines the Task 8 review gate; it is not itself a manual review and
does not claim that either backend passed trajectory adherence. The repository
contains persisted Kling and Seedance API pilot runs. Gate acceptance still
requires, for each backend, a real Task 7 manual evaluation and a separate JSON
review record with `manual_reviewed=true`.

Unit-test videos and test review records are mechanics-only evidence. They are
never accepted as experiment results in this report.

## Immutable review record

Create one JSON object for `kling` and one for `seedance`. The complete schema
is:

```json
{
  "schema_version": "0.1",
  "evidence_type": "real_api_trajectory_pilot_manual_review",
  "backend": "kling",
  "manual_reviewed": true,
  "source_artifacts": {
    "result_video": {
      "path": "runs/trajectory_api_pilot/real/<kling-run>/result.mp4",
      "sha256": "<sha256-of-exact-mp4-bytes>"
    },
    "evaluation_report": {
      "path": "runs/trajectory_observation/kling_s01/evaluation.json",
      "sha256": "<sha256-of-exact-evaluation-bytes>"
    },
    "api_audit": {
      "path": "runs/trajectory_api_pilot/real/<kling-run>/audit.json",
      "sha256": "<sha256-of-exact-audit-bytes>"
    },
    "trajectory": {
      "path": "runs/trajectory/s01/trajectory.json",
      "sha256": "<sha256-of-exact-trajectory-bytes>"
    },
    "session_manifest": {
      "path": "runs/trajectory_observation/kling_s01/session_manifest.json",
      "sha256": "<sha256-of-exact-session-manifest-bytes>"
    },
    "annotation": {
      "path": "runs/trajectory_observation/kling_s01/manual_annotation.json",
      "sha256": "<sha256-of-exact-annotation-bytes>"
    }
  }
}
```

All artifact paths are workspace-relative. The planner snapshots and hashes
all six files, reruns the complete Task 7 evaluator, and requires exact equality
with the persisted evaluation. It also rehashes every Stage 3 evidence JSON
listed by the audit and checks task IDs, status history, backend, official
gateway request, download record, and result-video binding. It rejects
duplicate JSON keys, unknown fields, path escapes, booleans used as numbers,
non-finite values, and oversized numeric literals.

The Seedance record uses the same schema with `backend` set to `seedance` and
its own exact paths and hashes. Do not copy placeholder digests from this
document into a review file.

## Bounded revision policy

Task 8 accepts only these operations, in this stable order:

1. `strengthen_direction`
2. `split_time_segments`
3. `reduce_amplitude`
4. `strengthen_screen_direction`
5. `simplify_orbit_to_truck`
6. `preserve_matched_control`

Every metric control receives a known verdict. A matched control maps only to
`preserve_matched_control`; mismatched controls map to their one allow-listed
operation. Operations are deduplicated without changing the order above. The
maximum output revision generation is 1.

## Formal matrix boundary

The plan contains exactly 24 jobs: four fixed scenes × Kling/Seedance × manual
text, trajectory-compiled, and one-feedback-revision conditions. It records
per-call and total cost fields. An exact submission argument array is emitted
only when that job's scene/backend/condition bundle exists, passes its real
bundle validator, contains a CLI-supported prompt choice, and is accepted by
the actual `jd_smoke` parser. Missing bundles are labelled
`preparation_required` and have a null command. Global submission remains
disabled unless both reviews and all 24 bundles pass. Planning never sends a request,
queries a task, or downloads a video. A user must execute any reviewed command
separately.

## Local planning evidence

An earlier offline planner produced
`runs/trajectory_experiment/matrix_pending_task8_20260730.json` without pilot
review inputs. Its SHA-256 is
`817f2fa6e9016f1dc9353d507615b31db7e227ae0a54049ee46117070d0f3351`
and its byte size is 21,736. Independent JSON inspection found 24 jobs,
4 scenes, 2 backends, and 3 conditions, with `submission_allowed=false`,
`network_called=false`, and `submitted=false`. The two explicit gate reasons
are `missing_kling_pilot_report` and `missing_seedance_pilot_report`.

That file predates the per-job bundle gate and must be treated as superseded,
not as an executable matrix or current acceptance evidence. Its byte/hash facts
remain historical local-planning evidence only.

## Completed real pilot and one bounded revision

The two original pilots were submitted exactly once per backend, queried until
terminal success, and downloaded exactly once. Independent Blender audits and
the Task 7 evaluator bound all results to the exact MP4 bytes.

| Backend | Original MP4 SHA-256 | Mean distance | Normalized endpoint | Normalized DTW |
|---|---|---:|---:|---:|
| Kling | `5cfce7a0a887bad62904dd05f9934f647770defa11b9c066be5636f1c34bd180` | 0.042759 | 0.005170 | 0.004883 |
| Seedance | `0cc51800c70f45f94314210ac590041a1e3f1ce3a913a375c4f4d24ce960bae2` | 0.096872 | 0.112788 | 0.059630 |

After manual review, each backend received one generation-1 revision with only
the bounded `split_time_segments` operation (plus
`preserve_matched_control`). No seed, model, duration, or resolution changed.
The revised calls were also submitted exactly once per backend, queried to
success, and downloaded exactly once.

| Backend | Revised MP4 SHA-256 | Mean distance | Normalized endpoint | Normalized DTW | Outcome |
|---|---|---:|---:|---:|---|
| Kling | `53c3bf8b67778882a5cefed654aebefc5ee0a5b317fc91920f48f340870778ca` | 0.075576 | 0.057554 | 0.053441 | worse than original |
| Seedance | `c6fbb1d98e14c1f90436ab79e3ee796bcaccc153bc7902d85f2925e4587f9deb` | 0.144585 | 0.125810 | 0.102237 | worse than original |

The revision did not improve trajectory adherence in this bounded pilot. This
is a real negative result, not a failed software test and not a reason to
silently search seeds or launch the 24-call matrix. The full matrix remains
closed because the other three scenes do not yet have hash-bound bundles.

Revision evidence hashes:

- Kling audit `7540833677958395f69c21baa739738ba2f5384507f36d91bb11255d336486be`;
  evaluation `4614e565d1b941f73636a9d7d8a09ed1921bab941e34118bf149d997876cbb5d`.
- Seedance audit `cffe52ca654cabcf4e26c584afe4617f936169f400a53a4a86a3723966d18240`;
  evaluation `5f48c29d41f350dc5e8a7bc395c6633bb85bd684544ed0b7878c6abe583383d8`.
