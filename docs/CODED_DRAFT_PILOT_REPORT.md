# Coded Draft 实施与 pilot 报告

日期：2026-07-31

## 已完成

本轮借鉴 VideoCoCo 的是可验证的架构思想，而不是其不完整公开推理代码：

```text
人工 story / ShotScript / SemanticStoryPlan
  -> diagnostic Blender draft（仅供人检查）
  -> clay Blender draft（未来唯一 V2V 条件）
  -> hash-bound source_video_edit bundle
```

已实现严格语义计划、两种 Blender profile、端到端 coded-draft 编排器和人工对比标注器。人工标注器使用真实解码帧，区分 draft 与独立 review，并对 session、视频、帧和 reference 标注做 SHA-256 绑定；它不会自动生成标注或伪造相机 3D 真值。

## 真实 station_reunion pilot

产物位于 `runs/work/coded_draft_v1/station_reunion/`。本机 Blender 实际运行约 60 秒，未调用 API 或服务器。

| 项目 | diagnostic | clay |
|---|---:|---:|
| 帧数 | 120 | 120 |
| FPS | 24 | 24 |
| 时长 | 5.0 s | 5.0 s |
| 分辨率 | 960×540 | 960×540 |
| MP4 SHA-256 前缀 | `2a0fb304fcfa` | `dccf134bd995` |
| 解码像素 SHA-256 前缀 | `a16f1772f913` | `efe38cae5c4f` |

K0–K4 对应真实帧 0、24、60、95、119。目视检查 contact sheet 可见：diagnostic 保留人物颜色、标签和红色动作轴；clay 使用中性灰度且不含诊断覆盖物；两列的人物接近过程和锁定相机一致。该检查只证明 coded draft 正确，不证明生成模型会遵从它。

`bundle.json` 明确记录：

- `conditioning_mode=source_video_edit`；
- 只有 clay MP4 是 `conditioning_video`；
- diagnostic 是 `evidence_only`；
- `backend_consumed=false`。

因此，本轮没有声称 VACE、Kling 或 Seedance 已经因新 proxy 得到改善。

## 旧结果为什么差

### VACE 几乎等于 proxy

对已有 8 条 VACE 输出的只读审计得到平均 SSIM 0.9707（范围 0.9523–0.9833）；station 使用服务器预处理源时 SSIM 0.9742、PSNR 32.02 dB。它不是字节复制，但属于非常弱的重绘。

主要原因不是“没有人工轨迹”本身，而是条件构造：彩色诊断 proxy 既进入逐帧 VACE 条件，又把同一 proxy 首帧作为 reference，形成双重外观锚定；prompt 又没有明确要求真人比例、真实材质、灯光以及移除标签/红线。旧 proxy 同时承担人类调试图和生成条件，职责混在一起。

新实现先解决这项架构错误：诊断图永不进入 bundle 条件，中性 clay 独立成为未来 V2V source。是否足以改善真实输出仍需一个受控 VACE A/B 推理验证。

### Kling / Seedance 质量差

审计 16 份真实提交 payload 后确认：它们全部是纯文本请求，没有包含 proxy、ShotScript、关键帧或轨迹。因此：

1. proxy 质量不可能是这些 API 结果差的直接原因，因为后端从未看到 proxy；
2. 仓库里的人工规划没有进入模型条件，不能算作模型实际接受了人工轨迹控制；
3. 文本 prompt 丢失了中间里程碑、人物身份/颜色、姿态和终态约束；
4. 5 秒内要求多人物相反运动再叠加相机运动，对纯 T2V 单次采样过载。

已有 15 条成功 API 视频均为 121 帧、24 FPS、约 5.04 秒，媒体本身完整；此前“远短于 proxy”的判断来自 fps/抽样时间轴混用，旧轨迹分数不应继续引用。另有 1 条 Seedance 是供应商版权过滤失败，应保持 failed/unscored。

## 下一次最小真实实验

在开放大矩阵前只做三个小对照：

1. **VACE，一条 station**：旧 diagnostic+首帧双锚定 vs 新 clay-only、无重复首帧 reference、明确真人化 edit instruction。只新增一条必要的新推理；保存输入、日志、显存峰值、视频和人工标注。
2. **Kling/Seedance，各一条 station**：保持模型、时长和人物事件一致，比较旧短 prompt 与由 K0–K4 编译的完整文本 prompt。若当前 API 仍只支持 T2V，不宣称使用了 proxy。
3. 使用本轮人工标注器在相同归一化时间点标人物脚点、可见性/身份和可观察背景运动；必须经过不同身份 reviewer 后才进入统计。

只有这三个最小对照显示方向指标和至少一个距离指标改善，才扩展到 8 个 story 或正式矩阵。

