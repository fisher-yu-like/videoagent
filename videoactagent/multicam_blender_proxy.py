"""Blender-side synchronized three-camera renderer."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bpy
from mathutils import Vector

from videoactagent.blender_proxy import (
    RenderProfile,
    apply_camera_trajectory,
    apply_trajectory,
    configure_scene,
)
from videoactagent.director_annotation import CameraKeyframe
from videoactagent.shotscript import ShotScript
from videoactagent.trajectory import TrajectoryInstruction


def _resolution(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([1-9][0-9]*)x([1-9][0-9]*)", value)
    if match is None:
        raise ValueError("resolution must be WIDTHxHEIGHT")
    return int(match.group(1)), int(match.group(2))


def _args() -> argparse.Namespace:
    values = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--shotscript", type=Path, required=True)
    parser.add_argument("--trajectory", type=Path, required=True)
    parser.add_argument("--camera-bundle", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--render-style", choices=("diagnostic", "clay"), required=True)
    parser.add_argument("--fps", type=int, required=True)
    parser.add_argument("--resolution", type=_resolution, required=True)
    return parser.parse_args(values)


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_bundle(path: Path, script: ShotScript) -> dict[str, tuple[CameraKeyframe, ...]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != "1.0"
        or value.get("scene_id") != script.scene_id
        or value.get("shot_id") != script.shots[0].shot_id
        or set(value.get("cameras", {})) != {"camera_a", "camera_b", "camera_c"}
    ):
        raise ValueError("camera bundle identity is invalid")
    result = {}
    for camera_id, camera in value["cameras"].items():
        states = camera.get("states") if isinstance(camera, dict) else None
        if not isinstance(states, list) or len(states) != 5:
            raise ValueError(f"{camera_id} must contain K0--K4")
        result[camera_id] = tuple(CameraKeyframe(
            keyframe_id=state["keyframe_id"],
            t=float(state["t"]),
            position=tuple(float(item) for item in state["position"]),
            look_at=tuple(float(item) for item in state["look_at"]),
            focal_length_mm=float(state["focal_length_mm"]),
            shot_size=state["shot_size"],
            interpolation=state["interpolation"],
            roll_degrees=float(state["roll_degrees"]),
        ) for state in states)
    return result


def _new_camera(camera_id: str):
    data = bpy.data.cameras.new(camera_id + "_data")
    camera = bpy.data.objects.new(camera_id, data)
    bpy.context.collection.objects.link(camera)
    return camera


def _actor_meshes(actor_id: str):
    return [
        obj for obj in bpy.data.objects
        if obj.type == "MESH" and obj.name.startswith(actor_id + "__")
    ]


def _configure_structure_outputs(scene, camera_dir: Path, actor_ids: list[str]) -> list[str]:
    view_layer = scene.view_layers[0]
    view_layer.use_pass_z = True
    view_layer.use_pass_cryptomatte_object = True
    for index, actor_id in enumerate(actor_ids, start=1):
        for obj in _actor_meshes(actor_id):
            obj.pass_index = index

    if not hasattr(scene, "compositing_node_group"):
        raise RuntimeError("multicam structure rendering requires Blender 5.0 or newer")
    tree = scene.compositing_node_group
    if tree is None:
        tree = bpy.data.node_groups.new("MulticamCompositor", "CompositorNodeTree")
        scene.compositing_node_group = tree
    nodes = tree.nodes
    links = tree.links
    nodes.clear()
    render_layers = nodes.new("CompositorNodeRLayers")
    structure = nodes.new("CompositorNodeOutputFile")
    structure_dir = camera_dir / "structure"
    structure_dir.mkdir()
    structure.directory = str(structure_dir)
    structure.file_name = "structure_"
    passes = ["Depth", "CryptoObject00", "CryptoObject01", "CryptoObject02"]
    structure.file_output_items.new("FLOAT", "Depth")
    for name in passes[1:]:
        structure.file_output_items.new("RGBA", name)
    for name in passes:
        if name not in render_layers.outputs:
            raise RuntimeError(f"Blender structure pass is unavailable: {name}")
        links.new(render_layers.outputs[name], structure.inputs[name])
    return passes


def _object_roots(instruction: TrajectoryInstruction):
    roots = {}
    for track in instruction.tracks:
        if track.target_type != "object":
            continue
        object_name = f"prop__{track.target_id}"
        root = bpy.data.objects.get(object_name)
        if root is None:
            raise RuntimeError(f"object trajectory target is missing from Blender: {object_name}")
        roots[track.target_id] = root
    return roots


def _world_frames(scene, actor_roots, object_roots, camera_objects):
    records = []
    for frame in range(scene.frame_start, scene.frame_end + 1):
        scene.frame_set(frame)
        cameras = {}
        for camera_id, camera in camera_objects.items():
            direction = camera.matrix_world.to_quaternion() @ Vector((0.0, 0.0, -1.0))
            cameras[camera_id] = {
                "position": [round(float(value), 8) for value in camera.matrix_world.translation],
                "view_direction": [round(float(value), 8) for value in direction],
                "focal_length_mm": round(float(camera.data.lens), 8),
            }
        records.append({
            "frame": frame,
            "actors": {
                actor_id: [round(float(value), 8) for value in root.matrix_world.translation]
                for actor_id, root in actor_roots.items()
            },
            "objects": {
                object_id: [round(float(value), 8) for value in root.matrix_world.translation]
                for object_id, root in object_roots.items()
            },
            "cameras": cameras,
        })
    return records


def render(args: argparse.Namespace) -> None:
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    script = ShotScript.from_path(args.shotscript)
    instruction = TrajectoryInstruction.from_path(args.trajectory)
    camera_states = _load_bundle(args.camera_bundle, script)
    profile = RenderProfile(args.render_style, args.fps, args.resolution)
    scene, actor_roots, base_camera, ranges = configure_scene(script, profile)
    shot, start_frame, end_frame, applied_tracks = apply_trajectory(
        profile, script, instruction, actor_roots, base_camera, ranges
    )
    base_camera.name = "camera_a"
    base_camera.data.name = "camera_a_data"
    cameras = {
        "camera_a": base_camera,
        "camera_b": _new_camera("camera_b"),
        "camera_c": _new_camera("camera_c"),
    }
    applied_cameras = {
        camera_id: apply_camera_trajectory(
            camera_states[camera_id], camera, start_frame, end_frame
        )
        for camera_id, camera in cameras.items()
    }
    object_roots = _object_roots(instruction)
    shared_frames = _world_frames(scene, actor_roots, object_roots, cameras)
    blend = output / "multicam_proxy.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(blend))

    actor_ids = sorted(actor_roots)
    camera_records: dict[str, Any] = {}
    for camera_id, camera in cameras.items():
        camera_dir = output / camera_id
        camera_dir.mkdir()
        structure_passes = _configure_structure_outputs(scene, camera_dir, actor_ids)
        scene.camera = camera
        video = camera_dir / "proxy.mp4"
        scene.render.image_settings.file_format = "FFMPEG"
        scene.render.ffmpeg.format = "MPEG4"
        scene.render.ffmpeg.codec = "H264"
        scene.render.filepath = str(video)
        scene.frame_set(scene.frame_start)
        bpy.ops.render.render(animation=True)
        structure_frames = sorted((camera_dir / "structure").glob("structure_*.exr"))
        expected = scene.frame_end - scene.frame_start + 1
        if (
            not video.is_file() or video.stat().st_size == 0
            or len(structure_frames) != expected
        ):
            raise RuntimeError(f"{camera_id} synchronized outputs are incomplete")
        camera_records[camera_id] = {
            "video": {
                "path": video.relative_to(output).as_posix(),
                "sha256": _sha(video),
                "bytes": video.stat().st_size,
                "frame_count": expected,
                "fps": scene.render.fps,
                "resolution": [scene.render.resolution_x, scene.render.resolution_y],
            },
            "structure_frames": [
                path.relative_to(output).as_posix() for path in structure_frames
            ],
            "structure_passes": structure_passes,
            "applied_camera_trajectory": applied_cameras[camera_id],
        }

    manifest = {
        "schema_version": "1.0",
        "renderer": "blender",
        "blender_version": bpy.app.version_string,
        "scene_id": script.scene_id,
        "shot_id": shot.shot_id,
        "shared_blend": {
            "path": blend.relative_to(output).as_posix(),
            "sha256": _sha(blend),
            "bytes": blend.stat().st_size,
        },
        "source_hashes": {
            "shotscript": _sha(args.shotscript),
            "trajectory": _sha(args.trajectory),
            "camera_bundle": _sha(args.camera_bundle),
        },
        "applied_actor_tracks": applied_tracks,
        "shared_world_frames": shared_frames,
        "cameras": camera_records,
    }
    manifest_path = output / "multicam_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print("MULTICAM_PROXY_OK " + json.dumps({
        "manifest": str(manifest_path),
        "frames": scene.frame_end - scene.frame_start + 1,
        "cameras": sorted(camera_records),
    }))


if __name__ == "__main__":
    render(_args())
