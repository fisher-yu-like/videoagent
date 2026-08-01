# DeepSeek Truncated Plan Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent DeepSeek V4 reasoning from truncating schema-constrained multicamera plans and report token-limit failures precisely.

**Architecture:** Keep the existing one-call adapter and evidence layout. Add the documented top-level thinking switch to the request, then specialize only the `length` finish-reason error; no retry or model fallback is added inside the adapter.

**Tech Stack:** Python standard library, `unittest`, DeepSeek OpenAI-compatible chat-completions API.

---

### Task 1: Lock request and failure behavior with tests

**Files:**
- Modify: `tests/test_deepseek_planner.py`

- [ ] **Step 1: Add the request assertion**

In `test_single_call_writes_redacted_validated_evidence`, assert:

```python
self.assertEqual(request_payload["thinking"], {"type": "disabled"})
```

- [ ] **Step 2: Add a length-response test**

Use a fake response with `finish_reason: "length"`; assert that `DeepSeekPlannerError` contains `truncated at max_tokens=4096`, exactly one transport call occurred, and evidence records zero retries.

- [ ] **Step 3: Run the focused tests and confirm failure**

Run: `python -m unittest tests.test_deepseek_planner -v`

Expected: request assertion and specialized error test fail before implementation.

### Task 2: Apply the minimal adapter fix

**Files:**
- Modify: `videoactagent/deepseek_planner.py`

- [ ] **Step 1: Disable thinking for the JSON planning request**

Add to `request_payload`:

```python
"thinking": {"type": "disabled"},
```

- [ ] **Step 2: Specialize the truncation error**

Replace the generic check with:

```python
finish_reason = choice.get("finish_reason")
if finish_reason == "length":
    raise DeepSeekPlannerError(
        "DeepSeek response was truncated at max_tokens=4096"
    )
if finish_reason != "stop":
    raise DeepSeekPlannerError("DeepSeek response did not finish normally")
```

- [ ] **Step 3: Run the focused tests**

Run: `python -m unittest tests.test_deepseek_planner -v`

Expected: all tests pass.

- [ ] **Step 4: Commit the code and tests**

Commit message: `fix: prevent DeepSeek planning truncation`

### Task 3: Verify one real plan generation

**Files:**
- Create through the existing workflow: `runs/work/agent_multicam_suite_20260801/station_reunion/plans/P2/`

- [ ] **Step 1: Trigger the existing station planning operation once**

Use the saved S3 staging state and existing workflow entry point. Do not retry automatically.

- [ ] **Step 2: Inspect real evidence**

Confirm `response.json` has `finish_reason=stop`, `plan.json` parses and `evidence.json` has `status=succeeded`, `api_call_count=1`, `retry_count=0`.

- [ ] **Step 3: Use Flash only on real Pro failure**

If P2 fails, record the failure unchanged, set `DEEPSEEK_MODEL=deepseek-v4-flash`, restart the service, and require a separate human-triggered P3. Do not automatically retry or overwrite P2.
