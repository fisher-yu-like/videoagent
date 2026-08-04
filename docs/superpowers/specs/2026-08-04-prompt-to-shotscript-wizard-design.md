# Prompt 到 ShotScript 一体化导演向导设计

日期：2026-08-04  
状态：用户已确认总体方案、前端流程与模型选择

## 1. 目标

在现有 `director-multicam` 前端最前面补齐“自然语言 Prompt → 可执行 ShotScript”阶段，形成单页端到端向导：

```text
输入故事 Prompt
→ DeepSeek 生成场景草案
→ 人工表单审批 ShotScript
→ Blender 渲染第一版单机位 Proxy
→ 人工审批人物与物体轨迹
→ DeepSeek 规划 Camera A/B/C
→ Blender 渲染三视角 Proxy
→ 人工最终批准并导出
```

Agent 负责语义规划，本地确定性代码负责校验、编译和渲染。任何 LLM 输出都不能作为 Blender Python 代码执行。

## 2. 范围

本阶段包括：

- 使用 `deepseek-v4-flash` 从中文或英文 Prompt 生成结构化场景草案；
- 在同一前端用简化表单修改并批准草案；
- 批准后调用现有 Blender 编译器生成第一版完整 Proxy；
- 将批准结果无缝交给现有人物轨迹审批与三机位 Agent 规划流程；
- 为 Prompt、LLM 请求/响应、ShotScript、人工修改、Proxy 和审批建立不可覆盖的版本绑定；
- 保持原 `director-loop` 标注器和现有已准备工作区可用。

本阶段不包括：

- 由 LLM 生成或执行任意 Blender Python；
- 自动下载或生成新 3D 资产；
- Seedance、Kling、VACE 或显式控制模型推理；
- 自动重试、自动切换模型或在失败时伪造字段；
- 多镜头分别生成与拼接。

## 3. 场景能力边界

场景规划器只能选择现有受支持模板：`station`、`city`、`forest`、`studio`、`cafe`、`warehouse`，或选择 `generic` 通用模板。第一版支持一个连续故事、1 至 3 个人物、可选关键物体、人物起终点、基础动作和一台用于检查调度的初始摄像机。

初始摄像机不承担最终摄影设计。Camera A/B/C 仍然只在人物与物体轨迹被人工批准后由现有多机位规划器生成。

## 4. 组件边界

### 4.1 场景规划适配器

新增独立的 Prompt 场景规划接口，读取 `DEEPSEEK_API_KEY` 和 `DEEPSEEK_BASE_URL`，模型固定为 `deepseek-v4-flash`。每次调用都必须由用户点击触发，调用一次且零自动重试。适配器保存脱敏请求、原始响应、解析结果、耗时和调用次数，不保存密钥。

输出使用独立 `ScenePlanDraft` 合同，包含模板、时间线、人物、物体、初始相机和中文规划说明。LLM 输出先按此合同校验，再由确定性转换器生成现有 `ShotScript`，避免把宽松 LLM JSON 直接交给 Blender。

### 4.2 ShotScript 校验与编译

现有 `ShotScript` 继续作为 Blender 的唯一权威输入。本地代码检查：

- 恰好一个连续 shot；
- 时长、FPS 和坐标为有限且合法的数值；
- 人物 ID 唯一且总数为 1 至 3；
- 起终点位于世界边界内；
- 模板、动作、物体和初始镜头属于支持集合；
- Camera look-at 目标存在；
- 输出能被当前 Blender Proxy 编译器执行。

DeepSeek 不生成 Blender 代码。现有 `blender_runner.py` 调用 `D:\blender\blender.exe`，`blender_proxy.py` 根据已批准 ShotScript 创建模板环境、低模人物、关键物体、关键帧、灯光和初始摄像机，并生成 MP4、`.blend`、日志和轨迹报告。

### 4.3 向导控制器

扩展 `director-multicam` 工作区状态，但不改变已有工作区的读取能力。新工作区依次处于：

1. `prompt_entry`
2. `scene_plan_review`
3. `reference_review`
4. `staging_review`
5. `camera_plan_review`
6. `multicam_review`
7. `complete`

后一步只接受已批准前一步的哈希绑定。返回前一步后，已有后续版本保留但标记为过期；用户可以从任意批准版本重新分叉。

## 5. 前端设计

页面顶部显示七段简洁进度条；当前步骤展开，已完成步骤折叠为摘要，未开放步骤禁用。高级 JSON、哈希和日志放入“技术详情”折叠区。

### 5.1 输入故事

提供故事 Prompt 和时长输入，默认时长 5 秒。用户点击“生成场景草案”后才调用一次 DeepSeek。页面显示当前项目累计 API 调用次数。

### 5.2 审批场景草案

表单展示并允许修改：场景模板、时长、FPS、世界范围、人物名称/颜色/动作/起点/终点、关键物体及其位置、初始镜头景别/运动/关注目标，以及 Agent 的中文解释。用户不需要编辑 JSON。

人工保存只产生新的本地场景版本，不调用 API。“让 Agent 根据反馈重写”才调用一次 DeepSeek。批准后才开放 Blender 渲染。

### 5.3 审批第一版 Proxy

显示完整视频和人物运动摘要。用户可以批准并进入轨迹编辑器，返回表单修改参数，或输入反馈生成新的 Agent 草案。每次重新渲染生成新的 Reference 版本，不覆盖旧视频。

### 5.4 后续现有步骤

人物/物体轨迹阶段复用现有 K0 至 K4 自动初始化、绘制、删除、撤回和冻结前缀功能。批准轨迹后，复用现有 DeepSeek 三机位职责规划和人工审批。最后同步展示三个完整 Proxy，并允许返回轨迹或摄像机规划阶段重新分叉。

## 6. 版本与证据

新工作区使用以下目录：

```text
project/
├─ source/prompt.txt
├─ scene_plans/SP1/{draft.json,shotscript.json,request.json,response.json,record.json}
├─ references/R1/{reference.mp4,scene.blend,trajectory_report.json,record.json}
├─ staging/S1/
├─ camera_plans/P1/
├─ iterations/M1/
├─ state.json
└─ multicam_manifest.json
```

所有 record 保存来源路径、SHA-256、字节数、父版本和创建时间。`R1` 必须绑定某个已批准的 `SP`；`S1` 必须绑定某个 `R`；`P1` 必须绑定已批准的 `S`；`M1` 必须绑定已批准的 `P`。已有结果永不原地覆盖。

## 7. 失败处理

- DeepSeek 超时、非正常结束或非法 JSON：保存真实响应并停留当前步骤；不自动重试。
- 场景草案缺字段或越界：显示字段级错误，不补造默认语义。
- Blender 非零退出、缺少成功标记或缺少视频：版本标记失败并保存日志，不开放审批。
- 来源哈希变化：拒绝继续并提示绑定不一致。
- 返回旧步骤：后续结果标记过期，但文件保留以供审计。
- 页面刷新：从 `state.json` 恢复当前步骤和未提交表单，不要求用户重新开始。

## 8. 测试与真实验收

单元测试覆盖模型名、一次调用、响应解析、受支持枚举、越界、版本分叉、批准门禁、哈希绑定和旧工作区兼容。HTTP 页面测试覆盖七步状态、表单保存、批准跳转、失败显示和页面恢复。

真实验收只使用真实 DeepSeek 响应和本机 Blender 输出：至少输入一个全新 Prompt，人工批准其 ShotScript，渲染完整第一版 Proxy，再完成轨迹批准、三机位规划与三视角 Blender 渲染。报告 API 调用次数、模型、耗时、文件哈希、视频时长和人工审批状态；单元测试素材不得作为真实实验结果。

## 9. 成功条件

- 用户无需编写 JSON 或 CLI 即可从 Prompt 开始完成整条流程；
- `deepseek-v4-flash` 的输出必须经过表单审批和本地校验才能进入 Blender；
- 第一版 Proxy 的全部可执行参数来自已批准 ShotScript；
- 人物轨迹批准仍发生在三机位规划之前；
- 原人工标注器和已有多机位工作区继续可启动；
- 任意阶段失败都不会被显示为通过，也不会覆盖历史证据。
