# ACCAD BVH retarget run (2026-08-10)

本记录对应真实 Blender 渲染，不包含伪造视频、伪造指标或 Seedance/Kling 请求。

## 输入与实现

- 场景：`plaza_dance_circle`
- 最终 Proxy revision：`revision_024`（021 BVH → 022 可读性 → 023 直立 → 024 有界编舞叠加）
- 资产：`assets/canonical_humanoid/CesiumMan.glb`
- 真实动作：ACCAD/Open Motion Project 的 `Female1_C24_SideStepLeft.bvh` 与 `Female1_C25_SideStepRight.bvh`
- Blender：`D:\blender\blender.exe`，四机位共享一个 WorldState 和一个 `.blend`
- API：Seedance/Kling 0 次；VLM 代理审查 1 次（revision_021）、1 次（022）、1 次（023）、1 次（024）

BVH 文件的来源、CC BY 3.0 说明和 SHA-256 见
`assets/motion_clips/accad/README.md`。每次运行仍单独保存源码路径、SHA、动作日志和渲染 manifest。

## 最终真实运行

目录：
`runs/results/complex_scene_suite_20260809_171050/plaza_dance_circle_20260809_171050/`

四个真实 MP4：

- `sandbox/master.mp4`
- `sandbox/lateral.mp4`
- `sandbox/reverse.mp4`
- `sandbox/elevated.mp4`

每个视频均为 640×360、24 fps、120 帧、5.0 秒；manifest SHA-256 与文件一致，黑帧检查无事件。WorldState、人物/物体轨迹、四个相机的 120 帧 camera log、canonical asset log 均通过确定性检查。

`motion_log.json` 中 `person_a` 的真实记录为 `bvh_retarget+authored_side_step`，包含 14 个实际映射四肢骨骼、两段 BVH source hash、左右手/腿的非零 pose_stats，以及有界 side-step overlay；没有把动作只写进文字日志。

## VLM 结果与当前结论

revision_024 的一次真实 VLM 审查仍为 `revision_requested`。反馈集中在：低模代理下音乐人的举手、路人的回挥、主舞者两次侧步与最终转身仍不够一眼可读。该反馈属于 `character_trajectory` / `physical_event` / `camera_trajectory`，因此没有进入 Appearance-only Prompt，也没有提交后端 API。

这轮已经证明：真实 BVH 可以导入、按骨骼重定向、与 authored root/camera 轨迹共享同一世界并产生可审计的非零姿态。当前瓶颈是 CesiumMan 低模外观与局部骨骼轴/镜头取样的可读性，而不是 BVH 文件缺失或渲染链路失败。下一步应优先换更标准的人形 rig/动作片段或接入 hand/foot IK，再重新走 ProxyVerifier→VLM；在 Proxy 获批前继续保持 Seedance/Kling 为 0。

## 测试边界

本轮相关回归测试：`tests/test_complex_scene_suite.py` 与
`tests/test_pipeline_v2_blender_sandbox.py` 共 **52 passed**。完整仓库测试不能作为本轮通过依据：直接收集 `third_party/VACE/tests` 时缺少已安装的 `vace` 包；仅运行 `tests/` 会触发既有外部 API/集成测试并在 360 秒超时。以上阻塞没有被伪装成通过，也没有影响真实 Blender Proxy 证据。
