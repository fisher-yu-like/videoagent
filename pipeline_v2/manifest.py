"""Minimal v2 pipeline manifest.

This file deliberately contains only stable boundary metadata. Runtime modules
and schemas are added one approved module at a time.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PIPELINE_VERSION = "v2"
DEFAULT_CAMERA_COUNT = 3
TARGET_CAMERA_COUNT = 8


@dataclass(frozen=True)
class PipelineLayout:
    root: Path

    @property
    def schemas(self) -> Path:
        return self.root / "schemas"

    @property
    def runs(self) -> Path:
        return self.root / "runs"


def default_layout() -> PipelineLayout:
    return PipelineLayout(Path(__file__).resolve().parent)
