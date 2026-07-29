from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import warnings

import bpy
from mathutils import Vector


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from videoactagent.shotscript import ActorPlan, Shot, ShotScript, Vec3
from videoactagent.trajectory import TrajectoryInstruction
from videoactagent.trajectory_proxy import camera_world_xy


def parse_args() -> argparse.Namespace:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--shotscript", type=Path)
    parser.add_argument("--trajectory", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--keyframes-only", action="store_true")
    parser.add_argument("--keyframe-frames")
    return parser.parse_args(argv)


def hex_color(value: str) -> tuple[float, float, float, float]:
    cleaned = value.lstrip("#")
    if len(cleaned) != 6:
        raise ValueError(f"unsupported color: {value}")
    return tuple(int(cleaned[index : index + 2], 16) / 255 for index in (0, 2, 4)) + (1.0,)


def create_material(name: str, color: tuple[float, float, float, float], metallic=0.0, roughness=0.5):
    material = bpy.data.materials.new(name)
    material.diffuse_color = color
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        material.use_nodes = True
    principled = material.node_tree.nodes.get("Principled BSDF")
    principled.inputs["Base Color"].default_value = color
    principled.inputs["Metallic"].default_value = metallic
    principled.inputs["Roughness"].default_value = roughness
    return material


def create_emissive_material(name: str, color: tuple[float, float, float, float]):
    material = create_material(name, color, metallic=0.0, roughness=0.25)
    principled = material.node_tree.nodes.get("Principled BSDF")
    emission = principled.inputs.get("Emission Color") or principled.inputs.get("Emission")
    if emission is not None:
        emission.default_value = color
    strength = principled.inputs.get("Emission Strength")
    if strength is not None:
        strength.default_value = 4.0
    return material


def add_cube(name: str, location, scale, material):
    bpy.ops.mesh.primitive_cube_add(location=location)
    obj = bpy.context.object
    obj.name = name
    obj.scale = scale
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    obj.data.materials.append(material)
    return obj


def create_environment():
    platform_material = create_material("platform_mat", (0.32, 0.34, 0.37, 1.0), roughness=0.85)
    edge_material = create_material("safety_line_mat", (0.95, 0.72, 0.08, 1.0), roughness=0.65)
    rail_material = create_material("rail_mat", (0.08, 0.09, 0.11, 1.0), metallic=0.75, roughness=0.28)
    sleeper_material = create_material("sleeper_mat", (0.24, 0.13, 0.08, 1.0), roughness=0.9)
    axis_material = create_material("axis_mat", (0.85, 0.12, 0.12, 1.0), roughness=0.5)

    add_cube("platform", (0, 0, -0.15), (5.5, 2.5, 0.15), platform_material)
    add_cube("safety_line", (0, 2.15, 0.02), (5.3, 0.07, 0.025), edge_material)
    add_cube("action_axis", (0, 0, 0.035), (5.0, 0.025, 0.025), axis_material)
    for y in (3.0, 3.8):
        add_cube(f"rail_{y}", (0, y, -0.05), (6.0, 0.06, 0.06), rail_material)
    for index, x in enumerate(range(-5, 6)):
        add_cube(f"sleeper_{index}", (x, 3.4, -0.12), (0.08, 0.75, 0.05), sleeper_material)


def create_actor(actor: ActorPlan):
    root = bpy.data.objects.new(actor.actor_id, None)
    bpy.context.collection.objects.link(root)
    material = create_material(f"{actor.actor_id}_mat", hex_color(actor.color), roughness=0.55)

    bpy.ops.mesh.primitive_cylinder_add(vertices=24, radius=0.34, depth=1.45, location=(0, 0, 0.95))
    body = bpy.context.object
    body.name = f"{actor.actor_id}_body"
    body.data.materials.append(material)
    body.parent = root

    bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12, radius=0.31, location=(0, 0, 1.9))
    head = bpy.context.object
    head.name = f"{actor.actor_id}_head"
    head.data.materials.append(material)
    head.parent = root

    label_curve = bpy.data.curves.new(f"{actor.actor_id}_label_curve", type="FONT")
    label_curve.body = actor.actor_id.replace("actor_", "").upper()
    label_curve.align_x = "CENTER"
    label_curve.size = 0.32
    label = bpy.data.objects.new(f"{actor.actor_id}_label", label_curve)
    bpy.context.collection.objects.link(label)
    label.location = (0, 0, 2.45)
    label.rotation_euler = (math.radians(90), 0, 0)
    label.data.materials.append(material)
    label.parent = root
    return root


def vec(value: Vec3) -> Vector:
    return Vector((value.x, value.y, value.z))


def lerp(start: Vector, end: Vector, amount: float) -> Vector:
    return start.lerp(end, amount)


def actor_plan(shot: Shot, actor_id: str) -> ActorPlan:
    return next(actor for actor in shot.actors if actor.actor_id == actor_id)


def target_position(shot: Shot, frame_fraction: float, look_at: str) -> Vector:
    if look_at == "actors_midpoint":
        positions = [
            lerp(vec(actor.start), vec(actor.end), frame_fraction)
            for actor in shot.actors
        ]
        target = sum(positions, Vector()) / len(positions)
    else:
        actor = actor_plan(shot, look_at)
        target = lerp(vec(actor.start), vec(actor.end), frame_fraction)
    target.z += 1.25
    return target


def point_camera(camera, target: Vector):
    direction = target - camera.location
    camera.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def configure_scene(script: ShotScript):
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for datablocks in (bpy.data.meshes, bpy.data.curves, bpy.data.materials, bpy.data.cameras, bpy.data.lights):
        for datablock in list(datablocks):
            if datablock.users == 0:
                datablocks.remove(datablock)

    scene = bpy.context.scene
    bpy.context.preferences.edit.keyframe_new_interpolation_type = "LINEAR"
    scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x = 960
    scene.render.resolution_y = 540
    scene.render.resolution_percentage = 100
    scene.render.fps = script.fps
    if scene.render.image_settings.file_format != "FFMPEG":
        raise RuntimeError("launch Blender with '-F FFMPEG' for MP4 output")
    scene.render.ffmpeg.format = "MPEG4"
    scene.render.ffmpeg.codec = "H264"
    scene.render.ffmpeg.constant_rate_factor = "MEDIUM"
    scene.render.film_transparent = False
    scene.world.color = (0.035, 0.055, 0.09)

    create_environment()

    actor_roots = {
        actor.actor_id: create_actor(actor)
        for actor in script.shots[0].actors
    }

    camera_data = bpy.data.cameras.new("DirectorCamera")
    camera = bpy.data.objects.new("DirectorCamera", camera_data)
    bpy.context.collection.objects.link(camera)
    scene.camera = camera

    bpy.ops.object.light_add(type="SUN", location=(0, -2, 8))
    sun = bpy.context.object
    sun.name = "Sun"
    sun.rotation_euler = (math.radians(28), math.radians(-18), math.radians(-28))
    sun.data.energy = 2.0

    bpy.ops.object.light_add(type="AREA", location=(0, -3, 7))
    area = bpy.context.object
    area.name = "AreaKey"
    area.data.energy = 900
    area.data.shape = "DISK"
    area.data.size = 6.0
    point_camera(area, Vector((0, 0, 0.8)))

    frame_cursor = 1
    ranges = []
    for shot in script.shots:
        shot_frames = int(round(shot.duration * script.fps))
        start_frame = frame_cursor
        end_frame = frame_cursor + shot_frames - 1
        ranges.append((shot, start_frame, end_frame))

        for actor in shot.actors:
            root = actor_roots[actor.actor_id]
            root.location = vec(actor.start)
            root.keyframe_insert(data_path="location", frame=start_frame)
            root.location = vec(actor.end)
            root.keyframe_insert(data_path="location", frame=end_frame)

        for frame in range(start_frame, end_frame + 1):
            fraction = 0.0 if end_frame == start_frame else (frame - start_frame) / (end_frame - start_frame)
            camera.location = lerp(vec(shot.camera.start), vec(shot.camera.end), fraction)
            camera.data.lens = shot.camera.focal_length_mm
            point_camera(camera, target_position(shot, fraction, shot.camera.look_at))
            camera.keyframe_insert(data_path="location", frame=frame)
            camera.keyframe_insert(data_path="rotation_euler", frame=frame)
            camera.data.keyframe_insert(data_path="lens", frame=frame)

        frame_cursor = end_frame + 1

    scene.frame_start = 1
    scene.frame_end = frame_cursor - 1
    return scene, actor_roots, camera, ranges


def _remove_keyframes(owner, data_paths: tuple[str, ...], start_frame: int, end_frame: int):
    for frame in range(start_frame, end_frame + 1):
        for data_path in data_paths:
            try:
                owner.keyframe_delete(data_path=data_path, frame=frame)
            except (RuntimeError, TypeError):
                pass


def _frame_for_time(start_frame: int, end_frame: int, normalized_time: float) -> int:
    return start_frame + int(round(normalized_time * (end_frame - start_frame)))


def _actor_world(point, bounds: tuple[float, float, float, float]) -> Vector:
    min_x, max_x, min_y, max_y = bounds
    return Vector(
        (
            min_x + float(point.x) * (max_x - min_x),
            max_y - float(point.y) * (max_y - min_y),
            0.0,
        )
    )


def _camera_world(shot: Shot, point) -> Vector:
    centre = target_position(shot, float(point.t), shot.camera.look_at)
    orbit_scale = 18.0
    height = (shot.camera.start.z + shot.camera.end.z) / 2.0
    world_x, world_y = camera_world_xy(
        point.x, point.y, centre.x, centre.y, orbit_scale
    )
    return Vector(
        (
            world_x,
            world_y,
            height,
        )
    )


def _add_trajectory_curve(name: str, world_points: list[Vector], material):
    curve_data = bpy.data.curves.new(f"{name}_data", type="CURVE")
    curve_data.dimensions = "3D"
    curve_data.bevel_depth = 0.055
    curve_data.bevel_resolution = 3
    spline = curve_data.splines.new("POLY")
    spline.points.add(len(world_points) - 1)
    for spline_point, world in zip(spline.points, world_points):
        spline_point.co = (world.x, world.y, max(0.12, world.z), 1.0)
    obj = bpy.data.objects.new(name, curve_data)
    bpy.context.collection.objects.link(obj)
    obj.data.materials.append(material)
    return obj


def _add_control_marker(track_id: str, index: int, world: Vector, material):
    location = (world.x, world.y, max(0.16, world.z))
    bpy.ops.mesh.primitive_uv_sphere_add(
        segments=12,
        ring_count=8,
        radius=0.10,
        location=location,
    )
    marker = bpy.context.object
    marker.name = f"TrajectoryPoint_{track_id}_{index:02d}"
    marker.data.materials.append(material)

    label_data = bpy.data.curves.new(
        f"TrajectoryLabel_{track_id}_{index:02d}_data", type="FONT"
    )
    label_data.body = str(index)
    label_data.align_x = "CENTER"
    label_data.align_y = "CENTER"
    label_data.size = 0.20
    label_data.extrude = 0.008
    label = bpy.data.objects.new(
        f"TrajectoryLabel_{track_id}_{index:02d}", label_data
    )
    bpy.context.collection.objects.link(label)
    label.location = (world.x, world.y, max(0.28, world.z))
    label.data.materials.append(material)


def apply_trajectory(
    script: ShotScript,
    instruction: TrajectoryInstruction,
    actor_roots,
    camera,
    ranges,
):
    if instruction.scene_id != script.scene_id:
        raise ValueError("trajectory scene does not match ShotScript")
    matches = [item for item in ranges if item[0].shot_id == instruction.shot_id]
    if len(matches) != 1:
        raise ValueError("trajectory must identify exactly one ShotScript shot")
    shot, start_frame, end_frame = matches[0]
    if not math.isclose(instruction.duration_seconds, shot.duration, abs_tol=1e-9):
        raise ValueError("trajectory duration does not match ShotScript shot")

    camera_material = create_emissive_material(
        "trajectory_camera_material", (1.0, 0.02, 0.72, 1.0)
    )
    actor_material = create_emissive_material(
        "trajectory_actor_material", (0.02, 0.95, 1.0, 1.0)
    )
    anchor_material = create_emissive_material(
        "trajectory_anchor_material", (0.95, 0.9, 0.05, 1.0)
    )
    applied = {}

    for track in sorted(instruction.tracks, key=lambda item: item.track_id):
        if track.target_type == "camera":
            if track.target_id != "main_camera":
                raise ValueError(f"unknown camera trajectory target: {track.target_id}")
            _remove_keyframes(
                camera, ("location", "rotation_euler"), start_frame, end_frame
            )
            world_points = [_camera_world(shot, point) for point in track.points]
            for point, world in zip(track.points, world_points):
                frame = _frame_for_time(start_frame, end_frame, point.t)
                camera.location = world
                point_camera(camera, target_position(shot, point.t, shot.camera.look_at))
                camera.keyframe_insert(data_path="location", frame=frame)
                camera.keyframe_insert(data_path="rotation_euler", frame=frame)
            overlay_points = [Vector((world.x, world.y, 0.12)) for world in world_points]
            material = camera_material
        elif track.target_type == "actor":
            if track.target_id not in actor_roots:
                raise ValueError(f"unknown actor trajectory target: {track.target_id}")
            actor = actor_roots[track.target_id]
            _remove_keyframes(actor, ("location",), start_frame, end_frame)
            world_points = [_actor_world(point, script.world_bounds) for point in track.points]
            for point, world in zip(track.points, world_points):
                frame = _frame_for_time(start_frame, end_frame, point.t)
                actor.location = world
                actor.keyframe_insert(data_path="location", frame=frame)
            overlay_points = [Vector((world.x, world.y, 0.12)) for world in world_points]
            material = actor_material
        elif track.target_type == "anchor":
            world_points = [_actor_world(point, script.world_bounds) for point in track.points]
            overlay_points = [Vector((world.x, world.y, 0.12)) for world in world_points]
            material = anchor_material
        else:
            raise ValueError(f"unsupported Blender trajectory target: {track.target_type}")

        _add_trajectory_curve(
            f"TrajectoryCurve_{track.track_id}", overlay_points, material
        )
        for index, world in enumerate(overlay_points, start=1):
            _add_control_marker(track.track_id, index, world, material)
        applied[track.track_id] = {
            "target_type": track.target_type,
            "target_id": track.target_id,
            "primitive": track.primitive,
            "semantic": track.semantic,
            "keyframe_count": len(track.points),
            "frames": [
                _frame_for_time(start_frame, end_frame, point.t)
                for point in track.points
            ],
            "first_world": rounded_vector(world_points[0]),
            "middle_world": rounded_vector(world_points[len(world_points) // 2]),
            "last_world": rounded_vector(world_points[-1]),
        }
    return shot, start_frame, end_frame, applied


def rounded_vector(value: Vector) -> list[float]:
    return [round(float(component), 6) for component in value]


def render_keyframes(output_dir: Path, frames: list[int]) -> list[Path]:
    scene = bpy.context.scene
    if scene.render.image_settings.file_format != "PNG":
        raise RuntimeError("launch keyframe Blender process with '-F PNG'")
    keyframe_dir = output_dir.resolve() / "keyframes"
    keyframe_dir.mkdir(parents=True, exist_ok=True)
    png_paths = []
    for index, frame in enumerate(frames, start=1):
        scene.frame_set(frame)
        png_path = keyframe_dir / f"shot_{index:02d}.png"
        scene.render.filepath = str(png_path)
        bpy.ops.render.render(write_still=True)
        png_paths.append(png_path)
    missing = [str(path) for path in png_paths if not path.is_file() or path.stat().st_size == 0]
    if missing:
        raise RuntimeError(f"missing Blender keyframes: {missing}")
    print("BLENDER_KEYFRAMES_OK", json.dumps([str(path) for path in png_paths]))
    return png_paths


def render_keyframes_in_png_process(
    blend_path: Path, output_dir: Path, frames: list[int]
) -> list[Path]:
    completed = subprocess.run(
        [
            bpy.app.binary_path,
            "--background",
            str(blend_path),
            "-F",
            "PNG",
            "--python",
            str(Path(__file__).resolve()),
            "--",
            "--keyframes-only",
            "--keyframe-frames",
            ",".join(str(frame) for frame in frames),
            "--output-dir",
            str(output_dir),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    evidence = completed.stdout + completed.stderr
    if (
        completed.returncode != 0
        or "Traceback" in completed.stderr
        or "BLENDER_KEYFRAMES_OK" not in evidence
    ):
        raise RuntimeError(f"keyframe Blender process failed:\n{evidence}")
    return [
        output_dir / "keyframes" / f"shot_{index:02d}.png"
        for index in range(1, len(frames) + 1)
    ]


def render_outputs(script: ShotScript, output_dir: Path):
    output_dir = output_dir.resolve()
    keyframe_dir = output_dir / "keyframes"
    keyframe_dir.mkdir(parents=True, exist_ok=True)

    scene, actor_roots, camera, ranges = configure_scene(script)
    blend_path = output_dir / "station_proxy.blend"
    video_path = output_dir / "station_proxy.mp4"
    report_path = output_dir / "trajectory_report.json"
    bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))

    scene.render.ffmpeg.format = "MPEG4"
    scene.render.ffmpeg.codec = "H264"
    scene.render.filepath = str(video_path)
    scene.frame_set(scene.frame_start)
    bpy.ops.render.render(animation=True)

    middle_frames = [
        (start_frame + end_frame) // 2
        for _shot, start_frame, end_frame in ranges
    ]
    png_paths = render_keyframes_in_png_process(
        blend_path, output_dir, middle_frames
    )

    shot_reports = []
    for shot, start_frame, end_frame in ranges:
        actor_positions = {}
        for actor_id, root in actor_roots.items():
            scene.frame_set(start_frame)
            start_position = rounded_vector(root.matrix_world.translation)
            scene.frame_set(end_frame)
            end_position = rounded_vector(root.matrix_world.translation)
            actor_positions[actor_id] = {
                "start": start_position,
                "end": end_position,
            }
        scene.frame_set(start_frame)
        camera_start = rounded_vector(camera.matrix_world.translation)
        scene.frame_set(end_frame)
        camera_end = rounded_vector(camera.matrix_world.translation)
        shot_reports.append(
            {
                "shot_id": shot.shot_id,
                "frame_start": start_frame,
                "frame_end": end_frame,
                "camera_motion": shot.camera.motion,
                "camera_positions": {"start": camera_start, "end": camera_end},
                "actor_positions": actor_positions,
            }
        )

    report = {
        "blender_version": bpy.app.version_string,
        "scene_frame_start": scene.frame_start,
        "scene_frame_end": scene.frame_end,
        "rendered_frames": scene.frame_end - scene.frame_start + 1,
        "fps": scene.render.fps,
        "resolution": [scene.render.resolution_x, scene.render.resolution_y],
        "shots": shot_reports,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    required = [blend_path, video_path, report_path, *png_paths]
    missing = [str(path) for path in required if not path.is_file() or path.stat().st_size == 0]
    if missing:
        raise RuntimeError(f"missing Blender outputs: {missing}")
    print(
        "BLENDER_PROXY_OK",
        json.dumps(
            {
                "blend": str(blend_path),
                "video": str(video_path),
                "report": str(report_path),
                "frames": scene.frame_end,
            },
            ensure_ascii=False,
        ),
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def render_trajectory_outputs(
    script: ShotScript,
    instruction: TrajectoryInstruction,
    shotscript_path: Path,
    trajectory_path: Path,
    output_dir: Path,
):
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    scene, actor_roots, camera, ranges = configure_scene(script)
    shot, start_frame, end_frame, applied = apply_trajectory(
        script, instruction, actor_roots, camera, ranges
    )

    blend_path = output_dir / "trajectory_proxy.blend"
    video_path = output_dir / "trajectory_proxy.mp4"
    manifest_path = output_dir / "trajectory_proxy_manifest.json"
    bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))

    scene.render.ffmpeg.format = "MPEG4"
    scene.render.ffmpeg.codec = "H264"
    scene.render.filepath = str(video_path)
    scene.frame_set(scene.frame_start)
    bpy.ops.render.render(animation=True)

    middle_frame = (start_frame + end_frame) // 2
    temporary_frames = render_keyframes_in_png_process(
        blend_path, output_dir, [start_frame, middle_frame, end_frame]
    )
    frames_dir = output_dir / "frames"
    frames_dir.mkdir()
    frame_paths = []
    for source, name in zip(temporary_frames, ("first.png", "middle.png", "last.png")):
        target = frames_dir / name
        source.replace(target)
        frame_paths.append(target)
    (output_dir / "keyframes").rmdir()
    overlay_path = output_dir / "trajectory_overlay.png"
    shutil.copyfile(frame_paths[1], overlay_path)

    scene_objects = sorted(
        obj.name for obj in bpy.data.objects if obj.name.startswith("Trajectory")
    )
    manifest = {
        "schema_version": "0.1",
        "renderer": "blender",
        "blender_version": bpy.app.version_string,
        "shotscript_sha256": sha256_file(shotscript_path),
        "trajectory_sha256": sha256_file(trajectory_path),
        "controlled_shot": {
            "scene_id": instruction.scene_id,
            "shot_id": shot.shot_id,
            "frame_range": [start_frame, end_frame],
            "sample_frames": [start_frame, middle_frame, end_frame],
        },
        "video": {
            "path": video_path.name,
            "sha256": sha256_file(video_path),
            "bytes": video_path.stat().st_size,
            "frame_count": scene.frame_end - scene.frame_start + 1,
            "fps": scene.render.fps,
            "resolution": [scene.render.resolution_x, scene.render.resolution_y],
        },
        "blend": {
            "path": blend_path.name,
            "sha256": sha256_file(blend_path),
            "bytes": blend_path.stat().st_size,
        },
        "frames": {
            path.stem: {
                "path": str(path.relative_to(output_dir)).replace("\\", "/"),
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
            for path in frame_paths
        },
        "overlay": {
            "path": overlay_path.name,
            "sha256": sha256_file(overlay_path),
            "bytes": overlay_path.stat().st_size,
            "source": "blender_rendered_middle_frame",
        },
        "applied_tracks": applied,
        "scene_objects": scene_objects,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    required = [blend_path, video_path, manifest_path, overlay_path, *frame_paths]
    missing = [
        str(path) for path in required if not path.is_file() or path.stat().st_size == 0
    ]
    if missing:
        raise RuntimeError(f"missing Blender trajectory outputs: {missing}")
    print(
        "TRAJECTORY_PROXY_OK",
        json.dumps(
            {
                "video": str(video_path),
                "manifest": str(manifest_path),
                "frames": scene.frame_end,
                "controlled_shot": shot.shot_id,
            },
            ensure_ascii=False,
        ),
    )


def main():
    args = parse_args()
    if args.keyframes_only:
        if not args.keyframe_frames:
            raise ValueError("--keyframe-frames is required with --keyframes-only")
        render_keyframes(
            args.output_dir,
            [int(value) for value in args.keyframe_frames.split(",")],
        )
        return
    if args.shotscript is None:
        raise ValueError("--shotscript is required")
    script = ShotScript.from_path(args.shotscript)
    if args.trajectory is not None:
        instruction = TrajectoryInstruction.from_path(args.trajectory)
        render_trajectory_outputs(
            script,
            instruction,
            args.shotscript,
            args.trajectory,
            args.output_dir,
        )
    else:
        render_outputs(script, args.output_dir)


if __name__ == "__main__":
    main()
