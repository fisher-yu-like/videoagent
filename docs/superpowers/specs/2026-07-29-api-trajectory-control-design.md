# API Trajectory Control Design

Date: 2026-07-29

## Objective

Add an API-agnostic trajectory instruction layer to VideoActAgent. A user draws
camera or actor motion on the locally generated Blender preview. The system
stores that intent as inspectable JSON, compiles it into ShotScript changes and
time-segmented cinematic prompts, submits the prompt to a black-box T2V API,
and compares the resulting real video with the requested trajectory.

The primary generation backends remain Kling and Seedance APIs. Open-source
generators are optional research baselines and are not required by this design.

## Research Position

[ATI](https://github.com/bytedance/ATI) injects trajectory features into a
Wan2.1-I2V-14B latent representation through a learned motion injector. It uses
an initial image and dense trajectory tensors, so it cannot be connected
directly to a text-only closed API.

VideoActAgent adopts ATI's interaction concept and unified treatment of camera,
object, and local motion, but changes the execution mechanism. Its central
contribution is an **API-agnostic Trajectory Instruction Layer**:

1. a backend-independent trajectory language;
2. executable grounding through a Blender/code preview;
3. deterministic compilation into ShotScript and timed prompts;
4. black-box trajectory-adherence evaluation and bounded revision.

The method must be described as training-free, approximate black-box control.
It must not claim ATI-level pixel control when the backend accepts text only.

## Scope

Version 0.1 supports:

- camera pan, truck, dolly, orbit/circular motion, and zoom;
- one actor following a line or curve with an arrival interval;
- static anchor points used to preserve composition;
- Kling and Seedance T2V submission through the existing JD gateway;
- manual observed-track annotation and camera-motion heuristics;
- one bounded feedback revision.

Version 0.1 does not support:

- local limb or cloth deformation tracks;
- identity-grounded multi-actor pixel trajectories;
- learned adapters or generator fine-tuning;
- claims of exact coordinate reproduction by T2V APIs;
- unattended high-volume API submission.

## Architecture

```text
Story / ShotScript
        |
        v
Blender preview first frame
        |
        v
Local trajectory editor ---> trajectory.json
        |                         |
        +-------------------------+
        v
Trajectory compiler
  |- patched_shotscript.json
  |- timed_prompt.json
  |- trajectory_overlay.png
  |- trajectory_proxy.mp4
  `- backend_capability.json
        |
        v
Kling / Seedance T2V API
        |
        v
Real result.mp4
        |
        v
Manual observed track + camera evaluator
        |
        v
trajectory_eval.json ---> bounded revision.json
```

## Components

### 1. Module I/O Inspector

`videoactagent.module_io` reads a declarative module manifest and verifies
persisted inputs and outputs without executing an API or model. It reports:

- file existence, byte size, and SHA-256;
- JSON schema/version and selected semantic fields;
- decoded MP4 dimensions, frames, FPS, and duration;
- input/output hash bindings;
- evidence type: real, manual, mechanics-only, missing, or stale;
- an overall non-zero exit status if required evidence is missing or stale.

The command writes a machine-readable report and prints a compact terminal
table. It never treats a mock response or filename alone as successful evidence.

### 2. Trajectory Schema

The canonical file is UTF-8 JSON rather than ATI's serialized PyTorch tensor.
Coordinates are normalized so plans survive changes in preview resolution.

```json
{
  "schema_version": "0.1",
  "scene_id": "station_platform",
  "shot_id": "s01",
  "coordinate_space": "normalized_0_1_top_left",
  "duration_seconds": 5.0,
  "sample_count": 121,
  "tracks": [
    {
      "track_id": "camera_orbit_01",
      "target": {"type": "camera", "id": "camera"},
      "primitive": "circle",
      "semantic": "orbit_clockwise",
      "points": [
        {"t": 0.0, "x": 0.35, "y": 0.50, "visible": true},
        {"t": 1.0, "x": 0.65, "y": 0.50, "visible": true}
      ]
    }
  ]
}
```

Validation rejects duplicate IDs, non-finite coordinates, times outside
`[0, 1]`, unsorted points, unsupported targets/primitives, and mismatched
scene/shot identity.

### 3. Local Trajectory Editor

A small localhost-only browser canvas displays the Blender preview. It supports:

- polyline/free trajectory;
- circle/orbit trajectory with clockwise/counter-clockwise direction;
- static points;
- duration/progress control;
- camera versus actor target selection;
- save, load, delete, and edit.

The editor writes only the trajectory JSON and an overlay PNG. It does not
submit an API request. The server binds only to `127.0.0.1` and rejects paths
outside the workspace.

### 4. Trajectory Compiler

The compiler deterministically maps trajectory semantics into three layers:

- ShotScript keyframes and screen-direction constraints;
- time-segmented natural-language prompt clauses;
- a Blender overlay/proxy for inspection before API submission.

Camera mappings include:

- circle plus clockwise direction -> clockwise orbit/arc;
- translated circle centre -> pan/truck component;
- increasing radius -> dolly/zoom out;
- decreasing radius -> dolly/zoom in;
- near-static points -> composition anchors.

Actor mappings express normalized start/end region, curve direction, arrival
time, facing, and screen direction. Unsupported local deformation is emitted as
`unsupported_by_t2v_backend`, never silently reduced to a successful control.

### 5. API Adapter

The compiler produces a new control bundle compatible with the existing
`videoactagent.jd_smoke` gateway. Every real submission creates a new immutable
run directory with request, response, task ID, query history, result MP4, and
hash records. The first pilot allows one submission per backend and no automatic
retry.

### 6. Observed Track Annotation and Evaluation

For the first version, a user clicks the controlled actor or composition point
on selected frames from the real API video. This produces an inspectable
`observed_trajectory.json`; it is labelled manual evidence. Camera motion is
evaluated independently with the existing background-motion heuristic.

Metrics are:

- normalized endpoint error;
- dynamic time warping distance after temporal resampling;
- requested versus observed direction;
- arrival-time error;
- camera-direction verdict and strict confidence reference;
- manual semantic-action verdict.

Automated tracking may be added later, but it cannot replace real observations
with synthetic points.

### 7. Bounded Revision

Revision operations are an allow-list:

- strengthen motion direction;
- split one motion clause into time segments;
- reduce trajectory amplitude;
- strengthen screen-direction wording;
- convert orbit to simpler pan/truck if the API repeatedly fails;
- preserve an already matched camera or actor constraint.

One pilot revision is allowed. The system records the original and revised
prompt rather than overwriting either run.

## Local Debugging Contract

Every component exposes `--help`, accepts explicit input/output paths, and has
a focused `unittest` module. The normal inspection sequence is:

```powershell
$PY = 'C:\Users\sy\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $PY -m videoactagent.module_io inspect --all `
  --manifest examples\module_io_manifest.json `
  --output runs\local_debug\module_io_report.json
```

Each command must report actual missing or stale evidence with a non-zero exit
code. Unit-test mocks prove mechanics only and are never used as API acceptance.

## Experiments

### Module experiments

1. Schema: save/load one circle, one actor curve, and one static point; render
   their real overlay and verify normalized-coordinate round trip.
2. Compiler: compare trajectory JSON with patched ShotScript, prompt clauses,
   and Blender overlay; reject unsupported local tracks.
3. API pilot: submit one compiled prompt to Kling and one to Seedance, save real
   task IDs and MP4 hashes, and stop before any batch run.
4. Evaluation: manually annotate the requested target on real frames and compute
   the listed metrics.
5. Revision: perform one bounded prompt revision and compare the new metrics.

### Formal comparison

Use four scenes, two backends, and three conditions:

- manually written text baseline;
- trajectory-compiled prompt;
- trajectory-compiled prompt plus one feedback revision.

This produces 24 API videos. All submissions, costs/call counts, failures,
latencies, hashes, and evaluator versions are archived. Full submission starts
only after the two-video pilot has been reviewed.

## Success Criteria

- A user can draw and reload a camera circle and one actor path locally.
- The same trajectory deterministically produces the same prompt and ShotScript.
- The Blender preview visibly displays the requested path before API use.
- Two real pilot API videos are downloaded and hash-audited.
- Requested and observed tracks are compared with stored numeric metrics.
- A missing, stale, unsupported, or low-confidence result is reported as such.
- The method shows improved direction/trajectory adherence over manual text in
  the formal comparison without reducing failures to hidden retries.

## Failure Handling

- Invalid JSON or coordinates: stop before rendering or API submission.
- Blender mismatch: preserve render logs and do not submit.
- Missing API credential: fail before creating a run directory.
- API timeout: preserve the task ID and mark outcome unknown; do not resubmit
  automatically.
- Generated video decode failure: preserve bytes/hash and mark evaluation failed.
- Tracking ambiguity: require manual annotation and label it manual evidence.
- Backend lacks requested capability: emit an explicit unsupported record.

## Licensing and Attribution

ATI's official repository is Apache-2.0. The project may study its interaction
and schema ideas and may add an optional export adapter, but copied source must
retain upstream attribution and license notices. The API-only implementation is
kept independent from ATI model weights and learned motion-injector code.
