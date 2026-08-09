# 参考视频接口探针

历史 `runs/work/full_chain_24_v1` 的请求文件只有文本 `content`，因此那轮
Seedance/Kling 实验是 T2V，不是 Proxy 优化。

2026-08-08 使用旧的公开 `clay.mp4` 做了单独的参考视频能力探针：

- Seedance `Doubao-Seedance-2.0`：`text + video_url`（`role=reference_video`）
  被网关接受，任务号为 `task-r32kbehluis2d2q`；最后一次查询仍为
  `running`。
- Kling `Kling-V3-omni`：同形状请求返回 HTTP 400 `not support`；没有回退
  到 T2V，也没有重试。

探针输入是旧视频（960×540），不是当前 `revision_004` 的 640×360 proxy，
所以不能作为当前质量结论。完整请求、响应、错误、查询和哈希见
`runs/results/reference_video_probe_20260808_01/REPORT.md`。
