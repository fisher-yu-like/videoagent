# Stage 2 实验报告：Control Bridge

日期：2026-07-29  
状态：通过（仅限本地控制资产准备）

## 结论

实际 station ShotScript 和 Stage 1 的真实 `.blend` 已被转换为三个独立 shot 的普通 prompt、电影语言 prompt、分时 prompt、首帧、尾帧和 proxy MP4。所有媒体均由本机 Blender 5.1.2 重新打开场景后实际渲染，不是占位文件；`control_bundle.json` 记录并重验了每个文件的字节数和 SHA-256。

本阶段证明 Control Bridge 能准备可追溯的本地控制输入，不证明 Seedance、Kling 或 VACE 已接收或遵循这些输入。

## 环境和资源

- Blender：`D:\blender\blender.exe`，5.1.2。
- 输入场景：`runs/stage1_blender/station_proxy.blend`。
- 本阶段未调用任何外部或付费 API。
- 本阶段未使用服务器、A100 或其他租赁 GPU。
- 未伪造公网 URL、task ID、后端响应或生成视频。

## 实际过程

正式命令：

```powershell
& 'C:\Users\sy\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' videoactagent/control_bridge.py --blender 'D:\blender\blender.exe' --shotscript examples/station_shotscript.json --blend runs/stage1_blender/station_proxy.blend --output-dir runs/stage2_control_bridge
```

最终退出码为 0，并输出 `CONTROL_BRIDGE_OK`。每个 shot 分别启动真实 Blender PNG/FFmpeg 渲染流程，无自动重试。

Blender 直接读取三个最终 MP4 的结果：

| Shot | 分辨率 | 帧数 | FPS | 时长 |
|---|---:|---:|---:|---:|
| s01 | 960×540 | 15 | 3.0 | 5.0 s |
| s02 | 960×540 | 15 | 3.0 | 5.0 s |
| s03 | 960×540 | 15 | 3.0 | 5.0 s |

## 最终媒体证据

| Shot | 控制 | 字节数 | SHA-256 |
|---|---|---:|---|
| s01 | first frame | 369597 | `531124ab4baed29ce3dc7dc9fa007985da21401c5841ce5345a3f20b6228ec4d` |
| s01 | last frame | 375266 | `fa6a1ff1dd25ffb7bf8856fe3ffc75f5417d0336997792d592dc263d7928dc30` |
| s01 | proxy video | 92783 | `2fe40427bed79725cc18b0394d0c0fb2ab9943fe542d1196c0d2b0815c168b2e` |
| s02 | first frame | 395327 | `f43acce0f61a81a6ef1487b6b1df8b1f0136eddf50bf536317bbe6e22150680b` |
| s02 | last frame | 442591 | `5a869d47b87ffcac353fdfefde42c4418f911012d950b6e03f30bbbaf0b1d96d` |
| s02 | proxy video | 123397 | `dfcdda3eace43a3376f295fc9ee48688458dbaa779b0e2aaaf40e2286b3e3e75` |
| s03 | first frame | 432014 | `e3d31950f016ff84699aabd91c8066e8569c15cce258601840801dd06e72e5f9` |
| s03 | last frame | 442195 | `4474345bcc06e59da628c051ee35525ff66c8f786e08c45e8f06327bc4bdf45b` |
| s03 | proxy video | 110984 | `fe38811a2f21fea7d029b8e833872e9f6313543f8e5bcbf1b239d78cc758f74c` |

`control_bundle.json` 为 4201 字节，SHA-256 为 `febf93a5ed5e1a3e8611322c280126239b57164ec56e429708f283d03f7aa617`。

独立 PowerShell 检查重新计算了全部九项媒体的 SHA-256，结果均为 `HashMatch=True`。

## 目视检查

- s01：A 从画面左侧接近 B，同时相机 truck right；首尾帧构图和人物位置明显不同。
- s02：A 继续接近 B，相机 dolly in；尾帧人物尺度明显增大。
- s03：人物保持位置，相机执行小幅 clockwise arc；透视和画面横向位置发生变化，轴线侧未翻转。
- s03 是较紧的双人近景近似，不是真实人体 over-the-shoulder，因为当前代理没有肩部和骨骼模型。

## 真实问题与修正

1. 直接运行包内脚本时曾因仓库根目录不在 `sys.path` 而无法导入 ShotScript；真实集成测试复现后修正入口路径。
2. Blender 实际输出文件名包含帧号或范围，例如 `frame0001.png`、`proxy0001-0015.mp4`；Control Bridge 根据真实命名解析后原子替换为语义文件名。
3. 最终 bundle 检查发现 s03 prompt 中出现小写 `actor b`。根因是通用词汇转换只处理下划线；新增失败测试后，在同一规范化入口将角色引用统一为 `actor A/B`。

## 后端就绪状态

`control_bundle.json` 明确写入：

```json
{
  "submission_ready": false,
  "submission_blocker": "public asset URLs are not configured"
}
```

bundle 中不存在 `http://`、`https://` 或 `task_id`。下一阶段必须先确定真实、受支持的资产上传方式和后端输入字段；在此之前不得把本地文件路径当作可提交 URL。

## 验证范围

最终全量测试包含 10 项。Control Bridge 集成测试会先真实创建临时 Blender 场景，再实际导出六张 PNG 和三段 MP4，并独立读取像素、文件大小和 SHA-256。其余 dry-run 和源代码安全测试仍只属于本地代码证据。
