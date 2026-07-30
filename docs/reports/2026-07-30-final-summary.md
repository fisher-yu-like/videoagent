# VideoActAgent 阶段总结报告

日期：2026-07-30
证据原则：本报告只把仓库中可重新读取、可解码、可核对 SHA-256 的产物列为真实实验；单元测试中的临时视频、拦截 transport 和合成坐标仅属于机制验证。

## 1. 最终交付概览

项目已经形成一条可运行的最小完整主线：

```text
Story
  -> ShotScript
  -> Trajectory Instruction
  -> Deterministic Compiler
  -> Blender Proxy / Control Bundle
  -> VACE 或 Kling/Seedance
  -> Real Video
  -> Manual Observation + Metrics
  -> One Bounded Revision
```

已完成的工程模块包括 ShotScript、Blender 代理、Control Bridge、模块 I/O 检查、ATI 风格轨迹协议、本地轨迹编辑器、确定性轨迹编译器、轨迹控制 Blender Proxy、Kling/Seedance bundle 与提交闸门、真实视频人工观察器、轨迹指标评估器、一次有限修订、VACE 输入适配和正式矩阵规划器。

## 2. 分阶段实现与真实证据

### Stage 1：ShotScript → Blender Proxy

- 用三镜头、双人物站台故事实现 `truck_right`、`dolly_in`、`arc_clockwise` 及跨镜头人物连续性。
- 本机真实调用 Blender 5.1.2，产出 960×540、45 帧、3 fps、15 秒 MP4、可重开的 `.blend` 和检查帧。
- 最终 MP4 SHA-256：`e9da8b7a6c8211033541b02d217c23d5731277f69c69adbdef3dcbb2291029fe`。
- 修复了 Blender 5.1 渲染引擎枚举、FFmpeg/PNG 动态格式冲突、Action API 变化和第三镜头构图裁切。
- 外部 API：0；服务器/GPU：0。

### Stage 2：Control Bridge

- 把真实 `.blend` 和 ShotScript 转为三个 shot 的普通提示、电影提示、分时提示、首尾帧和 proxy MP4。
- `control_bundle.json` SHA-256：`febf93a5ed5e1a3e8611322c280126239b57164ec56e429708f283d03f7aa617`。
- s01 proxy：960×540、15 帧、3 fps、5 秒，SHA-256 `2fe40427bed79725cc18b0394d0c0fb2ab9943fe542d1196c0d2b0815c168b2e`。
- 修复了脚本入口导入、Blender 实际输出命名和角色名规范化问题。
- 外部 API：0；服务器/GPU：0。

### Stage 3：后端网关与第一条语义基线

- 基于原有 `kling_demo.py`、`seedance_demo.py` 提取能力矩阵、payload builder、无自动重试提交、查询、下载和不可变运行记录。
- 第一条真实 Kling cinematic prompt 基线：1 次 POST、4 次 GET、1 次下载；结果 1280×720、121 帧、24 fps、5.042 秒。
- 结果 MP4 SHA-256：`f3a95d76e898858726be8651f0472bafd0ff6a33e6c0e6743f5e23730aae35f3`。
- 真实画面完成“两人在站台会面并握手”的语义，但 `camera truck right` 不明显，因此只证明 prompt-only 语义生成可用，不证明相机控制通过。
- 另有一次历史 Seedance 最小 smoke 尝试在取得 task ID 前进程超时，外部是否接受未知；没有重试，也不计为可证实成功提交。

### Stage 4：相机背景运动启发式

- 对同一真实 Kling 视频的首/中/尾帧做 FFT phase correlation。
- 首到尾：`dx=-59 px`、`dy=0 px`、confidence `1.0803985977360666`。
- 用户允许的放宽阈值 1.05 得到 `matched`；固定严格阈值 1.25 为 `inconclusive`。闭环只消费严格结论，放宽结果仅用于诊断。
- 报告 SHA-256：`f3b0e1d41e03affc04334b9c6c891b3f04ced8aea8951d09f626a88a1403c390`。
- 该测量不是相机位姿真值，不能表述为运镜已通过。

### Stage 5：官方 Baseline 源码固定

- VACE 固定 commit：`48eb44f1c4be87cc65a98bff985a26976841e9f3`。
- ReCamMaster 固定 commit：`fcf98bc86e876bb534518cd99e8a65b282f0f16e`。
- 审计记录 SHA-256：`11bfb5a20a918c898439e1019a6f1d9f845890eb267ff20758479db60d5a1017`。
- 修复了 Git dubious ownership 环境下的只读审计：每次命令使用 checkout 级 `safe.directory`，不修改全局配置。
- 本阶段只证明源码和选定入口可复现；ReCamMaster 尚未推理。

### Stage 6：VACE 输入、服务器预处理与真实推理

本地适配：

- 从真实 Stage 2 bundle 生成 `src_video`、全白 `src_mask`、首尾参考图和 prompt。
- 当前 VACE job SHA-256：`2ecc5fe51b9fe9d2e9bf608a0829c68b316fb45ae1b8a444bc661375a6cd5063`；`source_validation_passed=false`、`inference_success=false`，没有伪造当前成功标记。
- 加固了输出/输入别名检查、hardlink 防护、唯一临时文件、失败清理、并发发布、报告到 validated job 的路径/字节/SHA/摘要绑定。

历史真实预处理：

- A100 上真实调用固定 VACE processor；返回 source/mask tensor `[3,13,480,832]`，CUDA float32，有限值；耗时约 3.128 秒。
- 历史通过报告 SHA-256：`813046fc4441ed249f21e598d82f32e172dc3038401cdc4dd591edcf3c6d7dae`。
- 该结果因早期 validated job 未绑定报告字节而归档为 `.historical-old-job.json`，不作为当前 job 的成功标记。
- 两个保留失败分别来自 Windows/Linux CRLF/LF 字节差异和手工估计 frame-id 与真实 Decord 采样不一致；修复依据是固定 Git blob 与真实上游输出，而不是降低校验。

真实 VACE 推理：

- 服务器：NVIDIA A100-PCIE-40GB；PyTorch 2.5.1+cu124；CUDA build 12.4。
- 第一次推理到真实模型路径后因 FlashAttention assertion 失败；随后安装官方 `flash-attn 2.7.4.post1` wheel，并通过 BF16 CUDA varlen smoke。
- 第二次真实推理退出码 0；20/20 采样步完成；墙钟时间 2 分 31.99 秒；最大 RSS 24,879,420 KiB；峰值显存 17,225 MiB；峰值 GPU 利用率 100%；峰值温度 50°C。
- 输出 511,428 字节，SHA-256 `47781097969b8dd801fabcb95f5f6124d72d25d90a8c7128379233bf4e405430`。
- 独立解码：13 帧、832×480、16 fps、13 个不同帧哈希，空间和时间均非恒定，全部有限；运行后模型进程数和 GPU 进程数均为 0。
- 这证明 VACE 1.3B 能在该 A100 40 GB 上完成一次真实生成，不证明其人物/相机控制遵循度。

### 轨迹 Task 1–5：协议、画布、编译与 Blender Proxy

- 轨迹协议使用 `normalized_0_1_top_left` 坐标，当前规范化轨迹 121 个样本、5 秒，SHA-256 `52792642f4887a78cc332898714fa877d3d6336073302e3a127cd69b7d8921e6`。
- 浏览器真实完成 polyline 绘制、拖拽、`Finish`、`Save`、`Load`；最终时间仍为 `[0.2,0.8]`。
- 浏览器轨迹 SHA-256：`856f7b1e92587a9ddb85f68004e23bd555318ae8318f062c52585f9529468bb8`；叠加图 SHA-256：`5f04d30ea04a0c1ceccbb2340d8063dc1b9271c81811247097553c252e9adbf5`；验收清单 SHA-256：`abb26570b7d64324d7405627e7cbd20a2084cf24de89fee0c87d1150dce3ab61`。
- 编译器输出 `compiled_control.json` SHA-256：`50b66351a657b768723c2fe8712093fde9bcefd6c87f00ce4e8891acddb4063b`。不支持的 local deformation 会 fail closed，不会静默降级。
- 真实轨迹 Blender Proxy 同时应用 9 个相机关键帧和 3 个人物关键帧；输出 MP4 960×540、45 帧、3 fps，SHA-256 `8ecf7cbce19e537d722e9c0321a66b933722fca7ea8eb3b1063645ec45b7dee3`。
- 首次真实检查发现相机 Y 方向映射错误；修复 top-left Y 映射后，`.blend` 中相机有符号转角为 `-92.0844°`，与 clockwise 语义一致。
- 编辑器和渲染器采用锁、临时目录、原子发布及 source/output 别名防护；失败会保存 stdout/stderr 和 failure manifest，不用半成品冒充成功。

### 轨迹 Task 6：Kling/Seedance 原始 pilot

- 为两个后端准备严格哈希绑定 bundle；Kling bundle SHA-256 `edf29840aa49e133119b98fe61168805cf02d0751f42263270d8b659414aaa75`，Seedance bundle SHA-256 `faeccd2077b6fa54e8f1b97a0070fceb8fef02910aa7e0f1a2c397bfcd3a3238`。
- 提交前重新验证轨迹、编译控制、提示文本和七项来源 SHA；篡改提示、unsupported、trajectory SHA 或文件替换均在读取凭据和联网前拒绝。
- 原始 pilot：Kling 1 次 POST、2 次 GET、1 次下载；Seedance 1 次 POST、2 次 GET、1 次下载；两者都到达 `success`。
- Kling MP4：9,870,702 字节，SHA-256 `5cfce7a0a887bad62904dd05f9934f647770defa11b9c066be5636f1c34bd180`。
- Seedance MP4：2,990,207 字节，SHA-256 `0cc51800c70f45f94314210ac590041a1e3f1ce3a913a375c4f4d24ce960bae2`。

### 轨迹 Task 7：真实视频人工观察与指标

- 对两个真实 MP4 解码帧 0、30、60、90、120；每帧由人工标注人物位置或显式遮挡，没有自动检测器或合成坐标。
- Seedance 第 0 帧人物不可见，被记录为 occluded，不做插值伪造。

| 指标 | Kling 原始 | Seedance 原始 |
|---|---:|---:|
| compared samples | 121 | 91 |
| occluded samples | 0 | 30 |
| direction cosine | 0.876756 | 0.637167 |
| direction match | true | true |
| mean distance | 0.042759 | 0.096872 |
| normalized endpoint error | 0.005170 | 0.112788 |
| normalized DTW | 0.004883 | 0.059630 |
| observed arrival | 0.658333 | unavailable |
| arrival error | 0.175000 | unavailable |

- Kling 基本遵循左到中央路径，但提前到达；Seedance 保留大方向，但端点/路径误差更大，且未进入终点容差。
- Kling evaluation SHA-256：`a0be8fc5f2caf3e089f18e9adada0d808b645dd0dc7317307f3861720b2513bf`。
- Seedance evaluation SHA-256：`4281bf331e18089dc63b2673aef8ff2266f4ea75516c9c52b298db5e92b99d7b`。

### 轨迹 Task 8：一次有限修订与正式矩阵

- 每个后端只执行 generation-1 一次，操作固定为 `split_time_segments` 加 `preserve_matched_control`；seed、模型、时长和分辨率不变。
- 修订提交：Kling 1 次 POST、2 次 GET、1 次下载；Seedance 1 次 POST、3 次 GET、1 次下载；都到达 `success`。

| 后端 | 原始 mean / endpoint / DTW | 修订 mean / endpoint / DTW | 结论 |
|---|---|---|---|
| Kling | 0.042759 / 0.005170 / 0.004883 | 0.075576 / 0.057554 / 0.053441 | 变差 |
| Seedance | 0.096872 / 0.112788 / 0.059630 | 0.144585 / 0.125810 / 0.102237 | 变差 |

- 修订 Kling MP4 SHA-256：`53c3bf8b67778882a5cefed654aebefc5ee0a5b317fc91920f48f340870778ca`；evaluation SHA-256：`4614e565d1b941f73636a9d7d8a09ed1921bab941e34118bf149d997876cbb5d`。
- 修订 Seedance MP4 SHA-256：`c6fbb1d98e14c1f90436ab79e3ee796bcaccc153bc7902d85f2925e4587f9deb`；evaluation SHA-256：`5f48c29d41f350dc5e8a7bc395c6633bb85bd684544ed0b7878c6abe583383d8`。
- 一次有限修订没有提升轨迹遵循度。这是有效的真实负结果，不是软件失败；因此没有静默换 seed 或继续自动迭代。
- 正式矩阵固定为 4 场景 × 2 后端 × 3 条件 = 24 作业。两份 pilot 人工复核已验证，但 24 个逐条件 bundle 均未就绪：`ready_job_count=0`、24 个 `preparation_required`、`submission_allowed=false`、`network_called=false`、`submitted=false`。
- 当前真实复核矩阵计划 SHA-256：`53007726830ab0f8ae80b45cb8198fb1182438cd4125295d5776daedcfbe99bf`。它是计划证据，不是 24 次实验结果。

## 3. API 与服务器使用汇总

### 可证实的成功生成提交

| 组别 | POST | GET | 下载 |
|---|---:|---:|---:|
| Stage 3 Kling cinematic baseline | 1 | 4 | 1 |
| 原始轨迹 pilot：Kling | 1 | 2 | 1 |
| 原始轨迹 pilot：Seedance | 1 | 2 | 1 |
| 一次修订：Kling | 1 | 2 | 1 |
| 一次修订：Seedance | 1 | 3 | 1 |
| **合计** | **5** | **13** | **5** |

补充：存在一次历史 Seedance 尝试，在 task ID 出现前进程超时。无法证明网关是否接受，因此不计入上表的成功 POST，也没有重试。离线 `audit.json` 中的 `network_called=false` 表示“审计过程没有联网”，不是说原始提交未联网。

### 服务器

- 租用配置：1 × A100 40 GB、12 CPU、90 GB 内存、200 GB 硬盘。
- 完成固定 VACE 源码预处理、模型权重下载、FlashAttention 修复、BF16 CUDA smoke 和一次 VACE/Wan2.1 1.3B 成功推理。
- 保留了真实失败日志：初次 FlashAttention assertion 失败；修复后才重新运行并通过。
- 服务器成功输出已下载到本地并核对远端/本地 SHA 一致；完成后 GPU/模型进程清空。

## 4. 测试与审查统计

最终在当前工作树执行了完整回归（通过 `cmd` 保留原始退出码）：
`python -m unittest discover -s tests -q`，退出码 `0`，共 `306` 个测试，`OK`，其中 `5` 个 Windows 符号链接权限 skip。测试输出保存为本地临时日志 `full_test_cmd.log`；它不是实验结果，也不替代 `runs/` 中的真实视频证据。

下列是各阶段最新报告中的聚焦验证，测试集合存在重叠，不能把数字相加当成仓库总测试数：

| 范围 | 结果 |
|---|---|
| Stage 4 camera evaluator | 13/13 passed |
| 旧 Stage 7 shot closed loop | 10/10 passed |
| 加固后的 Stage 6 + module I/O 回归 | 72 passed，0 failed，1 个 Windows symlink 权限 skip |
| 轨迹编辑器 + schema + acceptance | 56 passed，0 failed，2 个 Windows symlink 权限 skip |
| 轨迹编译相关严格回归 | 65 passed，0 failed，1 个 Windows symlink 权限 skip |
| 轨迹 Proxy | 4 个 Task 5 + 1 个 Stage 1 真实 Blender 测试通过 |
| Task 7 observer/evaluator | 28 passed，0 failed，1 个 Windows symlink 权限 skip |
| 修订 API bridge + backend gateway | 32 passed，0 failed，0 skipped |

Windows 的 symlink 跳过是当前账户缺少创建符号链接权限；仓库仍保留不需要该权限的路径逃逸和真实 hardlink 测试。真实 Blender 集成测试会启动 `D:\blender\blender.exe`；API success-path 单元测试会拦截 transport，因此它只验证提交机制，不能替代上表单独列出的真实 API 产物。

## 5. 真实负结果与未完成项

1. 纯文字 Kling 基线的叙事正确，但指定的 `truck_right` 不明显；严格相机启发式仍为 `inconclusive`。
2. 两个后端的一次有限修订都比原始轨迹结果更差；闭环策略需要新假设，不能仅靠增强分时措辞。
3. Seedance 原始结果开头人物不可见，30 个公共时间样本被明确计为 occluded。
4. 当前加强后的 VACE job 尚未重跑服务器预处理；历史通过报告只能作为历史证据。
5. VACE 成功输出只完成了解码、非恒定性和资源验证，尚未完成同一套人工轨迹/相机遵循度评估。
6. ReCamMaster 尚未运行推理；训练没有执行。
7. 24 作业矩阵尚未执行。缺少 4 场景 × 2 后端 × 3 条件的完整、逐项哈希绑定 bundle，调用价格也未提供。

## 6. 后续建议

1. 先为 `city_crosswalk`、`forest_path`、`studio_room` 各完成 ShotScript、轨迹、Blender Proxy 和三种条件 bundle；逐模块看预览，不先启动全矩阵。
2. 用当前 Task 7 方法评估 VACE 成功输出，将“模型能跑”升级为“结构/人物/相机是否遵循”的可比较指标。
3. 对真实负结果做单变量实验：原始 prompt 与 `split_time_segments` 之外，只新增一个可解释操作，例如减小幅度或把 orbit 简化为 truck；每次固定 seed/时长/模型。
4. 若需要更强的轨迹创新，优先接入 ATI/Wan 的显式中间轨迹张量通道，保留现有 API prompt 路径作为弱控制对照，而不是在闭源 API 上继续堆叠复杂多 Agent。
5. ReCamMaster 只用于少量相机重渲染对照；不要与 VACE 同时塞进第一版主图，避免架构失控。
6. 只有当 24 个 bundle 全部通过验证、成本已填写、用户明确允许后，才运行正式矩阵。每个作业保持一次 POST、零自动重试，并立即保存任务记录、视频、哈希和人工评估。

## 7. 参考依据

架构和实现参考 ATI、CamTrol、SceneCraft、MovieAgent、Camera Artist、VACE/Wan2.1 与 ReCamMaster。论文、官方代码链接、采用方式和未采用边界见仓库根目录 `README.md`；逐阶段原始报告位于 `docs/reports/`。
