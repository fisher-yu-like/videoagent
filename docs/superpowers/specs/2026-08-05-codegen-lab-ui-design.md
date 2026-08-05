# Codegen Lab UI 设计

## 目标

新增一个独立的本地 Codegen 实验页面，让用户从浏览器真实启动一次 DeepSeek-v4-pro 代码生成和本地 Blender 渲染，同时保留现有单机位标注器和三机位/完整向导的启动路径不变。

## 端口与入口

| 功能 | CLI | 默认端口 | 页面 |
|---|---|---:|---|
| 单机位人工标注器 | `videoactagent director-loop serve` | 8769 | `director_panel.html` |
| 三机位标注器/完整向导 | `videoactagent director-multicam serve` 或 `director-wizard start` | 8770 | 现有页面 |
| Codegen 实验页 | `videoactagent codegen-lab serve` | 8781 | `codegen_lab.html` |

Codegen 服务使用独立的 HTTP server 和独立 URL，不注册到 8770，不读写现有向导的状态，也不修改现有人工标注器页面。

## 用户流程

1. 填写或选择 prompt、ShotScript、轨迹 JSON、保护工作区和 `D:\blender\blender.exe`。
2. 点击“准备 CG 作业”。服务调用 `prepare_codegen_job`，只做输入快照、源文件哈希和 8770 guard 检查，不调用 API/Blender。
3. 点击“真实生成”。服务调用 `generate_codegen_job`，只允许一次真实 DeepSeek 请求，显示模型、耗时、调用次数、重试次数和 AST 安全门结果。
4. 生成成功后，用户可点击一次 Smoke 渲染；通过后可点击 Full 渲染。每种 profile 的输出目录不可覆盖，失败后页面只显示失败证据。
5. 页面显示 `job.json` 状态、日志、生成代码摘要、MP4 播放器、三张关键帧、manifest 和轨迹误差。Smoke 或 API 失败时不显示“成功”状态，也不会自动重试。

## HTTP API

服务保存一个实验根目录和当前 job 路径；所有路径由用户显式填写并在服务端解析为绝对路径。

- `GET /`：返回 Codegen 页面。
- `GET /api/health`：返回服务版本和 Blender 路径是否存在，不调用 API。
- `POST /api/prepare`：接收 `experiments_root`、`protected_workspace`、`protected_url`、`prompt`、`shotscript`、`trajectory`、`fps`、`resolution`，返回 `job_path` 和准备状态。
- `POST /api/generate`：接收 `job_path`，执行一次真实 DeepSeek 请求，返回 `job.json` 摘要；状态已不是 `prepared` 时拒绝。
- `POST /api/render`：接收 `job_path`、`profile`、`blender`，执行一次真实 Blender；profile 只允许 `smoke`/`full`。
- `GET /api/status?job=...`：返回脱敏状态、调用次数、失败信息、代码安全证据和渲染文件索引。
- `GET /api/artifact?job=...&path=...`：只允许读取 job 根目录内的 MP4、PNG、日志、manifest 和生成代码，拒绝 `..` 路径和 job 外文件。

服务端为每个变更操作使用 job lock；重复点击返回明确的 busy/terminal 错误。响应不包含 API key，日志和请求证据沿用现有脱敏规则。

## 前端布局

- 左侧“输入与操作”卡：表单、路径提示、准备/生成/渲染按钮。
- 中间“状态时间线”卡：prepared → generating → code_validated → rendering → succeeded 或失败终态。
- 右侧“证据”卡：模型、调用次数、哈希、Blender 版本、轨迹误差和失败原因。
- 下方“结果”卡：Smoke/Full 标签、原始 MP4 播放器、first/middle/last PNG、可折叠日志与生成代码。
- 页面内小字说明：生成是一次真实 API 调用；渲染是本地真实 Blender；失败不会自动重试；视频质量仍需人工观看。

## 安全与隔离

- 不修改 `director_loop.py`、`director_multicam.py`、`director_wizard.py` 和现有 run 目录。
- Codegen job 继续复用现有输入契约、AST 安全门、Blender runner、视频验证器和保护树 guard。
- 任何 API/Blender 调用前都检查状态和路径；服务进程环境不把凭据写入响应。
- 真实失败保留在 `CG<n>`，不通过修改生成代码或更换 seed 搜索成功。

## 验收

- 8769、8770、8781 三个服务可以同时启动并返回 HTTP 200，页面内容不同。
- 页面准备操作 API 调用数为 0；生成最多 1 次；Smoke/Full 使用同一份代码哈希。
- 真实生成失败时页面显示失败证据；真实渲染失败时不出现成功视频链接。
- 现有 director-loop/director-multicam/director-wizard 测试全部保持通过。
- 新增 UI/API 合同测试覆盖路径越界、重复点击、错误状态、脱敏和 artifact 访问。
