# VideoActAgent Pipeline v2

这是新管线的独立边界。旧版 `videoactagent/`、旧版标注器和旧版实验接口暂时保持不变。

当前目标是：

```text
Prompt
  -> Agent Director
  -> ScenePlan + PhysicalStatePlan + Character/ObjectTrajectoryPlan
     + CameraTrajectoryPlan
  -> Blender Code Agent
  -> Sandbox Blender
  -> Shared-state multi-view Proxy
  -> ProxyVerifier
  -> 人工审批/反馈
  -> Appearance-only Prompt
  -> Backend Adapter
  -> 真实视频与评估
```

当前实现边界：

- 先复用现有三机位能力验证共享世界状态；
- 摄像机数量使用配置项，后续可扩展到八机位；
- 暂不构建 Syn4D 数据集、UE5 渲染器或训练流程；
- 暂不调用 VACE、Seedance 或其他外部 API；
- 每个模块单独审批、单独运行、单独保存证据。

参考实现：

- `third_party/VideoCoCo/`：只读参考，不作为本项目的直接复制代码；
- `videoactagent/`：已有功能的兼容来源，迁移前保持可启动。

