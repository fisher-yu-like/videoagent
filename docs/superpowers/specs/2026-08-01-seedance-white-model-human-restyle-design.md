# Seedance 白模直输真人化设计

## 目标

把人工批准的人物与摄像机轨迹编译成一段中性低模人形 Blender Proxy，并将完整 Proxy 直接作为最新 Seedance 的白模/参考视频输入，生成两名写实真人。第一轮不预先生成人物图片，不训练基础视频模型，不拆成多个镜头，也不自动重试。

本设计解决当前真实实验暴露的两个根因：圆柱 Proxy 缺少人体形状先验，以及自动 Prompt 与实际 K0–K4 轨迹冲突。

## 外部能力依据

- ByteDance 官方 Seedance 2.5 页面明确提供 reference-video 理解、white-model control、green-screen editing、camera movement 和 performance blocking：<https://seed.bytedance.com/en/seedance2_5>。
- Seedance 2.0 官方说明支持文本、图片、音频和视频混合输入，可参考构图、动作和运镜：<https://seed.bytedance.com/en/blog/seedance-2-0-official-launch>。
- Kling VIDEO 3.0 支持图片/视频 Elements；Motion Control 支持“角色图片 + 动作视频”，但当前主要控制一个主体，且推荐动作参考避免切镜和运镜：<https://kling.ai/quickstart/klingai-video-3-model-user-guide>、<https://kling.ai/quickstart/motion-control-user-guide>。

因此，本场景优先 Seedance；Kling 3.0 只作为后续单人物或 Elements 对照，不进入第一轮实现。

## VideoCoCo 审计与借鉴边界

审计来源：`micky-li-hd/VideoCoCo`，commit `7c5eaad084b1413b15ced75f48833e1dd48b0b3c`。

VideoCoCo 的实际流程是：

```text
Prompt
  → physical-state plan
  → Blender neutral clay draft
  → causal/preview verification
  → case-specific edit instruction
  → OmniWeaving editing model
  → photoreal target
```

它不是在公开推理脚本里直接调用 Seedance。仓库中的 `seedance.mp4` 是 photoreal target；公开脚本以 clay proxy、edit prompt 和这些目标形成的调优权重驱动 OmniWeaving editing。8 个 toy cases 都是单一物理对象，不包含双人调度或移动摄像机。

### 复用

1. **中性 Proxy 原则**：conditioning 视频只用灰白材质；语义通过形状、位置、遮挡、形变和运动表达，不能依赖诊断颜色。
2. **形状接近原则**：Proxy 不需要真实材质，但几何类别必须接近目标。人物应有头、躯干、骨盆、手臂和腿，不能继续用圆柱代表完整人体。
3. **K0–K4 状态与连续变化**：每段运动分别描述，不再仅比较首尾状态。
4. **专用 edit instruction**：按主体外观、driving motion、环境、灯光、摄像机、色调/质量和禁止项组织。
5. **三元组数据协议**：每次真实实验保存 `source proxy + instruction + generated target`，再加当前项目已有的审批、请求、响应、日志和 SHA-256。

### 不复用

1. 不接入 OmniWeaving/HunyuanVideo 训练和 8-GPU 推理栈；这会显著增加依赖、显存和复现难度。
2. 不下载或训练 VideoCoCo 权重作为第一轮路线；其权重和推理代码受 Tencent HY Community License 约束。
3. 不复制其物理物体 Prompt 作为人物 Prompt；只复用字段结构。
4. 不把 8 个 toy cases 当作双人轨迹控制证据。

## 新链路

```text
Story Prompt
  → ShotScript + K0–K4 actor/camera trajectory
  → neutral humanoid Blender Proxy
  → human approval
  → trajectory-derived restyle instruction
  → Seedance capability gate
  → one direct reference-video generation
  → real media validation + human trajectory annotation
```

### 1. 低模人形 Proxy

每个 actor 使用简洁关节人形：头、胸腔、骨盆、上/下臂、上/下腿。人物的屏幕轨迹仍由现有人工标注驱动；身体朝向根据相邻轨迹切线计算。第一版只实现自然站立和步行摆臂，不做复杂手指、面部或衣料模拟。

conditioning profile 仅使用灰白材质和稳定照明。diagnostic profile 可以保留 actor ID 颜色和轨迹线，但绝不能送入生成模型。

### 2. 逐段轨迹 Prompt

Prompt 编译器不得复制与人工轨迹冲突的旧动作或摄像机句子。它必须从实际 K0–K4 计算：

- 每个 actor 在 K0→K1、K1→K2、K2→K3、K3→K4 的方向、位移和停留；
- 两人距离在每个 K 点的接近、会合、分离变化；
- 摄像机每段的平移、环绕、推进/拉远和景别变化；
- 不得仅用首尾距离概括非单调运动。

restyle instruction 固定为以下顺序：

```text
Subjects and wardrobe
Driving motion from the approved proxy
Environment
Lighting
Camera motion and framing
Photoreal quality
Must preserve / must replace / must avoid
```

其中必须明确：完整 Proxy 仅用于人物调度、动作节奏、遮挡、构图和运镜；所有灰白低模人体必须替换为完整写实真人，不保留圆柱、木偶、塑料或 CG 形态。

### 3. Seedance 适配器与能力门禁

当前 JD 主链只验证过 `Doubao-Seedance-2.0` 的文本、图片和首尾帧结构，`reference_video` 仍为 `gateway_unverified`；不能仅改模型字符串后假设 2.5 可用。

适配器分两层：

1. 本地生成零网络的候选请求和证据包，包含 Proxy URL/上传句柄角色、完整 restyle instruction、5 秒、16:9、720p，以及来源哈希。
2. 优先使用网关的模型列表、schema 或非生成校验接口完成 capability probe，确认 JD 网关真实接受的模型名、视频字段和 role。若网关没有非计费校验接口，不提交一条“试错任务”；此时把唯一一次真实生成同时作为能力验证，并在请求被拒绝时停止。

优先顺序：

1. `Seedance 2.5 white-model/reference-video`；
2. 网关没有 2.5 时，`Doubao-Seedance-2.0 reference-video/R2V`；
3. 两者都不开放则停止，不退回 prompt-only，不伪称使用了 Proxy。

### 4. 第一轮不先生图

第一轮目标是“两个虚构但写实的人”，人物具体面孔由模型生成。只有在真人形态已成功、但身份或服装跨帧漂移时，第二轮设计才增加人物参考图。这样可以先验证 white-model control 本身，避免把生图质量与动作控制混成一个变量。

## 真实实验与门禁

第一轮最多产生一个付费视频：

1. 本地生成一条新 humanoid Proxy；使用真实 Blender，保存 MP4、媒体元数据和 SHA-256。
2. 人工观看完整视频并批准；未批准不调用 API。
3. capability probe 不产生测试视频；如果网关只能通过生成端点验证，则不单独 probe，唯一一次真实提交同时承担能力验证。
4. 对批准的 Proxy 运行一次 Seedance reference-video，不自动重试，不换 seed 搜索结果。
5. 下载原始结果，验证时长、帧数、分辨率、可解码性和 SHA-256。
6. 人工标注相同 K 点的人物位置与摄像机背景运动。

## 验收条件

链路通过必须同时满足：

- 结果覆盖完整约 5 秒，不是短片段或截断输出；
- K0–K4 采样帧均可解码；
- 两个主体均具有完整可辨识的人体结构，不残留圆柱/木偶主体；
- 至少一名人物的衣着符合指定外观，且两人可区分；
- 人物在 K2 的会合和之后的分离能够由真实人工标注确认；
- 摄像机背景运动方向与批准 Proxy 一致；
- 所有请求、响应、任务 ID、日志、媒体和来源文件均有 SHA-256；
- 不把单元测试、fixture 或自动猜测当作真人效果验收。

若媒体技术验证通过但人物仍是低模形态，记录为“API 链路成功、真人化失败”，停止本轮，不自动重试。

## 实施边界

预计代码边界：

- 修改 `videoactagent/blender_proxy.py`：低模人形和行走朝向；
- 修改 `videoactagent/trajectory_prompt.py`：逐段人物/距离/摄像机描述；
- 新增 `videoactagent/restyle_prompt.py`：VideoCoCo 风格但人物专用的 edit instruction；
- 扩展 `videoactagent/backends/capabilities.py` 和 Seedance 请求构造：真实 reference-video capability；
- 扩展 director 页面：并排显示 diagnostic 与 neutral humanoid Proxy，并显示只读 restyle instruction；
- 增加单元测试和一次真实 Blender 集成测试；测试数据不作为实验结果。

第一轮不增加人物生图模块、不训练模型、不接入 OmniWeaving、不实现 Kling 多人合成，也不开放批量矩阵。
