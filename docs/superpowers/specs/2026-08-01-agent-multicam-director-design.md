# Agent 多视角导演标注器设计

日期：2026-08-01  
状态：设计已逐段确认，等待书面规格最终审阅

## 1. 目标

在不破坏现有单摄像机人工标注器的前提下，新增一套 Agent 多视角导演标注器。系统从完整故事出发，先规划人物调度及每台摄像机的职责，再生成三台同步摄像机的连续轨迹和完整 Blender Proxy。人工可以修改或否决 Agent 方案；只有人工批准的 Proxy 才能导出下游控制包。

本阶段解决以下问题：

1. 将 Clay 外观预览与人物轨迹、相机参数、深度和分割等结构条件分离。
2. 从单条连续相机轨迹升级为同一世界、同一时间轴上的三摄像机同步覆盖。
3. 用 DeepSeek 生成可审计的初始人物调度和相机职责，但保留确定性校验与人工批准。
4. 修复浮点漂移被误编译为镜头变化，以及相机跨越角度边界时翻转的问题。
5. 用六个不同场景的真实 DeepSeek 规划和真实 Blender 输出检验泛化，不把单元测试素材当成实验结果。

真人重绘质量、Seedance/Kling/VACE 正式矩阵，以及 ATI、ReCamMaster 或 CamTrol 的 GPU 推理不属于本阶段。当前服务器可以释放；以后接入显式控制模型时再申请 A100。

## 2. 兼容策略

采用“共享底层、两个独立入口”：

```text
共享项目数据、轨迹数学、Blender 执行和版本证据
├── 原标注器：director-loop，单摄像机、纯人工、保持现有行为
└── 新标注器：director-multicam，DeepSeek 规划、三摄像机、人工审批
```

约束如下：

- 原标注器的启动命令、manifest、页面行为和输出目录保持可用。
- 新标注器使用独立的命令、manifest、页面入口和输出目录。
- 两套标注器可以读取同一个故事 bundle，但不能覆盖彼此的 annotation 或 Proxy。
- 共享层的修改必须通过原标注器兼容性测试。
- 旧结果默认只读；新运行生成不可覆盖的版本目录。

原标注器继续使用已有输出目录，不迁移、不重命名。新标注器使用新的独立根目录，例如：

```text
runs/work/director_loop_v1/<scene>/              # 现有目录保持不变
runs/work/director_loop_multicam_v1/<scene>/     # 新标注器
```

## 3. 总体数据流

```text
故事 Prompt
→ 连续 SceneTimeline
→ DeepSeek 生成 MulticamPlan
→ 本地 schema 与几何校验
→ 人工审核或修改
→ 确定性轨迹编译
→ 一次 Blender 世界模拟
→ Camera A/B/C 同步渲染三条完整 Proxy
→ 自动证据检查与人工并排审批
→ 导出结构控制包
```

系统不把故事拆成分别生成的短镜头。三台摄像机都贯穿完整时间轴，只是在不同时间段承担不同的主要覆盖职责。第一版不自动剪辑，只输出三条同步完整视频供并排比较。

## 4. 核心数据对象

### 4.1 SceneTimeline

包含场景边界、人物、动作事件、K0–K4 的时间、可通行区域和物体交互。人物动作只有一份权威世界状态，三台摄像机不得分别重算人物动画。

### 4.2 MulticamPlan

每台摄像机至少包含：

- `camera_id`
- 全局角色：主全景、人物跟随、侧面或反打等
- `responsibility_segments`：负责的连续时间段
- 目标人物或物体
- 景别
- 相机运动方式
- 注视目标策略
- 遮挡、碰撞和构图限制
- 简短规划理由

规划还必须覆盖完整时间轴，明确人物调度建议，并记录哪些 K 帧已经被人工锁定。

### 4.3 ProxyVersion

一个版本同时绑定：

- 故事、时间轴、Agent 规划和人工修改的来源哈希；
- Blender 场景、版本、执行日志；
- 三条 MP4 的帧数、FPS、时长、SHA-256；
- 人物轨迹和三台摄像机轨迹；
- 自动检查结果和人工审批状态。

### 4.4 StructureControlBundle

人工批准后导出：人物二维/三维轨迹、摄像机内外参、深度、人物分割及对应的帧时间。Clay RGB 只作为人工预览，不再被描述为外观无关的控制信号。

## 5. DeepSeek 规划边界

DeepSeek 从系统环境变量读取：

- `DEEPSEEK_API_KEY`
- `DEEPSEEK_BASE_URL`

密钥不得写入日志、manifest、README 或错误信息。保存的证据包括模型名、脱敏后的服务地址、请求摘要、原始响应、解析结果、耗时和调用次数。

LLM 只提出高层规划，不直接写 Blender 文件，也不绕过人工门禁。首次规划由用户明确触发；重新规划也只能由用户点击触发。API 调用不自动重试。JSON 外壳等不改变语义的格式问题可以本地修复；缺少摄像机职责、时间覆盖或约束时必须标记失败，不能擅自补造内容。

选择 K2 为修改起点时，K0–K1 保持冻结，Agent 只允许重新规划 K2–K4。用户可以只重规划一台摄像机，不影响人物动作和另外两台摄像机。

## 6. 新标注器界面

新页面包含：

1. 完整故事和人物事件时间轴。
2. 三条 Proxy 的同步播放器，并支持单独放大一个视角。
3. Camera A/B/C 的职责时间条，标明当前主负责机位。
4. 当前 K 帧的人物位置、摄像机位置、注视点、景别和 Agent 理由。
5. “生成 Agent 初始方案”“只重规划当前摄像机”“生成下一版 Proxy”“批准当前版本”等明确动作。

所有轨迹编辑图必须从当前 Proxy 自动加载人物与摄像机起点。轨迹 Prompt 由批准后的数值轨迹自动编译，避免人工 Prompt 与轨迹冲突。原标注器页面不增加这些控件。

## 7. 轨迹与相机朝向

以场景边界对角线 `D` 作为位置容差基准：

- 注视点位移小于 `0.01D` 且视线夹角小于 `1°`：编译为保持固定注视目标。
- 摄像机位移小于 `0.005D`：视为静止。
- Roll 变化小于 `0.5°`：视为无 Roll 变化。
- 焦距变化小于 `1 mm`：不生成变焦描述。

`D` 必须有正的数值下限，避免退化场景产生零容差。只有超过容差的变化才能生成镜头变化 Prompt。

摄像机朝向每帧根据连续位置轨迹和注视目标重新计算，不直接在起止 Euler 角之间插值。相邻四元数保持符号连续；Roll 单独应用；环绕运动使用展开后的连续方位角。无计划切镜时，相邻帧视线夹角超过 `30°` 直接判定失败，从而捕获仰向天空或瞬时翻转。

## 8. 同步与自动检查

三台摄像机必须复用同一次 Blender 世界模拟，具有完全相同的帧数、FPS、时长和人物世界坐标。自动检查包括：

- 三台摄像机的职责时间段合计是否覆盖完整时间轴；
- 每台摄像机是否具有完整时间轴上的有效连续轨迹；
- 负责时间段内目标人物是否在画面；
- 目标景别是否满足规划范围；
- 严重遮挡、穿模和摄像机碰撞；
- 轨迹位置或旋转不连续；
- 固定注视目标是否被错误编译为变化；
- 三条视频是否缺帧或时长不一致。

DeepSeek 格式错误、Blender 非零退出、缺帧、哈希缺失和时长不一致均产生明确失败状态，不能进入批准状态。自动指标只报告可直接计算的事实；构图是否自然仍由人工判断。

## 9. 泛化实验

第一轮使用六个完整故事：

1. `station_reunion`：双人接近、会合、环绕。
2. `city_crosswalk`：横向运动、动态背景、道路约束。
3. `forest_path`：连续跟随、遮挡和弯曲路径。
4. `studio_room`：狭小室内、固定注视和有限机位。
5. `cafe_handoff`：双人交互、物品交接和景别变化。
6. `warehouse_chase`：快速人物运动和多机位协作。

每个故事初始规划调用 DeepSeek 一次，共预计六次，零自动重试；本阶段不调用 Seedance、Kling 或其他视频生成 API。每个场景必须生成三条真实 Blender MP4 并保存完整证据。

验收要求：

- 三视角帧数、FPS 和时长完全一致。
- 负责时间段内目标人物可见率不低于 95%。
- 不出现相机翻转、穿模或异常跳跃。
- Agent 规划覆盖完整故事时间轴。
- 记录人工修改的轨迹点、摄像机职责和规划文本。
- 六个场景至少五个完成真实三视角 Proxy；失败场景保留并解释，不能通过更换随机种子隐藏。

在 `station_reunion` 上保留原标注器与新标注器对照，比较人工操作量、完整性、轨迹问题和构图。单摄像机与三摄像机不合并为一个没有意义的总分。

## 10. 测试与证据原则

测试分为三层：

1. 单元测试验证 schema、锁定边界、容差、角度连续性和版本隔离；这些素材只证明代码行为。
2. 本地集成测试使用真实 Blender 可执行文件，验证两个标注器都能启动并生成真实文件。
3. 六场景实验使用真实 DeepSeek 响应、真实 Blender 渲染和真实人工审批，才可写入实验报告。

不得用 mock、占位视频、复制旧视频或人工编造指标宣称全链路通过。无法判断的结果标记为未知，并说明需要的人工检查。

## 11. README 修改

README 统一保存为无乱码的 UTF-8 中文，并保持简洁，只包含：项目目的、当前真实能力与限制、两套标注器入口、端到端流程、输出位置、DeepSeek 环境变量、实验结果入口和参考文献。

参考文献表必须说明论文对应模块、是否有已核验的官方代码，以及本项目状态属于“已采用”“计划采用”或“仅供比较”。没有本地实现或官方来源证据时，不得声称已经复现。

核心参考来源：

- [SceneCraft: An LLM Agent for Synthesizing 3D Scene as Blender Code](https://arxiv.org/abs/2403.01248)
- [Automated Movie Generation via Multi-Agent CoT Planning](https://arxiv.org/abs/2503.07314)
- [Camera Artist: A Multi-Agent Framework for Cinematic Language Storytelling Video Generation](https://arxiv.org/abs/2604.09195)
- [VideoCoCo: Code-as-CoT for Physically-Consistent Video Generation via an Agentic Dual-Engine System](https://arxiv.org/abs/2607.27380)
- [ATI: Any Trajectory Instruction for Controllable Video Generation](https://arxiv.org/abs/2505.22944)
- [Training-free Camera Control for Video Generation](https://arxiv.org/abs/2406.10126)
- [CameraCtrl: Enabling Camera Control for Text-to-Video Generation](https://arxiv.org/abs/2404.02101)
- [MotionCtrl: A Unified and Flexible Motion Controller for Video Generation](https://arxiv.org/abs/2312.03641)
- [ReCamMaster: Camera-Controlled Generative Rendering from A Single Video](https://arxiv.org/abs/2503.11647)
- [VACE: All-in-One Video Creation and Editing](https://arxiv.org/abs/2503.07598)
- [OmniWeaving: Towards Unified Video Generation with Free-form Composition and Reasoning](https://github.com/Tencent-Hunyuan/OmniWeaving)

## 12. 实施阶段边界

实施按以下顺序进行，每个阶段完成真实检查后再进入下一阶段：

1. 冻结原标注器兼容基线，整理共享数据边界。
2. 实现 MulticamPlan、DeepSeek 适配器及本地校验。
3. 实现三摄像机职责、轨迹编译和同步 Blender 渲染。
4. 实现独立的新标注器页面和版本证据。
5. 修复容差与朝向连续性，并完成真实 Blender 集成测试。
6. 执行六场景真实实验并逐场保存结果。
7. 更新 README、参考文献和实验入口。

本设计完成后不自动进入显式控制大模型或真人重绘实验；这些作为后续独立规格处理。
