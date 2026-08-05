# 中文使用说明

## 两个标注器

- `director-loop` 是原来的单摄像机人工循环，界面和数据格式不变。
- `director-multicam` 是新的多机位向导。人物/物体轨迹先由人批准，再让 Agent 规划三台摄像机。
- `codegen-lab` 是独立的 DeepSeek Blender 代码生成实验页，不改变前两个页面。

日常操作都在一个网页完成，默认地址是 `http://127.0.0.1:8770`。

## 三个界面的启动方法

从仓库根目录分别启动即可；8769、8770、8781 可以同时运行。8770 的两个命令是二选一。

```powershell
# 原单机位标注器
.\.venv\Scripts\python.exe -m videoactagent.cli director-loop serve --manifest runs\work\director_loop_humanoid_v1\station_reunion\director_loop_manifest.json --port 8769

# 已有三机位工作区
.\.venv\Scripts\python.exe -m videoactagent.cli director-multicam serve --manifest <multicam_manifest.json> --port 8770

# 或：从 Prompt 开始的三机位向导
.\.venv\Scripts\python.exe -m videoactagent.cli director-wizard start --blender D:\blender\blender.exe --workspace runs\work\my_story --port 8770

# 独立 Codegen Lab
.\.venv\Scripts\python.exe -m videoactagent.cli codegen-lab serve --workspace C:\Users\sy\Desktop\videoactagent --blender D:\blender\blender.exe --port 8781
```

Codegen Lab 页面需要填写 Prompt、ShotScript、轨迹 JSON 和受保护工作区。点击“准备作业”只做哈希快照；“真实生成”只调用一次 DeepSeek-v4-pro，失败不会自动重试；随后先 Smoke，再 Full。页面显示的是实际 `job.json`、日志、哈希和 MP4，不把 HTTP 受理状态当成成功。

## 新页面只做三步

1. **人物与物体轨迹**：先播放完整参考 Proxy。选择目标和 K0–K4 后，在图上点击新位置；需要道具时填写名称并点“增加物体”。保存为新的 S 版本，再人工批准。
2. **三台摄像机**：批准轨迹后，页面自动跳到这里。DeepSeek 只调用一次、不会自动重试；它分配 Master、Follow、Reverse 的目标、景别和负责时段。你可再移动机位并批准职责，然后渲染真实 Blender Proxy。
3. **结果检查**：渲染完成后，页面自动显示 A/B/C 三路同步视频。自动检查只验证几何和文件，不代替人工构图判断；看完后由你批准。

## “从 K2 开始”是什么意思

K0–K4 是整段视频的五个控制点，分别位于 0%、20%、50%、80%、100%。选择“从 K2 开始”后，K0–K1 会冻结，后端也会拒绝任何偷改；K2–K4 可以继续调整。

- **景别**：画面拍得多近，例如 wide 是全景，medium 是中景。
- **焦距**：数值越大，通常画面越近。
- **高度**：摄像机离地高度。
- **画面倾斜角（Roll）**：0° 表示地平线不倾斜，通常保持 0。
- **Look-at**：摄像机朝向的人物、物体或空间点。

## 数据真实性

每次保存都会建立不可变的 `S` 轨迹目录；轨迹批准、Agent 规划、摄像机批准和 Blender 作业通过 SHA-256 串联。修改轨迹只会取消当前下游选择，不会删除旧计划、旧视频或失败记录。系统不会把自动检查冒充人工批准，也不会自动调用 Seedance、Kling 或 VACE。
## 隔离的 DeepSeek Blender Codegen 实验

这是独立于现有 8770 向导的实验后端，不会修改人工标注器或现有 `runs`。它把完整 ShotScript、K0–K4 轨迹和 prompt 快照到 `CG<n>` 作业，然后只允许 DeepSeek-v4-pro 调用一次生成 `build_scene(context)`。代码先经过 AST 安全门，再由本地 Blender 渲染；宿主 runner 会移除 `DEEPSEEK_*` 环境变量，Blender 只接收哈希绑定的输入。

### 五个命令

```text
videoactagent codegen-blender prepare       # 只快照输入，不调用 API/Blender
videoactagent codegen-blender generate      # 一次 DeepSeek 请求，零重试
videoactagent codegen-blender render-smoke  # 640x360、8 FPS 的真实 Blender 视频
videoactagent codegen-blender render-full   # 同一份代码，960x540、24 FPS
videoactagent codegen-blender status        # 查看不可变作业状态
```

每个作业的证据位于 `runs/work/codegen_blender_v1/CG<n>/`：`source/input.json`、`api/request.json`、脱敏响应、`generated_scene.py`、`renders/<profile>/video.mp4`、`scene.blend`、三张 PNG、`codegen_manifest.json` 和 `render.log`。终态会明确标记 `api_failed`、`code_rejected`、`blender_failed`、`output_invalid`、`isolation_failed` 或 `succeeded`；已失败作业不能自动重试。

测试中的 API transport 是假的，只验证请求结构和脱敏；真实 Blender 集成使用手写 fixture，不代表模型质量。只有一次真实 CG 作业、完整视频人工观看、轨迹误差和保护树哈希都通过后，才算 DeepSeek 实验结果。该后端不接入当前向导，也不调用 Seedance、Kling、VACE。
