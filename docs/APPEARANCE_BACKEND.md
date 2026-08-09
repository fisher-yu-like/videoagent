# Appearance-only Prompt 与 Backend Adapter

## 目的

这一层对应 VideoCoCo 的 `clay proxy + edit_prompt -> photoreal video` 思路。它位于 ProxyVerifier 和真实视频后端之间，职责是把“结构已经批准，只改变外观”的意图变成可审计输入。

它不会重新规划人物、物体或摄像机，也不会把外观文字当成轨迹控制。

## Appearance-only Prompt Compiler

入口是 `pipeline_v2.appearance_prompt.compile_appearance_prompt`，输入：

- 已验证的 `WorldState`；
- Blender 真实渲染的 `render_manifest.json`；
- 人工或后续 VLM 确认的 appearance profile。

profile 必须覆盖 WorldState 中的每个实体，只包含：人物/物体外观、环境外观、灯光、真实感质量和外观伪影黑名单。出现 `camera`、`trajectory`、`move`、`walk`、`zoom`、`cut` 等运动或镜头指令会被拒绝。

编译器输出：

- `appearance-only-prompt-1.0`；
- 完整英文 edit prompt；
- WorldState hash、Proxy manifest hash、profile hash 和 prompt SHA-256；
- 明确声明 approved proxy 负责 blocking、timing、occlusion、identity 和 camera movement。

这保留了 VideoCoCo 的 appearance edit 边界，同时加入了我们多机位共享世界的来源绑定。

## Backend Adapter

入口是 `pipeline_v2.backend_adapter.prepare_backend_adapter`。它只准备 bundle，不发起网络请求：

| 后端 | conditioning mode | 当前行为 |
|---|---|---|
| `vace` | `source_video_edit` | 当前 blocked；本项目没有启用可接收 Proxy 的 VACE adapter |
| `omniweaving` | `source_video_edit` | 当前 blocked；不能把 VideoCoCo 的推理脚本误称为本地可用后端 |
| `seedance_reference` | `reference_video` | 当前唯一已实现的 Seedance Proxy candidate；必须提供公开 HTTPS URL，先做单次 probe |
| `kling_reference` | `reference_video` | 当前唯一已实现的 Kling Proxy candidate；请求形状仍需网关 probe 验证 |
| `seedance_t2v` | `prompt_only` | 明确记录没有消费 Proxy，不能宣称有轨迹控制 |
| `kling_t2v` | `prompt_only` | 同上 |

每个 bundle 保存：

- 3 路 camera 视频、字节数和 SHA-256；
- WorldState/Proxy manifest/Appearance prompt 的哈希；
- camera_id、conditioning mode、请求 payload；
- `network_called=false` 和 `automatic_retry_limit=0`。

## 当前真实 station 产物

当前使用 `revision_004/blender_run_retry_001` 的真实 3 机位 Proxy，生成在：

`pipeline_v2/runs/module3_station_20260808_019/revision_004/backend_adapter_bundles_002/`

VACE 和 OmniWeaving bundle 会明确标记为 blocked；Seedance/Kling reference 只有在提供公开 Proxy URL 后进入 `ready_for_single_probe`，没有 URL 时保持 blocked；Kling/Seedance text-only 仅作为明确标注的 prompt-only 输入。此阶段 API 调用数为 0，未使用服务器或 A100。

## 与 VideoCoCo 的关系

借鉴的是 planner → standalone Blender → clay audit → appearance edit 的数据流，不复制其 OmniWeaving 权重或单镜头假设。我们的新增约束是同一个 WorldState 驱动多台摄像机，并让每一路后端输入绑定同一份人物/物体轨迹、相机日志和 Proxy 哈希。
