from __future__ import annotations

import argparse
from dataclasses import dataclass
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

import numpy as np
from PIL import Image


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from videoactagent.shotscript import ShotScript


DEFAULT_MINIMUM_CONFIDENCE = 1.05
STRICT_REFERENCE_MINIMUM_CONFIDENCE = 1.25


@dataclass(frozen=True)
class Translation:
    dx: float
    dy: float
    confidence: float


def estimate_translation(reference: np.ndarray, observed: np.ndarray) -> Translation:
    reference = np.asarray(reference, dtype=np.float64)
    observed = np.asarray(observed, dtype=np.float64)
    if reference.ndim != 2 or observed.ndim != 2:
        raise ValueError("phase correlation inputs must be 2D grayscale arrays")
    if reference.shape != observed.shape:
        raise ValueError("phase correlation inputs must have equal dimensions")

    height, width = reference.shape
    window = np.outer(np.hanning(height), np.hanning(width))
    reference_windowed = (reference - reference.mean()) * window
    observed_windowed = (observed - observed.mean()) * window
    reference_fft = np.fft.fft2(reference_windowed)
    observed_fft = np.fft.fft2(observed_windowed)
    cross_power = observed_fft * np.conj(reference_fft)
    magnitude = np.abs(cross_power)
    cross_power /= np.maximum(magnitude, np.finfo(np.float64).eps)
    correlation = np.abs(np.fft.ifft2(cross_power))

    peak_y, peak_x = np.unravel_index(np.argmax(correlation), correlation.shape)
    dy = float(peak_y if peak_y <= height // 2 else peak_y - height)
    dx = float(peak_x if peak_x <= width // 2 else peak_x - width)
    flattened = correlation.ravel()
    if flattened.size > 1:
        top_two = np.partition(flattened, -2)[-2:]
        confidence = float(top_two.max() / max(top_two.min(), 1e-12))
    else:
        confidence = 1.0
    return Translation(dx=dx, dy=dy, confidence=confidence)


def classify_translation(
    dx: float,
    expected_motion: str,
    confidence: float,
    threshold_px: float = 5.0,
    minimum_confidence: float = DEFAULT_MINIMUM_CONFIDENCE,
) -> str:
    return decompose_translation_evidence(
        dx=dx,
        expected_motion=expected_motion,
        confidence=confidence,
        threshold_px=threshold_px,
        minimum_confidence=minimum_confidence,
    )["overall_verdict"]


def _observed_background_direction(dx: float, threshold_px: float) -> str:
    if dx < -threshold_px:
        return "negative"
    if dx > threshold_px:
        return "positive"
    return "near_zero"


def _classify_direction(dx: float, expected_motion: str, threshold_px: float) -> str:
    if expected_motion != "truck_right":
        raise ValueError(f"unsupported camera motion verdict: {expected_motion}")
    if abs(dx) <= threshold_px:
        return "insufficient"
    return "matched" if dx < 0 else "opposite"


def decompose_translation_evidence(
    dx: float,
    expected_motion: str,
    confidence: float,
    threshold_px: float = 5.0,
    minimum_confidence: float = DEFAULT_MINIMUM_CONFIDENCE,
) -> dict:
    """Separate observed direction from the confidence gate.

    Direction is only a statement about the measured background translation.
    The overall verdict remains inconclusive unless the correlation confidence
    clears the selected heuristic minimum. This is not a camera-pose estimate.
    """
    if not np.isfinite(minimum_confidence) or minimum_confidence <= 1.0:
        raise ValueError(
            "minimum_confidence must be finite and greater than 1.0"
        )
    direction_verdict = _classify_direction(dx, expected_motion, threshold_px)
    confidence_gate_passed = bool(
        np.isfinite(confidence) and confidence >= minimum_confidence
    )
    confidence_verdict = "passed" if confidence_gate_passed else "below_minimum"
    overall_verdict = direction_verdict if confidence_gate_passed else "inconclusive"
    return {
        "observed_background_direction": _observed_background_direction(
            dx, threshold_px
        ),
        "direction_verdict": direction_verdict,
        "confidence_gate_passed": confidence_gate_passed,
        "confidence_verdict": confidence_verdict,
        "overall_verdict": overall_verdict,
    }


def _input_record(path: Path) -> dict:
    contents = path.read_bytes()
    return {
        "path": str(path.resolve()),
        "bytes": len(contents),
        "sha256": hashlib.sha256(contents).hexdigest(),
    }


def _load_grayscale(path: Path) -> tuple[np.ndarray, tuple[int, int]]:
    with Image.open(path) as image:
        size = image.size
        grayscale = np.asarray(image.convert("L"), dtype=np.float64)
    return grayscale, size


def _write_json_atomic(path: Path, value: dict) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    temporary = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
        if os.name == "posix":
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _assert_output_not_alias_inputs(output: Path, inputs: tuple[Path, ...]) -> None:
    destination = output.resolve(strict=False)
    if output.is_symlink():
        raise ValueError("camera evaluation output must not be a symlink")
    for source in inputs:
        resolved_source = source.resolve(strict=True)
        same_identity = False
        if output.exists():
            try:
                same_identity = os.path.samefile(output, source)
            except OSError:
                same_identity = False
        if destination == resolved_source or same_identity:
            raise ValueError(f"output must not alias input: {source}")


def evaluate_camera_motion(
    first_path: Path,
    middle_path: Path,
    last_path: Path,
    expected_motion: str,
    crop_fraction: float = 0.42,
    threshold_px: float = 5.0,
    minimum_confidence: float = DEFAULT_MINIMUM_CONFIDENCE,
) -> dict:
    paths = {
        "first": Path(first_path),
        "middle": Path(middle_path),
        "last": Path(last_path),
    }
    loaded = {name: _load_grayscale(path) for name, path in paths.items()}
    sizes = {size for _, size in loaded.values()}
    if len(sizes) != 1:
        raise ValueError("evaluation frames must have identical dimensions")
    width, height = sizes.pop()
    crop_height = int(round(height * crop_fraction))
    if crop_height < 8 or crop_height > height:
        raise ValueError("crop_fraction produces an invalid background crop")
    crops = {
        name: array[:crop_height, :]
        for name, (array, _size) in loaded.items()
    }
    measurements = {
        "first_to_middle": asdict(
            estimate_translation(crops["first"], crops["middle"])
        ),
        "middle_to_last": asdict(
            estimate_translation(crops["middle"], crops["last"])
        ),
        "first_to_last": asdict(
            estimate_translation(crops["first"], crops["last"])
        ),
    }
    directional_evidence = {
        name: decompose_translation_evidence(
            dx=measurement["dx"],
            expected_motion=expected_motion,
            confidence=measurement["confidence"],
            threshold_px=threshold_px,
            minimum_confidence=minimum_confidence,
        )
        for name, measurement in measurements.items()
    }
    strict_directional_evidence = {
        name: decompose_translation_evidence(
            dx=measurement["dx"],
            expected_motion=expected_motion,
            confidence=measurement["confidence"],
            threshold_px=threshold_px,
            minimum_confidence=STRICT_REFERENCE_MINIMUM_CONFIDENCE,
        )
        for name, measurement in measurements.items()
    }
    return {
        "schema_version": "0.2",
        "evidence_type": "heuristic_phase_correlation",
        "expected_motion": expected_motion,
        "expected_background_dx": "negative",
        "threshold_px": threshold_px,
        "minimum_confidence": minimum_confidence,
        "crop_fraction": crop_fraction,
        "dimensions": [width, height],
        "crop_bounds": [0, 0, width, crop_height],
        "inputs": {name: _input_record(path) for name, path in paths.items()},
        "measurements": measurements,
        "directional_evidence": directional_evidence,
        "verdict": directional_evidence["first_to_last"]["overall_verdict"],
        "strict_reference": {
            "minimum_confidence": STRICT_REFERENCE_MINIMUM_CONFIDENCE,
            "directional_evidence": strict_directional_evidence,
            "verdict": strict_directional_evidence["first_to_last"][
                "overall_verdict"
            ],
        },
        "limitations": [
            "global background translation is not ground-truth camera pose",
            "actor motion, zoom, parallax, and generated scene changes can contaminate the estimate",
            "a heuristic matched verdict does not mean the model or camera control improved",
        ],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--first", type=Path, required=True)
    parser.add_argument("--middle", type=Path, required=True)
    parser.add_argument("--last", type=Path, required=True)
    parser.add_argument("--shotscript", type=Path, required=True)
    parser.add_argument("--shot", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--crop-fraction", type=float, default=0.42)
    parser.add_argument(
        "--minimum-confidence",
        type=float,
        default=DEFAULT_MINIMUM_CONFIDENCE,
        help=(
            "heuristic phase-correlation confidence gate; must be finite and "
            "greater than 1.0 (default: 1.05; strict reference: 1.25)"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    _assert_output_not_alias_inputs(
        args.output,
        (args.first, args.middle, args.last, args.shotscript),
    )
    script = ShotScript.from_path(args.shotscript)
    try:
        shot = next(shot for shot in script.shots if shot.shot_id == args.shot)
    except StopIteration as exc:
        raise ValueError(f"shot not found: {args.shot}") from exc
    result = evaluate_camera_motion(
        args.first,
        args.middle,
        args.last,
        shot.camera.motion,
        crop_fraction=args.crop_fraction,
        minimum_confidence=args.minimum_confidence,
    )
    result["shot_id"] = shot.shot_id
    _write_json_atomic(args.output, result)
    reread = json.loads(args.output.read_text(encoding="utf-8"))
    if reread.get("shot_id") != shot.shot_id:
        raise RuntimeError("camera evaluation output verification failed")
    print(
        "CAMERA_EVAL_OK",
        json.dumps(
            {"output": str(args.output), "verdict": result["verdict"]},
            ensure_ascii=False,
        ),
    )


if __name__ == "__main__":
    main()
