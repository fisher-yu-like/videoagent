# Codegen Lab 真实链路验证

日期：2026-08-05（Asia/Shanghai）  
分支：`feature/deepseek-blender-codegen`  
作业：`runs/work/codegen_blender_v1/CG2/`

## 验证范围

本次从新建的 8781 Codegen Lab HTTP 页面接口启动一条真实作业：

```text
真实 Prompt + ShotScript + K0–K4 trajectory
  → DeepSeek-v4-pro（一次）
  → AST safety gate
  → Blender 5.1.2 Smoke（一次）
  → MP4/PNG/scene.blend/manifest
```

没有调用 Full，没有自动重试，没有更换 seed，也没有把测试 fixture 当成模型结果。

## 真实 API 证据

- endpoint：环境变量 `DEEPSEEK_BASE_URL` 对应的 `https://api.deepseek.com/v1`
- model：`deepseek-v4-pro`
- API 调用次数：`1`
- retry：`0`
- API elapsed：`19.328 s`
- response id：已写入脱敏 `api/response.json`
- code safety：`accepted`
- generated code：3,809 bytes
- `generated_scene.py` SHA-256：`27a675fc3e0500bd3cd17f4754f759586ff0483978ac7780782239d4470cde55`

API 原始请求、脱敏响应和证据文件位于：

```text
runs/work/codegen_blender_v1/CG2/api/request.json
runs/work/codegen_blender_v1/CG2/api/response.json
runs/work/codegen_blender_v1/CG2/api/evidence.json
```

密钥没有写入响应或仓库文件。

## 真实 Blender 证据

- executable：`D:\blender\blender.exe`
- version：`Blender 5.1.2`
- profile：`smoke`
- resolution：`640x360`
- FPS：`8`
- frames：`40`
- duration：`5.0 s`
- MP4：`38,938 bytes`
- MP4 SHA-256：`35a17183062dfaedd759d7ec2ade5535367bab51b331e50f5bb0fccce3d5506e`
- scene.blend SHA-256：`85d879ada11fc07dd6e057dd06b975cf2e74e7fb41afec1aa33e4e68a4c56a64`（以 `codegen_manifest.json` 为准）
- render verification：`succeeded`
- video stream duration：`5.0 s`
- decoded frame count：`40`
- trajectory max error：`0.047686`

实际输出：

```text
runs/work/codegen_blender_v1/CG2/renders/smoke/video.mp4
runs/work/codegen_blender_v1/CG2/renders/smoke/scene.blend
runs/work/codegen_blender_v1/CG2/renders/smoke/frames/{first,middle,last}.png
runs/work/codegen_blender_v1/CG2/renders/smoke/codegen_manifest.json
runs/work/codegen_blender_v1/CG2/render.log
```

## 完整哈希摘要

| 文件 | bytes | SHA-256 |
|---|---:|---|
| `job.json` | 209968 | `f3585a176025b15a97e5528632cf1e365f0c19a18a6d361b658b6dfba36f4ba4` |
| `api/request.json` | 4900 | `85c1fe905f948508cfb69527cc7dbf9b04cd4d4619cd44aada1e82bbb22a9a9b` |
| `api/response.json` | 4926 | `d50005683f9af0477efcf7b4e9b507978f3b3824123796db4da750824b4d9ff7` |
| `api/evidence.json` | 807 | `1ceb52b5f37428cc2b13892f5dd561d4ecbc070384053324dff8a1b82f13ce74` |
| `api/generated_scene.py` | 3809 | `27a675fc3e0500bd3cd17f4754f759586ff0483978ac7780782239d4470cde55` |
| `render.log` | 3830 | `2f3f71fb7c69a13171036e5f669fe739f5974ab272a3e586b5d9b4090f5a2e73` |
| `renders/smoke/video.mp4` | 38938 | `35a17183062dfaedd759d7ec2ade5535367bab51b331e50f5bb0fccce3d5506e` |
| `renders/smoke/codegen_manifest.json` | 2559 | `47dc37c723cc712d700df098dd6df49a76fe9e315f56ab8cfb8ecf4b333af11d` |

## 自动化验证

- `test_codegen*.py`：32 tests，全部通过，19.155 s。
- CLI dispatcher：4 tests，全部通过。
- 旧单机位/多机位兼容性：3 tests，全部通过。
- 多机位页面契约：24 tests，全部通过。
- 8781 实际 HTTP `/`：HTTP 200，页面包含 `Codegen Lab`。
- 8781 实际 `/api/health`：HTTP 200，Blender 路径存在。
- 已运行的 8770 服务：HTTP 200。
- 真实 Prepare：`prepared`，API 调用 `0`，retry `0`，保护工作区和 guard 哈希已记录。

仓库全量 `unittest discover` 在 124 秒仍未结束，因此没有把全量套件标记为通过；这不影响上面列出的完整 Codegen/CLI/旧界面相关测试结果。

## 结论

本次真实端到端链路已跑通：DeepSeek-v4-pro 返回的代码通过安全门，并由 Blender 5.1.2 真实生成了 5 秒、40 帧 MP4。该结果只证明“代码生成→本地 Blender 渲染”链路和证据记录可用，不代表真人视频质量，也没有声称 Seedance/Kling/VACE 结果已经改善。
