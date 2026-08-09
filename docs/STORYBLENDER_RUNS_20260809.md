# StoryBlender-style local Proxy runs — 2026-08-09

This record contains only real local Blender renders. No Seedance or Kling request was made in these runs.

## Runs

| Run | Change | Real result | VLM result |
|---|---|---|---|
| `runs/results/complex_scene_suite_20260809_130253` | canonical procedural humanoid v3, leg tracks, foot-contact sidecar | 4 MP4s and shared `.blend`; no black-frame failure | 1 call, `revision_requested`: actors/props clustered and gestures not readable |
| `runs/results/complex_scene_suite_20260809_130524` | `revision_006` camera/layout reflection | 4 MP4s; no black-frame failure | 1 call, `revision_requested`: elevated view too top-down and dance phases weak |
| `runs/results/complex_scene_suite_20260809_130818` | amplified authored gesture phases and corrected elevated blocking | 4 MP4s; no black-frame failure; motion materialization recheck passed | 1 call returned invalid textual `evidence_frames`; strict corrected review (1 call) returned `revision_requested` |
| `runs/results/complex_scene_suite_20260809_132708` | `revision_007` arm/head clearance guard; no upper-arm center lift | 4 MP4s, 640×360, 24 FPS, 120 frames, 5 seconds; no black-frame failure; arm clearance passed | 1 call, `revision_requested`: no arm/head defect reported, but staging/gesture/prop/camera readability still weak |

## Evidence

- [last render manifest](../runs/results/complex_scene_suite_20260809_130818/plaza_dance_circle_20260809_130818/sandbox/render_manifest.json)
- [last master Proxy](../runs/results/complex_scene_suite_20260809_130818/plaza_dance_circle_20260809_130818/sandbox/master.mp4)
- [last elevated Proxy](../runs/results/complex_scene_suite_20260809_130818/plaza_dance_circle_20260809_130818/sandbox/elevated.mp4)
- [motion sidecar](../runs/results/complex_scene_suite_20260809_130818/plaza_dance_circle_20260809_130818/motion_tracks.json)
- [motion verifier recheck](../runs/results/complex_scene_suite_20260809_130818/plaza_dance_circle_20260809_130818/proxy_verifier_motion_recheck.json)
- [strict VLM feedback](../runs/results/complex_scene_suite_20260809_130818/plaza_dance_circle_20260809_130818/proxy_review_strict.json)
- [revision_007 master Proxy](../runs/results/complex_scene_suite_20260809_132708/plaza_dance_circle_20260809_132708/sandbox/master.mp4)
- [revision_007 arm pose log](../runs/results/complex_scene_suite_20260809_132708/plaza_dance_circle_20260809_132708/sandbox/arm_pose_log.json)
- [revision_007 verifier](../runs/results/complex_scene_suite_20260809_132708/plaza_dance_circle_20260809_132708/proxy_verifier_report.json)

## Evidence-backed conclusion

The procedural asset is clearer than the old single low-poly primitive, but it is still a storyboard proxy. The VLM feedback identifies weakly readable dance/wave phases, speaker overlap, passerby crossing ambiguity, and insufficiently distinct camera coverage. The proxy is therefore not approved and must not be sent to Seedance yet.

The arm/head structural defect is fixed and guarded by a real verifier. The next StoryBlender-inspired step is still a real rigged asset/retargeted animation adapter (or a deliberately simpler action plan), not another appearance-only prompt edit.
