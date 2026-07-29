# API Trajectory Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local ATI-style trajectory authoring, compilation, API submission, and real-video evaluation pipeline without modifying the closed T2V generators.

**Architecture:** Store camera and single-actor intent in normalized JSON, render it over the existing Blender preview, compile it deterministically into ShotScript and timed prompts, submit through the existing JD API gateway, and compare real outputs with manually observed tracks. Every component exposes a focused CLI and produces hash-bound evidence that the local module inspector can audit.

**Tech Stack:** Python 3.10, stdlib HTTP server, JSON, NumPy, Pillow, imageio-ffmpeg, existing Blender integration, existing Kling/Seedance JD gateway, `unittest`.

---

### Task 1: Local Module I/O Inspector

**Files:**
- Create: `videoactagent/module_io.py`
- Create: `examples/module_io_manifest.json`
- Create: `tests/test_module_io.py`
- Modify: `docs/DEBUGGING.md`

- [ ] **Step 1: Write failing manifest and media-inspection tests**

Add tests that invoke the wished-for API against the existing real Stage 2 and
Stage 6 artifacts:

```python
from pathlib import Path
from videoactagent.module_io import inspect_manifest


def test_real_stage2_and_stage6_records_are_hash_and_media_verified():
    report = inspect_manifest(
        Path("examples/module_io_manifest.json"),
        workspace=Path("."),
    )
    by_id = {item["module_id"]: item for item in report["modules"]}
    assert by_id["stage2_control_bundle"]["status"] == "passed"
    assert by_id["stage6_source_validation"]["status"] == "passed"
    assert by_id["stage6_source_validation"]["outputs"][0]["sha256"] == (
        "813046fc4441ed249f21e598d82f32e172dc3038401cdc4dd591edcf3c6d7dae"
    )


def test_missing_required_output_returns_failed_not_passed(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        '{"schema_version":"0.1","modules":[{"module_id":"missing",'
        '"inputs":[],"outputs":[{"path":"missing.mp4","kind":"video",'
        '"required":true}]}]}',
        encoding="utf-8",
    )
    report = inspect_manifest(manifest, workspace=tmp_path)
    assert report["ok"] is False
    assert report["modules"][0]["status"] == "failed"
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
& $PY -m unittest tests.test_module_io -v
```

Expected: import failure because `videoactagent.module_io` does not exist.

- [ ] **Step 3: Implement the inspector and CLI**

Implement public functions `sha256_file(path: Path) -> str`,
`inspect_json(path: Path) -> dict[str, object]`,
`inspect_video(path: Path) -> dict[str, object]`,
`inspect_manifest(manifest_path: Path, workspace: Path) -> dict[str, object]`,
and `main(argv: list[str] | None = None) -> int`. `sha256_file` streams 1 MiB
chunks. `inspect_json` parses one object and records schema/evidence fields.
`inspect_video` decodes metadata and counts frames. `inspect_manifest` resolves
and inspects selected records, and `main` atomically persists the resulting
report and returns its real pass/fail status.

The parser is:

```python
parser.add_argument("command", choices=("inspect",))
parser.add_argument("--manifest", type=Path, required=True)
parser.add_argument("--workspace", type=Path, default=Path("."))
parser.add_argument("--module", action="append", default=[])
parser.add_argument("--all", action="store_true")
parser.add_argument("--output", type=Path, required=True)
```

Resolve every manifest path under `workspace`; reject absolute paths and `..`
escapes. JSON inspection records schema version and evidence fields. Video
inspection uses `imageio_ffmpeg.count_frames_and_secs` plus stream metadata.
Write output with UUID temporary file, file `fsync`, replace, failure cleanup,
and POSIX parent-directory `fsync`. Return `0` only when every selected required
record is present, decodable, and satisfies its declared hashes/bindings.

The initial manifest contains current real modules:

```json
{
  "schema_version": "0.1",
  "modules": [
    {
      "module_id": "stage2_control_bundle",
      "inputs": [],
      "outputs": [
        {"path": "runs/stage2_control_bridge/control_bundle.json", "kind": "json", "required": true}
      ]
    },
    {
      "module_id": "stage6_source_validation",
      "inputs": [
        {"path": "runs/stage6_vace_inputs/s01/vace_job.json", "kind": "json", "required": true}
      ],
      "outputs": [
        {
          "path": "runs/stage6_vace_inputs/s01/source_validation.json",
          "kind": "json",
          "required": true,
          "sha256": "813046fc4441ed249f21e598d82f32e172dc3038401cdc4dd591edcf3c6d7dae"
        },
        {
          "path": "runs/stage6_vace_inputs/s01/vace_job.validated.json",
          "kind": "json",
          "required": true,
          "sha256": "fcf235e7b2c4aae02098ae0ca1ea326e1565b4b9bbc14c9feb88a48a072ebc5e"
        }
      ]
    }
  ]
}
```

- [ ] **Step 4: Run focused and real CLI verification**

Run:

```powershell
& $PY -m unittest tests.test_module_io -v
& $PY -m videoactagent.module_io inspect --all `
  --manifest examples\module_io_manifest.json `
  --workspace . `
  --output runs\local_debug\module_io_report.json
```

Expected: tests pass; the CLI prints `MODULE_IO_OK` and writes a report whose
Stage 6 source validation is real while inference remains false.

- [ ] **Step 5: Commit**

```bash
git add videoactagent/module_io.py examples/module_io_manifest.json tests/test_module_io.py docs/DEBUGGING.md
git commit -m "feat: add local module IO inspector"
```

### Task 2: Canonical Trajectory JSON

**Files:**
- Create: `videoactagent/trajectory.py`
- Create: `examples/trajectory_circle_s01.json`
- Create: `tests/test_trajectory.py`
- Modify: `examples/module_io_manifest.json`

- [ ] **Step 1: Write failing schema and validation tests**

```python
from videoactagent.trajectory import TrajectoryInstruction


def test_round_trips_camera_circle_and_actor_curve():
    instruction = TrajectoryInstruction.from_path(
        Path("examples/trajectory_circle_s01.json")
    )
    assert instruction.scene_id == "station_platform"
    assert [track.target_type for track in instruction.tracks] == ["camera", "actor"]
    assert instruction.to_dict() == TrajectoryInstruction.from_dict(
        instruction.to_dict()
    ).to_dict()


def test_rejects_nonfinite_unsorted_or_duplicate_track_points():
    for mutation in invalid_trajectory_mutations():
        with pytest_raises(ValueError):
            TrajectoryInstruction.from_dict(mutation)
```

Use `unittest.TestCase.assertRaises` in the actual repository test; do not add
pytest as a dependency.

- [ ] **Step 2: Verify RED**

```powershell
& $PY -m unittest tests.test_trajectory -v
```

Expected: missing module import.

- [ ] **Step 3: Implement immutable dataclasses and CLI**

Create frozen dataclasses `TrajectoryPoint`, `TrajectoryTarget`,
`TrajectoryTrack`, and `TrajectoryInstruction`. Supported values are:

```python
TARGET_TYPES = {"camera", "actor", "anchor"}
PRIMITIVES = {"polyline", "circle", "static"}
CAMERA_SEMANTICS = {
    "pan_left", "pan_right", "truck_left", "truck_right",
    "dolly_in", "dolly_out", "orbit_clockwise",
    "orbit_counterclockwise", "zoom_in", "zoom_out",
}
```

Validate finite normalized coordinates, sorted unique time values in `[0, 1]`,
unique track IDs, non-empty point lists, scene/shot IDs, and target/primitive
compatibility. Add CLI
`validate --input examples/trajectory_circle_s01.json --output runs/trajectory/s01/trajectory.json`
that writes canonical JSON atomically and prints its SHA-256.

- [ ] **Step 4: Run focused tests and canonicalize the real example**

```powershell
& $PY -m unittest tests.test_trajectory -v
& $PY -m videoactagent.trajectory validate `
  --input examples\trajectory_circle_s01.json `
  --output runs\trajectory\s01\trajectory.json
```

Expected: canonical JSON contains one clockwise camera circle and one actor
polyline, with all coordinates in normalized preview space.

- [ ] **Step 5: Commit**

```bash
git add videoactagent/trajectory.py examples/trajectory_circle_s01.json tests/test_trajectory.py examples/module_io_manifest.json
git commit -m "feat: add canonical trajectory instructions"
```

### Task 3: Localhost Trajectory Editor

**Files:**
- Create: `videoactagent/trajectory_editor.py`
- Create: `videoactagent/static/trajectory_editor.html`
- Create: `tests/test_trajectory_editor.py`
- Modify: `docs/DEBUGGING.md`

- [ ] **Step 1: Write failing path-safety and save tests**

Test that the server binds to `127.0.0.1`, rejects absolute/escaping paths,
serves only the selected preview, accepts canonical JSON validated by Task 2,
and saves both `trajectory.json` and `trajectory_overlay.png` beneath the chosen
output directory. Invalid JSON must leave no partial output.

- [ ] **Step 2: Verify RED**

```powershell
& $PY -m unittest tests.test_trajectory_editor -v
```

- [ ] **Step 3: Implement the local server and canvas**

The CLI is:

```powershell
& $PY -m videoactagent.trajectory_editor `
  --image runs\stage2_control_bridge\shots\s01\first.png `
  --shotscript examples\station_shotscript.json `
  --shot s01 `
  --output-dir runs\trajectory\s01 `
  --port 8765
```

Use `ThreadingHTTPServer(("127.0.0.1", port), Handler)`. The HTML canvas supports
polyline, circle, and static tools; target selection; direction; progress; edit;
delete; save; and load. JavaScript serializes normalized points. The Python POST
handler validates through `TrajectoryInstruction.from_dict`, writes canonical
JSON atomically, and renders the overlay with Pillow.

- [ ] **Step 4: Run tests and manually save one real circle**

Run focused tests, launch the editor, draw one clockwise camera circle on the
real Stage 2 first frame, save, stop the server, and rerun Task 2 validation on
the saved JSON. Record the overlay SHA and dimensions.

- [ ] **Step 5: Commit**

```bash
git add videoactagent/trajectory_editor.py videoactagent/static/trajectory_editor.html tests/test_trajectory_editor.py docs/DEBUGGING.md
git commit -m "feat: add local trajectory editor"
```

### Task 4: Trajectory Compiler

**Files:**
- Create: `videoactagent/trajectory_compile.py`
- Create: `tests/test_trajectory_compile.py`
- Modify: `videoactagent/prompts.py`
- Modify: `examples/module_io_manifest.json`

- [ ] **Step 1: Write failing deterministic compilation tests**

Assert that the real camera circle always creates a clockwise orbit clause,
the actor curve creates start/end screen regions and an arrival interval, and
repeated compilation is byte-identical. Also assert that a local-deformation
target produces `unsupported_by_t2v_backend` and prevents submission readiness.

- [ ] **Step 2: Verify RED**

```powershell
& $PY -m unittest tests.test_trajectory_compile -v
```

- [ ] **Step 3: Implement compilation**

Expose `compile_trajectory(instruction: TrajectoryInstruction, shotscript:
ShotScript, shot_id: str) -> dict[str, object]`. It selects exactly one matching
shot, applies the explicit mapping table below, sorts tracks by ID and points by
time, and returns the complete output contract without writing files.

The output contract is:

```json
{
  "schema_version": "0.1",
  "scene_id": "station_platform",
  "shot_id": "s01",
  "trajectory_sha256": "sha256(canonical trajectory bytes)",
  "patched_camera": {},
  "patched_actors": [],
  "prompt": {
    "cinematic": "Orbit clockwise around the meeting actors while preserving the action axis.",
    "timed": ["0.0-5.0s: actor_a moves from the left region to the centre region."]
  },
  "backend_capability": {"t2v": "prompt_approximation"},
  "unsupported": []
}
```

Map primitives through explicit tables, not an LLM. Use screen regions
`left/centre/right` and `top/middle/bottom` with fixed boundaries `1/3` and
`2/3`. Serialize prompt clauses in stable track/time order.

- [ ] **Step 4: Run tests and compile the saved real trajectory**

```powershell
& $PY -m videoactagent.trajectory_compile `
  --trajectory runs\trajectory\s01\trajectory.json `
  --shotscript examples\station_shotscript.json `
  --shot s01 `
  --output-dir runs\trajectory\s01\compiled
```

Expected outputs: `compiled_control.json`, `trajectory_prompt.txt`, and
`patched_shotscript.json`, all hash-bound to the input trajectory.

- [ ] **Step 5: Commit**

```bash
git add videoactagent/trajectory_compile.py videoactagent/prompts.py tests/test_trajectory_compile.py examples/module_io_manifest.json
git commit -m "feat: compile trajectories into shot controls"
```

### Task 5: Blender Trajectory Overlay and Proxy

**Files:**
- Create: `videoactagent/trajectory_proxy.py`
- Create: `tests/test_trajectory_proxy_integration.py`
- Modify: `videoactagent/blender_proxy.py`
- Modify: `docs/DEBUGGING.md`

- [ ] **Step 1: Write a failing real Blender integration test**

Launch the configured Blender executable against the real ShotScript and
trajectory. Require a playable proxy MP4, first/last PNG, overlay PNG, render
manifest, exact input hashes, and visible non-background trajectory pixels.

- [ ] **Step 2: Verify RED**

```powershell
& $PY -m unittest tests.test_trajectory_proxy_integration -v
```

- [ ] **Step 3: Implement overlay/proxy generation**

Add curve objects and numbered control points to the Blender scene. Camera
tracks modify camera keyframes; actor tracks modify the selected actor's XY
keyframes; anchors render as non-moving markers. Preserve the existing proxy
renderer and emit an independent `trajectory_proxy_manifest.json`.

- [ ] **Step 4: Run the real Blender test and inspect frames**

Decode first/middle/last frames, view them, record observations, and fail on a
hash/dimension/frame-count mismatch. Do not substitute a Pillow-only fake proxy.

- [ ] **Step 5: Commit**

```bash
git add videoactagent/trajectory_proxy.py videoactagent/blender_proxy.py tests/test_trajectory_proxy_integration.py docs/DEBUGGING.md
git commit -m "feat: render trajectory-controlled proxies"
```

### Task 6: API Pilot Bundle and Two Real Calls

**Files:**
- Create: `videoactagent/trajectory_backend.py`
- Create: `tests/test_trajectory_backend.py`
- Modify: `videoactagent/jd_smoke.py`
- Modify: `examples/module_io_manifest.json`
- Create at runtime: `runs/trajectory_api_pilot/`

- [ ] **Step 1: Write failing preparation and safety tests**

Assert that the compiled trajectory hash and prompt are preserved in metadata,
missing credentials fail before run creation, unsupported controls prevent
submission, no credential is persisted, and submit retry limit is zero.

- [ ] **Step 2: Verify RED**

```powershell
& $PY -m unittest tests.test_trajectory_backend -v
```

- [ ] **Step 3: Implement API preparation**

Create an immutable API-ready bundle with `prompt_condition=trajectory_compiled`,
source trajectory/compiler/proxy hashes, and backend capability
`prompt_approximation`. Reuse the reviewed `submit-kling`, `submit-seedance`,
`query`, and `download` paths rather than creating a second transport.

- [ ] **Step 4: Perform exactly two real pilot submissions**

Submit one Kling and one Seedance call. Query at bounded intervals, download one
video per successful task, and record calls, task IDs, status history, byte
size, SHA-256, and decoded media metadata. A timeout remains unknown and is not
automatically resubmitted.

- [ ] **Step 5: Commit code and report pilot state**

```bash
git add videoactagent/trajectory_backend.py videoactagent/jd_smoke.py tests/test_trajectory_backend.py examples/module_io_manifest.json
git commit -m "feat: submit trajectory-compiled API pilots"
```

### Task 7: Observed Trajectory Annotation and Metrics

**Files:**
- Create: `videoactagent/trajectory_observe.py`
- Create: `videoactagent/trajectory_eval.py`
- Create: `videoactagent/static/trajectory_observer.html`
- Create: `tests/test_trajectory_eval.py`
- Modify: `examples/module_io_manifest.json`

- [ ] **Step 1: Write failing numeric and provenance tests**

Test temporal resampling, normalized endpoint error, direction match, arrival
error, and DTW against fixed numeric arrays. Reject observation JSON whose video
SHA, frame SHA, shot ID, or track ID differs from the actual files.

- [ ] **Step 2: Verify RED**

```powershell
& $PY -m unittest tests.test_trajectory_eval -v
```

- [ ] **Step 3: Implement the observer and evaluator**

The localhost observer extracts selected real frames and accepts one click per
controlled target per frame. It writes:

```json
{
  "schema_version": "0.1",
  "evidence_type": "manual_visual_annotation",
  "video_sha256": "ec1913b28a5cd2c3d7d8f1f1d527bc06601822c52e6e4d60537122fcbd90dc27",
  "track_id": "actor_path_01",
  "points": [{"frame": 0, "x": 0.1, "y": 0.5, "visible": true}]
}
```

The evaluator resamples requested and observed tracks to the same time base and
writes every intermediate distance plus aggregate metrics. Missing/occluded
points remain explicit and cannot be interpolated across the whole video.

- [ ] **Step 4: Annotate and evaluate both real pilot videos**

Use the real Kling and Seedance MP4s, save manual annotations, run evaluation,
and inspect the metric JSON. Also bind the existing Stage 4 automatic camera
report and its strict reference.

- [ ] **Step 5: Commit**

```bash
git add videoactagent/trajectory_observe.py videoactagent/trajectory_eval.py videoactagent/static/trajectory_observer.html tests/test_trajectory_eval.py examples/module_io_manifest.json
git commit -m "feat: evaluate requested and observed trajectories"
```

### Task 8: Bounded Closed Loop and Experiment Matrix

**Files:**
- Create: `videoactagent/trajectory_closed_loop.py`
- Create: `videoactagent/trajectory_experiment.py`
- Create: `tests/test_trajectory_closed_loop.py`
- Create: `docs/reports/2026-07-29-trajectory-api-pilot.md`
- Modify: `docs/DEBUGGING.md`

- [ ] **Step 1: Write failing revision-policy tests**

Cover every allowed operation, stable ordering, deduplication, preservation of
matched controls, rejection of unknown metrics/verdicts, and a maximum of one
revision generation. No test may invoke a real API.

- [ ] **Step 2: Verify RED**

```powershell
& $PY -m unittest tests.test_trajectory_closed_loop -v
```

- [ ] **Step 3: Implement fixed revision and experiment manifests**

Allow only:

```python
ALLOWED_OPERATIONS = {
    "strengthen_direction",
    "split_time_segments",
    "reduce_amplitude",
    "strengthen_screen_direction",
    "simplify_orbit_to_truck",
    "preserve_matched_control",
}
```

The experiment manifest fixes four scenes, two backends, and three conditions;
it records `submission_allowed=false` until pilot reports for both backends are
present and manually reviewed. It produces planned call count 24 and never
submits from the planning command.

- [ ] **Step 4: Run one reviewed revision, then prepare—not submit—the matrix**

After reviewing the pilot, allow one revised call per backend. Compare metrics
against baseline and compiled-only conditions. Generate the 24-call manifest,
cost/call-count estimate fields, and exact commands, but do not execute the full
matrix until the pilot report is accepted.

- [ ] **Step 5: Run full local verification and commit**

```powershell
& $PY -X tracemalloc=25 -W error::ResourceWarning -m unittest discover -s tests -v
```

Then:

```bash
git add videoactagent/trajectory_closed_loop.py videoactagent/trajectory_experiment.py tests/test_trajectory_closed_loop.py docs/reports/2026-07-29-trajectory-api-pilot.md docs/DEBUGGING.md
git commit -m "feat: close the trajectory control loop"
```

Expected: every local test passes; only persisted real API runs are described as
API evidence; the full experiment manifest remains non-submitting until the
pilot gate is explicitly accepted.
