# Rigged humanoid Proxy：revision 018–020

更新时间：2026-08-09

## 目标

针对 `revision_017` 之后程序化骨架的表达上限，引入一个真实 glTF 网格+骨骼蒙皮资产作为 Proxy。此阶段仍只处理 Proxy，不进入 Appearance-only Prompt、Seedance 或 Kling。

## 实现

- 资产：`assets/canonical_humanoid/CesiumMan.glb`，来源和 SHA-256 见该目录 README。
- `--asset-dir` 支持一个经过验证的 `CesiumMan.glb`，并在每次 run 内复制为 `person_a/b/c.glb`。
- Blender 只导入一次每个角色的共享实体，Y-up 层级统一到 Z-up，重置 GLB 默认迈步姿态，再写入 authored root、肩部、前臂和腿部 pose action。
- 四台相机仍读取同一个 WorldState 和时间线；没有为不同机位复制世界。
- `arm_pose_log.json` 和 `motion_log.json` 标记 `asset_kind=rigged_glb`，可追溯到真实应用的骨骼 keyframe。
- `pipeline_v2.vlm_feedback` 在每张真实抽帧后写入 camera/frame 标签，避免 VLM 丢失时间顺序；重复审查使用 `proxy_vlm_review_001` 等新目录，不覆盖旧证据。

## 真实运行

| revision | 运行目录 | 确定性结果 | VLM |
|---|---|---|---|
| 018 | `runs/results/complex_scene_suite_20260809_160842/` | 20 passed, 0 failed, 2 unknown | revision_requested |
| 019 | `runs/results/complex_scene_suite_20260809_161246/` | 20 passed, 0 failed, 2 unknown | revision_requested |
| 020 | `runs/results/complex_scene_suite_20260809_162059/` | 20 passed, 0 failed, 2 unknown | revision_requested；增加 frame 标签后再次复审仍为 revision_requested |

每次均生成四个真实 640×360、24 fps、120 帧、5 秒 MP4；Seedance/Kling submit/query/download 均为 0。

## 已解决的工程问题

1. GLB 导入后人物被抬到 `z≈3–6m`：修正统一变换的垂直偏移。
2. 导入 GLB 后 `arm_pose_log` 为空：增加真实肩部/前臂 pose keyframe 和日志。
3. 标准人形默认姿态是迈步姿态：导入后重置所有 pose bone 的 rotation/location/scale。
4. 领舞被错误编译为 alternating stride：`revision_020` 使用 `side_step_transfer`，领舞腿部角度为零。
5. VLM 无法判断帧顺序：请求中加入 camera/frame 标签，且重复审查证据不覆盖旧目录。

## 尚未解决的视觉问题

最新 VLM 仍指出：

- 领舞的两次侧步和交替手臂仍接近中性 T-pose，动作不够像舞蹈；
- K1/K2/K3 的领舞波手、乐手 beat raise、路人回挥难以形成清晰事件顺序；
- 背包在后段机位出现与人物视觉脱离的错觉；
- lateral/reverse 仍存在人物重叠，动作在 640×360 下过小。

这些反馈说明当前 GLB 只是“标准网格+骨骼接口”，还没有真实侧步/挥手动画片段和脚掌 IK。下一阶段应接入可复现的 BVH/动作捕捉片段并完成骨骼重定向，同时把背包挂到角色骨骼或其稳定局部挂点；不应继续通过 Appearance-only Prompt 掩盖结构问题。
