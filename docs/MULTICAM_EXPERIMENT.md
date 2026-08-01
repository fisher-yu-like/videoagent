# 多视角 Agent 真实实验记录（2026-08-01）

## 结论

本轮完成了 6 个不同场景的完整 5 秒 Blender 参考 Proxy，并执行了 6 次正式 DeepSeek 三机位规划请求。基础 Proxy 为真实 Blender 输出；没有 Seedance、Kling、VACE 调用，没有自动重试，没有伪造人工批准。

当前状态：5 个场景 `planned_waiting_human`，1 个场景 `failed`。因为人工尚未批准规划，所以 6 场景三机位正式渲染数为 0，人工批准数为 0；不能称为六场景端到端实验完成。

## 参考 Proxy

全部视频均为 15 帧、3 FPS、960×540、5 秒；目录位于 `runs/work/agent_multicam_suite_20260801/`。

| 场景 | 字节 | SHA-256 |
|---|---:|---|
| cafe_handoff | 139110 | `4f624e6d0b7ce90ca6aba8de14f118587e8c2023bf6501ca4f2daa1ea7cdd0a2` |
| city_crosswalk | 148671 | `515d62bbe235eeca72606628926fe50dd5d9d865d07c71ab09a5dde920908315` |
| forest_path | 142184 | `fb2dd41a5c8f5d6e2c64426c11f90ca8a9efc018cecd2d242b6cca477a0e5299` |
| station_reunion | 80109 | `3784483013d5b292d4dc08cc0333774ab7339f38bcd29e0ddd669b46341a0dae` |
| studio_room | 126242 | `0d604f01ce4696970c418e2bd483c52f4878a1b1c015da5f668974875f1ed63f` |
| warehouse_chase | 212275 | `5d477eab8bff0d78833d3a045face6f9ae07d4cdf1888a46d44f6b3509f37263` |

## DeepSeek 规划

环境变量在执行进程中可见。每场景调用 1 次，零自动重试，总调用 6 次。

| 场景 | 结果 | 耗时 | 调用 / 重试 | 说明 |
|---|---|---:|---:|---|
| station_reunion | planned_waiting_human | 52.1 s | 1 / 0 | P1 schema 通过 |
| city_crosswalk | planned_waiting_human | 47.93 s | 1 / 0 | P1 schema 通过 |
| forest_path | failed | 45.29 s | 1 / 0 | 返回的 `actor_staging` 为空，被严格 schema 拒绝 |
| studio_room | planned_waiting_human | 58.50 s | 1 / 0 | P1 schema 通过 |
| cafe_handoff | planned_waiting_human | 50.87 s | 1 / 0 | P1 schema 通过 |
| warehouse_chase | planned_waiting_human | 53.49 s | 1 / 0 | P1 schema 通过 |

## 已验证的软件链

单独的真实 Blender 集成检查已跑通：批准过的测试规划 → 三套 K0–K4 数值轨迹 → 一个共享 `.blend` → 三路各 3/3 帧 MP4 → 每路 Depth + CryptoObject00/01/02 EXR → 同步、职责目标可见性、相邻视角角度和相机/人物间距评估。该检查是代码集成证据，不冒充用户场景的人工实验结论。

## 下一道人工作业门

打开 `http://127.0.0.1:8770` 查看 station 的 Agent 规划。只有用户点击“批准职责规划”后，系统才允许渲染该场景三路正式 Proxy。forest 不自动重试；需要用户明确选择重新规划或人工修正失败响应。
