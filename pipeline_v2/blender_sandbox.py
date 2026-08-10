"""Generic CLI sandbox for executing an LLM-generated Blender script."""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

from .state import WorldState


class SandboxError(RuntimeError):
    """Raised when Blender execution or its output contract fails."""


def build_blender_command(
    blender_executable: Path | str,
    generated_script: Path | str,
    world_state_path: Path | str,
    output_dir: Path | str,
    *,
    render_style: str = "clay",
    resolution: tuple[int, int] = (640, 360),
    motion_bvh: Path | str | None = None,
    motion_bvh_alt: Path | str | None = None,
) -> list[str]:
    if render_style not in {"clay", "canonical", "skeleton", "storyhuman", "diagnostic"}:
        raise SandboxError("render_style must be clay, canonical, skeleton, storyhuman, or diagnostic")
    if len(resolution) != 2 or any(type(item) is not int or item <= 0 for item in resolution):
        raise SandboxError("resolution must contain two positive integers")
    command = [
        str(Path(blender_executable).resolve()),
        "--background",
        "--factory-startup",
        "--python",
        str(Path(generated_script).resolve()),
        "--",
        "--world-state",
        str(Path(world_state_path).resolve()),
        "--output-dir",
        str(Path(output_dir).resolve()),
        "--render-style",
        render_style,
        "--resolution",
        f"{resolution[0]}x{resolution[1]}",
    ]
    if motion_bvh is not None:
        command.extend(["--motion-bvh", str(Path(motion_bvh))])
    if motion_bvh_alt is not None:
        command.extend(["--motion-bvh-alt", str(Path(motion_bvh_alt))])
    return command


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_blender_sandbox(
    *,
    blender_executable: Path | str,
    generated_script: Path | str,
    world_state_path: Path | str,
    output_dir: Path | str,
    render_style: str = "clay",
    resolution: tuple[int, int] = (640, 360),
    timeout_seconds: int = 900,
    expected_world_state_hash: str | None = None,
    motion_bvh: Path | str | None = None,
    motion_bvh_alt: Path | str | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    command = build_blender_command(
        blender_executable,
        generated_script,
        world_state_path,
        output,
        render_style=render_style,
        resolution=resolution,
        motion_bvh=motion_bvh,
        motion_bvh_alt=motion_bvh_alt,
    )
    try:
        completed = runner(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        raise SandboxError(f"Blender timed out after {timeout_seconds}s") from exc
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    (output / "blender.stdout.log").write_text(stdout, encoding="utf-8")
    (output / "blender.stderr.log").write_text(stderr, encoding="utf-8")
    if completed.returncode != 0 or "PIPELINE_V2_BLENDER_OK" not in stdout + stderr:
        raise SandboxError(f"Blender returned {completed.returncode}; inspect logs")
    manifest_path = output / "render_manifest.json"
    if not manifest_path.is_file():
        raise SandboxError("render_manifest.json is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    world = WorldState.from_dict(json.loads(Path(world_state_path).read_text(encoding="utf-8")))
    expected_hash = expected_world_state_hash or world.world_state_hash()
    if manifest.get("schema_version") != "pipeline-v2-render-manifest-1.0":
        raise SandboxError("render manifest schema_version is invalid")
    if manifest.get("world_state_hash") != expected_hash:
        raise SandboxError("render manifest world_state_hash does not match input")
    if manifest.get("frame_count") != world.frame_count or manifest.get("fps") != world.fps:
        raise SandboxError("render manifest frame metadata does not match input WorldState")
    if manifest.get("resolution") != list(resolution):
        raise SandboxError("render manifest resolution does not match requested resolution")
    if not isinstance(manifest.get("videos"), list) or len(manifest["videos"]) != world.camera_count:
        raise SandboxError("render manifest has no videos")
    camera_ids = {camera["id"] for camera in world.to_dict()["camera_trajectory_plan"]["cameras"]}
    for video in manifest["videos"]:
        required = {"camera_id", "path", "sha256", "bytes", "frame_count", "fps", "resolution"}
        if not required.issubset(video):
            raise SandboxError("video manifest item is missing required metadata")
        if video["camera_id"] not in camera_ids:
            raise SandboxError(f"unknown camera in video manifest: {video['camera_id']}")
        if video["frame_count"] != world.frame_count or video["fps"] != world.fps or video["resolution"] != list(resolution):
            raise SandboxError(f"video metadata does not match WorldState: {video['camera_id']}")
        relative = Path(str(video["path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise SandboxError("video path must stay inside sandbox output directory")
        path = output / relative
        if not path.is_file() or path.stat().st_size == 0:
            raise SandboxError(f"missing or empty video: {path}")
        if video.get("sha256") != sha256_file(path):
            raise SandboxError(f"video hash mismatch: {path}")
    for log_name in ("state_log.json", "camera_log.json", "applied_state_log.json"):
        if not (output / log_name).is_file() or (output / log_name).stat().st_size == 0:
            raise SandboxError(f"missing required log: {log_name}")
    if manifest.get("couplings") and (not (output / "coupling_log.json").is_file() or (output / "coupling_log.json").stat().st_size == 0):
        raise SandboxError("manifest declares couplings but coupling_log.json is missing")
    blend_files = [path for path in output.glob("*.blend") if path.is_file() and path.stat().st_size > 0]
    if not blend_files:
        raise SandboxError("missing non-empty .blend preview artifact")
    return manifest
