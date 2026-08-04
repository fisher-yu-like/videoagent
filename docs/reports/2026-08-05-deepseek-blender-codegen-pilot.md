# DeepSeek Blender Codegen CG1 Pilot

日期：2026-08-05  
作业：`runs/work/codegen_blender_v1/CG1`  
状态：`blender_failed`（这是一次有效的失败证据，不是通过）

## 实际调用

- 模型：`deepseek-v4-pro`
- DeepSeek API：1 次
- 自动重试：0 次
- Seedance/Kling/VACE/GPU 服务器：0 次
- API evidence：`CG1/api/evidence.json`
- 原始请求/脱敏响应：`CG1/api/request.json`、`CG1/api/response.json`
- 生成代码：`CG1/api/generated_scene.py`
- AST 安全门：通过；只使用 `bpy`、`mathutils`，代码大小 4711 字节（模型响应文本）

## 输入与隔离

- ShotScript：`station_reunion` / `whole`，5 秒，8 FPS，640×360
- 轨迹：两个 actor，各自严格 K0–K4；K0/K4 与 ShotScript 起止位置绑定
- 准备阶段 API 调用数：0
- 保护工作区：`C:\Users\sy\Desktop\videoactagent\runs\work\my_story`
- 准备前和 smoke 失败后的保护树 SHA-256：
  `a327c6dea4c622b2d1cbba3d9ad8e442b09499977d5802d3ab3fa5f28741e9c2`
- 8770 guard：准备时和复核时均 HTTP 200；响应摘要未变化

## 真实 Blender 结果

使用 `D:\blender\blender.exe`（Blender 5.1.2）运行同一份未修改的 `generated_scene.py`。
smoke 在执行 `build_scene(context)` 时失败，错误为：

```text
AttributeError: 'Action' object has no attribute 'fcurves'
```

失败位置是模型生成的兼容性代码：
`obj.animation_data.action.fcurves`。Blender 5.1 使用分层 Action API，不能直接访问旧版 `fcurves` 属性。
因此没有发布 MP4、BLEND、PNG 或轨迹指标，也没有进行人工视频评分；full render 按计划没有启动。

## 哈希注意事项

CG1 生成时 Windows 文本写入把 `\n` 转为 `\r\n`，导致 API evidence 中的代码哈希（响应文本）与落盘文件字节哈希不同：

- API evidence：`61c263e44f974843f00011675699e56a7d6a2fba56039fa9963a56798df8a7cc`
- 落盘 `generated_scene.py`：`91cb5bb05e3533f16d7e4fe2969bb24872430c0f73f3b10b3453a263df66c93f`

这不是篡改生成代码；两者差异来自换行编码。已在后续实现中改为二进制 UTF-8 写入并增加单元测试。CG1 原始证据保留，不用修订后的逻辑覆盖。

## 结论

端到端实验已经真实走到“DeepSeek 单次生成 → AST 门 → Blender 执行”边界，但 CG1 没有生成可播放视频，不能宣称 codegen pilot 成功，也不能据此评价画面质量。下一步应新建 CG2 决策，先在 prompt/适配层要求 Blender 5.1 Action API，或在可信入口提供明确的兼容 shim；未经新作业批准，不应重放 CG1 或偷偷修改其原始代码。
