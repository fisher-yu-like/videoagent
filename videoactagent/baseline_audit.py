from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def _git_head(checkout: Path) -> str | None:
    if not (checkout / ".git").exists():
        return None
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=checkout,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _file_record(path: Path) -> dict:
    contents = path.read_bytes()
    return {
        "exists": True,
        "is_file": True,
        "bytes": len(contents),
        "sha256": hashlib.sha256(contents).hexdigest(),
    }


def audit_baselines(manifest_path: Path, third_party: Path) -> dict:
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    records = []
    all_verified = True
    for baseline in manifest["baselines"]:
        checkout = Path(third_party) / baseline["checkout"]
        head = _git_head(checkout)
        required = {
            name: (checkout / name).exists()
            for name in baseline["required_files"]
        }
        hashed = {}
        hash_files_verified = True
        for name in baseline["hash_files"]:
            path = checkout / name
            if path.is_file():
                hashed[name] = _file_record(path)
            else:
                hashed[name] = {
                    "exists": path.exists(),
                    "is_file": False,
                    "bytes": None,
                    "sha256": None,
                }
                hash_files_verified = False
        commit_match = head == baseline["commit"]
        checkout_verified = (
            checkout.is_dir()
            and commit_match
            and all(required.values())
            and hash_files_verified
        )
        all_verified = all_verified and checkout_verified
        records.append(
            {
                "name": baseline["name"],
                "role": baseline["role"],
                "repository": baseline["repository"],
                "expected_commit": baseline["commit"],
                "actual_commit": head,
                "checkout_exists": checkout.is_dir(),
                "commit_match": commit_match,
                "required_files": required,
                "hashed_files": hashed,
            }
        )
    return {
        "schema_version": "0.1",
        "status": "verified_source_only" if all_verified else "source_incomplete",
        "weights_downloaded": False,
        "inference_verified": False,
        "baselines": records,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--third-party", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    report = audit_baselines(args.manifest, args.third_party)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(args.output)
    print(
        "BASELINE_AUDIT_OK",
        json.dumps({"status": report["status"], "output": str(args.output)}),
    )


if __name__ == "__main__":
    main()
