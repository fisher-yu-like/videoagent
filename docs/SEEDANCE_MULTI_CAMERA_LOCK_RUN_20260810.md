# Seedance 四机位一致性修复实验（2026-08-10）

本记录对应 indoor_market_exchange 的 approved revision_029 storyhuman Proxy。Proxy 使用一个 Blender shared world，四个机位均有真实 MP4、state log、camera log、asset/coupling log；ProxyVerifier 通过，Proxy VLM approve。Seedance 每个 camera_id 独立提交，不覆盖旧 run。

## 修复顺序

1. camera-specific structural-lock prompt：在原 appearance-only prompt 后加入 camera role、target、实体全集、锁定 reference plate、禁止新增/合并实体等约束。
2. identity-anchor：每个 camera task 额外发送 master Proxy 作为共享 identity/world anchor；机位 Proxy 作为 camera motion reference。
3. real-master anchor：复用上一轮真实 master MP4 作为身份/材质锚点，只提交 lateral、reverse、elevated 三个新任务。
4. camera-first：将当前机位 Proxy 放在第一 reference，真实 master 放在第二 reference，并明确第二 reference 不得覆盖机位构图。

## 真实结果

| endpoint | 真实模型调用 | 媒体核验 | 最终 VLM |
|---|---:|---|---|
| [camera prompt](../runs/results/e2e_seedance_storyhuman_camera_prompt_20260810/) | submit 4 / query 124 / download 4 | 四路 MP4 均存在、1280x720、约 5 s、24 fps、无黑帧 | revision_requested |
| [identity anchor](../runs/results/e2e_seedance_storyhuman_identity_anchor_20260810_retry/) | submit 4 / query 130 / download 4 | 四路 MP4 均通过文件与 ffprobe 检查 | revision_requested |
| [real master anchor](../runs/results/e2e_seedance_storyhuman_real_anchor_20260810/) | submit 3 / query 97 / download 3，master 复用 | 四路 MP4 均通过文件与 ffprobe 检查 | revision_requested |
| [camera-first](../runs/results/e2e_seedance_storyhuman_camera_first_20260810/) | submit 3 / query 98 / download 3，master 复用 | 四路 MP4 均通过文件与 ffprobe 检查 | revision_requested |

最后一轮的四个结果：

- [master](../runs/results/e2e_seedance_storyhuman_camera_first_20260810/complex_scene_suite_20260810_025110/indoor_market_exchange_20260810_025110/seedance/camera_tasks/indoor_market_exchange_master/result.mp4)
- [lateral](../runs/results/e2e_seedance_storyhuman_camera_first_20260810/complex_scene_suite_20260810_025110/indoor_market_exchange_20260810_025110/seedance/camera_tasks/indoor_market_exchange_lateral/result.mp4)
- [reverse](../runs/results/e2e_seedance_storyhuman_camera_first_20260810/complex_scene_suite_20260810_025110/indoor_market_exchange_20260810_025110/seedance/camera_tasks/indoor_market_exchange_reverse/result.mp4)
- [elevated](../runs/results/e2e_seedance_storyhuman_camera_first_20260810/complex_scene_suite_20260810_025110/indoor_market_exchange_20260810_025110/seedance/camera_tasks/indoor_market_exchange_elevated/result.mp4)

最后一轮 API 计数为 submit=3、query=98、download=3；master 是上一轮真实结果的 immutable reuse，不是伪造或重新生成。每条视频的 SHA-256、probe、blackdetect 和 task 记录均在 endpoint 的 `scene_summary.json`、`final_video_verifier_report.json`、`seedance/camera_submission_summary.json` 中。

## 失败原因

VLM 的最终反馈不是“视频损坏”，而是内容不满足计划：四路仍常被 Seedance 重建成近似 front-facing 镜头；vendor/customer/helper 角色混淆；纸张在 cart 到达前出现；lateral/reverse/elevated 没有呈现对应的 profile、reverse、overhead coverage。Blender `camera_log.json` 已证明 Proxy 的 authored/applied camera position 和 look-at rotation 是不同的，问题发生在独立 reference-video 任务的后端条件化，而不是 Proxy 日志或媒体下载。

结论：提示词和 anchor 顺序的四次真实修复均不能把独立 Seedance task 变成共享世界多视角生成。当前不能把“四个 MP4 下载成功”报告成“四机位一致性通过”。下一步若要达到验收条件，应改为模型原生 multiview/多视频联合条件化，或使用能显式锁定 identity 与 camera control 的后端；不应继续无 seed 地重复提交同一接口。
