# Proxy Revision 009–012 实验记录

本记录对应当前 VideoActAgent pipeline 的 Proxy 阶段：

`ScenePlan / PhysicalStatePlan / Character-ObjectTrajectoryPlan / CameraTrajectoryPlan → Blender sandbox → shared-world four-camera Proxy → deterministic ProxyVerifier → VLM review`

没有向 Seedance 或 Kling 提交任何一条本轮 Proxy；结构和可读性未通过前，不能进入 Appearance-only prompt 或后端。

## 本地整合

骨骼/IK 实现已直接整合到当前 `stage0-api-baseline` 工作区。`feature/deepseek-blender-codegen`、`feature/codegen-lab-ui` 和 `feature/seedance-human-restyle` 是历史分支，接口与当前“Codex + Blender MCP / 本地 sandbox”主线不同，本轮没有盲目合并它们。

## 真实运行

每一版都是独立目录，不覆盖旧 revision；每版 4 个真实 MP4，640×360、24 FPS、120 帧、5 秒。

| revision | 真实目录 | deterministic | VLM | 主要反馈 |
|---|---|---|---|---|
| 009 | `runs/results/complex_scene_suite_20260809_141809/plaza_dance_circle_20260809_141809` | 通过 | `revision_requested` | 继承机位仍偏后/高，动作顺序不可读 |
| 010 | `runs/results/complex_scene_suite_20260809_142238/plaza_dance_circle_20260809_142238` | 通过 | `revision_requested` | 角色/音箱关系和四机位覆盖不清 |
| 011 | `runs/results/complex_scene_suite_20260809_142621/plaza_dance_circle_20260809_142621` | 通过 | `revision_requested` | 手势像站立/行走，背包和音箱仍混淆 |
| 012 首次 | `runs/results/complex_scene_suite_20260809_143042/plaza_dance_circle_20260809_143042` | **失败** | 未调用 | hand.R 在 frame 44 超出二段 IK 可达范围 |
| 012 修正版 | `runs/results/complex_scene_suite_20260809_143518/plaza_dance_circle_20260809_143518` | 通过 | `revision_requested` | 手势仍不够清晰，机位仍有遮挡/高位视角 |

### revision_012 修正版证据

- master MP4：`runs/results/complex_scene_suite_20260809_143518/plaza_dance_circle_20260809_143518/sandbox/master.mp4`
- 四机位 manifest：`runs/results/complex_scene_suite_20260809_143518/plaza_dance_circle_20260809_143518/sandbox/render_manifest.json`
- 骨骼帧日志：`runs/results/complex_scene_suite_20260809_143518/plaza_dance_circle_20260809_143518/sandbox/skeleton_pose_log.json`
- deterministic 绑定报告：`runs/results/complex_scene_suite_20260809_143518/plaza_dance_circle_20260809_143518/proxy_verifier_report.json`
- VLM 绑定报告：`runs/results/complex_scene_suite_20260809_143518/plaza_dance_circle_20260809_143518/proxy_verifier_report_after_vlm.json`
- master SHA-256：`f3952cc27898ddaf32f35f8f18fbacdc893ec4a1acd5d4d7d041f796995f7f89`（原始值保存在 manifest/report；此处仅作人工索引）

## API 与验证计数

- Seedance submit/query/download：`0 / 0 / 0`
- Kling：`0`
- VLM：009、010、011、012 修正版各 1 次，共 `4` 次；012 首次 deterministic 失败没有调用 VLM。
- 定向测试：`44 passed`；`py_compile scripts/run_complex_scene_suite.py` 通过。

## 结论与后续门禁

当前失败属于 Proxy 的 character/camera/physical readability，不是外观问题。因此按既定规则停在 Proxy 阶段：不生成 Appearance-only 修订，不调用真实视频后端，不把任何 Proxy 结果标为“通过”。下一步如果继续，应先由人工决定是否接受“低模骨骼仅用于轨迹结构”的质量上限，或切换到真实 humanoid/rig 资产；不能再只靠小幅 prompt 或机位参数叠加。
