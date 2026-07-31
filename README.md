# VideoActAgent

VideoActAgent 用 Blender coded proxy 组织一条可人工修订、可追溯的整段视频生成链路：

```text
Prompt / Semantic Plan
  → Blender diagnostic + clay proxy
  → 人工锁定前缀并修改人物、摄像机和 K 帧描述
  → Blender 重渲染完整 proxy
  → 人工继续修订或批准
  → VACE 生成最终视频
```

当前重点是整段连续视频、人物调度、摄像机轨迹和真实证据链，不把分镜片段拼接冒充端到端结果。

## 使用人工导演页面

首次准备工作区：

```powershell
python -m videoactagent.cli director-loop prepare --bundle runs/work/coded_draft_v1/station_reunion/bundle.json --blender D:\blender\blender.exe --output-dir runs/work/director_loop_v1/station_reunion
```

启动页面：

```powershell
python -m videoactagent.cli director-loop serve --manifest runs/work/director_loop_v1/station_reunion/director_loop_manifest.json
```

打开 `http://127.0.0.1:8769`。先播放完整 Proxy，再选择“锁定起点”：例如选择 K2 会锁定 K0–K2，系统自动带出边界处的人物、摄像机和注视点，你从 K3 开始修改。要改 K2，应选择 K1。页面下方有景别、焦距、插值和画面倾斜的中文说明。

“生成下一版 Proxy”只调用本机 Blender 并保存不可覆盖的新版本。人工看完并点击“批准当前 Proxy”后，VACE 门禁才会开放；页面不会自动调用 VACE 或付费 API。

准备已批准版本的 VACE 作业：

```powershell
python -m videoactagent.cli vace-coded-draft prepare --director-manifest runs/work/director_loop_v1/station_reunion/director_loop_manifest.json --output-dir runs/work/vace_director_v1/station_reunion
```

该命令只准备本地作业，不启动 GPU 推理。

## 结果约束

- 初始 coded draft：`runs/work/coded_draft_v1/station_reunion/`
- 人工导演循环：`runs/work/director_loop_v1/station_reunion/`
- D1、D2 等版本分别保存真实视频、annotation、轨迹、日志和 SHA-256。
- VACE 只读取人工批准的 clay proxy；diagnostic proxy 仅供检查。
- Seedance/Kling 当前是 Prompt-only 基线，不能声称使用了完整 proxy。
- 单元测试素材只验证代码行为，不作为实验结果；实验报告只接受真实 Blender/VACE 输出与真实人工操作。

## 文档

- [导演闭环设计](docs/superpowers/specs/2026-07-31-human-director-panel-design.md)
- [本次实施计划](docs/superpowers/plans/2026-07-31-frozen-boundary-ui.md)
- [架构](docs/ARCHITECTURE.md)
- [使用说明](docs/USAGE.md)
