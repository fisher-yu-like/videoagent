# Prompt-only Blender Codegen 真实验证

日期：2026-08-05  
分支：`feature/codegen-lab-ui`

## 链路

本次使用真实 `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、DeepSeek-v4-pro 和 `D:\blender\blender.exe`，通过 8781 的 Prompt-only 页面提交 `prompts/station_reunion.txt`：

```text
Prompt → DeepSeek-v4-pro Planner → scene-plan JSON
      → ShotScript + 人物 K0–K4 轨迹
      → DeepSeek-v4-pro Blender Codegen
      → AST 安全检查 → Blender 5.1.2 → MP4
```

## PF2 结果

- Planner：1 次真实 API 调用，schema 校验通过。
- Blender Codegen：2 次真实 API 调用。第一次生成的代码缺少 `traveler_orange`，真实 Blender 返回退出码 2；第二次把该日志作为修复反馈后通过。
- 总 API 调用：3 次；自动修复重试：1 次；没有更换 seed。
- Blender：5 秒、8 FPS、640×360、40 帧，输出 MP4 可解码。
- 轨迹验证：最大人物位置误差 `0.089927`，低于 `0.15` 容差。
- 保护区前后 tree SHA-256 与 8770 guard 指纹一致。

最终视频：

`runs/work/codegen_blender_v1/PF2/renders/smoke/video.mp4`

对应的 `job.json`、输入快照、Planner/Codegen 请求与响应、AST 安全结果、Blender 日志、三张关键帧和 manifest 均在同一 `PF2` 目录中。

## PF1 依赖阻塞

第一次真实提交已完成 Planner、两次 Blender 渲染，但当时 8781 进程使用的 Python 环境没有 `imageio_ffmpeg`，导致视频验证器无法解码。该作业没有被标记为成功，也没有把它当作最终结果；切换到包含 `imageio_ffmpeg` 的 Python 3.12 环境后重新提交 PF2，才得到上面的通过结果。

## 视觉观察

PF2 的视频是真实 Blender 输出，轨迹和时长验证通过；当前 Codegen 默认使用受约束的简单几何体，因此人物外观仍是代理几何体，不是写实真人。这属于后续外观/资产模块问题，不影响本次 Prompt→Agent→Blender 全链路验证。
