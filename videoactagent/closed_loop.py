from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
from typing import Any
import uuid

from videoactagent.shotscript import ShotScript
from videoactagent.camera_eval import (
    STRICT_REFERENCE_MINIMUM_CONFIDENCE,
    decompose_translation_evidence,
)


ALLOWED_CAMERA_MOTIONS = {"truck_right", "dolly_in", "arc_clockwise"}
ALLOWED_VERDICTS = {"matched", "mismatched", "uncertain"}


class ClosedLoopError(ValueError):
    """Raised when closed-loop evidence cannot be bound to its source bytes."""


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _input_record(path: Path | str) -> dict[str, Any]:
    source = Path(path)
    if not source.is_file():
        raise ClosedLoopError(f"evidence file missing: {source}")
    return {
        "path": str(source.resolve()),
        "bytes": source.stat().st_size,
        "sha256": sha256_file(source),
    }


def _read_json(path: Path | str) -> dict[str, Any]:
    source = Path(path)
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ClosedLoopError(f"cannot read JSON evidence {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise ClosedLoopError(f"JSON evidence must be an object: {source}")
    return value


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ClosedLoopError(f"{label} must be a finite numeric value")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ClosedLoopError(f"{label} must be a finite numeric value") from exc
    if not math.isfinite(result):
        raise ClosedLoopError(f"{label} must be a finite numeric value")
    return result


def write_json_atomic(path: Path | str, value: dict[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _publish_record_group(
    output_dir: Path | str, records: dict[str, dict[str, Any]]
) -> None:
    """Publish one closed-loop generation without exposing mixed records."""

    destination = Path(output_dir)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and (destination.is_symlink() or not destination.is_dir()):
        raise ClosedLoopError("output directory must be a real directory")
    staging = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.staging"
    backup = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.backup"
    moved_previous = False
    published = False
    try:
        staging.mkdir()
        for name, value in records.items():
            if Path(name).name != name:
                raise ClosedLoopError(f"invalid output record name: {name}")
            path = staging / name
            with path.open("xb") as handle:
                handle.write(_json_bytes(value))
                handle.flush()
                os.fsync(handle.fileno())
        if os.name == "posix":
            directory_fd = os.open(staging, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        if destination.exists():
            os.replace(destination, backup)
            moved_previous = True
        try:
            os.replace(staging, destination)
            published = True
        except BaseException:
            if moved_previous and not destination.exists():
                os.replace(backup, destination)
                moved_previous = False
            raise
        if os.name == "posix":
            parent_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if published and backup.exists():
            shutil.rmtree(backup)


def _assert_output_excludes_inputs(output_dir: Path, inputs: tuple[Path, ...]) -> None:
    destination = output_dir.resolve(strict=False)
    for source in inputs:
        resolved_source = source.resolve(strict=True)
        if resolved_source.is_relative_to(destination):
            raise ClosedLoopError(
                f"output directory contains input evidence: {source}"
            )


def compile_expectation(shotscript: Path | str | ShotScript, shot_id: str) -> dict[str, Any]:
    if isinstance(shotscript, ShotScript):
        script = shotscript
        source_record = None
    else:
        source = Path(shotscript)
        script = ShotScript.from_path(source)
        source_record = _input_record(source)

    matches = [shot for shot in script.shots if shot.shot_id == shot_id]
    if len(matches) != 1:
        raise ClosedLoopError(f"expected exactly one shot {shot_id!r}, found {len(matches)}")
    shot = matches[0]
    if shot.camera.motion not in ALLOWED_CAMERA_MOTIONS:
        raise ClosedLoopError(f"unsupported camera motion: {shot.camera.motion}")

    result: dict[str, Any] = {
        "schema_version": "0.1",
        "scene_id": script.scene_id,
        "shot_id": shot.shot_id,
        "duration": shot.duration,
        "camera": {
            "shot_size": shot.camera.shot_size,
            "focal_length_mm": shot.camera.focal_length_mm,
            "motion": shot.camera.motion,
            "start": shot.camera.start.as_list(),
            "end": shot.camera.end.as_list(),
            "look_at": shot.camera.look_at,
        },
        "actors": [
            {
                "actor_id": actor.actor_id,
                "start": actor.start.as_list(),
                "end": actor.end.as_list(),
                "action": actor.action,
                "facing": actor.facing,
            }
            for actor in shot.actors
        ],
        "continuity": {
            "previous_shot": shot.continuity.previous_shot,
            "screen_direction": shot.continuity.screen_direction,
            "axis_side": shot.continuity.axis_side,
        },
    }
    if source_record is not None:
        result["shotscript"] = source_record
    return result


def _validate_frame_bindings(camera_report: dict[str, Any], inspection: dict[str, Any]) -> None:
    camera_inputs = camera_report.get("inputs")
    inspection_frames = inspection.get("frames")
    if not isinstance(camera_inputs, dict) or not isinstance(inspection_frames, dict):
        raise ClosedLoopError("camera report and inspection must contain frame records")
    for name in ("first", "middle", "last"):
        camera_frame = camera_inputs.get(name)
        inspected_frame = inspection_frames.get(name)
        if not isinstance(camera_frame, dict) or not isinstance(inspected_frame, dict):
            raise ClosedLoopError(f"missing {name} frame evidence")
        camera_hash = camera_frame.get("sha256")
        inspected_hash = inspected_frame.get("sha256")
        if camera_hash != inspected_hash:
            raise ClosedLoopError(f"{name} frame hash does not match camera report")
        frame_path = camera_frame.get("path")
        if not isinstance(frame_path, str) or sha256_file(frame_path) != camera_hash:
            raise ClosedLoopError(f"{name} frame hash does not match source bytes")


def build_feedback(
    expectation: dict[str, Any],
    video_path: Path | str,
    camera_report_path: Path | str,
    inspection_path: Path | str,
) -> dict[str, Any]:
    video = _input_record(video_path)
    camera_input = _input_record(camera_report_path)
    inspection_input = _input_record(inspection_path)
    camera_report = _read_json(camera_report_path)
    inspection = _read_json(inspection_path)
    shot_id = expectation.get("shot_id")
    if camera_report.get("shot_id") != shot_id or inspection.get("shot_id") != shot_id:
        raise ClosedLoopError("shot id does not match expectation")
    if inspection.get("video_sha256") != video["sha256"]:
        raise ClosedLoopError("inspection video hash does not match source video")
    if inspection.get("source") != "manual_visual_inspection":
        raise ClosedLoopError("inspection source must be manual_visual_inspection")
    if camera_report.get("expected_motion") != expectation.get("camera", {}).get("motion"):
        raise ClosedLoopError("camera report motion does not match expectation")
    heuristic_verdict = camera_report.get("verdict")
    if heuristic_verdict not in {"matched", "opposite", "insufficient", "inconclusive"}:
        raise ClosedLoopError(f"unknown automatic camera verdict: {heuristic_verdict!r}")
    strict_reference = camera_report.get("strict_reference")
    if not isinstance(strict_reference, dict):
        raise ClosedLoopError("camera report must contain a strict_reference")
    if strict_reference.get("minimum_confidence") != STRICT_REFERENCE_MINIMUM_CONFIDENCE:
        raise ClosedLoopError("strict camera minimum confidence is not fixed at 1.25")
    camera_verdict = strict_reference.get("verdict")
    if camera_verdict not in {"matched", "opposite", "insufficient", "inconclusive"}:
        raise ClosedLoopError(f"unknown strict camera verdict: {camera_verdict!r}")
    measurements = camera_report.get("measurements")
    strict_evidence = strict_reference.get("directional_evidence")
    if not isinstance(measurements, dict) or not isinstance(strict_evidence, dict):
        raise ClosedLoopError("strict camera evidence is incomplete")
    threshold_px = _finite_number(
        camera_report.get("threshold_px", 5.0), "camera threshold_px"
    )
    for name in ("first_to_middle", "middle_to_last", "first_to_last"):
        measurement = measurements.get(name)
        if not isinstance(measurement, dict):
            raise ClosedLoopError(f"strict camera measurement missing: {name}")
        dx = _finite_number(measurement.get("dx"), f"camera {name}.dx")
        _finite_number(measurement.get("dy"), f"camera {name}.dy")
        confidence = _finite_number(
            measurement.get("confidence"), f"camera {name}.confidence"
        )
        try:
            expected_strict = decompose_translation_evidence(
                dx=dx,
                expected_motion=camera_report.get("expected_motion"),
                confidence=confidence,
                threshold_px=threshold_px,
                minimum_confidence=STRICT_REFERENCE_MINIMUM_CONFIDENCE,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ClosedLoopError(f"strict camera evidence is invalid: {exc}") from exc
        if strict_evidence.get(name) != expected_strict:
            raise ClosedLoopError(f"strict camera directional evidence mismatch: {name}")
    if camera_verdict != strict_evidence["first_to_last"]["overall_verdict"]:
        raise ClosedLoopError("strict camera verdict does not match first-to-last evidence")
    _validate_frame_bindings(camera_report, inspection)

    observations = inspection.get("observations")
    if not isinstance(observations, dict):
        raise ClosedLoopError("inspection observations must be an object")
    required = ("actor_a_action", "actor_b_action", "actors_facing", "screen_direction")
    for name in required:
        if observations.get(name) not in ALLOWED_VERDICTS:
            raise ClosedLoopError(f"unknown manual inspection verdict for {name}")

    return {
        "schema_version": "0.1",
        "shot_id": shot_id,
        "video": video,
        "camera_report": camera_input,
        "inspection": inspection_input,
        "automatic_camera": {
            "source": "stage4_camera_eval",
            "verdict": camera_verdict,
            "acceptance_basis": "strict_reference",
            "heuristic_verdict": heuristic_verdict,
            "evidence_type": camera_report.get("evidence_type"),
            "measurements": camera_report.get("measurements"),
            "strict_reference": strict_reference,
            "limitations": camera_report.get("limitations", []),
        },
        "manual_visual_inspection": {
            **{name: observations[name] for name in required},
            "source": "manual_visual_inspection",
            "frames": inspection["frames"],
            "notes": inspection.get("notes", []),
        },
    }


def propose_revision(feedback: dict[str, Any]) -> dict[str, Any]:
    camera = feedback.get("automatic_camera", {}).get("verdict")
    manual = feedback.get("manual_visual_inspection", {})
    operations: list[dict[str, str]] = []
    if camera == "inconclusive":
        operations.extend(
            [
                {"op": "enable_structural_proxy", "channel": "vace_src_video"},
                {"op": "preserve_camera_trajectory", "source": "shotscript"},
            ]
        )
    elif camera not in {"matched", "opposite", "insufficient"}:
        raise ClosedLoopError(f"unknown camera revision verdict: {camera!r}")

    for key, actor_id in (("actor_a_action", "actor_a"), ("actor_b_action", "actor_b")):
        verdict = manual.get(key)
        if verdict not in ALLOWED_VERDICTS:
            raise ClosedLoopError(f"unknown manual revision verdict for {key}")
        if verdict == "mismatched":
            operations.append({"op": "strengthen_timed_action", "actor_id": actor_id})
    facing = manual.get("actors_facing")
    screen_direction = manual.get("screen_direction")
    if facing not in ALLOWED_VERDICTS or screen_direction not in ALLOWED_VERDICTS:
        raise ClosedLoopError("unknown continuity revision verdict")
    if screen_direction == "mismatched":
        operations.extend(
            [
                {"op": "bind_previous_last_frame", "source": "previous_shot"},
                {"op": "preserve_action_axis", "source": "shotscript"},
            ]
        )

    return {
        "schema_version": "0.1",
        "shot_id": feedback.get("shot_id"),
        "source_feedback_sha256": feedback.get("feedback_sha256"),
        "operations": operations,
        "policy": "fixed_bounded_operations",
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate one real generated video against a ShotScript offline."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    evaluate = subparsers.add_parser("evaluate", help="write expectation, feedback, and revision records")
    evaluate.add_argument("--shotscript", type=Path, required=True)
    evaluate.add_argument("--shot", required=True)
    evaluate.add_argument("--video", type=Path, required=True)
    evaluate.add_argument("--camera-report", type=Path, required=True)
    evaluate.add_argument("--inspection", type=Path, required=True)
    evaluate.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _assert_output_excludes_inputs(
        args.output_dir,
        (args.shotscript, args.video, args.camera_report, args.inspection),
    )
    expectation = compile_expectation(args.shotscript, args.shot)
    feedback = build_feedback(expectation, args.video, args.camera_report, args.inspection)
    feedback["expectation_sha256"] = hashlib.sha256(_json_bytes(expectation)).hexdigest()

    revision_input = dict(feedback)
    revision_input["feedback_sha256"] = hashlib.sha256(_json_bytes(feedback)).hexdigest()
    revision = propose_revision(revision_input)
    _publish_record_group(
        args.output_dir,
        {
            "expectation.json": expectation,
            "feedback.json": feedback,
            "revision.json": revision,
        },
    )
    revision_path = args.output_dir / "revision.json"

    print(
        "CLOSED_LOOP_EVALUATED "
        + json.dumps(
            {
                "shot_id": args.shot,
                "camera_verdict": feedback["automatic_camera"]["verdict"],
                "output_dir": str(args.output_dir.resolve()),
                "revision_sha256": sha256_file(revision_path),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
