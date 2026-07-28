from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid


SENSITIVE_KEYS = {"authorization", "api_key", "access_key", "secret_key"}


def redact(value):
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in SENSITIVE_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


@dataclass(frozen=True)
class RunDirectory:
    path: Path

    @classmethod
    def create(cls, root: Path, backend: str):
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = root / f"{timestamp}_{backend}_{uuid.uuid4().hex[:8]}"
        path.mkdir(parents=True, exist_ok=False)
        return cls(path)

    def write_json(self, name: str, value: dict) -> Path:
        path = self.path / name
        path.write_text(
            json.dumps(redact(value), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path
