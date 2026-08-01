# DeepSeek 规划截断修复设计

## 问题

`station_reunion` 的 P1 调用真实返回 `finish_reason=length`。`deepseek-v4-pro` 默认思考模式消耗了 4096 token 输出预算，导致最终摄像机规划 JSON 截断，现有解析器因此拒绝响应。轨迹保存和 S3 数据本身没有失败。

## 设计

- 在结构化摄像机规划请求中显式发送 `thinking: {"type": "disabled"}`。
- 保持 `deepseek-v4-pro`、`max_tokens=4096`、单次调用和零自动重试，避免增加隐藏成本。
- 若 `finish_reason=length`，返回明确的截断错误，并把原始请求、响应和失败状态继续写入证据目录。
- 保留失败的 P1。修复后下一次人工触发生成 P2，不覆盖历史证据。
- 若一次真实 P2 调用仍不能正常完成，再将默认模型改为 `deepseek-v4-flash` 后人工触发下一次规划；不在一次请求内自动切换模型。

## 验证

1. 单元测试确认请求包含关闭思考模式字段。
2. 单元测试确认 `finish_reason=length` 会产生明确错误且不会重试。
3. 运行相关现有测试。
4. 人工触发一次真实规划，检查 P2 的 `finish_reason=stop`、JSON 可解析、规划文件及证据文件完整。若失败，再执行 Flash 方案。

## 非目标

不修改轨迹、Proxy、前端工作流或自动重试策略。
