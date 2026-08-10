# Seedance E2E: indoor_market_exchange / revision_027

Date: 2026-08-10. This record references only real Blender renders, real uploads, real Seedance MP4 downloads, and one final VLM review. No media, metric, or review result was fabricated.

## Proxy fixes

`revision_025` repaired reverse-camera occlusion and the floating handcart. `revision_026` changed the counter to a table-like proxy and pulled back the elevated camera. `revision_027` added explicit vendor/helper depth layers, a customer K1-K2 pause and raised-hand interval, synchronized cart/box pause, and a visible paper-flight event. Its four Proxy videos passed deterministic checks and received VLM `approve`.

Approved Proxy:

`runs/results/complex_scene_suite_20260810_014636/indoor_market_exchange_20260810_014636/`

## Seedance requests

- Model: `Doubao-Seedance-2.0`
- Input: four approved Proxy camera videos, one independent reference-video task per `camera_id`
- Upload provider: Uguu (the verified research fallback used when no TOS credentials are configured)
- No resubmission: 4 initial submits; after the polling window, only the original tasks were queried once more; the delayed elevated task was then downloaded
- API calls: `submit=4, query=125, download=4`
- Task IDs: `task-2udime5z3vph2rf` (master), `task-k2515hehjzxd02g` (lateral), `task-uw9643qu4560qhx` (reverse), `task-7ubr0fa3odks75a` (elevated)

Endpoint:

`runs/results/e2e_seedance_indoor_market_revision_027_20260810/`

## Real media checks

All four results were downloaded. Each is 1280x720 at 24 fps with 121 frames and approximately five seconds (reverse is 5.086 seconds); black-frame detection is zero. The verifier previously reported a false FPS failure because the caller supplied `24.0` while ffprobe returned `24/1`; it now compares FPS numerically and has a regression test. SHA-256, ffprobe, and source hashes are in `final_video_verifier_report.json` and `seedance/media_inventory.json`.

## Final VLM result

The final VLM was called exactly once and returned `revision_requested`. This is not an API/download failure. It found that the four independent Seedance tasks did not preserve shared identity and environment: the master is a checkout scene, elevated is a different supermarket layout, and lateral/reverse contain different people, carts, and props. Feedback categories were `scene_structure`, `character_trajectory`, `object_trajectory`, `camera_trajectory`, and `physical_event`; according to the pipeline these must route back to Director/Blender/multi-view backend, not be hidden by an appearance-only prompt.

Viewable files:

- Proxy: `sandbox/master.mp4`, `sandbox/lateral.mp4`, `sandbox/reverse.mp4`, `sandbox/elevated.mp4`
- Seedance: `seedance/camera_tasks/indoor_market_exchange_master/result.mp4`, `..._lateral/result.mp4`, `..._reverse/result.mp4`, `..._elevated/result.mp4`
- Review: `final_review.json` and `final_vlm_review/`

This run proves the approved shared-world Proxy -> four real Seedance tasks -> download -> media verification -> final VLM path end to end. It also exposes the actual limitation: independent reference-video tasks do not automatically guarantee cross-camera identity/environment consistency. The next fix should use a shared appearance anchor or a backend with native multi-view consistency, rather than resubmitting the same Seedance task.
