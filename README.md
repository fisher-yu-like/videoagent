# VideoActAgent

VideoActAgent 是一个面向可控视频生成的实验框架。核心思想是先把故事编译成可审计的三维状态和摄像机计划，在 Blender 中生成共享世界 Proxy，经过人工或 VLM 验收后，才把“外观替换”交给 Seedance/Kling 等视频后端。

## 整体 Pipeline

```text
Story Prompt
    ↓
Director / WorldState
    ↓
ScenePlan + PhysicalStatePlan
    + Character/ObjectTrajectoryPlan
    + CameraTrajectoryPlan
    ↓
Blender CodeAgent
    ↓
Blender Sandbox（一个共享世界、多个同步摄像机）
    ↓
Proxy MP4 + state_log + camera_log + manifest
    ↓
ProxyVerifier
    ↓
人工审批 / VLM
    ├─ 结构、轨迹、物理、机位问题 → Director/Blender 修订并生成新 revision
    └─ 通过 → Appearance-only Prompt Compiler
                    ↓
              Backend Adapter
              ├─ Seedance reference-video
              ├─ Kling reference-video
              └─ 其他显式控制后端（独立实验分支）
                    ↓
              最终视频媒体检查与人工/VLM 复核
```

Proxy 只负责空间、动作、物体关系和机位结构；它不是最终真人资产。外观问题才可以进入 Appearance-only prompt，结构问题不能用 prompt 掩盖。

## 代码分层

| 层 | 主要目录 | 职责 |
|---|---|---|
| 语义规划与旧版兼容接口 | `videoactagent/` | Director、ScenePlan、ShotScript、轨迹、旧后端和人工标注器 |
| 动态 Pipeline v2 | `pipeline_v2/` | WorldState、CodeAgent 合约、Blender sandbox、ProxyVerifier、revision、VLM、Appearance 和后端 bundle |
| 可复现实验入口 | `scripts/` | 复杂场景、多机位 Proxy、Seedance 上传/查询和审计脚本 |
| 输入与示例 | `configs/`, `stories/`, `prompts/`, `examples/` | 场景、故事、轨迹和 API 示例 |
| 前端 | `static/` | 旧版人工轨迹标注器和 Director 面板 |
| 证据与文档 | `docs/` | 架构、使用说明、实验记录和参考文献 |
| 测试 | `tests/` | schema、轨迹、渲染、Verifier、API 合约和回归测试 |

## 快速开始

环境要求：Python 3.12、Blender 5.1（默认路径 `D:\blender\blender.exe`）。

```powershell
python -m pip install -e .
python -m pytest -q tests/test_pipeline_v2_state.py tests/test_pipeline_v2_proxy_verifier.py tests/test_complex_scene_suite.py
python scripts/run_complex_scene_suite.py --scene-id plaza_dance_circle --proxy-style skeleton --skip-seedance
```

只想生成本地 Proxy 时使用 `--skip-seedance`。真实后端必须在 Proxy 通过人工/VLM 门禁后再启用；每个 camera 独立提交一个 reference-video task，不自动重试或换 seed。

Seedance/Kling 的 API 需要相应环境变量和可访问的 reference URL。上传、请求、响应、task ID、媒体元数据和 SHA-256 都会写入独立 run 目录；密钥不会写入仓库。

## 关键不变量

1. 所有摄像机共享同一个 Blender 世界和时间线，不能为每个视角分别生成不同场景。
2. 每个修订使用新的 `revision_000`, `revision_001`, … 目录，不覆盖旧证据。
3. `scene_structure`、人物/物体轨迹、机位和物理事件问题必须回到 Director/Blender Proxy。
4. 只有 `appearance_only` 才能进入最终视频编辑 prompt。
5. 自动检查只能证明 schema、轨迹、媒体和哈希；视觉创作意图必须由人工或 VLM 判断。
6. 没有真实 MP4、媒体检查和来源哈希绑定的结果不能标记为生成成功。

## 当前边界

当前仓库已经具备完整的规划、共享世界 Proxy、四机位日志、revision 闭环、Appearance bundle 和 Seedance/Kling 适配器。Procedural canonical/skeleton Proxy 用于结构预演，不等同于真人资产；VACE、ATI、ReCamMaster、CamTrol 等属于后续独立控制分支，不会被假装成当前主线已经可用。

最新真实实验和失败原因见：

- [`docs/PIPELINE_OVERVIEW.md`](docs/PIPELINE_OVERVIEW.md)
- [`docs/CURRENT_PIPELINE_AND_RESULTS.md`](docs/CURRENT_PIPELINE_AND_RESULTS.md)
- [`docs/PROXY_REVISION_009_012_RUN.md`](docs/PROXY_REVISION_009_012_RUN.md)
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- [`docs/USAGE.md`](docs/USAGE.md)
- [`docs/STORYBLENDER_ADAPTATION.md`](docs/STORYBLENDER_ADAPTATION.md)

## 版本控制范围

仓库只提交源码、配置、测试、示例和文档。`runs/`、MP4、Blend、模型权重、缓存、临时上传和本地密钥均被 `.gitignore` 排除；真实实验通过文档中的路径、manifest 和哈希复现，而不是把大文件塞进 Git。
