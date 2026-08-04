"""Trusted Blender-side executor for validated scene code.

Only this entrypoint owns filesystem access and rendering. Model-generated code is
limited to ``build_scene(context)`` by :mod:`videoactagent.codegen_safety`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import runpy
import sys
from typing import Any

def frame_for_time(start: int, end: int, time: float) -> int:
    """Map normalized shot time to an inclusive Blender frame range."""

    return start + int(round(float(time) * (end - start)))


def world_xy(bounds: list[float], x: float, y: float) -> tuple[float, float]:
    """Map top-left normalized preview coordinates into world X/Y."""

    min_x, max_x, min_y, max_y = bounds
    return min_x + float(x) * (max_x - min_x), max_y - float(y) * (max_y - min_y)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse(argv: list[str] | None) -> argparse.Namespace:
    if argv is None:
        raw = sys.argv[1:]
        if "--" in raw:
            raw = raw[raw.index("--") + 1 :]
    else:
        raw = argv
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--code", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-input-sha256", required=True)
    parser.add_argument("--expected-code-sha256", required=True)
    return parser.parse_args(raw)


def _vector(value: Any) -> list[float]:
    return [round(float(value[i]), 6) for i in range(3)]


def main(argv: list[str] | None = None) -> int:
    """Load the validated code, render real evidence and print a success marker."""

    try:
        args = _parse(argv)
        input_path = args.input.resolve()
        code_path = args.code.resolve()
        output = args.output_dir.resolve()
        if not input_path.is_file() or not code_path.is_file():
            raise ValueError("input and generated code must be files")
        input_hash = _sha256(input_path)
        code_hash = _sha256(code_path)
        if input_hash != args.expected_input_sha256:
            raise ValueError("input SHA-256 does not match the runner binding")
        if code_hash != args.expected_code_sha256:
            raise ValueError("generated code SHA-256 does not match the runner binding")
        code = code_path.read_text(encoding="utf-8")
        document = json.loads(input_path.read_text(encoding="utf-8"))
        contract = document["render_contract"]
        fps = int(contract["fps"])
        width, height = (int(contract["resolution"][0]), int(contract["resolution"][1]))
        frame_start, frame_end = int(contract["frame_start"]), int(contract["frame_end"])
        if frame_end < frame_start:
            raise ValueError("invalid render frame range")
        actors = document["shotscript"]["shots"][0]["actors"]
        actor_ids = [actor["id"] for actor in actors]
        trajectory_by_actor = {track["target"]["id"]: track for track in document["trajectory"]["tracks"]}

        # Blender is imported only in the Blender process; ordinary unit tests stay
        # importable with the host Python interpreter.
        import bpy  # type: ignore

        # Do not call read_factory_settings here: it resets the ``-F FFMPEG``
        # output selection in current Blender releases. Clear the startup scene
        # in-place while retaining the process-level movie encoder setting.
        bpy.ops.object.select_all(action="SELECT")
        bpy.ops.object.delete(use_global=False)
        for datablocks in (bpy.data.meshes, bpy.data.curves, bpy.data.materials, bpy.data.cameras, bpy.data.lights):
            for datablock in list(datablocks):
                if datablock.users == 0:
                    datablocks.remove(datablock)
        scene = bpy.context.scene
        scene.frame_start = frame_start
        scene.frame_end = frame_end
        scene.render.fps = fps
        scene.render.resolution_x = width
        scene.render.resolution_y = height
        scene.render.resolution_percentage = 100
        if hasattr(scene.render, "engine"):
            try:
                scene.render.engine = "BLENDER_EEVEE_NEXT"
            except Exception:
                pass
        namespace = runpy.run_path(str(code_path), run_name="__codegen_scene__")
        build_scene = namespace.get("build_scene")
        if not callable(build_scene):
            raise ValueError("generated code did not expose build_scene")
        context = {
            "input": document,
            "scene": scene,
            "world_xy": world_xy,
            "frame_for_time": frame_for_time,
        }
        build_scene(context)
        for actor_id in actor_ids:
            if bpy.data.objects.get(actor_id) is None:
                raise ValueError(f"generated scene is missing actor object: {actor_id}")
        camera = bpy.data.objects.get("DirectorCamera")
        if camera is None or camera.type != "CAMERA":
            raise ValueError("generated scene is missing camera DirectorCamera")
        scene.camera = camera
        output.mkdir(parents=True, exist_ok=False)
        frames_dir = output / "frames"
        frames_dir.mkdir()

        # Render contract is enforced here, after model code has run.
        video_path = output / "video.mp4"
        blend_path = output / "scene.blend"
        if scene.render.image_settings.file_format != "FFMPEG":
            raise ValueError("trusted runner must launch Blender with FFMPEG output")
        scene.render.ffmpeg.format = "MPEG4"
        scene.render.ffmpeg.codec = "H264"
        scene.render.filepath = str(video_path)
        scene.frame_set(frame_start)
        bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))
        bpy.ops.render.render(animation=True)

        samples = [(frame_start, "first"), ((frame_start + frame_end) // 2, "middle"), (frame_end, "last")]
        frame_records: dict[str, dict[str, Any]] = {}
        for frame, name in samples:
            scene.frame_set(frame)
            image_path = frames_dir / f"{name}.png"
            bpy.ops.render.render()
            render_result = bpy.data.images.get("Render Result")
            if render_result is None:
                raise ValueError("Blender did not produce a Render Result for keyframe")
            render_result.save_render(filepath=str(image_path), scene=scene)
            frame_records[name] = {"path": str(image_path.relative_to(output)).replace("\\", "/"), "sha256": _sha256(image_path), "bytes": image_path.stat().st_size}

        transforms: dict[str, dict[str, Any]] = {}
        for actor_id in actor_ids:
            actor = bpy.data.objects[actor_id]
            values: dict[str, Any] = {}
            for keyframe_id in ("K0", "K2", "K4"):
                index = int(keyframe_id[1])
                point = trajectory_by_actor[actor_id]["points"][index]
                frame = frame_for_time(frame_start, frame_end, point["t"])
                scene.frame_set(frame)
                bpy.context.view_layer.update()
                values[keyframe_id] = {"frame": frame, "expected_world": list(point["world"]), "observed": _vector(actor.matrix_world.translation)}
            transforms[actor_id] = values

        required = [video_path, blend_path, *(frames_dir / f"{name}.png" for _frame, name in samples)]
        missing = [str(path) for path in required if not path.is_file() or path.stat().st_size <= 0]
        if missing:
            raise ValueError(f"render outputs are missing or empty: {missing}")
        manifest = {
            "schema_version": "blender-codegen-manifest-1.0",
            "input_sha256": input_hash,
            "code_sha256": code_hash,
            "blender_version": getattr(bpy.app, "version_string", "unknown"),
            "frame_start": frame_start,
            "frame_end": frame_end,
            "fps": fps,
            "resolution": [width, height],
            "duration_seconds": (frame_end - frame_start + 1) / fps,
            "video": {"path": video_path.name, "sha256": _sha256(video_path), "bytes": video_path.stat().st_size},
            "blend": {"path": blend_path.name, "sha256": _sha256(blend_path), "bytes": blend_path.stat().st_size},
            "frames": frame_records,
            "actor_transforms": transforms,
        }
        (output / "codegen_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print("BLENDER_CODEGEN_OK=" + json.dumps(manifest, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"BLENDER_CODEGEN_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
