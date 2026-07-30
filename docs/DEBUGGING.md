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

The probe requires both outputs to stay inside the job directory and refuses
aliases (including hard links) of the job, source bundle, proxy, or mask. It
publishes the report first, then binds its relative path, actual bytes, SHA-256,
and summary into the validated job. The 2026-07-29 successful files were moved
to `.historical-old-job.json` names after this contract was strengthened; they
are not current-job success markers.

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

### 本地轨迹画布编辑器

启动只监听本机 `127.0.0.1` 的编辑器：

```powershell
& $PY -m videoactagent.trajectory_editor `
  --image runs\stage2_control_bridge\shots\s01\first.png `
  --shotscript examples\station_shotscript.json `
  --shot s01 `
  --output-dir runs\trajectory\s01\browser_polyline `
  --port 8765
```

浏览器打开 `http://127.0.0.1:8765`，选择 target、primitive 和 semantic；通过 progress 指定每个点的归一化时间。画完或拖拽编辑后点击 `Finish`，再点击 `Save`。点击 `Load` 会重新读取已保存的轨迹。Polyline 的 progress 必须严格递增且唯一，否则页面会显示错误并拒绝 Finish。

保存后独立校验页面产物：

```powershell
& $PY -m videoactagent.trajectory validate `
  --input runs\trajectory\s01\browser_polyline\trajectory.json `
  --output runs\trajectory\s01\browser_polyline\validated_trajectory.json `
  --expected-scene station_platform `
  --expected-shot s01
```

主代理已通过实际页面完成 polyline 绘制，并在滑条为 `0.8` 时拖拽原始 `t=0.2` 的点，再执行 `Finish`、`Save`、`Load`。最终时间仍为 `[0.2, 0.8]`；这不是 `session.save` 单测。验收产物为：

- `runs/trajectory/s01/browser_polyline/trajectory.json`，SHA-256 `856f7b1e92587a9ddb85f68004e23bd555318ae8318f062c52585f9529468bb8`；
- `runs/trajectory/s01/browser_polyline/trajectory_overlay.png`，SHA-256 `5f04d30ea04a0c1ceccbb2340d8063dc1b9271c81811247097553c252e9adbf5`，`960×540 RGB`；
- `runs/trajectory/s01/browser_polyline/acceptance_manifest.json`，SHA-256 `abb26570b7d64324d7405627e7cbd20a2084cf24de89fee0c87d1150dce3ab61`。

独立重算验收清单：

```powershell
& $PY -m videoactagent.trajectory_acceptance validate `
  --manifest runs\trajectory\s01\browser_polyline\acceptance_manifest.json `
  --workspace .
```

聚焦测试：

```powershell
& $PY -m unittest tests.test_trajectory_editor -v
```

后续模块会按相同方式增加：确定性轨迹编译器、Blender 轨迹 Proxy、API pilot、真实视频轨迹标注器和指标评估器。

## 9. 全量本地回归

### Real Blender trajectory proxy (Task 5)

This command starts the installed Blender executable and writes a new output
directory; it refuses to overwrite an existing directory:

```powershell
& $PY -m videoactagent.trajectory_proxy `
  --blender $BLENDER `
  --shotscript examples\station_shotscript.json `
  --trajectory runs\trajectory\s01\trajectory.json `
  --output-dir runs\trajectory\s01\proxy_<new_run_name>
```

Inspect `trajectory_proxy.mp4`, `frames/first.png`, `frames/middle.png`,
`frames/last.png`, `trajectory_overlay.png`, and
`trajectory_proxy_manifest.json`. The manifest binds the exact ShotScript and
trajectory SHA-256 values and records applied camera/actor keyframes. Blender
stdout/stderr are always saved as `blender.stdout.log` and
`blender.stderr.log`; a non-zero exit or timeout atomically publishes
`failure_manifest.json` with the exit/timeout state instead of exposing an
unlabelled partial run. The PNGs and MP4 are rendered by Blender; this module
does not generate a Pillow-only stand-in.

Focused real integration test (about one minute on the current workstation):

```powershell
& $PY -X tracemalloc=15 -W error::ResourceWarning -m unittest `
  tests.test_trajectory_proxy_integration -v
```

```powershell
& $PY -X tracemalloc=25 -W error::ResourceWarning -m unittest discover -s tests -v
```

## 10. Manual observation and trajectory metrics (Task 7)

The observer decodes the selected frames from the supplied MP4 with ffmpeg.
It does not run a tracker. Every frame needs either one human click or an
explicit **Mark occluded** decision. Choose frame indices that exist in the
real pilot video and use a new, non-existing output directory:

```powershell
& $PY -m videoactagent.trajectory_observe serve `
  --video <real-kling-or-seedance.mp4> `
  --trajectory runs\trajectory\s01\trajectory.json `
  --track actor_path_01 `
  --frames 0,4,8,12 `
  --output-dir runs\trajectory_observation\<backend>\actor_path_01 `
  --workspace . `
  --port 8766
```

Open `http://127.0.0.1:8766`, annotate every displayed frame, and press
**Save manual_visual_annotation**. The session contains the exact video hash,
actual decoded frame hashes, shot identity, track identity, and target
identity. The saved annotation is
`<output-dir>/manual_annotation.json`. Stop the local server with Ctrl+C after
the save succeeds.

Evaluate only against the same real MP4 and trajectory bytes:

```powershell
& $PY -m videoactagent.trajectory_eval evaluate `
  --trajectory runs\trajectory\s01\trajectory.json `
  --video <the-same-real-pilot.mp4> `
  --session-manifest runs\trajectory_observation\<backend>\actor_path_01\session_manifest.json `
  --annotation runs\trajectory_observation\<backend>\actor_path_01\manual_annotation.json `
  --output runs\trajectory_observation\<backend>\actor_path_01\evaluation.json `
  --workspace .
```

The evaluator reports every common-time-base distance, endpoint error,
direction agreement, arrival timing error, and DTW matrices/path. Occluded
points split interpolation and DTW into separate visible segments; the tool
never interpolates across an occluded interval. A provenance mismatch or
invalid annotation returns a failure and does not publish a new evaluation.

Focused mechanics and real-codec verification (no API calls):

```powershell
& $PY -X tracemalloc=15 -W error::ResourceWarning -m unittest `
  tests.test_trajectory_eval -v
```

如果某个需要真实文件的测试因为文件缺失而失败，不应把它改成模拟通过；应先恢复对应真实输入或明确报告缺失。Windows 无符号链接权限导致的 symlink 测试可以明确 `skip`，仓库同时保留无特权模拟逃逸测试和真实 hardlink 测试。

## 11. Bounded trajectory revision and 24-call matrix (Task 8)

First finish the real Task 7 evaluation for the same backend video. Then
prepare generation 1 of the revision plan with every evaluator source supplied
again for independent hashing:

```powershell
& $PY -m videoactagent.trajectory_closed_loop prepare `
  --evaluation runs\trajectory_observation\<backend>_s01\evaluation.json `
  --prompt runs\trajectory\s01\compiled\trajectory_prompt.txt `
  --video runs\trajectory_api_pilot\real\<run-directory>\result.mp4 `
  --trajectory runs\trajectory\s01\trajectory.json `
  --session-manifest runs\trajectory_observation\<backend>_s01\session_manifest.json `
  --annotation runs\trajectory_observation\<backend>_s01\manual_annotation.json `
  --revision-generation 0 `
  --workspace . `
  --output runs\trajectory_revision\<backend>_s01\revision.json
```

The command rejects generation 1 as an input, so no second revision can be
prepared. It rehashes the video, trajectory, observer manifest, annotation,
and every decoded observer frame, then reruns Task 7 and requires the complete
persisted evaluation to match exactly. Its output always records
`submission_allowed=false` and `network_called=false`; it does not import or
call an API transport.

The formal matrix is fixed at four scenes, two backends, and three conditions.
Planning it without pilot review files is safe and leaves the gate closed:

```powershell
& $PY -m videoactagent.trajectory_experiment prepare `
  --workspace . `
  --output runs\trajectory_experiment\matrix_pending_review.json
```

To evaluate the gate, pass one immutable manual-review JSON report per backend.
Each report must hash-bind the real `result.mp4`, Task 7 `evaluation.json`, its
trajectory/session/annotation sources, and the successful Stage 3 `audit.json`:

```powershell
& $PY -m videoactagent.trajectory_experiment prepare `
  --pilot-report kling=runs\trajectory_api_pilot\reviews\kling.json `
  --pilot-report seedance=runs\trajectory_api_pilot\reviews\seedance.json `
  --kling-cost-per-call <known-cost> `
  --seedance-cost-per-call <known-cost> `
  --currency CNY `
  --workspace . `
  --output runs\trajectory_experiment\matrix_reviewed.json
```

Both reports are necessary but not sufficient. Every one of the 24 expected
condition bundles must also exist and pass its real backend validator. A
missing bundle produces `status=preparation_required` and a null command;
therefore `submission_allowed` stays false. For a ready bundle, the command is
checked by the actual `jd_smoke` parser before it is persisted. The planner
never executes any command. Run the local-only focused tests with:

```powershell
& $PY -X tracemalloc=15 -W error::ResourceWarning -m unittest `
  tests.test_trajectory_closed_loop -v
```
