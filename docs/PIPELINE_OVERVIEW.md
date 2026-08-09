# VideoActAgent Pipeline Overview

## 1. Director 层

输入自然语言 story prompt，输出可审计的 `ScenePlan`、`PhysicalStatePlan`、人物/物体轨迹和摄像机轨迹。LLM 只负责语义规划，不直接决定未约束的 Blender 几何。

核心对象：

- `WorldState`：统一时间线、FPS、实体和所有轨迹。
- `scene_layout.json`：空间关系、摄像机职责和目标对象。
- `gesture_tracks.json` / `motion_tracks.json`：可复核的动作侧车文件。

## 2. Blender Proxy 层

`pipeline_v2/code_agent.py` 把 WorldState 合约传给本地 Blender CodeAgent；`pipeline_v2/blender_sandbox.py` 在隔离 Blender 中执行脚本。复杂场景入口使用 `scripts/run_complex_scene_suite.py`。

所有机位从同一个 `.blend` 世界渲染，只改变 active camera。输出包括：

- 每个 camera 一个 MP4；
- `state_log.json`、`camera_log.json`、`asset_log.json`；
- `render_manifest.json`（帧数、FPS、分辨率、文件哈希）；
- `skeleton_pose_log.json` 或 `motion_log.json`（如果启用对应 Proxy profile）。

## 3. Proxy 验收与修订

`pipeline_v2/proxy_verifier.py` 检查 schema、轨迹、物理事件引用、camera target、媒体元数据和 SHA-256。视觉意图不由确定性检查推断：人工或 `pipeline_v2/vlm_feedback.py` 必须明确给出 `approve` 或 `revision_requested`。

反馈路由：

```text
scene_structure / character_trajectory / object_trajectory
camera_trajectory / physical_event
    → Director revision → Blender revision → 新 Proxy

appearance_only
    → Appearance-only Prompt Compiler
```

每次修订写入新的 run/revision 目录，不覆盖历史结果。

## 4. 外观与后端层

`pipeline_v2/appearance_prompt.py` 只编译外观、材质、灯光和真人化要求，并绑定通过审批的 WorldState/manifest 哈希。`pipeline_v2/backend_adapter.py` 负责把 bundle 转换为后端输入，不自动提交。

Seedance/Kling reference-video 采用“一台 camera 一个 task、一个 reference URL”的规则。API 证据必须保存 request、response、task ID、调用次数、下载 MP4、ffprobe 元数据和 SHA-256。失败不能通过自动重试或换 seed 搜索结果。

## 5. 评估边界

最终视频检查分为媒体完整性、轨迹/摄像机一致性、人物和物体身份、物理事件以及人工/VLM 视觉判断。当前 procedural Proxy 的目标是结构预演，不代表真人外观；ATI、ReCamMaster、CamTrol、VACE 等模型只作为后续独立控制分支接入。

## 6. 2026-08-10 全链路实测补充

一条真实 endpoint run 已完成：批准的共享世界 Proxy 经 Uguu 上传后，按四个
`camera_id` 分别调用 `Doubao-Seedance-2.0`，4 个任务全部下载并通过媒体完整性检查。
最终 VLM 发现四个独立任务的人物身份、服装和场景布局发生漂移，因此“独立 task
链路跑通”与“多视角一致性通过”必须分开记录。当前 pipeline 的后端 gate 仍保留：
只有 Proxy approve 才能进入 Appearance-only 和 Seedance；最终 VLM 不通过则结果标记
为 `revision_requested`，不自动再次生成。
