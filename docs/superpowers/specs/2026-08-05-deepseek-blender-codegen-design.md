# DeepSeek-v4-pro Blender Codegen Lab 设计

日期：2026-08-05

## 目标

新增一条与现有导演向导完全隔离的实验链路：把已验证的 ShotScript、人物/物体轨迹和可选摄像机轨迹复制为不可变输入快照，调用一次 `deepseek-v4-pro` 生成受约束的 Blender Python 场景代码，再由本地 `D:\blender\blender.exe` 真实渲染视频并保存完整证据。

这项实验只回答一个问题：在相同故事、时长和轨迹输入下，DeepSeek 生成的 Blender 场景代码是否能在保持控制约束的同时，比当前固定 Blender 模板产生更丰富的 Proxy。

## 明确不做

- 不替换或修改现有 `director-wizard`、`director_multicam.py`、`blender_proxy.py` 和端口 8770 的运行路径。
- 不覆盖 `runs/work/my_story` 或任何已有 Proxy、Reference、三机位视频和报告。
- 不连接 Seedance、Kling、VACE，也不讨论真人化质量。
- 不训练模型，不搜索多个 seed，不在失败后自动重试 API。
- 第一轮不做多镜头剪辑；输入是一个完整连续镜头。
- 第一轮不把 Codegen 结果提升为当前向导的正式结果。

## 方案选择

考虑了三种方案：

1. 让 DeepSeek 自由生成包含场景创建、文件管理和渲染调用的完整脚本。自由度最高，但输出路径、帧数和失败状态不可控，执行风险也最高。
2. 让 DeepSeek 生成独立的 Blender 场景构建代码，由可信本地运行器负责输入、渲染和证据。该方案保留代码生成能力，同时把不可变输出和失败判断留在确定性程序中。
3. 让 DeepSeek 只输出 JSON，再由固定模板渲染。成功率高，但不能验证“LLM 直接生成 Blender 代码”的研究问题。

采用方案 2。

## 隔离边界

实现阶段在独立 Git worktree 和独立分支中进行。现有工作区继续提供端口 8770 服务，不从实验分支重启现有服务。

实验运行目录固定为：

```text
runs/work/codegen_blender_v1/CG<n>/
```

每个实验编号只创建一次，禁止复用和覆盖。实验开始前生成 `baseline_guard.json`，记录：

- 当前主工作区 Git commit；
- 端口 8770 的 HTTP 状态和 session 摘要；
- 当前 wizard state、approved Reference、current staging、current plan 和现有视频的 SHA-256；
- 所有被复制输入的原路径、字节数和 SHA-256。

实验结束后重新计算这些记录。任何主线哈希变化都使本次实验进入 `isolation_failed`，即使新视频已经渲染成功也不能标记为成功。

## 数据流

```text
主线已验证输入（只读）
  -> 原子复制和 SHA-256 绑定
  -> CodegenInput 规范化
  -> 单次 DeepSeek-v4-pro 请求
  -> 严格 JSON 响应解析
  -> generated_scene.py
  -> Python AST 安全门
  -> 本地 Blender 后台进程
  -> MP4 / BLEND / manifest / 真实采样帧
  -> 自动结构检查
  -> 人工播放审看
  -> 主线隔离复核
```

## CodegenInput 契约

可信本地程序生成 `input.json`。模型不能直接读取主工作区文件。输入包含：

- `schema_version = "blender-codegen-input-1.0"`；
- `scene_id`、`shot_id` 和完整故事 prompt；
- 规范化 ShotScript，包括环境、世界边界、人物、物体、动作和完整时长；
- K0、K1、K2、K3、K4 人物/物体轨迹；
- 可选的摄像机位置、注视点、焦距和 Roll；
- `render_contract`，包括 FPS、分辨率、帧范围、输出相机名和期望文件名；
- 所有来源文件的 SHA-256。

第一轮 station pilot 使用一个主摄像机。数据结构允许后续携带 Camera A/B/C，但只有单摄像机 pilot 通过后才扩展到三机位输出。

## DeepSeek 响应契约

模型固定为 `deepseek-v4-pro`，忽略环境中的模型覆盖值。每个 CG job 最多调用一次，没有自动重试。

模型必须返回一个 JSON 对象：

```json
{
  "schema_version": "blender-codegen-response-1.0",
  "summary": "short scene construction summary",
  "python_code": "import bpy\n...\ndef build_scene(context):\n    ...\n"
}
```

`finish_reason` 不是正常完成、响应为空、不是 JSON、字段不精确、代码超过 40 KiB 或代码为空时，job 直接失败。原始请求、原始响应、HTTP 状态、耗时、模型名、调用次数和错误必须保存。API key 和 URL 中的 credential 不得进入证据文件。

## 生成代码契约

`generated_scene.py` 只能声明常量、导入允许模块、定义辅助函数和定义以下入口：

```python
def build_scene(context: dict) -> None:
    """Create Blender objects, materials, lights, animation and cameras."""
```

可信 Blender entrypoint 在内存中把 `input.json` 解析为 `context`，重置默认场景，然后调用此函数。生成代码不负责：

- 读取输入文件；
- 决定输出目录；
- 保存 `.blend`；
- 设置最终帧率、分辨率、帧范围和编码器；
- 启动渲染；
- 写成功标记。

这些职责全部由可信本地 entrypoint 完成。因此即使模型代码漏设渲染参数，也不会生成时长不明或路径不明的输出。

## 安全门

AST 检查只允许导入 `bpy`、`math` 和 `mathutils`。拒绝：

- `os`、`sys`、`pathlib`、`subprocess`、`socket`、`requests`、`urllib`、`shutil`；
- `eval`、`exec`、`compile`、`open`、`__import__`；
- 动态 import；
- 顶层函数调用；
- `bpy.ops.wm`、`bpy.ops.script`、`bpy.ops.render`；
- Blender 用户设置修改；
- 外部库加载、文件删除和网络访问入口。

通过 AST 不代表操作系统级安全。因此 Blender 仍使用 `--background --factory-startup`、独立工作目录、收缩后的环境变量和硬超时运行。超时后只终止本次 Blender 进程，不扫描或删除其他目录。

## 可信 Blender 运行器

运行器采用：

```text
D:\blender\blender.exe --background --factory-startup --python codegen_blender_entry.py -- ...
```

entrypoint 负责：

1. 验证输入、生成代码和 job 记录的 SHA-256；
2. 清空默认场景；
3. 受控加载已经通过 AST 检查的 `generated_scene.py`；
4. 调用 `build_scene(context)`；
5. 检查至少存在要求的人物、环境对象、灯光和有效摄像机；
6. 强制设置 FPS、分辨率、frame_start、frame_end、H.264 和 MP4 输出路径；
7. 保存 `scene.blend`；
8. 真实渲染完整动画；
9. 在 K0、K2、K4 读取人物和摄像机 Blender 世界坐标；
10. 写 `codegen_manifest.json` 和成功标记。

外层 runner 只在 Blender 退出码为 0、成功标记存在、MP4 非空且 manifest 与输入哈希一致时进入输出验证。

## Job 状态与失败证据

状态机为：

```text
prepared
  -> generating
  -> code_validated
  -> rendering
  -> succeeded
```

终止状态包括：

- `api_failed`；
- `code_rejected`；
- `blender_failed`；
- `output_invalid`；
- `isolation_failed`；
- `succeeded`。

失败目录和日志不可删除。原始代码失败后不自动修改，也不把人工修补后的结果冒充原始模型结果。若以后允许人工修补，必须创建新的派生 job，并记录父 job 和代码差异。

## 实验产物

```text
CG1/
  job.json
  baseline_guard.json
  source/
    prompt.txt
    shotscript.json
    trajectory.json
    camera_bundle.json        # 有输入时才存在
    input.json
  api/
    request.json
    response.json
    evidence.json
  code/
    generated_scene.py
    safety_report.json
  render_smoke/
    scene.blend
    video.mp4
    codegen_manifest.json
    render.log
    frames/first.png
    frames/middle.png
    frames/last.png
  render_full/                # smoke 通过后才存在
    scene.blend
    video.mp4
    codegen_manifest.json
    render.log
    frames/first.png
    frames/middle.png
    frames/last.png
  review/
    human_review.json
```

所有 JSON 中引用的本地文件必须包含相对路径、字节数和 SHA-256。成功 job 的 inventory 必须与磁盘文件一一对应。

## 第一轮真实 pilot

使用已有 `station_reunion`：一个完整 5 秒镜头、两个人物、K0-K4 轨迹和固定主摄像机。它比当前三人物森林故事更容易区分“代码生成失败”和“场景复杂度过高”。

顺序如下：

1. 复制输入并建立 baseline guard，不调用 API；
2. 调用一次 DeepSeek-v4-pro；
3. 对原始生成代码做 AST 检查；
4. 若通过，使用 640x360、8 FPS 做真实 smoke render；
5. 若 smoke 通过，复用同一份代码以 960x540、24 FPS 渲染完整版本；
6. 不为 full render 再次调用 DeepSeek；
7. 与相同 ShotScript 的现有确定性 Blender Proxy 并排人工审看；
8. 复核 baseline guard。

该 pilot 的预算是 1 次 API 调用和最多 2 次本地 Blender 渲染。任何失败都不自动产生第二次 API 调用。

## 验收标准

自动验收必须全部满足：

- API 证据显示 `deepseek-v4-pro`、`api_call_count = 1`、`retry_count = 0`；
- 生成代码通过 AST 安全门且只有一个 `build_scene`；
- Blender 真实退出码为 0；
- MP4 可解码，实际帧数、FPS、分辨率和时长符合 render contract；
- 非空采样帧存在且哈希不同，避免把静态占位文件当视频；
- 场景包含所有主要人物和有效摄像机；
- K0、K2、K4 人物位置与输入轨迹的世界坐标误差不超过 0.15 世界单位；
- manifest 的输入、代码、BLEND、MP4 和采样帧哈希全部可复核；
- 实验后主线文件哈希不变，端口 8770 仍返回 HTTP 200。

自动验收不能替代人工评价。人工 reviewer 必须观看完整视频并标注：故事语义、人物可辨识性、运动连贯性、环境丰富度、灯光、穿模、消失、瞬移和摄像机合理性。最终结论允许是失败或不确定。

## 测试策略

测试分四层：

1. 纯单元测试：输入契约、严格 JSON、AST 白名单、哈希和状态机。
2. API 边界测试：使用本地模拟响应验证模型名、一次调用、错误分类和密钥脱敏；不把模拟响应当真实实验。
3. 真实 Blender runner 测试：使用人工编写的安全 fixture 脚本运行一次本地 Blender，只证明执行器和证据链真实可用，不声称 DeepSeek 成功。
4. 真实 pilot：一次 DeepSeek API 调用和真实 Blender 渲染，产物由用户人工观看。

每层必须明确标记证据类型，禁止把 mock、fixture 或静态文件检查报告为真实生成模型效果。

## 独立实验前端

只有 station pilot 成功后才实现独立的 `codegen-lab` 页面，默认端口 8781。页面只提供来源选择、输入摘要、一次代码生成、安全状态、本地渲染、视频并排播放和人工评价。它不注册到现有 8770 路由，也不修改当前向导状态。

至少两个不同场景均通过真实人工审看后，才能讨论把 Codegen 作为现有向导中的可选 Proxy backend。即使接入，也必须由用户显式选择，不能改变当前默认 Blender 路径。

## 交付判定

第一轮交付不是“DeepSeek 代码一定优于固定模板”，而是形成一个可复现、可失败、不会污染主线的真实实验闭环。只有当 station pilot 同时满足自动验收和人工认为场景丰富度有价值时，才扩展到第二个场景和三机位。
