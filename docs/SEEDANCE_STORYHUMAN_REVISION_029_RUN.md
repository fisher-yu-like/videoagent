# Seedance E2E: storyhuman Proxy / indoor_market_exchange revision_029

日期：2026-08-10。该记录只引用真实 Blender/MCP 检查、真实 CLI 渲染、真实 Seedance 下载和真实 VLM 审查。

## 解决方案

1. 新增 `storyhuman` Proxy：使用圆润躯干/骨盆、头发、肩/肘/膝关节、手、胶囊式四肢和圆角鞋，不再使用低模 CesiumMan 或方块人体作为唯一形体。
2. 对 `backpack -> person_a`、`box_a/box_b -> handcart`、`luggage/suitcase -> traveler` 增加 Blender 根级父变换、局部轨迹和 `coupling_log.json`，防止物体只靠复制绝对坐标而脱离主体。
3. revision_028 将客户移到手柄后方、helper 移入可读动作层、手推车根坐标归零以保证轮子接地。
4. revision_029 进一步分离 helper 的后方轨迹，并把两张纸的起点、上升、飘落和 counter 侧落点明确写入轨迹。

## Proxy 结果

Proxy 目录：

`runs/results/storyhuman_market_proxy_20260810_105110/complex_scene_suite_20260810_025110/indoor_market_exchange_20260810_025110/`

四机位真实 Blender MP4、coupling log 和 render manifest 均生成；VLM 对 revision_029 返回 `approve`。

## 实际发送给 Seedance 的 prompt

文件：`appearance/prompt.txt`。正文为：

```text
Appearance-only edit instruction

Use the approved shared-world clay proxy as the sole source of blocking, timing, occlusion, subject identity, and camera movement. Preserve the complete event and all camera behavior exactly; change appearance only.

Subjects and appearance
- box_a: the same large brown cardboard box on the left side of the handcart in every view
- box_b: the same smaller brown cardboard box on the right side of the handcart in every view
- counter: believable real-world prop with coherent material, scale and surface details
- customer: the same adult man in a rust-red jacket and dark trousers in every view
- handcart: the same small silver two-wheel handcart with one fixed handle in every view
- helper: the same adult woman in a mustard work vest and dark trousers in every view
- paper_a: believable real-world prop with coherent material, scale and surface details
- paper_b: believable real-world prop with coherent material, scale and surface details
- vendor: the same middle-aged woman in a dark blue apron and cream shirt in every view

Environment
natural live-action location with physically plausible architecture and props

Lighting
continuous natural cinematic lighting with stable exposure and realistic contact shadows

Photoreal quality
photorealistic live-action image quality, natural skin and fabric detail, stable identity, no low-poly geometry

Must preserve and must avoid
Must preserve every entity, its identity, its timing, its blocking, and the shared-world composition. Must avoid:
- extra people or duplicated props
- clay, white cylinders, primitive limbs, labels, guide lines or storyboard overlays
- identity drift, face drift, clothing drift or texture flicker
- unrealistic plastic skin, broken anatomy, floating props or inconsistent materials
- cross-view identity or prop drift between the independently rendered reference-video tasks
```

## API 与真实结果

- 模型：`Doubao-Seedance-2.0`
- 每个 camera_id 一个独立 reference-video task
- 上传：Uguu
- task：master `task-9kudzhdl0bikle3`、lateral `task-z4yle9oys44noa6`、reverse `task-jlfqwxmf6x7iqh6`、elevated `task-to8774iodhdbrgp`
- API：`submit=4, query=118, download=4`；后续只查询原 task，没有重新 submit
- 四条 MP4 均真实下载，1280×720、24fps、121 frames、约 5 秒、黑帧 0

结果目录：

`runs/results/e2e_seedance_storyhuman_market_revision_029_20260810/`

## 最终 VLM

最终 VLM 调用 1 次，结论为 `revision_requested`。它指出 elevated、lateral、master、reverse 仍生成了不同人物、推车、箱子和市场布局。反馈类别为 scene/character/object/camera/physical，因此该问题不能再通过 appearance prompt 修补，也不能把“下载成功”当作多视角一致。

结论：`storyhuman` 和根级物体耦合解决了 Proxy 侧的粗糙人体、悬空轮子和脱离轨迹；剩余阻塞是 Seedance 独立 task 的跨视角一致性限制。下一阶段应使用真正的共享外观参考/原生多视角后端，而不是继续重复同一类独立提交。
