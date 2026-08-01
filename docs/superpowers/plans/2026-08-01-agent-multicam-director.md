# Agent Multicam Director Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the existing single-camera human director operational while adding a separate DeepSeek-planned, human-approved, three-camera Blender proxy workflow with source-bound evidence and six real-scene validation cases.

**Architecture:** Build from `feature/seedance-human-restyle` so the latest immutable director-loop evidence and camera-wrap fix remain available. Add focused modules for plan validation, deterministic camera-rig compilation, DeepSeek transport, multicamera workspace state, and one-process Blender rendering; expose them through a separate `director-multicam` command and page. The legacy `director-loop` command and workspace schema remain unchanged.

**Tech Stack:** Python 3.10+, standard-library HTTP/JSON, existing dataclasses and `unittest`, Blender Python API, HTML/CSS/vanilla JavaScript, imageio-ffmpeg, Pillow.

---

## Execution base and file map

At execution time, first use `superpowers:using-git-worktrees` and create `.worktrees/agent-multicam-director` from `feature/seedance-human-restyle`. Cherry-pick the approved design and this plan from the documentation branch with:

```powershell
git cherry-pick d354edd..stage0-api-baseline
```

New focused modules:

- `videoactagent/multicam_plan.py`: strict `MulticamPlan` schema and coverage validation.
- `videoactagent/multicam_rig.py`: deterministic conversion from roles to K0–K4 camera states.
- `videoactagent/multicam_prompt.py`: scale-aware camera fact compilation.
- `videoactagent/deepseek_planner.py`: one-call DeepSeek JSON adapter and redacted evidence.
- `videoactagent/director_multicam.py`: independent workspace, HTTP API, versioning and CLI.
- `videoactagent/multicam_blender_runner.py`: native runner that launches one Blender process.
- `videoactagent/multicam_blender_proxy.py`: shared-scene, three-camera RGB/depth/mask rendering.
- `static/director_multicam_panel.html`: independent multicamera review UI.

Existing files changed only at narrow extension points:

- `videoactagent/cli.py`: register `director-multicam` without changing `director-loop`.
- `videoactagent/blender_proxy.py`: expose reusable scene helpers while preserving existing CLI behavior.
- `videoactagent/__init__.py`: no public behavior change; version only if required by existing convention.
- `README.md`, `docs/USAGE.md`: concise UTF-8 instructions and verified references.

Experiment assets:

- `prompts/city_crosswalk.txt`, `prompts/forest_path.txt`, `prompts/studio_room.txt`
- `prompts/cafe_handoff.txt`, `prompts/warehouse_chase.txt`
- `stories/cafe_handoff.json`, `stories/warehouse_chase.json`
- `configs/multicam_suite.json`

## Task 1: Isolate the branch and freeze legacy behavior

**Files:**
- Create: `tests/test_director_multicam_compat.py`
- Modify: `videoactagent/cli.py`
- Create: `videoactagent/director_multicam.py`

- [ ] **Step 1: Create the isolated worktree and confirm the base**

Run the worktree commands described above, then:

```powershell
git log -3 --oneline
git status --short
```

Expected: the branch contains `b64b274` and the two documentation commits; status is clean.

- [ ] **Step 2: Write the failing compatibility test**

```python
import io
from contextlib import redirect_stdout
from pathlib import Path
import unittest

from videoactagent.cli import COMMANDS, main


class DirectorMulticamCompatibilityTests(unittest.TestCase):
    def test_legacy_and_multicam_are_separate_commands(self):
        self.assertEqual(COMMANDS["director-loop"], "videoactagent.director_loop")
        self.assertEqual(COMMANDS["director-multicam"], "videoactagent.director_multicam")

    def test_both_help_pages_are_available(self):
        for command in ("director-loop", "director-multicam"):
            output = io.StringIO()
            with redirect_stdout(output):
                code = main([command, "--help"])
            self.assertEqual(code, 0)
            self.assertIn("usage:", output.getvalue().lower())

    def test_legacy_panel_is_not_replaced(self):
        html = Path("static/director_panel.html").read_text(encoding="utf-8")
        self.assertIn("人工导演循环", html)
        self.assertNotIn("Agent 多视角导演", html)
```

- [ ] **Step 3: Run the test and verify the missing new command fails**

Run: `python -m unittest tests.test_director_multicam_compat -v`

Expected: FAIL because `director-multicam` is not registered.

- [ ] **Step 4: Add the separate dispatcher and minimal parser**

Add to `COMMANDS`:

```python
"director-multicam": "videoactagent.director_multicam",
```

Create a minimal module whose parser establishes only the approved two everyday commands:

```python
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--prompt", type=Path, required=True)
    prepare.add_argument("--shotscript", type=Path, required=True)
    prepare.add_argument("--blender", type=Path, required=True)
    prepare.add_argument("--output-dir", type=Path, required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--manifest", type=Path, required=True)
    serve.add_argument("--port", type=int, default=8770)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    print(f"DIRECTOR_MULTICAM_{args.command.upper()}_NOT_READY")
    return 2
```

- [ ] **Step 5: Run compatibility suites and commit**

Run:

```powershell
python -m unittest tests.test_director_multicam_compat tests.test_cli tests.test_director_loop -v
```

Expected: all tests pass except no test may interpret the temporary `NOT_READY` status as a completed workflow.

Commit:

```powershell
git add videoactagent/cli.py videoactagent/director_multicam.py tests/test_director_multicam_compat.py
git commit -m "feat: add isolated multicam director entrypoint"
```

## Task 2: Define and validate the Agent planning contract

**Files:**
- Create: `videoactagent/multicam_plan.py`
- Create: `tests/test_multicam_plan.py`

- [ ] **Step 1: Write schema tests before implementation**

Use this canonical valid fixture in `tests/test_multicam_plan.py`:

```python
VALID_PLAN = {
    "schema_version": "1.0",
    "scene_id": "station_reunion",
    "director_intent": "Keep both people spatially legible throughout the reunion.",
    "actor_staging": ["actor_a approaches actor_b", "actor_b remains waiting"],
    "locked_through_keyframe": None,
    "cameras": [
        {
            "camera_id": "camera_a", "role": "master", "target": "all_actors",
            "side": "south", "shot_size": "wide", "motion": "static",
            "look_at_policy": "actors_midpoint", "responsibility_segments": [[0.0, 0.4]],
            "constraints": ["keep both actors visible"], "rationale": "establish space",
        },
        {
            "camera_id": "camera_b", "role": "follow", "target": "actor_a",
            "side": "south_west", "shot_size": "medium", "motion": "follow",
            "look_at_policy": "target_actor", "responsibility_segments": [[0.4, 0.75]],
            "constraints": ["preserve headroom"], "rationale": "cover approach",
        },
        {
            "camera_id": "camera_c", "role": "reverse", "target": "actor_b",
            "side": "north_east", "shot_size": "medium", "motion": "arc",
            "look_at_policy": "target_actor", "responsibility_segments": [[0.75, 1.0]],
            "constraints": ["do not cross actor path"], "rationale": "cover response",
        },
    ],
}
```

Tests must assert that `load_multicam_plan()` accepts this plan, rejects unknown fields, rejects camera IDs other than exactly `camera_a/b/c`, rejects gaps or overlaps outside `[0,1]`, rejects an unknown actor target, and rejects a responsibility segment that touches a locked prefix during suffix replanning.

- [ ] **Step 2: Run the tests and verify import failure**

Run: `python -m unittest tests.test_multicam_plan -v`

Expected: FAIL with `ModuleNotFoundError: videoactagent.multicam_plan`.

- [ ] **Step 3: Implement strict immutable types**

Implement these public types and entrypoint:

```python
@dataclass(frozen=True)
class ResponsibilitySegment:
    start: float
    end: float


@dataclass(frozen=True)
class CameraAssignment:
    camera_id: str
    role: str
    target: str
    side: str
    shot_size: str
    motion: str
    look_at_policy: str
    responsibility_segments: tuple[ResponsibilitySegment, ...]
    constraints: tuple[str, ...]
    rationale: str


@dataclass(frozen=True)
class MulticamPlan:
    scene_id: str
    director_intent: str
    actor_staging: tuple[str, ...]
    locked_through_keyframe: str | None
    cameras: tuple[CameraAssignment, ...]


```

Implement the exact public signature `load_multicam_plan(value: Mapping[str, Any], *, scene_id: str, actors: Sequence[str], locked_through_keyframe: str | None = None) -> MulticamPlan`. It must enforce exact field sets, finite numeric endpoints, strictly positive segment length, collective full coverage with no gap larger than `1e-9`, safe text, allowed enum values, and locked-prefix equality.

- [ ] **Step 4: Run schema tests**

Run: `python -m unittest tests.test_multicam_plan -v`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add videoactagent/multicam_plan.py tests/test_multicam_plan.py
git commit -m "feat: validate multicam agent plans"
```

## Task 3: Compile deterministic camera rigs and scale-aware prompts

**Files:**
- Create: `videoactagent/multicam_rig.py`
- Create: `videoactagent/multicam_prompt.py`
- Create: `tests/test_multicam_rig.py`
- Create: `tests/test_multicam_prompt.py`

- [ ] **Step 1: Write failing camera-rig tests**

Tests must use real station world bounds and five actor keyframes, then assert:

```python
states = compile_camera_rig(
    plan=load_multicam_plan(VALID_PLAN, scene_id="station_reunion", actors=["actor_a", "actor_b"]),
    world_bounds=(-5.0, 5.0, -4.0, 4.0),
    actor_keyframes=ACTOR_KEYFRAMES,
)
self.assertEqual(set(states), {"camera_a", "camera_b", "camera_c"})
self.assertTrue(all(len(camera_states) == 5 for camera_states in states.values()))
self.assertTrue(all(camera_states[0].t == 0.0 and camera_states[-1].t == 1.0 for camera_states in states.values()))
```

Add a prompt test where look-at drift is `0.009 * D`, camera drift is `0.004 * D`, roll drift is `0.4°`, and focal drift is `0.9 mm`; assert the compiled text contains `camera holds position and keeps a fixed look-at` and contains no change facts. Add a second test just above each threshold and assert changes are reported.

- [ ] **Step 2: Verify tests fail**

Run: `python -m unittest tests.test_multicam_rig tests.test_multicam_prompt -v`

Expected: FAIL because both modules are missing.

- [ ] **Step 3: Implement deterministic role templates**

Expose:

```python
def compile_camera_rig(
    *, plan: MulticamPlan, world_bounds: tuple[float, float, float, float],
    actor_keyframes: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[CameraKeyframe, ...]]:
    """Map approved semantic roles to complete K0--K4 numeric camera states."""
```

Use the scene diagonal for distance and height. `master` stays outside the actor envelope and looks at all-actor midpoint; `follow` offsets behind and to the selected side of its target; `reverse` remains on the declared opposite side and tracks its target. Role templates must clamp positions to a padded legal camera region, never alter actor positions, and always emit linear interpolation with zero roll initially.

- [ ] **Step 4: Implement scale-aware fact compilation**

Expose:

```python
LOOK_AT_POSITION_RATIO = 0.01
CAMERA_POSITION_RATIO = 0.005
LOOK_ANGLE_DEGREES = 1.0
ROLL_DEGREES = 0.5
FOCAL_MM = 1.0


```

Implement the exact public signature `compile_multicam_prompt(*, world_bounds: tuple[float, float, float, float], camera_states: Mapping[str, Sequence[CameraKeyframe]]) -> str`. Use both look-at displacement and view-direction angle before emitting a look-at change. Keep this compiler separate from legacy `trajectory-facts-v2` so old approved evidence remains valid.

- [ ] **Step 5: Run, commit**

Run: `python -m unittest tests.test_multicam_rig tests.test_multicam_prompt -v`

Expected: all tests pass.

```powershell
git add videoactagent/multicam_rig.py videoactagent/multicam_prompt.py tests/test_multicam_rig.py tests/test_multicam_prompt.py
git commit -m "feat: compile deterministic multicam trajectories"
```

## Task 4: Add one-call DeepSeek planning with redacted evidence

**Files:**
- Create: `videoactagent/deepseek_planner.py`
- Create: `tests/test_deepseek_planner.py`

- [ ] **Step 1: Write transport and redaction tests**

Inject a fake transport rather than making a network call:

```python
class FakeResponse:
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self):
        content = json.dumps(VALID_PLAN)
        return json.dumps({
            "id": "call-1", "model": "deepseek-v4-pro",
            "choices": [{"finish_reason": "stop", "message": {"content": content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 200},
        }).encode()

evidence = request_multicam_plan(
    scene_context=SCENE_CONTEXT,
    output_dir=Path(root),
    environ={"DEEPSEEK_API_KEY": "secret", "DEEPSEEK_BASE_URL": "https://example.test/v1"},
    transport=lambda request, timeout: FakeResponse(),
)
self.assertNotIn("secret", json.dumps(evidence))
self.assertEqual(evidence["api_call_count"], 1)
self.assertEqual(evidence["retry_count"], 0)
```

Also test missing environment variables, HTTP failure, empty content, `finish_reason != stop`, malformed JSON and schema-invalid JSON. Each must write a failure evidence record and raise `DeepSeekPlannerError`; none may invoke transport twice.

- [ ] **Step 2: Verify failure**

Run: `python -m unittest tests.test_deepseek_planner -v`

Expected: FAIL because the adapter is missing.

- [ ] **Step 3: Implement the official OpenAI-compatible request**

Build exactly one `POST` request to `DEEPSEEK_BASE_URL.rstrip("/") + "/chat/completions"` with authorization in memory only:

```python
payload = {
    "model": environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro"),
    "messages": [
        {"role": "system", "content": SYSTEM_JSON_PROMPT},
        {"role": "user", "content": json.dumps(scene_context, ensure_ascii=False)},
    ],
    "response_format": {"type": "json_object"},
    "temperature": 0.2,
    "max_tokens": 4096,
    "stream": False,
}
```

Validate the response with `load_multicam_plan()`. Persist canonical `request.json` without headers, `response.json`, `plan.json`, and `evidence.json` atomically. Store only the origin and path of the base URL, never query credentials or authorization.

- [ ] **Step 4: Run adapter tests and a no-secret scan**

Run:

```powershell
python -m unittest tests.test_deepseek_planner -v
rg -n "Bearer |DEEPSEEK_API_KEY.*:" tests videoactagent
```

Expected: tests pass; scan finds no serialized secret.

- [ ] **Step 5: Commit**

```powershell
git add videoactagent/deepseek_planner.py tests/test_deepseek_planner.py
git commit -m "feat: add audited DeepSeek camera planner"
```

## Task 5: Build the independent multicamera workspace and approval gates

**Files:**
- Modify: `videoactagent/director_multicam.py`
- Create: `tests/test_director_multicam.py`

- [ ] **Step 1: Write workspace state-machine tests**

Tests must cover these public functions:

```python
manifest = prepare_workspace(PROMPT, SHOTSCRIPT, BLENDER, output)
plan_record = create_plan(manifest, planner=fake_planner)
approve_plan(manifest, plan_record["plan_id"], author_id="human-reviewer")
job = prepare_render(manifest, plan_record["plan_id"])
session = session_document(manifest)
```

Assert that preparing never calls DeepSeek, an unapproved plan cannot render, an approved plan cannot be overwritten, suffix replanning preserves the locked prefix byte-for-byte, and the multicam output directory does not modify a separately prepared legacy workspace.

- [ ] **Step 2: Verify state-machine tests fail**

Run: `python -m unittest tests.test_director_multicam -v`

Expected: FAIL because the workspace functions do not exist.

- [ ] **Step 3: Implement immutable workspace records**

Use schema `multicam-director-1.0` and this state shape:

```python
{
    "schema_version": "multicam-director-1.0",
    "current_plan": None,
    "approved_plan": None,
    "current_iteration": None,
    "approved_iteration": None,
    "next_plan": 1,
    "next_iteration": 1,
}
```

`prepare_workspace()` must snapshot the prompt and one-take ShotScript inside staging, validate actors/world bounds/timeline with the existing `ShotScript` type, render one initial reference Proxy through the unchanged `blender_runner`, and atomically publish the multicam root. It must not create or modify a legacy director workspace. `create_plan()` writes directories matching `plans/P[1-9][0-9]*/`; `approve_plan()` writes an author/time/hash-bound approval; `prepare_render()` writes paths matching `iterations/M[1-9][0-9]*/job.json` only after verifying every source record again.

- [ ] **Step 4: Implement HTTP endpoints without starting background API calls**

Provide:

```text
GET  /api/session
POST /api/plans
POST /api/plans/{plan_id}/approve
POST /api/plans/{plan_id}/render
GET  /api/jobs/{job_id}
POST /api/iterations/{iteration_id}/approve
```

Only `POST /api/plans` calls DeepSeek. Render work runs in a local background thread. All errors return JSON and preserve `failed`/`unknown` evidence rather than changing approval state.

- [ ] **Step 5: Run, commit**

Run: `python -m unittest tests.test_director_multicam tests.test_director_multicam_compat tests.test_director_loop -v`

Expected: all pass.

```powershell
git add videoactagent/director_multicam.py tests/test_director_multicam.py
git commit -m "feat: add multicam director workspace gates"
```

## Task 6: Render three synchronized cameras and structure passes in one Blender process

**Files:**
- Create: `videoactagent/multicam_blender_runner.py`
- Create: `videoactagent/multicam_blender_proxy.py`
- Modify: `videoactagent/blender_proxy.py`
- Create: `tests/test_multicam_blender_integration.py`

- [ ] **Step 1: Write a real Blender integration test**

Build a one-second, 3 FPS station job in a temporary directory and execute:

```python
completed = subprocess.run([
    sys.executable, "-m", "videoactagent.multicam_blender_runner",
    "--blender", str(BLENDER), "--shotscript", str(shotscript),
    "--trajectory", str(actor_trajectory), "--camera-bundle", str(camera_bundle),
    "--output-dir", str(output), "--render-style", "diagnostic",
    "--fps", "3", "--resolution", "160x90", "--timeout", "240",
], capture_output=True, text=True, timeout=260)
self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
```

Decode all three MP4 files and assert identical frame count/FPS/duration. Assert the manifest records one shared `.blend`, three distinct camera trajectories and video hashes, three depth frames per camera, one mask sequence per actor/camera, and identical actor world positions at each sampled frame.

- [ ] **Step 2: Verify the real integration test fails**

Run: `python -m unittest tests.test_multicam_blender_integration -v`

Expected: FAIL because the multicam runner is missing. This test invokes real local Blender and no external API.

- [ ] **Step 3: Expose reusable Blender scene operations without changing legacy main**

Keep `blender_proxy.main()` and all old arguments unchanged. Expose focused helpers for the new module:

```python
def duplicate_render_camera(source, camera_id: str):
    camera_data = source.data.copy()
    camera = source.copy()
    camera.data = camera_data
    camera.name = camera_id
    bpy.context.collection.objects.link(camera)
    return camera
```

The new renderer calls existing `configure_scene()`, `apply_trajectory()` and `apply_camera_trajectory()` once per camera object, but calls `apply_trajectory()` only once for the shared actors.

- [ ] **Step 4: Implement RGB, depth and actor masks**

For each camera, set `scene.camera`, set its RGB MP4 path, and configure compositor File Output nodes from the same render layer:

```python
view_layer.use_pass_z = True
for index, actor_id in enumerate(actor_ids, start=1):
    for obj in actor_mesh_objects(actor_id):
        obj.pass_index = index
```

Write lossless OpenEXR depth frames and one 8-bit PNG ID mask sequence per actor. Validate exact expected frame counts before printing `MULTICAM_PROXY_OK`. The manifest binds the shared blend, input hashes, camera states, media metadata, structure-pass file inventories and per-frame actor/camera world transforms.

- [ ] **Step 5: Run legacy and multicam Blender tests, commit**

Run:

```powershell
python -m unittest tests.test_multicam_blender_integration tests.test_blender_proxy_integration tests.test_blender_render_profiles -v
```

Expected: all tests pass with real Blender outputs.

```powershell
git add videoactagent/blender_proxy.py videoactagent/multicam_blender_proxy.py videoactagent/multicam_blender_runner.py tests/test_multicam_blender_integration.py
git commit -m "feat: render synchronized multicam control bundles"
```

## Task 7: Add geometry checks and publish immutable multicamera iterations

**Files:**
- Create: `videoactagent/multicam_eval.py`
- Modify: `videoactagent/director_multicam.py`
- Create: `tests/test_multicam_eval.py`

- [ ] **Step 1: Write failing fact-based evaluation tests**

Tests must assert that `evaluate_multicam_iteration()` reports:

```python
{
    "sync": {"frame_count_equal": True, "fps_equal": True, "duration_equal": True},
    "coverage": {"camera_a": 1.0, "camera_b": 1.0, "camera_c": 1.0},
    "responsibility_target_visibility": {"minimum": 0.95},
    "orientation": {"maximum_adjacent_view_angle_degrees": 4.2, "passed": True},
    "human_composition_status": "unknown",
}
```

Use measured frame transforms and camera projection, not copied expected values. Add failures for one missing frame, target visibility below 95%, adjacent view direction over 30°, camera/geometry collision and a mismatched actor world coordinate across camera records.

- [ ] **Step 2: Run and verify failure**

Run: `python -m unittest tests.test_multicam_eval -v`

Expected: FAIL because the evaluator is missing.

- [ ] **Step 3: Implement evaluation and publication gates**

Expose:

Implement the exact public signature `evaluate_multicam_iteration(*, plan: MulticamPlan, render_manifest: Mapping[str, Any]) -> dict[str, Any]`. Projection must use stored camera intrinsics/extrinsics and actor anchors. Automatic evaluation may mark numeric checks pass/fail, but must always leave `human_composition_status` as `unknown` until a human approval record exists.

- [ ] **Step 4: Bind report and output bytes transactionally**

`run_render_job()` must stage outputs, probe the exact staged MP4 bytes, run evaluation, then atomically publish sequential iterations `M1`, `M2`, and so on. A failed check keeps the job failed and cannot update `current_iteration`. Approval records bind plan hash, render manifest hash, evaluation hash, all three MP4 hashes and `author_id`.

- [ ] **Step 5: Run, commit**

Run: `python -m unittest tests.test_multicam_eval tests.test_director_multicam -v`

Expected: all pass.

```powershell
git add videoactagent/multicam_eval.py videoactagent/director_multicam.py tests/test_multicam_eval.py tests/test_director_multicam.py
git commit -m "feat: gate multicam proxy evidence"
```

## Task 8: Build the independent Agent multicamera page

**Files:**
- Create: `static/director_multicam_panel.html`
- Modify: `videoactagent/director_multicam.py`
- Create: `tests/test_director_multicam_panel.py`

- [ ] **Step 1: Write static UI contract tests**

Require these independent elements and actions:

```python
html = Path("static/director_multicam_panel.html").read_text(encoding="utf-8")
for element_id in (
    "proxy-camera-a", "proxy-camera-b", "proxy-camera-c", "sync-play",
    "coverage-timeline", "camera-tabs", "trajectory-plane", "agent-plan",
    "approve-plan", "render-proxy", "approve-proxy", "status",
):
    self.assertIn(f'id="{element_id}"', html)
self.assertIn("/api/plans", html)
self.assertIn("/approve", html)
self.assertIn("三台摄像机同步播放", html)
self.assertNotEqual(
    html, Path("static/director_panel.html").read_text(encoding="utf-8")
)
```

- [ ] **Step 2: Run and verify missing page failure**

Run: `python -m unittest tests.test_director_multicam_panel -v`

Expected: FAIL because the page is missing.

- [ ] **Step 3: Implement synchronized playback and camera editing**

The page must load `/api/session`, display the full story, Agent rationale and coverage lanes, and keep three video elements within 80 ms of the lead video's time. Camera tabs edit independent K0–K4 camera states while actor points remain shared. `selectEditStart("K2")` freezes K0–K1 and edits K2–K4. The request converts that UI choice to the internal `locked_through_keyframe="K1"` contract.

Use explicit fetch functions:

```javascript
async function createAgentPlan(){return postJSON('/api/plans',{locked_through_keyframe:boundary});}
async function approvePlan(id){return postJSON('/api/plans/'+id+'/approve',{author_id:author()});}
async function renderPlan(id){return postJSON('/api/plans/'+id+'/render',buildEditedPlan());}
async function approveProxy(id){return postJSON('/api/iterations/'+id+'/approve',{author_id:author()});}
```

No page action may automatically retry DeepSeek or call Seedance/Kling/VACE.

- [ ] **Step 4: Serve only the new page from the new command**

`director_multicam.serve_workspace()` must read `static/director_multicam_panel.html`; `director_loop.serve_workspace()` must continue reading `static/director_panel.html`. Validate ports in `1..65535` and keep default ports 8770 and 8769 distinct.

- [ ] **Step 5: Run, commit**

Run:

```powershell
python -m unittest tests.test_director_multicam_panel tests.test_director_multicam_compat tests.test_director_loop -v
```

Expected: all pass.

```powershell
git add static/director_multicam_panel.html videoactagent/director_multicam.py tests/test_director_multicam_panel.py
git commit -m "feat: add human-reviewed multicam director panel"
```

## Task 9: Add the six-scene experiment suite

**Files:**
- Create: `prompts/city_crosswalk.txt`
- Create: `prompts/forest_path.txt`
- Create: `prompts/studio_room.txt`
- Create: `prompts/cafe_handoff.txt`
- Create: `prompts/warehouse_chase.txt`
- Create: `stories/cafe_handoff.json`
- Create: `stories/warehouse_chase.json`
- Create: `configs/multicam_suite.json`
- Modify: `videoactagent/blender_proxy.py`
- Create: `tests/test_multicam_suite.py`

- [ ] **Step 1: Write suite validation tests**

The config must contain exactly these unique cases:

```python
EXPECTED = {
    "station_reunion", "city_crosswalk", "forest_path",
    "studio_room", "cafe_handoff", "warehouse_chase",
}
```

Map station to `stories/station_reunion.json` and the next three scenes to `examples/city_crosswalk_shotscript.json`, `examples/forest_path_shotscript.json`, and `examples/studio_room_shotscript.json`. Add a distinct prompt file for each. Create the last two as one-take, five-second, 3 FPS ShotScripts. Assert every environment preset renders, every story has K0–K4-compatible actors, and no prompt, ShotScript or motion signature hash is duplicated.

- [ ] **Step 2: Run and verify failure**

Run: `python -m unittest tests.test_multicam_suite -v`

Expected: FAIL because the new suite and two environments do not exist.

- [ ] **Step 3: Add minimal cafe and warehouse Blender environments**

Use the existing primitive/material helpers. Cafe must include floor, counter, table and chairs with a clear central handoff area. Warehouse must include floor, shelving rows and a clear chase corridor. Keep actor paths and legal camera region free of geometry; do not introduce downloaded assets.

- [ ] **Step 4: Add suite preparation to the existing new command**

Add `prepare-suite --config configs/multicam_suite.json --output-dir runs/work/director_loop_multicam_suite_v1` to `director-multicam`. It prepares six isolated workspaces and writes a suite manifest; it performs zero DeepSeek calls. Planning remains an explicit UI action per scene, preserving the six-call budget.

- [ ] **Step 5: Validate source suite and commit**

Run: `python -m unittest tests.test_multicam_suite tests.test_station_shotscript tests.test_whole_story -v`

Expected: all pass without calling external APIs.

```powershell
git add prompts/city_crosswalk.txt prompts/forest_path.txt prompts/studio_room.txt prompts/cafe_handoff.txt prompts/warehouse_chase.txt stories/cafe_handoff.json stories/warehouse_chase.json configs/multicam_suite.json videoactagent/blender_proxy.py videoactagent/director_multicam.py tests/test_multicam_suite.py
git commit -m "feat: add six-scene multicam suite"
```

## Task 10: Rewrite concise documentation and run real checkpoints

**Files:**
- Modify: `README.md`
- Modify: `docs/USAGE.md`
- Create: `docs/MULTICAM_EXPERIMENT.md`
- Modify: relevant tests only if documentation commands expose a real defect

- [ ] **Step 1: Rewrite README as valid UTF-8 Chinese**

Keep one compact flow and only two routine command examples:

```powershell
python -m videoactagent.cli director-loop serve --manifest runs/work/director_loop_v1/station_reunion/director_loop_manifest.json
python -m videoactagent.cli director-multicam serve --manifest runs/work/director_loop_multicam_v1/station_reunion/multicam_manifest.json
```

Document current capability, explicit limitations, separate output roots, environment-variable names, experiment result entry, and a reference table with columns `论文 / 借鉴模块 / 官方代码 / 本项目状态`. Include the eleven primary references from the approved design. Do not claim a paper is reproduced unless its code and a local result are both present.

- [ ] **Step 2: Verify documentation and full non-API suite**

Run:

```powershell
python -m unittest discover -s tests -v
git diff --check
```

Expected: all unit and configured real-Blender integration tests pass; no paid video API is called. If a test is skipped, record its exact reason rather than calling the suite fully passed.

- [ ] **Step 3: Start both real local pages**

Prepare separate station workspaces, then start legacy on port 8769 and multicam on 8770 with hidden background processes. Request `/api/session` from both and save the responses. Stop the processes after the probe. Expected: both return HTTP 200 and different schema versions/pages.

- [ ] **Step 4: Run the real DeepSeek/Blender experiment only when environment is visible**

Before any request, check only whether `DEEPSEEK_API_KEY` and `DEEPSEEK_BASE_URL` exist; never print values. If absent, write `environment_blocked` to `docs/MULTICAM_EXPERIMENT.md` and stop without substituting fake responses. If present, invoke exactly one initial planning action for each of the six scenes, zero automatic retries. Save API evidence and wait for real human plan approval before rendering each scene's three-camera Proxy.

- [ ] **Step 5: Record truthful experiment status and commit docs**

For each scene, record one of `planned_waiting_human`, `rendered_waiting_human`, `human_approved`, `failed`, or `unknown`; link the actual run directory and hashes. Do not summarize `planned_waiting_human` as a completed end-to-end experiment.

```powershell
git add README.md docs/USAGE.md docs/MULTICAM_EXPERIMENT.md
git commit -m "docs: explain multicam director and evidence"
```

## Final verification gate

Before claiming completion, invoke `superpowers:verification-before-completion` and run the exact targeted suites again from a clean status. Report separately:

- legacy compatibility;
- multicamera schema and planner adapter;
- real local Blender integration;
- DeepSeek call count and state;
- number of scenes actually rendered and number actually human-approved;
- any test skips, unknown states or blocked environment variables.

The implementation is complete only when the software and documentation are finished. The six-scene experiment may remain explicitly `waiting_human` if user review has not occurred; it must not be called experimentally complete in that state.
