from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import subprocess
import sys
import warnings

import bpy
from mathutils import Vector


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from videoactagent.shotscript import ActorPlan, Shot, ShotScript, Vec3


def parse_args() -> argparse.Namespace:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--shotscript", type=Path)
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
    render_outputs(script, args.output_dir)


if __name__ == "__main__":
    main()
