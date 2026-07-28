# Stage 1 ShotScript and Blender Proxy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Compile one concrete three-shot, two-actor story into a validated ShotScript and a real Blender 5.1 scene, `.blend` file, rendered MP4, keyframes, and measured trajectory report.

**Architecture:** A standard-library parser validates a canonical ShotScript JSON. A Blender Python entry point consumes exactly that validated structure, creates simple but identifiable actors, animates actors and camera, renders a three-shot MP4 with Blender's bundled FFmpeg, saves the scene, and exports measurements. No diffusion model or external API is involved.

**Tech Stack:** Bundled Python 3.12.13 for validation; Blender 5.1.2 at `D:\blender\blender.exe`; Blender Python 3.13; Eevee; Blender-bundled FFmpeg; unittest.

---

## Evidence boundary

- JSON parsing, source checks, and unit tests are local code evidence only.
- `.blend`, PNG frames, and MP4 must be produced by the actual installed Blender executable in background mode.
- The report must include Blender version, exit code, file sizes, hashes, video frame metadata available from Blender, and visual inspection of rendered PNG frames.
- These outputs prove the control plan is executable in Blender; they do not prove Seedance/Kling/VACE follows it.
- No fake API response, mock render success, invented task ID, server, GPU, or package installation is permitted.

## Files

- Create: `examples/station_story.md`
- Create: `examples/station_shotscript.json`
- Create: `videoactagent/shotscript.py`
- Create: `tests/test_station_shotscript.py`
- Create: `videoactagent/blender_proxy.py`
- Create at runtime: `runs/stage1_blender/station_proxy.blend`
- Create at runtime: `runs/stage1_blender/station_proxy.mp4`
- Create at runtime: `runs/stage1_blender/keyframes/shot_01.png`, `shot_02.png`, `shot_03.png`
- Create at runtime: `runs/stage1_blender/trajectory_report.json`

## Task 1: Add the controlled story and canonical ShotScript

Create a single-platform scene with recurring `actor_a` (orange) and `actor_b` (blue):

1. `s01`, 5 s wide shot: A walks from `[-3,0,0]` to `[-1,0,0]`; B stays `[2,0,0]`; camera makes a small truck-right move.
2. `s02`, 5 s medium shot: A walks from `[-1,0,0]` to `[0.5,0,0]`; B stays `[1.5,0,0]`; camera dollies in.
3. `s03`, 5 s over-shoulder shot: A stays `[0.5,0,0]`; B stays `[1.5,0,0]`; camera makes a small clockwise arc.

The top-level JSON contains `scene_id`, `fps=3`, `world_bounds`, and three shots. Every shot contains `shot_id`, `duration`, camera start/end/focal length/motion/look-at, both actor plans, and continuity (`previous_shot`, `screen_direction`, `axis_side`).

Validate the actual file with `python -m json.tool`. Commit the data separately.

## Task 2: Parse and validate the actual ShotScript using TDD

Write `tests/test_station_shotscript.py` before production code. It loads the checked-in example and asserts actual values:

- shot IDs are `s01`, `s02`, `s03`;
- all durations are 5 seconds and preview fps is 3;
- both actors recur in every shot;
- previous-shot chain is correct;
- A's end position in each shot equals its next start position;
- all shots remain `left_to_right` and `north` of the axis.

The initial test must fail because `videoactagent.shotscript` is absent. Implement immutable dataclasses `Vec3`, `CameraPlan`, `ActorPlan`, `ContinuityPlan`, `Shot`, and `ShotScript`, plus `ShotScript.from_path()` and `validate()`.

Reject duplicate IDs, malformed vectors, non-positive timing, missing recurring actors, broken shot references, actor discontinuity, unsupported screen direction, and unsupported axis side. Run focused and full suites with bundled Python 3.12, then commit.

## Task 3: Build the real Blender compiler

Create `videoactagent/blender_proxy.py`. It runs only inside Blender and:

1. parses arguments after `--`;
2. imports the same `ShotScript` parser from the repository;
3. clears the factory scene;
4. creates a platform, tracks, action-axis line, sunlight, area light, world background, and labeled actor materials;
5. creates each actor from a cylinder body and sphere head parented to an empty root;
6. creates one camera and animates location, focal length, and look-at rotation for every frame;
7. keyframes actor root locations with linear interpolation inside shots and one-frame cuts between shots;
8. renders at 960x540, 3 fps, Eevee, MPEG-4/H.264;
9. saves the `.blend` before rendering;
10. renders the middle frame of each shot as PNG;
11. exports `trajectory_report.json` from actual Blender object keyframes and scene frame ranges.

The script must print `BLENDER_PROXY_OK` only after `.blend`, MP4, three PNGs, and report exist and have non-zero sizes.

## Task 4: Run a real headless Blender experiment

Execute:

```powershell
& 'D:\blender\blender.exe' --background --factory-startup --python videoactagent/blender_proxy.py -- --shotscript examples/station_shotscript.json --output-dir runs/stage1_blender
```

Expected external evidence:

- Blender exits 0 and prints its real version plus `BLENDER_PROXY_OK`;
- `.blend` exists and can be reopened headlessly;
- MP4 exists, is non-empty, and Blender reports 45 rendered frames;
- three actual PNG frames exist and are visually distinct;
- trajectory report contains the real frame ranges and Blender-evaluated positions.

If Blender fails, use systematic debugging. Do not replace it with a fake renderer or mark success from file-name existence alone.

## Task 5: Inspect and report

Use the local image viewer to inspect all three PNGs. Reopen the `.blend` with:

```powershell
& 'D:\blender\blender.exe' --background runs/stage1_blender/station_proxy.blend --python-expr "import bpy; print('REOPEN_OK', len(bpy.context.scene.objects), bpy.context.scene.frame_start, bpy.context.scene.frame_end)"
```

Record SHA-256, size, dimensions, frame ranges, Blender object count, actual actor endpoints, and visible issues. Show clickable PNGs and MP4/GIF if the application supports it. Clearly mark Seedance/Kling/VACE as unverified.

