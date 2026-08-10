# VideoActAgent 当前 Pipeline 与实验结果

更新时间：2026-08-10

这份文档是当前实现的单一事实来源。后续任务开始前，先读取本文件，再读取与任务匹配的 skill；如果代码、旧报告和本文件冲突，以最新真实 run 目录中的 manifest、日志和报告为准。

## 1. 当前状态

- 已完成：Prompt → Director/WorldState → Blender CodeAgent → 共享世界四机位 Proxy → ProxyVerifier → VLM 审核 → Appearance-only Prompt/Backend bundle。
- 最新已通过 Proxy：`revision_005`，VLM 已通过；最新结构修订实验为 `revision_007`，VLM 仍为 `revision_requested`。
- Seedance 2.0：已完成一条真实独立机位 endpoint run；4 个 task 均下载成功，但最终 VLM 判定跨机位人物/场景不一致。
- 当前最新 Proxy 主线：`revision_024` 已接入 ACCAD BVH，但 VLM 仍拒绝动作可读性；后端 endpoint 使用历史已批准的 `revision_005`，不绕过当前 Proxy gate。
- 尚未完成：Seedance 多视角一致性修复、Kling 对照、VACE 控制评估、24 条正式矩阵、ATI/ReCamMaster/CamTrol 对照。
- 当前结果可表述为“真实 Seedance 视频生成和媒体链路完成”，不能表述为“多视角一致性通过”。

## 2. 当前端到端结构

```text
Story Prompt
  ↓
Director Scene Spec
  ├─ ScenePlan / PhysicalStatePlan
  ├─ CharacterTrajectoryPlan / ObjectTrajectoryPlan
  └─ CameraTrajectoryPlan
  ↓
WorldState（单一时间线、共享世界）
  ↓
asset_registry.json + scene_layout.json + gesture_tracks.json + motion_tracks.json
  ↓
Codex 本地 Blender CodeAgent
  ↓
standalone Blender Python
  ↓
Blender MCP：检查共享世界、关键帧、机位和局部修复
  ↓
Blender CLI：最终确定性渲染
  ↓
四机位 Proxy MP4 + state_log + camera_log + asset_log + manifest
  ↓
ProxyVerifier（结构/轨迹/媒体/哈希/黑帧）
  ↓
人工审核或 VLM（全部机位 K0-K4）
  ├─ revision_requested → Director/CodeAgent 修订并生成新 revision
  └─ approve
       ↓
Appearance-only Prompt Compiler
       ↓
Backend Adapter
  ├─ Seedance reference-video
  ├─ Kling reference-video
  ├─ Seedance/Kling T2V baseline（不消费 Proxy）
  └─ VACE/OmniWeaving（当前未开放真实后端）
       ↓
真实视频媒体检查 + 人工/VLM 最终审核
```

## 3. 模块职责与真实实现

### 3.1 Director 与 WorldState

当前复杂场景入口是 `scripts/run_complex_scene_suite.py`，场景定义在 `videoactagent/complex_scene_prompts_v2.py`。每个场景显式保存：

- 角色、物体和环境实体；
- K0-K4 动作阶段和物理事件；
- 人物/物体轨迹关键帧；
- 摄像机位置、目标、镜头和 roll；
- 物体耦合关系和必须保持的约束。

`compile_scene_world()` 将这些输入转换为现有 `pipeline_v2` 的 `WorldState`，不改变既有 schema。所有机位共享同一个 WorldState 和同一条完整时间线。

### 3.2 Asset Registry 与 Proxy

`asset_registry.json` 是实体到 Blender 资产的映射，不替换轨迹协议：

- `clay`：旧的中性低模 Proxy；
- `canonical`：分段人体代理，至少包含头、躯干、骨盆、上下臂、手、上下腿和脚；
- `--asset-dir`：可选接入 `<entity_id>.glb`，当前没有强制要求真实资产。

同一实体只在共享世界中实例化一次，四个摄像机都读取该实例，避免多视角身份漂移。

### 3.3 Blender CodeAgent、MCP 与 CLI

当前 CodeAgent 是 Codex 本地执行的代码生成与修订流程，不调用托管 LLM 直接生成 Blender 代码。它生成可审计的 standalone Python，并保存脚本哈希。

- Blender MCP：适合读取场景、检查对象层级、渲染单帧、定位黑屏/遮挡和执行小修复；
- Blender CLI：使用 `D:\blender\blender.exe` 做最终全时间线渲染；
- MCP 连接成功不等于最终渲染成功，必须检查实际 MP4、ffprobe 和 manifest。

### 3.4 ProxyVerifier

`pipeline_v2/proxy_verifier.py` 检查：

- WorldState、DirectorPlan、生成脚本和 manifest 哈希；
- 实体数量和每帧 state log；
- 人物轨迹、物体轨迹、摄像机目标/位置/朝向；
- 视频文件、帧数、时长、分辨率、FPS 和 SHA-256；
- 四个机位是否出现真实黑帧；
- canonical 资产是否存在一次且部件数量足够。

确定性检查通过仍不代表创意意图可读，必须经过人工或 VLM 审核。

### 3.5 VLM 审核与修订路由

VLM 每次只调用一次，读取所有机位的独立 K0-K4 帧，不只看 contact sheet。反馈必须归类为：

- `scene_structure`；
- `character_trajectory`；
- `object_trajectory`；
- `camera_trajectory`；
- `physical_event`；
- `appearance_only`。

前五类回到 Director/Blender CodeAgent，创建新的 `revision_00N`，不覆盖旧版本。只有 `appearance_only` 才能进入 Appearance-only Prompt Compiler。

### 3.6 Appearance-only Prompt 与后端

`pipeline_v2/appearance_prompt.py` 只允许描述人物/物体外观、材质、光照和真实感，禁止新增动作、轨迹、镜头、剪辑或事件。它绑定：

- WorldState hash；
- Proxy manifest hash；
- appearance profile hash；
- compiled prompt SHA-256。

`pipeline_v2/backend_adapter.py` 只准备请求 bundle，不隐藏网络调用，也不自动重试。

### 3.7 Seedance 上传和请求

当前生产建议使用 TOS。研究临时上传后端支持：

- `tmpfiles`：旧兼容路径，本机当前 TLS 连接失败；
- `0x0`：代码保留，但本次实测返回 503；
- `uguu`：本轮真实上传和远端 HEAD 检查成功。

当前标准是每个 manifest camera 独立提交：每个 task 只带一个 reference-video URL，并按 `camera_id` 保存 request、task ID、查询记录和最终 MP4。这样不依赖网关是否接受多个 video_url。四个机位都可以提交；elevated 不再因为 API 请求形状而被静默丢弃。Seedance reference-video 才消费 Proxy，T2V 只消费文本，不能宣称 T2V 具有轨迹控制。

## 4. 最新实验结果

### 4.1 VLM 拒绝的 revision_004

目录：`runs/results/complex_scene_suite_20260809_112616/`

真实 VLM 反馈：

- 舞者从左到中、两段 side-step 和转身不够可读；
- 三个人外观相似，角色关系不清；
- speaker 看起来像粘在人物身上，没有明确落地；
- 机位过紧或遮挡，不能同时验证人物、speaker 和穿越路人。

处理方式：不修改最终视频 prompt，而是回到 Director/CodeAgent，生成 `revision_005`。

### 4.2 最新 Proxy revision_005

目录：[complex_scene_suite_20260809_114719](../runs/results/complex_scene_suite_20260809_114719/)

真实结果：

| 项目 | 结果 |
|---|---|
| 共享世界 | 1 个 `.blend`，4 个摄像机 |
| 视频 | 4 个 MP4，640×360，24 FPS，120 帧，5 秒 |
| 轨迹/相机/资产/哈希 | 通过 |
| 黑帧检测 | 4 个机位均无黑帧事件 |
| VLM | 1 次调用，`approve` |
| Seedance API | 本次 Proxy 审核 run 未调用 |

VLM 明确确认：蓝色 lead dancer 与背包、绿色 musician 与落地 speaker、棕色 passerby、bench 和四机位覆盖均可读，没有明显 teleportation 或身份漂移。

可查看：

- [Proxy 总结](../runs/results/complex_scene_suite_20260809_114719/plaza_dance_circle_20260809_114719/scene_summary.json)
- [VLM 审核](../runs/results/complex_scene_suite_20260809_114719/plaza_dance_circle_20260809_114719/proxy_vlm_review/feedback.json)
- [master MP4](../runs/results/complex_scene_suite_20260809_114719/plaza_dance_circle_20260809_114719/sandbox/master.mp4)
- [elevated MP4](../runs/results/complex_scene_suite_20260809_114719/plaza_dance_circle_20260809_114719/sandbox/elevated.mp4)

### 4.3 Seedance 2.0 当前任务

目录：[seedance_canonical_revision_005_20260809](../runs/results/seedance_canonical_revision_005_20260809/)

| 项目 | 结果 |
|---|---|
| 模型 | `Doubao-Seedance-2.0` |
| 参考视频 | 3 个，来自 revision_005 的共享世界 Proxy；这是修改前的历史 multi-video request |
| 上传 | Uguu，3 个 URL 均返回 `200 video/mp4` |
| task ID | `task-d8z2wbm0ihb5b1d` |
| submit | 1 |
| query | 15 |
| download | 0 |
| 当前状态 | `running` |

因此当前没有可报告的 Seedance 最终视频质量结论，也没有进行最终视频 VLM 审核。该任务不重提；后续新实验使用每机位独立 task。

代码层面的新入口已经切换为 `videoactagent.seedance_auto_chain.run_uploaded_camera_jobs()`：每个 camera_id 创建独立目录和独立请求。当前尚未为这个新请求形状再次提交模型任务，避免重复消耗 API；下一次 Seedance 实验才会使用该入口。

## 5. 当前测试证据

- 定向编译检查通过；
- Proxy、Appearance、VLM、FinalVerifier、Seedance upload 和独立机位提交测试共 `37 passed`；
- 完整仓库测试没有作为通过依据：其中存在未安装 `vace` 的第三方测试收集问题，不能把它写成全仓库通过。

## 6. 后续执行规则

1. 任务开始先读取本文件；
2. 再读取匹配的三个 VideoActAgent skill；
3. 先确认当前最新 revision、manifest、VLM 状态和 API 任务状态；
4. 结构问题回 Proxy，不在最终视频 prompt 中修复；
5. 每次 API 提交前报告模型、URL 数量、submit/query/download 预算；
6. 不自动重试、不换 seed 搜索结果、不用伪造数据替代真实视频；
7. 只有实际下载 MP4 并通过媒体检查后，才能报告 Seedance/Kling 生成完成。

## 7. 本次修正记录

| 问题 | 错误做法 | 修正做法 |
|---|---|---|
| Seedance 参考视频数量 | 将多个机位 URL 放进一个 request，或只上传 master | 每个 camera_id 独立上传、独立 submit、独立 query、独立 download |
| VLM 只看部分机位 | 只抽取 manifest 前两个视频 | 对 manifest 中全部 camera 抽取 K0-K4 |
| VLM 结构拒绝 | 修改最终真实化 prompt | 回到 Director/CodeAgent，生成新 revision |
| Blender 黑屏判断 | 用过高亮度阈值把深色 clay 当黑屏 | 用真实 ffmpeg blackdetect，并用 MCP 渲染受影响机位关键帧复核 |
| 上传失败 | 把临时托管失败当成模型失败 | 单独记录 provider、URL、远端 HEAD 和 API 计数；TOS 优先，Uguu 仅研究使用 |
| 多机位请求 | 依赖一个 request 接受多个 video_url | `run_uploaded_camera_jobs()` 为每个机位创建独立 task，汇总结果但不合并请求 |

### 4.4 独立机位 Seedance 实验

结果目录：[seedance_independent_revision_005_20260809](../runs/results/seedance_independent_revision_005_20260809/)

本次使用 revision_005 的四个真实 Proxy，并按 camera_id 独立提交：

| camera_id | task ID | submit | download | 媒体结果 |
|---|---|---:|---:|---|
| master | `task-rj9271mnz8jdvd9` | 1 | 1 | 1280×720，121 帧，5.086 秒，无黑帧 |
| lateral | `task-7meeutac18s159x` | 1 | 1 | 1280×720，121 帧，5.086 秒，无黑帧 |
| reverse | `task-12sbs8rdzkcgn3d` | 1 | 1 | 1280×720，121 帧，5.062 秒，无黑帧 |
| elevated | `task-rf1paoqzpi7losc` | 1 | 1 | 1280×720，121 帧，5.086 秒，无黑帧 |

总计：上传 4、submit 4、query 94、download 4。媒体层面全部通过；视觉审核仍为 `pending_review`。

重要观察：四个独立 Seedance 任务虽然都生成了真人视频，但跨机位的环境和构图一致性没有保持好。master 更接近室内/墙面场景，lateral、reverse、elevated 的环境和景别出现明显差异。因此这轮只能证明“每机位独立提交和真实下载链路跑通”，不能证明 Seedance 独立任务天然保持多视角共享世界一致性。

### 4.5 当前人体 Proxy 的动作限制

当前 canonical 人体不是标准人体资产，而是由头、躯干、骨盆、上下臂、手、上下腿和脚组成的低模结构代理。动作精细化目前覆盖：

- root 的整体位置和朝向；
- 左右上臂的 `gesture_tracks`，前臂和手通过父子层级跟随；
- `motion_tracks.json` 中的左右腿交替关键帧和脚接触标记，Blender 输出 `motion_log.json` 并由 `verify_motion_materialization()` 复核。

尚未覆盖真实绑定骨骼、膝盖 IK、脚掌锁定、落脚事件求解和 foot-skate 几何检查。因此不能把当前 Proxy 描述为完整的人体动作控制。第三机位暴露出的胳膊穿头、非标准人形和动作不可读问题应回到 Proxy/CodeAgent 修复，而不是通过 Appearance-only prompt 掩盖。

StoryBlender 的调研和兼容融合方案见：[STORYBLENDER_ADAPTATION.md](STORYBLENDER_ADAPTATION.md)。下一轮若修复人体 Proxy，目标是 `revision_006` 的标准人体比例、腿部步态和脚接触，而不是重新修改 Seedance appearance prompt。

本轮 StoryBlender 风格真实实验记录见：[STORYBLENDER_RUNS_20260809.md](STORYBLENDER_RUNS_20260809.md)。当前 `revision_006` 及后续动作反思均被 VLM 标记为 `revision_requested`，所以没有进入 Seedance。

本轮代码改动：`canonical_motion_profile_for()` 生成 `motion_tracks.json`，Blender 写出 `motion_log.json`，`verify_motion_materialization()` 检查真实消费情况；`storyblender_revision()` 只做布局/动作反思，不改变 pipeline_v2 schema。VLM 提示词现在要求帧号必须是非负整数，不确定时写空数组；非法输出保持 `vlm_failed`，不转换为通过。

### 4.9 procedural_skeleton_v1 / revision_008

在 `revision_007` 的手臂安全约束之上，新增独立 `skeleton` Proxy 分支：

- `skeleton_motion.json` 为每个角色保存 root、pelvis、spine、head、上臂/前臂/手、上腿/小腿/脚的五个语义关键帧；
- Blender 为每个角色创建一个 Armature、手/脚 IK targets 和 pole targets，并使用两段 IK 求解器生成逐帧关节姿态；
- 生成 `skeleton_pose_log.json`，记录 120 帧中所有骨骼位置和 hand/foot IK 可达性；
- `verify_skeleton_materialization()` 检查骨骼身份、帧覆盖、有限坐标和 IK 可达性；
- 旧 `clay`、`canonical` 分支不变，`WorldState`、物体轨迹和四个摄像机轨迹不变。

真实运行记录见：[SKELETON_PROXY_RUN_20260809.md](SKELETON_PROXY_RUN_20260809.md)。四机位媒体和骨骼确定性检查通过；一次 VLM 审核仍为 `revision_requested`，问题转移为镜头覆盖、人物聚集和动作节拍不可读。因此 `revision_008` 尚未进入 Appearance-only 或 Seedance。

本轮定向测试：`39 passed`；Seedance/Kling API：0 次。

### 4.8 胳膊穿头修复（revision_007）

问题原因：旧程序化人体把上臂中心当作旋转原点，并在挥臂时额外抬高上臂中心；大角度姿态会把上臂/前臂链送入头部区域。该问题属于 Proxy/CodeAgent 结构错误，不属于 Appearance-only。

已实施：

- 新增 `safe_arm_angle()`，左右手臂分别限制向身体内侧的角度，保留向外的动作幅度；
- 删除错误的 `arm.location.z` 动态抬升，保持上臂中心在肩部高度；
- Blender 写出真实 `arm_pose_log.json`，记录每条手臂轨迹的角度范围和最小肘部安全距离；
- `verify_arm_collision_constraints()` 要求每条实际应用的手臂轨迹肘部安全距离至少为 `0.12m`；
- 新增回归测试，防止以后再次加入“抬高手臂中心”或取消内收约束。

真实结果目录：[revision_007 run](../runs/results/complex_scene_suite_20260809_132708/plaza_dance_circle_20260809_132708/)。四机位媒体检查通过，`character.arm_head_clearance` 通过，VLM 仍指出动作时序、speaker 落地可读性和机位遮挡问题。因此该 revision 仍不能进入 Seedance。
### 4.10 revision_009–revision_012 Proxy 复核（2026-08-09）

本轮在当前主线本地整合了 procedural skeleton/IK，并按 ProxyVerifier → Director/Blender Proxy 修订循环真实运行了四轮机位/动作修订；每轮四个独立 MP4，旧 revision 未覆盖。完整证据、目录、哈希和 API 计数见 [PROXY_REVISION_009_012_RUN.md](PROXY_REVISION_009_012_RUN.md)。

| revision | deterministic ProxyVerifier | VLM | 结论 |
|---|---|---|---|
| 009 | passed | revision_requested | 机位仍偏后/高，动作顺序不可读 |
| 010 | passed | revision_requested | 角色、音箱关系和机位覆盖不清 |
| 011 | passed | revision_requested | 手势像站立/行走，背包与音箱混淆 |
| 012 首次 | failed | 未调用 | hand.R frame 44 超出 IK 可达范围，已保留失败证据 |
| 012 修正版 | passed | revision_requested | 仍有遮挡、手势和全身上下文可读性问题 |

本轮 Seedance/Kling 调用均为 0；VLM 调用 4 次。由于最终反馈属于 `scene_structure`、`character_trajectory`、`camera_trajectory` 和 `physical_event`，未进入 Appearance-only prompt，也未提交真实视频后端。

### 4.11 revision_013–revision_017（2026-08-09）

最新真实运行和 VLM 反馈见 [PROXY_REVISION_013_017_RUN.md](PROXY_REVISION_013_017_RUN.md)。`revision_017` 的四个 Blender MP4、state/camera/skeleton 日志和哈希均通过确定性检查；本次 VLM 只调用 1 次，结果仍为 `revision_requested`。反馈集中在侧步像走位、最终回头不清楚、路人回挥被长椅/角色遮挡以及机位重叠。

这轮还修复了一个真实代码根因：显式手 landmark 原先只对 `revision_013` 生效，`revision_014–016` 被错误地降级为角度推断；现在所有 revision 编号 >= 013 都使用显式 down/out/up landmark。该修复不改变 pipeline_v2 schema、实体 ID、K0–K4 帧或共享世界。

结论：当前失败不在 API、编码、哈希或轨迹日志，而在程序化球/圆柱骨架无法稳定表达侧步、转身和后景交互。未通过 Proxy 视觉门禁，因此本轮 Seedance/Kling、Appearance-only Prompt 和最终视频审查均为 0 次。下一步应接入标准 rigged humanoid/GLB 与动作重定向，并把 bench 移出 passerby lane，再对同一场景重新复审。

### 4.12 rigged humanoid / revision_018–revision_020（2026-08-09）

详细记录见 [RIGGED_HUMANOID_REVISION_018_020_RUN.md](RIGGED_HUMANOID_REVISION_018_020_RUN.md)。已接入带网格、骨骼和蒙皮的 `CesiumMan.glb`，修复了 Y-up 垂直偏移、默认迈步姿态、真实 pose 日志和 VLM camera/frame 顺序标签。`revision_018–020` 每次真实四机位渲染均为 20 passed、0 failed、2 unknown；VLM 均为 `revision_requested`，Seedance/Kling 均为 0。

最新阻塞是动作素材而非管线：标准人形仍缺少可读的侧步/挥手动作片段和脚掌 IK，640×360 下背包和角色容易重叠。下一阶段应接入真实 BVH/动作捕捉片段重定向到该骨骼，并把背包绑定到角色骨骼局部挂点；未通过 Proxy 视觉门禁前不得进入 Appearance-only 或后端视频生成。

### 4.13 ACCAD BVH retarget / revision_021–revision_024（2026-08-10）

详细证据见 [BVH_RETARGET_RUN_20260810.md](BVH_RETARGET_RUN_20260810.md)。本轮使用 ACCAD/Open Motion Project 的真实 BVH（CC BY 3.0），将两个 side-step clip 导入 Blender 隐藏源骨架，再把四肢局部旋转重定向到共享 CesiumMan。修复过三个真实问题：Blender `frame_set` 浮点错误、Euler BVH 被误读为单位 quaternion、BVH 躯干后仰掩盖侧步。最终 revision_024 增加了明确记录的有界 side-step overlay 和手势 overlay。

四个 revision 的真实 Proxy 确定性检查均通过（20 passed / 0 failed / 2 unknown，低模创意可读性仍需人工/VLM）；每个 revision 都是独立目录，没有覆盖旧结果。revision_021–024 各调用 VLM 1 次，均为 `revision_requested`，因此本轮 Seedance/Kling/API 调用保持 0，未进入 Appearance-only Prompt。当前应优先换更标准的人形 rig/动作片段或接入 hand/foot IK，再重新走 ProxyVerifier→VLM。

本轮相关回归测试为 52 passed。完整仓库测试未作为通过依据：VACE 第三方测试收集时缺少 `vace` 包，`tests/` 全量还触发既有外部集成测试并在 360 秒超时；这些失败均保留原始证据。

### 4.14 full-chain Seedance endpoint / 2026-08-10

完整证据见 [E2E_SEEDANCE_RUN_20260810.md](E2E_SEEDANCE_RUN_20260810.md)。使用历史已通过 VLM 的 plaza Proxy，在新不可变目录中按 camera_id 独立提交 Seedance：4 submit、84 query、4 download，四个真实 MP4 均通过文件、ffprobe、时长和黑帧检查。最终 VLM 发现独立 task 之间出现人物身份、服装和场景布局漂移，因此最终视觉门禁为 `revision_requested`；没有把“成功下载”误报为“多视角一致”，也没有再次生成。

同日 `park_badminton` canonical/skeleton 两个 Proxy 真实 VLM 均拒绝，Seedance 调用均为 0；问题回到场景 staging、接触轨迹和机位覆盖。

本轮还修复了两个可复现的执行问题：无 TOS 凭据时复杂场景 runner 不再隐式选择已失败的 tmpfiles，而是使用此前真实跑通的 Uguu 研究上传通道；Seedance 默认最大轮询从 20 提高到 30，只增加同一 task 的状态等待，不增加 submit、不自动重试。相关回归测试共 65 passed。

### 4.15 多场景 Proxy 试验 / 2026-08-10

本轮使用三个不同 story prompt 生成新的 canonical 四机位 Proxy，命令入口仍为 `scripts/run_complex_scene_suite.py`，Seedance/Kling 调用为 0：

| 场景 | 真实 Proxy 目录 | VLM | 主要阻塞 |
|---|---|---|---|
| `plaza_dance_circle` | `runs/results/complex_scene_suite_20260810_181253/plaza_dance_circle_20260810_181253/` | `revision_requested` | 人物/背包/音箱重叠，道具接地和动作顺序不清 |
| `park_badminton` | `runs/results/complex_scene_suite_20260810_181253/park_badminton_20260810_181346/` | `revision_requested` | 网、球拍、球员和 spectator 重叠，serve/return 接触与脚接地不可读 |
| `indoor_market_exchange` | `runs/results/complex_scene_suite_20260810_181253/indoor_market_exchange_20260810_181413/` | `revision_requested` | reverse 机位被 counter 遮挡，手推车轮子接地和 vendor/helper 关系不清 |

三条场景均生成 4 个真实 MP4、manifest、state/camera/asset 日志并通过确定性媒体检查；每条各调用 VLM 1 次。由于反馈全部属于结构、轨迹、相机或物理事件，均没有进入 Appearance-only Prompt，也没有提交 Seedance。下一步应按场景分别做 Director/Blender revision，而不是用同一个真人化 prompt 覆盖这些结构问题。

### 4.16 indoor_market_exchange revision_025–revision_027 与 Seedance 真实端到端 / 2026-08-10

本轮按 VLM 结构反馈连续生成 `revision_025`、`revision_026`、`revision_027`，修复反向机位遮挡、柜台被读成躯干、手推车轮子方向/悬空、vendor/helper 深度层、customer 停顿举手、cart/box 同步停顿和纸张事件。`revision_027` 的四机位 Proxy 通过确定性检查并获得 VLM approve，未覆盖旧 revision。

随后把 approved Proxy 复制到不可变 endpoint，按 camera_id 独立提交 Seedance：4 submit、125 query、4 download；最后一次只是继续查询原 task，没有新 submit。四条真实 MP4 均成功下载，媒体检查均通过（1280×720、24 fps、121 frames、约 5 秒、黑帧 0）。此前 verifier 因调用方把 scene plan 的 `24.0` 与 ffprobe 的 `24/1` 做字符串比较而误报失败；现已改为数值化 FPS 比较并加入回归测试。

完整证据见 [SEEDANCE_MARKET_REVISION_027_RUN.md](SEEDANCE_MARKET_REVISION_027_RUN.md)，endpoint 见 [e2e_seedance_indoor_market_revision_027_20260810](../runs/results/e2e_seedance_indoor_market_revision_027_20260810/)。

最终 VLM 真实调用 1 次，结论为 `revision_requested`：四个独立 Seedance task 虽都返回可播放真人视频，但没有保持跨机位共享人物、推车、道具和环境，反馈类别为 scene/character/object/camera/physical。因此按 pipeline 不能用 Appearance-only Prompt 掩盖，必须回到多视角一致性后端或加入共享外观锚点。这是后端限制的真实失败证据，不把“下载成功”误报为“多视角一致”。
