from __future__ import annotations

from dataclasses import dataclass

from videoactagent.shotscript import ActorPlan, Shot, Vec3


@dataclass(frozen=True)
class ShotPrompts:
    plain: str
    cinematic: str
    timed: str


def _words(value: str) -> str:
    return value.replace("_", " ")


def _actor_name(actor_id: str) -> str:
    prefix = "actor_"
    suffix = actor_id[len(prefix) :] if actor_id.startswith(prefix) else actor_id
    return f"actor {suffix.upper()}"


def _position(value: Vec3) -> str:
    return f"({value.x:.1f}, {value.y:.1f})"


def _actor_timing(actor: ActorPlan) -> str:
    return (
        f"{_actor_name(actor.actor_id)} moves from {_position(actor.start)} "
        f"to {_position(actor.end)}, performs {_words(actor.action)}, "
        f"and faces {_actor_name(actor.facing)}"
    )


def compile_shot_prompts(shot: Shot) -> ShotPrompts:
    camera = shot.camera
    actor_timing = "; ".join(_actor_timing(actor) for actor in shot.actors)
    cinematic = (
        f"{shot.prompt} {shot.duration:.1f}s {_words(camera.shot_size)} shot, "
        f"{camera.focal_length_mm:g}mm lens, camera {_words(camera.motion)} "
        f"from {_position(camera.start)} to {_position(camera.end)}, "
        f"look at {_words(camera.look_at)}. Maintain "
        f"{_words(shot.continuity.screen_direction)} screen direction on the "
        f"{_words(shot.continuity.axis_side)} side of the action axis."
    )
    timed = f"0.0-{shot.duration:.1f}s: {actor_timing}."
    return ShotPrompts(plain=shot.prompt, cinematic=cinematic, timed=timed)
