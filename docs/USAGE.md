# 使用说明

## 1. 准备

- Python 3.10+
- Blender，默认路径 `D:\blender\blender.exe`
- 安装依赖：`python -m pip install -e .`

无需配置 API 密钥；当前入口硬性拒绝联网提交。

## 2. 一次运行整套故事

```powershell
python run.py configs/whole_story_suite.json
```

配置包含 8 个独立故事、统一媒体 profile、Blender 路径和输出目录。不要复用已经存在的输出目录，程序会拒绝覆盖。

## 3. 查看结果

```text
runs/work/whole_story_v4/
  summary.json
  contact_sheet.png
  <story_id>/
    proxy.mp4
    proxy.blend
    manifest.json
    blender.log
    inspection/{first,middle,last}.png
    bundles/{kling,seedance,vace}.json
```

`summary.json` 是整套结果；每条 `manifest.json` 保存真实解码信息和来源哈希。Kling/Seedance 清单使用 `prompt_only`，不含 proxy 输入；VACE 清单使用 `source_video`，实际绑定 proxy。这些 JSON 尚不是现有提交器可直接消费的 payload 或 preprocess job。

## 4. 修改故事

通常只需修改三处：

1. 在 `prompts/` 添加一个完整、连续、无切镜的文本；
2. 在 `stories/` 添加对应的单 shot ShotScript；
3. 在 `configs/whole_story_suite.json` 注册 case，并换一个新的输出目录。

当前 Blender 只可靠表达人物 root 线性位移和相机线性关键帧。不要把 `action`、`facing` 或运镜名称本身当作骨骼动作和曲线轨迹已实现的证据。
当前 prompt 与 ShotScript 也是人工配对输入，尚无自动语义编译或一致性评分。

## 5. 完整性门

生成结果进入运动评分前必须满足：

- `decoded_duration / requested_duration >= 0.95`；
- 分辨率与目标一致；
- 首、中、末关键时间可解码。

不满足时标记 `incomplete`，不得继续计算人物轨迹或相机控制分数。人工标注帧数与插值点数必须分开报告。
当前离线入口没有生成模型输出适配器，因此只对 Blender proxy 执行媒体检查；完整性门由代码和单测定义，待后端接入后才能用于真实生成结果。
