from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from PIL import Image


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _record(path: Path, root: Path) -> dict[str, object]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": _sha(path),
        "bytes": path.stat().st_size,
    }


def _fixture(root: Path) -> Path:
    (root / "sources").mkdir(parents=True)
    (root / "semantic_frames").mkdir()
    shotscript = {
        "scene_id": "tiny_story",
        "environment_preset": "station",
        "fps": 24,
        "world_bounds": [-5.0, 5.0, -4.0, 4.0],
        "shots": [{
            "shot_id": "whole", "duration": 5.0,
            "prompt": "two actors move", 
            "camera": {"shot_size": "wide", "focal_length_mm": 35.0,
                "motion": "static", "start": [0, -10, 6], "end": [0, -10, 6],
                "look_at": "fixed_actors_midpoint"},
            "actors": [
                {"id": "actor_a", "color": "#777777", "start": [-3, 0, 0],
                 "end": [0, 0, 0], "action": "walk", "facing": "actor_b"},
                {"id": "actor_b", "color": "#999999", "start": [2, 0, 0],
                 "end": [1, 0, 0], "action": "walk", "facing": "actor_a"}],
            "continuity": {"previous_shot": None, "screen_direction": "left_to_right",
                           "axis_side": "north"}
        }],
    }
    semantic = {
        "schema_version": "1.0", "story_id": "tiny_story", "duration_seconds": 5.0,
        "appearance_instruction": "natural people",
        "semantic_keyframes": [
            {"id": f"K{i}", "t": t, "visible_state": "visible",
             "actor_states": ["moving"], "camera_state": "locked",
             "must_not_show": ["cut"]}
            for i, t in enumerate((0.0, .2, .5, .8, 1.0))
        ],
        "transitions": [
            {"from": f"K{i}", "to": f"K{i+1}", "cause": "motion",
             "continuous_change": "position", "should_not_jump": "actors"}
            for i in range(4)
        ],
        "causal_constraints": ["move"], "must_show": ["move"],
        "must_avoid": ["cut"], "uncertain_assumptions": ["none"],
    }
    (root / "sources" / "shotscript.json").write_text(json.dumps(shotscript), "utf-8")
    (root / "sources" / "semantic_plan.json").write_text(json.dumps(semantic), "utf-8")
    frames = []
    for i, t in enumerate((0.0, .2, .5, .8, 1.0)):
        image = root / "semantic_frames" / f"K{i}_diagnostic.png"
        Image.new("RGB", (80, 45), (10 + i, 20, 30)).save(image)
        frames.append({"semantic_id": f"K{i}", "t": t,
                       "frame_index": round(t * 119), "diagnostic": _record(image, root)})
    sources = {
        "shotscript": {"snapshot_path": "sources/shotscript.json",
            "snapshot_sha256": _sha(root / "sources" / "shotscript.json"),
            "bytes": (root / "sources" / "shotscript.json").stat().st_size,
            "original_path": "fixture", "original_sha256": _sha(root / "sources" / "shotscript.json"),
            "verified_equal": True},
        "semantic_plan": {"snapshot_path": "sources/semantic_plan.json",
            "snapshot_sha256": _sha(root / "sources" / "semantic_plan.json"),
            "bytes": (root / "sources" / "semantic_plan.json").stat().st_size,
            "original_path": "fixture", "original_sha256": _sha(root / "sources" / "semantic_plan.json"),
            "verified_equal": True},
    }
    bundle = {"schema_version": "1.0", "story_id": "tiny_story",
              "motion_semantics": {"semantic_plan": sources["semantic_plan"], "keyframes": frames},
              "source_bindings": sources, "backend_consumed": False}
    bundle_path = root / "bundle.json"
    bundle_path.write_text(json.dumps(bundle), "utf-8")
    inventory = [_record(root / "sources" / "shotscript.json", root),
                 _record(root / "sources" / "semantic_plan.json", root)]
    inventory.extend(item["diagnostic"] for item in frames)
    inventory.append(_record(bundle_path, root))
    manifest = {"schema_version": "1.0", "story_id": "tiny_story",
                "expected_media": {"frame_count": 120, "fps": 24,
                                   "duration_seconds": 5.0, "resolution": [960, 540]},
                "sources": sources, "semantic_frames": frames,
                "outputs": {"bundle": _record(bundle_path, root)},
                "backend_consumed": False, "artifact_inventory": inventory}
    (root / "manifest.json").write_text(json.dumps(manifest), "utf-8")
    return bundle_path


class TrajectoryAuthorTests(unittest.TestCase):
    def test_top_down_projection_round_trip_uses_y_up(self):
        from videoactagent.trajectory_author import normalized_to_world, world_to_normalized

        bounds = (-5.0, 5.0, -4.0, 4.0)
        self.assertEqual(normalized_to_world(.25, .25, bounds), (-2.5, 2.0, 0.0))
        x, y = world_to_normalized(-2.5, 2.0, bounds)
        self.assertAlmostEqual(x, .25)
        self.assertAlmostEqual(y, .25)

    def test_prepare_copies_exact_k0_k4_evidence_and_declares_locked_camera(self):
        from videoactagent.trajectory_author import prepare_workspace, verify_workspace

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bundle = _fixture(root / "coded")
            out = root / "author"
            manifest_path = prepare_workspace(bundle, out)
            doc = verify_workspace(manifest_path)
            self.assertEqual(doc["actors"], ["actor_a", "actor_b"])
            self.assertEqual([k["id"] for k in doc["keyframes"]], [f"K{i}" for i in range(5)])
            self.assertEqual([k["t"] for k in doc["keyframes"]], [0, .2, .5, .8, 1])
            self.assertEqual(doc["timeline"], {"frame_count": 120, "fps": 24, "duration_seconds": 5.0})
            self.assertEqual(doc["projection_policy"], "top_down_world_bounds_linear_y_up_z0")
            self.assertEqual(doc["camera_policy"], "shotscript_locked")
            for item in doc["keyframes"]:
                self.assertTrue((out / item["diagnostic_frame"]["path"]).is_file())

    def test_prepare_rejects_existing_output_and_source_frame_tampering(self):
        from videoactagent.trajectory_author import prepare_workspace, verify_workspace

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bundle = _fixture(root / "coded")
            out = root / "author"
            manifest = prepare_workspace(bundle, out)
            with self.assertRaises(ValueError):
                prepare_workspace(bundle, out)
            frame = out / "reference" / "K0.png"
            frame.write_bytes(frame.read_bytes() + b"tamper")
            with self.assertRaises(ValueError):
                verify_workspace(manifest)

    def test_save_requires_every_actor_k_point_and_never_fills_missing_points(self):
        from videoactagent.trajectory_author import prepare_workspace, save_authoring

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = prepare_workspace(_fixture(root / "coded"), root / "author")
            points = [
                {"actor_id": actor, "keyframe_id": f"K{i}", "t": t,
                 "x": .1 + i * .1, "y": .2 + j * .1}
                for j, actor in enumerate(("actor_a", "actor_b"))
                for i, t in enumerate((0.0, .2, .5, .8, 1.0))
            ]
            with self.assertRaises(ValueError):
                save_authoring(manifest, {"author_id": "human", "points": points[:-1]})
            trajectory = save_authoring(manifest, {"author_id": "human", "points": points})
            doc = json.loads(trajectory.read_text("utf-8"))
            self.assertEqual(doc["sample_count"], 120)
            self.assertEqual(len(doc["tracks"]), 2)
            self.assertEqual([p["t"] for p in doc["tracks"][0]["points"]], [0, .2, .5, .8, 1])
            self.assertTrue(all(p["visible"] is True for track in doc["tracks"] for p in track["points"]))
            evidence = json.loads((trajectory.parent / "trajectory_authoring.json").read_text("utf-8"))
            self.assertEqual(evidence["author_id"], "human")
            self.assertEqual(evidence["trajectory_sha256"], _sha(trajectory))
            self.assertEqual(evidence["source"]["shotscript"]["sha256"],
                             _sha(trajectory.parent / "source" / "shotscript.json"))
            self.assertEqual(evidence["source"]["semantic_plan"]["sha256"],
                             _sha(trajectory.parent / "source" / "semantic_plan.json"))
            self.assertEqual([item["id"] for item in evidence["source_frames"]],
                             [f"K{i}" for i in range(5)])
            self.assertEqual(evidence["authoring_manifest"]["sha256"],
                             _sha(manifest))

    def test_save_rejects_duplicates_unknowns_out_of_range_and_blank_author(self):
        from videoactagent.trajectory_author import prepare_workspace, save_authoring

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = prepare_workspace(_fixture(root / "coded"), root / "author")
            points = [
                {"actor_id": actor, "keyframe_id": f"K{i}", "t": t, "x": .2, "y": .3}
                for actor in ("actor_a", "actor_b")
                for i, t in enumerate((0.0, .2, .5, .8, 1.0))
            ]
            for payload in (
                {"author_id": " ", "points": points},
                {"author_id": "human", "points": points + [points[0]]},
                {"author_id": "human", "points": [{**points[0], "actor_id": "intruder"}, *points[1:]]},
                {"author_id": "human", "points": [{**points[0], "x": 1.1}, *points[1:]]},
            ):
                with self.assertRaises(ValueError):
                    save_authoring(manifest, payload)

    def test_html_inline_script_has_valid_javascript(self):
        html = (Path(__file__).parents[1] / "static" / "trajectory_author.html").read_text("utf-8")
        script = html.split("<script>", 1)[1].split("</script>", 1)[0]
        completed = subprocess.run(["node", "--check", "-"], input=script, text=True,
                                   capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_blender_runner_forwards_optional_trajectory_and_requires_matching_marker(self):
        from videoactagent import blender_runner

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ("blender.exe", "shot.json", "trajectory.json"):
                (root / name).write_text("x", "utf-8")
            with mock.patch("videoactagent.blender_runner.subprocess.run") as run:
                run.return_value = types.SimpleNamespace(returncode=0,
                    stdout="TRAJECTORY_PROXY_OK {}\n", stderr="")
                code = blender_runner.main([
                    "--blender", str(root / "blender.exe"), "--shotscript", str(root / "shot.json"),
                    "--trajectory", str(root / "trajectory.json"), "--output-dir", str(root / "out")])
            self.assertEqual(code, 0)
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("--trajectory") + 1], str((root / "trajectory.json").resolve()))


if __name__ == "__main__":
    unittest.main()
