# VideoCoCo 借鉴与 Pipeline v2 改进

## VideoCoCo 实际采用的方法

本地 `third_party/VideoCoCo` 不是一个新的 Blender 渲染器，而是一组
Agent Skill、toy v2v 数据和 OmniWeaving 推理脚本：

1. `physical-state-planner` 先输出实现无关的物理计划，包括
   `semantic_keyframes`、`transitions`、`causal_constraints`、`must_show` 和
   `must_avoid`。
2. `physical-video-blender-implementer` 再把计划实现为独立 Blender 脚本，
   使用白/灰 clay 材质、稳定灯光、关键帧预演和 preview sheet；它强调结果
   必须从计划中的状态转移产生，而不是只把最终画面做对。
3. Blender MCP 只用于快速检查，最终视频用 Blender CLI 渲染，并用 ffprobe
   检查帧数、分辨率、帧率和时长。
4. `batch_infer_edit.py` 接受 `clay proxy + edit_prompt`，再交给
   OmniWeaving/VideoCoCo 权重做外观重绘。仓库附带的 8 个 toy case 只是格式
   和推理链路示例，不是训练规模数据集。

## 对本项目的直接改进

Pipeline v2 保留自己的共享 `WorldState` 和三机位设计，但把 VideoCoCo 的
实现约束加入 Code Agent：

- prompt 中加入最小可模仿样例：Blender `--` 参数解析、`VIDEO` 后再设置
  `FFMPEG`、逐帧设置变换、camera authored/applied 日志；
- 明确要求 Python 3.7 兼容，禁止 walrus operator，避免运行环境语法错误；
- 明确要求至少两盏有能量的 Blender 灯，避免 clay Proxy 真实渲染成近黑画面；
- 将 `must_show/must_avoid`、因果可见性和关键帧审计写入实现 checklist；
- 静态检查在 Blender 启动前拒绝旧 `Action.fcurves`、错误媒体设置顺序、危险
  import、缺少灯光和缺少 `sys.argv` 分隔符解析；
- ProxyVerifier 同时兼容嵌套 camera log 和真实 Code Agent 生成的扁平逐帧 log，
  但仍要求 authored/applied 数据完整，不能把缺失日志当作通过。

## 与 VideoCoCo 的边界

VideoCoCo 的目标是物理事件的 proxy-to-photoreal edit；本项目还需要共享
WorldState、多机位职责、人物/物体/相机轨迹、人工/VLM 修订和哈希证据。因此
我们借鉴它的 planner→standalone Blender→preview audit→appearance edit 结构，
没有直接复制其 OmniWeaving 权重或把 toy dataset 当作我们的实验数据。
