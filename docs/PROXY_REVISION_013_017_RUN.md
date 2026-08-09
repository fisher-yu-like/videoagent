# Proxy revision 013–017：真实复审记录

更新时间：2026-08-09 15:28（Asia/Shanghai）

本记录只包含真实 Blender 渲染和真实 VLM 复审，不把确定性检查当作视觉通过，也没有调用 Seedance/Kling。

## 发现与修复

| revision | 针对的真实反馈 | 结果 |
|---|---|---|
| 013 | 演员聚集、镜头覆盖和手势不清 | 分离前/中/后景 lane，增加显式手 landmark |
| 014 | 乐手与 speaker 角色不明显 | 增加麦克风 cue，拉开 speaker 间距 |
| 015 | 跟随镜头抵消领舞位移、后方镜头遮挡 | 固定镜头中心到乐手 lane，统一正面三分之四覆盖 |
| 016 | 演员被裁切或过小 | 收紧四机位构图，限制演员 x 范围 |
| 017 | 路人路径、动作顺序、背包身份仍不清 | 路人改为单调右→左后景穿越；领舞 y 轨迹做两次交替侧步；恢复 013 之后丢失的显式手 landmark；背包加前侧橙色可见标记并保持与领舞轨迹耦合 |

## 真实产物

运行目录：

`runs/results/complex_scene_suite_20260809_152751/plaza_dance_circle_20260809_152751/`

- 共享世界：`sandbox/shared_world.blend`
- 四机位 Proxy：`sandbox/master.mp4`、`lateral.mp4`、`reverse.mp4`、`elevated.mp4`
- 抽帧 contact sheet：`sandbox/*.sheet.jpg`
- 轨迹与骨骼日志：`state_log.json`、`camera_log.json`、`skeleton_pose_log.json`
- 确定性报告：`proxy_verifier_report.json`
- VLM 报告：`proxy_review.json`、`proxy_verifier_report_after_vlm.json`

四个视频均为真实 640×360、24 fps、120 帧、5 秒 MP4，媒体哈希匹配，黑帧检查通过，骨骼/相机/实体日志检查通过。

## VLM 结果

本次只调用 VLM 1 次，结果为 `revision_requested`；Seedance/Kling 调用数均为 0。

VLM 具体反馈：

1. 两个侧步仍像走位而不是舞蹈；
2. 最终回头朝向路人不明确；
3. 路人回挥在长椅或其他角色后方不够可读；
4. 多机位存在重叠轮廓和长椅遮挡，动作顺序难以追踪。

## 根因判断

这轮不再是 JSON、轨迹编译、IK 可达性、共享世界或视频编码问题。`revision_014–016` 中曾有一个明确代码缺陷：显式手 landmark 只对 `revision_013` 生效，后续 revision 被错误地降级为角度推断；`revision_017` 已修复该缺陷。

剩余失败来自代理表达能力：当前 skeleton 是由球、立方体和圆柱组成的程序化人体，没有真实绑定网格、躯干重心、脚掌接触和清晰的朝向变化。低分辨率四机位下，侧步、回头和后景回挥无法稳定区分；长椅位置又遮挡了后景路人。这属于代理资产/动作表达层的架构限制，不能通过 Appearance-only prompt 修复。

## 决策

本轮不进入 Appearance-only Prompt、Seedance 或 Kling。下一阶段应改为：引入一个标准 rigged humanoid/GLB（或可复现的开源绑定人形），把现有 `CharacterTrajectoryPlan` 和 `gesture_tracks` 重定向到骨骼，增加脚掌接触与 root/neck 朝向约束；bench 作为独立静态物体移出路人 lane。完成后重新跑同一场景、同一 K0–K4 和四机位，再请求一次 VLM。旧 revision 和本轮 017 保留，不覆盖。
