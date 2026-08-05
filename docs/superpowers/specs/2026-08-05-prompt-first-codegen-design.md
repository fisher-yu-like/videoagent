# Prompt-first Blender 视频生成设计

## 目标

新增一个极简用户入口：用户只输入自然语言 Prompt，系统依次调用两个 Agent 生成 ShotScript 和 Blender Python，使用本地 Blender 真实渲染，页面只展示状态和最终 MP4。

现有单机位标注器、三机位/向导页面，以及当前 Codegen Lab 的多输入调试接口全部保留，不修改其端口和数据协议。

## 用户体验

页面只有：

1. 一个 Prompt 多行输入框；
2. 一个“生成视频”按钮；
3. 简单状态文字：规划中、代码生成中、Blender 渲染中、成功或失败；
4. 成功后的 MP4 播放器。

页面不要求用户填写 ShotScript、轨迹、工作区、FPS、分辨率或模型参数，也不展示中间 JSON 和 Python。中间证据仍保存在本地作业目录，供实验复现和排错。

## 后端数据流

```text
POST /api/prompt-run {prompt}
        ↓
Agent 1：DeepSeek-v4-pro 生成 ScenePlan
        ↓ ScenePlanDraft schema 校验
确定性编译：ScenePlan → ShotScript + actor-only K0–K4 trajectory
        ↓
Agent 2：DeepSeek-v4-pro 生成 build_scene(context)
        ↓ AST 安全门
D:\blender\blender.exe Smoke 渲染
        ↓ MP4/manifest/轨迹验证
GET /api/prompt-status?operation=<id>
```

Agent 1 复用现有 `ScenePlanDraft` 校验和 `to_shotscript()` / `to_trajectory()` 编译器。代码生成阶段继续复用 `request_blender_code()`、`validate_generated_code()`、`run_codegen_blender()` 和 `verify_codegen_render()`。轨迹只保留人物轨迹给当前 Codegen contract；物体和镜头仍写入 ShotScript，供 Blender Agent 读取。

## 模型与重试

- 两个 Agent 默认均使用 `deepseek-v4-pro`；现有向导使用的 `deepseek-v4-flash` 默认行为不变。
- 每个阶段最多 3 次尝试：第一次真实调用，失败后最多 2 次修复重试。
- 每次尝试保存独立的 request/response/evidence，API 调用次数按真实请求累计。
- Planner schema 错误：把校验错误放进下一次 planner repair payload。
- Blender 代码 AST 错误：把安全门错误放进下一次 codegen repair payload。
- Blender 进程或视频验证失败：读取真实 `render.log` 和验证错误，把摘要放进下一次 codegen repair payload。
- 网络、截断、非法 JSON 可以重试；不会换 seed、不会搜索结果、不会伪造成功。
- 3 次尝试仍失败时，页面显示失败原因，作业标记为终态，保留全部证据。

## 作业目录

每次 Prompt 生成一个独立的 `PF<n>` 目录：

```text
runs/work/codegen_blender_v1/PF<n>/
├── prompt.txt
├── plan/attempt-1/{request.json,response.json,draft.json,shotscript.json,trajectory.json,evidence.json}
├── plan/attempt-2/...
├── codegen/attempt-1/{request.json,response.json,generated_scene.py,evidence.json}
├── codegen/attempt-2/...
├── renders/smoke/{video.mp4,scene.blend,frames/,codegen_manifest.json}
└── job.json
```

`job.json` 记录当前状态、模型、真实 API 调用数、重试数、每次错误、最终代码哈希、Blender 版本和视频产物哈希。密钥永不写入文件或 HTTP 响应。

## HTTP 接口

新增接口：

- `POST /api/prompt-run`：只接收非空 `prompt`，返回 `202` 和 `operation_id`；不接受用户覆盖模型、路径或渲染参数。
- `GET /api/prompt-status?operation=<id>`：返回状态、阶段、错误摘要和成功后的 MP4 artifact URL；不返回完整 ShotScript、代码或密钥。

现有接口保持兼容：`/api/prepare`、`/api/generate`、`/api/render`、`/api/status`、`/api/artifact` 继续服务调试页和既有作业。

## 配置边界

CLI 仍接收 `--workspace`、`--blender` 和 `--port`。Prompt 页面从服务器配置派生保护工作区、固定 5 秒时长、8 FPS、640×360 Smoke profile。若保护工作区或 8770 guard 不可用，作业在本地准备阶段失败并给出明确原因；用户不需要在页面填写这些字段。

## 错误与安全

- 所有 Agent 响应先做严格 JSON/schema 校验，再进入下一步。
- Blender Python 继续经过 AST 安全门，禁止文件、网络、进程、环境变量和渲染控制越权。
- 作业锁防止同一操作重复提交。
- 失败不返回视频链接；只有真实 MP4、manifest 和视频验证成功后才显示播放器。
- UI 不自动重复点击；重试由后端按明确次数执行并在状态中可见。

## 验收标准

- 页面只显示 Prompt 输入、生成按钮、状态和最终视频。
- 一次完整成功作业包含两个 Agent 阶段和一次真实 Blender 渲染。
- Agent 失败最多自动重试两次，每次都有真实 evidence；成功时能看到实际 MP4。
- 现有 8769、8770 页面和旧 Codegen 调试接口测试继续通过。
- 单元/HTTP 测试使用注入的真实流程边界，不伪造成功视频；至少一条手动验收使用用户点击触发的真实 DeepSeek-v4-pro 和 Blender。
