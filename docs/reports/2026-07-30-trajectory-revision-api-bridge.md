# Trajectory feedback revision → JD gateway bridge

## Outcome

The bridge is implemented as an offline preparation step plus a fail-closed
submission gate. It does not submit during preparation. Both gateway backends
use the experiment-matrix condition name
`trajectory_compiled_feedback_revision`.

The prepared bundle binds:

- the original approved backend bundle SHA-256;
- all seven Task 6 source digests;
- the Task 8 revision artifact SHA-256 and its six source digests;
- the generation-1 operation allow-list;
- the exact revised prompt UTF-8 SHA-256;
- an explicit revision-artifact/revised-prompt binding.

Before credential lookup, run-directory creation, or transport, `jd_smoke`
strictly parses and validates that provenance. It then rechecks the bundle
snapshot immediately before creating the run. A valid invocation calls the
existing transport exactly once and records retry limit zero.

## Real source artifacts read

| Artifact | SHA-256 |
| --- | --- |
| Kling Task 8 revision | `07d65ff3a95e221357491dd78d8943f83f292cca97e4be55d23fdb4cf67a4e09` |
| Seedance Task 8 revision | `0260b5fe129bf2cf4db3d377bd6389cb78d52a7ea250b2433e18e1c99ddaea7d` |
| Kling approved Task 6 bundle | `edf29840aa49e133119b98fe61168805cf02d0751f42263270d8b659414aaa75` |
| Seedance approved Task 6 bundle | `faeccd2077b6fa54e8f1b97a0070fceb8fef02910aa7e0f1a2c397bfcd3a3238` |

No source artifact under `runs/` was changed.

## Fresh verification

Command:

```powershell
python -m unittest tests.test_trajectory_revision_backend tests.test_trajectory_backend tests.test_jd_smoke tests.test_jd_response_parsing
```

Result: **32 tests passed, 0 failed, 0 skipped**.

The tests cover both backend bundles, strict JSON duplicate/non-finite/oversized
number rejection, JSON-boolean type confusion, source and prompt tampering,
operation and decision allow-lists, immutable/no-clobber publication, hard-link
input collision, source replacement during validation, bundle replacement after
validation, semantic verdict-to-operation mapping, canonical operation order,
base/revision original-prompt binding, duration-to-integer API safety, rejection
before credential access, and exactly-one transport-call
mechanics with persisted secret scanning.

The transport call in the success-path test is intercepted. Therefore this
report proves local bridge mechanics and provenance gating only; it does not
claim a real API generation result.

## Usage

Prepare one immutable revised backend bundle:

```powershell
python -m videoactagent.trajectory_backend prepare-revision `
  --revision runs/trajectory_closed_loop/kling_s01_revision.json `
  --base-bundle runs/trajectory_api_pilot/prepared/kling/bundle.json `
  --backend kling `
  --workspace . `
  --output runs/trajectory_api_pilot/revised/kling/bundle.json
```

The matching single-call gateway command is:

```powershell
python -m videoactagent.jd_smoke submit-kling `
  --bundle runs/trajectory_api_pilot/revised/kling/bundle.json `
  --shot s01 `
  --prompt trajectory_compiled_feedback_revision `
  --run-root runs/trajectory_api_pilot/revised/real
```

Replace `kling` with `seedance` in the paths, backend, and submit command for the
Seedance pilot. Running a submit command is a real API action; none was run while
implementing or verifying this bridge.
