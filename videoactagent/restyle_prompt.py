"""Strict, deterministic human-restyle instructions from approved trajectory facts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
import re
from typing import Any


RESTYLE_COMPILER_VERSION = "human-restyle-v1"
_PROFILE_KEYS = {
    "schema_version",
    "scene_id",
    "subjects",
    "environment",
    "lighting",
    "quality",
}
_SUBJECT_KEYS = {"actor_id", "description"}
_ACTOR_ID = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")
_NUMBER_PATTERN = r"-?\d+(?:\.\d+)?"
_SEGMENT_PREFIX = re.compile(r"K\d+ to K\d+, ")
_FRAMING_FACT = re.compile(
    rf"framing starts as [A-Za-z0-9_-]+ at {_NUMBER_PATTERN} mm "
    rf"and ends as [A-Za-z0-9_-]+ at {_NUMBER_PATTERN} mm"
)


class RestylePromptError(ValueError):
    """Raised when a restyle profile or trajectory source is malformed."""


@dataclass(frozen=True, slots=True)
class SubjectProfile:
    actor_id: str
    description: str


@dataclass(frozen=True, slots=True)
class RestyleProfile:
    schema_version: str
    scene_id: str
    subjects: tuple[SubjectProfile, ...]
    environment: str
    lighting: str
    quality: str


def _strict_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RestylePromptError(f"{label} must be a non-empty string")
    if value != value.strip():
        raise RestylePromptError(f"{label} must not have surrounding whitespace")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value.keys())
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(str(key) for key in actual - expected)
        raise RestylePromptError(
            f"{label} has invalid keys; missing={missing}, unknown={unknown}"
        )


def load_restyle_profile(value: Mapping[str, Any]) -> RestyleProfile:
    """Validate and load the exact version-1 human-restyle profile schema."""
    if not isinstance(value, Mapping):
        raise RestylePromptError("restyle profile must be an object")
    _exact_keys(value, _PROFILE_KEYS, "restyle profile")

    schema_version = _strict_text(value["schema_version"], "schema_version")
    if schema_version != "1.0":
        raise RestylePromptError("schema_version must be exactly '1.0'")

    raw_subjects = value["subjects"]
    if not isinstance(raw_subjects, list) or not raw_subjects:
        raise RestylePromptError("subjects must be a non-empty array")

    subjects: list[SubjectProfile] = []
    actor_ids: set[str] = set()
    for index, raw_subject in enumerate(raw_subjects):
        if not isinstance(raw_subject, Mapping):
            raise RestylePromptError(f"subjects[{index}] must be an object")
        _exact_keys(raw_subject, _SUBJECT_KEYS, f"subjects[{index}]")
        actor_id = _strict_text(raw_subject["actor_id"], f"subjects[{index}].actor_id")
        if _ACTOR_ID.fullmatch(actor_id) is None:
            raise RestylePromptError(
                f"subjects[{index}].actor_id must be a stable identifier"
            )
        if actor_id in actor_ids:
            raise RestylePromptError(f"duplicate actor_id: {actor_id}")
        actor_ids.add(actor_id)
        subjects.append(SubjectProfile(
            actor_id=actor_id,
            description=_strict_text(
                raw_subject["description"], f"subjects[{index}].description"
            ),
        ))

    return RestyleProfile(
        schema_version=schema_version,
        scene_id=_strict_text(value["scene_id"], "scene_id"),
        subjects=tuple(subjects),
        environment=_strict_text(value["environment"], "environment"),
        lighting=_strict_text(value["lighting"], "lighting"),
        quality=_strict_text(value["quality"], "quality"),
    )


def _positive_duration(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RestylePromptError("duration_seconds must be a finite positive number")
    duration = float(value)
    if not math.isfinite(duration) or duration <= 0:
        raise RestylePromptError("duration_seconds must be a finite positive number")
    return duration


def _fact_patterns(actor_ids: tuple[str, ...]) -> tuple[re.Pattern[str], re.Pattern[str]]:
    actors = "|".join(re.escape(actor_id) for actor_id in actor_ids)
    motion = re.compile(
        rf"(?P<actor>{actors}) (?:holds position|moves "
        rf"(?:left|right|up|down)(?: and (?:left|right|up|down))?)\Z"
    )
    spacing = re.compile(
        rf"(?P<first>{actors}) and (?P<second>{actors}) "
        rf"(?:move closer|move farther apart|keep similar spacing)\Z"
    )
    return motion, spacing


def _camera_fact(value: str) -> bool:
    exact = {
        "camera moves along the approved path",
        "camera look-at changes",
    }
    if value in exact:
        return True
    return any(re.fullmatch(pattern, value) is not None for pattern in (
        rf"focal length changes from {_NUMBER_PATTERN} mm to {_NUMBER_PATTERN} mm",
        r"shot size changes from [A-Za-z0-9_-]+ to [A-Za-z0-9_-]+",
        rf"roll changes from {_NUMBER_PATTERN} degrees to {_NUMBER_PATTERN} degrees",
    ))


def _partition_trajectory_facts(
    trajectory_prompt: object, actor_ids: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    source = _strict_text(trajectory_prompt, "trajectory_prompt")
    motion_pattern, spacing_pattern = _fact_patterns(actor_ids)
    driving: list[str] = []
    camera: list[str] = []
    seen_actors: set[str] = set()
    has_spacing = False

    for chunk in source.split(";"):
        prefix = _SEGMENT_PREFIX.search(chunk)
        if prefix is None:
            continue
        candidate = chunk[prefix.start():].strip().rstrip(".")
        prefix_text = prefix.group(0)
        body = candidate[len(prefix_text):]
        motion_match = motion_pattern.fullmatch(body)
        spacing_match = spacing_pattern.fullmatch(body)
        if motion_match is not None:
            seen_actors.add(motion_match.group("actor"))
            driving.append(candidate)
        elif spacing_match is not None:
            if spacing_match.group("first") == spacing_match.group("second"):
                raise RestylePromptError("spacing facts require two different actors")
            has_spacing = True
            driving.append(candidate)
        elif _camera_fact(body):
            camera.append(candidate)
        else:
            raise RestylePromptError(f"unrecognized trajectory-v2 fact: {candidate}")

    framing = _FRAMING_FACT.findall(source)
    if len(framing) != 1:
        raise RestylePromptError("trajectory_prompt must contain one framing fact")
    camera.append(framing[0])

    if seen_actors != set(actor_ids):
        raise RestylePromptError("trajectory_prompt must contain motion for every subject")
    if len(actor_ids) > 1 and not has_spacing:
        raise RestylePromptError("trajectory_prompt must contain subject spacing facts")
    return tuple(driving), tuple(camera)


def compile_restyle_prompt(
    *, profile: RestyleProfile, trajectory_prompt: str, duration_seconds: float,
) -> str:
    """Return the seven ordered, source-bound restyle sections."""
    if not isinstance(profile, RestyleProfile):
        raise RestylePromptError("profile must be a validated RestyleProfile")
    duration = _positive_duration(duration_seconds)
    subjects = tuple(sorted(profile.subjects, key=lambda subject: subject.actor_id))
    actor_ids = tuple(subject.actor_id for subject in subjects)
    driving_facts, camera_facts = _partition_trajectory_facts(
        trajectory_prompt, actor_ids
    )

    subject_lines = "\n".join(
        f"- {subject.actor_id}: {subject.description}" for subject in subjects
    )
    driving_lines = "\n".join(f"- {fact}" for fact in driving_facts)
    camera_lines = "\n".join(f"- {fact}" for fact in camera_facts)
    subject_count = len(subjects)
    distinguishable = (
        "two distinguishable subjects" if subject_count == 2
        else f"all {subject_count} distinguishable subjects"
    )

    sections = (
        ("Subjects and wardrobe", subject_lines),
        (
            "Driving motion from the approved proxy",
            f"Render one complete, approximately {duration:g}-second continuous take "
            f"with no cuts, time jumps, or teleporting.\n{driving_lines}",
        ),
        ("Environment", profile.environment),
        ("Lighting", profile.lighting),
        ("Camera motion and framing", camera_lines),
        ("Photoreal quality", profile.quality),
        (
            "Must preserve / must replace / must avoid",
            f"Must preserve: {distinguishable} and the explicit wardrobe profile above. "
            "The approved proxy controls blocking, timing, occlusion, composition, and "
            "camera movement.\n"
            "Must replace: replace every clay/low-poly body with a complete photoreal "
            "human; all bodies must become complete photoreal humans.\n"
            "Must avoid: cylinders, mannequins, plastic, clay, labels, trajectory lines, "
            "path lines, and CG residue.",
        ),
    )
    return "\n\n".join(f"{heading}\n{content}" for heading, content in sections)
