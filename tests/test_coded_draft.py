"""Tests for the local coded-draft bundler using real decoded MP4 fixtures."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest import mock

import imageio_ffmpeg
import numpy as np
from PIL import Image


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


class CodedDraftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.blender = self.root / "blender.exe"
        self.blender.write_bytes(b"test executable placeholder")
        self.shotscript = self.root / "story.json"
        self.shotscript.write_text(
            json.dumps(
                {
                    "scene_id": "tiny_story",
                    "fps": 3,
                    "shots": [{"shot_id": "whole", "duration": 1.0}],
                }
            ),
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
            return types.SimpleNamespace(returncode=0, stdout="BLENDER_PROXY_OK\n", stderr="")

        with mock.patch("videoactagent.coded_draft.subprocess.run", side_effect=identical_runner):
            result = main(self._args())
        self.assertEqual(result, 2)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".coded.*.staging")), [])

    def test_runner_failure_is_logged_and_not_published(self):
        from videoactagent.coded_draft import main

        completed = subprocess.CompletedProcess([], 9, stdout="render started\n", stderr="fatal\n")
        with mock.patch("videoactagent.coded_draft.subprocess.run", return_value=completed):
            result = main(self._args())
        self.assertEqual(result, 2)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".coded.*.staging")), [])


if __name__ == "__main__":
    unittest.main()
