# 固定人物资产目录

`catalog.json` 是 Proxy 阶段唯一的人物资产入口。每个角色必须有网格、profile、统一骨骼映射和许可证记录，并绑定模型 SHA-256。

当前 `human_male_v1` 仅用于验证固定资产接口，来源是 `assets/canonical_humanoid/CesiumMan.glb`；它是低模 Proxy，不是最终真人资产。`human_female_v1` 的真实 GLB 尚未放入仓库，运行时会明确失败为 `asset_missing`，不会把男性模型冒充女性模型。

添加真实资产时，请将文件放入对应目录，更新 `catalog.json` 的 SHA-256，并补充来源、许可证、单位、朝向和骨骼映射。资产实例化不会改变人物、物体或摄像机轨迹协议。
