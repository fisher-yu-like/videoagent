"""Tests for the local coded-draft bundler using real decoded MP4 fixtures."""

from __future__ import annotations

import hashlib
import json
from contextlib import redirect_stderr
import io
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest import mock

import imageio_ffmpeg
import numpy as np
from PIL import Image

from videoactagent.trajectory import (
    TrajectoryInstruction, TrajectoryPoint, TrajectoryTarget, TrajectoryTrack,
    canonical_bytes,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_video(
    path: Path,
    *,
    frame_count: int,
    fps: int,
    resolution: tuple[int, int],
    base_value: int,
) -> None:
    """Encode a tiny real H.264 MP4; production code must decode it."""

    path.parent.mkdir(parents=True, exist_ok=True)
    writer = imageio_ffmpeg.write_frames(
        str(path), resolution, fps=fps, codec="libx264", pix_fmt_in="rgb24"
    )
    writer.send(None)
    try:
        width, height = resolution
        for index in range(frame_count):
            pixels = np.full((height, width, 3), base_value + index, dtype=np.uint8)
            writer.send(pixels.tobytes())
    finally:
        writer.close()


def _write_report(
    output_dir: Path,
    *,
    style: str,
    fps: int = 4,
    resolution: tuple[int, int] = (64, 48),
    frames: int = 4,
) -> None:
    (output_dir / "trajectory_report.json").write_text(
        json.dumps(
            {
                "render_style": style,
                "effective_profile": {
                    "fps": fps,
                    "resolution": list(resolution),
                },
                "rendered_frames": frames,
            }
        ),
        encoding="utf-8",
    )


def _semantic_document(duration: float = 1.0) -> dict[str, object]:
    times = (0.0, 0.25, 0.5, 1.0)
    keyframes = [
        {
            "id": f"K{index}",
            "t": t,
            "visible_state": f"visible state {index}",
            "actor_states": [f"actor state {index}"],
            "camera_state": "locked camera",
            "must_not_show": ["jump cut"],
        }
        for index, t in enumerate(times)
    ]
    transitions = [
        {
            "from": f"K{index}",
            "to": f"K{index + 1}",
            "cause": "continuous action",
            "continuous_change": "distance changes",
            "should_not_jump": "position",
        }
        for index in range(3)
    ]
    return {
        "schema_version": "1.0",
        "story_id": "tiny_story",
        "duration_seconds": duration,
        "appearance_instruction": "Render natural people with coherent lighting.",
        "semantic_keyframes": keyframes,
        "transitions": transitions,
        "causal_constraints": ["movement precedes arrival"],
        "must_show": ["continuous movement"],
        "must_avoid": ["teleportation"],
        "uncertain_assumptions": ["the second actor waits"],
    }


def _shotscript_document() -> dict[str, object]:
    return {
        "scene_id": "tiny_story",
        "environment_preset": "station",
        "fps": 3,
        "world_bounds": [-5.0, 5.0, -4.0, 4.0],
        "shots": [
            {
                "shot_id": "whole",
                "duration": 1.0,
                "prompt": "One continuous approach.",
                "camera": {
                    "shot_size": "wide",
                    "focal_length_mm": 35.0,
                    "motion": "static",
                    "start": [0.0, -10.0, 6.0],
                    "end": [0.0, -10.0, 6.0],
                    "look_at": "actors_midpoint",
                },
                "actors": [
                    {
                        "id": "actor_a",
                        "color": "#777777",
                        "start": [-2.0, 0.0, 0.0],
                        "end": [0.0, 0.0, 0.0],
                        "action": "walk",
                        "facing": "actor_b",
                    },
                    {
                        "id": "actor_b",
                        "color": "#999999",
                        "start": [1.0, 0.0, 0.0],
                        "end": [1.0, 0.0, 0.0],
                        "action": "wait",
                        "facing": "actor_a",
                    },
                ],
                "continuity": {
                    "previous_shot": None,
                    "screen_direction": "left_to_right",
                    "axis_side": "north",
                },
            }
        ],
    }


class CodedDraftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.blender = self.root / "blender.exe"
        self.blender.write_bytes(b"test executable placeholder")
        self.shotscript = self.root / "story.json"
        self.shotscript.write_text(
            json.dumps(_shotscript_document()),
            encoding="utf-8",
        )
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("One unbroken approach shot.\n", encoding="utf-8")
        self.semantic = self.root / "semantic.json"
        self.semantic.write_text(
            json.dumps(_semantic_document(), ensure_ascii=False), encoding="utf-8"
        )
        self.output = self.root / "coded"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _args(self, *extra: str) -> list[str]:
        return [
            "--blender",
            str(self.blender),
            "--shotscript",
            str(self.shotscript),
            "--prompt",
            str(self.prompt),
            "--semantic-plan",
            str(self.semantic),
            "--output-dir",
            str(self.output),
            "--fps",
            "4",
            "--resolution",
            "64x48",
            *extra,
        ]

    def _trajectory_inputs(self) -> tuple[Path, Path]:
        instruction = TrajectoryInstruction(
            scene_id="tiny_story", shot_id="whole", duration_seconds=1.0,
            sample_count=120,
            tracks=tuple(
                TrajectoryTrack(
                    track_id=f"human_{actor}", target=TrajectoryTarget("actor", actor),
                    primitive="polyline", semantic="move",
                    points=tuple(TrajectoryPoint(t=t, x=.2 + i * .1, y=.3 + j * .1, visible=True)
                                 for i, t in enumerate((0.0, .25, .5, 1.0))),
                )
                for j, actor in enumerate(("actor_a", "actor_b"))
            ),
        )
        trajectory = self.root / "trajectory.json"
        trajectory.write_bytes(canonical_bytes(instruction))
        authoring = self.root / "trajectory_authoring.json"
        authoring.write_text(json.dumps({
            "schema_version": "1.0", "author_id": "real_human",
            "trajectory_path": "trajectory.json", "trajectory_sha256": _sha256(trajectory),
            "world_bounds": [-5.0, 5.0, -4.0, 4.0],
            "projection_policy": "top_down_world_bounds_linear_y_up_z0",
            "camera_policy": "shotscript_locked", "auto_filled_points": 0,
        }), "utf-8")
        return trajectory, authoring

    @staticmethod
    def _successful_trajectory_runner(command, **_kwargs):
        style = command[command.index("--render-style") + 1]
        output_dir = Path(command[command.index("--output-dir") + 1])
        trajectory = Path(command[command.index("--trajectory") + 1])
        _write_video(output_dir / "trajectory_proxy.mp4", frame_count=4, fps=4,
                     resolution=(64, 48), base_value=35 if style == "diagnostic" else 155)
        manifest = {
            "render_style": style,
            "effective_profile": {"fps": 4, "resolution": [64, 48]},
            "trajectory_sha256": _sha256(trajectory),
            "applied_tracks": {
                "human_actor_a": {"target_type": "actor", "target_id": "actor_a"},
                "human_actor_b": {"target_type": "actor", "target_id": "actor_b"},
            },
        }
        (output_dir / "trajectory_proxy_manifest.json").write_text(json.dumps(manifest), "utf-8")
        return types.SimpleNamespace(returncode=0, stdout="TRAJECTORY_PROXY_OK {}\n", stderr="")

    def test_explicit_trajectory_is_required_as_a_pair_and_bound_into_both_profiles(self):
        from videoactagent.coded_draft import main

        trajectory, authoring = self._trajectory_inputs()
        with self.assertRaises(SystemExit):
            main(self._args("--trajectory", str(trajectory)))
        with mock.patch("videoactagent.coded_draft.subprocess.run",
                        side_effect=self._successful_trajectory_runner) as run:
            result = main(self._args("--trajectory", str(trajectory),
                                     "--trajectory-authoring", str(authoring)))
        self.assertEqual(result, 0)
        self.assertEqual(run.call_count, 2)
        commands = [call.args[0] for call in run.call_args_list]
        forwarded = {command[command.index("--trajectory") + 1] for command in commands}
        self.assertEqual(len(forwarded), 1)
        self.assertEqual(Path(next(iter(forwarded))).name, "trajectory.json")
        bundle = json.loads((self.output / "bundle.json").read_text("utf-8"))
        binding = bundle["motion_semantics"]["explicit_trajectory_binding"]
        self.assertTrue(binding["available"])
        self.assertEqual(binding["projection_policy"], "top_down_world_bounds_linear_y_up_z0")
        self.assertEqual(binding["trajectory"]["sha256"], _sha256(self.output / "sources" / "trajectory.json"))
        self.assertEqual(bundle["conditioning_video"]["path"],
                         "renders/clay/trajectory_proxy.mp4")

    @staticmethod
    def _successful_runner(command, **_kwargs):
        style = command[command.index("--render-style") + 1]
        output_dir = Path(command[command.index("--output-dir") + 1])
        _write_video(
            output_dir / "station_proxy.mp4",
            frame_count=4,
            fps=4,
            resolution=(64, 48),
            base_value=30 if style == "diagnostic" else 150,
        )
        _write_report(output_dir, style=style)
        return types.SimpleNamespace(
            returncode=0,
            stdout=f"BLENDER_PROXY_OK {style}\n",
            stderr="",
        )

    def test_cli_defaults_to_native_24fps_and_960x540(self):
        from videoactagent.coded_draft import parse_args

        args = parse_args(self._args()[:-4])
        self.assertEqual(args.fps, 24)
        self.assertEqual(args.resolution, (960, 540))
        self.assertEqual(args.render_timeout, 300)
        pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'videoactagent-coded-draft = "videoactagent.coded_draft:main"',
            pyproject,
        )

    def test_build_snapshots_sources_runs_both_profiles_and_binds_bundle(self):
        from videoactagent.coded_draft import main

        original_hashes = {
            path.name: _sha256(path)
            for path in (self.shotscript, self.prompt, self.semantic)
        }
        with mock.patch(
            "videoactagent.coded_draft.subprocess.run",
            side_effect=self._successful_runner,
        ) as run:
            result = main(self._args())

        self.assertEqual(result, 0)
        self.assertEqual(run.call_count, 2)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(
            [command[command.index("--render-style") + 1] for command in commands],
            ["diagnostic", "clay"],
        )
        for command in commands:
            self.assertEqual(command[:3], [mock.ANY, "-m", "videoactagent.blender_runner"])
            self.assertEqual(command[command.index("--fps") + 1], "4")
            self.assertEqual(command[command.index("--resolution") + 1], "64x48")
            self.assertEqual(
                Path(command[command.index("--shotscript") + 1]).name,
                "shotscript.json",
            )

        manifest = json.loads((self.output / "manifest.json").read_text("utf-8"))
        bundle = json.loads((self.output / "bundle.json").read_text("utf-8"))
        for key, filename in (
            ("shotscript", "shotscript.json"),
            ("prompt", "prompt.txt"),
            ("semantic_plan", "semantic_plan.json"),
        ):
            record = manifest["sources"][key]
            snapshot = self.output / record["snapshot_path"]
            self.assertEqual(record["original_sha256"], original_hashes[self._source_name(key)])
            self.assertEqual(record["snapshot_sha256"], _sha256(snapshot))
            self.assertTrue(record["verified_equal"])
            self.assertEqual(snapshot.name, filename)

        self.assertEqual(bundle["conditioning_mode"], "source_video_edit")
        self.assertEqual(
            bundle["conditioning_video"]["path"],
            "renders/clay/station_proxy.mp4",
        )
        self.assertEqual(bundle["appearance_instruction"], _semantic_document()["appearance_instruction"])
        self.assertFalse(bundle["backend_consumed"])
        self.assertEqual(bundle["diagnostic_video"]["role"], "evidence_only")
        self.assertFalse(bundle["diagnostic_video"]["backend_consumed"])
        self.assertEqual(
            bundle["conditioning_video"]["sha256"],
            _sha256(self.output / bundle["conditioning_video"]["path"]),
        )
        self.assertEqual(
            manifest["outputs"]["bundle"]["sha256"],
            _sha256(self.output / "bundle.json"),
        )
        self.assertTrue((self.output / "logs" / "diagnostic.log").is_file())
        self.assertTrue((self.output / "logs" / "clay.log").is_file())

    @staticmethod
    def _source_name(key: str) -> str:
        return {"shotscript": "story.json", "prompt": "prompt.txt", "semantic_plan": "semantic.json"}[key]

    def test_real_media_gate_and_semantic_frames_use_decoded_frame_indices(self):
        from videoactagent.coded_draft import main

        with mock.patch(
            "videoactagent.coded_draft.subprocess.run",
            side_effect=self._successful_runner,
        ):
            self.assertEqual(main(self._args()), 0)

        manifest = json.loads((self.output / "manifest.json").read_text("utf-8"))
        for style in ("diagnostic", "clay"):
            media = manifest["videos"][style]["media"]
            self.assertEqual(media["frame_count"], 4)
            self.assertAlmostEqual(media["fps"], 4.0, places=2)
            self.assertAlmostEqual(media["duration_seconds"], 1.0, places=2)
            self.assertEqual(media["resolution"], [64, 48])

        frames = manifest["semantic_frames"]
        self.assertEqual([item["frame_index"] for item in frames], [0, 1, 2, 3])
        self.assertEqual([item["semantic_id"] for item in frames], ["K0", "K1", "K2", "K3"])
        for item in frames:
            for style in ("diagnostic", "clay"):
                image = self.output / item[style]["path"]
                self.assertEqual(item[style]["sha256"], _sha256(image))
                with Image.open(image) as opened:
                    self.assertEqual(opened.size, (64, 48))
        sheet = self.output / manifest["outputs"]["contact_sheet"]["path"]
        with Image.open(sheet) as opened:
            self.assertEqual(opened.size, (128, 48 * 4))

    def test_existing_output_is_rejected_before_runner_launch(self):
        from videoactagent.coded_draft import main

        self.output.mkdir()
        marker = self.output / "keep.txt"
        marker.write_text("untouched", encoding="utf-8")
        with mock.patch("videoactagent.coded_draft.subprocess.run") as run:
            result = main(self._args())
        self.assertEqual(result, 2)
        run.assert_not_called()
        self.assertEqual(marker.read_text("utf-8"), "untouched")

    def test_semantic_keyframe_ids_cannot_escape_the_frame_directory(self):
        from videoactagent.coded_draft import main

        sentinel = self.root / "escaped_diagnostic.png"
        sentinel.write_bytes(b"outside sentinel")
        document = _semantic_document()
        document["semantic_keyframes"][0]["id"] = "../../escaped"  # type: ignore[index]
        document["transitions"][0]["from"] = "../../escaped"  # type: ignore[index]
        self.semantic.write_text(json.dumps(document), encoding="utf-8")

        with mock.patch("videoactagent.coded_draft.subprocess.run") as run:
            result = main(self._args())

        self.assertEqual(result, 2)
        run.assert_not_called()
        self.assertEqual(sentinel.read_bytes(), b"outside sentinel")
        self.assertFalse(self.output.exists())

    def test_shotscript_identity_duration_and_single_story_are_checked_before_render(self):
        from videoactagent.coded_draft import main

        invalid_documents: dict[str, dict[str, object]] = {}
        wrong_story = _shotscript_document()
        wrong_story["scene_id"] = "other_story"
        invalid_documents["identity"] = wrong_story

        wrong_duration = _shotscript_document()
        wrong_duration["shots"][0]["duration"] = 0.75  # type: ignore[index]
        invalid_documents["duration"] = wrong_duration

        nonfinite = _shotscript_document()
        nonfinite["shots"][0]["duration"] = float("nan")  # type: ignore[index]
        invalid_documents["nonfinite"] = nonfinite

        multiple = _shotscript_document()
        second = json.loads(json.dumps(multiple["shots"][0]))  # type: ignore[index]
        second["shot_id"] = "second"
        second["duration"] = 0.5
        second["continuity"]["previous_shot"] = "whole"
        for actor in second["actors"]:
            first_actor = next(
                item
                for item in multiple["shots"][0]["actors"]  # type: ignore[index]
                if item["id"] == actor["id"]
            )
            actor["start"] = first_actor["end"]
        multiple["shots"][0]["duration"] = 0.5  # type: ignore[index]
        multiple["shots"].append(second)  # type: ignore[union-attr]
        invalid_documents["multiple"] = multiple

        for label, document in invalid_documents.items():
            with self.subTest(label=label):
                self.output = self.root / f"coded_{label}"
                self.shotscript.write_text(json.dumps(document), encoding="utf-8")
                with mock.patch("videoactagent.coded_draft.subprocess.run") as run:
                    result = main(self._args())
                self.assertEqual(result, 2)
                run.assert_not_called()
                self.assertFalse(self.output.exists())

    def test_wrong_frame_count_fails_without_publishing_output(self):
        from videoactagent.coded_draft import main

        def short_runner(command, **_kwargs):
            style = command[command.index("--render-style") + 1]
            output_dir = Path(command[command.index("--output-dir") + 1])
            _write_video(
                output_dir / "station_proxy.mp4",
                frame_count=3 if style == "clay" else 4,
                fps=4,
                resolution=(64, 48),
                base_value=50,
            )
            _write_report(output_dir, style=style)
            return types.SimpleNamespace(returncode=0, stdout="BLENDER_PROXY_OK\n", stderr="")

        with mock.patch("videoactagent.coded_draft.subprocess.run", side_effect=short_runner):
            result = main(self._args())
        self.assertEqual(result, 2)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".coded.*.staging")), [])

    def test_fps_resolution_and_nonempty_media_gates_reject_real_bad_files(self):
        from videoactagent.coded_draft import main

        def bad_runner(kind: str):
            def run(command, **_kwargs):
                style = command[command.index("--render-style") + 1]
                output_dir = Path(command[command.index("--output-dir") + 1])
                video = output_dir / "station_proxy.mp4"
                if kind == "empty" and style == "diagnostic":
                    video.parent.mkdir(parents=True, exist_ok=True)
                    video.write_bytes(b"")
                else:
                    _write_video(
                        video,
                        frame_count=5 if kind == "fps" else 4,
                        fps=5 if kind == "fps" else 4,
                        resolution=(32, 48) if kind == "resolution" else (64, 48),
                        base_value=30 if style == "diagnostic" else 150,
                    )
                _write_report(output_dir, style=style)
                return types.SimpleNamespace(
                    returncode=0, stdout="BLENDER_PROXY_OK\n", stderr=""
                )

            return run

        for kind in ("fps", "resolution", "empty"):
            with self.subTest(kind=kind):
                self.output = self.root / f"coded_{kind}"
                with mock.patch(
                    "videoactagent.coded_draft.subprocess.run",
                    side_effect=bad_runner(kind),
                ):
                    result = main(self._args())
                self.assertEqual(result, 2)
                self.assertFalse(self.output.exists())
                self.assertEqual(
                    list(self.root.glob(f".{self.output.name}.*.staging")), []
                )

    def test_identical_profile_videos_fail_without_publishing_output(self):
        from videoactagent.coded_draft import main

        def identical_runner(command, **_kwargs):
            output_dir = Path(command[command.index("--output-dir") + 1])
            _write_video(
                output_dir / "station_proxy.mp4",
                frame_count=4,
                fps=4,
                resolution=(64, 48),
                base_value=90,
            )
            _write_report(output_dir, style=command[command.index("--render-style") + 1])
            return types.SimpleNamespace(returncode=0, stdout="BLENDER_PROXY_OK\n", stderr="")

        with mock.patch("videoactagent.coded_draft.subprocess.run", side_effect=identical_runner):
            result = main(self._args())
        self.assertEqual(result, 2)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".coded.*.staging")), [])

    def test_same_decoded_pixels_with_different_container_bytes_are_rejected(self):
        from videoactagent.coded_draft import main

        def same_pixels_runner(command, **_kwargs):
            style = command[command.index("--render-style") + 1]
            output_dir = Path(command[command.index("--output-dir") + 1])
            video = output_dir / "station_proxy.mp4"
            _write_video(
                video,
                frame_count=4,
                fps=4,
                resolution=(64, 48),
                base_value=90,
            )
            if style == "clay":
                with video.open("ab") as handle:
                    handle.write(b"different container bytes")
            _write_report(output_dir, style=style)
            return types.SimpleNamespace(
                returncode=0, stdout="BLENDER_PROXY_OK\n", stderr=""
            )

        with mock.patch(
            "videoactagent.coded_draft.subprocess.run",
            side_effect=same_pixels_runner,
        ):
            result = main(self._args())

        self.assertEqual(result, 2)
        self.assertFalse(self.output.exists())

    def test_render_report_must_identify_the_matching_profile(self):
        from videoactagent.coded_draft import main

        def wrong_report(command, **kwargs):
            completed = self._successful_runner(command, **kwargs)
            style = command[command.index("--render-style") + 1]
            if style == "clay":
                output_dir = Path(command[command.index("--output-dir") + 1])
                _write_report(output_dir, style="diagnostic")
            return completed

        with mock.patch(
            "videoactagent.coded_draft.subprocess.run", side_effect=wrong_report
        ):
            result = main(self._args())

        self.assertEqual(result, 2)
        self.assertFalse(self.output.exists())

    def test_runner_failure_is_logged_and_not_published(self):
        from videoactagent.coded_draft import main

        completed = subprocess.CompletedProcess([], 9, stdout="render started\n", stderr="fatal\n")
        with mock.patch("videoactagent.coded_draft.subprocess.run", return_value=completed):
            result = main(self._args())
        self.assertEqual(result, 2)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".coded.*.staging")), [])
        failure_log = self.root / "coded.failed" / "diagnostic.log"
        log = failure_log.read_text(encoding="utf-8")
        self.assertIn("RETURN_CODE=9", log)
        self.assertIn("render started", log)
        self.assertIn("fatal", log)

    def test_timeout_persists_command_limit_and_partial_output_outside_staging(self):
        from videoactagent.coded_draft import main

        def timed_out(command, **kwargs):
            self.assertEqual(kwargs["timeout"], 17)
            self.assertEqual(command[command.index("--timeout") + 1], "7")
            raise subprocess.TimeoutExpired(
                command, 7, output="partial stdout\n", stderr="partial stderr\n"
            )

        with mock.patch(
            "videoactagent.coded_draft.subprocess.run", side_effect=timed_out
        ):
            result = main(self._args("--render-timeout", "7"))

        self.assertEqual(result, 2)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".coded.*.staging")), [])
        log = (self.root / "coded.failed" / "diagnostic.log").read_text("utf-8")
        self.assertIn("OUTCOME=timeout", log)
        self.assertIn("TIMEOUT_SECONDS=7", log)
        self.assertIn("partial stdout", log)
        self.assertIn("partial stderr", log)
        self.assertIn("videoactagent.blender_runner", log)

    def test_existing_failure_evidence_is_never_overwritten(self):
        from videoactagent.coded_draft import main

        failure_dir = self.root / "coded.failed"
        failure_dir.mkdir()
        marker = failure_dir / "diagnostic.log"
        marker.write_text("prior failure evidence", encoding="utf-8")

        with mock.patch("videoactagent.coded_draft.subprocess.run") as run:
            result = main(self._args())

        self.assertEqual(result, 2)
        run.assert_not_called()
        self.assertEqual(marker.read_text("utf-8"), "prior failure evidence")

    def test_late_output_mutation_is_rejected_by_final_hash_verification(self):
        import videoactagent.coded_draft as coded_draft

        original_atomic_json = coded_draft._atomic_json

        def mutate_after_manifest(path, value):
            original_atomic_json(path, value)
            if path.name == "manifest.json":
                target = path.parent / "renders" / "diagnostic" / "station_proxy.mp4"
                with target.open("ab") as handle:
                    handle.write(b"late mutation")

        with mock.patch(
            "videoactagent.coded_draft.subprocess.run",
            side_effect=self._successful_runner,
        ), mock.patch(
            "videoactagent.coded_draft._atomic_json",
            side_effect=mutate_after_manifest,
        ):
            result = coded_draft.main(self._args())

        self.assertEqual(result, 2)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".coded.*.staging")), [])

    def test_mutation_between_media_decode_and_inventory_cannot_publish_stale_hashes(self):
        import videoactagent.coded_draft as coded_draft

        original_inventory = coded_draft._artifact_inventory

        def mutate_before_inventory(root, below):
            if below.name == "renders":
                clay = below / "clay" / "station_proxy.mp4"
                with clay.open("ab") as handle:
                    handle.write(b"changed after media record")
            return original_inventory(root, below)

        with mock.patch(
            "videoactagent.coded_draft.subprocess.run",
            side_effect=self._successful_runner,
        ), mock.patch(
            "videoactagent.coded_draft._artifact_inventory",
            side_effect=mutate_before_inventory,
        ):
            result = coded_draft.main(self._args())

        self.assertEqual(result, 2)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".coded.*.staging")), [])

    def test_atomic_json_refuses_nonstandard_nan_values(self):
        from videoactagent.coded_draft import CodedDraftError, _atomic_json

        target = self.root / "nan.json"
        with self.assertRaisesRegex(CodedDraftError, "serialize JSON"):
            _atomic_json(target, {"duration": float("nan")})
        self.assertFalse(target.exists())

    def test_decode_rejects_nonfinite_stream_duration_from_real_video(self):
        import videoactagent.coded_draft as coded_draft

        video = self.root / "duration.mp4"
        _write_video(
            video,
            frame_count=4,
            fps=4,
            resolution=(64, 48),
            base_value=20,
        )
        real_read_frames = imageio_ffmpeg.read_frames

        def frames_with_nan_duration(*args, **kwargs):
            reader = real_read_frames(*args, **kwargs)
            try:
                metadata = dict(next(reader))
                metadata["duration"] = float("nan")
                yield metadata
                yield from reader
            finally:
                reader.close()

        with mock.patch(
            "videoactagent.coded_draft.imageio_ffmpeg.read_frames",
            side_effect=frames_with_nan_duration,
        ), self.assertRaisesRegex(coded_draft.CodedDraftError, "duration"):
            coded_draft._decode_video(
                video,
                expected_frames=4,
                expected_fps=4,
                expected_duration=1.0,
                expected_resolution=(64, 48),
                selected_indices={0, 3},
            )

    def test_cli_reports_json_serialization_failure_without_traceback(self):
        import videoactagent.coded_draft as coded_draft

        real_decode = coded_draft._decode_video

        def inject_nan(*args, **kwargs):
            media, frames = real_decode(*args, **kwargs)
            media["codec"] = float("nan")
            return media, frames

        stderr = io.StringIO()
        with mock.patch(
            "videoactagent.coded_draft.subprocess.run",
            side_effect=self._successful_runner,
        ), mock.patch(
            "videoactagent.coded_draft._decode_video", side_effect=inject_nan
        ), redirect_stderr(stderr):
            result = coded_draft.main(self._args())

        self.assertEqual(result, 2)
        self.assertIn("CODED_DRAFT_FAILED: CodedDraftError", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
