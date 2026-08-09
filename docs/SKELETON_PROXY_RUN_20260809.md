# Procedural Skeleton Proxy Run — 2026-08-09

## Configuration

- Revision: `revision_008`, parent `revision_007`
- Scene: `plaza_dance_circle`
- Proxy style: `skeleton` / `procedural_skeleton_v1`
- External video API calls: `submit=0`, `query=0`, `download=0`
- VLM calls: 1 Proxy review
- Cameras: `master`, `lateral`, `reverse`, `elevated`

## Deterministic result

- Four real MP4 files rendered from one shared Blender world;
- 640×360, 24 FPS, 120 frames, 5 seconds per camera;
- media, trajectory, camera, asset and black-frame checks passed;
- `skeleton_pose_log.json` contains 120 frame rows and all configured bones for all three characters;
- all hand/foot IK targets were reachable in the recorded frames;
- skeleton materialization verifier passed;
- old `canonical` and all previous revision directories were preserved.

Artifacts:

- [master.mp4](../runs/results/complex_scene_suite_20260809_135946/plaza_dance_circle_20260809_135946/sandbox/master.mp4)
- [lateral.mp4](../runs/results/complex_scene_suite_20260809_135946/plaza_dance_circle_20260809_135946/sandbox/lateral.mp4)
- [reverse.mp4](../runs/results/complex_scene_suite_20260809_135946/plaza_dance_circle_20260809_135946/sandbox/reverse.mp4)
- [elevated.mp4](../runs/results/complex_scene_suite_20260809_135946/plaza_dance_circle_20260809_135946/sandbox/elevated.mp4)
- [skeleton pose log](../runs/results/complex_scene_suite_20260809_135946/plaza_dance_circle_20260809_135946/sandbox/skeleton_pose_log.json)
- [ProxyVerifier report](../runs/results/complex_scene_suite_20260809_135946/plaza_dance_circle_20260809_135946/proxy_verifier_report.json)
- [ProxyVerifier report bound to VLM decision](../runs/results/complex_scene_suite_20260809_135946/plaza_dance_circle_20260809_135946/proxy_verifier_report_after_vlm.json)

## VLM result

The single VLM review returned `revision_requested` with structural categories:

- camera coverage hides the onset wave, two dance phases and final turn;
- performers repeatedly compress into a cluster;
- musician/passerby gesture beats are not visually distinct.

It did not report arm/head penetration. This means the skeleton/IK layer solved the targeted limb-geometry defect, but the complete Proxy is not approved for Appearance-only compilation or Seedance submission.

## Decision

Keep the skeleton branch as the new motion-control base. The next revision should modify Director staging and camera responsibilities while preserving the bone contract; do not change the final-video prompt to hide these structural failures.
