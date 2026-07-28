# Stage 1 ShotScript Preview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn one concrete three-shot, two-actor story into a validated ShotScript and an actually rendered 2D animated preview with trajectory artifacts.

**Architecture:** A small standard-library validator parses the canonical ShotScript JSON and enforces cross-shot references, durations, camera vectors, actor identities, and continuity fields. A Pillow renderer consumes the same parsed objects to render top-down frames, camera frustums, actor paths, GIF output, contact sheet, and measured trajectory endpoints. This is a real local compiler experiment, not evidence for Blender or a video-generation API.

**Tech Stack:** Bundled Python 3.12.13, bundled Pillow, JSON, dataclasses, unittest.

---

## Real-evidence boundary

- Input is the checked-in station story and its checked-in ShotScript, not an invented API response.
- The renderer must create actual image files and the report must inspect actual pixels, dimensions, frame count, and measured endpoints.
- The result is labeled `2D control-plan preview`; it is not labeled Blender output, model generation, or camera-control success.
- No network, API, server, GPU, model download, or third-party package installation is permitted.

## File map

- Create: `examples/station_story.md` — controlled three-shot story and intended blocking.
- Create: `examples/station_shotscript.json` — canonical Stage 1 input.
- Create: `videoactagent/shotscript.py` — typed parser and validation errors.
- Create: `tests/test_station_shotscript.py` — reads and validates the actual example.
- Create: `videoactagent/preview2d.py` — Pillow renderer and CLI.
- Create: `tests/test_preview2d.py` — renders actual temporary GIF/PNG files and inspects them.
- Create at runtime: `runs/stage1_station/preview.gif`
- Create at runtime: `runs/stage1_station/contact_sheet.png`
- Create at runtime: `runs/stage1_station/trajectory_report.json`

## Task 1: Add the real controlled story and canonical ShotScript

Create `examples/station_story.md` with the exact controlled scenario:

```markdown
# Station meeting — controlled three-shot experiment

Two recurring actors meet on one railway platform. Actor A wears orange and enters from screen-left. Actor B wears blue and waits on screen-right. The sequence preserves the left-to-right movement and stays on the north side of the action axis.

1. `s01`, 5 s, wide establishing shot. Camera makes a small truck-right move. Actor A walks from x=-3 to x=-1; actor B remains at x=2.
2. `s02`, 5 s, medium shot. Camera dollies toward the actors. Actor A walks from x=-1 to x=0.5; actor B remains at x=1.5.
3. `s03`, 5 s, over-shoulder shot. Camera makes a small clockwise arc while both actors remain in place and face each other.
```

Create `examples/station_shotscript.json` as a top-level object containing `scene_id`, `fps`, `world_bounds`, and three `shots`. Every shot contains `shot_id`, `duration`, `camera`, `actors`, and `continuity`. Coordinates use `[x, y, z]`; camera paths use `start`, `end`, and `look_at`; actor paths use `start`, `end`, `color`, `action`, and `facing`.

Run `python -m json.tool examples/station_shotscript.json` with bundled Python. Expected: formatted JSON and exit 0.

Commit: `git commit -m "data: add controlled station ShotScript"`.

## Task 2: Parse and validate the actual ShotScript

Write `tests/test_station_shotscript.py` first. It must load `examples/station_shotscript.json` and assert:

- exactly three shots with IDs `s01`, `s02`, `s03`;
- every duration is 5 seconds;
- recurring actor set is exactly `{actor_a, actor_b}`;
- `s02.previous_shot == s01` and `s03.previous_shot == s02`;
- actor A's `s01.end` equals `s02.start`;
- all shots keep `axis_side == north` and `screen_direction == left_to_right`.

The first run must FAIL because `videoactagent.shotscript` does not exist. Convert import absence into an assertion failure, not a test-loader error.

Implement `videoactagent/shotscript.py` with:

```python
class ShotScriptError(ValueError): ...

@dataclass(frozen=True)
class Vec3:
    x: float
    y: float
    z: float

@dataclass(frozen=True)
class CameraPlan:
    shot_size: str
    focal_length_mm: float
    motion: str
    start: Vec3
    end: Vec3
    look_at: str

@dataclass(frozen=True)
class ActorPlan:
    actor_id: str
    color: str
    start: Vec3
    end: Vec3
    action: str
    facing: str

@dataclass(frozen=True)
class ContinuityPlan:
    previous_shot: str | None
    screen_direction: str
    axis_side: str

@dataclass(frozen=True)
class Shot:
    shot_id: str
    duration: float
    camera: CameraPlan
    actors: tuple[ActorPlan, ...]
    continuity: ContinuityPlan

@dataclass(frozen=True)
class ShotScript:
    scene_id: str
    fps: int
    world_bounds: tuple[float, float, float, float]
    shots: tuple[Shot, ...]

    @classmethod
    def from_path(cls, path: Path) -> "ShotScript": ...

    def validate(self) -> None: ...
```

Validation must reject duplicate shot IDs, non-positive fps/durations, malformed vectors, missing recurring actors, broken `previous_shot` chains, actor position discontinuity, and unsupported axis/screen-direction values.

Run the focused test, then the full local suite using the bundled Python executable. Commit: `git commit -m "feat: validate executable ShotScript"`.

## Task 3: Render actual 2D preview files

Write `tests/test_preview2d.py` first. It loads the real station ShotScript, renders into a real temporary directory, opens the resulting files with Pillow, and asserts:

- GIF exists, format is GIF, size is 960x540, and has 45 frames (3 shots x 5 s x 3 preview fps);
- contact sheet exists, format is PNG, and contains three labeled panels;
- trajectory report exists and reports actor A ending `s01` exactly where it starts `s02`;
- at least 1% of pixels differ between the first and last frame of `s01`.

The first run must FAIL because `videoactagent.preview2d` does not exist.

Implement `videoactagent/preview2d.py` with:

- linear interpolation for camera and actor positions;
- top-down world-to-pixel projection from `world_bounds`;
- platform grid and action axis;
- colored actor circles with IDs;
- actor path polylines;
- camera position, look direction, and a simple frustum triangle;
- shot ID, time, motion, shot size, and focal length labels;
- GIF writer using Pillow `save_all=True`;
- three-panel contact sheet from the middle frame of each shot;
- JSON trajectory report measured from the parsed positions;
- CLI: `python -m videoactagent.preview2d <shotscript.json> --output-dir <dir> --preview-fps 3`.

Run the focused test and full suite. Commit: `git commit -m "feat: render ShotScript as 2D animated preview"`.

## Task 4: Produce and inspect the actual Stage 1 artifacts

Run with bundled Python:

```powershell
& 'C:\Users\sy\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m videoactagent.preview2d examples/station_shotscript.json --output-dir runs/stage1_station --preview-fps 3
```

Inspect the actual output using Pillow and the local image viewer. Record:

- file sizes and SHA-256 hashes;
- GIF dimensions and frame count;
- first/middle/last frame screenshots;
- actual trajectory endpoint values;
- any visible clipping, wrong labels, discontinuity, or camera-direction errors.

Run the complete local suite fresh. Do not call the stage complete if the real output is missing or visually incorrect.

## Task 5: Stage 1 report

Report separately:

**Local code evidence** — tests, JSON parse, hashes, image dimensions, frame count, trajectory measurements.

**Visual evidence** — clickable contact sheet and preview GIF.

**External evidence** — explicitly `not applicable / not run`: no Blender, Seedance, Kling, VACE, server, or GPU.

List actual limitations and the next Blender installation/runtime decision. Continue autonomously only within the existing no-server/no-paid-resource authorization.

