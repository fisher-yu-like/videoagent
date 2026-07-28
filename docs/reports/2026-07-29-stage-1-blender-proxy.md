# Stage 1 实验报告：ShotScript → Blender Proxy

日期：2026-07-29  
状态：通过（仅限本地可执行镜头代理）

## 结论

受控的三镜头双人物 ShotScript 已由本机实际安装的 Blender 5.1.2 编译并渲染为 `.blend`、H.264 MP4、三张镜头中帧 PNG 和 Blender 求值后的轨迹报告。最终视频为 960×540、45 帧、3 fps、15.0 秒；场景可重新无界面打开。

这证明镜头和人物调度可以执行并产出可检查预览，不证明 Seedance、Kling 或 VACE 会遵循同一控制计划。

## 实际环境与调用

- Blender：`D:\blender\blender.exe`，5.1.2，构建 hash `ec6e62d40fa9`。
- 本阶段未调用任何付费或外部 API。
- 本阶段未使用服务器、A100 或其他 GPU 租赁资源。
- 输入是仓库内人工受控的合成故事，不是真实采集视频，也不是模型生成数据。

## 真实失败与修正

1. Blender 5.1 不接受旧的 `BLENDER_EEVEE_NEXT` 标识，改用实际枚举 `BLENDER_EEVEE`。
2. Blender 5.1 的图像/视频输出格式是互斥动态枚举；同一进程完成 FFMPEG 后切换 PNG 会抛出 `TypeError`。最终实现使用两个真实 Blender 进程：FFMPEG 主进程和重开 `.blend` 的 PNG 子进程。
3. Blender 5.1 的 Action API 不再提供旧式 `action.fcurves` 路径，改为在插关键帧前设置全局线性插值偏好。
4. 第一次真实目视检查发现第三镜头 65 mm 构图过紧，人物头部和 A 被裁切。将其改为 50 mm 并后移机位后重渲染；最终画面中两个人物头部和相对位置均清晰可见。

## 最终产物证据

| 产物 | 字节数 | SHA-256 |
|---|---:|---|
| `station_proxy.blend` | 127191 | `EB8AA8DC30343803714F63AD4A75DF9D24D49D46CCDEEC90655E75A0F5BE480B` |
| `station_proxy.mp4` | 346759 | `E9DA8B7A6C8211033541B02D217C23D5731277F69C69ADBDEF3DCBB2291029FE` |
| `trajectory_report.json` | 2531 | `321AC23592805B14F182E33C7C300B52D4BF0E7C4A83851408DE036D17BEE6AE` |
| `shot_01.png` | 370125 | `693283F8D89DA2A4A2A3DD83A68F883DB4D019868F5E2FA95C1DDB9806074F71` |
| `shot_02.png` | 420045 | `73EE63B1FC0D7893744BDAD494F23A0A9B7EFB62F25160D6321466CAC4DEE66B` |
| `shot_03.png` | 436789 | `EB4BB8ECA5CBC7444EE947F4107D6090D8B2419C4FF848C7E7F74646A82E9CBB` |

Blender 重新读取 MP4 的结果为 `VIDEO_OK 960 540 45 3.0 15.0`。重新打开 `.blend` 的结果为 `REOPEN_OK 27 1 45 960 540 3`，即 27 个场景对象、1–45 帧、960×540、3 fps。

轨迹报告的连续性检查显示：A 在 s01 末尾和 s02 开头均为 `[-1,0,0]`，在 s02 末尾和 s03 开头均为 `[0.5,0,0]`；B 在全部镜头保持 `[1.5,0,0]`。镜头运动分别为 `truck_right`、`dolly_in`、`arc_clockwise`。

## 验证命令与结果

正式入口：

```powershell
& 'C:\Users\sy\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' videoactagent/blender_runner.py --blender 'D:\blender\blender.exe' --shotscript examples/station_shotscript.json --output-dir runs/stage1_blender
```

最终全量测试：7 项通过，其中 Blender 集成测试实际启动 Blender、渲染临时产物并读取 PNG 像素；它不是 mock 渲染。源代码 dry-run 测试和安全扫描仍只属于本地代码证据。

## 已知限制

- 人物是几何代理，没有骨骼、步态、表情或遮挡调度。
- 当前 over-shoulder 是近景近似，不具备真实肩部模型。
- 只有单一场景和两名角色，尚未验证多视角生成模型的一致性。
- Seedance 上一次最小 smoke 请求在调用进程被超时终止前没有拿到 task ID，外部结果未知；为避免重复计费没有重试，仍标记为未验证。
- Kling 和 VACE 尚未进行真实生成验证。
