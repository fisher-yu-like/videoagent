# 项目执行约定

开始任何新的 VideoActAgent 任务前，必须先读取：

1. `docs/CURRENT_PIPELINE_AND_RESULTS.md`；
2. 与任务匹配的 `videoactagent-*` skill；
3. 当前最新 run 的 `summary.json`、`scene_summary.json`、manifest 和反馈文件。

该文档是当前 Pipeline、实验状态、已知限制和 API 计数的单一事实来源。不得根据旧对话、旧报告或旧目录推断当前状态。
每次修改代码、skill、运行入口或实验状态后，必须同步更新 `docs/CURRENT_PIPELINE_AND_RESULTS.md`，记录修改原因、真实验证结果和未完成项。

执行要求：

- 结构问题回到 Director/Blender CodeAgent/Proxy，不在最终视频 prompt 中掩盖；
- 每个 revision 单独保存，不覆盖旧 revision；
- 不伪造视频、指标、VLM 结论或 API 成功；
- API 提交前说明调用预算，禁止自动重试和换 seed 搜索；
- Seedance/Kling reference-video 必须按 camera_id 分别提交，一个 task 只带一个 reference URL；不能把多个机位塞进一个 request，也不能只上传 master；
- 只有真实下载并通过媒体检查的 MP4 才能标为生成完成。
