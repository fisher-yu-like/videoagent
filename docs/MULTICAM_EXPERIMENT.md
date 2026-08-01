# 多视角 Agent 当前真实实验状态（2026-08-01）

## 当前结论

仓库现有六个不同场景的完整 5 秒 Blender 参考 Proxy，均为本地真实重新渲染：15 帧、3 FPS、960×540。本轮恢复调用外部 API 0 次，自动重试 0 次。

当前 station 工作区位于 `runs/work/agent_multicam_suite_20260801/station_reunion/`，状态是人物/物体轨迹 `S1` 尚未人工批准；当前没有 DeepSeek 计划，也没有三机位 iteration。`http://127.0.0.1:8770` 从向导第一阶段开始。

## 证据恢复说明

合并功能分支后的 Windows worktree 清理遇到 `runs` Junction。`git worktree remove` 取消 worktree 注册后错误跟随链接，删除了原 `agent_multicam_suite_20260801` 目录。原六次 DeepSeek 请求/响应、审批文件和 station M1 视频没有本机备份，无法恢复，因此不能继续作为本地实验依据，也没有用伪造文件替代。

随后用当前源码、原六份 prompt、原六份 ShotScript 和 `D:\blender\blender.exe` 完全离线重建六个参考 Proxy。新 MP4 与文档曾记录的旧 SHA-256 均不同，故它们被明确记为重新生成，而不是字节级恢复。

## 当前六个参考 Proxy

| 场景 | 当前 SHA-256 |
|---|---|
| cafe_handoff | `56ddfb211087e534f43f6fbc6da4b5f394d1100cd8ab6106640264bbb72adc4e` |
| city_crosswalk | `d3027447db5aab5b595f0ea8dec203b8b1666ca80275303df17c078d79182e7e` |
| forest_path | `0354fe894d3092104d97ccee119f1f668d5d8aba92a4b41b55671eb8ede39823` |
| station_reunion | `7438c9d06995fa80ffe5d037742402b2e26b4f1005bd8624a14275cd41b1ee9e` |
| studio_room | `8598fbd4df83d5f66950962ad8f5d58e1abcc23cdba241a3d727058576f4de14` |
| warehouse_chase | `9e77dfd84cc5ad99ed03d61d6be75c7fe09f9391a71d95b67152384bc8ed4d36` |

## 已验证的软件链

自动化集成测试真实运行过以下短链路：批准 S1 → 规划 P1 → 三套 K0–K4 摄像机状态 → 一个共享 `.blend` → A/B/C 三路 MP4 → 每路 Depth 与 CryptoObject00/01/02 → 自动几何评估。三路 MP4 均被实际解码，测试不冒充用户对构图的人工批准。

正式 station 工作区尚未执行上述 P1/M1 步骤。接下来应由用户在网页中检查并批准 S1，再决定是否进行一次真实 DeepSeek 规划调用。
