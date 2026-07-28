# VideoActAgent：可执行多镜头控制到视频生成的研究设计

日期：2026-07-29  
状态：待用户最终审阅

## 0. 强制审批与报告制度

本项目采用逐阶段人工审批，不允许自动连续执行。

### 0.1 阶段门

每一阶段遵循固定顺序：

1. 开始阶段前，报告本阶段目标、预计修改、实验方式、风险和资源需求；
2. 获得用户明确批准后才开始该阶段；
3. 阶段完成后，提交阶段结果报告；
4. 等待用户检查真实产物并明确回复允许继续；
5. 未获得批准时，不进入下一阶段，不以后台任务、自动续跑或提前准备替代审批。

阶段结果报告必须包括：

- 完成的模块、文件和关键实现；
- 实际执行的命令与实验配置；
- 可点击或可播放的真实产物；
- 指标、耗时、失败案例和已知问题；
- API 调用次数、生成时长、实际或估计费用；
- 服务器型号、GPU 数量、运行时长和实际或估计费用；
- 下一阶段拟做事项、风险与预计资源；
- 明确的“等待批准”状态。

### 0.2 API 调用审批

任何会向外部视频、图像、LLM 或存储服务发送请求的操作，都必须在调用前单独报告并获得批准。报告至少包含：

- 服务与模型名称；
- 将发送的数据类型，是否包含人物、真实视频或其他敏感素材；
- prompt、参考图、参考视频或音频的用途；
- 计划调用次数、单次时长/分辨率和最大费用；
- 结果保存位置和失败后的重试上限。

批准可以针对一个边界明确的实验批次，例如“允许 Seedance 2.0 调用 4 次、每次 5 秒 720p、总费用不超过给定上限”。超过次数、费用、素材范围或重试上限时必须重新申请。读取公开网页和本地只读检查不视为模型 API 实验，但涉及登录、付费、上传或改变外部状态时仍需批准。

### 0.3 服务器与 GPU 审批

在租用、登录、启动或持续使用远程服务器/GPU 前，必须报告并获得批准。报告至少包含：

- 云服务商或服务器来源；
- GPU 型号、显存、数量；
- 镜像、依赖、模型和数据下载量；
- 预计占用时长、最高预算和自动关机条件；
- 训练/推理命令、checkpoint 周期和日志位置；
- API Key、SSH Key 和数据的安全处理方式。

获批资源只能用于批准的阶段和时限。训练结束、失败或空闲达到约定阈值后应停止实例，并在阶段报告中说明实际时长和费用。

### 0.4 默认禁止事项

未经当次明确批准，不得：

- 调用 Seedance、Kling、OpenAI 或其他付费/配额 API；
- 上传人物图片、真实视频、音频或未公开数据；
- 租用、登录或启动 A100 等远程 GPU；
- 下载大型模型或数据集到远程服务器；
- 启动训练、批量推理或自动重试任务；
- 创建持续运行的后台进程或跨阶段自动流水线。

## 1. 目标

构建一个面向研究与可复现实验的视频生成 Agent。系统把剧本编译为可执行的多镜头控制计划，使用 Blender 生成包含相机运动和人物走位的低成本 proxy video，再将 proxy、关键帧和电影语言提示注入 Seedance、Kling 或 VACE，生成最终视频。

第一篇工作的核心问题是：

> 与普通 prompt 或电影语言 prompt 相比，可执行 proxy 是否能更准确地把相机轨迹、人物调度和跨镜头连续性传递给视频生成模型？

## 2. 相比原方案的调整

原方案以 VACE-Wan2.1 和可训练 Proxy Adapter 为中心，并把 Story Graph、Shot Program、Continuity Memory、Seedance Bridge、Evaluator 设计成多个独立模块。

最终方案作出以下调整：

1. 训练从前置条件改为可选增强。先用 Seedance、Kling 和 stock VACE 做 training-free 实验，只有结果证明 proxy 有价值但控制强度不足时才训练适配器。
2. 多 Agent 改为单一分阶段 Planner。借鉴 MovieAgent 和 Camera Artist 的层次化与递归规划，但不复制导演、场景、摄影等多个 LLM Agent。
3. Story Graph、Continuity Memory 和 Shot Program 合并为一个可执行数据结构 `ShotScript`。
4. SceneCraft 式自由 Blender 代码生成改为受约束模板编译，降低 LLM 代码错误率。
5. 每个模块完成后立即运行真实实验并保存可观看的视频，不等待整套系统完成。
6. VACE-1.3B 保留为唯一训练基座；ATI、MotionCtrl、CamTrol、ReCamMaster 仅作为方法参考、工具、数据或对照。

## 3. 文献继承关系

- SceneCraft：继承“场景图 -> 数值约束 -> Blender Python -> 渲染检查”，扩展到人物动画、相机关键帧和多镜头。
- CamTrol：继承“显式几何草稿优于仅靠运镜 prompt”的假设；闭源模型使用 proxy video，开源模型可进一步研究 latent/control 注入。
- MovieAgent：继承剧本、场景、镜头的分层规划和角色库数据组织。
- Camera Artist：继承相邻镜头递归规划和景别、运镜、构图、光照等电影语言注入。
- VACE：复用 reference-to-video、video-to-video、mask、depth、pose 等控制通道，并作为可训练开源基座。

## 4. 候选创新点

### 4.1 可执行多镜头控制表示

提出 `ShotScript`，在一个统一表示中联合描述：

- 场景与角色 ID；
- 镜头时长与时间轴；
- 相机内参、外参、焦点和关键帧；
- 人物起止位置、动作与注视目标；
- 屏幕方向、轴线、道具状态等跨镜头连续性。

它同时面向 LLM 规划、Blender 执行、生成模型控制和自动评测。

### 4.2 Proxy-to-Video 控制迁移

将 ShotScript 编译为真实可播放的 RGB proxy、depth、ID mask、pose 和轨迹文件；研究这些信号通过 prompt、首尾帧、参考视频或 VACE 结构通道迁移到最终视频时的控制忠实度。

### 4.3 可测量的镜头与人物调度

不只评估审美和文本一致性，还比较预期与生成结果之间的相机运动方向、人物屏幕轨迹、身份保持和跨镜头状态连续性。

## 5. 研究假设

- H1：电影语言 prompt 比普通 prompt 提高运镜指令遵循率。
- H2：Blender proxy video 比电影语言 prompt 更准确地传递相机运动和人物屏幕轨迹。
- H3：proxy + 角色参考 + 分时段 prompt 优于单独使用任一条件。
- H4：递归 ShotScript 规划和连续性状态注入优于各 shot 独立规划与生成。
- H5：如果闭源模型只能部分遵循 proxy，VACE 结构控制或轻量 adapter 能进一步提高控制精度。

## 6. 最小架构

系统只保留四个模块。

### 6.1 Shot Planner

输入：故事梗概、角色参考、用户导演约束。  
输出：经过 JSON Schema 校验的场景列表与 ShotScript 序列。

单个 LLM 按顺序完成：

1. 提取场景、人物和叙事状态；
2. 分解为多个 shot；
3. 根据上一 shot 递归生成当前 shot；
4. 注入景别、焦距、机位、运镜、构图、光照与剪辑方式；
5. 输出结构化 JSON，不直接写 Blender 代码。

### 6.2 Blender Proxy Compiler

输入：ShotScript。  
输出：`.blend`、Blender Python、RGB proxy MP4、depth、ID mask、pose、相机轨迹和人物轨迹。

实现原则：

- 使用固定模板与可复用函数库；
- 第一版角色使用彩色 capsule、骨架或低模人物；
- 用 Blender 关键帧实现相机和人物运动；
- 相机和角色状态全部由 ShotScript 数值驱动；
- 编译失败时返回结构化错误给 Planner 修正一次，禁止无限反思循环。

### 6.3 Control Bridge

输入：ShotScript、proxy 和角色/场景参考。  
输出：不同后端所需的 payload。

同一 shot 支持五种实验条件：

1. 普通 prompt；
2. 电影语言 prompt；
3. 首帧/首尾帧；
4. RGB proxy reference video；
5. RGB proxy + depth/pose/mask 的 VACE 结构控制。

### 6.4 Generator and Evaluator

统一生成接口：

- `SeedanceBackend`；
- `KlingBackend`；
- `VACEBackend`。

统一实验输出目录：

```text
runs/<experiment_id>/
  input/
  shotscript/
  proxy/
  generations/
  comparisons/
  metrics.json
  report.md
```

Evaluator 使用固定视觉工具和人工打分表，不增加新的 LLM Agent。

## 7. ShotScript 最小字段

```json
{
  "scene_id": "station",
  "shot_id": "s02",
  "duration": 5,
  "prompt": "两人在车站月台相遇",
  "camera": {
    "shot_size": "medium",
    "focal_length_mm": 50,
    "motion": "dolly_in",
    "start": [0, -6, 1.7],
    "end": [0, -3, 1.7],
    "look_at": "actor_a"
  },
  "actors": [
    {
      "id": "actor_a",
      "start": [-2, 0, 0],
      "end": [0, 0, 0],
      "action": "walk",
      "facing": "actor_b"
    },
    {
      "id": "actor_b",
      "start": [2, 0, 0],
      "end": [1, 0, 0],
      "action": "stand",
      "facing": "actor_a"
    }
  ],
  "continuity": {
    "previous_shot": "s01",
    "screen_direction": "left_to_right",
    "axis_side": "north",
    "carry_props": []
  }
}
```

第一版不加入复杂情绪曲线、自动剪辑理论库、音频规划或 4D 世界模型。

## 8. 逐模块真实实验计划

### 阶段 0：安全与 API 基线

工作：

- 轮换已写入源码的京东云 API Key；
- 将 Kling 和 Seedance demo 改为仅从环境变量读取密钥；
- 统一提交、轮询、重试、下载和实验记录；
- 核验京东云 Seedance 网关是否支持 `reference_video`。

真实实验：

- 用同一普通 prompt 分别调用 Seedance 和 Kling；
- 保存原始 payload、响应、MP4 和成本/耗时。

验收：至少一个后端能稳定生成并自动下载视频；任何密钥均不进入仓库。

### 阶段 1：电影语言 Prompt 实验

工作：

- 实现单 LLM Shot Planner；
- 固化普通 prompt、分层 prompt、电影语言 prompt 三种模板；
- JSON Schema 校验 ShotScript。

真实实验：

- 使用一个真实三镜头双人物短剧本；
- 在同一模型、相同角色参考下生成三组 MP4；
- 并排展示普通 prompt、MovieAgent 风格分层 prompt、Camera Artist 风格电影语言 prompt。

验收：输出可解析 ShotScript；电影语言版本包含明确景别、机位、运镜、人物位置和时间描述；生成视频可直接观看。

### 阶段 2：Blender Proxy 实验

工作：

- 实现 ShotScript 到 Blender Python 的模板编译；
- 创建相机、两名角色、地面、基础灯光和关键帧；
- 渲染 RGB、depth、ID mask 和俯视轨迹图。

真实实验：

- 将阶段 1 的三镜头剧本实际渲染成三个 proxy MP4；
- 拼接为一段 storyboard preview；
- 人工检查景别、运动方向、遮挡和轴线。

验收：三个 proxy 可重复渲染；相机与人物终点误差在数值层面为零；无对象丢失或越界。

### 阶段 3：Proxy 注入实验

工作：

- 扩展 Seedance 后端以支持参考视频；若京东云网关不支持，则切换火山方舟官方接口或先使用 Kling/VACE；
- 自动从 proxy 提取首帧、尾帧和参考视频；
- 生成分时段 prompt。

真实实验：

- 对同一 shot 生成普通 prompt、电影语言 prompt、首尾帧、proxy video 四组真实视频；
- 增加一条真实拍摄参考视频作为 motion reference，与 Blender proxy 比较。

验收：形成至少一个四宫格 MP4；能观察并记录相机方向和人物屏幕轨迹差异。

### 阶段 4：多镜头连续性实验

工作：

- 让当前 ShotScript 条件化于上一 shot 的文本状态、角色参考和结束帧；
- 保持角色 canonical reference 不变；
- 拼接多 shot 结果。

真实实验：

- 对同一个三镜头故事运行“独立规划/生成”和“递归规划 + 连续性注入”；
- 展示两版完整视频和逐镜头关键帧表。

验收：递归版本在角色身份、屏幕方向或叙事状态中至少有两项优于独立版本；如果没有提升，停止增加更复杂的 memory 机制，先分析后端是否忽略参考条件。

### 阶段 5：Stock VACE 开源对照

工作：

- 部署 VACE-Wan2.1-1.3B 480p；
- 输入 RGB proxy、depth、pose 和 mask；
- 与闭源后端使用相同 ShotScript 和评测脚本。

真实实验：

- 对代表性 camera-only、camera+actor、multi-actor 三类镜头运行 stock VACE；
- 输出 prompt-only、RGB proxy、结构控制三组视频。

验收：确认结构通道是否比 RGB proxy 更稳定；在这个阶段之前不租训练服务器。

### 阶段 6：可选轻量训练

触发条件：H2 获得支持，即 proxy 明显有帮助，但 stock VACE 对复杂相机或多人物轨迹仍控制不足。

训练范围：

- 冻结 VAE、文本编码器和主 DiT；
- 只训练 LoRA、零初始化 control block 或小型 proxy encoder；
- 使用 1-2 张 A100 40GB，最长 72 小时；
- 480p，49-81 帧，BF16，gradient checkpoint，缓存 VAE/text latent。

数据优先级：

1. 自建 Blender 合成 ShotScript-proxy 对；
2. ReCamMaster MultiCamVideo 的相机轨迹和动态角色；
3. RealCam-Vid/RealEstate10K 的真实相机数据；
4. 小规模真实双人物视频用于域差验证。

验收：相对 stock VACE，在保留视频质量的前提下提高相机或人物轨迹控制；若 24 小时内验证集不改善，停止长训练并回到条件设计。

### 阶段 7：完整评测与论文实验

建立 30 个核心 ShotScript：

- 10 个 camera-only；
- 10 个单人物 camera+actor；
- 10 个双人物三镜头序列。

扩展实验在核心结果稳定后增加到 100 个 ShotScript。

比较方法：

- 普通 prompt；
- 电影语言 prompt；
- 首尾帧；
- RGB proxy；
- proxy + 结构控制；
- 可选训练 adapter。

## 9. 指标

### 9.1 控制指标

- Camera Direction Accuracy：pan/tilt/zoom/dolly/truck/orbit 方向是否正确；
- Camera Motion Similarity：proxy 与生成视频的全局光流或估计相机轨迹相似度；
- Actor Trajectory ADE/FDE：人物 bbox 中心轨迹与 proxy 归一化轨迹的距离；
- Identity Consistency：同一角色跨帧、跨 shot 的人脸或 DINO 特征相似度；
- Screen Direction Consistency：人物运动方向和 180 度轴线是否保持；
- ShotScript Coverage：计划中的角色、动作、镜头属性被实现的比例。

### 9.2 质量指标

- VBench 的 motion smoothness、subject consistency、aesthetic quality；
- 人工五分制：剧本忠实度、运镜忠实度、人物调度、连续性、总体可用性；
- VLM 评分只作为辅助，不替代人工对照。

### 9.3 内部 go/no-go 门槛

- Proxy 相比电影语言 prompt 的 Camera Direction Accuracy 提升至少 15 个百分点，或 Actor ADE 降低至少 15%；
- 连续性注入相比独立生成的跨 shot 身份或屏幕方向指标提升至少 10%；
- 若两个门槛均未达到，则论文主张收缩为“可执行视频导演与诊断框架”，不投入 adapter 长训练。

## 10. 第一组固定测试故事

使用一个三镜头、双人物、单场景的车站相遇故事，减少风格、场景切换和音频造成的干扰：

1. 广角建立镜头：角色 A 从左向右进入，角色 B 在远端等待；相机固定或轻微横移。
2. 中景相遇镜头：相机 dolly in，A 走到 B 面前停止；两人保持身份和服装。
3. 过肩镜头：保持轴线，A 与 B 交换注视；相机做小幅 arc。

第二组使用真实拍摄的双人物参考视频，复用其人物和相机运动，检验 synthetic proxy 与 real motion reference 的差异。

## 11. 失败处理

- API 不支持参考视频：先完成 prompt、首尾帧和 VACE 实验，同时切换官方 Seedance 接口；不伪造 capability。
- Blender 代码失败：只允许一次基于错误信息的修正，之后回退到固定场景模板。
- 生成后端忽略人物轨迹：增加 pose/ID mask 或分解为更短 shot，不立即引入更多 Agent。
- 身份跨 shot 漂移：每个 shot 重复注入 canonical reference，上一结束帧只传递状态，不承担唯一身份来源。
- 显存不足：降低帧数而不改变实验定义；保持所有方法使用相同分辨率和帧数。
- 指标不稳定：同时报告自动指标、人工评分和失败案例，保留完整随机种子与 payload。

## 12. 预期仓库边界

```text
videoactagent/
  configs/
  schemas/
  planner/
  blender_proxy/
  control_bridge/
  backends/
  evaluation/
  experiments/
  assets/
  runs/
  tests/
  docs/
```

训练代码作为 `experiments/vace_adapter/` 的可选子项目，不污染 training-free 主流程。

## 13. 顺序与预估时间

在不计算大规模批量生成等待时间的情况下：

1. 阶段 0：0.5-1 天；
2. 阶段 1：1-2 天；
3. 阶段 2：3-5 天；
4. 阶段 3：2-3 天；
5. 阶段 4：3-5 天；
6. 阶段 5：2-4 天；
7. 阶段 6：可选，1-2 张 A100 40GB、最多 3 天；
8. 阶段 7：1-2 周，取决于 API 生成数量和人工评测规模。

最早在阶段 1 就能看到真实生成视频；阶段 2 能看到代码预览；阶段 3 能判断核心论文假设是否成立。

## 14. 最终交付

- 可编辑 ShotScript 与三镜头示例；
- Blender proxy、轨迹图和结构控制通道；
- Seedance/Kling/VACE 统一实验后端；
- 每个阶段的输入、真实 MP4、并排比较、指标和报告；
- 30-100 个 ShotScript 评测集；
- training-free 主方法与可选 VACE adapter；
- 完整消融、失败案例和论文级实验表格。
