# StoryBlender 方法调研与本项目融合方案

更新时间：2026-08-09

参考：

- [StoryBlender project page](https://engineeringai-lab.github.io/StoryBlender/)
- [StoryBlender paper](https://arxiv.org/abs/2604.03315)
- [StoryBlender code](https://github.com/EngineeringAI-LAB/StoryBlender)
- [CineBoard3D dataset](https://huggingface.co/datasets/EngineeringAI-LAB/CineBoard3D)

## 1. StoryBlender 实际做法

StoryBlender 的核心不是“让 LLM 一次生成一段 Blender Python”，而是把故事拆成可持久化、可验证、可编辑的 3D 制作阶段。论文把方法概括为三层：

1. **Semantic-Spatial Grounding**：Director 把故事写成 continuity memory graph，分离全局资产、场景布局和每个 shot 的变量；
2. **Canonical Asset Materialization**：为每个实体检索或生成规范 3D 资产，统一尺度、坐标轴、朝向和身份；
3. **Spatial-Temporal Dynamics**：在 Blender 中完成空间布局、灯光、环境、动作和摄像机，并用引擎状态做物理/空间反馈。

官方仓库的 UI 是 Blender 插件 + Gradio，多阶段包括：Director、核心资产、核心布局、补充资产、补充布局、灯光、环境、跨镜头资产修改、绑定与动画、摄像机 blocking、后处理、渲染和视频拼接。它不是把每个镜头独立生成后再拼接。

关键闭环是：

```text
Agent 生成阶段结果
  ↓
Blender 执行/渲染
  ↓
VLM 审美与语义检查 + 引擎几何检查
  ↓
阶段通过，或只修改当前阶段
```

论文还指出，人物动画不是仅靠 root 轨迹，而是检索/重定向单人物动画序列；没有 Visual Effects/Animation 阶段时，布局虽存在，动作和氛围会明显退化。

## 2. 与当前项目的差异

### 已经具备

- `WorldState`：相当于连续性世界状态；
- `asset_registry.json`：已经有 canonical asset registry；
- `scene_layout.json`：保存空间关系和摄像机责任；
- 共享 Blender World 和多机位完整时间线；
- MCP + CLI 双通道；
- ProxyVerifier 和全部机位 VLM 审核；
- revision 不覆盖旧版本。

### 当前缺口

- canonical 人物仍是低模程序化人体，不是标准绑定人体资产；
- 目前只有上臂 gesture track，缺少腿部步态、IK、脚掌接触和 foot-skate 检查；
- 没有独立的资产格式化阶段，当前只记录轴向，尚未通过多视图自动确认正面/顶部/尺度；
- VLM 主要在 Proxy 总体完成后审核，尚未对资产、布局、动作、摄像机逐阶段反思；
- Seedance 独立任务会各自重建环境，不能假设它们保持共享 3D 世界一致性。

## 3. 可融合的最小方案

不直接安装 StoryBlender 插件：其 README 建议 Blender 4.5 LTS，并提示 5.0+ 存在兼容问题；当前项目使用 Blender 5.1。我们只借鉴其数据组织和阶段闭环。

### Stage A：Canonical Human Adapter

在现有 `asset_registry` 下增加 `human_asset_version`：

- `procedural_humanoid_v3`：更标准的人体比例、肩/髋关节、手掌、脚掌和朝向标记；
- `rigged_glb`：未来可接入本地 Mixamo/MB-Lab/MakeHuman/StoryBlender 检索得到的绑定资产；
- 资产必须通过统一尺度、前向轴、顶部轴和多视图 still 检查；
- 资产替换不能改变原始人物/物体/摄像机轨迹。

### Stage B：Motion Adapter

保持现有 `CharacterTrajectoryPlan` 作为 root 轨迹，在其下增加局部动作层：

```text
root trajectory
  + limb tracks
  + foot contacts
  + retargeted walk/dance clip
  + facing/look-at constraint
```

第一版不训练模型，只支持：

- 左右臂关键帧；
- 左右腿交替步态；
- 脚掌接触地面的关键帧；
- root 轨迹对动作片段的平移/旋转重定向。

### Stage C：分阶段反思

把一次 VLM Proxy 审核拆成低成本门禁：

1. Asset：是否标准人形、部件是否齐全、朝向是否正确；
2. Layout：人物/物体关系、遮挡、落地和尺度；
3. Motion：动作顺序、脚接触、是否滑步、人物是否穿模；
4. Camera：每个 camera_id 的目标、覆盖、黑帧和关键动作可见性；
5. Final Proxy：只有全部通过才进入 Appearance-only Prompt。

每一阶段失败只回到对应 Agent，不修改最终视频 prompt。

### Stage D：真实视频后端

继续使用当前的独立机位 Seedance 方式：每个 camera_id 一个 task、一个 reference URL。四个独立结果必须分别做媒体检查和最终人工/VLM 检查；不能把四个独立结果误称为天然一致的多视角视频。

## 4. 实现边界

第一轮只做 Stage A+B 的本地 Proxy，不调用 Seedance：

1. 生成 `revision_006` 标准人体 Proxy；
2. 保留当前四个 camera plan 和所有实体轨迹；
3. 增加腿部关键帧和脚接触 log；
4. MCP 检查 reverse 机位和关键动作帧；
5. CLI 渲染四机位；
6. VLM 只调用一次审核全部机位；
7. 通过后再决定是否重跑四个 Seedance task。

## 5. 预期收益与限制

预期收益：人物形体更标准、动作顺序更可读、腿部不再完全静止、Proxy 更接近 StoryBlender 的可编辑 3D storyboard。

限制：没有真实绑定资产或动作库时，程序化人体仍是预演代理，不会自动变成真人；Seedance 独立任务的环境一致性仍需要额外的 identity/environment reference 或后续视频一致性模型解决。

## 6. 已实施的本地适配与真实结果

已实施：

- `canonical_motion_profile_for()` 将人物 root 轨迹编译为左右腿交替轨迹和脚接触关键帧；
- Blender 脚本消费 sidecar 并写出 `motion_log.json`；
- `verify_motion_materialization()` 在 ProxyVerifier 中检查 sidecar 是否真的被 Blender 消费；
- `storyblender_revision()` 做布局/动作反思：保留实体 root/object 轨迹，放宽镜头、降低 elevated 俯视程度、增强已有 gesture 阶段；
- VLM 审核严格要求真实非负整数帧号，非法证据保持失败，不会被当成通过。

2026-08-09 的四次本地真实渲染均生成了四个 MP4 和共享 `.blend`，媒体检查通过。`revision_007` 新增了左右手臂内收限制和 `arm_pose_log.json`，确定性 `character.arm_head_clearance` 检查通过，最新 VLM 未再报告胳膊穿头；但 VLM 仍要求继续修订，因为程序化人体仍像低模 storyboard proxy，舞蹈/挥手阶段不够明显，speaker 与人物有重叠，passerby 穿越不清晰，镜头责任区分不足。因此当前不能把它送入 Seedance，也不能声称已经复现 StoryBlender 的 retargeted animation 效果。

下一步应接入真正的 rigged GLB/动作重定向（或把动作计划简化到可读的少数姿态），完成 Asset → Layout → Motion → Camera 的分阶段验收后再生成真实视频。

## 7. procedural_skeleton_v1 实验

`revision_008` 已接入独立的程序化骨骼分支：每个角色拥有 Armature、手/脚 IK target、pole target 和显式骨骼轨迹；两段 IK 数学保证目标不可达时进行有限范围裁剪，避免 NaN 和关节爆炸。真实四机位渲染中，骨骼日志与 IK 可达性检查均通过，VLM 未再报告胳膊穿头。

但 VLM 仍拒绝整体 Proxy，说明骨骼层解决的是局部运动学问题，不会自动解决导演层的镜头覆盖、人物间距和动作节拍。下一步应在保持 `skeleton_motion.json` 合同不变的情况下修订 Camera/Layout，而不是退回 Appearance-only prompt。
## 8. revision_009–revision_012 真实复核

本轮把 StoryBlender 风格的“布局/资产/动作分层”继续落到 procedural skeleton/IK 分支，并保留共享世界和四个 CameraTrajectoryPlan。真实 VLM 仍连续指出动作 silhouettes、speaker/backpack 辨识、角色间距和后/高位机位可读性不足；因此当前结果仍是结构 Proxy，不是可直接进入 Seedance 的合格 Proxy。完整目录和计数见 `docs/PROXY_REVISION_009_012_RUN.md`。
