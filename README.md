# VideoActAgent

VideoActAgent 是一个以 Blender coded proxy 为中间表示的整段视频导演代理：

```text
Prompt / Semantic Plan
  → Blender 初始 diagnostic + clay proxy
  → 人工标注人物、摄像机和 K0–K4 Prompt
  → Blender 重新渲染完整 proxy
  → 人工观看完整视频并继续修订
  → 人工批准
  → VACE 生成最终视频
```

当前重点是整段连续视频、人物调度、摄像机轨迹和真实证据链，不按分镜分别搜索结果。

## 浏览器导演闭环

准备一次工作区：

```powershell
python -m videoactagent.cli director-loop prepare --bundle runs/work/coded_draft_v1/station_reunion/bundle.json --blender D:\blender\blender.exe --output-dir runs/work/director_loop_v1/station_reunion
```

以后只需启动页面：

```powershell
python -m videoactagent.cli director-loop serve --manifest runs/work/director_loop_v1/station_reunion/director_loop_manifest.json
```

打开 `http://127.0.0.1:8769`。页面会播放完整 proxy；用户逐个填写 K0–K4 的人物位置、摄影机位置/朝向/焦距和画面描述。每次点击 `Generate next proxy` 都会运行真实 Blender 并生成不可覆盖的新版本。只有观看完整视频并点击 `Approve current proxy` 后，VACE 门禁才会开放。

生成批准绑定的 VACE 作业：

```powershell
python -m videoactagent.cli vace-coded-draft prepare --director-manifest runs/work/director_loop_v1/station_reunion/director_loop_manifest.json --output-dir runs/work/vace_director_v1/station_reunion
```

该命令只准备本地作业，不调用 API，也不启动 GPU 推理。

## 结果和证据

- 初始 coded draft：`runs/work/coded_draft_v1/station_reunion/`
- 人工导演循环：`runs/work/director_loop_v1/station_reunion/`
- 每个 `D1`、`D2` 版本保存真实视频、标注、轨迹、日志和 SHA-256。
- VACE 只读取人工批准版本的 clay proxy；diagnostic proxy 仅供检查。
- 当前 Seedance/Kling 接口是 Prompt-only 基线，不能声明其消费了完整 proxy。

单元测试中的小型素材只验证代码行为，不作为实验结果。实验报告只接受真实 Blender/VACE 输出和真实人工操作。

## 文档

- [导演闭环设计](docs/superpowers/specs/2026-07-31-human-director-panel-design.md)
- [实施计划](docs/superpowers/plans/2026-07-31-human-director-loop.md)
- [架构](docs/ARCHITECTURE.md)
- [使用说明](docs/USAGE.md)
