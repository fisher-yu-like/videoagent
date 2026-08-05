# Prompt-first Blender 视频生成 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task with TDD checkpoints.

**Goal:** Add a prompt-only browser flow that uses DeepSeek-v4-pro to create a validated ShotScript, uses DeepSeek-v4-pro to create safe Blender Python, renders a real local MP4, and exposes only status plus the final video.

**Architecture:** Keep the existing `/api/prepare`, `/api/generate`, `/api/render` debugging API and old director pages unchanged. Add a focused `PromptCodegenRunner` that owns `PF<n>` evidence jobs and retries, then expose it through `/api/prompt-run` and `/api/prompt-status`. The existing Codegen Lab page becomes a thin Prompt/video client; all intermediate artifacts remain server-side.

**Tech Stack:** Python standard library, existing `ScenePlanDraft`, `request_scene_plan`, `request_blender_code`, AST safety gate, Blender runner/verifier, `ThreadingHTTPServer`, vanilla HTML/JavaScript, `unittest`.

---

## Task 1: Allow the scene planner to run v4-pro with repair feedback

**Files:**
- Modify: `videoactagent/deepseek_planner.py:348-510`
- Modify: `tests/test_deepseek_planner.py`

### Step 1: Write the failing tests

Add tests that inject the existing transport and inspect the saved request:

```python
def test_scene_plan_accepts_model_override_and_repair_feedback(self):
    output = self.root / "plan"
    request_scene_plan(
        story_prompt="a station meeting", duration_seconds=5.0,
        output_dir=output, model="deepseek-v4-pro",
        feedback="Previous response used an invalid environment_preset.",
        environ=self.environment, transport=self.transport,
    )
    request = json.loads((output / "request.json").read_text())
    self.assertEqual(request["payload"]["model"], "deepseek-v4-pro")
    user_content = request["payload"]["messages"][1]["content"]
    self.assertIn("invalid environment_preset", user_content)

def test_scene_plan_default_model_remains_flash(self):
    output = self.root / "default-plan"
    request_scene_plan(
        story_prompt="a station meeting", duration_seconds=5.0,
        output_dir=output, environ=self.environment, transport=self.transport,
    )
    request = json.loads((output / "request.json").read_text())
    self.assertEqual(request["payload"]["model"], "deepseek-v4-flash")
```

The second test protects the 8770 wizard contract; its default must remain `deepseek-v4-flash`.

### Step 2: Run the focused tests and confirm the new test fails

```powershell
..\..\.venv\Scripts\python.exe -m unittest tests.test_deepseek_planner -v
```

Expected: `request_scene_plan()` rejects the unknown `model` argument.

### Step 3: Implement the compatible extension

Change the signature to:

```python
def request_scene_plan(*, story_prompt: str, duration_seconds: float,
                       output_dir: Path | str, feedback: str | None = None,
                       previous_draft: Mapping[str, Any] | None = None,
                       model: str = MODEL, environ: Mapping[str, str] | None = None,
                       transport: Callable[..., Any] = urlopen) -> dict[str, Any]:
```

Use `model` for request payload and evidence. Preserve the existing default. Add `feedback` to the user payload when non-empty, capped at 2,000 characters, and reject an invalid model string before any network call. Do not add retries in this adapter; retry ownership belongs to the Prompt pipeline.

### Step 4: Run planner tests and commit

```powershell
..\..\.venv\Scripts\python.exe -m unittest tests.test_deepseek_planner -v
git add videoactagent/deepseek_planner.py tests/test_deepseek_planner.py
git commit -m "feat: allow prompt pipeline planner model overrides"
```

## Task 2: Implement the two-agent PromptCodegenRunner

**Files:**
- Create: `videoactagent/prompt_codegen_pipeline.py`
- Create: `tests/test_prompt_codegen_pipeline.py`

### Step 1: Write failing pipeline tests

Define injection-friendly boundaries and test real control behavior without network or Blender:

```python
def test_prompt_pipeline_creates_shotscript_then_code_and_returns_video(self):
    result = self.runner.run("a station meeting")
    self.assertEqual(result["status"], "succeeded")
    self.assertTrue(Path(result["video"]).is_file())

def test_planner_schema_failure_retries_with_error_feedback(self):
    result = self.runner.run("repair the scene")
    self.assertEqual(result["plan_attempts"], 2)
    self.assertIn("scene plan schema", self.planner_feedback[1])

def test_blender_failure_retries_codegen_with_render_log_feedback(self):
    result = self.runner.run("repair the render")
    self.assertEqual(result["code_attempts"], 2)
    self.assertIn("render.log", self.codegen_feedback[1])

def test_retry_budget_is_three_attempts_per_stage(self):
    with self.assertRaises(PromptPipelineError):
        self.runner.run("always invalid")
    self.assertEqual(self.planner_calls, 3)

def test_failure_never_returns_video_artifact(self):
    with self.assertRaises(PromptPipelineError):
        self.runner.run("always invalid")
    self.assertFalse(list(self.root.glob("PF*/renders/smoke/video.mp4")))
```

The fake planner must write a valid `draft.json` using `ScenePlanDraft.to_dict()`. The fake code generator must write a safe `generated_scene.py`; the fake renderer writes an explicitly named fixture file only for testing operation transitions. Assert API call counters, attempt directories, feedback strings, and terminal state. Do not assert that a fake MP4 is visually valid.

### Step 2: Run the tests and confirm the module is absent

```powershell
..\..\.venv\Scripts\python.exe -m unittest tests.test_prompt_codegen_pipeline -v
```

Expected: import failure for `videoactagent.prompt_codegen_pipeline`.

### Step 3: Implement the pipeline contracts

Add:

```python
@dataclass(frozen=True)
class PromptPipelineConfig:
    experiments_root: Path
    blender: Path
    protected_workspace: Path
    protected_url: str
    duration_seconds: float = 5.0
    fps: int = 8
    resolution: tuple[int, int] = (640, 360)
    max_attempts: int = 3

class PromptPipelineError(ValueError):
    pass

class PromptCodegenRunner:
    def run(self, prompt: str) -> dict[str, Any]:
        raise NotImplementedError
```

Implementation rules:

- Allocate the next `PF<number>` directory atomically below `experiments_root`; write `prompt.txt` and an initial `job.json` before calling an Agent.
- Call `request_scene_plan(story_prompt=prompt, duration_seconds=config.duration_seconds, output_dir=plan_attempt, model="deepseek-v4-pro", feedback=planner_feedback)` into `plan/attempt-N`. On success, load and validate `draft.json`, write `shotscript.json`, and write an actor-only `trajectory.json` wrapper from `draft.to_trajectory()`.
- Copy those source files into `source/` and call `snapshot_codegen_inputs()` to create the same hash-bound `source/input.json` contract used by the existing codegen adapter.
- For code attempt N, call `request_blender_code()` with the input document and write to `codegen/attempt-N`. Validate the resulting code with `validate_generated_code()` before accepting it as `api/generated_scene.py`.
- Run `run_codegen_blender()` with the configured Blender path into `renders/attempt-N`, then call `verify_codegen_render()` against that directory. On success, copy verified artifacts to `renders/smoke` and create a final artifact index.
- If planner validation fails, use the exact validation message as the next planner `feedback`. If code safety, Blender process, or video verification fails, include the bounded error plus the last 4,000 characters of `render.log` in the next codegen input under `repair_feedback`.
- Each loop has at most `max_attempts == 3`; no seed changes, no unbounded loops, and no silent fallback. `api_call_count` is the sum of actual planner and codegen requests; `retry_count` is the number after the first attempt.
- Record `job.json` transitions `planning`, `shot_validated`, `code_generating`, `code_validated`, `rendering`, `succeeded` or `failed`, with every attempt directory and error. Only `succeeded` exposes the MP4 URL.
- Capture the configured protected workspace and guard response hashes before execution and re-check them before accepting the final render. If either guard is unavailable or changed, fail the job with no video.

### Step 4: Run pipeline tests and commit

```powershell
..\..\.venv\Scripts\python.exe -m unittest tests.test_prompt_codegen_pipeline -v
git add videoactagent/prompt_codegen_pipeline.py tests/test_prompt_codegen_pipeline.py
git commit -m "feat: add two-agent prompt-to-blender pipeline"
```

## Task 3: Add Prompt-only application and HTTP endpoints

**Files:**
- Modify: `videoactagent/codegen_lab.py`
- Create: `tests/test_prompt_codegen_http.py`

### Step 1: Write failing HTTP tests

Extend the server fixture with an injected `PromptCodegenRunner` and add:

```python
def test_prompt_run_accepts_only_prompt_and_returns_202(self):
    response = post_json("/api/prompt-run", {"prompt": "a station meeting"})
    self.assertEqual(response.status, 202)
    self.assertTrue(response.json["operation_id"])

def test_prompt_status_returns_only_final_video_after_success(self):
    operation = post_json("/api/prompt-run", {"prompt": "a station meeting"})
    status = poll_until_done(operation["operation_id"])
    self.assertEqual(status["status"], "succeeded")
    self.assertTrue(status["video_url"])
    self.assertNotIn("shotscript", json.dumps(status))
    self.assertNotIn("generated_scene.py", json.dumps(status))

def test_prompt_failure_does_not_return_video_url(self):
    operation = post_json("/api/prompt-run", {"prompt": "invalid test"})
    status = poll_until_done(operation["operation_id"])
    self.assertEqual(status["status"], "failed")
    self.assertNotIn("video_url", status)

def test_prompt_run_rejects_empty_prompt(self):
    with self.assertRaises(HTTPError) as error:
        post_json("/api/prompt-run", {"prompt": "   "})
    self.assertEqual(error.exception.code, 400)
```

The fake runner reports operation results; the test never calls a model or Blender process. Also assert that extra fields such as `model`, `blender`, `retries`, and `resolution` are ignored or rejected rather than overriding server configuration.

### Step 2: Run the new HTTP tests and confirm they fail

```powershell
..\..\.venv\Scripts\python.exe -m unittest tests.test_prompt_codegen_http -v
```

Expected: 404 because `/api/prompt-run` and `/api/prompt-status` do not exist.

### Step 3: Add the prompt operation registry and routes

Add `start_prompt_run(prompt)` to `CodegenLabApplication`, using the same lock-protected background operation registry as existing Generate/Render actions. The worker invokes exactly one `PromptCodegenRunner.run(prompt)` call; retries remain inside the runner and are visible in job evidence.

Add:

- `POST /api/prompt-run` with `{ "prompt": "a station meeting" }`, returning `202 {"ok":true,"operation_id":"generated-id"}`.
- `GET /api/prompt-status?operation=<id>`, returning stage, counts, error, and on success only `video_url`, duration, resolution, and artifact hashes.

Use 400 for empty/oversized prompt, 404 for unknown operation, 409 for duplicate active operation, and 500 for unexpected server errors. Never put full ShotScript, generated Python, environment values, or API keys in the response.

### Step 4: Run HTTP/application tests and commit

```powershell
..\..\.venv\Scripts\python.exe -m unittest tests.test_codegen_lab tests.test_codegen_lab_http tests.test_prompt_codegen_http -v
git add videoactagent/codegen_lab.py tests/test_prompt_codegen_http.py
git commit -m "feat: expose prompt-only generation endpoints"
```

## Task 4: Replace the new page with the Prompt/video-only client

**Files:**
- Modify: `static/codegen_lab.html`
- Modify: `tests/test_codegen_lab_panel.py`

### Step 1: Write failing page-contract tests

Replace the old multi-input assertions with:

```python
def test_page_exposes_only_prompt_action_status_and_video(self):
    html = PAGE.read_text(encoding="utf-8")
    self.assertIn('id="promptInput"', html)
    self.assertIn('id="runButton"', html)
    self.assertIn('id="statusText"', html)
    self.assertIn('id="resultVideo"', html)
    self.assertIn("/api/prompt-run", html)
    self.assertIn("/api/prompt-status", html)
    for forbidden in ("shotscript", "trajectory", "resolution", "blender", "api-key"):
        self.assertNotIn(f'id="{forbidden}', html.lower())
```

### Step 2: Run the page test and confirm it fails

```powershell
..\..\.venv\Scripts\python.exe -m unittest tests.test_codegen_lab_panel -v
```

Expected: failure because the current page exposes file/path fields and old endpoints.

### Step 3: Implement the minimal client

Keep the existing visual style but reduce the DOM to a Prompt textarea, one button, one status line, one failure line, and one video element. JavaScript must:

```javascript
const result = await request("/api/prompt-run", {
  method: "POST",
  body: JSON.stringify({prompt: promptInput.value.trim()})
});
poll(`/api/prompt-status?operation=${encodeURIComponent(result.operation_id)}`);
```

Poll once per second, stop on `succeeded` or `failed`, set `resultVideo.src` only when `video_url` is present, and never expose intermediate artifacts. Disable the button while active and allow a new run after settlement. Do not persist prompt or credentials in localStorage.

### Step 4: Run page/HTTP tests and commit

```powershell
..\..\.venv\Scripts\python.exe -m unittest tests.test_codegen_lab_panel tests.test_prompt_codegen_http -v
git add static/codegen_lab.html tests/test_codegen_lab_panel.py
git commit -m "feat: simplify codegen lab to prompt and video"
```

## Task 5: Wire server defaults, CLI help, docs, and Prompt examples

**Files:**
- Modify: `videoactagent/codegen_lab.py`
- Modify: `docs/USAGE.md`
- Modify: `README.md`
- Modify: `tests/test_cli.py`

### Step 1: Write failing default/config tests

Add assertions that the Codegen Lab configuration derives fixed defaults: 5 seconds, 8 FPS, 640×360, model `deepseek-v4-pro`, and retry budget of 3 attempts. Assert that the CLI help says the page accepts only a Prompt and returns a Blender video.

### Step 2: Implement deterministic server defaults

Keep CLI arguments to `--workspace`, `--blender`, and `--port`. Derive the protected workspace from `<project-root>/runs/work/my_story` when it exists, otherwise report a clear startup/first-run error directing the user to start the 8770 guard. Do not add these paths to the page form.

### Step 3: Document the simplified command and six example Prompts

Document:

```powershell
.\.venv\Scripts\python.exe -m videoactagent.cli codegen-lab serve --workspace C:\Users\sy\Desktop\videoactagent --blender D:\blender\blender.exe --port 8781
```

Add these six directly runnable examples:

```text
车站月台上，两个人在 5 秒内相遇。穿橙色衣服的人从左侧缓慢走向穿蓝色衣服的人，蓝色人物原地等待。镜头保持中景，完整展示月台柱子和站台空间，动作简单清晰。
城市十字路口，一名行人从画面左侧走向右侧，另一名行人在路边等待。红绿灯和斑马线清晰可见，镜头使用宽景并缓慢跟随行人，保持人物和环境都在画面中。
森林小路上，一名徒步者沿小路从远处走近，另一名人物站在路旁等待。镜头从宽景缓慢向前移动，保持小路、树木和两个人物的空间关系清晰。
摄影棚内，一名演员从画面左侧走到中央，另一名演员在中央等待并转身面对他。背景有三盏简单的摄影灯，镜头使用稳定的中景，动作不要复杂。
咖啡馆交接场景，一名人物拿着小物体从左侧走到桌边，另一名人物在桌旁等待并接过物体。镜头保持中景，桌子、两个人物和咖啡馆环境都要可见。
仓库通道中，一名人物从前景向后方移动，另一名人物从侧面跟随。使用宽景展示货架和通道，摄像机沿通道缓慢移动，人物动作保持简单连续。
```

Explain that one click may produce two or more real API calls because each Agent can use up to two repair retries, and that only a verified real Blender MP4 is shown.

### Step 4: Run tests and commit

```powershell
..\..\.venv\Scripts\python.exe -m unittest tests.test_cli tests.test_codegen_lab_panel -v
git add videoactagent/codegen_lab.py tests/test_cli.py README.md docs/USAGE.md
git commit -m "docs: document prompt-first codegen flow"
```

## Task 6: Verify the real user path

**Files:**
- Create: `docs/reports/2026-08-05-prompt-first-verification.md`

### Step 1: Run focused automated tests

```powershell
..\..\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_prompt_codegen*.py" -v
..\..\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_codegen*.py" -q
..\..\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_cli.py" -q
```

Record exact counts and failures. These tests must not call DeepSeek or Blender.

### Step 2: Start the real 8781 service and test the page

Start the server with the documented command, open `http://127.0.0.1:8781`, paste one approved Prompt, and click once. Record the operation ID and every status transition.

### Step 3: Verify the real two-agent/Blender result

Confirm the final job contains planner and codegen request/response/evidence, model names, true API counts, attempt count, `generated_scene.py` hash, Blender version, MP4 hash, duration, frame count, and trajectory verification. If a repair happens, report the actual error and repaired attempt. If the run fails, keep the failure job and do not claim a video.

### Step 4: Inspect the actual MP4

Use the existing local video/frame inspection tools to confirm the MP4 is decodable and visually inspect first/middle/last frames. Record observations separately from automated metrics; do not call a successful render based only on HTTP status.

### Step 5: Write and commit the report

```powershell
git diff --check
git add docs/reports/2026-08-05-prompt-first-verification.md
git commit -m "test: verify prompt-first real video path"
```

The final handoff must include the local URL, job path, real API call counts, retry count, Blender version, MP4 path/hash, test counts, and any unresolved model-quality limitation.
