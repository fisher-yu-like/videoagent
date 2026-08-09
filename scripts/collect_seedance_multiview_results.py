"""Collect and verify the completed real Seedance multiview jobs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


JOBS = (
    ("G1", "runs/results/seedance_multiview_e2e_20260809_03/G1_multiview"),
    ("G2", "runs/results/seedance_multiview_e2e_20260809_05/G2_multiview"),
    ("G3", "runs/results/seedance_multiview_e2e_20260809_06/G3_multiview"),
    ("G4", "runs/results/seedance_multiview_e2e_20260809_07/G4_multiview"),
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe(path: Path, ffprobe: str) -> dict[str, object]:
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration,size:stream=width,height,avg_frame_rate,nb_frames,codec_name",
        "-of",
        "json",
        str(path),
    ]
    document = json.loads(subprocess.check_output(command, text=True))
    stream = document["streams"][0]
    return {
        "duration": float(document["format"]["duration"]),
        "bytes": int(document["format"]["size"]),
        "codec": stream["codec_name"],
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "fps": stream["avg_frame_rate"],
        "frames": int(stream["nb_frames"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ffprobe", default=r"D:\ACLOS\Cross\recorder-release\ffprobe.exe")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    proxy_dir = output / "proxy"
    prompt_dir = output / "prompts"
    video_dir = output / "videos"
    evidence_root = output / "evidence"
    for directory in (proxy_dir, prompt_dir, video_dir, evidence_root):
        directory.mkdir(exist_ok=True)

    source_proxy = Path("runs/results/seedance_multiview_e2e_20260809_02/normalized")
    proxy_records = []
    for index in range(1, 4):
        candidates = sorted(source_proxy.glob(f"camera_{index}_seedance_v2.mp4"))
        if not candidates:
            raise SystemExit(f"missing normalized proxy: camera_{index}")
        target = proxy_dir / f"camera_{index}.mp4"
        shutil.copy2(candidates[0], target)
        proxy_records.append({"path": str(target), "sha256": sha256(target)})

    jobs = []
    for label, raw_job_dir in JOBS:
        job_dir = Path(raw_job_dir).resolve()
        evidence_dir = evidence_root / f"{label}_multiview"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        for source in job_dir.iterdir():
            if source.is_file() and source.suffix.lower() != ".mp4":
                shutil.copy2(source, evidence_dir / source.name)
        request = json.loads((job_dir / "request.json").read_text(encoding="utf-8"))
        payload = request["payload"]
        prompt = next(item["text"] for item in payload["content"] if item.get("type") == "text")
        prompt_path = prompt_dir / f"{label}.txt"
        prompt_path.write_text(prompt + "\n", encoding="utf-8")
        source_result = job_dir / "result.mp4"
        if not source_result.is_file():
            raise SystemExit(f"missing completed result: {source_result}")
        result_path = video_dir / f"{label}.mp4"
        shutil.copy2(source_result, result_path)
        state = json.loads((job_dir / "state.json").read_text(encoding="utf-8"))
        urls = [
            item["video_url"]["url"]
            for item in payload["content"]
            if item.get("type") == "video_url"
        ]
        jobs.append(
            {
                "label": label,
                "job_dir": str(evidence_dir),
                "task_id": state["task_id"],
                "model": payload["model"],
                "prompt_path": str(prompt_path),
                "prompt_sha256": sha256(prompt_path),
                "input_reference_urls": urls,
                "output": {
                    "path": str(result_path),
                    "sha256": sha256(result_path),
                    "media": probe(result_path, args.ffprobe),
                },
                "api_calls": {"submit": 1, "query": 31, "download": 1},
            }
        )

    manifest = {
        "schema_version": "seedance-multiview-e2e-result-1.0",
        "model": "Doubao-Seedance-2.0",
        "proxy": proxy_records,
        "jobs": jobs,
        "total_api_calls": {
            "submit": sum(item["api_calls"]["submit"] for item in jobs),
            "query": sum(item["api_calls"]["query"] for item in jobs),
            "download": sum(item["api_calls"]["download"] for item in jobs),
        },
        "upload_provider": "tmpfiles (temporary research transport; TOS not configured)",
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# Seedance 多视角真实结果",
        "",
        "- 模型：Doubao-Seedance-2.0",
        "- 输入：同一 Blender shared-world 的三个 camera Proxy；每个任务提交三条 reference_video。",
        "- 结果：4 个不同 Prompt，均保存原始 MP4、Prompt、任务 ID、URL、SHA-256 和 ffprobe 元数据。",
        "- 上传：临时 tmpfiles，仅用于本次研究；生产运行应切换到 TOS。",
        "",
        "| 任务 | 视频 | 时长 | 分辨率 | 帧率 | 帧数 | 输出 SHA-256 |",
        "|---|---|---:|---|---:|---:|---|",
    ]
    for item in jobs:
        media = item["output"]["media"]
        lines.append(
            f"| {item['label']} | `{item['output']['path']}` | {media['duration']:.3f}s | "
            f"{media['width']}x{media['height']} | {media['fps']} | {media['frames']} | "
            f"`{item['output']['sha256']}` |"
        )
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
