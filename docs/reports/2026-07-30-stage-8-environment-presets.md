# Stage 8 实验报告：多场景 Blender Environment Presets

## 结论

本阶段把 Blender proxy 从单一硬编码车站扩展为四个确定性环境：`station`、`city_crosswalk`、`forest_path`、`studio_room`。三个新环境均由本机 Blender 5.1.2 真实生成 5 秒 MP4 和 `.blend`，不是占位数据；每个视频均实际解码为 15 帧、960×540、15 个不同的原始解码帧，且对应 `.blend` 已在后台重新打开验证。

没有调用 Seedance、Kling 或其他生成 API。本阶段没有使用 GPU 服务器；同期另行获批的 VACE 12-run 服务器矩阵不计入本阶段，并单独报告。

## 实现边界

- `ShotScript.environment_preset` 仅接受四个固定值，不执行 LLM 生成的自由 Blender 代码。
- 新 ShotScript 必须显式声明 preset。
- 历史 `station_platform` 单独保留 `station` 兼容默认值，使 Stage 1–7 的原始 ShotScript 继续保持 2,986 bytes 和 SHA-256 `149c5ae13bbcb6e98b8774808956f8062d82903c504f8c3279c9ea7019a1dc28`。
- Legacy station 的 canonical snapshot 同样不写入推导字段；最新代码重编译后 canonical SHA 仍为 `ade0f8dedd55cd4e65b1595e4d20f51720a626cc91aa07d5be64da20be046dfb`，`compiled_control.json` SHA 仍为 `50b66351a657b768723c2fe8712093fde9bcefd6c87f00ce4e8891acddb4063b`。
- 角色调度和相机关键帧仍走既有编译器；本阶段只扩展场景几何、材质和确定性调度输入。
- 输出名仍沿用 `station_proxy.*`，这是为避免破坏旧调用者的兼容命名，不能据文件名判断实际场景；`trajectory_report.json` 中新增的 `scene_id` 与 `environment_preset` 才是正式证据。

## 真实本地实验

所有有效结果都使用：

```powershell
python videoactagent\blender_runner.py `
  --blender D:\blender\blender.exe `
  --shotscript <new-scene-shotscript.json> `
  --output-dir <fresh-run-directory>
```

| 场景 | 实际控制 | MP4 bytes | MP4 SHA-256 | 解码结果 |
|---|---|---:|---|---|
| city_crosswalk | A 横向接近 B；camera truck right | 135,577 | `847124048d4acd88ea5ea1e968b591bbe1632cfea842fa7b1eb080cf56d40737` | 15 帧，960×540，15 unique |
| forest_path | A 横向接近 B；camera dolly in | 122,372 | `1566b25e2f8707d7c6132078e568896bc9b63fa0cc60f131500a5f968601e8c4` | 15 帧，960×540，15 unique |
| studio_room（修订） | 两人静止；camera truck right | 109,750 | `a45616f89c34ba718d535d6e39e6ac5d52ec58c4d89e4f9a23567bbc1614be8a` | 15 帧，960×540，15 unique |

重新打开 `.blend` 后分别验证了场景特有对象 `city_building_left`、`tree_trunk_0`、`studio_back_wall`，对应 object 数为 31、26、22，帧范围均为 1–15。

主要文件哈希：

| 文件 | bytes | SHA-256 |
|---|---:|---|
| `city_crosswalk/station_proxy.blend` | 138,579 | `ca008dc34719a0601a62d23b2a864d5b5afb7e455e644a70d1ec1117d1ebe858` |
| `city_crosswalk/trajectory_report.json` | 1,048 | `9f11537b9a2491657a348d868d86e6e46fdbc0fa9faf80e8a55212b8eff8a518` |
| `forest_path/station_proxy.blend` | 151,191 | `a00b78ca43bb7e74ff78836c45ec389d36f74c1eb67588e154f737b191dcbfcf` |
| `forest_path/trajectory_report.json` | 1,037 | `60d93a982a4ff631f791ebffbb1dd47f1915d4cfe13d79c53c54c677092ac469` |
| `studio_room_fixed/station_proxy.blend` | 123,108 | `eaf4debb3add51a7ce8d51b35dce92dc083d58885ce2a4b74ab66baf050b3232` |
| `studio_room_fixed/trajectory_report.json` | 1,040 | `0eccbbff903ec7b832b5baf55beb18ce97ea915a66c5d934c0eede43d92d084c` |

## 视觉检查

- City：斑马线、道路、路灯和建筑体可见；A 从画面左侧向 B 靠近，同时背景随横移机位改变。
- Forest：泥土路径、草地、树干/树冠和路边石块可见；dolly-in 使两人随时间真实放大，A 同时向 B 靠近。
- Studio：墙面吸声板、沙发和暖色灯条可见；两人世界坐标不变，构图仅随 camera truck 改变。
- 三个场景首/中/尾帧中 A、B 均可见，未观察到遮挡或角色交换。

## 发现的问题和修订

Studio 初版把镜头标成 `arc_clockwise`，但既有普通 ShotScript 编译路径只在 camera 起止点之间线性插值，因此真实画面是 truck，不是圆弧。该结果没有被记为通过，保留于 `runs/stage8_environment_presets/studio_room/` 作为诊断证据；ShotScript 已修订为真实语义 `truck_right`，有效结果输出到 `studio_room_fixed/`。

这说明“motion 标签正确”不等于“相机路径正确”。真正圆弧的通用插值暂未并入本阶段；另行运行的 coupled-arc VACE 实验使用 15 个显式圆弧关键帧验证该问题，待矩阵完成后再决定是否将其下沉为核心编译能力。

## 测试证据

第一次全量测试误用了旧 Anaconda Python，因缺少 `imageio_ffmpeg` 且不支持当前 Python API，得到 73 个环境性错误；该次运行无效，未作为验收依据。

有效验收使用固定 Python 3.12.13：

```powershell
& 'C:\Users\sy\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' `
  -m unittest discover -s tests -v
```

结果：`Ran 309 tests in 168.767s`，`OK (skipped=5)`。5 项跳过均为当前 Windows 权限下不可创建 symlink 的既有平台跳过。聚焦 parser/trajectory/module-I/O 回归另有 52 项通过。

## 下一阶段闸门

下一阶段应为三个新场景分别增加真实 trajectory JSON、轨迹 Blender proxy 和三种条件 bundle，并一次只完成一个场景后展示结果。该工作不自动提交 Seedance/Kling；任何生成 API 矩阵仍需单独报告调用次数、预计费用并获得用户确认。
