# Canonical Proxy 模式

Canonical Proxy 是现有白膜 Proxy 的可选升级，不改变 `WorldState`、人物轨迹、物体轨迹或摄像机轨迹协议。

## 做法

- Director/Planner 继续生成同一条完整时间线；所有摄像机仍共享一个 Blender World。
- 每个实体旁边保存 `asset_registry.json`，记录 `asset_id`、尺寸、坐标轴和资产来源。
- 角色使用分段人体代理：头、颈、躯干、骨盆、上下臂、手、上下腿、脚。
- 角色根节点仍严格使用原始人物轨迹；动作阶段和摄像机位置/目标/roll 不被资产替换改写。
- Blender 渲染同时保存 `asset_log.json`、`state_log.json`、`camera_log.json` 和四个摄像机 MP4。
- ProxyVerifier 增加 canonical asset materialization 检查。

## 运行

旧白膜模式保持不变：

```powershell
& .venv\Scripts\python.exe scripts\run_complex_scene_suite.py --proxy-style clay --skip-seedance
```

使用 canonical 人物代理并只渲染 Proxy（不调用外部 API）：

```powershell
& .venv\Scripts\python.exe scripts\run_complex_scene_suite.py --proxy-style canonical --skip-seedance
```

启用一次真实 Proxy VLM 审核：

```powershell
& .venv\Scripts\python.exe scripts\run_complex_scene_suite.py --proxy-style canonical --proxy-review vlm --skip-seedance
```

`scene_structure`、`character_trajectory`、`object_trajectory`、`camera_trajectory` 或 `physical_event` 反馈会阻止后端提交；只有 VLM 明确 `approve` 后才会编译 Appearance-only Prompt。

确认 Proxy 后，再使用 Seedance 2.0：

```powershell
& .venv\Scripts\python.exe scripts\run_complex_scene_suite.py --proxy-style canonical --model Doubao-Seedance-2.0
```

纯 T2V 仅作为对照，不接收 Proxy，也不代表轨迹控制：

```powershell
& .venv\Scripts\python.exe scripts\run_complex_scene_suite.py --proxy-style canonical --realization-mode t2v --allow-unreviewed-backend --final-review vlm --model Doubao-Seedance-2.0
```

`reference_video` 才是 Proxy → Seedance 的控制分支；它需要可访问的 HTTPS Proxy URL（推荐配置 TOS，而不是临时上传服务）。

每次运行会创建新的时间戳目录，不覆盖旧 revision。`proxy_verifier_report.json` 中的 `pending_review` 表示结构检查通过、仍需要人工查看视觉效果，并不等同于真人外观已经解决。

当前 canonical v2 是确定性的程序化人体代理，主要解决人物可识别性、动作分段和多视角一致性；提供 `--asset-dir` 后可按 `<entity_id>.glb` 接入本地规范资产。若需要 StoryBlender 级别的真实网格和材质，也可以在同一个 `asset_registry` 接入 Hunyuan3D/Meshy/TRELLIS2 生成的规范资产。
