# 实验记录与证据边界

## Whole-story 离线基线（2026-07-30）

命令：`python run.py configs/whole_story_suite.json`

结果：`runs/work/whole_story_v4/summary.json`

- 8 个不同 prompt、8 个不同 ShotScript、8 个不同 motion signature；
- 8 个真实 Blender MP4 均为 15 帧、3 fps、5.0 秒、960×540；
- 8 个 MP4 SHA-256 互不相同，且与各自 manifest 复算一致；
- 每条首/中/末检查帧至少有两帧哈希不同；
- 8/8 Blender 人物与相机起止位置和 ShotScript 匹配；station_reunion 的相机位置与朝向均锁定；
- 总览包含 8 条 × 首/中/末 3 帧，SHA-256 `d82358b3972633d823e8551c834a31428a14ef4ef83fe42943480283f6439049`；
- API 调用 0 次，服务器推理 0 次。

本次只验收输入多样性、proxy 可播放性和数据合同，不声称生成模型已经遵循人物或相机控制。
三后端 JSON 仅为离线输入清单；当前逐镜头提交器和 VACE 预处理器尚未消费这个 whole-story schema。

## 已保留的 VACE/A100 证据

旧 12-run 矩阵的精简复现信息保留如下，原始本地证据位于 `runs/vace_matrix_20260730/`：

- 条件：actor-only、camera-only、coupled-arc × short/structured prompt × seed 2026/2027，共 12 个 job；
- 12/12 次真实 GPU 推理成功且 12/12 MP4 可解码；每条 13 帧、16 fps、832×480；
- 纯推理总墙钟 1829.38 秒，包含下载检查的 batch 墙钟 2135 秒；
- 峰值显存 17227 MiB，峰值 GPU 利用率 100%，峰值温度 51°C；
- VACE commit `48eb44f1c4be87cc65a98bff985a26976841e9f3`；Wan commit `9737cba9c1c3c4d04b33fcad41c111989865d315`；
- matrix SHA-256 `83394a89255257f3bf8247ff5635efc4ea912971ba181409c2aef6f1fae34735`；
- `final_generation_summary.json` SHA-256 `41e8d285409a8e88393267429b213f79a44822d16f8922a4a3318e832ec91c62`；
- `preflight.json` SHA-256 `72baf997d3879d07a160fec6d7c913e67d5b9d71214b7f691b349a7c47247ba7`。

这只证明真实推理、传输、哈希和解码完整；`control_effect_evaluation.completed=false`。已有 seed2025 相机评估的 strict verdict 为 `inconclusive`，不能据此声称相机控制成功。服务器租金未提供，因此只报告 GPU/墙钟，不虚构成本。

## 旧评分为何不能支持端到端和因果结论

旧 Blender trajectory proxy 是 15 秒三镜头：

- 路径：`runs/trajectory/s01/proxy_blender_fixed_20260730T001819CST/trajectory_proxy.mp4`
- SHA-256：`8ecf7cbce19e537d722e9c0321a66b933722fca7ea8eb3b1063645ec45b7dee3`

但旧 gateway 从 ShotScript 取 `s01`，请求时长为 5 秒，且 Kling / Seedance 请求只发送文本，没有发送 proxy、首尾帧或源视频。因此四条约 5.04 秒结果不是下载截断，而是请求本来只覆盖第一段：

| 后端/条件 | MP4 SHA-256 |
|---|---|
| Kling 原始 | `5cfce7a0a887bad62904dd05f9934f647770defa11b9c066be5636f1c34bd180` |
| Kling 修订 | `53c3bf8b67778882a5cefed654aebefc5ee0a5b317fc91920f48f340870778ca` |
| Seedance 原始 | `0cc51800c70f45f94314210ac590041a1e3f1ce3a913a375c4f4d24ce960bae2` |
| Seedance 修订 | `c6fbb1d98e14c1f90436ab79e3ee796bcaccc153bc7902d85f2925e4587f9deb` |

旧观察器使用稀疏人工记录再插值：两条 Kling 各有 5 个坐标点击并插值为 121 点；两条 Seedance 各有 4 个坐标点击和 1 个首帧遮挡记录，比较点数为 91。它们都不是 121/91 次独立人工观察。`t=frame/(N-1)` 对 5 秒 s01 的归一化时间映射本身正确，问题是指标只覆盖 s01 的人物位置，不覆盖 s02/s03，也不验证相机控制。

## “修订后更差”的可解释范围

在这一个 s01 样本中，修订视频的描述性人物指标确实更差：Kling 的端点/均值偏差增大；Seedance 人物先向右后反向向左，方向余弦下降。但不能把差异归因于修订策略：

- 当前请求和本地 payload builder 没有设置或暴露 seed；
- Seedance 原始与修订实际 seed 分别为 80784 与 53450；
- Kling seed 未知；
- 修订只追加了笼统的 beginning/middle/ending 文本，没有精确时间区间和位置；
- 不存在同条件的人工文本 baseline。

因此以下历史结论全部撤销：同 seed 公平比较、修订策略导致退化、15 秒全片完成、相机控制已验证、121/91 个独立人工观察、三条件公平对照。四条真实 MP4、媒体元数据、哈希、合计 18 个坐标点和 2 个遮挡状态可作为描述性 s01 证据保留，本轮不重新生成它们。四条视频按 121 帧/24 fps 的计帧时长约为 5.04 秒；Seedance 容器的 stream duration 为 5.09 秒，不影响“请求仅覆盖 5 秒 s01”的结论。

## 当前全链路实验（2026-08-10）

### Proxy 阶段

- `revision_021–024`：真实 ACCAD BVH 导入、四肢重定向、直立约束和有界 side-step overlay；每个 revision 都产生独立四机位 Blender MP4。
- 确定性检查：每轮 20 passed、0 failed、2 unknown；VLM 每轮 1 次，均为 `revision_requested`。
- `park_badminton` canonical 和 skeleton 两个真实 Proxy 试验也均被 VLM 拒绝，问题集中在网/球拍/人物接触、spectator 角色和机位覆盖。

### Seedance 端到端

使用历史已获 VLM approve 的 plaza `revision_005` Proxy，复制到新的不可变目录后按 camera_id 独立提交：

`runs/results/e2e_seedance_approved_proxy_20260810_174500/`

| 阶段 | 真实结果 |
|---|---|
| 上传 | Uguu 成功；tmpfiles 首次真实连接被远端关闭 |
| submit | 4 次，4 个独立 task |
| query | 84 次（初始 80 次 + 同 task 续查 4 次） |
| download | 4 次，全部得到 MP4 |
| 媒体检查 | 四条均 1280×720、121 帧、24fps、5.086 秒、无黑帧 |
| 最终 VLM | `revision_requested` |

最终 VLM 的问题不是视频文件损坏，而是独立 Seedance task 生成了不同人物、服装、场景布局和镜头语义。由此确认：当前“每机位独立 reference-video task”能保证提交与下载审计，但不能保证模型内部的跨机位身份/环境一致性。该问题不能靠伪造轨迹指标或重复提交掩盖。

本轮完整证据见 [E2E_SEEDANCE_RUN_20260810.md](E2E_SEEDANCE_RUN_20260810.md)。上传 fallback 改用 Uguu、默认轮询增至 30 的代码修复已通过相关测试；当前相关测试累计 65 passed。

## 多场景与多 prompt Proxy 试验（2026-08-10）

为避免只依赖 plaza 单一 prompt，本轮对三个不同故事分别生成共享世界四机位 canonical Proxy，并各做一次真实 VLM 审核：

1. `plaza_dance_circle`：人物、背包、speaker 互相遮挡，侧步/挥手和接地关系不够清楚；
2. `park_badminton`：两名球员、短网、球拍、shuttlecock、spectator 的空间关系和 serve/return 接触不够清楚；
3. `indoor_market_exchange`：reverse 机位被 counter 遮挡，手推车轮子未表现出地面接触，vendor/helper 关系不稳定。

三条 VLM 均为 `revision_requested`，Seedance/Kling 均为 0。该轮说明问题具有场景依赖性：不能用一个通用 Appearance-only Prompt 解决，需要对每个场景单独修 CameraTrajectoryPlan、角色/物体轨迹和接触约束。三条真实 Proxy 的目录和反馈已写入 `runs/results/complex_scene_suite_20260810_181253/`。

## 下一实验门

轨迹处理保持冻结。只有 whole-story 输入先通过人工验收，并重新定义完整时长协议、真实视频输入方式和人工观察规则后，才讨论新的 API 小样本或 A100 显式控制实验。任何 API 调用或服务器推理必须先报告预算并获得允许。

## indoor_market_exchange revision_025–027 + Seedance 真实端到端（2026-08-10）

本轮连续修复 Proxy：`revision_025` 修复 reverse 遮挡和手推车接地，`revision_026` 将 counter 改为桌面+四脚并拉远 elevated，`revision_027` 增加 vendor/helper 深度层、customer 停顿举手、cart/box 耦合停顿和纸张抛起。revision_027 的四机位 Proxy 获得 VLM approve。

Endpoint：`runs/results/e2e_seedance_indoor_market_revision_027_20260810/`

Seedance 按 camera_id 独立提交四个 reference-video task：`submit=4, query=125, download=4`，无重新 submit。四个 MP4 均真实下载、1280×720、24 fps、121 frames、约 5 秒、黑帧 0。修复了 verifier 将 `24.0` 与 `24/1` 字符串比较导致的假失败，并保留真实媒体哈希和源 manifest 哈希。

最终 VLM 调用 1 次，`revision_requested`。四个独立任务各自生成了真人视频，但没有保持跨机位人物/推车/道具/环境一致，问题类别为 scene、character、object、camera、physical。该失败必须回到多视角一致性后端或共享外观锚点，不能改写为成功，也不能用重复提交掩盖。详细证据见 [SEEDANCE_MARKET_REVISION_027_RUN.md](SEEDANCE_MARKET_REVISION_027_RUN.md)。

## storyhuman Proxy + Seedance revision_029（2026-08-10）

通过 Blender MCP 检查 revision_027 后确认：CesiumMan 仍是低模人物，箱子/行李类物体只使用绝对轨迹。新增 `storyhuman` 圆润人体代理和根级 coupling materialization；revision_028 修复客户与车重叠和轮子接地，revision_029 分离 helper 并明确两张纸的完整事件。Proxy VLM 为 `approve`。

使用 revision_029 的 appearance prompt（增加固定 cast/prop identity bible）真实提交 Seedance：4 submit、118 query、4 download，无重新 submit。四条 MP4 媒体检查通过；最终 VLM 仍判定 `revision_requested`，因为独立 task 之间的人物、推车、箱子和市场环境不一致。该轮详细证据和 prompt 见 [SEEDANCE_STORYHUMAN_REVISION_029_RUN.md](SEEDANCE_STORYHUMAN_REVISION_029_RUN.md)。
