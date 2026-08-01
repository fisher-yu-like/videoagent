# 中文使用说明

## 选哪个标注器

- 原来的单摄像机人工循环仍用 `director-loop`，界面和工作区格式未改。
- 要看 Agent 先规划三台摄像机，再人工修改与批准，使用 `director-multicam`。

新页面默认地址是 `http://127.0.0.1:8770`。当前 station 页面已对应：

`runs/work/agent_multicam_suite_20260801/station_reunion/multicam_manifest.json`

## 页面从上到下怎么用

1. **完整故事参考**：播放完整 5 秒 Blender Proxy，不是只看 K0–K4 静态帧。
2. **三台摄像机同步播放**：渲染完成前暂时显示参考视频；完成后显示 A/B/C 三路，按钮会同步播放，误差超过 80 ms 自动校正。
3. **Agent 职责规划**：阅读 Master、Follow、Reverse 各自负责的时间段、目标和理由。
4. **修改从 Kx 开始**：例如选 K2，K0–K1 冻结；重规划时内部发送 `locked_through_keyframe=K1`。
5. **批准职责规划**：这是第一道人工作业门。未批准不能渲染。
6. **摄像机轨迹编辑**：人物圆点和三台摄像机 K0–K4 初始点会自动出现。选择摄像机与关键帧后在图上点击，修改其 XY；高度 Z、焦距和 Roll 用旁边输入框。
7. **渲染 Blender Proxy**：一次 Blender 进程加载同一世界，顺序渲染三台摄像机；不会调用 Seedance、Kling 或 VACE。
8. **批准本次三机位 Proxy**：自动检查成功后仍需你看视频并批准，之后才是可用于下游模型的正式版本。

## 术语

- **K0–K4**：完整时间线的五个控制点，分别位于 0%、20%、50%、80%、100%。
- **插值**：控制点之间如何连续移动；当前默认线性，避免突然跳变。
- **景别**：画面拍得多近，例如 wide 是全景，medium 是中景。
- **Roll**：摄像机沿镜头正前方轴旋转；0° 表示地平线不倾斜。
- **Look-at**：摄像机朝向的世界点。人物移动时可跟随人物或人物中点。
- **Master / Follow / Reverse**：全局主机位 / 跟随关键人物 / 从另一侧覆盖反应的机位。

## 工作区证据

每个场景目录包含：原 prompt 与 ShotScript 哈希、完整参考视频、每次 DeepSeek 请求/响应/调用次数、人工批准记录、三机位轨迹、共享 `.blend`、三路 MP4、Depth/CryptoObject EXR、自动评估和人工 Proxy 批准。失败结果保留真实状态，不会被写成通过。

若要重新准备整套 6 个参考 Proxy，使用 `director-multicam prepare-suite --help` 查看一个命令即可；日常操作都在网页里完成。
