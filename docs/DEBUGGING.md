# VideoActAgent 模块调试指南

以下命令默认在仓库根目录
`C:\Users\sy\Desktop\videoactagent` 运行。Windows 自带的默认 Python 版本过旧，
本项目本地测试使用：

```powershell
$PY = 'C:\Users\sy\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
```

## Stage 3：已有后端结果离线审计

查看接口：

```powershell
& $PY -m videoactagent.stage3_audit --help
```

审计现有真实 Kling 运行目录，不调用网络 API：

```powershell
& $PY -m videoactagent.stage3_audit `
  --run-dir runs\stage3_api\20260729T012120Z_kling_d8bde2ef `
  --blender D:\blender\blender.exe `
  --output runs\stage3_api\20260729T012120Z_kling_d8bde2ef\audit.json
```

成功标记是 `STAGE3_AUDIT_OK`。该命令检查请求/状态记录内部一致性、任务 ID、
终态视频 URL、MP4 哈希以及 Blender 解码元数据。运行目录内只允许覆盖保留文件
`audit.json`，不会覆盖 `result.mp4`。

测试：

```powershell
& $PY -m unittest tests.test_stage3_audit -v
```

## Stage 4：相机方向与置信度评估

查看接口：

```powershell
& $PY -m videoactagent.camera_eval --help
```

重新评估真实 Kling 首、中、尾帧：

```powershell
& $PY -m videoactagent.camera_eval `
  --first runs\stage3_api\20260729T012120Z_kling_d8bde2ef\frames\first.png `
  --middle runs\stage3_api\20260729T012120Z_kling_d8bde2ef\frames\middle.png `
  --last runs\stage3_api\20260729T012120Z_kling_d8bde2ef\frames\last.png `
  --shotscript examples\station_shotscript.json `
  --shot s01 `
  --output runs\stage4_camera_eval\kling_s01_camera_eval.json
```

成功标记是 `CAMERA_EVAL_OK`。重点查看输出中的
`directional_evidence.first_to_last`：`direction_verdict` 表示测得的背景运动方向，
`confidence_gate_passed` 表示相关峰是否足够可信，`verdict` 是保守总结果。

测试：

```powershell
& $PY -m unittest tests.test_camera_eval -v
```

## Stage 6：VACE 输入和环境诊断

查看接口：

```powershell
& $PY -m videoactagent.stage6_debug --help
```

检查本地环境：

```powershell
& $PY -m videoactagent.stage6_debug doctor --vace-root third_party\VACE
```

本地没有 CUDA 时 `cuda_ok=false` 和退出码 1 是真实诊断，不表示接口损坏。

验证真实 Stage 6 job、proxy 与 mask：

```powershell
& $PY -m videoactagent.stage6_debug verify-inputs `
  --job runs\stage6_vace_inputs\s01\vace_job.json `
  --bundle runs\stage2_control_bridge\control_bundle.json
```

打印服务器预处理探针命令但不执行：

```powershell
& $PY -m videoactagent.stage6_debug print-probe-command
```

服务器真实运行时必须分别指定验证报告和新的 validated job；原始 job 不会被覆盖：

```bash
/root/venvs/vace/bin/python -m videoactagent.vace_preprocess_probe \
  --job runs/stage6_vace_inputs/s01/vace_job.json \
  --vace-root third_party/VACE \
  --output runs/stage6_vace_inputs/s01/source_validation.json \
  --validated-job-output runs/stage6_vace_inputs/s01/vace_job.validated.json
```

只有同一次真实 upstream decode 和 CUDA transfer 完成并通过合约检查时，程序才会写出 `vace_job.validated.json`。手写或单独修改 `source_validation.json` 不能升级原始 job。

测试：

```powershell
& $PY -m unittest tests.test_stage6_debug -v
```

## Stage 7：真实视频闭环反馈

查看接口：

```powershell
& $PY -m videoactagent.closed_loop --help
```

用真实 Kling 视频、Stage 4 结果和人工检查记录生成三份互相绑定的 JSON：

```powershell
& $PY -m videoactagent.closed_loop evaluate `
  --shotscript examples\station_shotscript.json `
  --shot s01 `
  --video runs\stage3_api\20260729T012120Z_kling_d8bde2ef\result.mp4 `
  --camera-report runs\stage4_camera_eval\kling_s01_camera_eval.json `
  --inspection examples\stage7_s01_inspection.json `
  --output-dir runs\stage7_closed_loop\kling_s01
```

成功标记是 `CLOSED_LOOP_EVALUATED`。输出包括：

- `expectation.json`：ShotScript 的相机、人物和连续性期望；
- `feedback.json`：真实视频/帧哈希、自动相机结果和人工观察；
- `revision.json`：固定操作词汇内的下一轮修订建议。

测试：

```powershell
& $PY -m unittest tests.test_closed_loop -v
```

## 全量回归

```powershell
& $PY -X tracemalloc=25 -W always::ResourceWarning -m unittest discover -s tests -v
```

## Stage 4 当前门槛与复现实验

默认门槛已调整为 `1.05`。对已有真实 Kling 三帧运行：

```powershell
& $PY -m videoactagent.camera_eval `
  --first runs\stage3_api\20260729T012120Z_kling_d8bde2ef\frames\first.png `
  --middle runs\stage3_api\20260729T012120Z_kling_d8bde2ef\frames\middle.png `
  --last runs\stage3_api\20260729T012120Z_kling_d8bde2ef\frames\last.png `
  --shotscript examples\station_shotscript.json `
  --shot s01 `
  --output runs\stage4_camera_eval\kling_s01_camera_eval.json
```

这会得到宽松启发式 `verdict=matched`，同时 JSON 内固定保留
`strict_reference.minimum_confidence=1.25` 和
`strict_reference.verdict=inconclusive`。三段原始 `dx`、`dy`、`confidence`
位于 `measurements`，不可只看最终标签。

显式复现旧的严格结果：

```powershell
& $PY -m videoactagent.camera_eval `
  --first runs\stage3_api\20260729T012120Z_kling_d8bde2ef\frames\first.png `
  --middle runs\stage3_api\20260729T012120Z_kling_d8bde2ef\frames\middle.png `
  --last runs\stage3_api\20260729T012120Z_kling_d8bde2ef\frames\last.png `
  --shotscript examples\station_shotscript.json `
  --shot s01 `
  --minimum-confidence 1.25 `
  --output runs\stage4_camera_eval\kling_s01_camera_eval.strict.json
```

门槛必须是有限数且严格大于 `1.0`。`matched` 仅表示 phase-correlation
背景平移启发式通过，不是相机位姿真值，也不能证明模型或 camera control 变好。

任何模块失败时保留终端输出和对应 `runs/` 目录，不要用手写 JSON 或测试夹具替代
真实产物。

## 本地模块输入输出检查

该接口只读取本地文件，不调用视频生成 API。它会重新计算 SHA-256、解析 JSON，
并用 imageio-ffmpeg 实际解码视频和计数帧；缺少必需文件、哈希过期、JSON 绑定
不一致、视频无法解码或路径越出工作区时均返回失败。检查时 manifest 和每个存在的
证据会先复制到私有临时目录；哈希、JSON、binding 与 FFmpeg 都消费同一份 snapshot，
避免检查过程中原文件变化导致哈希和解析内容不一致。

检查清单里的全部真实 Stage 2/Stage 6 记录：

```powershell
& $PY -m videoactagent.module_io inspect --all `
  --manifest examples\module_io_manifest.json `
  --workspace . `
  --output runs\local_debug\module_io_report.json
```

成功时终端输出 `MODULE_IO_OK` 且退出码为 `0`。报告使用 UUID 临时文件、
`fsync` 和原子替换写入；中途写入失败不会留下临时报告。`--output` 必须位于
`--workspace` 内，且不能与 manifest 或任何本次选中的输入/输出证据指向同一文件，
因此调试报告不会覆盖原始证据。

只检查一个模块：

```powershell
& $PY -m videoactagent.module_io inspect `
  --manifest examples\module_io_manifest.json `
  --workspace . `
  --module stage6_source_validation `
  --output runs\local_debug\stage6_module_io_report.json
```

当前 Stage 6 清单要求真实 CUDA 源预处理已经通过，同时明确绑定
`inference.model_constructed=false` 和 `inference.checkpoint_loaded=false`；因此它不把
尚未发生的模型推理写成通过。聚焦测试命令：

```powershell
& $PY -m unittest tests.test_module_io -v
```
