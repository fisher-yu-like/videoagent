# 使用说明

## 1. 生成 coded draft

当前入口接收人工配对的 prompt、单段 whole-story ShotScript 和 `SemanticStoryPlan`，同时渲染 diagnostic 与 clay 两个版本。输出目录必须不存在，失败时不会覆盖已有证据。

```powershell
python -m videoactagent.coded_draft --blender D:\blender\blender.exe --shotscript stories/station_reunion.json --prompt prompts/station_reunion.txt --semantic-plan plans/station_reunion.semantic.json --output-dir runs/work/coded_draft_v2/station_reunion
```

默认媒体规格为 24 fps、960×540；示例故事为 5 秒，因此严格期望 120 个可解码帧。当前仅 `station_reunion` 完成真实 Blender pilot，输出在 `runs/work/coded_draft_v1/station_reunion/`：

```text
bundle.json                         未来 source_video_edit 输入说明
manifest.json                       完整媒体、来源与制品哈希
semantic_contact_sheet.png          五个语义时间点的双 profile 对照
renders/diagnostic/station_proxy.mp4 人工检查用彩色预演
renders/clay/station_proxy.mp4       未来后端候选条件视频
semantic_frames/                     真实解码得到的 K0–K4 帧
sources/                             只读输入快照
logs/                                两次真实 Blender 日志
```

构建门禁包括：

- 语义计划、ShotScript 的故事 ID 和 5 秒时长一致；
- 两个视频都精确匹配帧数、fps、时长和分辨率；
- K0–K4 均能从真实视频解码，diagnostic 与 clay 不能相同；
- 来源快照、所有制品和 JSON 引用通过 SHA-256 绑定；
- `bundle.json` 与 `manifest.json` 保持 `backend_consumed=false`。

不要把 diagnostic 发给生成模型：它的颜色、文字和轨迹辅助线会被模型当成外观内容。clay 隐藏这些诊断元素，但它目前也只是候选条件；仓库尚无把该 bundle 提交给 VACE、Kling 或 Seedance 的适配器。

## 2. 标注现有真实结果

准备命令读取 [full_result_index.json](../runs/work/full_chain_24_v1/final_results/full_result_index.json)，核对视频 SHA-256，并从每个真实视频解码固定的 9 个归一化时间点。它会得到 8 个参考 proxy、23 个成功结果和 1 个禁用的生成失败项，不会生成或修改视频。

```powershell
python -m videoactagent.cli annotate prepare --index runs/work/full_chain_24_v1/final_results/full_result_index.json --output-dir runs/work/manual_annotation_v1
python -m videoactagent.cli annotate serve --session-dir runs/work/manual_annotation_v1
```

浏览器打开 `http://127.0.0.1:8767`。标注规则如下：

- 人物位置记录脚底点，坐标为左上角原点的 `[0,1] × [0,1]`；
- 每帧同时记录可见性和身份置信度；遮挡、出框或身份歧义时不猜位置，不纳入几何评分；
- 相机只标注可观察的背景运动类别、强度和置信度，不宣称恢复三维相机参数；
- 初次保存是 `draft`；必须由不同人员复核后，才标记 `reviewed` 并具备论文统计资格；
- 结果标注绑定已经复核的参考标注 SHA，防止参考轨迹被静默替换。

这个标注工作区针对旧的 `full_chain_24_v1` 结果，不代表旧后端已经使用新 clay。

## 3. 当前结果应如何解释

- Kling/Seedance 的旧矩阵提交是纯文本条件，没有传入任何 proxy、语义关键帧或显式轨迹，所以其质量不能用来判断新 clay 是否有效。
- 旧 VACE 结果使用的是此前的彩色诊断 proxy 路径，不是本次 clay bundle；观察到接近 proxy 的外观继承，不能算新流程成功。
- 下一步应先为一个场景接入真正消费 clay 的视频编辑适配器，做少量、同设置的条件消融，再决定是否扩大矩阵。

以上两个入口当前只执行本地渲染、解码、哈希和网页标注；它们不会调用 API 或服务器。仓库中保留的历史实验代码不等于全仓库被“绝对禁止联网”，运行其他入口前仍需单独检查其行为与调用预算。
