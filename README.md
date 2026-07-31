# VideoActAgent

VideoActAgent 是一个 whole-story 视频代理原型：先把人工规划的连续故事变成 Blender coded draft，再准备可追溯的视频编辑输入。当前借鉴 VideoCoCo 的是流程边界，而不是其模型代码或 80GB GPU 推理：

`SemanticStoryPlan → diagnostic/clay Blender proxy → source_video_edit bundle`

- `diagnostic` 使用人物颜色、标签和轨迹辅助线，只供人检查调度；
- `clay` 使用灰度材质并隐藏标签/辅助线，才是未来后端的候选条件视频；
- `bundle.json` 绑定 prompt、ShotScript、语义关键帧、视频和 SHA-256。

语义计划目前由人编写，尚未从自然语言自动生成。bundle 也尚未接入生成后端：真实 pilot 中 `backend_consumed=false`，VACE、Kling、Seedance 都没有消费这次新生成的 clay proxy。

## 最短运行

需要 Python 3.10+、项目依赖和 `D:\blender\blender.exe`。安装后为新输出目录运行：

```powershell
python -m pip install -e .
python -m videoactagent.coded_draft --blender D:\blender\blender.exe --shotscript stories/station_reunion.json --prompt prompts/station_reunion.txt --semantic-plan plans/station_reunion.semantic.json --output-dir runs/work/coded_draft_v2/station_reunion
```

当前唯一完成真实 Blender 验收的 coded-draft pilot 位于 [runs/work/coded_draft_v1/station_reunion](runs/work/coded_draft_v1/station_reunion)：两种 proxy 都是 120 帧、24 fps、5 秒、960×540。重点查看：

- [语义关键帧对照图](runs/work/coded_draft_v1/station_reunion/semantic_contact_sheet.png)
- [diagnostic proxy](runs/work/coded_draft_v1/station_reunion/renders/diagnostic/station_proxy.mp4)
- [clay proxy](runs/work/coded_draft_v1/station_reunion/renders/clay/station_proxy.mp4)
- [bundle](runs/work/coded_draft_v1/station_reunion/bundle.json) 与 [manifest](runs/work/coded_draft_v1/station_reunion/manifest.json)

这次 pilot 只证明了本地规划、双 profile 渲染和证据绑定成立，不证明生成视频质量已经改善。本阶段的 coded-draft 与人工标注命令只读写本地文件，不调用 API、服务器或云端 GPU。

## 人工标注

人工标注器比较现有 24 项矩阵中的真实结果与参考 proxy。准备一次工作区，再启动本地网页：

```powershell
python -m videoactagent.cli annotate prepare --index runs/work/full_chain_24_v1/final_results/full_result_index.json --output-dir runs/work/manual_annotation_v1
python -m videoactagent.cli annotate serve --session-dir runs/work/manual_annotation_v1
```

默认地址是 `http://127.0.0.1:8767`。标注器不自动跟踪人物，也不从视频伪造三维相机位姿；论文级结果必须经过独立复核。

## 文档

- [架构](docs/ARCHITECTURE.md)
- [使用说明](docs/USAGE.md)
- [Coded Draft 真实 pilot 报告](docs/CODED_DRAFT_PILOT_REPORT.md)
- [测试基线审计](docs/BASELINE_TEST_AUDIT.md)
- [实验与历史评分纠正](docs/EXPERIMENTS.md)
