# VideoActAgent 模块调试指南

以下命令默认在仓库根目录 `C:\Users\sy\Desktop\videoactagent` 执行。建议先设置：

```powershell
$PY = 'C:\Users\sy\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$BLENDER = 'D:\blender\blender.exe'
```

每个 `tests/test_*.py` 文件顶部也写有该模块的输入、输出和聚焦命令。单元测试中的临时数据只验证接口机制；只有明确读取 `runs/` 中真实文件的用例可以说明当前真实产物状态。

## 0. 统一模块 I/O 检查

检查现有全部已注册模块：

```powershell
& $PY -m videoactagent.module_io inspect --all `
  --manifest examples\module_io_manifest.json `
  --workspace . `
  --output runs\local_debug\module_io_report.json
```

只检查当前轨迹模块：

```powershell
& $PY -m videoactagent.module_io inspect `
  --module trajectory_instruction_s01 `
  --manifest examples\module_io_manifest.json `
  --workspace . `
  --output runs\local_debug\trajectory_module_io_report.json
```

成功标记是 `MODULE_IO_OK`。报告会重新读取文件、解码 JSON/视频，并核对声明的 SHA-256 和字段绑定。

聚焦测试：

```powershell
& $PY -X tracemalloc=15 -W error::ResourceWarning -m unittest tests.test_module_io -v
```

## 1. ShotScript 与真实 Blender Proxy

验证 ShotScript 与提示编译：

```powershell
& $PY -m unittest tests.test_station_shotscript tests.test_prompts -v
```

重新渲染真实 Stage 1 Proxy：

```powershell
& $PY videoactagent\blender_runner.py `
  --blender $BLENDER `
  --shotscript examples\station_shotscript.json `
  --output-dir runs\stage1_blender
```

输出包括 `station_proxy.blend`、各镜头 MP4、首尾帧和渲染清单。真实 Blender 集成测试：

```powershell
& $PY -m unittest tests.test_blender_proxy_integration -v
```

## 2. Control Bridge

```powershell
& $PY videoactagent\control_bridge.py `
  --blender $BLENDER `
  --shotscript examples\station_shotscript.json `
  --blend runs\stage1_blender\station_proxy.blend `
  --output-dir runs\stage2_control_bridge
```

主要输出：

- `runs/stage2_control_bridge/control_bundle.json`
- `runs/stage2_control_bridge/shots/s01/proxy.mp4`
- `runs/stage2_control_bridge/shots/s01/first.png`
- `runs/stage2_control_bridge/shots/s01/last.png`

聚焦测试：

```powershell
& $PY -m unittest tests.test_control_bridge_integration -v
```

## 3. Kling / Seedance 后端接口

先只做离线准备，不调用 API：

```powershell
& $PY -m videoactagent.backend_prepare `
  --bundle runs\stage2_control_bridge\control_bundle.json `
  --backend kling `
  --output runs\stage3_backend\kling_readiness.json
```

真实调用入口如下。它们会产生费用，只应在确认环境变量已配置并且确实需要新样本时运行：

```powershell
& $PY -m videoactagent.jd_smoke submit-kling `
  --bundle runs\stage2_control_bridge\control_bundle.json `
  --shot s01 --prompt cinematic --run-root runs\stage3_api

& $PY -m videoactagent.jd_smoke submit-seedance `
  --bundle runs\stage2_control_bridge\control_bundle.json `
  --shot s01 --prompt cinematic --run-root runs\stage3_api
```

对已有任务只查询一次或下载一次：

```powershell
& $PY -m videoactagent.jd_smoke query --run-dir <真实运行目录>
& $PY -m videoactagent.jd_smoke download --run-dir <真实运行目录>
```

离线审计现有 Kling 结果：

```powershell
& $PY -m videoactagent.stage3_audit `
  --run-dir runs\stage3_api\20260729T012120Z_kling_d8bde2ef `
  --blender $BLENDER `
  --output runs\stage3_api\20260729T012120Z_kling_d8bde2ef\audit.json
```

接口测试不调用真实 API：

```powershell
& $PY -m unittest `
  tests.test_backend_contracts `
  tests.test_backend_prepare `
  tests.test_dry_run `
  tests.test_jd_response_parsing `
  tests.test_jd_smoke `
  tests.test_run_directory `
  tests.test_stage3_audit -v
```

## 4. Camera Motion Evaluator

对现有真实 Kling 首、中、尾帧重新计算：

```powershell
& $PY -m videoactagent.camera_eval `
  --first runs\stage3_api\20260729T012120Z_kling_d8bde2ef\frames\first.png `
  --middle runs\stage3_api\20260729T012120Z_kling_d8bde2ef\frames\middle.png `
  --last runs\stage3_api\20260729T012120Z_kling_d8bde2ef\frames\last.png `
  --shotscript examples\station_shotscript.json `
  --shot s01 `
  --output runs\stage4_camera_eval\kling_s01_camera_eval.json
```

默认阈值是 `1.05`；传入 `--minimum-confidence 1.25` 可复现严格参考。`matched` 是背景平移启发式结论，不是相机位姿真值。

```powershell
& $PY -m unittest tests.test_camera_eval -v
```

## 5. 固定开源 Baseline

```powershell
& $PY -m videoactagent.baseline_audit `
  --manifest baselines\manifest.json `
  --third-party third_party `
  --output runs\stage5_baseline_audit\audit.json
```

```powershell
& $PY -m unittest tests.test_baseline_audit tests.test_source_safety -v
```

## 6. VACE 输入适配与服务器预处理

本地从真实 Stage 2 bundle 生成 VACE 输入：

```powershell
& $PY -m videoactagent.vace_inputs prepare `
  --bundle runs\stage2_control_bridge\control_bundle.json `
  --shot s01 `
  --output-dir runs\stage6_vace_inputs\s01
```

本地诊断：

```powershell
& $PY -m videoactagent.stage6_debug doctor --vace-root third_party\VACE
& $PY -m videoactagent.stage6_debug verify-inputs `
  --job runs\stage6_vace_inputs\s01\vace_job.json `
  --bundle runs\stage2_control_bridge\control_bundle.json
```

服务器端真实预处理探针的入口：

```bash
/root/venvs/vace/bin/python -m videoactagent.vace_preprocess_probe \
  --job runs/stage6_vace_inputs/s01/vace_job.json \
  --vace-root third_party/VACE \
  --output runs/stage6_vace_inputs/s01/source_validation.json \
  --validated-job-output runs/stage6_vace_inputs/s01/vace_job.validated.json
```

聚焦测试：

```powershell
& $PY -m unittest `
  tests.test_vace_inputs `
  tests.test_stage6_debug `
  tests.test_vace_preprocess_probe -v
```

## 7. 现有离线闭环

```powershell
& $PY -m videoactagent.closed_loop evaluate `
  --shotscript examples\station_shotscript.json `
  --shot s01 `
  --video runs\stage3_api\20260729T012120Z_kling_d8bde2ef\result.mp4 `
  --camera-report runs\stage4_camera_eval\kling_s01_camera_eval.json `
  --inspection examples\stage7_s01_inspection.json `
  --output-dir runs\stage7_closed_loop\kling_s01
```

```powershell
& $PY -m unittest tests.test_closed_loop -v
```

## 8. ATI 风格轨迹指令

校验并规范化示例轨迹：

```powershell
& $PY -m videoactagent.trajectory validate `
  --input examples\trajectory_circle_s01.json `
  --output runs\trajectory\s01\trajectory.json `
  --expected-scene station_platform `
  --expected-shot s01
```

当前真实规范化输出 SHA-256：
`52792642f4887a78cc332898714fa877d3d6336073302e3a127cd69b7d8921e6`。

```powershell
& $PY -m unittest tests.test_trajectory -v
```

后续模块会按相同方式增加：本地画布编辑器、确定性轨迹编译器、Blender 轨迹 Proxy、API pilot、真实视频轨迹标注器和指标评估器。

## 9. 全量本地回归

```powershell
& $PY -X tracemalloc=25 -W error::ResourceWarning -m unittest discover -s tests -v
```

如果某个需要真实文件的测试因为文件缺失而失败，不应把它改成模拟通过；应先恢复对应真实输入或明确报告缺失。Windows 无符号链接权限导致的 symlink 测试可以明确 `skip`，仓库同时保留无特权模拟逃逸测试和真实 hardlink 测试。
