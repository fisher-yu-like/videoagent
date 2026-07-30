"""Offline whole-story suite validation, bundles, and completeness gates."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import imageio_ffmpeg
from PIL import Image, ImageDraw

from videoactagent.shotscript import ShotScript


class SuiteError(ValueError):
    """Raised when the checked-in whole-story experiment contract is invalid."""


@dataclass(frozen=True)
class StoryProfile:
    duration_seconds: float
    proxy_fps: int
    proxy_resolution: tuple[int, int]
    target_resolution: tuple[int, int]
    seed: None
    seed_support: str


@dataclass(frozen=True)
class StoryCase:
    story_id: str
    prompt_path: Path
    shotscript_path: Path
    prompt: str
    prompt_sha256: str
    submitted_prompt_sha256: str
    shotscript_sha256: str
    motion_signature: str
    shotscript: ShotScript


@dataclass(frozen=True)
class WholeStorySuite:
    workspace: Path
    config_path: Path
    config_sha256: str
    blender: Path
    output_dir: Path
    submit: bool
    max_api_calls: int
    profile: StoryProfile
    cases: tuple[StoryCase, ...]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _close_reader(reader: object, process: object | None = None) -> None:
    generator_frame = getattr(reader, "gi_frame", None)
    if process is None and generator_frame is not None:
        process = generator_frame.f_locals.get("process")
    try:
        reader.close()  # type: ignore[attr-defined]
    finally:
        if process is not None:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            for pipe_name in ("stdin", "stdout", "stderr"):
                pipe = getattr(process, pipe_name, None)
                if pipe is not None and not pipe.closed:
                    pipe.close()


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SuiteError(f"{name} must be an object")
    return value


def _resolution(value: object, name: str) -> tuple[int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in value)
    ):
        raise SuiteError(f"{name} must contain two positive integers")
    return int(value[0]), int(value[1])


def _workspace_path(workspace: Path, value: object, name: str) -> Path:
    if not isinstance(value, str) or not value:
        raise SuiteError(f"{name} must be a non-empty path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise SuiteError(f"{name} must stay inside the workspace")
    resolved = (workspace / relative).resolve(strict=False)
    if workspace != resolved and workspace not in resolved.parents:
        raise SuiteError(f"{name} must stay inside the workspace")
    return resolved


def _motion_signature(script: ShotScript) -> str:
    shot = script.shots[0]
    payload = {
        "camera": [shot.camera.start.as_list(), shot.camera.end.as_list()],
        "actors": [
            [actor.actor_id, actor.start.as_list(), actor.end.as_list()]
            for actor in sorted(shot.actors, key=lambda actor: actor.actor_id)
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def load_suite(path: Path | str, *, workspace: Path | str | None = None) -> WholeStorySuite:
    config_path = Path(path).resolve()
    root = Path(workspace).resolve() if workspace is not None else config_path.parent.parent
    try:
        document = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SuiteError(f"cannot read suite: {exc}") from exc
    data = _object(document, "suite")
    if data.get("schema_version") != "1.0":
        raise SuiteError("schema_version must be 1.0")

    submit = data.get("submit")
    max_api_calls = data.get("max_api_calls")
    if submit is not False or max_api_calls != 0:
        raise SuiteError("whole-story phase is offline: submit=false and max_api_calls=0 required")

    profile_data = _object(data.get("profile"), "profile")
    duration = profile_data.get("duration_seconds")
    fps = profile_data.get("proxy_fps")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration <= 0:
        raise SuiteError("profile.duration_seconds must be positive")
    if isinstance(fps, bool) or not isinstance(fps, int) or fps <= 0:
        raise SuiteError("profile.proxy_fps must be a positive integer")
    if profile_data.get("seed") is not None:
        raise SuiteError("profile.seed must be null while the gateways do not expose seed control")
    if profile_data.get("seed_support") != "unsupported_by_gateway":
        raise SuiteError("profile.seed_support must disclose unsupported_by_gateway")
    profile = StoryProfile(
        duration_seconds=float(duration),
        proxy_fps=fps,
        proxy_resolution=_resolution(profile_data.get("proxy_resolution"), "proxy_resolution"),
        target_resolution=_resolution(profile_data.get("target_resolution"), "target_resolution"),
        seed=None,
        seed_support="unsupported_by_gateway",
    )

    cases_value = data.get("cases")
    if not isinstance(cases_value, list) or len(cases_value) != 8:
        raise SuiteError("suite must contain exactly eight cases")
    cases: list[StoryCase] = []
    for index, item in enumerate(cases_value):
        case_data = _object(item, f"cases[{index}]")
        story_id = case_data.get("story_id")
        if not isinstance(story_id, str) or not story_id:
            raise SuiteError(f"cases[{index}].story_id must be non-empty")
        if re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", story_id) is None:
            raise SuiteError(f"cases[{index}].story_id must be a safe slug")
        prompt_path = _workspace_path(root, case_data.get("prompt"), "prompt")
        shotscript_path = _workspace_path(root, case_data.get("shotscript"), "shotscript")
        if not prompt_path.is_file() or not shotscript_path.is_file():
            raise SuiteError(f"{story_id} source file is missing")
        prompt = prompt_path.read_text(encoding="utf-8").strip()
        if not prompt:
            raise SuiteError(f"{story_id} prompt is empty")
        script = ShotScript.from_path(shotscript_path)
        if script.scene_id != story_id:
            raise SuiteError(f"{story_id} must equal ShotScript scene_id")
        if len(script.shots) != 1:
            raise SuiteError(f"{story_id} must be one continuous one-take")
        shot = script.shots[0]
        if not math.isclose(shot.duration, profile.duration_seconds, abs_tol=1e-9):
            raise SuiteError(f"{story_id} duration differs from suite profile")
        if script.fps != profile.proxy_fps:
            raise SuiteError(f"{story_id} fps differs from suite profile")
        cases.append(
            StoryCase(
                story_id=story_id,
                prompt_path=prompt_path,
                shotscript_path=shotscript_path,
                prompt=prompt,
                prompt_sha256=sha256_file(prompt_path),
                submitted_prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                shotscript_sha256=sha256_file(shotscript_path),
                motion_signature=_motion_signature(script),
                shotscript=script,
            )
        )

    for label, values in (
        ("story IDs", [case.story_id for case in cases]),
        ("prompts", [case.prompt_sha256 for case in cases]),
        ("ShotScripts", [case.shotscript_sha256 for case in cases]),
        ("motion signatures", [case.motion_signature for case in cases]),
    ):
        if len(set(values)) != len(values):
            raise SuiteError(f"suite contains duplicate {label}")

    blender_value = data.get("blender")
    if not isinstance(blender_value, str) or not blender_value:
        raise SuiteError("blender must be a non-empty path")
    output_dir = _workspace_path(root, data.get("output_dir"), "output_dir")
    return WholeStorySuite(
        workspace=root,
        config_path=config_path,
        config_sha256=sha256_file(config_path),
        blender=Path(blender_value),
        output_dir=output_dir,
        submit=False,
        max_api_calls=0,
        profile=profile,
        cases=tuple(cases),
    )


def build_story_bundles(
    suite: WholeStorySuite,
    case: StoryCase,
    proxy_path: Path,
    *,
    proxy_sha256: str,
) -> dict[str, dict[str, Any]]:
    common: dict[str, Any] = {
        "schema_version": "1.0",
        "story_id": case.story_id,
        "duration_seconds": suite.profile.duration_seconds,
        "prompt": case.prompt,
        "source_hashes": {
            "prompt_file_sha256": case.prompt_sha256,
            "submitted_prompt_sha256": case.submitted_prompt_sha256,
            "shotscript_sha256": case.shotscript_sha256,
        },
    }
    bundles: dict[str, dict[str, Any]] = {}
    for backend in ("kling", "seedance"):
        bundles[backend] = {
            **common,
            "backend": backend,
            "conditioning_mode": "prompt_only",
            "adapter_status": "offline_input_manifest_not_submission_payload",
            "target_resolution": list(suite.profile.target_resolution),
            "seed": suite.profile.seed,
            "seed_support": suite.profile.seed_support,
        }
    bundles["vace"] = {
        **common,
        "backend": "vace",
        "conditioning_mode": "source_video",
        "adapter_status": "offline_input_manifest_not_preprocess_job",
        "source_video": {
            "path": proxy_path.name,
            "path_base": "case_dir",
            "sha256": proxy_sha256,
        },
    }
    return bundles


def evaluate_output_completeness(
    *,
    requested_duration: float,
    decoded_duration: float,
    decoded_resolution: tuple[int, int],
    expected_resolution: tuple[int, int],
    key_times_decodable: bool,
    annotated_frame_count: int,
    interpolation_sample_count: int,
    threshold: float = 0.95,
    maximum_coverage: float = 1.05,
) -> dict[str, Any]:
    if requested_duration <= 0 or decoded_duration <= 0:
        raise ValueError("durations must be positive")
    coverage = decoded_duration / requested_duration
    media_complete = (
        threshold <= coverage <= maximum_coverage
        and tuple(decoded_resolution) == tuple(expected_resolution)
        and key_times_decodable
    )
    scoring_allowed = media_complete and annotated_frame_count > 0
    status = (
        "incomplete"
        if not media_complete
        else "complete" if scoring_allowed else "complete_unscored"
    )
    return {
        "status": status,
        "duration_coverage": coverage,
        "resolution_matches": tuple(decoded_resolution) == tuple(expected_resolution),
        "key_times_decodable": bool(key_times_decodable),
        "movement_scoring_allowed": scoring_allowed,
        "annotated_frame_count": int(annotated_frame_count),
        "interpolation_sample_count": int(interpolation_sample_count),
    }


def build_contact_sheet(
    inputs: list[tuple[str, Path]], output_path: Path | str, *, columns: int = 2
) -> dict[str, Any]:
    if not inputs:
        raise SuiteError("contact sheet requires at least one image")
    output = Path(output_path)
    cell_width, cell_height, label_height = 480, 270, 36
    columns = min(columns, len(inputs))
    rows = math.ceil(len(inputs) / columns)
    sheet = Image.new(
        "RGB", (cell_width * columns, (cell_height + label_height) * rows), "white"
    )
    draw = ImageDraw.Draw(sheet)
    for index, (story_id, source) in enumerate(inputs):
        with Image.open(source) as image:
            frame = image.convert("RGB").resize((cell_width, cell_height))
        x = (index % columns) * cell_width
        y = (index // columns) * (cell_height + label_height)
        sheet.paste(frame, (x, y))
        draw.text((x + 8, y + cell_height + 8), story_id, fill="black")
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output)
    return {"path": output.name, "sha256": sha256_file(output), "image_count": len(inputs)}


def _decode_proxy(
    video_path: Path, output_dir: Path, expected_frames: int
) -> tuple[int, float, tuple[int, int], float, dict[str, dict[str, Any]]]:
    selected = {0: "first", expected_frames // 2: "middle", expected_frames - 1: "last"}
    reader = imageio_ffmpeg.read_frames(str(video_path), pix_fmt="rgb24")
    records: dict[str, dict[str, Any]] = {}
    frame_count = 0
    process = None
    try:
        metadata = next(reader)
        generator_frame = getattr(reader, "gi_frame", None)
        process = (
            generator_frame.f_locals.get("process")
            if generator_frame is not None
            else None
        )
        size_value = metadata.get("size")
        fps_value = metadata.get("fps")
        if not isinstance(size_value, tuple) or len(size_value) != 2:
            raise SuiteError("proxy resolution is unavailable")
        size = int(size_value[0]), int(size_value[1])
        fps = float(fps_value)
        for index, frame in enumerate(reader):
            frame_count += 1
            name = selected.get(index)
            if name is None:
                continue
            frame_path = output_dir / f"{name}.png"
            Image.frombytes("RGB", size, frame).save(frame_path)
            records[name] = {
                "path": f"inspection/{frame_path.name}",
                "frame_index": index,
                "sha256": sha256_file(frame_path),
            }
    finally:
        _close_reader(reader, process)
    if set(records) != {"first", "middle", "last"}:
        raise SuiteError("proxy did not decode first, middle and last inspection frames")
    return frame_count, frame_count / fps, size, fps, records


def _validate_render_report(case: StoryCase, report_path: Path) -> dict[str, Any]:
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SuiteError(f"cannot read Blender scheduling report: {exc}") from exc
    reports = report.get("shots")
    if report.get("scene_id") != case.story_id or not isinstance(reports, list) or len(reports) != 1:
        raise SuiteError(f"{case.story_id} Blender scheduling report identity mismatch")
    actual = reports[0]
    shot = case.shotscript.shots[0]

    def close_vector(left: object, right: list[float]) -> bool:
        return (
            isinstance(left, list)
            and len(left) == 3
            and all(
                isinstance(value, (int, float))
                and math.isclose(float(value), expected, abs_tol=1e-4)
                for value, expected in zip(left, right)
            )
        )

    camera = actual.get("camera_positions", {})
    camera_matched = isinstance(camera, dict) and close_vector(
        camera.get("start"), shot.camera.start.as_list()
    ) and close_vector(camera.get("end"), shot.camera.end.as_list())
    actor_report = actual.get("actor_positions", {})
    rotations = actual.get("camera_rotations", {})
    camera_rotation_locked = isinstance(rotations, dict) and close_vector(
        rotations.get("start"), rotations.get("end")
        if isinstance(rotations.get("end"), list)
        else [math.inf, math.inf, math.inf]
    )
    actors_matched = isinstance(actor_report, dict) and set(actor_report) == {
        actor.actor_id for actor in shot.actors
    }
    if actors_matched:
        for actor in shot.actors:
            positions = actor_report.get(actor.actor_id)
            actors_matched = (
                isinstance(positions, dict)
                and close_vector(positions.get("start"), actor.start.as_list())
                and close_vector(positions.get("end"), actor.end.as_list())
            )
            if not actors_matched:
                break
    rotation_required = (
        shot.camera.motion == "static" and shot.camera.look_at == "fixed_actors_midpoint"
    )
    if not camera_matched or not actors_matched or (
        rotation_required and not camera_rotation_locked
    ):
        raise SuiteError(f"{case.story_id} Blender scheduling report differs from ShotScript")
    return {
        "status": "matched",
        "camera_positions_matched": True,
        "actor_positions_matched": True,
        "camera_rotation_locked": camera_rotation_locked,
        "report_path": report_path.name,
        "report_sha256": sha256_file(report_path),
    }


def render_story_case(
    suite: WholeStorySuite, case: StoryCase, output_dir: Path | str
) -> dict[str, Any]:
    case_dir = Path(output_dir).resolve()
    if case_dir.exists():
        raise SuiteError(f"case output already exists: {case_dir}")
    if not suite.blender.is_file():
        raise SuiteError(f"Blender executable is missing: {suite.blender}")
    case_dir.mkdir(parents=True)

    if sha256_file(case.prompt_path) != case.prompt_sha256 or sha256_file(
        case.shotscript_path
    ) != case.shotscript_sha256:
        raise SuiteError(f"{case.story_id} source changed after suite validation")
    source_dir = case_dir / "sources"
    source_dir.mkdir()
    prompt_snapshot = source_dir / "prompt.txt"
    shotscript_snapshot = source_dir / "shotscript.json"
    shutil.copyfile(case.prompt_path, prompt_snapshot)
    shutil.copyfile(case.shotscript_path, shotscript_snapshot)
    if sha256_file(prompt_snapshot) != case.prompt_sha256 or sha256_file(
        shotscript_snapshot
    ) != case.shotscript_sha256:
        raise SuiteError(f"{case.story_id} source snapshot hash mismatch")

    command = [
        sys.executable,
        "-m",
        "videoactagent.blender_runner",
        "--blender",
        str(suite.blender),
        "--shotscript",
        str(shotscript_snapshot),
        "--output-dir",
        str(case_dir),
    ]
    completed = subprocess.run(
        command,
        cwd=suite.workspace,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=240,
    )
    log = completed.stdout + completed.stderr
    (case_dir / "blender.log").write_text(log, encoding="utf-8")
    if completed.returncode != 0 or "BLENDER_PROXY_OK" not in log:
        raise SuiteError(f"real Blender render failed for {case.story_id}; see blender.log")

    legacy_blend = case_dir / "station_proxy.blend"
    legacy_video = case_dir / "station_proxy.mp4"
    proxy_blend = case_dir / "proxy.blend"
    proxy_video = case_dir / "proxy.mp4"
    render_report = case_dir / "trajectory_report.json"
    if not legacy_blend.is_file() or not legacy_video.is_file() or not render_report.is_file():
        raise SuiteError(f"real Blender outputs are incomplete for {case.story_id}")
    legacy_blend.replace(proxy_blend)
    legacy_video.replace(proxy_video)

    expected_frames = round(suite.profile.duration_seconds * suite.profile.proxy_fps)
    inspection_dir = case_dir / "inspection"
    inspection_dir.mkdir()
    frame_count, duration_seconds, size, fps, inspection = _decode_proxy(
        proxy_video, inspection_dir, expected_frames
    )
    if frame_count != expected_frames:
        raise SuiteError(
            f"{case.story_id} rendered {frame_count} frames; expected {expected_frames}"
        )
    if size != suite.profile.proxy_resolution or not math.isclose(
        fps, suite.profile.proxy_fps, abs_tol=1e-6
    ):
        raise SuiteError(f"{case.story_id} proxy media profile is incorrect")
    if not math.isclose(duration_seconds, suite.profile.duration_seconds, abs_tol=0.1):
        raise SuiteError(f"{case.story_id} proxy duration is incorrect")
    scheduling = _validate_render_report(case, render_report)

    proxy_sha256 = sha256_file(proxy_video)
    bundles = build_story_bundles(suite, case, proxy_video, proxy_sha256=proxy_sha256)
    bundle_dir = case_dir / "bundles"
    bundle_dir.mkdir()
    bundle_files: dict[str, dict[str, Any]] = {}
    for backend, bundle in bundles.items():
        bundle_path = bundle_dir / f"{backend}.json"
        bundle_path.write_text(
            json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        bundle_files[backend] = {
            "path": f"bundles/{bundle_path.name}",
            "sha256": sha256_file(bundle_path),
        }
    manifest = {
        "schema_version": "1.0",
        "story_id": case.story_id,
        "status": "media_complete",
        "source_hashes": {
            "prompt_file_sha256": case.prompt_sha256,
            "submitted_prompt_sha256": case.submitted_prompt_sha256,
            "shotscript_sha256": case.shotscript_sha256,
        },
        "media": {
            "path": proxy_video.name,
            "sha256": proxy_sha256,
            "frame_count": int(frame_count),
            "duration_seconds": float(duration_seconds),
            "fps": fps,
            "size": list(size),
        },
        "scheduling": scheduling,
        "inspection_frames": inspection,
        "bundles": bundles,
        "bundle_files": bundle_files,
    }
    manifest_path = case_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {"story_id": case.story_id, "case_dir": str(case_dir), "manifest": manifest}


def run_suite(suite: WholeStorySuite) -> dict[str, Any]:
    output_dir = suite.output_dir.resolve()
    if output_dir.exists():
        raise SuiteError(f"run output already exists: {output_dir}")
    output_dir.mkdir(parents=True)
    if sha256_file(suite.config_path) != suite.config_sha256:
        raise SuiteError("suite config changed after validation")
    run_sources = output_dir / "sources"
    run_sources.mkdir()
    config_snapshot = run_sources / "suite.json"
    shutil.copyfile(suite.config_path, config_snapshot)
    if sha256_file(config_snapshot) != suite.config_sha256:
        raise SuiteError("suite config snapshot hash mismatch")
    results = [
        render_story_case(suite, case, output_dir / case.story_id) for case in suite.cases
    ]
    proxy_hashes = [result["manifest"]["media"]["sha256"] for result in results]
    if len(set(proxy_hashes)) != len(proxy_hashes):
        raise SuiteError("rendered suite contains duplicate proxy videos")
    contact_sheet = build_contact_sheet(
        [
            (
                f"{result['story_id']}:{frame_name}",
                Path(result["case_dir"])
                / result["manifest"]["inspection_frames"][frame_name]["path"],
            )
            for result in results
            for frame_name in ("first", "middle", "last")
        ],
        output_dir / "contact_sheet.png",
        columns=3,
    )
    contact_sheet["case_count"] = len(results)
    summary = {
        "schema_version": "1.0",
        "status": "media_complete",
        "submit": False,
        "api_calls": 0,
        "server_inference_jobs": 0,
        "suite_config_sha256": suite.config_sha256,
        "case_count": len(results),
        "contact_sheet": contact_sheet,
        "cases": [
            {
                "story_id": result["story_id"],
                "manifest": str(
                    (Path(result["case_dir"]) / "manifest.json").relative_to(output_dir)
                ).replace("\\", "/"),
                "proxy_sha256": result["manifest"]["media"]["sha256"],
            }
            for result in results
        ],
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary
