# Stage 0 Secure API Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove inherited credentials, preserve the real Kling and Seedance CLI workflows, then validate submission, polling, response parsing, and MP4 download only with user-approved real API tasks.

**Architecture:** Keep the inherited working scripts intact until real responses are captured. First add credential safety and an offline dry-run that only validates locally constructed payloads. After approved real calls, refactor shared gateway behavior against redacted fixtures derived from those real responses; never invent a gateway response or report a fake transport as API validation.

**Tech Stack:** Python 3.10+, standard library, JD Cloud AI Gateway, real Seedance/Kling task responses and MP4 outputs.

---

## Evidence policy

1. Static scans, syntax checks, and dry-runs are reported only as local code checks.
2. They do not prove the gateway, model, polling, response schema, or download works.
3. No `FakeTransport`, mocked HTTP success, invented task ID, invented response JSON, or synthetic MP4 may be used as acceptance evidence.
4. Gateway behavior passes only when an approved real call returns a real task ID, reaches a terminal status, and produces a real MP4 or a recorded real failure.
5. Regression fixtures must be derived from real responses, carry provenance metadata, and redact credentials and expiring URLs.

## Mandatory gates

- Gate A — approved: local Git, security edits, dry-run, source scans, and syntax checks. No network.
- Gate B — not approved: real Seedance/Kling task submission. Requires exact model, prompt, duration, resolution/mode, call count, retry count, data disclosure, and cost ceiling.
- Gate C — not approved: any subsequent query/download request outside Gate B's stated request budget.
- Stage 1 remains blocked until the Stage 0 report is reviewed and approved.

## Task 1: Initialize repository and package scaffold

Status: completed on branch `stage0-api-baseline`, commit `70c545b`.

Created `.gitignore`, `pyproject.toml`, package directories, design document, and this plan. No network was used.

## Task 2: Remove the real embedded credential using a real source scan

**Files:**
- Create: `tests/test_source_safety.py`
- Modify: `kling_demo.py`
- Modify: `seedance_demo.py`

- [ ] **Step 1: Write a source-safety test that scans the real repository**

```python
# tests/test_source_safety.py
from pathlib import Path
import re
import unittest


class SourceSafetyTests(unittest.TestCase):
    def test_python_sources_contain_no_embedded_pk_key(self):
        pattern = re.compile(r"pk-[A-Za-z0-9-]{20,}")
        offenders = []
        for path in Path(".").rglob("*.py"):
            if pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(str(path))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test and observe the real failure**

Run: `python -m unittest tests.test_source_safety -v`

Expected: FAIL listing the two inherited Python files. The report must not reproduce the credential value.

- [ ] **Step 3: Remove the default credential without changing API behavior**

In both scripts replace the module-level credential and `_headers` behavior with:

```python
API_KEY = os.environ.get("JD_KLING_KEY", "").strip()


def _headers():
    if not API_KEY:
        raise RuntimeError("JD_KLING_KEY is required for API execution")
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
        "Trace-id": uuid.uuid4().hex,
    }
```

- [ ] **Step 4: Run the real source scan again**

Run: `python -m unittest tests.test_source_safety -v`

Expected: PASS for repository credential absence only.

Run: `rg -n "pk-[A-Za-z0-9-]{20,}" . -g "*.py"`

Expected: no output. This is not API validation.

- [ ] **Step 5: Parse the actual edited files**

Run: `python -c "import ast,pathlib; [ast.parse(pathlib.Path(f).read_text(encoding='utf-8'), filename=f) for f in ['kling_demo.py','seedance_demo.py']]; print('syntax: ok')"`

Expected: `syntax: ok`.

- [ ] **Step 6: Commit the security fix**

Run: `git add kling_demo.py seedance_demo.py tests/test_source_safety.py docs/superpowers/plans/2026-07-29-stage-0-api-baseline.md`

Run: `git commit -m "security: remove embedded gateway credential"`

## Task 3: Add offline dry-run while preserving every inherited mode

**Files:**
- Create: `tests/test_dry_run.py`
- Modify: `kling_demo.py`
- Modify: `seedance_demo.py`

- [ ] **Step 1: Write subprocess tests against the actual CLIs**

```python
# tests/test_dry_run.py
import json
import subprocess
import sys
import unittest


class DryRunTests(unittest.TestCase):
    def run_cli(self, script, *args):
        completed = subprocess.run(
            [sys.executable, script, "--dry-run", *args],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return json.loads(completed.stdout)

    def test_seedance_t2v_dry_run_prints_real_payload_builder_output(self):
        payload = self.run_cli("seedance_demo.py", "t2v")
        self.assertEqual(payload["model"], "Doubao-Seedance-2.0")
        self.assertEqual(payload["parameters"]["duration"], 5)

    def test_kling_t2v_dry_run_prints_real_payload_builder_output(self):
        payload = self.run_cli("kling_demo.py", "t2v")
        self.assertEqual(payload["model"], "Kling-V2-5-Turbo")
        self.assertEqual(payload["parameters"]["duration"], 5)


if __name__ == "__main__":
    unittest.main()
```

These tests execute the actual scripts and actual payload functions. They verify only local CLI behavior, not external API correctness.

- [ ] **Step 2: Run and observe the real CLI failure**

Run: `python -m unittest tests.test_dry_run -v`

Expected: FAIL because `--dry-run` is currently treated as an unknown mode.

- [ ] **Step 3: Add a dry-run flag to each actual script**

At the start of each `main()`:

```python
args = sys.argv[1:]
dry_run = False
if args and args[0] == "--dry-run":
    dry_run = True
    args = args[1:]
mode = args[0] if args else "t2v"
```

Immediately after the existing mode-to-payload branch and before `submit(payload)`:

```python
if dry_run:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return
```

Do not change any model identifier, prompt, mode, role, duration, or response parser during this task.

- [ ] **Step 4: Run the real CLIs locally**

Run: `python -m unittest tests.test_dry_run -v`

Expected: 2 tests PASS as local CLI checks.

Run: `python seedance_demo.py --dry-run ff https://example.invalid/first.png https://example.invalid/last.png`

Expected: JSON containing real code paths for `first_frame` and `last_frame`; no network call because submission is bypassed.

Run: `python kling_demo.py --dry-run v3_t2v`

Expected: JSON containing `Kling-V3-omni`; no network call.

- [ ] **Step 5: Commit dry-run support**

Run: `git add kling_demo.py seedance_demo.py tests/test_dry_run.py`

Run: `git commit -m "feat: add offline payload dry-run to inherited CLIs"`

## Task 4: Save real experiment artifacts without changing gateway semantics

**Files:**
- Create: `tests/test_run_directory.py`
- Create: `videoactagent/run_record.py`
- Modify: `kling_demo.py`
- Modify: `seedance_demo.py`

- [ ] **Step 1: Test a real temporary filesystem operation**

```python
# tests/test_run_directory.py
import json
from pathlib import Path
import tempfile
import unittest

from videoactagent.run_record import RunDirectory


class RunDirectoryTests(unittest.TestCase):
    def test_writes_actual_json_file_and_redacts_authorization(self):
        with tempfile.TemporaryDirectory() as root:
            run = RunDirectory.create(Path(root), "seedance")
            path = run.write_json(
                "request.json",
                {"Authorization": "Bearer local-test-value", "prompt": "hello"},
            )
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(path.is_file())
            self.assertEqual(saved["Authorization"], "[REDACTED]")
            self.assertEqual(saved["prompt"], "hello")


if __name__ == "__main__":
    unittest.main()
```

This validates a real filesystem write and redaction function. It does not validate any external response.

- [ ] **Step 2: Observe failure**

Run: `python -m unittest tests.test_run_directory -v`

Expected: FAIL because `RunDirectory` does not exist.

- [ ] **Step 3: Implement the minimal real artifact writer**

```python
# videoactagent/run_record.py
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid


SENSITIVE_KEYS = {"authorization", "api_key", "access_key", "secret_key"}


def redact(value):
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in SENSITIVE_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


@dataclass(frozen=True)
class RunDirectory:
    path: Path

    @classmethod
    def create(cls, root: Path, backend: str):
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = root / f"{timestamp}_{backend}_{uuid.uuid4().hex[:8]}"
        path.mkdir(parents=True, exist_ok=False)
        return cls(path)

    def write_json(self, name: str, value: dict) -> Path:
        path = self.path / name
        path.write_text(
            json.dumps(redact(value), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path
```

- [ ] **Step 4: Route future real outputs into an explicit directory**

Add `VIDEOACT_RUNS_DIR` support without executing an API:

```python
RUNS_DIR = os.path.expanduser(os.environ.get("VIDEOACT_RUNS_DIR", "./runs"))
```

The actual response/task/video wiring will be implemented only after the real response schema is observed in Task 6.

- [ ] **Step 5: Run local filesystem checks and commit**

Run: `python -m unittest tests.test_run_directory -v`

Expected: PASS for real local filesystem behavior only.

Run: `git add videoactagent/run_record.py tests/test_run_directory.py kling_demo.py seedance_demo.py`

Run: `git commit -m "feat: add redacted local experiment directories"`

## Task 5: Submit the bounded real API batch — separate approval required

No step in this task may execute until Gate B is explicitly approved.

- [ ] **Step 1: Pre-call report**

Proposed batch:

```text
Calls: at most 2 billable task submissions
Seedance: Doubao-Seedance-2.0, T2V, 5 seconds, 720p, 1 call
Kling: Kling-V2-5-Turbo, T2V, 5 seconds, std, 16:9, 1 call
Prompt: the exact built-in prompt from each inherited script
Uploaded user media: none; text only
Automatic submit retries: 0
Poll interval: 15 seconds
Task timeout: 30 minutes each
Output: ./runs and the configured download directory
Cost ceiling: supplied and approved by the user before execution
```

- [ ] **Step 2: Rotate the leaked key**

The user revokes/rotates the inherited key in JD Cloud. The replacement is supplied only through `JD_KLING_KEY`; it is never pasted into source, fixtures, reports, or committed shell scripts.

- [ ] **Step 3: Execute one real Seedance call**

Run only after approval: `python seedance_demo.py t2v`

Acceptance evidence: real task ID, raw terminal transcript, actual terminal status, actual response, actual elapsed time, actual/estimated charge, and downloaded real MP4 or recorded real failure.

- [ ] **Step 4: Report Seedance before deciding on Kling if approval is per-call**

Do not infer success from a local test. Do not retry a failed submission without new approval.

- [ ] **Step 5: Execute one real Kling call**

Run only if included in approval: `python kling_demo.py t2v`

Acceptance evidence is identical to Step 3.

## Task 6: Refactor the common gateway using redacted real responses

This task begins only after at least one real task response exists.

**Files:**
- Create: `tests/fixtures/real/<backend>_response.redacted.json`
- Create: `tests/fixtures/real/<backend>_provenance.json`
- Create: `tests/test_real_response_parsing.py`
- Create: `videoactagent/backends/jd_gateway.py`
- Modify: `kling_demo.py`
- Modify: `seedance_demo.py`

- [ ] **Step 1: Produce a redacted derivative of the real response**

The fixture must retain the actual nesting, status fields, content types, IDs hashed if necessary, and video URL field names. Credentials, signatures, and expiring URLs are redacted. Provenance records backend, model, UTC time, source run directory, and a SHA-256 hash of the unredacted local response.

- [ ] **Step 2: Write parser tests against only that real-derived fixture**

The exact assertions are written after observing the response. They must assert the actual status path and actual video URL path; no guessed schema is allowed.

- [ ] **Step 3: Observe parser-test failure before implementing the shared client**

Run the fixture-specific test and confirm failure because the shared parser does not exist.

- [ ] **Step 4: Implement only the observed real schema plus documented top-level/nested variants seen across the two real backends**

`POST /v1/task/submit` is never automatically retried. Only non-billable GET status queries may use bounded transport retries.

- [ ] **Step 5: Re-query/download an existing real task only if Gate C permits it**

No new generation is submitted. Verify the refactored client against the existing task and actual MP4. If Gate C does not permit requests, report the parser result separately and leave external verification pending.

- [ ] **Step 6: Commit the real-response-driven refactor**

The commit message identifies that fixtures are redacted derivatives of real responses, not synthetic fixtures.

## Task 7: Stage 0 report and hard stop

Report:

- commits and changed files;
- local source/syntax/filesystem checks, explicitly labeled local;
- exact real API request count and model settings;
- real task IDs in a private/redacted form as appropriate;
- actual statuses, MP4 links, durations, and cost;
- actual response-schema findings;
- failures and limitations, especially reference-video support;
- Stage 1 proposal;
- `WAITING FOR USER APPROVAL`.

Do not begin Stage 1.

## Self-review

- No fake HTTP transport or invented external success appears in the plan.
- All external claims require approved real tasks.
- Local tests are limited to real source files, subprocess CLIs, filesystem writes, and real-derived response fixtures.
- POST task submission has zero automatic retries.
- All inherited modes remain in place; dry-run is additive.
- API and server gates remain unchanged.

