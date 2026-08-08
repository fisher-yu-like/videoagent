# ProxyVerifier 与不可变修订设计

## 目标

在 Blender Proxy 到最终视频后端之间增加一个可审计的验证与反馈边界。ProxyVerifier 不修改最终视频 Prompt；它验证真实 Proxy 是否符合 ScenePlan、PhysicalStatePlan、Character/ObjectTrajectoryPlan 和 CameraTrajectoryPlan，并将人工或 VLM 反馈路由到结构修订或外观编辑。

## 数据流

```text
DirectorPlan + WorldState + CodeAgent 文档/脚本 + Blender 输出
  -> ProxyVerifier
  -> deterministic report
  -> human approval (station) / VLM adapter (dataset)
  -> feedback category
  -> Director Revision 或 Appearance-only Edit Prompt
```

每次修订都是独立目录 `revision_000/`、`revision_001/`、`revision_002/`，不存在覆盖旧版本。修订目录保存父版本引用、反馈 JSON、输入来源哈希和下一步路由。

## 确定性检查

- provenance：WorldState、CodeAgent 脚本和 manifest 的 SHA-256 绑定。
- scene_structure：scene id、实体 id/kind、帧数、fps 和相机数量。
- physical_event：事件帧在时间轴内，参与者存在，事件参数不丢失；不能由日志证明的语义物理关系标为 `unknown`。
- character_trajectory / object_trajectory：逐关键帧比较 state log 的位置/旋转，记录最大绝对误差。
- camera_trajectory：比较相机位置、target id、target height、authored rotation 和 applied rotation 日志。
- media：逐视频检查相对路径、文件存在、字节数和 SHA-256；若提供真实 ffprobe，则额外检查帧数、时长、fps、分辨率。

确定性检查不会声称判断“表演是否自然”或“镜头是否好看”。这些由人工或 VLM 反馈完成，并且结果单独记录。

## 反馈路由

反馈类别固定为：`scene_structure`、`character_trajectory`、`object_trajectory`、`camera_trajectory`、`physical_event`、`appearance_only`。

前五类路由到 `Director Revision -> Blender Code Agent Revision -> Blender rerender`；只有 `appearance_only` 路由到 Appearance-only Edit Prompt。人工审批和 VLM 审批使用相同的 Feedback schema。

## API 边界

本阶段 ProxyVerifier 不调用 API。station 使用人工反馈；数据集批处理时通过独立 VLM Adapter 调用 GPT-5.6-luna，并保存请求、响应、模型、耗时和调用次数。VLM 不负责替代确定性哈希和轨迹检查。
