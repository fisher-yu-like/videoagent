# DeepSeek Blender Codegen Lab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an isolated experiment that calls `deepseek-v4-pro` once to generate constrained Blender scene code, renders it locally with real Blender, and proves that the existing director wizard and result files remain unchanged.

**Architecture:** Snapshot validated story inputs into an immutable `CG<n>` job, generate one `build_scene(context)` function, reject unsafe Python with an AST gate, and execute accepted code through a trusted Blender entrypoint that owns render settings and evidence. The experiment uses a separate worktree and `runs/work/codegen_blender_v1`; it never mutates the port-8770 workflow.

**Tech Stack:** Python 3.12, `unittest`, `urllib.request`, Python `ast`, Blender `bpy`, `D:\blender\blender.exe`, Pillow, `imageio-ffmpeg`, SHA-256 JSON evidence.

---

## Scope and files

This plan implements the backend experiment and one real station pilot. The optional port-8781 UI is a separate follow-up after the pilot passes.

Create:

- `videoactagent/codegen_contract.py`: strict input snapshots.
- `videoactagent/codegen_safety.py`: generated-code AST gate.
- `videoactagent/deepseek_blender_codegen.py`: one-call API adapter.
- `videoactagent/codegen_blender_entry.py`: trusted Blender-side entrypoint.
- `videoactagent/codegen_blender_runner.py`: host-side Blender runner.
- `videoactagent/codegen_verify.py`: video, trajectory, hash and isolation verifier.
- `videoactagent/codegen_job.py`: immutable CG job state machine and CLI.
- `examples/station_codegen_trajectory.json`: reproducible K0-K4 pilot input.
- `tests/fixtures/codegen/safe_station_scene.py`: handwritten real-Blender fixture.
- `tests/test_codegen_contract.py`
- `tests/test_codegen_safety.py`
- `tests/test_deepseek_blender_codegen.py`
- `tests/test_codegen_blender_runner.py`
- `tests/test_codegen_verify.py`
- `tests/test_codegen_job.py`
- `tests/test_codegen_blender_integration.py`
- `docs/reports/2026-08-05-deepseek-blender-codegen-pilot.md`: only after the real pilot.

Modify:

- `videoactagent/cli.py`: register independent `codegen-blender` command.
- `docs/USAGE.md`: concise usage and evidence labels.

Do not modify `README.md`, `director_wizard.py`, `director_multicam.py`, `blender_proxy.py`, or existing run directories.

## Execution preflight

Before Task 1, use `superpowers:using-git-worktrees` to create a sibling worktree and branch from commit `4dcdd03` or its descendant containing this plan. Start no copy of the director wizard from that worktree. Record the main worktree path `C:\Users\sy\Desktop\videoactagent`, confirm its existing port 8770 returns HTTP 200, and leave its dirty `README.md` and untracked `videoactagent.pdf` untouched. All implementation commits occur in the isolated worktree; only reviewed commits may later be merged.

## Task 1: Create the input contract and station trajectory

**Files:**

- Create: `examples/station_codegen_trajectory.json`
- Create: `videoactagent/codegen_contract.py`
- Test: `tests/test_codegen_contract.py`

- [ ] **Step 1: Write failing tests for exact K0-K4 and source hashes**

```python
class CodegenContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_snapshots_exact_five_keyframe_hash_bound_input(self):
        value = snapshot_codegen_inputs(
            prompt_path=Path("prompts/station_reunion.txt"),
            shotscript_path=Path("stories/station_reunion.json"),
            trajectory_path=Path("examples/station_codegen_trajectory.json"),
            destination=self.root / "source",
            fps=8,
            resolution=(640, 360),
        )
        self.assertEqual(value["schema_version"], "blender-codegen-input-1.0")
        self.assertEqual(value["render_contract"]["frame_end"], 40)
        self.assertEqual(
            [point["keyframe_id"] for point in value["trajectory"]["tracks"][0]["points"]],
            ["K0", "K1", "K2", "K3", "K4"],
        )
        self.assertTrue(all(record["sha256"] for record in value["source_bindings"].values()))

    def test_rejects_a_four_point_track(self):
        shotscript = json.loads(Path("stories/station_reunion.json").read_text(encoding="utf-8"))
        trajectory = json.loads(Path("examples/station_codegen_trajectory.json").read_text(encoding="utf-8"))
        trajectory["tracks"][0]["points"].pop()
        with self.assertRaisesRegex(CodegenContractError, "K0--K4"):
            build_codegen_input(
                prompt="station reunion",
                shotscript=shotscript,
                trajectory=trajectory,
                fps=8,
                resolution=(640, 360),
                source_bindings={},
            )
```

Add equivalent concrete test methods for wrong `scene_id`, wrong `shot_id`, wrong duration, wrong times, duplicate actor and missing actor.

- [ ] **Step 2: Run RED**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_codegen_contract -v
```

Expected: import failure for `videoactagent.codegen_contract`.

- [ ] **Step 3: Add `station_codegen_trajectory.json`**

Use schema `0.1`, the scene and whole-shot IDs from `stories/station_reunion.json`, normalized top-left coordinates, one `move/polyline` actor track per ShotScript actor, and exact times `[0.0, 0.2, 0.5, 0.8, 1.0]`. K0 and K4 must map exactly to declared actor start/end world positions.

- [ ] **Step 4: Implement the public contract API**

```python
TIMES = (0.0, 0.2, 0.5, 0.8, 1.0)

class CodegenContractError(ValueError):
    pass

def build_codegen_input(*, prompt: str, shotscript: Mapping[str, Any],
                        trajectory: Mapping[str, Any], fps: int,
                        resolution: tuple[int, int],
                        source_bindings: Mapping[str, Any]) -> dict[str, Any]:
    """Validate identities and return schema blender-codegen-input-1.0."""

def snapshot_codegen_inputs(*, prompt_path: Path, shotscript_path: Path,
                            trajectory_path: Path, destination: Path,
                            fps: int, resolution: tuple[int, int]) -> dict[str, Any]:
    """Copy immutable inputs, verify before/after hashes and write input.json."""
```

Use existing `ShotScript` and `TrajectoryInstruction` public loaders. Reject anything other than one complete shot, exact target equality and exact K0-K4 times. `input.json` includes the raw validated ShotScript, normalized trajectory with `keyframe_id`, render contract and `{path, sha256, bytes}` source bindings. Write atomically and never overwrite `destination`.

- [ ] **Step 5: Run GREEN and existing schema regressions**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_codegen_contract tests.test_station_shotscript tests.test_trajectory -v
```

- [ ] **Step 6: Commit**

```powershell
git add examples/station_codegen_trajectory.json videoactagent/codegen_contract.py tests/test_codegen_contract.py
git commit -m "feat: add Blender codegen input contract"
```

## Task 2: Reject unsafe generated Python

**Files:**

- Create: `videoactagent/codegen_safety.py`
- Test: `tests/test_codegen_safety.py`

- [ ] **Step 1: Write allowlist and bypass tests**

```python
SAFE = """import bpy
import math
def build_scene(context):
    bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
"""

BLOCKED = {
    "import os\ndef build_scene(context): pass": "import os",
    "def build_scene(context): open('x', 'w')": "open",
    "def build_scene(context): exec('x=1')": "exec",
    "import bpy\ndef build_scene(context): bpy.ops.wm.open_mainfile(filepath='x')": "bpy.ops.wm",
    "import bpy\ndef build_scene(context): bpy.ops.render.render(animation=True)": "bpy.ops.render",
    "import bpy\nbpy.ops.mesh.primitive_cube_add()\ndef build_scene(context): pass": "top-level",
}
```

Assert `SAFE` returns `accepted`; every concrete blocked case raises `CodegenSafetyError`. Add duplicate `build_scene`, missing argument, dynamic import, alias import and code larger than 40 KiB.

- [ ] **Step 2: Run RED**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_codegen_safety -v
```

- [ ] **Step 3: Implement the AST gate**

```python
ALLOWED_IMPORTS = {"bpy", "math", "mathutils"}
BLOCKED_NAMES = {"open", "eval", "exec", "compile", "__import__"}
BLOCKED_BPY_PREFIXES = {
    "bpy.ops.wm", "bpy.ops.script", "bpy.ops.render",
    "bpy.data.libraries.load", "bpy.context.preferences",
}

class CodegenSafetyError(ValueError):
    pass

def validate_generated_code(code: str) -> dict[str, Any]:
    """Require one build_scene(context), safe imports and no top-level calls."""
```

Parse with `ast.parse`. Top-level nodes may only be imports, constant assignments and function definitions. Walk every import and call, reconstruct dotted attributes, and reject the blocked names/prefixes. Return schema, status, entrypoint, imports and code SHA-256.

- [ ] **Step 4: Run GREEN**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_codegen_safety -v
```

- [ ] **Step 5: Commit**

```powershell
git add videoactagent/codegen_safety.py tests/test_codegen_safety.py
git commit -m "feat: validate generated Blender Python safely"
```

## Task 3: Add the one-call DeepSeek-v4-pro adapter

**Files:**

- Create: `videoactagent/deepseek_blender_codegen.py`
- Test: `tests/test_deepseek_blender_codegen.py`

- [ ] **Step 1: Write fake-transport tests**

```python
def test_calls_v4_pro_once_without_retry_and_saves_code(self):
    evidence = request_blender_code(
        codegen_input=VALID_INPUT,
        output_dir=self.output,
        environ={
            "DEEPSEEK_API_KEY": "secret",
            "DEEPSEEK_BASE_URL": "https://example.test/v1?credential=hidden",
        },
        transport=self.transport_for(VALID_RESPONSE),
    )
    self.assertEqual(self.request_payload()["model"], "deepseek-v4-pro")
    self.assertEqual(self.call_count, 1)
    self.assertEqual(evidence["api_call_count"], 1)
    self.assertEqual(evidence["retry_count"], 0)
    self.assertTrue((self.output / "generated_scene.py").is_file())
    self.assertNotIn("secret", self.all_saved_text())
    self.assertNotIn("credential=hidden", self.all_saved_text())
```

Add explicit tests for missing environment (zero calls), `finish_reason=length`, non-JSON API body, non-JSON content, extra envelope fields, empty code and >40 KiB code. Failures preserve redacted evidence and never retry.

- [ ] **Step 2: Run RED**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_deepseek_blender_codegen -v
```

- [ ] **Step 3: Implement exact adapter contract**

```python
MODEL = "deepseek-v4-pro"
RESPONSE_FIELDS = {"schema_version", "summary", "python_code"}

class DeepSeekCodegenError(ValueError):
    pass

def request_blender_code(*, codegen_input: Mapping[str, Any],
                         output_dir: Path | str,
                         environ: Mapping[str, str] | None = None,
                         transport: Callable[..., Any] = urlopen) -> dict[str, Any]:
    """Make one non-streaming request and persist redacted raw evidence."""
```

Follow the evidence behavior in `deepseek_planner.py` without modifying it. Use JSON response format, disabled thinking, temperature 0.1, max tokens 8192, timeout 120 seconds, zero retries and exact response schema `blender-codegen-response-1.0`. Save `request.json`, redacted `response.json` or `response.raw`, `generated_scene.py` and `evidence.json`.

- [ ] **Step 4: Run GREEN plus existing DeepSeek tests**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_deepseek_blender_codegen tests.test_deepseek_planner -v
```

- [ ] **Step 5: Commit**

```powershell
git add videoactagent/deepseek_blender_codegen.py tests/test_deepseek_blender_codegen.py
git commit -m "feat: generate Blender code with DeepSeek v4 pro"
```

## Task 4: Add the trusted Blender-side entrypoint

**Files:**

- Create: `videoactagent/codegen_blender_entry.py`
- Create: `tests/fixtures/codegen/safe_station_scene.py`
- Test: `tests/test_codegen_blender_runner.py`

- [ ] **Step 1: Write pure mapping tests before Blender code**

```python
def test_frame_and_world_mapping(self):
    self.assertEqual(frame_for_time(1, 40, 0.0), 1)
    self.assertEqual(frame_for_time(1, 40, 1.0), 40)
    self.assertEqual(world_xy([-0.5, 0.5, -1.0, 1.0], 0.25, 0.25), (-0.25, 0.5))
```

Run `tests.test_codegen_blender_runner` and verify missing-module RED.

- [ ] **Step 2: Implement pure helpers and Blender `main()`**

```python
def frame_for_time(start: int, end: int, time: float) -> int:
    return start + int(round(time * (end - start)))

def world_xy(bounds: list[float], x: float, y: float) -> tuple[float, float]:
    min_x, max_x, min_y, max_y = bounds
    return min_x + x * (max_x - min_x), max_y - y * (max_y - min_y)

def main(argv: list[str] | None = None) -> int:
    """Load validated code, force render settings, render and write manifest."""
```

Keep `import bpy` inside Blender-only functions so ordinary unit tests can import the module. `main()` verifies passed input/code hashes, clears the default scene, loads code with trusted `runpy`, calls `build_scene(context)`, requires every actor object and `DirectorCamera`, forces frame range/FPS/resolution/H.264, saves `scene.blend`, renders `video.mp4`, renders first/middle/last PNGs, records K0/K2/K4 transforms and writes `codegen_manifest.json`. Print `BLENDER_CODEGEN_OK=<json>` only after all files exist.

- [ ] **Step 3: Add handwritten fixture code**

The fixture defines `build_scene(context)`, creates simple actors named by input IDs, inserts K0-K4 location keyframes, adds ground/light/`DirectorCamera`, and performs no file access or rendering. Validate it with `validate_generated_code`; label it `handwritten_fixture` in tests.

- [ ] **Step 4: Run GREEN**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_codegen_blender_runner -v
```

- [ ] **Step 5: Commit**

```powershell
git add videoactagent/codegen_blender_entry.py tests/fixtures/codegen/safe_station_scene.py tests/test_codegen_blender_runner.py
git commit -m "feat: add trusted Blender codegen entrypoint"
```

## Task 5: Add the host-side Blender runner

**Files:**

- Create: `videoactagent/codegen_blender_runner.py`
- Modify: `tests/test_codegen_blender_runner.py`

- [ ] **Step 1: Add failing process-boundary tests**

Mock `subprocess.run` only at this unit boundary. Assert the command starts with the requested Blender executable and contains `--background --factory-startup --python`. Add concrete failure tests for timeout, nonzero exit, missing marker, empty MP4, wrong manifest input/code hash and output path outside the job.

- [ ] **Step 2: Implement the runner**

```python
class CodegenBlenderRunnerError(ValueError):
    pass

def run_codegen_blender(*, blender: Path, job_root: Path, input_path: Path,
                        code_path: Path, output_dir: Path,
                        timeout: int = 330) -> dict[str, object]:
    """Run one Blender process and return verified artifact records."""
```

Resolve paths strictly, require inputs beneath `job_root`, require a new output child, compute hashes, and pass them to the trusted entrypoint. Run with `cwd=job_root`, captured UTF-8 output and reduced environment that excludes DeepSeek credentials. Persist `render.log` before evaluating return code. Require marker, manifest/hash agreement and nonempty MP4/BLEND/PNG files.

- [ ] **Step 3: Run GREEN**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_codegen_blender_runner -v
```

These tests prove command/evidence behavior only, not real Blender success.

- [ ] **Step 4: Commit**

```powershell
git add videoactagent/codegen_blender_runner.py tests/test_codegen_blender_runner.py
git commit -m "feat: run generated scenes in isolated Blender"
```

## Task 6: Verify decoded video, trajectories and hashes

**Files:**

- Create: `videoactagent/codegen_verify.py`
- Test: `tests/test_codegen_verify.py`

- [ ] **Step 1: Write real decode fixture tests**

Create tiny MP4s inside temporary directories with installed `imageio-ffmpeg`. Assert exact FPS, resolution, duration, frame count, differing first/middle/last pixel hashes, safe artifact inventory and K0/K2/K4 error <= 0.15. Add concrete failures for truncated/static video, missing actor, NaN, unsafe path, hash mismatch and error > 0.15.

- [ ] **Step 2: Implement verifier API**

```python
class CodegenVerifyError(ValueError):
    pass

def verify_codegen_render(*, job_root: Path, input_path: Path,
                          render_dir: Path,
                          position_tolerance: float = 0.15) -> dict[str, object]:
    """Decode MP4 and compare render evidence with immutable input."""
```

Use the robust decode approach from `coded_draft.py` without importing orchestration. Reject non-finite metadata, duration beyond half a frame, wrong FPS/resolution/count, identical sampled pixel hashes, unsafe paths and record mismatches. Convert normalized points with `world_xy` and compare manifest K0/K2/K4 transforms for each actor.

- [ ] **Step 3: Run RED then GREEN**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_codegen_verify -v
```

First run before implementation must fail on missing module; final run must pass.

- [ ] **Step 4: Commit**

```powershell
git add videoactagent/codegen_verify.py tests/test_codegen_verify.py
git commit -m "feat: verify Blender codegen video evidence"
```

## Task 7: Add immutable jobs and isolation guard

**Files:**

- Create: `videoactagent/codegen_job.py`
- Test: `tests/test_codegen_job.py`
- Modify: `videoactagent/cli.py`

- [ ] **Step 1: Write state-machine tests**

Test `CG1` selection, input snapshot, protected tree digest and `prepared` state. Inject adapter/runner/verifier functions to test `code_validated`, `api_failed`, `code_rejected`, `blender_failed`, `output_invalid`, `isolation_failed` and `succeeded`. Reject duplicate active execution, illegal transition, stale source hash and protected-file mutation.

- [ ] **Step 2: Implement exact job API**

```python
TERMINAL = {
    "api_failed", "code_rejected", "blender_failed",
    "output_invalid", "isolation_failed", "succeeded",
}

def prepare_codegen_job(*, experiments_root: Path, protected_workspace: Path,
                        protected_url: str, prompt_path: Path,
                        shotscript_path: Path, trajectory_path: Path,
                        fps: int = 8,
                        resolution: tuple[int, int] = (640, 360)) -> Path:
    """Create a new prepared CG job without API or Blender."""

def generate_codegen_job(job_path: Path, *, environ=None,
                         transport=urlopen) -> dict[str, object]:
    """Make the job's only API call and validate raw code."""

def render_codegen_job(job_path: Path, *, blender: Path,
                       profile: str) -> dict[str, object]:
    """Render smoke or full profile from identical validated code."""
```

Use atomic writes and a per-job lock. Hash the protected workspace recursively in sorted relative-path order and GET the protected URL without writing. Profiles are exactly `smoke` and `full`; full derives 24 FPS/960x540 but reuses source/code hashes. Preserve all failures.

- [ ] **Step 3: Add minimal CLI mapping**

Add to `videoactagent/cli.py`:

```python
"codegen-blender": "videoactagent.codegen_job",
```

Expose `prepare`, `generate`, `render-smoke`, `render-full`, `status`. Each requires explicit paths and never reuses an output directory.

- [ ] **Step 4: Run RED then GREEN**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_codegen_job -v
```

- [ ] **Step 5: Commit**

```powershell
git add videoactagent/codegen_job.py videoactagent/cli.py tests/test_codegen_job.py
git commit -m "feat: orchestrate isolated Blender codegen jobs"
```

## Task 8: Prove the runner with real local Blender

**Files:**

- Create: `tests/test_codegen_blender_integration.py`

- [ ] **Step 1: Write the real integration test**

The test skips only if `D:\blender\blender.exe` is absent. It creates a temporary one-second 160x90/3 FPS station job, validates the handwritten fixture, runs real Blender, requires exit 0 and marker, decodes the MP4, verifies nonempty BLEND/PNG files and checks measured actor K0/K4 positions. The module docstring explicitly says it does not call DeepSeek and does not prove model output quality.

- [ ] **Step 2: Run real Blender once**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_codegen_blender_integration -v
```

Expected: PASS with a real decoded video. On failure, preserve the captured log and fix the trusted runner/fixture without calling DeepSeek.

- [ ] **Step 3: Run all Codegen tests**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_codegen_contract tests.test_codegen_safety tests.test_deepseek_blender_codegen tests.test_codegen_blender_runner tests.test_codegen_verify tests.test_codegen_job tests.test_codegen_blender_integration -v
```

- [ ] **Step 4: Commit**

```powershell
git add tests/test_codegen_blender_integration.py
git commit -m "test: exercise Blender codegen runner with real video"
```

## Task 9: Document and regress the existing system

**Files:**

- Modify: `docs/USAGE.md`

- [ ] **Step 1: Add concise usage and evidence labels**

Document five subcommands, artifact tree, model name and one-call/no-retry policy. State that API unit tests use fake transport, the integration fixture uses handwritten code with real Blender, and only a real CG job plus manual video review counts as DeepSeek evidence. Do not add this backend to the current wizard.

- [ ] **Step 2: Run the full relevant suite**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_deepseek_planner tests.test_blender_render_profiles tests.test_coded_draft tests.test_director_multicam tests.test_director_multicam_panel tests.test_director_wizard tests.test_director_wizard_http tests.test_codegen_contract tests.test_codegen_safety tests.test_deepseek_blender_codegen tests.test_codegen_blender_runner tests.test_codegen_verify tests.test_codegen_job tests.test_codegen_blender_integration -v
```

Expected: all pass; no external API call occurs.

- [ ] **Step 3: Verify 8770 and current status**

```powershell
$page = Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8770/
$session = Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8770/api/session
[pscustomobject]@{Page=$page.StatusCode;Session=$session.StatusCode}
git status --short
```

Expected: both HTTP statuses 200. The main worktree retains only the user's pre-existing `README.md` and `videoactagent.pdf` changes.

- [ ] **Step 4: Commit**

```powershell
git add docs/USAGE.md
git commit -m "docs: explain isolated Blender codegen experiment"
```

## Task 10: Execute exactly one real station pilot

**Files:**

- Create after execution: `docs/reports/2026-08-05-deepseek-blender-codegen-pilot.md`

- [ ] **Step 1: Prepare CG1 with zero calls**

```powershell
.venv\Scripts\python.exe -m videoactagent.cli codegen-blender prepare `
  --experiments-root runs/work/codegen_blender_v1 `
  --protected-workspace C:\Users\sy\Desktop\videoactagent\runs\work\my_story `
  --protected-url http://127.0.0.1:8770/api/session `
  --prompt C:\Users\sy\Desktop\videoactagent\prompts\station_reunion.txt `
  --shotscript C:\Users\sy\Desktop\videoactagent\stories\station_reunion.json `
  --trajectory examples\station_codegen_trajectory.json `
  --fps 8 --resolution 640x360
```

Expected: `CG1/job.json` is `prepared`, API count 0, no render directory, guard HTTP 200.

- [ ] **Step 2: Inspect source hashes, IDs, duration and credential absence**

If any identity differs, stop and record invalid preparation. Do not edit CG1.

- [ ] **Step 3: Make the only DeepSeek call**

```powershell
.venv\Scripts\python.exe -m videoactagent.cli codegen-blender generate `
  --job runs/work/codegen_blender_v1/CG1/job.json
```

Expected: terminal API/code failure, or `code_validated` with `deepseek-v4-pro`, call count 1 and retry count 0. Never generate CG1 twice.

- [ ] **Step 4: Render smoke only when code is validated**

```powershell
.venv\Scripts\python.exe -m videoactagent.cli codegen-blender render-smoke `
  --job runs/work/codegen_blender_v1/CG1/job.json `
  --blender D:\blender\blender.exe
```

Expected: real 640x360, 8 FPS, five-second MP4 and K0/K2/K4 errors <= 0.15. Stop on failure; do not patch raw code.

- [ ] **Step 5: Reuse identical code for full render**

```powershell
.venv\Scripts\python.exe -m videoactagent.cli codegen-blender render-full `
  --job runs/work/codegen_blender_v1/CG1/job.json `
  --blender D:\blender\blender.exe
```

Expected: real 960x540, 24 FPS, five-second MP4 with the same code SHA; API count remains 1.

- [ ] **Step 6: Perform manual review**

Watch the complete MP4 and record story semantics, actor recognizability, continuity, environment richness, lighting, intersections, disappearances, teleportation and composition in `review/human_review.json`. Outcome may be `pass`, `fail` or `uncertain`; include reviewer and timestamp.

- [ ] **Step 7: Recheck isolation**

Run the guard verifier and GET 8770. Any protected digest change forces `isolation_failed` regardless of video quality.

- [ ] **Step 8: Write and commit the evidence report**

The report contains job/model/call count, timings, Blender version, frames/FPS/resolution/duration, hashes, trajectory errors, human outcome, failures and before/after protected digest. It must distinguish mock, handwritten fixture and real DeepSeek evidence.

```powershell
git add docs/reports/2026-08-05-deepseek-blender-codegen-pilot.md
git commit -m "docs: report DeepSeek Blender codegen pilot"
```

## Completion gate

Complete only when all tests pass, CG1 contains exactly one real request and no retry, raw code is unchanged, real MP4/BLEND artifacts are hash-bound and manually reviewed, measured K0/K2/K4 checks pass, port 8770 is healthy, protected hashes are unchanged, and no Seedance/Kling/VACE/GPU-server call occurred.

If CG1 fails, that evidence is the valid result. A second API attempt requires a new CG2 decision based on CG1; it is never automatic.
