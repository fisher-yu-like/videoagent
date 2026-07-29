from __future__ import annotations

import argparse
from dataclasses import dataclass
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from videoactagent.shotscript import ShotScript


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
    minimum_confidence: float = 1.25,
) -> str:
    if expected_motion != "truck_right":
        raise ValueError(f"unsupported camera motion verdict: {expected_motion}")
    if confidence < minimum_confidence:
        return "inconclusive"
    if abs(dx) < threshold_px:
        return "insufficient"
    return "matched" if dx < 0 else "opposite"


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


def evaluate_camera_motion(
    first_path: Path,
    middle_path: Path,
    last_path: Path,
    expected_motion: str,
    crop_fraction: float = 0.42,
    threshold_px: float = 5.0,
    minimum_confidence: float = 1.25,
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
    first_to_last_dx = measurements["first_to_last"]["dx"]
    return {
        "schema_version": "0.1",
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
        "verdict": classify_translation(
            first_to_last_dx,
            expected_motion,
            measurements["first_to_last"]["confidence"],
            threshold_px,
            minimum_confidence,
        ),
        "limitations": [
            "global background translation is not ground-truth camera pose",
            "actor motion, zoom, parallax, and generated scene changes can contaminate the estimate",
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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
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
    )
    result["shot_id"] = shot.shot_id
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(args.output)
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
