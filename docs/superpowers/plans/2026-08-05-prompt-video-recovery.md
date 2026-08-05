# Prompt Video Recovery Implementation Plan

**Goal:** 让 Prompt→Blender 页面在刷新后恢复最近一次已验收视频，同时保留 8770 三机位向导并行运行。

**Architecture:** 后端扫描 `codegen_blender_v1/PF*/job.json`，只选择状态为 `succeeded` 且视频文件真实存在的最新作业，返回受 `/api/artifact` 保护的 URL。前端首次加载调用该接口；当前作业完成或视频加载失败时更新播放器和状态提示。8770 不改动。

**Tech Stack:** Python `http.server`、现有 Codegen Lab API、原生 HTML/JavaScript、unittest/pytest。

---

### Task 1: Backend latest-result contract

**Files:**
- Modify: `videoactagent/codegen_lab.py`
- Test: `tests/test_codegen_lab.py`, `tests/test_codegen_lab_http.py`

- [ ] Add a failing test that creates one successful PF job with a real MP4 and asserts the latest endpoint returns its artifact URL while failed jobs are ignored.
- [ ] Run the focused test and observe the missing-method/route failure.
- [ ] Add a shared video URL builder and `latest_prompt_status()` that validates job ownership, status, and non-empty MP4 before returning a URL.
- [ ] Add `GET /api/prompt-latest`.
- [ ] Run backend tests and confirm they pass.

### Task 2: Frontend recovery and media errors

**Files:**
- Modify: `static/codegen_lab.html`
- Test: `tests/test_codegen_lab_panel.py`

- [ ] Add failing assertions for `/api/prompt-latest`, explicit `video.load()`, and a video error handler.
- [ ] Run the panel test and observe failure.
- [ ] Add `setVideoSource`, call it on initial page load and successful completion, and show a readable load error.
- [ ] Run the panel test and confirm it passes.

### Task 3: Real artifact verification and service check

**Files:**
- No production files beyond Tasks 1–2.

- [ ] Run the focused backend and panel tests.
- [ ] Query `/api/prompt-latest` on the running 8781 service and request the returned PF7 MP4 through `/api/artifact`.
- [ ] Confirm 8770 and 8781 are both listening and record the two URLs.
- [ ] Commit and push the change.
