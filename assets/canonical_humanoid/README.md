# Canonical humanoid proxy asset

This directory contains `CesiumMan.glb`, used only as a local rigged-human
Proxy asset. It is not a final photoreal asset and it is not submitted to
Seedance/Kling by itself.

- Source: <https://github.com/KhronosGroup/glTF-Sample-Assets/tree/main/Models/CesiumMan>
- Direct file: <https://raw.githubusercontent.com/KhronosGroup/glTF-Sample-Assets/main/Models/CesiumMan/glTF-Binary/CesiumMan.glb>
- Upstream description: textured, animated, skinned glTF sample
- Upstream notice: CC-BY 4.0 with Cesium trademark limitations
- Local SHA-256: `B7001EAEEA8254BD44773BCD247E78696D94169388FBB2A1800FC69434E777D9`

The pipeline copies this source to immutable `person_a.glb`, `person_b.glb`,
and `person_c.glb` files inside each run. Blender normalizes its Y-up hierarchy
to the shared Z-up world, resets the imported default pose, and then applies
authored root, arm, forearm, and (where configured) leg keyframes.
