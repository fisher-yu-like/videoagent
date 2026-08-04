import bpy
import mathutils

def _material(name, color):
    material = bpy.data.materials.new(name)
    material.diffuse_color = (*color, 1.0)
    return material

def _cube(name, location, scale, material=None):
    bpy.ops.mesh.primitive_cube_add(location=location)
    obj = bpy.context.object
    obj.name = name
    obj.scale = scale
    if material is not None:
        obj.data.materials.append(material)
    return obj

def build_scene(context):
    document = context["input"]
    actors = document["shotscript"]["shots"][0]["actors"]
    bounds = document["render_contract"]["world_bounds"]
    actor_tracks = {track["target"]["id"]: track for track in document["trajectory"]["tracks"]}
    orange = _material("orange", (0.9, 0.25, 0.04))
    blue = _material("blue", (0.05, 0.25, 0.75))
    gray = _material("platform", (0.18, 0.2, 0.24))
    _cube("Platform", (0.0, 0.0, -0.35), (5.0, 4.0, 0.35), gray)
    _cube("BackWall", (0.0, 3.8, 2.0), (5.0, 0.12, 2.0), gray)
    for index, actor in enumerate(actors):
        obj = _cube(actor["id"], (0.0, 0.0, 0.8), (0.38, 0.38, 0.8), orange if index == 0 else blue)
        for point in actor_tracks[actor["id"]]["points"]:
            world = point["world"]
            frame = context["frame_for_time"](document["render_contract"]["frame_start"], document["render_contract"]["frame_end"], point["t"])
            obj.location = (world[0], world[1], 0.8)
            obj.keyframe_insert(data_path="location", frame=frame)
    bpy.ops.object.camera_add(location=(0.0, -10.0, 6.0))
    camera = bpy.context.object
    camera.name = "DirectorCamera"
    camera.data.lens = 35.0
    direction = mathutils.Vector((0.0, 0.0, 0.6)) - camera.location
    camera.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    bpy.context.scene.world.color = (0.025, 0.03, 0.05)
    bpy.ops.object.light_add(type="AREA", location=(0.0, -2.0, 7.0))
    key = bpy.context.object
    key.name = "KeyLight"
    key.data.energy = 1000
    key.data.shape = "DISK"
    key.data.size = 8.0
