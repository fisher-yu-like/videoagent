"""Strict, deterministic human-restyle instructions from approved trajectory facts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
import re
import unicodedata
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
_HEADINGS = (
    "Subjects and wardrobe",
    "Driving motion from the approved proxy",
    "Environment",
    "Lighting",
    "Camera motion and framing",
    "Photoreal quality",
    "Must preserve / must replace / must avoid",
)
_ACTOR_ID = re.compile(r"(?=.*[A-Za-z0-9])[A-Za-z0-9_.-]+\Z")
_NUMBER_PATTERN = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?"
_SHOT_SIZE_PATTERN = r"[A-Za-z0-9_-]+(?: [A-Za-z0-9_-]+)*"
_SEGMENT_FACT = re.compile(r"K(?P<start>\d+) to K(?P<end>\d+), (?P<body>.+)\Z")
_FRAMING_FACT = re.compile(
    rf"framing starts as {_SHOT_SIZE_PATTERN} at {_NUMBER_PATTERN} mm "
    rf"and ends as {_SHOT_SIZE_PATTERN} at {_NUMBER_PATTERN} mm\Z"
)
_CONTINUITY = re.compile(
    rf"One continuous (?P<duration>{_NUMBER_PATTERN})-second take, no cuts, "
    r"no time jumps, and no teleporting\.\Z",
)
_CREATIVE_SENTENCES = frozenset(
    [
        "Use cinematic realistic visual style",
        "Use documentary visual style",
        "Use warm mood",
        "Use neutral mood",
        "Use tense mood",
    ]
    + [
        f"Use {style} and {mood}"
        for style in (
            "cinematic realistic visual style",
            "documentary visual style",
        )
        for mood in ("warm mood", "neutral mood", "tense mood")
    ]
)
_CAMERA_CLAIM_ORDER = ("motion", "look_at", "focal", "shot_size", "roll")


class RestylePromptError(ValueError):
    """Raised when a restyle profile or trajectory source is malformed."""


@dataclass(frozen=True, slots=True)
class SubjectProfile:
    actor_id: str
    description: str

    def __post_init__(self) -> None:
        _validated_actor_id(self.actor_id, "actor_id")
        _profile_text(self.description, "description")


@dataclass(frozen=True, slots=True)
class RestyleProfile:
    schema_version: str
    scene_id: str
    subjects: tuple[SubjectProfile, ...]
    environment: str
    lighting: str
    quality: str

    def __post_init__(self) -> None:
        if self.schema_version != "1.0":
            raise RestylePromptError("schema_version must be exactly '1.0'")
        _profile_text(self.scene_id, "scene_id")
        if not isinstance(self.subjects, tuple) or not self.subjects:
            raise RestylePromptError("subjects must be a non-empty tuple")
        if not all(isinstance(subject, SubjectProfile) for subject in self.subjects):
            raise RestylePromptError("subjects must contain only SubjectProfile values")
        actor_ids = [subject.actor_id for subject in self.subjects]
        if len(actor_ids) != len(set(actor_ids)):
            raise RestylePromptError("subjects must have unique actor IDs")
        _profile_text(self.environment, "environment")
        _profile_text(self.lighting, "lighting")
        _profile_text(self.quality, "quality")


def _strict_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RestylePromptError(f"{label} must be a non-empty string")
    if value != value.strip():
        raise RestylePromptError(f"{label} must not have surrounding whitespace")
    return value


def _profile_text(value: object, label: str) -> str:
    text = _strict_text(value, label)
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in text):
        raise RestylePromptError(f"{label} must not contain control characters")
    if text in _HEADINGS:
        raise RestylePromptError(f"{label} must not contain a reserved section heading")
    return text


def _validated_actor_id(value: object, label: str) -> str:
    actor_id = _strict_text(value, label)
    if _ACTOR_ID.fullmatch(actor_id) is None:
        raise RestylePromptError(
            f"{label} must use only ASCII letters, digits, '_', '.', or '-'"
        )
    return actor_id


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

    raw_subjects = value["subjects"]
    if not isinstance(raw_subjects, list) or not raw_subjects:
        raise RestylePromptError("subjects must be a non-empty array")

    subjects: list[SubjectProfile] = []
    for index, raw_subject in enumerate(raw_subjects):
        if not isinstance(raw_subject, Mapping):
            raise RestylePromptError(f"subjects[{index}] must be an object")
        _exact_keys(raw_subject, _SUBJECT_KEYS, f"subjects[{index}]")
        subjects.append(SubjectProfile(
            actor_id=raw_subject["actor_id"],
            description=raw_subject["description"],
        ))

    return RestyleProfile(
        schema_version=value["schema_version"],
        scene_id=value["scene_id"],
        subjects=tuple(subjects),
        environment=value["environment"],
        lighting=value["lighting"],
        quality=value["quality"],
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


def _camera_claim(value: str) -> str | None:
    exact = {
        "camera moves along the approved path": "motion",
        "camera look-at changes": "look_at",
    }
    if value in exact:
        return exact[value]
    patterns = (
        (
            "focal",
            rf"focal length changes from {_NUMBER_PATTERN} mm to {_NUMBER_PATTERN} mm",
        ),
        (
            "shot_size",
            rf"shot size changes from {_SHOT_SIZE_PATTERN} to {_SHOT_SIZE_PATTERN}",
        ),
        (
            "roll",
            rf"roll changes from {_NUMBER_PATTERN} degrees to {_NUMBER_PATTERN} degrees",
        ),
    )
    for claim, pattern in patterns:
        if re.fullmatch(pattern, value) is not None:
            return claim
    return None


def _trajectory_fact_block(source: str, duration: float) -> list[str]:
    continuity = _CONTINUITY.search(source)
    if continuity is None or continuity.start() == 0 or source[continuity.start() - 1] != " ":
        raise RestylePromptError("trajectory_prompt has no terminal continuity sentence")
    trajectory_duration = float(continuity.group("duration"))
    if not math.isfinite(trajectory_duration) or trajectory_duration != duration:
        raise RestylePromptError("trajectory and restyle durations must match")

    prefix = source[:continuity.start() - 1]
    for creative in sorted(_CREATIVE_SENTENCES, key=len, reverse=True):
        suffix = f" {creative}."
        if prefix.endswith(suffix):
            prefix = prefix[:-len(suffix)]
            break

    if not prefix.endswith("."):
        raise RestylePromptError("trajectory_prompt has a malformed fact block")
    before_final_period = prefix[:-1]
    boundary = before_final_period.rfind(". K")
    if boundary < 1:
        raise RestylePromptError("trajectory_prompt has no anchored trajectory fact block")
    appearance = before_final_period[:boundary]
    _strict_text(appearance, "trajectory appearance envelope")
    fact_block = "K" + before_final_period[boundary + 3:]
    facts = fact_block.split("; ")
    if not facts or "; ".join(facts) != fact_block or any(not fact for fact in facts):
        raise RestylePromptError("trajectory_prompt fact delimiters are malformed")
    return facts


def _partition_trajectory_facts(
    trajectory_prompt: object, actor_ids: tuple[str, ...], duration: float,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    source = _strict_text(trajectory_prompt, "trajectory_prompt")
    facts = _trajectory_fact_block(source, duration)
    if _FRAMING_FACT.fullmatch(facts[-1]) is None:
        raise RestylePromptError("trajectory_prompt must end with one framing fact")
    if any(_FRAMING_FACT.fullmatch(fact) is not None for fact in facts[:-1]):
        raise RestylePromptError("trajectory_prompt must contain exactly one framing fact")
    framing = facts.pop()

    motion_pattern, spacing_pattern = _fact_patterns(actor_ids)
    actor_pairs = tuple(
        (first, second)
        for index, first in enumerate(actor_ids)
        for second in actor_ids[index + 1:]
    )
    segments: dict[int, dict[str, dict[Any, str]]] = {}
    current_segment: int | None = None
    for fact in facts:
        segment_match = _SEGMENT_FACT.fullmatch(fact)
        if segment_match is None:
            raise RestylePromptError(f"malformed trajectory-v2 fact: {fact}")
        start = int(segment_match.group("start"))
        end = int(segment_match.group("end"))
        if end != start + 1:
            raise RestylePromptError("trajectory segments must move forward by one K frame")
        if current_segment is None:
            if start != 0:
                raise RestylePromptError("trajectory segments must start at K0 to K1")
            current_segment = start
        elif start != current_segment:
            if start != current_segment + 1:
                raise RestylePromptError("trajectory segments must be contiguous and ordered")
            current_segment = start

        segment = segments.setdefault(start, {
            "actors": {},
            "spacing": {},
            "camera": {},
        })
        body = segment_match.group("body")
        motion_match = motion_pattern.fullmatch(body)
        spacing_match = spacing_pattern.fullmatch(body)
        if motion_match is not None:
            actor = motion_match.group("actor")
            if actor in segment["actors"]:
                raise RestylePromptError(f"duplicate actor fact for K{start}: {actor}")
            segment["actors"][actor] = fact
            continue
        if spacing_match is not None:
            pair = (spacing_match.group("first"), spacing_match.group("second"))
            if pair not in actor_pairs:
                raise RestylePromptError("spacing actor pairs must use sorted distinct IDs")
            if pair in segment["spacing"]:
                raise RestylePromptError(f"duplicate spacing fact for K{start}: {pair}")
            segment["spacing"][pair] = fact
            continue
        camera_claim = _camera_claim(body)
        if camera_claim is None:
            raise RestylePromptError(f"unrecognized trajectory-v2 fact: {fact}")
        if camera_claim in segment["camera"]:
            raise RestylePromptError(
                f"duplicate camera {camera_claim} fact for segment K{start}"
            )
        segment["camera"][camera_claim] = fact

    if not segments:
        raise RestylePromptError("trajectory_prompt must contain trajectory segments")

    driving: list[str] = []
    camera: list[str] = []
    for start in range(len(segments)):
        segment = segments.get(start)
        if segment is None:
            raise RestylePromptError("trajectory segments must be contiguous")
        if set(segment["actors"]) != set(actor_ids):
            raise RestylePromptError(
                f"K{start} to K{start + 1} needs exactly one fact per actor"
            )
        if set(segment["spacing"]) != set(actor_pairs):
            raise RestylePromptError(
                f"K{start} to K{start + 1} needs exactly one fact per actor pair"
            )
        driving.extend(segment["actors"][actor] for actor in actor_ids)
        driving.extend(segment["spacing"][pair] for pair in actor_pairs)
        camera.extend(
            segment["camera"][claim]
            for claim in _CAMERA_CLAIM_ORDER
            if claim in segment["camera"]
        )
    camera.append(framing)
    return tuple(driving), tuple(camera)


def compile_restyle_prompt(
    *, profile: RestyleProfile, trajectory_prompt: str, duration_seconds: float,
) -> str:
    """Return the seven ordered, source-bound restyle sections."""
    if not isinstance(profile, RestyleProfile):
        raise RestylePromptError("profile must be a RestyleProfile")
    duration = _positive_duration(duration_seconds)
    subjects = tuple(sorted(profile.subjects, key=lambda subject: subject.actor_id))
    actor_ids = tuple(subject.actor_id for subject in subjects)
    driving_facts, camera_facts = _partition_trajectory_facts(
        trajectory_prompt, actor_ids, duration
    )

    subject_lines = "\n".join(
        f"- {subject.actor_id}: {subject.description}" for subject in subjects
    )
    driving_lines = "\n".join(f"- {fact}" for fact in driving_facts)
    camera_lines = "\n".join(f"- {fact}" for fact in camera_facts)
    subject_count = len(subjects)
    if subject_count == 1:
        distinguishable = "the distinguishable subject"
    elif subject_count == 2:
        distinguishable = "two distinguishable subjects"
    else:
        distinguishable = f"all {subject_count} distinguishable subjects"

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
