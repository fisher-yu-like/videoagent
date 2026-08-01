# Innovation-Focused Group Meeting Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a truthful, innovation-focused Chinese script for a 20-minute VideoActAgent group meeting presentation.

**Architecture:** Build the report from an evidence ledger rather than memory. Separate implemented contributions, measured results, failure lessons, related-work borrowing and future work, then express each section as concise on-page copy plus a speakable script.

**Tech Stack:** Markdown, repository JSON/MP4 evidence, Git history, primary paper/project sources.

---

### Task 1: Audit the current evidence

**Files:**
- Read: `README.md`
- Read: `docs/ARCHITECTURE.md`
- Read: `docs/CODED_DRAFT_PILOT_REPORT.md`
- Read: `docs/EXPERIMENTS.md`
- Read: `runs/work/agent_multicam_suite_20260801/station_reunion/state.json`
- Read: `runs/work/agent_multicam_suite_20260801/station_reunion/iterations/M3/job.json`
- Read: `runs/work/agent_multicam_suite_20260801/station_reunion/iterations/M3/evaluation.json`

- [ ] **Step 1: Record current workflow state**

Extract current staging, plan and iteration identifiers, approval state, model-call counts, Blender status and automatic-versus-human evaluation fields.

- [ ] **Step 2: Decode the current MP4 outputs**

Use OpenCV to count readable frames, FPS and duration for all M3 camera videos; compare their hashes and sizes with `job.json`.

- [ ] **Step 3: Classify claims**

Place each potential claim in exactly one class: `implemented`, `measured`, `borrowed_not_reproduced`, `failed_or_withdrawn`, or `future_work`.

### Task 2: Verify the related-work framing

**Files:**
- Read: `README.md`
- Read: `docs/superpowers/specs/2026-08-01-agent-multicam-director-design.md`

- [ ] **Step 1: Check primary sources**

Verify the project-level relationship to SceneCraft, VideoCoCo, Training-free Camera Control, Camera Artist, ReCamMaster and VACE using arXiv, official project pages or official repositories. Do not use secondary summaries for technical claims.

- [ ] **Step 2: Limit comparison language**

Use “借鉴接口/思想”“候选后端” or “未复现” where appropriate. Do not claim benchmark superiority, model reproduction or cross-view photoreal identity consistency.

### Task 3: Write the 20-minute report

**Files:**
- Create: `docs/GROUP_MEETING_REPORT.md`

- [ ] **Step 1: Write the title and one-sentence contribution**

State that VideoActAgent is a human-in-the-loop, executable director layer for controllable video generation, not a newly trained foundation video model.

- [ ] **Step 2: Write 14 timed sections**

Use this exact timing budget, totaling 20 minutes:

```text
1.0 + 1.5 + 1.5 + 1.5 + 1.5 + 2.0 + 1.5 + 1.5 + 1.5 + 1.5 + 1.5 + 1.0 + 1.0 + 1.0 = 20.0 minutes
```

Each section contains `建议时间`, `页面短文案`, and `口头讲稿`. Sections follow the 14-part structure in the approved design.

- [ ] **Step 3: Add a concise Q&A appendix**

Answer likely questions about novelty, why Blender is needed, why Proxy is not yet photoreal, what DeepSeek controls, whether Kling/Seedance consume Proxy, how metrics are real, and what experiment comes next.

- [ ] **Step 4: Add source links**

Include a compact related-work list and point repository claims to the local evidence paths used in the report.

### Task 4: Verify and commit the report

**Files:**
- Verify: `docs/GROUP_MEETING_REPORT.md`

- [ ] **Step 1: Check timing and required sections**

Confirm exactly 14 numbered sections exist, their declared times total 20 minutes, and every section contains both short copy and spoken script.

- [ ] **Step 2: Check prohibited overclaims**

Search for statements implying completed photoreal multi-view generation, reproduced unrun papers, fair same-seed Kling/Seedance comparisons, automatic human-composition approval or automatic downstream API execution; revise any such statement.

- [ ] **Step 3: Check repository evidence**

Confirm every local path mentioned exists and all M3 numeric claims match current JSON and decoded MP4 evidence.

- [ ] **Step 4: Commit**

Commit message: `docs: add innovation-focused group meeting report`
