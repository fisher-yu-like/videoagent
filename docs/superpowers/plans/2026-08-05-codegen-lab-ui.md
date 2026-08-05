# Codegen Lab UI Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use `superpowers:test-driven-development` for each behavior change and `superpowers:verification-before-completion` before claiming success.

**Goal:** Add an independent browser UI that prepares an immutable CG job, makes at most one real DeepSeek Blender-code request, then runs real local Smoke and Full Blender renders without changing the two existing director interfaces.

**Architecture:** Keep `codegen_job.py` as the evidence-bearing workflow. Add a thin `ThreadingHTTPServer` adapter and a dependency-free page. Slow generation/render operations run in background threads; the page polls sanitized job state. The new service owns port 8781, while 8769 and 8770 remain unchanged.

**Tech Stack:** Python standard library, `unittest`, vanilla HTML/CSS/JavaScript, existing DeepSeek and Blender adapters.

---

## Task 1: Support a real Smoke → Full render sequence

**Files:**
- Modify: `videoactagent/codegen_job.py:25,250-294`
- Modify: `tests/test_codegen_job.py`

### Step 1: Write failing state-transition tests

Extend the existing hash-bound test to render both profiles from one generated file:

```python
smoke = render_codegen_job(job_path, blender=Path("blender"), profile="smoke",
                           runner=runner, verifier=verifier)
self.assertEqual(smoke["status"], "smoke_succeeded")
hash_after_smoke = load_job(job_path)["code_sha256"]

full = render_codegen_job(job_path, blender=Path("blender"), profile="full",
                          runner=runner, verifier=verifier)
job = load_job(job_path)
self.assertEqual(full["status"], "succeeded")
self.assertEqual(job["code_sha256"], hash_after_smoke)
self.assertEqual(set(job["renders"]), {"smoke", "full"})
```

Add tests that Full before Smoke and repeating either profile are rejected.

### Step 2: Prove the test fails

Run:

```powershell
..\..\.venv\Scripts\python.exe -m unittest tests.test_codegen_job -v
```

Expected: Full is rejected because Smoke currently sets terminal `succeeded`.

### Step 3: Implement the minimum state correction

Use profile-specific gates:

```python
required = {"smoke": "code_validated", "full": "smoke_succeeded"}[profile]
final = "smoke_succeeded" if profile == "smoke" else "succeeded"
```

- Keep `smoke_succeeded` nonterminal.
- Reject duplicate render profiles.
- Recompute `generated_scene.py` SHA-256 before each render and compare it with `job["code_sha256"]`.
- Store Smoke and Full evidence separately under `job["renders"]`.
- Preserve a real Full failure as a failure; never fall back to Smoke and call the job complete.

### Step 4: Run the focused tests and commit

```powershell
..\..\.venv\Scripts\python.exe -m unittest tests.test_codegen_job -v
git add videoactagent/codegen_job.py tests/test_codegen_job.py
git commit -m "fix: support smoke then full codegen renders"
```

## Task 2: Build the sanitized Codegen Lab application layer

**Files:**
- Create: `videoactagent/codegen_lab.py`
- Create: `tests/test_codegen_lab.py`

### Step 1: Write failing unit tests

Cover defaults, zero-call preparation, sanitization, path confinement, and duplicate-operation rejection:

```python
def test_prepare_calls_job_preparer_only(self): ...
def test_status_omits_credentials_and_protected_file_inventory(self): ...
def test_artifact_path_must_remain_inside_job_root(self): ...
def test_duplicate_background_operation_is_rejected(self): ...
```

Use temporary files and injected functions. No test may invoke DeepSeek or Blender.

### Step 2: Prove the module is absent

```powershell
..\..\.venv\Scripts\python.exe -m unittest tests.test_codegen_lab -v
```

Expected: import failure.

### Step 3: Add configuration and strict path handling

```python
@dataclass(frozen=True)
class CodegenLabConfig:
    workspace: Path
    blender: Path
    port: int = 8781

    @property
    def experiments_root(self) -> Path:
        return self.workspace / "runs" / "work" / "codegen_blender_v1"
```

Implement `resolve_job`, `resolve_artifact`, and `status_document` with these rules:

- Jobs must be `CG<number>/job.json` below `experiments_root`.
- Artifacts must remain below the selected job and use `.mp4`, `.png`, `.json`, `.log`, `.txt`, or `.py`.
- Status includes state, error, API/retry counts, model/timing, hashes, safety, render verification, history, and artifact URLs.
- Status excludes environment variables, request credentials, `protected.files`, and complete generated code.

### Step 4: Add the operation registry

```python
@dataclass
class Operation:
    operation_id: str
    job_path: str
    kind: str
    state: str
    error: str | None = None
```

- `prepare(...)` is synchronous and calls only `prepare_codegen_job`.
- `start_generate(...)` and `start_render(...)` launch one daemon thread and one existing workflow call.
- A second mutation for the same job while running returns busy.
- There is no retry loop. Exceptions are recorded verbatim as failures.
- `job.json`, not the in-memory registry, remains the source of workflow truth.

### Step 5: Test and commit

```powershell
..\..\.venv\Scripts\python.exe -m unittest tests.test_codegen_lab -v
git add videoactagent/codegen_lab.py tests/test_codegen_lab.py
git commit -m "feat: add codegen lab application service"
```

## Task 3: Expose the local HTTP API

**Files:**
- Modify: `videoactagent/codegen_lab.py`
- Create: `tests/test_codegen_lab_http.py`

### Step 1: Write failing HTTP tests

Start `create_server(config, dependencies=...)` on port 0 and test:

```python
def test_root_and_health_are_available(self): ...
def test_prepare_returns_zero_api_calls(self): ...
def test_generate_returns_202_and_can_be_polled(self): ...
def test_render_accepts_only_smoke_or_full(self): ...
def test_artifact_supports_video_range_requests(self): ...
def test_artifact_rejects_traversal_and_foreign_jobs(self): ...
def test_errors_never_contain_api_keys(self): ...
```

Injected fake workers may create fixture files, but assertions must describe HTTP behavior only—not claim a real video passed.

### Step 2: Prove routes do not exist

```powershell
..\..\.venv\Scripts\python.exe -m unittest tests.test_codegen_lab_http -v
```

### Step 3: Implement the approved routes

- `GET /` serves `static/codegen_lab.html`.
- `GET /api/health` reports service version and configured path existence; it calls neither API nor Blender.
- `POST /api/prepare` validates JSON and creates one job synchronously.
- `POST /api/generate` returns HTTP 202 and `operation_id`.
- `POST /api/render` accepts only `smoke` or `full` and returns HTTP 202.
- `GET /api/status?job=...&operation=...` returns sanitized job/operation state.
- `GET /api/artifact?job=...&path=...` serves confined files and supports byte ranges for MP4.

Use JSON errors:

```json
{"ok": false, "error": {"type": "CodegenLabError", "message": "..."}}
```

Map invalid input to 400, missing files to 404, invalid state/busy to 409, and unexpected errors to 500. Never return stack traces or environment values.

### Step 4: Test and commit

```powershell
..\..\.venv\Scripts\python.exe -m unittest tests.test_codegen_lab tests.test_codegen_lab_http -v
git add videoactagent/codegen_lab.py tests/test_codegen_lab_http.py
git commit -m "feat: expose codegen lab HTTP API"
```

## Task 4: Add the independent browser page

**Files:**
- Create: `static/codegen_lab.html`
- Create: `tests/test_codegen_lab_panel.py`

### Step 1: Write the failing page contract test

Assert stable controls and truthful text:

```python
for marker in ('id="prepareButton"', 'id="generateButton"',
               'id="smokeButton"', 'id="fullButton"',
               'id="statusTimeline"', 'id="resultVideo"',
               'id="evidencePanel"', 'id="failurePanel"'):
    self.assertIn(marker, html)
self.assertIn("真实 DeepSeek 调用", html)
self.assertIn("不会自动重试", html)
```

Also require relative `/api/...` URLs and forbid API-key inputs.

### Step 2: Prove the page is absent

```powershell
..\..\.venv\Scripts\python.exe -m unittest tests.test_codegen_lab_panel -v
```

### Step 3: Build a compact Chinese page

- Input card: Prompt, ShotScript, trajectory, protected workspace/URL, FPS, resolution, Blender path.
- Four explicit buttons: 准备作业、真实生成、Smoke 渲染、Full 渲染.
- Timeline: `prepared → generating → code_validated → rendering → smoke_succeeded → succeeded`, plus failure states.
- Evidence: model, elapsed time, calls, retries, code hash, Blender and trajectory verification.
- Results: Smoke/Full tabs, MP4 player, first/middle/last frames, manifest, log, and generated-code links.
- Small Chinese help text explains the true cost/effect of each action.

JavaScript must poll every second while queued/running, stop at done/error, and derive success only from `job.json`. It must never auto-generate or auto-render. Form values may use `localStorage`, but credentials are never accepted or stored.

### Step 4: Test, visually inspect without an API call, and commit

```powershell
..\..\.venv\Scripts\python.exe -m unittest tests.test_codegen_lab_panel tests.test_codegen_lab_http -v
git add static/codegen_lab.html tests/test_codegen_lab_panel.py
git commit -m "feat: add codegen lab browser interface"
```

Open `http://127.0.0.1:8781` at desktop and narrow width. Do not click “真实生成” during layout verification.

## Task 5: Register the CLI and document three launch paths

**Files:**
- Modify: `videoactagent/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `docs/USAGE.md`
- Modify: `README.md`

### Step 1: Write the failing CLI assertion

```python
self.assertEqual(COMMANDS["codegen-lab"], "videoactagent.codegen_lab")
```

The new module exposes one command:

```text
videoactagent codegen-lab serve --workspace <repo> --blender <blender.exe> [--port 8781]
```

### Step 2: Prove registration is missing

```powershell
..\..\.venv\Scripts\python.exe -m unittest tests.test_cli -v
```

### Step 3: Register and document exact PowerShell commands

```powershell
# 单机位标注器
.\.venv\Scripts\python.exe -m videoactagent.cli director-loop serve --manifest runs\work\director_loop_humanoid_v1\station_reunion\director_loop_manifest.json --port 8769

# 已有三机位工作区
.\.venv\Scripts\python.exe -m videoactagent.cli director-multicam serve --manifest <multicam_manifest.json> --port 8770

# 或：从 Prompt 开始的三机位向导
.\.venv\Scripts\python.exe -m videoactagent.cli director-wizard start --blender D:\blender\blender.exe --workspace runs\work\my_story --port 8770

# 独立 Codegen Lab
.\.venv\Scripts\python.exe -m videoactagent.cli codegen-lab serve --workspace C:\Users\sy\Desktop\videoactagent --blender D:\blender\blender.exe --port 8781
```

Explain that 8769, 8770, and 8781 can coexist; the two 8770 commands are alternatives. Loading/Prepare costs zero API calls, while clicking Generate makes one real call with zero retries.

Keep README architectural and put field-level guidance in `docs/USAGE.md`.

### Step 4: Test help and commit

```powershell
..\..\.venv\Scripts\python.exe -m unittest tests.test_cli -v
..\..\.venv\Scripts\python.exe -m videoactagent.cli codegen-lab --help
git add videoactagent/cli.py tests/test_cli.py docs/USAGE.md README.md
git commit -m "docs: add independent interface launch guide"
```

## Task 6: Verify isolation and real-path readiness without another API call

**Files:**
- Create: `docs/reports/2026-08-05-codegen-lab-verification.md`
- Modify earlier files only if verification exposes a defect.

### Step 1: Run the full test suite

```powershell
..\..\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
```

Record the exact test count and elapsed time. Do not report a partial suite as complete.

### Step 2: Probe the new real service

Start 8781 with a hidden window, then run:

```powershell
Invoke-WebRequest http://127.0.0.1:8781/ -UseBasicParsing
Invoke-RestMethod http://127.0.0.1:8781/api/health
```

Expected: HTTP 200 and Blender path exists. This spends zero API calls.

### Step 3: Prove interface isolation

- Leave the user's existing 8770 service untouched and verify HTTP 200.
- Start 8769 only if the documented real manifest exists; otherwise report the missing manifest honestly.
- Confirm 8769, 8770, and 8781 return distinct page content.
- Stop only temporary processes started by this task.

### Step 4: Run real Prepare only

Use `/api/prepare` with real station inputs and the real 8770 guard. Confirm `prepared`, `api_call_count == 0`, both protection hashes, and absence of `api/` and `renders/`.

Do not call `/api/generate` during implementation verification. The user explicitly initiates paid calls from the real page.

### Step 5: Run the deterministic real-Blender fixture

Use the existing fixture that launches `D:\blender\blender.exe`. Record the real Blender version and exit status, while clearly distinguishing fixture evidence from model-generated video evidence.

### Step 6: Write evidence, audit scope, and commit

The report must include commands/results, exact tests, HTTP statuses, prepared job path/hashes, API calls made (`0` unless explicitly user-triggered), and the known earlier CG1 Blender 5.1 API incompatibility.

```powershell
git status --short
git diff --check
git add docs/reports/2026-08-05-codegen-lab-verification.md
git commit -m "test: verify codegen lab isolation and launch paths"
```

Confirm no edits to `director_loop.py`, `director_multicam.py`, `director_wizard.py`, existing run artifacts, or the user's uncommitted main-worktree files.

## Completion handoff

Deliver the 8781 URL and command, the existing 8769/8770 commands, the real prepared-job path, exact test/HTTP/Blender evidence, and a clear statement that UI implementation made no new DeepSeek call. Preserve the known CG1 failure unless a later user-triggered job produces newer real evidence.
