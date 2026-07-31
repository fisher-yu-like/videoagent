"""Strict, immutable semantic story plan parsing."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any


class SemanticPlanError(ValueError):
    """Raised when a semantic story plan is unreadable or invalid."""


_PLAN_KEYS = {
    "schema_version",
    "story_id",
    "duration_seconds",
    "appearance_instruction",
    "semantic_keyframes",
    "transitions",
    "causal_constraints",
    "must_show",
    "must_avoid",
    "uncertain_assumptions",
}
_KEYFRAME_KEYS = {
    "id",
    "t",
    "visible_state",
    "actor_states",
    "camera_state",
    "must_not_show",
}
_TRANSITION_KEYS = {
    "from",
    "to",
    "cause",
    "continuous_change",
    "should_not_jump",
}


def _object(value: Any, name: str, expected_keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SemanticPlanError(f"{name} must be a JSON object")
    actual_keys = set(value)
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)
        unknown = sorted(repr(key) for key in actual_keys - expected_keys)
        raise SemanticPlanError(
            f"{name} keys mismatch: missing={missing}, unknown={unknown}"
        )
    return value


def _non_empty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SemanticPlanError(f"{name} must be a non-empty string")
    return value


def _non_empty_string_list(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise SemanticPlanError(f"{name} must be a non-empty JSON array")
    return tuple(
        _non_empty_string(item, f"{name}[{index}]")
        for index, item in enumerate(value)
    )


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SemanticPlanError(f"{name} must be a finite number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise SemanticPlanError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise SemanticPlanError(f"{name} must be a finite number")
    return result


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SemanticPlanError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise SemanticPlanError(f"non-standard JSON number: {value}")


@dataclass(frozen=True)
class SemanticKeyframe:
    id: str
    t: float
    visible_state: str
    actor_states: tuple[str, ...]
    camera_state: str
    must_not_show: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: Any, index: int = 0) -> "SemanticKeyframe":
        prefix = f"semantic_keyframes[{index}]"
        data = _object(value, prefix, _KEYFRAME_KEYS)
        return cls(
            id=_non_empty_string(data["id"], f"{prefix}.id"),
            t=_finite_number(data["t"], f"{prefix}.t"),
            visible_state=_non_empty_string(
                data["visible_state"], f"{prefix}.visible_state"
            ),
            actor_states=_non_empty_string_list(
                data["actor_states"], f"{prefix}.actor_states"
            ),
            camera_state=_non_empty_string(
                data["camera_state"], f"{prefix}.camera_state"
            ),
            must_not_show=_non_empty_string_list(
                data["must_not_show"], f"{prefix}.must_not_show"
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "t": self.t,
            "visible_state": self.visible_state,
            "actor_states": list(self.actor_states),
            "camera_state": self.camera_state,
            "must_not_show": list(self.must_not_show),
        }


@dataclass(frozen=True)
class SemanticTransition:
    from_id: str
    to_id: str
    cause: str
    continuous_change: str
    should_not_jump: str

    @classmethod
    def from_dict(cls, value: Any, index: int = 0) -> "SemanticTransition":
        prefix = f"transitions[{index}]"
        data = _object(value, prefix, _TRANSITION_KEYS)
        return cls(
            from_id=_non_empty_string(data["from"], f"{prefix}.from"),
            to_id=_non_empty_string(data["to"], f"{prefix}.to"),
            cause=_non_empty_string(data["cause"], f"{prefix}.cause"),
            continuous_change=_non_empty_string(
                data["continuous_change"], f"{prefix}.continuous_change"
            ),
            should_not_jump=_non_empty_string(
                data["should_not_jump"], f"{prefix}.should_not_jump"
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "from": self.from_id,
            "to": self.to_id,
            "cause": self.cause,
            "continuous_change": self.continuous_change,
            "should_not_jump": self.should_not_jump,
        }


@dataclass(frozen=True)
class SemanticStoryPlan:
    schema_version: str
    story_id: str
    duration_seconds: float
    appearance_instruction: str
    semantic_keyframes: tuple[SemanticKeyframe, ...]
    transitions: tuple[SemanticTransition, ...]
    causal_constraints: tuple[str, ...]
    must_show: tuple[str, ...]
    must_avoid: tuple[str, ...]
    uncertain_assumptions: tuple[str, ...]

    @classmethod
    def from_path(cls, path: Path | str) -> "SemanticStoryPlan":
        source = Path(path)
        try:
            text = source.read_text(encoding="utf-8")
            data = json.loads(
                text,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
            return cls.from_dict(data)
        except (OSError, ValueError) as exc:
            raise SemanticPlanError(f"cannot read semantic plan {source}: {exc}") from exc

    @classmethod
    def from_dict(cls, value: Any) -> "SemanticStoryPlan":
        data = _object(value, "root", _PLAN_KEYS)
        if data["schema_version"] != "1.0":
            raise SemanticPlanError("schema_version must be '1.0'")

        duration_seconds = _finite_number(
            data["duration_seconds"], "duration_seconds"
        )
        if duration_seconds <= 0:
            raise SemanticPlanError("duration_seconds must be positive")

        keyframe_values = data["semantic_keyframes"]
        if not isinstance(keyframe_values, list) or not 4 <= len(keyframe_values) <= 6:
            raise SemanticPlanError("semantic_keyframes must contain four to six states")
        keyframes = tuple(
            SemanticKeyframe.from_dict(item, index)
            for index, item in enumerate(keyframe_values)
        )

        transition_values = data["transitions"]
        if not isinstance(transition_values, list):
            raise SemanticPlanError("transitions must be a JSON array")
        transitions = tuple(
            SemanticTransition.from_dict(item, index)
            for index, item in enumerate(transition_values)
        )

        plan = cls(
            schema_version="1.0",
            story_id=_non_empty_string(data["story_id"], "story_id"),
            duration_seconds=duration_seconds,
            appearance_instruction=_non_empty_string(
                data["appearance_instruction"], "appearance_instruction"
            ),
            semantic_keyframes=keyframes,
            transitions=transitions,
            causal_constraints=_non_empty_string_list(
                data["causal_constraints"], "causal_constraints"
            ),
            must_show=_non_empty_string_list(data["must_show"], "must_show"),
            must_avoid=_non_empty_string_list(data["must_avoid"], "must_avoid"),
            uncertain_assumptions=_non_empty_string_list(
                data["uncertain_assumptions"], "uncertain_assumptions"
            ),
        )
        plan._validate_sequence()
        return plan

    def _validate_sequence(self) -> None:
        ids = [frame.id for frame in self.semantic_keyframes]
        if len(set(ids)) != len(ids):
            raise SemanticPlanError("semantic keyframe ids must be unique")

        times = [frame.t for frame in self.semantic_keyframes]
        if times[0] != 0.0:
            raise SemanticPlanError("semantic keyframes must start at 0")
        if times[-1] != 1.0:
            raise SemanticPlanError("semantic keyframes must end at 1")
        if any(current >= following for current, following in zip(times, times[1:])):
            raise SemanticPlanError("semantic keyframe times must be strictly increasing")

        expected_edges = list(zip(ids, ids[1:]))
        actual_edges = [
            (transition.from_id, transition.to_id) for transition in self.transitions
        ]
        if actual_edges != expected_edges:
            raise SemanticPlanError(
                "transitions must cover each adjacent keyframe exactly once"
            )

    def to_dict(self) -> dict[str, object]:
        """Return a canonical JSON-compatible representation."""

        return {
            "schema_version": self.schema_version,
            "story_id": self.story_id,
            "duration_seconds": self.duration_seconds,
            "appearance_instruction": self.appearance_instruction,
            "semantic_keyframes": [
                keyframe.to_dict() for keyframe in self.semantic_keyframes
            ],
            "transitions": [transition.to_dict() for transition in self.transitions],
            "causal_constraints": list(self.causal_constraints),
            "must_show": list(self.must_show),
            "must_avoid": list(self.must_avoid),
            "uncertain_assumptions": list(self.uncertain_assumptions),
        }
