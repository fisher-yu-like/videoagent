# 固定人物资产库与轨迹驱动 Proxy 设计

## 1. 目标与边界

目标是在不改变现有 VideoActAgent pipeline_v2 合同的前提下，将人物几何、骨骼和基础材质从每次生成的 Blender 几何代码中抽离为可复用、可哈希绑定的固定资产。Director 仍负责生成 ScenePlan、PhysicalStatePlan、Character/ObjectTrajectoryPlan 和 CameraTrajectoryPlan；Blender CodeAgent 只负责选择资产、实例化场景、应用轨迹和渲染共享世界。

本设计只解决 Proxy 侧的人形、骨骼和跨机位一致性，不把 Seedance 的真人身份一致性误认为已经解决。Seedance 仍需要独立的 appearance/identity anchor 或原生多视角控制后端。

## 2. 方案选择

### 方案 A：固定 rigged GLB/Blend 资产（采用）

每个角色引用一个版本化资产 ID，例如 `human_male_v1`、`human_female_v1`。资产包含网格、骨骼、蒙皮、材质、骨骼映射和许可信息。优点是 Proxy 人形稳定、可复用、便于动作重定向和跨机位共享；缺点是需要准备兼容许可的资产。

### 方案 B：固定 procedural prefab（保留为 fallback）

继续使用当前 storyhuman/canonical 代码生成圆润低模人物。优点是无需外部资产；缺点是外观质量和骨骼表现有限，不能作为高质量真实人物基础。

### 方案 C：每个场景重新生成完整人物代码（不再作为默认）

灵活但最容易出现比例、骨骼和身份漂移，与当前实验暴露的问题一致。

最终采用 A+B：固定 rigged 资产为默认，现有 procedural 分支保持可启动和可回退。

## 3. 资产目录与契约

```text
assets/characters/
  human_male_v1/
    model.glb
    rig_map.json
    profile.json
    license.txt
  human_female_v1/
    model.glb
    rig_map.json
    profile.json
    license.txt
```

`profile.json` 保存身高、朝向、单位、默认姿态、材质标签和版本；`rig_map.json` 将统一骨骼名映射到资产骨骼名，至少覆盖 root、pelvis、spine、head、upper_arm、forearm、hand、upper_leg、lower_leg、foot；`license.txt` 保存来源和许可证据。每次运行将资产复制到 immutable run bundle 并计算 SHA-256。

ScenePlan 的角色实体只保存 `asset_id`、`trajectory_id` 和语义角色，不直接嵌入网格代码：

```json
{
  "entity_id": "customer",
  "kind": "character",
  "asset_id": "human_male_v1",
  "trajectory_id": "customer_cart_push"
}
```

## 4. Blender 阶段

Blender CodeAgent 生成的代码调用固定适配器：

1. 根据 `asset_id` 从 asset catalog 加载或链接人物 prefab；
2. 为每个实体建立稳定根节点和 entity ID；
3. 应用 CharacterTrajectoryPlan 的 root/bone/IK 轨迹；
4. 应用 ObjectTrajectoryPlan 的父子耦合和接触约束；
5. 在同一个 Blender shared world 中创建四个或更多摄像机；
6. 输出 Proxy MP4、`asset_log.json`、`motion_log.json`、`camera_log.json`、`state_log.json` 和 `render_manifest.json`。

任何摄像机都不能重新实例化人物资产。所有 camera log 必须引用同一组 entity ID 和同一 asset hash。

## 5. 轨迹接口

人物轨迹分为三层：

- root trajectory：人物整体位置和朝向；
- bone trajectory：头、手、脚等关键骨骼的关键帧；
- constraints：脚接触、手柄抓取、头部注视和关节限制。

摄像机轨迹继续使用现有 CameraTrajectoryPlan，包含每个关键帧的位置、目标点、roll、lens 和机位职责。固定人物资产不会改变机位协议。

## 6. 质量门禁

新增确定性检查：

- `asset.catalog_materialization`：asset ID、文件哈希、许可记录和骨骼映射完整；
- `asset.shared_world_identity`：所有 camera render 使用同一资产哈希；
- `motion.rig_mapping`：每个角色的统一骨骼均可映射；
- `motion.foot_contact`：脚接触和 IK 可达；
- `camera.entity_coverage`：每个 authored camera 的目标实体和职责在日志中存在；
- `media.black_frames` 和 ffprobe：继续使用现有真实媒体检查。

ProxyVerifier 或 VLM 仍负责遮挡、角色语义、动作顺序和可读性。未通过结构门禁时不得进入 Appearance-only Prompt 或 Seedance。

## 7. 兼容性与回滚

现有 `clay`、`canonical`、`storyhuman`、`skeleton` 分支保持启动方式和输出合同不变。新增分支建议命名为 `asset_humanoid`；资产缺失时明确失败并记录 `asset_missing`，不静默切换到伪造模型。只有显式配置 fallback 时才回退到 storyhuman。

## 8. 实施顺序

1. 建立 asset catalog schema 和两个角色 profile；先用现有 CesiumMan 作为接口探针，标注其低模局限；
2. 接入可许可的男性/女性 rigged GLB，并完成统一骨骼映射；
3. 将 Blender CodeAgent 的人物创建改为 prefab instantiation；
4. 接入现有 BVH/IK/root 轨迹并写 asset/motion verifier；
5. 对 indoor_market_exchange 运行一次共享世界四机位 Proxy，人工/VLM 验收；
6. Proxy 通过后再测试 Seedance，不把后端跨机位一致性与 Proxy 资产一致性混为一个指标。

## 9. 非目标

本阶段不训练人物生成模型，不修改 Director 的上层协议，不删除旧分支，不保证 Seedance 独立任务自动共享真人身份，也不通过更换 seed 搜索好结果。
