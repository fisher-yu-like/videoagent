# Stage 3 Backend Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an evidence-aware Seedance/Kling gateway layer that compiles ready prompt requests, blocks unsupported or unbound media controls, and performs exactly one real Kling prompt-only smoke submission without automatic submit retries.

**Architecture:** A static gateway capability registry separates model-level claims from capabilities verified in the inherited JD gateway client. A preparation CLI reads the real Stage 2 control bundle and produces ready prompt payloads plus explicit blockers for first/last-frame and proxy-video conditions. A small JD client records one request and its real response, while query and download operations are separate commands so task submission can never repeat automatically.

**Tech Stack:** Python 3.12 standard library; existing ShotScript/control bundle JSON; JD Cloud `/v1/task/submit` and `/v1/task/{task_id}` gateway; unittest.

---

## Evidence boundary

- Official Volcano Engine material confirms Seedance 2.0 accepts text, image, audio, and video modalities at the model level.
- The inherited JD script confirms only its currently coded text, image, first/last-frame, reference-image, and reference-audio payload shapes. It contains no `reference_video` field.
- Therefore `reference_video` is `model_supported` but `gateway_unverified`; the code must refuse to submit it.
- Unit tests use controlled dictionaries only as code evidence. They are not API acceptance evidence.
- Only the real gateway response and downloaded bytes may be reported as external evidence.
- Seedance must not be submitted again because the previous smoke outcome is unknown. Kling receives at most one POST, with zero automatic submit retries.
- No server or rented GPU is used.

## Baseline and source inheritance

- `seedance_demo.py` remains the authoritative local JD Seedance baseline for model names, `/v1/task/submit`, `/v1/task/{task_id}`, content items, first/last-frame roles, status polling, and result URL extraction.
- `kling_demo.py` remains the authoritative local JD Kling baseline for `Kling-V2-5-Turbo`, text/image payload shapes, gateway routes, and response handling.
- Stage 3 extracts only shared, testable gateway functions; it does not replace the inherited demos with a new framework or add a multi-agent runtime.
- Camera Artist and SceneCraft influence the already-approved structured planning and executable-scene representation only. They do not justify adding extra agents here.
- VACE remains the later open-source baseline for RGB/depth/pose/mask structural control. No custom diffusion architecture or training code is introduced in this stage.

## Files

- Create: `videoactagent/backends/capabilities.py`
- Create: `videoactagent/backends/jd.py`
- Create: `videoactagent/backend_prepare.py`
- Create: `videoactagent/jd_smoke.py`
- Create: `tests/test_backend_contracts.py`
- Create: `tests/test_backend_prepare.py`
- Create: `tests/test_jd_response_parsing.py`
- Create at runtime: `runs/stage3_backend/readiness.json`
- Create at runtime after the only POST: `runs/stage3_api/<run_id>/request.json`, `response.json`, `state.json`
- Create after verification: `docs/reports/2026-07-29-stage-3-backend-gateway.md`

## Task 1: Gateway capability registry and payload builders

**Files:**

- Create: `tests/test_backend_contracts.py`
- Create: `videoactagent/backends/capabilities.py`
- Create: `videoactagent/backends/jd.py`

- [ ] **Step 1: Write failing capability and payload tests**

Assert the wished-for gateway contract:

```python
seedance = gateway_capability("seedance")
self.assertEqual(seedance.reference_video, "gateway_unverified")
self.assertEqual(seedance.first_last_frame, "client_declared")
self.assertEqual(gateway_capability("kling").text_to_video, "client_declared")
```

Build prompt-only payloads from the real s01 cinematic prompt and assert the exact inherited gateway shape:

```python
kling = build_kling_t2v(prompt)
self.assertEqual(kling["model"], "Kling-V2-5-Turbo")
self.assertEqual(kling["content"], [{"type": "text", "text": prompt}])
self.assertEqual(kling["parameters"], {
    "duration": 5, "mode": "std", "aspect_ratio": "16:9"
})
```

Also assert Seedance first/last items use `image_url` with roles `first_frame` and `last_frame`. These are payload-shape tests only.

- [ ] **Step 2: Run RED**

Run `python -m unittest tests.test_backend_contracts -v`; expect import failure because the new modules do not exist.

- [ ] **Step 3: Implement immutable capabilities and minimal builders**

Use explicit evidence states: `client_declared`, `model_supported`, `gateway_unverified`, and `unsupported`. Do not add an override that permits `gateway_unverified` submission.

Implement:

- `build_kling_t2v(prompt, duration=5)`;
- `build_seedance_t2v(prompt, duration=5)`;
- `build_seedance_first_last(prompt, first_url, last_url, duration=5)`.

Do not implement a Seedance reference-video payload while its JD gateway field is unverified.

- [ ] **Step 4: Run focused and full tests**

- [ ] **Step 5: Commit**

```powershell
git add -- videoactagent/backends/capabilities.py videoactagent/backends/jd.py tests/test_backend_contracts.py
git commit -m "feat: define evidence-aware backend contracts"
```

## Task 2: Real-bundle readiness compiler

**Files:**

- Create: `tests/test_backend_prepare.py`
- Create: `videoactagent/backend_prepare.py`

- [ ] **Step 1: Write failing readiness tests**

Load `runs/stage2_control_bridge/control_bundle.json` when present, otherwise create the same bundle through the real Control Bridge fixture path. Invoke:

```python
report = prepare_backend(bundle, "seedance", bindings={})
```

Assert:

- plain and cinematic prompt conditions are `ready` and contain payloads;
- first/last is `blocked` with missing remote asset bindings;
- proxy video is `blocked` by both missing binding and `gateway_unverified` capability;
- the report contains no task ID and no invented URL.

Add pure URL validation tests: HTTPS and `asset://` bindings are syntactically accepted, local paths, `file://`, HTTP, data URLs, loopback, and placeholder domains are rejected. Acceptance here means syntax only, not proof the remote asset exists.

- [ ] **Step 2: Run RED**

Expected: import failure because `videoactagent.backend_prepare` does not exist.

- [ ] **Step 3: Implement readiness compilation and CLI**

The CLI takes `--bundle`, `--backend`, optional `--bindings`, and `--output`. It writes JSON containing backend capability evidence, each condition's `ready`/`blocked` status, ready payloads, and all blockers. It must never make a network request.

- [ ] **Step 4: Run the persistent readiness experiment**

```powershell
python videoactagent/backend_prepare.py --bundle runs/stage2_control_bridge/control_bundle.json --backend seedance --output runs/stage3_backend/readiness.json
```

Expected: two prompt conditions ready; first/last and proxy-video blocked with truthful reasons.

- [ ] **Step 5: Commit**

```powershell
git add -- videoactagent/backend_prepare.py tests/test_backend_prepare.py
git commit -m "feat: gate backend submissions on real capabilities"
```

## Task 3: One-shot JD client and response parsing

**Files:**

- Create: `tests/test_jd_response_parsing.py`
- Modify: `videoactagent/backends/jd.py`
- Create: `videoactagent/jd_smoke.py`

- [ ] **Step 1: Write failing parser tests**

Test pure parsers against both response shapes already handled by the inherited scripts:

```python
self.assertEqual(extract_task_id({"result": {"task_id": "real-shape-id"}}), "real-shape-id")
self.assertEqual(extract_status({"task_status": "success"}), "success")
self.assertEqual(extract_status({"result": {"task_status": "failed"}}), "failed")
```

Use obviously controlled identifiers and label these as parser unit tests. Do not create HTTP mocks and do not treat them as gateway evidence.

- [ ] **Step 2: Run RED**

Expected: missing parser functions.

- [ ] **Step 3: Implement submit-once, query-once, and download-once**

`submit_once` performs one `urllib.request.urlopen` call with a 60-second timeout and no retry loop. It writes the redacted request before the call, writes the raw parsed response after the call, extracts the task ID, writes `state.json`, flushes `SUBMITTED task_id=<id>` to stdout, and returns.

`query_once` performs one GET for an existing ID and writes a timestamped response. `download_once` downloads a returned video URL to the same run directory and records bytes plus SHA-256. Neither function can submit.

The CLI has separate subcommands:

```text
jd_smoke.py submit-kling --bundle ... --shot s01 --prompt cinematic --run-root ...
jd_smoke.py query --run-dir <existing-run>
jd_smoke.py download --run-dir <existing-run>
```

- [ ] **Step 4: Run focused and full tests**

- [ ] **Step 5: Commit**

```powershell
git add -- videoactagent/backends/jd.py videoactagent/jd_smoke.py tests/test_jd_response_parsing.py
git commit -m "feat: add non-retrying JD gateway client"
```

## Task 4: Exactly one real Kling smoke

- [ ] **Step 1: Confirm preconditions without exposing secrets**

Confirm the Windows User `JD_KLING_KEY` is set, the selected request payload contains only s01 cinematic text, `runs/stage3_api` has no prior Kling run for this stage, and no POST has occurred during Tasks 1–3.

- [ ] **Step 2: Submit once**

Load the key into the child process environment without printing it, run Python unbuffered, and allow the command to finish its one POST. Set the shell timeout longer than the HTTP timeout so task ID output is not lost. Never rerun this command regardless of outcome.

- [ ] **Step 3: Query the existing task without resubmission**

Use the separate query command at no more than 30-second intervals, reporting unchanged state without another POST. Stop after success, terminal failure, or a bounded observation window.

- [ ] **Step 4: Download and verify only on success**

If a real URL is returned, download it once, compute SHA-256, and use Blender to report resolution, frame count, fps, and duration. If submission or generation fails, retain and report the real response; do not fabricate a video or mark success.

## Task 5: Report and stage boundary

Write `docs/reports/2026-07-29-stage-3-backend-gateway.md` with:

- official model-level and local gateway evidence separated;
- exact readiness blockers;
- the one real Kling POST outcome, task ID redacted in the user-facing report if appropriate, terminal status, download metadata, cost if the response provides it, and number of query calls;
- Seedance still unverified and not retried;
- no server/GPU usage;
- explicit statement that prompt-only Kling output does not validate proxy injection.

Run the full suite, `git diff --check`, and secret scan before committing the report. Preserve `.idea/` and unrelated user files.
