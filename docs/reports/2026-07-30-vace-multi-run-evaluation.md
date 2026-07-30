# VACE 12-run 多 Proxy / Prompt / Seed 真实实验报告

## 结论

本轮不是复用同一个 Blender proxy。真实生成矩阵包含三类独立控制输入：

1. `actor_only`：相机位置和朝向均锁定，A 向右接近静止的 B；
2. `camera_only`：A、B 世界位置静止，camera truck right 并持续看向人物；
3. `coupled_arc`：A 接近 B，同时 camera 使用 15 个显式顺时针圆弧关键帧。

每类使用 short / structured 两种 prompt 和 seed 2026 / 2027，共 12 次真实 VACE-1.3B 推理。12/12 生成与文件完整性通过；独立人工观察显示 12/12 都在二维画面中复现了预期的宏观人物/构图变化，人物 A、B 全程可见且未观察到身份交换或严重形变。

但不能把结果写成“精确控制全部通过”：严格 camera phase-correlation 对 4 条 camera-only 输出均为 `inconclusive`，真实 Blender source proxy 本身也为 `inconclusive`；3D 世界坐标静止、严格顺/逆时针符号和 5 秒分段时间均未被当前评测证明。

## 服务器与调用报告

- 服务器：1× NVIDIA A100-PCIE-40GB。
- 模型：VACE-1.3B，480p，13 frames，20 sampling steps，BF16/offload 路径。
- VACE commit：`48eb44f1c4be87cc65a98bff985a26976841e9f3`。
- Wan commit：`9737cba9c1c3c4d04b33fcad41c111989865d315`。
- 真实推理：12 次，全部串行。
- 自动重试：0。
- Seedance / Kling / 其他外部 API 调用：0。
- 推理墙钟合计：1,829.38 秒（30 分 29.38 秒）。
- 含逐条下载和验证：2,135 秒（35 分 35 秒）。
- 峰值显存：17,227 MiB；峰值利用率 100%；最高温度 51°C。
- 输出总大小：6,684,681 bytes。
- 费用：服务器租赁单价未提供，不能编造金额。
- Postflight：VACE/runner 残留进程 0，GPU compute process 0，显存 0 MiB、利用率 0%、29°C；服务器仍保留运行，未擅自关机或释放。

最终生成汇总：`runs/vace_matrix_20260730/final_generation_summary.json`，SHA-256 `41e8d285409a8e88393267429b213f79a44822d16f8922a4a3318e832ec91c62`。

## 输入真实性

| 条件 | Proxy SHA-256 | 输入检查 |
|---|---|---|
| actor_only | `ab5ac2160c0a0b8790e490b549c7301caacefaeccdbff00ca65b3bc2f0eb4e6b` | 15 帧、960×540、3 fps、15 unique；位置/朝向双锁定 |
| camera_only | `07d3a1115e8146e02ad413a5f75668a266b7e03563676f30db208f3433fceb23` | 15 帧、960×540、3 fps、15 unique；两人物世界位置静止 |
| coupled_arc | `1843caf40a17c8a31555aff98ddd232b8db5949079a803eac7327b315cc9bdc6` | 15 帧、960×540、3 fps、15 unique；15 个实际圆弧关键帧 |

准备输入时发现 actor-only 初版虽然 camera 位置固定，朝向仍追踪移动的人物中点。该版本被排除，最终矩阵使用 `control_verified`；这正是多次真实检查而不是单次跑通的价值。

## 12 个真实输出

所有视频都满足：远端/本地 SHA 一致；13 帧；832×480；16 fps；13 个唯一解码帧；空间和时间内容非恒定。

| Job | bytes | SHA-256 |
|---|---:|---|
| actor_only / short / 2026 | 417,450 | `99bfed41074dd7e90e19da2dae253763b8a0eddf2652c2a2d92f9a254134b280` |
| actor_only / short / 2027 | 473,249 | `605a09c58abc8bc13626708395455d762063b11556344fa7b6abc8222fbfcff1` |
| actor_only / structured / 2026 | 426,593 | `03e3f4f8c3fcb05b0248c68beb5f187ba433e3a0880319163ae985bb4cad7de9` |
| actor_only / structured / 2027 | 489,547 | `6ddceb03e67c14b30147d87f19db12fe6f10866cdf68798712a5a8a3406371da` |
| camera_only / short / 2026 | 553,704 | `aa53f7565ce34efaf723bb2dcbb40aa1358d9327a4158523810e35f9f34d1e66` |
| camera_only / short / 2027 | 559,075 | `48f47f4455deb843a2468580e45b951e275c316e8838fe1b9dcd7697bbfef074` |
| camera_only / structured / 2026 | 639,312 | `74e360e9e1d17bc4113d9f28b036f4f579545f0aab9ecc95ba4aedcf066e707b` |
| camera_only / structured / 2027 | 663,691 | `ccbbd7bb9fea13127daa2b25049047bb0c033477e666a576e0abcd3c03461b43` |
| coupled_arc / short / 2026 | 607,331 | `ed6d8cb8ba3d57a8f75f39887fbb697492c4aea21fd1b7ac7d038fac3bb7b9d6` |
| coupled_arc / short / 2027 | 617,383 | `fffe85905de945ef59b4972a33d717212ffb9da89914524a5207013548ce6db6` |
| coupled_arc / structured / 2026 | 609,172 | `2d29adef12b7b35592cc527a284b72d0c5435c9b22084f7f52c1a19946a84978` |
| coupled_arc / structured / 2027 | 628,174 | `9d166dbc4a72559bc79dc7f5bcae7f84ab7cc2170cea77704371f26d29d9312a` |

视频位于 `runs/vace_matrix_20260730/jobs/<job_id>/out_video.mp4`。

## 独立视觉审计

审计实际解码每条视频的全部 13 帧，并重点检查 frame 0 / 6 / 12。联系表：

- `runs/vace_matrix_20260730/audit_tmp/actor_only__all13_grid.png`
- `runs/vace_matrix_20260730/audit_tmp/camera_only__all13_grid.png`
- `runs/vace_matrix_20260730/audit_tmp/coupled_arc__all13_grid.png`

观察结果：

- actor-only 4/4：A 连续右移并缩小与 B 的间距；B 近似稳定；铁轨与红黄线近似锁定。
- camera-only 4/4：两人物相对间距近似稳定且没有可见自主走动；站台/铁轨从一侧斜视经过正中再到另一侧斜视，构图变化与 source proxy 一致。
- coupled-arc 4/4：A 向 B 靠近，同时背景透视发生连续环绕式变化，两类宏观控制同时可见。
- 12/12：A、B 全程可见；未观察到身份交换、人物消失或严重几何形变。
- seed 差异主要体现为蓝色人物饱和度、A 的头部高光/色温和标签轮廓；运动方向高度一致。
- short 与 structured 没有肉眼可辨的宏观控制精度差异。

这里的 `4/4`、`12/12` 是人工二维可观察现象，不是 3D pose ground truth，也不是自然场景泛化结论。

## 严格相机启发式

项目已有 camera evaluator 使用上方背景 crop 的 phase correlation，并以 1.25 为严格置信度门。矩阵 ShotScript 生成早于 Stage 8 schema 变更，原文件缺少 `environment_preset`；首次调用因此真实失败。为不修改哈希锁定输入，评估使用 `audit_tmp/camera_only_eval_shotscript.json` 派生副本，验证其 JSON 除新增 `environment_preset: station` 外与原文件完全相同：

- 原文件 SHA：`748e132b981b0c6ae0555ba80329ca2bc22efdb9e0f696fb038d7b3184d2008a`
- 派生副本 SHA：`d36a55b4341da91cfac2f9e48838b76327299b07960df918ed0b7011ca420507`

结果：4 条 camera-only 输出均为 `inconclusive`。真实 source proxy 同样为 `inconclusive`。部分局部 pair 在置信度足够时被判为 expected sign 的 opposite，这是因为当前 camera 在横移的同时持续旋转看向人物，简单“背景整体水平平移”假设不成立。

因此本轮不能用该启发式宣称 camera truck 精确通过；只能报告输出与 source proxy 的可见透视演化一致。后续应采用 source-to-output 特征轨迹/单应性比较，或直接读取可用的 3D camera pose 条件，而不是继续放宽阈值。

## 负结果与设计问题

### 1. 时间协议不一致

VACE 输出是 13 frames / 16 fps，即 0.8125 秒。Structured prompt 却按 0–1.7、1.7–3.3、3.3–5.0 秒描述。输出时间不足以覆盖这些区间，所以“分段调度是否遵循”必须标记为 `inconclusive`；structured prompt 没有优于 short prompt 也不能直接解释为 prompt 无效。

下一轮短 smoke 应把 prompt 改成 early / middle / late 或归一化百分比。若要真正验证 5 秒时间调度，需要单独审批一个更长帧数的 VACE 试验，而不是直接复制本轮 12 次。

### 2. 真实感提升不足

输出基本保持 Blender primitive 的外观和站台布局，只出现平滑化、饱和度和高光差异，没有明显变成自然电影画面。VACE 在本轮更像高保真控制复现器，而不是最终视觉质量后端。这支持原架构中“Blender/VACE 用于控制验证，Seedance 用于最终画质”的职责划分。

### 3. 现有 camera evaluator 不适配 tracking truck / arc

全局平移启发式无法可靠处理横移与 look-at 旋转同时存在的镜头，也不能从单目二维结果证明严格 3D 顺时针方向。本轮不通过降低 confidence gate 来制造成功结论。

## 下一步建议

1. 先为 Stage 8 三个自然语义场景逐个增加 trajectory 和 control bundle，每个场景展示真实 proxy 后再继续。
2. 把短 VACE smoke 的 timed prompt 改为归一化阶段，保持 13 帧低成本验证。
3. 增加 source-output feature/homography 对齐评估；actor path 继续使用人工或可靠检测轨迹，不把 camera phase correlation 当 pose ground truth。
4. 圆弧控制先保留显式关键帧，不急着引入复杂 multi-agent 或自由 Blender code。
5. 若要验证自然场景最终质量，优先做每场景 1 次受控 Seedance 小样；调用次数和费用在提交前单独汇报并等待批准。
