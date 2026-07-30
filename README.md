# VideoActAgent

VideoActAgent 把人工配对的完整 story prompt 与 ShotScript 渲染为可执行的 Blender 预演，再为视频生成后端准备可追溯输入。当前版本尚不自动把自然语言编译成 ShotScript；它专注 whole-story：一次运行一个连续故事，不拆镜头提交、不把未发送的 proxy 写成模型条件。

## 当前能做什么

- 读取 8 组不同的 story prompt 与 ShotScript；
- 用本机 Blender 真实渲染 8 个 5 秒连续 proxy；
- 检查帧数、时长、分辨率、首/中/末帧与 SHA-256；
- 为 Kling / Seedance 写出诚实的 `prompt_only` 离线输入清单；
- 为 VACE 写出实际引用 proxy 的 `source_video` 离线输入清单；
- 在输出不完整时，先禁止轨迹与相机评分。

轨迹修订、ATI、ReCamMaster/CamTrol 和正式 API 矩阵暂时冻结，待 whole-story 输入验收后再继续。

## 运行

需要 Python 3.10+、项目依赖和 `D:\blender\blender.exe`：

```powershell
python -m pip install -e .
python run.py configs/whole_story_suite.json
```

结果写入 `runs/work/whole_story_v4/`。运行目录不可覆盖；需要重跑时请在配置中换一个新目录。

这些后端 JSON 是 hash-bound 离线输入清单，尚未接入现有逐镜头提交器/VACE 预处理器，不能当作可直接提交的 payload 或 preprocess job。

## 文档

- [架构](docs/ARCHITECTURE.md)
- [使用说明](docs/USAGE.md)
- [实验与历史评分纠正](docs/EXPERIMENTS.md)

本阶段默认 `submit=false`、`max_api_calls=0`，不会调用 Kling、Seedance 或服务器推理。API 与服务器实验必须另行报告并获准后执行。
