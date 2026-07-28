# Stage 0 Secure API Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the inherited hard-coded credentials with a tested, reusable JD Cloud asynchronous video-generation client, preserve the legacy Kling and Seedance T2V entry points, and produce one approved real smoke-test result per backend.

**Architecture:** Keep model-specific payload construction separate from a standard-library-only gateway client. All local tests inject fake transports and perform no network calls. Real API smoke tests are isolated behind an explicit `--execute` flag and a separate user approval gate.

**Tech Stack:** Python 3.10+, standard library (`argparse`, `dataclasses`, `json`, `urllib`, `unittest`), JD Cloud AI Gateway, existing Kling/Seedance scripts.

---

## Mandatory execution gates

This plan must not run continuously from start to finish.

1. Obtain user approval for local Stage 0 work, including `git init`, file edits, and local tests.
2. Complete Tasks 1-7 without network calls and send a local-results report.
3. Obtain a second explicit approval for a bounded API batch. The default proposed batch is two calls: one Seedance 2.0 T2V call and one Kling T2V call, both 5 seconds at the lowest approved cost/quality setting, with zero automatic paid retries.
4. Run Task 8 only within the approved model, duration, resolution, call-count, retry, and cost bounds.
5. Send the Stage 0 completion report and wait for approval before planning Stage 1.

## File map

- Create: `.gitignore` — excludes secrets, virtual environments, generated runs, and model artifacts.
- Create: `pyproject.toml` — defines the local package and CLI entry point without third-party dependencies.
- Create: `videoactagent/__init__.py` — package metadata.
- Create: `videoactagent/backends/__init__.py` — public backend exports.
- Create: `videoactagent/backends/jd_gateway.py` — authenticated submit/query/poll/download logic and bounded retry policy.
- Create: `videoactagent/backends/kling.py` — Kling payload builders.
- Create: `videoactagent/backends/seedance.py` — Seedance payload builders.
- Create: `videoactagent/run_record.py` — creates reproducible run directories and saves redacted JSON artifacts.
- Create: `videoactagent/cli.py` — dry-run-by-default command line interface.
- Modify: `kling_demo.py` — remove embedded credential and delegate to the package CLI.
- Modify: `seedance_demo.py` — remove embedded credential and delegate to the package CLI.
- Create: `tests/fakes.py` — deterministic fake gateway transport.
- Create: `tests/test_gateway.py` — gateway unit tests.
- Create: `tests/test_payloads.py` — model payload contract tests.
- Create: `tests/test_run_record.py` — redaction and artifact tests.
- Create: `tests/test_cli.py` — dry-run and execute-guard tests.

### Task 1: Initialize repository and safe package scaffold

**Files:**
- Create: `.gitignore`
- Create: `pyproject.toml`
- Create: `videoactagent/__init__.py`
- Create: `videoactagent/backends/__init__.py`

- [ ] **Step 1: Confirm no repository exists**

Run: `git rev-parse --show-toplevel`

Expected: exit code 1 with `fatal: not a git repository`.

- [ ] **Step 2: Initialize Git on a feature branch after the Stage 0 local-work approval**

Run: `git init -b stage0-api-baseline`

Expected: `Initialized empty Git repository` and `git branch --show-current` prints `stage0-api-baseline`, avoiding implementation on `main` or `master`.

- [ ] **Step 3: Create `.gitignore`**

```gitignore
__pycache__/
*.py[cod]
.pytest_cache/
.venv/
venv/
.env
.env.*
!.env.example
runs/
models/
checkpoints/
*.safetensors
*.ckpt
*.pth
*.mp4
*.blend
```

- [ ] **Step 4: Create `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "videoactagent"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = []

[project.scripts]
videoactagent = "videoactagent.cli:main"

[tool.setuptools.packages.find]
include = ["videoactagent*"]
```

- [ ] **Step 5: Create package exports**

```python
# videoactagent/__init__.py
__version__ = "0.1.0"
```

```python
# videoactagent/backends/__init__.py
from .jd_gateway import GatewayConfig, GatewayError, JdGatewayClient

__all__ = ["GatewayConfig", "GatewayError", "JdGatewayClient"]
```

- [ ] **Step 6: Verify imports**

Run: `python -c "import videoactagent; print(videoactagent.__version__)"`

Expected: `0.1.0`.

- [ ] **Step 7: Commit scaffold**

Run: `git add .gitignore pyproject.toml videoactagent/__init__.py videoactagent/backends/__init__.py docs`

Run: `git commit -m "chore: initialize videoactagent package"`

Expected: one root commit with no generated files or credentials.

### Task 2: Define credential and gateway configuration

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/test_gateway.py`
- Create: `videoactagent/backends/jd_gateway.py`

- [ ] **Step 1: Write the failing configuration tests**

```python
# tests/test_gateway.py
import os
import unittest
from unittest.mock import patch

from videoactagent.backends.jd_gateway import GatewayConfig, GatewayError


class GatewayConfigTests(unittest.TestCase):
    def test_from_env_requires_api_key(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(GatewayError, "JD_KLING_KEY"):
                GatewayConfig.from_env()

    def test_from_env_normalizes_base_url(self):
        env = {
            "JD_KLING_KEY": "test-secret",
            "JD_KLING_BASE": "https://example.invalid/",
        }
        with patch.dict(os.environ, env, clear=True):
            config = GatewayConfig.from_env()
        self.assertEqual(config.api_key, "test-secret")
        self.assertEqual(config.base_url, "https://example.invalid")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m unittest tests.test_gateway -v`

Expected: FAIL because `videoactagent.backends.jd_gateway` does not exist.

- [ ] **Step 3: Implement configuration without a default secret**

```python
# videoactagent/backends/jd_gateway.py
from __future__ import annotations

from dataclasses import dataclass
import os


class GatewayError(RuntimeError):
    pass


@dataclass(frozen=True)
class GatewayConfig:
    api_key: str
    base_url: str = "https://modelservice.jdcloud.com"
    poll_interval_s: float = 15.0
    timeout_s: float = 1800.0
    request_timeout_s: float = 60.0
    query_max_retries: int = 2

    @classmethod
    def from_env(cls) -> "GatewayConfig":
        api_key = os.environ.get("JD_KLING_KEY", "").strip()
        if not api_key:
            raise GatewayError("JD_KLING_KEY is required")
        base_url = os.environ.get(
            "JD_KLING_BASE", "https://modelservice.jdcloud.com"
        ).rstrip("/")
        return cls(api_key=api_key, base_url=base_url)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m unittest tests.test_gateway -v`

Expected: 2 tests PASS.

- [ ] **Step 5: Scan inherited files for embedded keys**

Run: `rg -n "pk-[A-Za-z0-9-]+|API_KEY\s*=.*['\"]pk-" . -g "*.py"`

Expected at this point: exactly the two inherited credential lines, which Task 7 removes. Do not print or copy the credential into reports.

- [ ] **Step 6: Commit configuration**

Run: `git add tests videoactagent/backends/jd_gateway.py`

Run: `git commit -m "feat: require gateway credentials from environment"`

### Task 3: Implement submit, query, retry, and polling with a fake transport

**Files:**
- Create: `tests/fakes.py`
- Modify: `tests/test_gateway.py`
- Modify: `videoactagent/backends/jd_gateway.py`

- [ ] **Step 1: Add a deterministic fake transport**

```python
# tests/fakes.py
class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers, payload, timeout_s):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "payload": payload,
                "timeout_s": timeout_s,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response
```

- [ ] **Step 2: Add failing client tests**

Append to `tests/test_gateway.py`:

```python
from tests.fakes import FakeTransport
from videoactagent.backends.jd_gateway import JdGatewayClient


class GatewayClientTests(unittest.TestCase):
    def setUp(self):
        self.config = GatewayConfig(
            api_key="test-secret",
            base_url="https://example.invalid",
            poll_interval_s=0,
            timeout_s=5,
        )

    def test_submit_returns_task_id(self):
        transport = FakeTransport([{"result": {"task_id": "task-1"}}])
        client = JdGatewayClient(self.config, transport=transport)
        task_id = client.submit({"model": "demo", "content": []})
        self.assertEqual(task_id, "task-1")
        self.assertEqual(transport.calls[0]["method"], "POST")
        self.assertEqual(
            transport.calls[0]["url"],
            "https://example.invalid/v1/task/submit",
        )

    def test_poll_accepts_nested_status(self):
        transport = FakeTransport(
            [
                {"result": {"task_status": "running"}},
                {"result": {"task_status": "success", "content": []}},
            ]
        )
        client = JdGatewayClient(self.config, transport=transport)
        final = client.poll("task-1", sleep=lambda _: None)
        self.assertEqual(final["result"]["task_status"], "success")

    def test_poll_raises_on_failed_task(self):
        transport = FakeTransport(
            [{"task_status": "failed", "error": {"message": "bad request"}}]
        )
        client = JdGatewayClient(self.config, transport=transport)
        with self.assertRaisesRegex(GatewayError, "bad request"):
            client.poll("task-1", sleep=lambda _: None)

    def test_submit_is_never_automatically_retried(self):
        transport = FakeTransport([GatewayError("temporary failure")])
        client = JdGatewayClient(self.config, transport=transport)
        with self.assertRaisesRegex(GatewayError, "temporary failure"):
            client.submit({"model": "demo", "content": []})
        self.assertEqual(len(transport.calls), 1)

    def test_extract_video_urls_accepts_nested_content(self):
        response = {
            "result": {
                "content": [
                    {
                        "id": "output-1",
                        "video_url": {"url": "https://files.invalid/video.mp4"},
                    }
                ]
            }
        }
        client = JdGatewayClient(self.config, transport=FakeTransport([]))
        self.assertEqual(
            client.extract_video_urls(response),
            [("output-1", "https://files.invalid/video.mp4")],
        )
```

- [ ] **Step 3: Run tests to verify failure**

Run: `python -m unittest tests.test_gateway -v`

Expected: FAIL because `JdGatewayClient` is not defined.

- [ ] **Step 4: Implement the transport and client**

Append to `videoactagent/backends/jd_gateway.py`:

```python
import json
import time
from typing import Callable, Protocol
import urllib.error
import urllib.request
import uuid


class Transport(Protocol):
    def request(self, method, url, headers, payload, timeout_s): ...


class UrllibTransport:
    def request(self, method, url, headers, payload, timeout_s):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise GatewayError(f"HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise GatewayError(f"network error: {exc.reason}") from exc


class JdGatewayClient:
    def __init__(self, config: GatewayConfig, transport: Transport | None = None):
        self.config = config
        self.transport = transport or UrllibTransport()

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
            "Trace-id": uuid.uuid4().hex,
        }

    def _request(
        self, method: str, path: str, payload=None, *, max_retries: int = 0
    ) -> dict:
        url = f"{self.config.base_url}{path}"
        last_error = None
        for attempt in range(max_retries + 1):
            try:
                return self.transport.request(
                    method,
                    url,
                    self._headers(),
                    payload,
                    self.config.request_timeout_s,
                )
            except GatewayError as exc:
                last_error = exc
                if attempt == max_retries:
                    break
                time.sleep(2**attempt)
        raise last_error or GatewayError("request failed")

    def submit(self, payload: dict) -> str:
        # Submitting can create a billable task, so POST is never retried automatically.
        response = self._request("POST", "/v1/task/submit", payload)
        if response.get("error"):
            raise GatewayError(str(response["error"]))
        task_id = (response.get("result") or {}).get("task_id")
        if not task_id:
            raise GatewayError("submit response has no result.task_id")
        return task_id

    def query(self, task_id: str) -> dict:
        return self._request(
            "GET",
            f"/v1/task/{task_id}",
            max_retries=self.config.query_max_retries,
        )

    @staticmethod
    def task_status(response: dict) -> str | None:
        return response.get("task_status") or (response.get("result") or {}).get(
            "task_status"
        )

    def poll(self, task_id: str, sleep: Callable[[float], None] = time.sleep) -> dict:
        deadline = time.monotonic() + self.config.timeout_s
        while time.monotonic() < deadline:
            response = self.query(task_id)
            status = self.task_status(response)
            if status == "success":
                return response
            if status in {"failed", "cancelled"}:
                error = response.get("error") or (response.get("result") or {}).get(
                    "error"
                ) or {}
                raise GatewayError(error.get("message") or f"task {status}")
            sleep(self.config.poll_interval_s)
        raise GatewayError(f"task {task_id} timed out")

    @staticmethod
    def extract_video_urls(response: dict) -> list[tuple[str, str]]:
        content = response.get("content") or (response.get("result") or {}).get(
            "content"
        ) or []
        urls = []
        for index, item in enumerate(content):
            url = (item.get("video_url") or {}).get("url")
            if url:
                urls.append((item.get("id") or f"video-{index}", url))
        return urls

    @staticmethod
    def download_video_urls(
        urls: list[tuple[str, str]],
        directory,
        *,
        fetch=urllib.request.urlretrieve,
    ) -> list[str]:
        from pathlib import Path

        destination = Path(directory)
        destination.mkdir(parents=True, exist_ok=True)
        paths = []
        for index, (item_id, url) in enumerate(urls):
            safe_id = "".join(
                character if character.isalnum() or character in "-_" else "_"
                for character in item_id
            )
            path = destination / f"{index:02d}_{safe_id}.mp4"
            fetch(url, str(path))
            paths.append(str(path))
        return paths
```

- [ ] **Step 5: Run tests to verify pass**

Run: `python -m unittest tests.test_gateway -v`

Expected: 7 tests PASS and no network access. The submit failure produces exactly one transport call.

- [ ] **Step 6: Commit gateway client**

Run: `git add tests videoactagent/backends/jd_gateway.py`

Run: `git commit -m "feat: add testable JD gateway task client"`

### Task 4: Implement and test model payload builders

**Files:**
- Create: `tests/test_payloads.py`
- Create: `videoactagent/backends/kling.py`
- Create: `videoactagent/backends/seedance.py`

- [ ] **Step 1: Write failing payload contract tests**

```python
# tests/test_payloads.py
import unittest

from videoactagent.backends.kling import kling_t2v_payload
from videoactagent.backends.seedance import (
    seedance_first_last_payload,
    seedance_t2v_payload,
)


class PayloadTests(unittest.TestCase):
    def test_kling_t2v_payload(self):
        payload = kling_t2v_payload("two people meet", duration=5)
        self.assertEqual(payload["model"], "Kling-V2-5-Turbo")
        self.assertEqual(payload["content"][0]["text"], "two people meet")
        self.assertEqual(payload["parameters"]["duration"], 5)

    def test_seedance_t2v_payload(self):
        payload = seedance_t2v_payload("dolly in", duration=5)
        self.assertEqual(payload["model"], "Doubao-Seedance-2.0")
        self.assertEqual(payload["parameters"]["resolution"], "720p")

    def test_seedance_first_last_roles(self):
        payload = seedance_first_last_payload(
            "smooth transition", "https://a/first.png", "https://a/last.png"
        )
        roles = [item.get("role") for item in payload["content"]]
        self.assertIn("first_frame", roles)
        self.assertIn("last_frame", roles)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m unittest tests.test_payloads -v`

Expected: FAIL because the backend modules do not exist.

- [ ] **Step 3: Implement minimal Kling builder**

```python
# videoactagent/backends/kling.py
def kling_t2v_payload(
    prompt: str,
    *,
    duration: int = 5,
    mode: str = "std",
    aspect_ratio: str = "16:9",
) -> dict:
    return {
        "model": "Kling-V2-5-Turbo",
        "content": [{"type": "text", "text": prompt}],
        "parameters": {
            "duration": duration,
            "mode": mode,
            "aspect_ratio": aspect_ratio,
        },
    }
```

- [ ] **Step 4: Implement minimal Seedance builders**

```python
# videoactagent/backends/seedance.py
DEFAULT_MODEL = "Doubao-Seedance-2.0"


def seedance_t2v_payload(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    duration: int = 5,
    ratio: str = "16:9",
    resolution: str = "720p",
) -> dict:
    return {
        "model": model,
        "content": [{"type": "text", "text": prompt}],
        "parameters": {
            "ratio": ratio,
            "resolution": resolution,
            "duration": duration,
            "watermark": False,
        },
    }


def seedance_first_last_payload(
    prompt: str,
    first_url: str,
    last_url: str,
    *,
    model: str = DEFAULT_MODEL,
    duration: int = 5,
) -> dict:
    return {
        "model": model,
        "content": [
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {"url": first_url},
                "role": "first_frame",
            },
            {
                "type": "image_url",
                "image_url": {"url": last_url},
                "role": "last_frame",
            },
        ],
        "parameters": {
            "ratio": "adaptive",
            "resolution": "720p",
            "duration": duration,
            "watermark": False,
        },
    }
```

- [ ] **Step 5: Run tests to verify pass**

Run: `python -m unittest tests.test_payloads -v`

Expected: 3 tests PASS.

- [ ] **Step 6: Commit payload builders**

Run: `git add tests/test_payloads.py videoactagent/backends/kling.py videoactagent/backends/seedance.py`

Run: `git commit -m "feat: add tested Kling and Seedance payload builders"`

### Task 5: Add redacted experiment recording

**Files:**
- Create: `tests/test_run_record.py`
- Create: `videoactagent/run_record.py`

- [ ] **Step 1: Write failing record tests**

```python
# tests/test_run_record.py
import json
from pathlib import Path
import tempfile
import unittest

from videoactagent.run_record import RunRecord


class RunRecordTests(unittest.TestCase):
    def test_save_json_redacts_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            record = RunRecord.create(Path(directory), "seedance", "dry-run")
            path = record.save_json(
                "request.json",
                {"Authorization": "Bearer secret", "prompt": "hello"},
            )
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["Authorization"], "[REDACTED]")
        self.assertEqual(saved["prompt"], "hello")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify failure**

Run: `python -m unittest tests.test_run_record -v`

Expected: FAIL because `RunRecord` does not exist.

- [ ] **Step 3: Implement run recording and recursive redaction**

```python
# videoactagent/run_record.py
from __future__ import annotations

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
class RunRecord:
    path: Path

    @classmethod
    def create(cls, root: Path, backend: str, label: str) -> "RunRecord":
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = root / f"{timestamp}_{backend}_{label}_{uuid.uuid4().hex[:8]}"
        path.mkdir(parents=True, exist_ok=False)
        return cls(path)

    def save_json(self, name: str, payload: dict) -> Path:
        path = self.path / name
        path.write_text(
            json.dumps(redact(payload), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path
```

- [ ] **Step 4: Run test to verify pass**

Run: `python -m unittest tests.test_run_record -v`

Expected: 1 test PASS.

- [ ] **Step 5: Commit run recorder**

Run: `git add tests/test_run_record.py videoactagent/run_record.py`

Run: `git commit -m "feat: record redacted experiment artifacts"`

### Task 6: Add dry-run-by-default CLI and explicit execute guard

**Files:**
- Create: `tests/test_cli.py`
- Create: `videoactagent/cli.py`

- [ ] **Step 1: Write failing CLI tests**

```python
# tests/test_cli.py
import io
import json
import unittest
from unittest.mock import patch

from videoactagent.cli import main


class CliTests(unittest.TestCase):
    def test_dry_run_does_not_require_api_key(self):
        output = io.StringIO()
        with patch("sys.stdout", output):
            code = main(["seedance", "t2v", "--prompt", "dolly in"])
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["model"], "Doubao-Seedance-2.0")

    def test_execute_requires_explicit_approval_token(self):
        with self.assertRaisesRegex(SystemExit, "--approval-token"):
            main(
                [
                    "seedance",
                    "t2v",
                    "--prompt",
                    "dolly in",
                    "--execute",
                ]
            )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m unittest tests.test_cli -v`

Expected: FAIL because `videoactagent.cli` does not exist.

- [ ] **Step 3: Implement the CLI**

```python
# videoactagent/cli.py
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .backends.jd_gateway import GatewayConfig, JdGatewayClient
from .backends.kling import kling_t2v_payload
from .backends.seedance import seedance_t2v_payload
from .run_record import RunRecord


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="videoactagent")
    parser.add_argument("backend", choices=["seedance", "kling"])
    parser.add_argument("mode", choices=["t2v"])
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--duration", type=int, default=5)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approval-token")
    parser.add_argument("--runs-dir", type=Path, default=Path("runs"))
    return parser


def build_payload(args) -> dict:
    if args.backend == "seedance":
        return seedance_t2v_payload(args.prompt, duration=args.duration)
    return kling_t2v_payload(args.prompt, duration=args.duration)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    payload = build_payload(args)
    if not args.execute:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if not args.approval_token:
        raise SystemExit("--approval-token is required for --execute")
    record = RunRecord.create(args.runs_dir, args.backend, "t2v")
    record.save_json("request.json", payload)
    client = JdGatewayClient(GatewayConfig.from_env())
    task_id = client.submit(payload)
    record.save_json("task.json", {"task_id": task_id})
    final = client.poll(task_id)
    record.save_json("response.json", final)
    urls = client.extract_video_urls(final)
    videos = client.download_video_urls(urls, record.path / "videos")
    record.save_json("videos.json", {"files": videos})
    print(record.path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run CLI tests**

Run: `python -m unittest tests.test_cli -v`

Expected: 2 tests PASS and no network calls.

- [ ] **Step 5: Run dry-run commands manually**

Run: `python -m videoactagent.cli seedance t2v --prompt "camera slowly dollies in"`

Expected: valid Seedance JSON printed; no API key required; no `runs/` directory created.

Run: `python -m videoactagent.cli kling t2v --prompt "camera slowly dollies in"`

Expected: valid Kling JSON printed; no network access.

- [ ] **Step 6: Commit CLI**

Run: `git add tests/test_cli.py videoactagent/cli.py`

Run: `git commit -m "feat: add approval-gated dry-run CLI"`

### Task 7: Remove inherited secret and preserve legacy entry points

**Files:**
- Modify: `kling_demo.py`
- Modify: `seedance_demo.py`
- Create: `tests/test_secret_scan.py`

- [ ] **Step 1: Write a failing repository secret-scan test**

```python
# tests/test_secret_scan.py
from pathlib import Path
import re
import unittest


class SecretScanTests(unittest.TestCase):
    def test_python_sources_contain_no_embedded_pk_key(self):
        offenders = []
        pattern = re.compile(r"pk-[A-Za-z0-9-]{20,}")
        for path in Path(".").rglob("*.py"):
            if pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(str(path))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify failure**

Run: `python -m unittest tests.test_secret_scan -v`

Expected: FAIL listing `kling_demo.py` and `seedance_demo.py`, without printing the key itself.

- [ ] **Step 3: Replace legacy scripts with thin wrappers**

```python
# kling_demo.py
"""Backward-compatible entry point for the Kling JD Gateway demo."""
import sys

from videoactagent.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["kling", *sys.argv[1:]]))
```

```python
# seedance_demo.py
"""Backward-compatible entry point for the Seedance JD Gateway demo."""
import sys

from videoactagent.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["seedance", *sys.argv[1:]]))
```

- [ ] **Step 4: Run the secret scan and full local suite**

Run: `python -m unittest discover -s tests -v`

Expected: all tests PASS; no network calls.

Run: `rg -n "pk-[A-Za-z0-9-]{20,}" . -g "*.py"`

Expected: no output and exit code 1.

- [ ] **Step 5: Confirm the leaked credential was rotated outside the repository**

Action: user rotates or revokes the previously embedded key in the JD Cloud console and supplies the replacement only through the `JD_KLING_KEY` environment variable when an approved API batch begins.

Expected: old key no longer authorizes requests. Do not test the old key.

- [ ] **Step 6: Commit secret removal**

Run: `git add kling_demo.py seedance_demo.py tests/test_secret_scan.py`

Run: `git commit -m "security: remove embedded gateway credential"`

### Task 8: Run the separately approved real API smoke-test batch

**Files:**
- Create at runtime: `runs/<experiment_id>/request.json`
- Create at runtime: `runs/<experiment_id>/task.json`
- Create at runtime: `runs/<experiment_id>/response.json`
- Create at runtime: `runs/<experiment_id>/*.mp4` after download support is verified

- [ ] **Step 1: Send the pre-call approval report**

The report must state the exact models, prompt, number of calls, duration, resolution/mode, maximum retries, maximum cost, uploaded data, and result location. The default proposal is:

```text
Calls: 2 total
1. Doubao-Seedance-2.0, T2V, 5 s, 720p, one call
2. Kling-V2-5-Turbo, T2V, 5 s, std mode, one call
Prompt: "A static two-person railway platform scene; camera slowly dollies in."
Inputs uploaded: text only
Paid retries: 0
Output: runs/<experiment_id>/
```

Expected: explicit user approval defining the cost ceiling. Without it, stop.

- [ ] **Step 2: Set the replacement credential only in the process environment**

Run in PowerShell after approval: `$env:JD_KLING_KEY='<replacement supplied outside source control>'`

Expected: the variable exists only in the shell environment and is absent from files and command logs. Never paste its value into a report.

- [ ] **Step 3: Run the approved Seedance call with no automatic paid retry**

The client never retries `POST /v1/task/submit`; only non-billable status queries have bounded retries. Run:

`python -m videoactagent.cli seedance t2v --prompt "A static two-person railway platform scene; camera slowly dollies in." --duration 5 --execute --approval-token stage0-approved`

Expected: one submitted task, a successful final response or one recorded failure, and a redacted run directory.

- [ ] **Step 4: Stop and report the Seedance result before the Kling call if the approval requires per-call review**

Expected report: task status, elapsed time, actual charge if available, response schema, run directory, and failure details. Do not retry without a new approval.

- [ ] **Step 5: Run the approved Kling call with no automatic paid retry**

`python -m videoactagent.cli kling t2v --prompt "A static two-person railway platform scene; camera slowly dollies in." --duration 5 --execute --approval-token stage0-approved`

Expected: one submitted task, a successful final response or one recorded failure, and a redacted run directory.

- [ ] **Step 6: Produce the Stage 0 completion report**

Include:

- commits and files changed;
- full local test result;
- secret-scan result;
- per-backend payload and response-schema summary;
- clickable MP4 or recorded failure;
- API call count, elapsed time, and actual/estimated cost;
- known limitations, including whether reference-video input is supported;
- exact Stage 1 proposal;
- explicit `WAITING FOR USER APPROVAL` status.

Do not begin Stage 1 until the user approves the Stage 0 report.

## Plan self-review record

- Spec coverage: covers Stage 0 security, common gateway flow, payloads, redacted records, dry-run guard, real smoke tests, and reporting gates.
- Scope boundary: intentionally excludes Shot Planner, Blender, reference-video control, VACE, and server/GPU work; each receives a separate post-approval plan.
- Type consistency: `GatewayConfig`, `JdGatewayClient`, payload builders, `RunRecord`, and `main(argv)` names match across tasks.
- Network boundary: Tasks 1-7 use fake transports or dry-run only; Task 8 is separately approval-gated.
- Credential boundary: no default key, `.env` excluded, repository scan enforced, old key never tested.
