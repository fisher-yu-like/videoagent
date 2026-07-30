# 全链路 24 条真实实验设计

## 目标与边界

从 8 个已验收的人工配对 story prompt/ShotScript 出发，复用 `whole_story_v4` 的完整 Blender proxy，分别得到 Kling、Seedance、VACE 的真实视频并执行同一套媒体完整性和人工观察流程。

矩阵共 24 条：

- 8 个 story × Kling T2V；
- 8 个 story × Seedance T2V；
- 8 个 story × VACE source-video control。

Kling/Seedance 只消费文本，VACE 实际消费 proxy；报告不得把三者描述为相同 conditioning，也不做虚假的同-seed公平性结论。prompt 与 ShotScript 当前仍是人工配对，不声称自然语言已自动编译为 code。

## 执行结构

新增一个 whole-story experiment orchestrator，但不修改已冻结的 trajectory compiler：

```text
whole_story_v4 manifest
  -> immutable experiment manifest
  -> backend adapter
       Kling / Seedance: prompt_only payload
       VACE: source_video job, seed=2026
  -> real submit / inference
  -> query and download
  -> decoded media completeness gate
  -> fixed-frame human observation
  -> actor and camera reports
  -> 24-video summary and contact sheet
```

每个 job 使用 `story_id + backend` 作为唯一身份，不出现 `shot_id`。所有输入先复制为不可变 snapshot，再创建运行目录。任一来源在提交前发生变化即拒绝运行。

## 分阶段放行

### A. 本地 adapter 与预检

API 生成提交 0 次，服务器推理 0 次。完成 24 个 dry-run job、payload/hash 审计、结果接收器和完整性门的真实本地测试。dry-run 只能证明合同可构造，不能算生成成功。

### B. 6 条 canary

选择：

- `station_reunion`：静止相机、人物运动；
- `studio_formation`：静止人物、相机运动。

执行 4 次 API 生成提交（2 story × Kling/Seedance）和 2 次 VACE A100 推理。完成下载、媒体检查和人工观察后停止，向用户报告并等待许可。

### C. 剩余 18 条

只有用户批准 canary 后执行其余 12 次 API 生成提交和 6 次 VACE 推理。失败任务不自动重试、不换 seed、不覆盖原运行。

## 调用与资源上限

- Kling/Seedance 生成提交：总计 16 次，每 job 恰好 1 次；
- 状态查询：每 job 最多 4 次，总计最多 64 次；
- 下载：每个完成 job 最多下载 1 个视频；
- VACE：总计 8 次真实推理，固定 seed 2026，顺序使用 1 张 A100 40GB；
- VACE 先对 canary 执行 81 帧、16 fps、约 5 秒显存探针；OOM 或输出不完整时停止，不缩短成 13 帧冒充通过；
- API 和服务器均为零自动重试。

状态查询是网关 HTTP 请求，必须与 16 次生成提交分开计数。服务器运行需保存峰值显存、墙钟、退出码和完整日志。成本未知时报告 unknown，不估造金额。

## 完整性和评分门

每条下载或推理视频先真实解码，只有同时满足以下条件才允许运动评分：

- `0.95 <= decoded_duration / requested_duration <= 1.05`；
- 分辨率符合该后端声明；
- 首、中、末关键时间可解码；
- 人工观察帧数大于 0。

不满足时记录 `incomplete`，不得计算轨迹或相机通过率。人工记录数和插值采样数分开保存。

完整视频固定观察首、25%、50%、75%、末五个时刻：

- 人物：每个可见人物的归一化中心点或 `occluded`；
- 相机：使用背景相位相关产生测量，再人工检查方向是否可解释；
- 单条报告分开给出 actor、camera 和 completeness，不合并成一个掩盖失败的总分。

当前人物是 Blender 代理圆柱，而生成视频可能变成人类；观察只使用屏幕位置和可见性，不依赖身份识别模型的伪标签。

## 证据格式

每个 job 保存：

- experiment manifest 与来源 SHA-256；
- backend conditioning 和 adapter 版本；
- 实际 request/payload；
- submit response、task ID 和每次 query response；
- API 提交/查询/下载次数；
- 原始 MP4、字节数和 SHA-256；
- 解码媒体报告；
- 五时刻人工观察；
- actor/camera 评估；
- `complete`、`incomplete`、`failed` 或 `unknown` 状态；
- API/服务器墙钟和 VACE 峰值显存。

总表按 backend、story 和失败类型汇总，同时生成 24 条首/中/末帧网格。旧 `whole_story_v1-v3` 只作为 superseded 调试证据；正式输入绑定 `whole_story_v4`。

## 停止条件

以下任一情况立即停止当前阶段并报告，不自动继续：

- payload 与批准的 conditioning 不一致；
- 来源、request 或结果哈希不匹配；
- canary 任一后端无法形成可解码完整视频；
- VACE 81 帧在 A100 40GB 上 OOM；
- API 返回权限、余额、模型或网关合同错误；
- 查询达到 4 次仍无终态；
- 发现会让剩余矩阵数据不可比较的 adapter 缺陷。

停止不会扩大权限；修复后是否重试由用户另行批准。
