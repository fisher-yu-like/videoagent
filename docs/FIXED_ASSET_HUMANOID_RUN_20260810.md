# Fixed Character Asset / Full-Chain Evidence — 2026-08-10

## Scope

This report records the fixed-character asset implementation and the real end-to-end regression requested after it. The existing pipeline is preserved: scene plans and trajectories are compiled into a shared Blender world, ProxyVerifier/VLM gates the backend, each camera is submitted independently to the reference-video adapter, and final media is reviewed separately.

## Fixed-asset preflight

Command branch: `--proxy-style asset_humanoid`, scene `indoor_market_exchange`.

The user supplied `Universal Base Characters[Standard].zip`. Its bundled license is CC0 1.0. The `Superhero_Female_FullBody.gltf` source was converted with Blender 5.1.2 to `assets/characters/human_female_v1/model.glb`; the catalog SHA-256 and source archive SHA-256 are stored in the profile and license sidecar. The source contains a humanoid armature and all 16 unified bones map successfully. The model is a stylized Proxy asset, not a photoreal final character.

The matching `Superhero_Male_FullBody.gltf` from the same CC0 package was also converted and registered as `human_male_quaternius_v1`. The older CesiumMan `human_male_v1` entry remains immutable for historical probes only.

## Male-only interface probe

Evidence: `runs/results/asset_humanoid_male_probe_20260810/indoor_market_asset_humanoid_male_probe_20260810_064817/`.

All three characters were explicitly assigned `human_male_v1` only to test the isolated Blender branch. Four real Blender MP4s were rendered. Catalog materialization, rig-map hash, shared-world asset identity, state/camera/motion logs, media metadata and blackdetect checks passed. The probe remains pending visual approval and is not a male/female or realism result.

## Asset-humanoid Proxy result

Evidence: `runs/results/asset_humanoid_market_20260810_retry2/complex_scene_suite_20260810_142818/indoor_market_exchange_20260810_142818/`.

The four real Blender camera videos, catalog materialization, rig hashes, shared-world identity, trajectory/camera logs, ffprobe and blackdetect all passed. Proxy VLM was called once and returned `revision_requested`: the vendor was occluded by the counter, the customer/helper/cart relation was not readable, paper lift/flutter/settle was ambiguous, and reverse/elevated coverage was insufficient. Seedance/Kling were not called because the Proxy gate correctly rejected the revision.

## Existing-branch full-chain regression

Evidence: `runs/results/fixed_asset_fallback_fullchain_20260810/complex_scene_suite_20260810_065421/indoor_market_exchange_20260810_065421/`.

For comparison, the old `storyhuman` branch was used without changing its source or overwriting previous revisions. The Proxy was reviewed by VLM and approved once. Four independent Seedance reference-video tasks were submitted:

| API operation | Count |
|---|---:|
| submit | 4 |
| query | 126 |
| download | 4 |

Each downloaded MP4 is real and non-empty: 121 frames, approximately 5.086 seconds, 24 fps, and zero black-frame events. The per-camera SHA-256 values are stored in `scene_summary.json`, `final_video_verifier_report.json`, and the per-camera verifier files.

The final VLM was called once. It returned `revision_requested` because the independent backend tasks changed people, cart/boxes, papers, and market layout between cameras. This is a genuine cross-camera consistency failure, not a media-download failure. The pipeline therefore correctly stops before claiming final approval or launching an Appearance-only revision.

## Required next input

The fixed-asset backend full chain remains blocked until a new Proxy revision passes the VLM gate. No male model was used as a female substitute.

## Same-package male/female revision

Evidence: `runs/results/asset_humanoid_market_20260810_quaternius029/complex_scene_suite_20260810_144813/indoor_market_exchange_20260810_144813/`.

The market customer now uses `human_male_quaternius_v1`, while vendor/helper use `human_female_v1`; both are derived from the same CC0 Standard package. This removes the old CesiumMan circular head and keeps the two roles on one asset family. Four-camera deterministic checks passed and Proxy VLM was called once. VLM still requested a structural revision for customer-to-handle contact and identity-specific pause/hand-raise readability. Seedance/Kling remained at zero because the Proxy gate was not approved.

## Approved revision_040 and real Seedance endpoint

Evidence: `runs/results/asset_humanoid_market_20260810_e2e_approved_revision040/indoor_market_exchange_approved_revision040/`.

`revision_040` passed the real Proxy VLM gate. Its copied immutable bundle was submitted as four independent Seedance reference-video tasks (one URL per `camera_id`): `submit=4`, initial `query=120`, continuation `query=4`, and `download=4`. All four downloaded MP4s passed real ffprobe and blackdetect checks; per-camera hashes and task IDs are recorded in the endpoint `seedance/camera_tasks/` and `final_video_verifier/` files.

The final VLM was called once and returned `revision_requested`: the backend did not preserve the shared counter, people, box arrangement, or market layout across independent camera tasks. This is the first complete real Prompt -> shared-world Proxy -> Seedance -> downloaded MP4 endpoint run for the fixed asset branch, but it is not a multi-view consistency pass.
