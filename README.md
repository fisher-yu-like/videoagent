# VideoActAgent

VideoActAgent 把“文字故事 → 可执行 Blender Proxy → 人工导演确认 → 视频模型”拆成可检查的阶段。当前重点不是让 Proxy 直接变真人，而是把人物调度、摄像机轨迹、多视角职责和证据链先做正确。

## 当前架构

```text
故事 Prompt
        ↓  DeepSeek-v4-flash 生成受约束场景草案
人工表单修改并批准 ShotScript
        ↓  固定 Blender 编译器
完整参考 Proxy + 人工修改人物/物体轨迹
        ↓  人工批准轨迹
DeepSeek-v4-flash 分配 Master / Follow / Reverse 三机位职责
        ↓  人工批准职责
确定性编译三套 K0–K4 摄像机轨迹
        ↓  人工可编辑摄像机点
Blender 一次加载共享场景，输出 3 路同步视频 + Depth/CryptoObject EXR
        ↓  数值检查 + 人工批准 Proxy
后续再接 VACE / Seedance / Kling / 显式轨迹模型
```

仓库保留两个互不替换的界面：

- `director-loop`：原单摄像机人工标注器，端口 8769。
- `director-multicam`：打开已有 ShotScript 工作区的原多机位页面，端口 8770。
- `director-wizard`：从 Prompt 开始的单页七阶段向导，复用上述多机位后半程，端口 8770。

新项目日常只需一条命令：

```powershell
.\.venv\Scripts\python.exe -m videoactagent.cli director-wizard start --blender D:\blender\blender.exe --workspace runs\work\my_story --port 8770
```

浏览器打开 `http://127.0.0.1:8770`，依次完成：输入 Prompt → 审批场景表单 → 审批完整 Reference Proxy → 审批人物/物体轨迹 → 审批三机位职责 → 审批三视角 Proxy。每次批准后页面自动进入下一步；可以返回旧版本重新分叉，旧证据不会覆盖。

DeepSeek 只读取 `DEEPSEEK_API_KEY` 和 `DEEPSEEK_BASE_URL`，模型固定为 `deepseek-v4-flash`。只有用户点击生成或重规划才调用 API，每次一次、零自动重试；密钥不会写入证据。DeepSeek 只生成受约束 JSON，本地 [Blender 编译器](videoactagent/blender_proxy.py) 负责生成真实 `.blend` 和 MP4，不执行 LLM 代码。详细说明见 [docs/USAGE.md](docs/USAGE.md)。

## 已完成与限制

- 已完成 6 个不同 prompt、不同 ShotScript、不同 Blender 环境的完整 5 秒参考 Proxy，不再复用同一 Proxy。
- 已完成 3 路共享世界时间线渲染、每路 RGB MP4、深度与 Cryptomatte 结构证据、同步/可见性/相机突变检查。
- Prompt 编译加入场景尺度容差；微小 look-at、位置、roll、焦距漂移会写为“保持固定”。
- Agent 负责语义规划，数值轨迹由确定性编译器生成；任何 LLM schema 违规都会失败，不会偷偷补数据。
- 当前三路是同一 Blender 世界的同步视角；尚未证明生成模型输出的跨视角真人身份一致性。
- VACE/Seedance/Kling 不在新页面中自动调用。Proxy 的低模外观仍可能污染 V2V 外观，因此“结构控制”和“真人重绘”继续分离。

## 参考工作

“借鉴模块”表示架构思想或接口来源，不等于已复现论文结果。

| 论文 | 借鉴模块 | 官方代码 | 本项目状态 |
|---|---|---|---|
| [SceneCraft](https://arxiv.org/abs/2403.01248) | LLM 规划 → Blender 可执行代码、迭代反馈 | [项目页](https://acbull.github.io/scenecraft/) | 借鉴规划/编译分层，未复现模型 |
| [MovieAgent](https://arxiv.org/abs/2503.07314) | 导演与镜头层级规划 | [GitHub](https://github.com/showlab/MovieAgent) | 简化为单个摄影规划 Agent |
| [Camera Artist](https://arxiv.org/abs/2604.09195) | 摄影职责、电影语言和人工导演门 | — | 借鉴三机位职责结构，未复现 |
| [VideoCoCo](https://arxiv.org/abs/2607.27380) | Blender 作为可执行时空草稿、Proxy→视频编辑 | [GitHub](https://github.com/micky-li-hd/VideoCoCo) | 已借鉴 Proxy 证据链；未训练 VideoCoCo-3K |
| [ATI](https://arxiv.org/abs/2505.22944) | 统一人物/局部/摄像机轨迹接口 | [GitHub](https://github.com/bytedance/ATI) | 上层轨迹协议兼容思路，权重分支未接入 |
| [CamTrol](https://arxiv.org/abs/2406.10126) | Training-free 摄像机控制 | [项目页](https://lifedecoder.github.io/CamTrol/) | 候选生成后端，未复现 |
| [CameraCtrl](https://arxiv.org/abs/2404.02101) | 显式相机姿态参数化 | [GitHub](https://github.com/hehao13/CameraCtrl) | 相机状态表达借鉴，未训练模块 |
| [MotionCtrl](https://arxiv.org/abs/2312.03641) | 分离人物运动和摄像机运动 | [GitHub](https://github.com/TencentARC/MotionCtrl) | 已在上层协议与评估中分离 |
| [ReCamMaster](https://arxiv.org/abs/2503.11647) | 单视频新相机轨迹重渲染、多机位数据 | [GitHub](https://github.com/KlingTeam/ReCamMaster) | 候选显式相机后端，未运行权重 |
| [VACE](https://arxiv.org/abs/2503.07598) | 统一 V2V/参考/遮罩条件 | [GitHub](https://github.com/ali-vilab/VACE) | 旧链路已有真实结果；新三机位链未调用 |
| [OmniWeaving](https://github.com/Tencent-Hunyuan/OmniWeaving) | 视频+图像+文本组合编辑 | [GitHub](https://github.com/Tencent-Hunyuan/OmniWeaving) | VideoCoCo 的编辑基线参考，未本地部署 |
