# Stage 4 实验报告：Camera Motion Evaluator

日期：2026-07-29  
状态：通过（评测工具通过；Kling s01 运镜结论为 inconclusive）

## 结论

项目已增加一个轻量、training-free 的相机运动评测 baseline：对生成视频的首/中/尾帧上方背景区域执行 FFT phase correlation，以背景水平位移方向检查 `truck_right`。该方法输出 raw shift、相关峰置信度、阈值、输入哈希和明确限制，不冒充光流、SLAM 或真实相机位姿。

对 Stage 3 真实 Kling 视频的最终结论是 `inconclusive`，不是 camera control passed。

## 实际输入

- 视频：`runs/stage3_api/20260729T012120Z_kling_d8bde2ef/result.mp4`
- 视频 SHA-256：`f3a95d76e898858726be8651f0472bafd0ff6a33e6c0e6743f5e23730aae35f3`
- 视频元数据：1280×720、121 帧、24 fps、5.042 秒。
- 目标 ShotScript：s01，`camera.motion=truck_right`。
- 评测帧：真实视频第 1、61、121 帧。
- 背景裁剪：`[0, 0, 1280, 302]`，即画面上方 42%。

## 实测结果

| 帧对 | dx | dy | confidence |
|---|---:|---:|---:|
| first → middle | -33 px | 4 px | 1.016 |
| middle → last | -26 px | -4 px | 1.198 |
| first → last | -59 px | 0 px | 1.080 |

对于 truck right，背景水平位移期望为负方向。虽然三个 dx 都为负，但最终相关峰置信度 1.080 低于最小门槛 1.25，最高峰与次高峰过于接近，位移解不唯一。因此最终 verdict 为：

```text
inconclusive
```

## 真实问题与修正

第一版分类器只检查 dx，曾把 -59 px 直接标为 `matched`。这与目视“中央站牌和长椅基本保持居中、truck right 不明显”冲突。

检查 raw phase-correlation surface 后发现 confidence 仅 1.080。根因是分类器忽略峰值歧义，而演员运动、透视、生成背景变化会制造多个相近相关峰。新增失败测试后，分类规则改为：confidence 小于 1.25 时优先输出 `inconclusive`，不再根据 dx 强行判定方向成功。

## 产物

- JSON：`runs/stage4_camera_eval/kling_s01_camera_eval.json`
- 字节数：1745
- SHA-256：`a5c38c7fbe4207f27a90ce5286628f31a2d8a8bb3cbff3e20023e7a66c937583`

## 证据边界

- 合成平移数组仅用于验证估计器符号和幅度约定。
- 阶段结论来自真实 Kling 视频帧。
- Phase correlation 是轻量 baseline，不提供物体级轨迹，也不能处理明显 zoom、rotation、parallax 或大幅场景重绘。
- 当前结果不能证明 truck right 成功，也不能证明相机完全静止。
- 结果支持下一步引入首尾帧、proxy 或更强的特征跟踪基线，但不预设这些控制一定有效。

## 资源与调用

- 本阶段外部 API 调用：0。
- 服务器/GPU：未使用。
- 新增运行依赖：NumPy、Pillow；本地现有运行时已实际加载。
- 最终全量测试：26 项，包括真实 Blender、真实 Control Bridge 媒体和真实 Kling 帧评测。
