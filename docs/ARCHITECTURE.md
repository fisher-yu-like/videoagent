# VideoActAgent：Whole-Story 架构

## 目标

系统以一个连续故事为一次执行单位：一个人工配对的 story prompt/ShotScript、一个完整 Blender proxy、一组 story-level 离线输入清单和完整性结果。当前没有自然语言到 ShotScript 的自动编译，也不再把三镜头 proxy 与单镜头 API 请求混在一起，或把未发送给后端的 proxy 描述成生成条件。

## 核心流程

```text
WholeStory Suite
  -> 8 个独立 story / prompt / ShotScript
  -> 8 个完整 5 秒 Blender proxy
  -> story-level bundles
       Kling / Seedance: prompt_only
       VACE: source_video
  -> duration / frame / hash 完整性门
  -> 后续经批准的真实生成与人工评估
```

每个 story 是一个 5 秒 continuous one-take。ShotScript 内部仍用一个 shot 表示连续时间段，但 orchestrator、bundle、结果和用户入口都只暴露 `story_id`，不逐镜头提交或拼接。

## 数据合同

Suite 配置固定统一 profile：

- story duration：5 秒；
- proxy：960×540、3 fps、15 帧；
- 闭源 API 目标输出：1280×720；
- 闭源 API seed：`null`，并标记 `unsupported_by_gateway`，不虚构同 seed；VACE 清单不继承该声明；
- `submit=false`、`max_api_calls=0` 为默认和当前阶段硬门。

每个 case 必须有唯一的 `story_id`、ShotScript、人工 prompt 和 proxy SHA-256。8 个 case 覆盖 station、city crosswalk、forest path 和 studio room，人物起止位置与相机路径不能全部相同。

Bundle 必须明确：

- `conditioning_mode=prompt_only`：Kling / Seedance 当前 T2V 请求只发送文本；proxy 只是离线证据；
- `conditioning_mode=source_video`：VACE 实际消费 proxy；
- `duration_seconds` 来自 story 根级 profile，不从 `shots[0]` 推导；
- 不包含 `shot_id`；
- 所有 prompt、ShotScript、proxy 和 profile 均有来源哈希。

这些 JSON 当前是离线输入合同，不是现有 `jd_smoke` 或 VACE preprocess 可直接消费的 job；下一阶段必须先写 whole-story adapter，不能退回逐镜头提交器。

## 完整性优先

真实生成下载后先计算：

```text
duration_coverage = decoded_output_duration / requested_story_duration
```

当 coverage < 0.95、分辨率不符或关键时间点不可解码时，结果标为 `incomplete`，不得进入轨迹或相机评分。相比把 5 个点击插值成 121 个点，报告必须同时保留真实人工观察帧数。

## 当前冻结项

轨迹编译、轨迹反馈修订、ATI/ReCamMaster/CamTrol 适配和 24 次正式矩阵暂时冻结。只有 8 个 whole-story proxy / bundle 完成并验收后才重新设计轨迹协议。

Blender 目前只可靠预演人物 root 位移和相机线性关键帧。`action`、`facing` 不等于骨骼动作；`arc_clockwise` 标签不等于真实圆弧。第一批 story 不以挥手、坐下、拿物体或精确圆弧作为 proxy 验收项。

## 仓库结构

```text
README.md
run.py
configs/whole_story_suite.json
stories/*.json
prompts/*.txt
videoactagent/
tests/
docs/
  ARCHITECTURE.md
  USAGE.md
  EXPERIMENTS.md
runs/
  evidence/   # 必须保留的真实、不可变证据
  work/       # 当前 whole-story 运行
  scratch/    # 可删除诊断产物
```

用户只需要一个入口：

```powershell
python run.py configs/whole_story_suite.json
```

当前入口只做离线验证、来源快照、Blender 渲染和输入清单准备，不联网。人物/相机起止位置会与 Blender report 复核；静止相机诊断还要求位置和朝向同时锁定。

## 历史评分纠正

旧 station proxy 为 15 秒三镜头；真实 Kling / Seedance 请求却只提交了 s01 的 5 秒文本，且没有发送 proxy。约 5.04 秒结果不是下载截断，而是请求本来只有 5 秒。

修订实验也没有控制 seed：Seedance 原始 / 修订返回 seed 分别为 80784 / 53450，Kling seed 未知。因此只能保留“四条真实视频和单条 s01 的描述性人物轨迹”，必须撤销“修订策略导致变差”“同 seed 公平比较”“全片控制完成”和“相机控制已由轨迹指标验证”等结论。
