# 锁定前缀渲染失败修复设计

## 根因

P3 与其审批证据都声明 `locked_through_keyframe: K1`。`prepare_render()` 使用该审批值校验成功并创建 M1，但后台 `run_render_job()` 再次加载计划时没有传递审批中的锁定值，校验器因此使用默认 `None`，在启动 Blender 前报出 `locked prefix differs from the planning request`。

## 设计

- 后台渲染工作进程继续从已做 SHA-256 绑定的 `approval.json` 读取审批数据。
- 调用 `load_multicam_plan()` 时传入 `plan_approval.get("locked_through_keyframe")`，使准备阶段和执行阶段使用同一可信来源。
- 不删除或改写失败的 M1，不放宽规划校验，不在 job 中复制锁定状态。
- 修复后通过现有界面/API 新建 M2，并运行一次真实 Blender 渲染；不调用 DeepSeek 或其他视频 API。

## 验证

1. 增加回归测试：K1 锁定计划生成作业后，后台校验能够通过并实际到达受控的 Blender 进程边界。
2. 原有来源篡改测试继续证明异常输入不会启动 Blender。
3. 运行相关多摄像机测试。
4. 运行一次真实 M2，检查 `job.json`、`render.log`、三个摄像机视频、渲染清单和自动评估；如失败，保留真实错误且不伪造通过。

## 非目标

不修改轨迹、P3 规划内容、锁定语义、摄像机编译算法或 Blender 场景外观。
