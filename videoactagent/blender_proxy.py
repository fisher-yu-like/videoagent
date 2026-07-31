from __future__ import annotations

import argparse
from dataclasses import dataclass
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
from videoactagent.blender_runner import positive_int, resolution_value
from videoactagent.trajectory import TrajectoryInstruction
from videoactagent.trajectory_proxy import camera_world_xy
from videoactagent.director_annotation import (
    CameraKeyframe,
    camera_trajectory_from_path,
)


@dataclass(frozen=True)
class RenderProfile:
    style: str
    fps: int
    resolution: tuple[int, int]

    def __post_init__(self):
        if self.style not in {"diagnostic", "clay"}:
            raise ValueError(f"unsupported render style: {self.style}")
        if type(self.fps) is not int or self.fps <= 0:
            raise ValueError("fps must be a positive integer")
        if (
            not isinstance(self.resolution, tuple)
            or len(self.resolution) != 2
            or any(type(value) is not int or value <= 0 for value in self.resolution)
        ):
            raise ValueError("resolution must contain two positive integers")

    def as_dict(self) -> dict[str, object]:
        return {"fps": self.fps, "resolution": list(self.resolution)}


DEFAULT_RENDER_PROFILE = RenderProfile("diagnostic", 3, (960, 540))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    if argv is None:
        argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--shotscript", type=Path)
    parser.add_argument("--trajectory", type=Path)
    parser.add_argument("--camera-trajectory", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--keyframes-only", action="store_true")
    parser.add_argument("--keyframe-frames")
    parser.add_argument(
        "--render-style",
        choices=("diagnostic", "clay"),
        default="diagnostic",
    )
    parser.add_argument("--fps", type=positive_int, default=3)
    parser.add_argument("--resolution", type=resolution_value, default=(960, 540))
    args = parser.parse_args(argv)
    if args.camera_trajectory is not None and args.trajectory is None:
        parser.error("--camera-trajectory requires --trajectory")
    return args


def hex_color(value: str) -> tuple[float, float, float, float]:
    cleaned = value.lstrip("#")
    if len(cleaned) != 6:
        raise ValueError(f"unsupported color: {value}")
    return tuple(int(cleaned[index : index + 2], 16) / 255 for index in (0, 2, 4)) + (1.0,)


def clay_actor_color(actor_index: int) -> tuple[float, float, float, float]:
    if type(actor_index) is not int or actor_index < 0:
        raise ValueError("actor_index must be a non-negative integer")
    value = actor_index + 1
    fraction = 0.0
    denominator = 1.0
    while value:
        value, remainder = divmod(value, 2)
        denominator *= 2.0
        fraction += remainder / denominator
    gray = 0.2 + 0.6 * fraction
    return gray, gray, gray, 1.0


def effective_material_color(
    profile: RenderProfile,
    color: tuple[float, float, float, float],
    clay_gray: float | None = None,
) -> tuple[float, float, float, float]:
    if profile.style == "diagnostic":
        return color
    gray = (
        float(clay_gray)
        if clay_gray is not None
        else 0.2126 * color[0] + 0.7152 * color[1] + 0.0722 * color[2]
    )
    return gray, gray, gray, color[3]


def hide_in_clay(object_name: str) -> bool:
    return (
        object_name == "action_axis"
        or object_name.endswith("_label")
        or object_name.startswith(
            ("TrajectoryCurve_", "TrajectoryPoint_", "TrajectoryLabel_")
        )
    )


def apply_render_visibility(profile: RenderProfile):
    hide_diagnostics = profile.style == "clay"
    for obj in bpy.data.objects:
        if hide_in_clay(obj.name):
            obj.hide_render = hide_diagnostics


def validate_shot_frame_counts(
    script: ShotScript, profile: RenderProfile
) -> dict[str, int]:
    frame_counts: dict[str, int] = {}
    for shot in script.shots:
        frame_count = int(round(shot.duration * profile.fps))
        if frame_count < 1:
            raise ValueError(
                f"{shot.shot_id} duration {shot.duration:g}s at {profile.fps} fps "
                f"rounds to {frame_count} frames; increase --fps or shot duration"
            )
        frame_counts[shot.shot_id] = frame_count
    return frame_counts


def create_material(
    profile: RenderProfile,
    name: str,
    color: tuple[float, float, float, float],
    metallic=0.0,
    roughness=0.5,
    clay_gray: float | None = None,
):
    color = effective_material_color(profile, color, clay_gray)
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


def create_emissive_material(
    profile: RenderProfile,
    name: str,
    color: tuple[float, float, float, float],
):
    material = create_material(
        profile, name, color, metallic=0.0, roughness=0.25
    )
    principled = material.node_tree.nodes.get("Principled BSDF")
    emission = principled.inputs.get("Emission Color") or principled.inputs.get("Emission")
    if emission is not None:
        emission.default_value = effective_material_color(profile, color)
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


def add_cylinder(name: str, location, radius, depth, material, vertices=24):
    bpy.ops.mesh.primitive_cylinder_add(
        vertices=vertices, radius=radius, depth=depth, location=location
    )
    obj = bpy.context.object
    obj.name = name
    obj.data.materials.append(material)
    return obj


def add_uv_sphere(name: str, location, radius, material):
    bpy.ops.mesh.primitive_uv_sphere_add(
        segments=20, ring_count=12, radius=radius, location=location
    )
    obj = bpy.context.object
    obj.name = name
    obj.data.materials.append(material)
    return obj


def create_action_axis(profile: RenderProfile):
    axis_material = create_material(
        profile, "axis_mat", (0.85, 0.12, 0.12, 1.0), roughness=0.5
    )
    add_cube("action_axis", (0, 0, 0.035), (5.0, 0.025, 0.025), axis_material)


def create_station_environment(profile: RenderProfile):
    platform_material = create_material(profile, "platform_mat", (0.32, 0.34, 0.37, 1.0), roughness=0.85)
    edge_material = create_material(profile, "safety_line_mat", (0.95, 0.72, 0.08, 1.0), roughness=0.65)
    rail_material = create_material(profile, "rail_mat", (0.08, 0.09, 0.11, 1.0), metallic=0.75, roughness=0.28)
    sleeper_material = create_material(profile, "sleeper_mat", (0.24, 0.13, 0.08, 1.0), roughness=0.9)

    add_cube("platform", (0, 0, -0.15), (5.5, 2.5, 0.15), platform_material)
    add_cube("safety_line", (0, 2.15, 0.02), (5.3, 0.07, 0.025), edge_material)
    create_action_axis(profile)
    for y in (3.0, 3.8):
        add_cube(f"rail_{y}", (0, y, -0.05), (6.0, 0.06, 0.06), rail_material)
    for index, x in enumerate(range(-5, 6)):
        add_cube(f"sleeper_{index}", (x, 3.4, -0.12), (0.08, 0.75, 0.05), sleeper_material)


def create_city_crosswalk_environment(profile: RenderProfile):
    asphalt = create_material(profile, "asphalt_mat", (0.075, 0.085, 0.095, 1.0), roughness=0.92)
    stripe = create_material(profile, "crosswalk_mat", (0.86, 0.88, 0.84, 1.0), roughness=0.72)
    curb = create_material(profile, "curb_mat", (0.38, 0.40, 0.42, 1.0), roughness=0.82)
    building_a = create_material(profile, "building_a_mat", (0.27, 0.38, 0.48, 1.0), roughness=0.78)
    building_b = create_material(profile, "building_b_mat", (0.48, 0.29, 0.24, 1.0), roughness=0.78)
    window = create_emissive_material(profile, "window_mat", (0.95, 0.69, 0.22, 1.0))
    pole = create_material(profile, "street_pole_mat", (0.07, 0.08, 0.09, 1.0), metallic=0.65, roughness=0.3)

    add_cube("road", (0, 0, -0.14), (6.0, 3.2, 0.14), asphalt)
    for index, x in enumerate((-3.6, -2.4, -1.2, 0.0, 1.2, 2.4, 3.6)):
        add_cube(f"crosswalk_stripe_{index}", (x, 0, 0.012), (0.38, 2.4, 0.018), stripe)
    add_cube("far_sidewalk", (0, 3.65, 0.02), (6.0, 0.45, 0.20), curb)
    add_cube("city_building_left", (-3.7, 5.0, 2.0), (1.7, 1.0, 2.2), building_a)
    add_cube("city_building_right", (3.2, 5.2, 2.6), (2.0, 1.1, 2.8), building_b)
    for index, x in enumerate((-4.2, -3.2, 2.5, 3.5)):
        add_cube(f"window_{index}", (x, 3.98, 2.7), (0.28, 0.03, 0.34), window)
    for index, x in enumerate((-5.0, 5.0)):
        add_cylinder(f"street_pole_{index}", (x, 2.9, 1.5), 0.07, 3.0, pole)
        add_uv_sphere(f"street_lamp_{index}", (x, 2.9, 3.05), 0.18, window)
    create_action_axis(profile)


def create_forest_path_environment(profile: RenderProfile):
    grass = create_material(profile, "grass_mat", (0.12, 0.29, 0.10, 1.0), roughness=0.95)
    path = create_material(profile, "path_mat", (0.40, 0.27, 0.14, 1.0), roughness=0.98)
    trunk = create_material(profile, "trunk_mat", (0.20, 0.095, 0.035, 1.0), roughness=0.95)
    foliage_a = create_material(profile, "foliage_a_mat", (0.07, 0.24, 0.055, 1.0), roughness=0.9)
    foliage_b = create_material(profile, "foliage_b_mat", (0.13, 0.38, 0.08, 1.0), roughness=0.9)
    stone = create_material(profile, "stone_mat", (0.28, 0.31, 0.29, 1.0), roughness=1.0)

    add_cube("forest_ground", (0, 0.8, -0.16), (6.0, 4.0, 0.16), grass)
    add_cube("forest_path", (0, 0, -0.005), (5.4, 1.15, 0.025), path)
    tree_positions = ((-5.0, 2.4), (-3.6, 3.4), (3.4, 3.1), (5.0, 2.1))
    for index, (x, y) in enumerate(tree_positions):
        add_cylinder(f"tree_trunk_{index}", (x, y, 1.2), 0.22, 2.4, trunk, vertices=16)
        foliage = foliage_a if index % 2 == 0 else foliage_b
        add_uv_sphere(f"tree_crown_{index}", (x, y, 2.75), 1.05, foliage)
    for index, x in enumerate((-4.2, -2.8, 2.6, 4.2)):
        add_uv_sphere(f"path_stone_{index}", (x, -1.45, 0.12), 0.16, stone)
    create_action_axis(profile)


def create_studio_room_environment(profile: RenderProfile):
    floor = create_material(profile, "studio_floor_mat", (0.17, 0.18, 0.20, 1.0), roughness=0.7)
    wall = create_material(profile, "studio_wall_mat", (0.30, 0.23, 0.35, 1.0), roughness=0.82)
    panel = create_material(profile, "acoustic_panel_mat", (0.08, 0.10, 0.14, 1.0), roughness=0.9)
    sofa = create_material(profile, "sofa_mat", (0.08, 0.28, 0.34, 1.0), roughness=0.75)
    warm = create_emissive_material(profile, "studio_warm_mat", (1.0, 0.42, 0.12, 1.0))

    add_cube("studio_floor", (0, 0.5, -0.14), (6.0, 4.0, 0.14), floor)
    add_cube("studio_back_wall", (0, 4.0, 2.8), (6.0, 0.12, 3.0), wall)
    add_cube("studio_left_wall", (-5.9, 1.0, 2.8), (0.12, 3.0, 3.0), wall)
    for index, x in enumerate((-3.6, -1.2, 1.2, 3.6)):
        add_cube(f"acoustic_panel_{index}", (x, 3.84, 2.8), (0.72, 0.05, 1.05), panel)
    add_cube("studio_sofa_base", (3.7, 2.75, 0.48), (1.15, 0.42, 0.48), sofa)
    add_cube("studio_sofa_back", (3.7, 3.08, 1.05), (1.15, 0.16, 0.72), sofa)
    add_cube("studio_light_bar", (-3.8, 3.72, 2.75), (0.07, 0.05, 1.1), warm)
    create_action_axis(profile)


ENVIRONMENT_BUILDERS = {
    "station": create_station_environment,
    "city_crosswalk": create_city_crosswalk_environment,
    "forest_path": create_forest_path_environment,
    "studio_room": create_studio_room_environment,
}


def create_environment(profile: RenderProfile, preset: str):
    try:
        builder = ENVIRONMENT_BUILDERS[preset]
    except KeyError as exc:
        raise ValueError(f"unsupported environment preset: {preset}") from exc
    builder(profile)


def create_actor(profile: RenderProfile, actor: ActorPlan, actor_index: int):
    root = bpy.data.objects.new(actor.actor_id, None)
    bpy.context.collection.objects.link(root)
    material = create_material(
        profile,
        f"{actor.actor_id}_mat",
        hex_color(actor.color),
        roughness=0.55,
        clay_gray=clay_actor_color(actor_index)[0],
    )

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
    elif look_at == "fixed_actors_midpoint":
        target = sum((vec(actor.start) for actor in shot.actors), Vector()) / len(
            shot.actors
        )
    else:
        actor = actor_plan(shot, look_at)
        target = lerp(vec(actor.start), vec(actor.end), frame_fraction)
    target.z += 1.25
    return target


def point_camera(camera, target: Vector):
    direction = target - camera.location
    camera.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def configure_scene(
    script: ShotScript,
    profile: RenderProfile = DEFAULT_RENDER_PROFILE,
):
    shot_frame_counts = validate_shot_frame_counts(script, profile)
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for datablocks in (bpy.data.meshes, bpy.data.curves, bpy.data.materials, bpy.data.cameras, bpy.data.lights):
        for datablock in list(datablocks):
            if datablock.users == 0:
                datablocks.remove(datablock)

    scene = bpy.context.scene
    bpy.context.preferences.edit.keyframe_new_interpolation_type = "LINEAR"
    scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x = profile.resolution[0]
    scene.render.resolution_y = profile.resolution[1]
    scene.render.resolution_percentage = 100
    scene.render.fps = profile.fps
    if scene.render.image_settings.file_format != "FFMPEG":
        raise RuntimeError("launch Blender with '-F FFMPEG' for MP4 output")
    scene.render.ffmpeg.format = "MPEG4"
    scene.render.ffmpeg.codec = "H264"
    scene.render.ffmpeg.constant_rate_factor = "MEDIUM"
    scene.render.film_transparent = False
    scene.world.color = (0.035, 0.055, 0.09)

    create_environment(profile, script.environment_preset)

    actor_roots = {
        actor.actor_id: create_actor(profile, actor, actor_index)
        for actor_index, actor in enumerate(script.shots[0].actors)
    }
    apply_render_visibility(profile)

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
        shot_frames = shot_frame_counts[shot.shot_id]
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
    profile: RenderProfile,
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
        profile, "trajectory_camera_material", (1.0, 0.02, 0.72, 1.0)
    )
    actor_material = create_emissive_material(
        profile, "trajectory_actor_material", (0.02, 0.95, 1.0, 1.0)
    )
    anchor_material = create_emissive_material(
        profile, "trajectory_anchor_material", (0.95, 0.9, 0.05, 1.0)
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
    apply_render_visibility(profile)
    return shot, start_frame, end_frame, applied


def apply_camera_trajectory(
    states: tuple[CameraKeyframe, ...], camera, start_frame: int, end_frame: int
) -> list[dict[str, object]]:
    _remove_keyframes(camera, ("location", "rotation_euler"), start_frame, end_frame)
    _remove_keyframes(camera.data, ("lens",), start_frame, end_frame)
    previous_interpolation = bpy.context.preferences.edit.keyframe_new_interpolation_type
    applied = []
    try:
        for state in states:
            frame = _frame_for_time(start_frame, end_frame, state.t)
            bpy.context.preferences.edit.keyframe_new_interpolation_type = state.interpolation.upper()
            camera.location = Vector(state.position)
            direction = Vector(state.look_at) - camera.location
            camera.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
            if state.roll_degrees:
                camera.rotation_euler.rotate_axis("Z", math.radians(state.roll_degrees))
            camera.data.lens = state.focal_length_mm
            camera.keyframe_insert(data_path="location", frame=frame)
            camera.keyframe_insert(data_path="rotation_euler", frame=frame)
            camera.data.keyframe_insert(data_path="lens", frame=frame)
            applied.append({
                "keyframe_id": state.keyframe_id, "t": state.t, "frame": frame,
                "position": list(state.position), "look_at": list(state.look_at),
                "focal_length_mm": state.focal_length_mm,
                "shot_size": state.shot_size, "interpolation": state.interpolation,
                "roll_degrees": state.roll_degrees,
            })
    finally:
        bpy.context.preferences.edit.keyframe_new_interpolation_type = previous_interpolation
    return applied


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


def render_outputs(
    script: ShotScript,
    output_dir: Path,
    profile: RenderProfile = DEFAULT_RENDER_PROFILE,
):
    output_dir = output_dir.resolve()
    keyframe_dir = output_dir / "keyframes"
    keyframe_dir.mkdir(parents=True, exist_ok=True)

    scene, actor_roots, camera, ranges = configure_scene(script, profile)
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
        camera_rotation_start = rounded_vector(camera.rotation_euler)
        scene.frame_set(end_frame)
        camera_end = rounded_vector(camera.matrix_world.translation)
        camera_rotation_end = rounded_vector(camera.rotation_euler)
        shot_reports.append(
            {
                "shot_id": shot.shot_id,
                "frame_start": start_frame,
                "frame_end": end_frame,
                "camera_motion": shot.camera.motion,
                "camera_positions": {"start": camera_start, "end": camera_end},
                "camera_rotations": {
                    "start": camera_rotation_start,
                    "end": camera_rotation_end,
                },
                "actor_positions": actor_positions,
            }
        )

    report = {
        "blender_version": bpy.app.version_string,
        "scene_id": script.scene_id,
        "environment_preset": script.environment_preset,
        "scene_frame_start": scene.frame_start,
        "scene_frame_end": scene.frame_end,
        "rendered_frames": scene.frame_end - scene.frame_start + 1,
        "render_style": profile.style,
        "effective_profile": profile.as_dict(),
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
    profile: RenderProfile = DEFAULT_RENDER_PROFILE,
    camera_trajectory_path: Path | None = None,
):
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    scene, actor_roots, camera, ranges = configure_scene(script, profile)
    shot, start_frame, end_frame, applied = apply_trajectory(
        profile, script, instruction, actor_roots, camera, ranges
    )
    applied_camera = []
    if camera_trajectory_path is not None:
        camera_states = camera_trajectory_from_path(
            camera_trajectory_path, scene_id=script.scene_id, shot_id=shot.shot_id,
            duration_seconds=shot.duration,
        )
        applied_camera = apply_camera_trajectory(
            camera_states, camera, start_frame, end_frame
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
        "render_style": profile.style,
        "effective_profile": profile.as_dict(),
        "shotscript_sha256": sha256_file(shotscript_path),
        "trajectory_sha256": sha256_file(trajectory_path),
        "camera_trajectory_sha256": (
            sha256_file(camera_trajectory_path)
            if camera_trajectory_path is not None else None
        ),
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
        "applied_camera_trajectory": applied_camera,
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
    profile = RenderProfile(args.render_style, args.fps, args.resolution)
    if args.trajectory is not None:
        instruction = TrajectoryInstruction.from_path(args.trajectory)
        render_trajectory_outputs(
            script,
            instruction,
            args.shotscript,
            args.trajectory,
            args.output_dir,
            profile,
            args.camera_trajectory,
        )
    else:
        render_outputs(script, args.output_dir, profile)


if __name__ == "__main__":
    main()
