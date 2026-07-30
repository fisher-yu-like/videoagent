"""Trajectory editor tests.

Debug this module with::

    $PY -m unittest tests.test_trajectory_editor -v

The HTTP tests prove local editor mechanics only.  The final test uses the real
persisted Stage 2 preview, but it still does not constitute API/model evidence.
"""

from __future__ import annotations

import importlib
import hashlib
import http.client
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageChops


ROOT = Path(__file__).resolve().parents[1]
REAL_PREVIEW = ROOT / "runs" / "stage2_control_bridge" / "shots" / "s01" / "first.png"
REAL_SHOTSCRIPT = ROOT / "examples" / "station_shotscript.json"


def _payload() -> dict[str, object]:
    return {
        "schema_version": "0.1",
        "scene_id": "station_platform",
        "shot_id": "s01",
        "coordinate_space": "normalized_0_1_top_left",
        "duration_seconds": 5.0,
        "sample_count": 121,
        "tracks": [
            {
                "track_id": "camera_orbit_01",
                "target": {"type": "camera", "id": "camera"},
                "primitive": "circle",
                "semantic": "orbit_clockwise",
                "points": [
                    {"t": 0.0, "x": 0.30, "y": 0.50, "visible": True},
                    {"t": 0.25, "x": 0.50, "y": 0.30, "visible": True},
                    {"t": 0.50, "x": 0.70, "y": 0.50, "visible": True},
                    {"t": 0.75, "x": 0.50, "y": 0.70, "visible": True},
                    {"t": 1.0, "x": 0.30, "y": 0.50, "visible": True},
                ],
            }
        ],
    }


def _write_fixture(workspace: Path) -> None:
    (workspace / "inputs").mkdir(parents=True)
    Image.new("RGB", (160, 90), (22, 28, 35)).save(workspace / "inputs" / "preview.png")
    (workspace / "inputs" / "shots.json").write_text(
        json.dumps(
            {
                "scene_id": "station_platform",
                "fps": 24,
                "shots": [{"shot_id": "s01", "duration": 5.0}],
            }
        ),
        encoding="utf-8",
    )


class TrajectoryEditorBootstrapTests(unittest.TestCase):
    def test_editor_module_exists(self) -> None:
        try:
            importlib.import_module("videoactagent.trajectory_editor")
        except ModuleNotFoundError as exc:
            self.fail(f"trajectory editor module is missing: {exc}")


class TrajectoryEditorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        _write_fixture(self.workspace)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _session(self):
        from videoactagent.trajectory_editor import EditorSession

        return EditorSession.from_paths(
            workspace=self.workspace,
            image=Path("inputs/preview.png"),
            shotscript=Path("inputs/shots.json"),
            shot_id="s01",
            output_dir=Path("outputs/s01"),
        )

    def test_rejects_absolute_and_escaping_paths(self) -> None:
        from videoactagent.trajectory_editor import EditorSession

        common = dict(workspace=self.workspace, shot_id="s01")
        bad_values = (
            dict(image=(self.workspace / "inputs/preview.png").resolve(), shotscript=Path("inputs/shots.json"), output_dir=Path("outputs")),
            dict(image=Path("../preview.png"), shotscript=Path("inputs/shots.json"), output_dir=Path("outputs")),
            dict(image=Path("inputs/preview.png"), shotscript=Path("../shots.json"), output_dir=Path("outputs")),
            dict(image=Path("inputs/preview.png"), shotscript=Path("inputs/shots.json"), output_dir=Path("../outputs")),
        )
        for values in bad_values:
            with self.subTest(values=values), self.assertRaises(ValueError):
                EditorSession.from_paths(**common, **values)

    def test_rejects_input_hardlink(self) -> None:
        from videoactagent.trajectory_editor import EditorSession

        image = self.workspace / "inputs/preview.png"
        hardlink = self.workspace / "inputs/hardlink.png"
        os.link(image, hardlink)
        with self.assertRaisesRegex(ValueError, "hardlink"):
            EditorSession.from_paths(
                workspace=self.workspace,
                image=Path("inputs/hardlink.png"),
                shotscript=Path("inputs/shots.json"),
                shot_id="s01",
                output_dir=Path("outputs/s01"),
            )

    def test_rejects_input_symlink(self) -> None:
        from videoactagent.trajectory_editor import EditorSession

        symlink = self.workspace / "inputs/link.json"
        try:
            symlink.symlink_to(self.workspace / "inputs/shots.json")
        except OSError:
            self.skipTest("symlink creation is unavailable")
        with self.assertRaisesRegex(ValueError, "symlink"):
            EditorSession.from_paths(
                workspace=self.workspace,
                image=Path("inputs/preview.png"),
                shotscript=Path("inputs/link.json"),
                shot_id="s01",
                output_dir=Path("outputs/s01"),
            )

    def test_rejects_output_symlink(self) -> None:
        from videoactagent.trajectory_editor import EditorSession

        outside = self.workspace / "actual-output"
        outside.mkdir()
        output_link = self.workspace / "output-link"
        try:
            output_link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("directory symlink creation is unavailable")
        with self.assertRaisesRegex(ValueError, "symlink"):
            EditorSession.from_paths(
                workspace=self.workspace,
                image=Path("inputs/preview.png"),
                shotscript=Path("inputs/shots.json"),
                shot_id="s01",
                output_dir=Path("output-link"),
            )

    def test_rejects_input_output_hardlink_alias(self) -> None:
        from videoactagent.trajectory_editor import EditorSession

        output = self.workspace / "outputs/s01"
        output.mkdir(parents=True)
        os.link(self.workspace / "inputs/preview.png", output / "trajectory.json")
        with self.assertRaisesRegex(ValueError, "collision|hardlink"):
            EditorSession.from_paths(
                workspace=self.workspace,
                image=Path("inputs/preview.png"),
                shotscript=Path("inputs/shots.json"),
                shot_id="s01",
                output_dir=Path("outputs/s01"),
            )

    def test_rejects_persistent_lock_path_that_is_also_an_input(self) -> None:
        from videoactagent.trajectory_editor import EditorSession

        lock_input = self.workspace / "outputs" / ".s01.trajectory.lock"
        lock_input.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (80, 45), (11, 22, 33)).save(lock_input, format="PNG")
        with self.assertRaisesRegex(ValueError, "lock.*input|collision"):
            EditorSession.from_paths(
                workspace=self.workspace,
                image=Path("outputs/.s01.trajectory.lock"),
                shotscript=Path("inputs/shots.json"),
                shot_id="s01",
                output_dir=Path("outputs/s01"),
            )

    def test_rejects_resolved_output_escape_without_symlink_privilege(self) -> None:
        from videoactagent.trajectory_editor import EditorSession

        original_resolve = Path.resolve
        escaped = self.workspace / "escaped"

        def simulate_reparse_escape(path: Path, strict: bool = False) -> Path:
            if path == escaped:
                return self.workspace.parent / "outside-workspace"
            return original_resolve(path, strict=strict)

        with mock.patch.object(Path, "resolve", new=simulate_reparse_escape):
            with self.assertRaisesRegex(ValueError, "outside workspace"):
                EditorSession.from_paths(
                    workspace=self.workspace,
                    image=Path("inputs/preview.png"),
                    shotscript=Path("inputs/shots.json"),
                    shot_id="s01",
                    output_dir=Path("escaped"),
                )

    def test_save_validates_identity_and_leaves_no_partial_output(self) -> None:
        session = self._session()
        invalid = _payload()
        invalid["shot_id"] = "s02"
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            session.save(invalid)
        self.assertFalse(session.trajectory_path.exists())
        self.assertFalse(session.overlay_path.exists())

    def test_overlay_failure_preserves_old_consistent_pair(self) -> None:
        session = self._session()
        first = session.save(_payload())
        old_json = session.trajectory_path.read_bytes()
        old_overlay = session.overlay_path.read_bytes()
        changed = _payload()
        changed["sample_count"] = 81
        with mock.patch(
            "videoactagent.trajectory_editor.render_overlay",
            side_effect=OSError("injected overlay failure"),
        ):
            with self.assertRaisesRegex(OSError, "injected"):
                session.save(changed)
        self.assertEqual(old_json, session.trajectory_path.read_bytes())
        self.assertEqual(old_overlay, session.overlay_path.read_bytes())
        self.assertEqual(first["trajectory_sha256"], hashlib.sha256(old_json).hexdigest())

    def test_second_replace_failure_rolls_back_both_outputs(self) -> None:
        session = self._session()
        session.save(_payload())
        old_json = session.trajectory_path.read_bytes()
        old_overlay = session.overlay_path.read_bytes()
        changed = _payload()
        changed["sample_count"] = 81

        real_replace = os.replace
        commit_count = 0

        def fail_second_new_file(source, destination):
            nonlocal commit_count
            if Path(destination) in {session.trajectory_path, session.overlay_path}:
                commit_count += 1
                if commit_count == 2:
                    raise OSError("injected second replace failure")
            return real_replace(source, destination)

        with mock.patch("videoactagent.trajectory_editor.os.replace", side_effect=fail_second_new_file):
            with self.assertRaisesRegex(OSError, "second replace"):
                session.save(changed)
        self.assertEqual(old_json, session.trajectory_path.read_bytes())
        self.assertEqual(old_overlay, session.overlay_path.read_bytes())

    def test_restore_failure_preserves_recoverable_backup(self) -> None:
        session = self._session()
        session.save(_payload())
        old_json = session.trajectory_path.read_bytes()
        changed = _payload()
        changed["sample_count"] = 81

        real_replace = os.replace
        failed_install = False

        def fail_install_then_json_restore(source, destination):
            nonlocal failed_install
            source_path, destination_path = Path(source), Path(destination)
            if destination_path == session.overlay_path and source_path.suffix == ".tmp":
                failed_install = True
                raise OSError("injected overlay install failure")
            if failed_install and destination_path == session.trajectory_path and source_path.suffix == ".bak":
                raise OSError("injected JSON restore failure")
            return real_replace(source, destination)

        with mock.patch("videoactagent.trajectory_editor.os.replace", side_effect=fail_install_then_json_restore):
            with self.assertRaisesRegex(OSError, "backup.*preserved"):
                session.save(changed)
        backups = list(session.output_dir.glob(".trajectory.json.*.bak"))
        self.assertEqual(1, len(backups), "the only recoverable old JSON backup must remain")
        self.assertEqual(old_json, backups[0].read_bytes())

    def test_http_is_loopback_only_and_serves_only_selected_preview(self) -> None:
        from videoactagent.trajectory_editor import create_server

        server = create_server(self._session(), port=0)
        self.assertEqual("127.0.0.1", server.server_address[0])
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
            connection.request("GET", "/preview.png")
            response = connection.getresponse()
            served = response.read()
            self.assertEqual(200, response.status)
            self.assertEqual((self.workspace / "inputs/preview.png").read_bytes(), served)
            connection.close()

            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
            connection.request("GET", "/inputs/shots.json")
            response = connection.getresponse()
            response.read()
            self.assertEqual(404, response.status)
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_jpeg_preview_is_decoded_and_served_as_real_png(self) -> None:
        from videoactagent.trajectory_editor import EditorSession, create_server

        jpeg = self.workspace / "inputs/preview.jpg"
        Image.new("RGB", (73, 41), (90, 20, 140)).save(jpeg, format="JPEG")
        session = EditorSession.from_paths(
            workspace=self.workspace,
            image=Path("inputs/preview.jpg"),
            shotscript=Path("inputs/shots.json"),
            shot_id="s01",
            output_dir=Path("outputs/jpeg"),
        )
        server = create_server(session, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
            connection.request("GET", "/preview.png")
            response = connection.getresponse()
            body = response.read()
            self.assertEqual(200, response.status)
            self.assertEqual("image/png", response.headers.get_content_type())
            self.assertTrue(body.startswith(b"\x89PNG\r\n\x1a\n"))
            with Image.open(io.BytesIO(body)) as served:
                self.assertEqual((73, 41), served.size)
                self.assertEqual("PNG", served.format)
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_wrong_host_and_cross_origin_post_are_forbidden_without_outputs(self) -> None:
        from videoactagent.trajectory_editor import create_server

        session = self._session()
        server = create_server(session, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        body = json.dumps(_payload()).encode("utf-8")
        try:
            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
            connection.putrequest("POST", "/save", skip_host=True)
            connection.putheader("Host", "attacker.example")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(len(body)))
            connection.endheaders(body)
            response = connection.getresponse()
            response.read()
            self.assertEqual(403, response.status)
            connection.close()

            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
            connection.request(
                "POST",
                "/save",
                body=body,
                headers={"Content-Type": "application/json", "Origin": "http://attacker.example"},
            )
            response = connection.getresponse()
            response.read()
            self.assertEqual(403, response.status)
            connection.close()
            self.assertFalse(session.trajectory_path.exists())
            self.assertFalse(session.overlay_path.exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_http_post_validates_and_saves_both_outputs(self) -> None:
        from videoactagent.trajectory_editor import create_server

        session = self._session()
        server = create_server(session, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps(_payload()).encode("utf-8")
            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
            connection.request(
                "POST",
                "/save",
                body=body,
                headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
            )
            response = connection.getresponse()
            result = json.loads(response.read())
            self.assertEqual(200, response.status)
            self.assertEqual("ok", result["status"])
            self.assertTrue(session.trajectory_path.is_file())
            self.assertTrue(session.overlay_path.is_file())
            with Image.open(session.overlay_path) as overlay:
                self.assertEqual((160, 90), overlay.size)
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_get_rejects_externally_replaced_wrong_identity_trajectory(self) -> None:
        from videoactagent.trajectory_editor import create_server

        session = self._session()
        session.save(_payload())
        wrong_identity = _payload()
        wrong_identity["scene_id"] = "another_scene"
        session.trajectory_path.write_text(json.dumps(wrong_identity), encoding="utf-8")
        server = create_server(session, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
            connection.request("GET", "/trajectory.json")
            response = connection.getresponse()
            result = json.loads(response.read())
            self.assertEqual(409, response.status)
            self.assertIn("identity mismatch", result["error"])
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_get_waits_for_in_progress_pair_commit_instead_of_observing_gap(self) -> None:
        from videoactagent.trajectory_editor import create_server

        session = self._session()
        session.save(_payload())
        changed = _payload()
        changed["tracks"][0]["points"][1]["x"] = 0.64  # type: ignore[index]
        server = create_server(session, port=0)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        real_replace = os.replace
        backup_count = 0
        pair_gap = threading.Event()
        allow_commit = threading.Event()
        save_failures: list[BaseException] = []
        response_status: list[int] = []

        def pause_after_both_backups(source, destination):
            nonlocal backup_count
            result = real_replace(source, destination)
            if Path(destination).suffix == ".bak":
                backup_count += 1
                if backup_count == 2:
                    pair_gap.set()
                    if not allow_commit.wait(timeout=5):
                        raise TimeoutError("test did not release commit")
            return result

        def save_changed():
            try:
                session.save(changed)
            except BaseException as exc:
                save_failures.append(exc)

        def get_trajectory():
            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
            try:
                connection.request("GET", "/trajectory.json")
                response = connection.getresponse()
                response.read()
                response_status.append(response.status)
            finally:
                connection.close()

        try:
            with mock.patch("videoactagent.trajectory_editor.os.replace", side_effect=pause_after_both_backups):
                save_thread = threading.Thread(target=save_changed)
                save_thread.start()
                self.assertTrue(pair_gap.wait(timeout=5))
                get_thread = threading.Thread(target=get_trajectory)
                get_thread.start()
                time.sleep(0.1)
                allow_commit.set()
                save_thread.join(timeout=5)
                get_thread.join(timeout=5)
            self.assertFalse(save_failures, save_failures)
            self.assertEqual([200], response_status)
        finally:
            allow_commit.set()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)

    def test_browser_logic_preserves_progress_and_rejects_invalid_polyline_times(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable for browser-logic regression")
        logic = ROOT / "videoactagent" / "static" / "trajectory_editor_logic.js"
        script = f"""
const assert = require('assert');
const logic = require({json.dumps(str(logic))});
const points = logic.prepareDraftPoints([
  {{t:0.2,x:0.1,y:0.3,visible:true}},
  {{t:0.8,x:0.9,y:0.7,visible:true}}
], 'polyline');
assert.deepStrictEqual(points.map(p => p.t), [0.2, 0.8]);
assert.throws(() => logic.prepareDraftPoints([
  {{t:0.8,x:0.1,y:0.3,visible:true}},
  {{t:0.2,x:0.9,y:0.7,visible:true}}
], 'polyline'), /strictly increasing/);
assert.throws(() => logic.prepareDraftPoints([
  {{t:0.2,x:0.1,y:0.3,visible:true}},
  {{t:0.2,x:0.9,y:0.7,visible:true}}
], 'polyline'), /strictly increasing/);
"""
        result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_browser_finish_uses_dragged_draft_not_stale_track_points(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable for browser-logic regression")
        logic = ROOT / "videoactagent" / "static" / "trajectory_editor_logic.js"
        script = f"""
const assert = require('assert');
const logic = require({json.dumps(str(logic))});
const tracks = [{{
  track_id:'actor_polyline_01', target:{{type:'actor',id:'actor_a'}},
  primitive:'polyline', semantic:'move',
  points:[{{t:0.2,x:0.1,y:0.3,visible:false}},{{t:0.8,x:0.7,y:0.3,visible:true}}]
}}];
let draft = tracks[0].points.map(p => ({{...p}}));
draft = logic.updateDraftPoint(draft, 0, {{t:0.95,x:0.45,y:0.55,visible:true}});
const finished = logic.finishTrack(tracks, 0, draft, {{
  track_id:'actor_polyline_01', target:{{type:'actor',id:'actor_a'}},
  primitive:'polyline', semantic:'move'
}});
assert.strictEqual(finished[0].points[0].x, 0.45);
assert.strictEqual(finished[0].points[0].y, 0.55);
assert.strictEqual(finished[0].points[0].t, 0.2);
assert.strictEqual(finished[0].points[0].visible, false);
assert.strictEqual(tracks[0].points[0].x, 0.1);
"""
        result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_html_exposes_required_authoring_controls(self) -> None:
        from videoactagent.trajectory_editor import load_editor_html

        html = load_editor_html().decode("utf-8")
        for token in (
            'id="targetType"',
            'id="primitive"',
            'id="semantic"',
            'id="duration"',
            'id="sampleCount"',
            'id="save"',
            'id="load"',
            'id="delete"',
            "normalized_0_1_top_left",
        ):
            with self.subTest(token=token):
                self.assertIn(token, html)
        self.assertIn("saveButton.disabled=true", html)
        self.assertIn("saveButton.disabled=false", html)

    def test_concurrent_saves_serialize_commit_and_leave_one_payload_pair(self) -> None:
        import videoactagent.trajectory_editor as editor
        from videoactagent.trajectory import TrajectoryInstruction

        session = self._session()
        payload_a = _payload()
        payload_b = _payload()
        payload_b["tracks"][0]["points"][1]["x"] = 0.61  # type: ignore[index]
        payload_b["tracks"][0]["points"][2]["x"] = 0.82  # type: ignore[index]
        render_barrier = threading.Barrier(2)
        real_render = editor.render_overlay
        real_commit = editor._commit_pair
        state_lock = threading.Lock()
        active = 0
        maximum_active = 0
        failures: list[BaseException] = []

        def synchronized_render(image_bytes, instruction):
            render_barrier.wait(timeout=5)
            return real_render(image_bytes, instruction)

        def observed_commit(*args, **kwargs):
            nonlocal active, maximum_active
            with state_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            try:
                time.sleep(0.08)
                return real_commit(*args, **kwargs)
            finally:
                with state_lock:
                    active -= 1

        def save(value):
            try:
                session.save(value)
            except BaseException as exc:
                failures.append(exc)

        with mock.patch.object(editor, "render_overlay", side_effect=synchronized_render), mock.patch.object(
            editor, "_commit_pair", side_effect=observed_commit
        ):
            threads = [threading.Thread(target=save, args=(value,)) for value in (payload_a, payload_b)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertFalse(failures, failures)
        self.assertEqual(1, maximum_active, "pair commits must never overlap within one session")
        final = TrajectoryInstruction.from_path(session.trajectory_path)
        expected_overlay = real_render(session.image_bytes, final)
        self.assertEqual(hashlib.sha256(expected_overlay).hexdigest(), hashlib.sha256(session.overlay_path.read_bytes()).hexdigest())

    def test_two_sessions_for_one_output_directory_serialize_complete_saves(self) -> None:
        import videoactagent.trajectory_editor as editor
        from videoactagent.trajectory import TrajectoryInstruction

        session_a = self._session()
        session_b = self._session()
        payload_a = _payload()
        payload_b = _payload()
        payload_b["tracks"][0]["points"][1]["x"] = 0.61  # type: ignore[index]
        payload_b["tracks"][0]["points"][2]["x"] = 0.82  # type: ignore[index]
        start_barrier = threading.Barrier(2)
        second_arrived = threading.Event()
        real_commit = editor._commit_pair
        actual_commit_lock = threading.Lock()
        state_lock = threading.Lock()
        active = 0
        maximum_active = 0
        failures: list[BaseException] = []

        def observed_commit(*args, **kwargs):
            nonlocal active, maximum_active
            with state_lock:
                active += 1
                maximum_active = max(maximum_active, active)
                if active == 2:
                    second_arrived.set()
            try:
                second_arrived.wait(timeout=0.5)
                # Keep the pre-fix reproduction from damaging the fixture while
                # still observing that both pair transactions entered together.
                with actual_commit_lock:
                    return real_commit(*args, **kwargs)
            finally:
                with state_lock:
                    active -= 1

        def save(session, value):
            try:
                start_barrier.wait(timeout=5)
                session.save(value)
            except BaseException as exc:
                failures.append(exc)

        with mock.patch.object(editor, "_commit_pair", side_effect=observed_commit):
            threads = [
                threading.Thread(target=save, args=(session_a, payload_a)),
                threading.Thread(target=save, args=(session_b, payload_b)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertFalse(failures, failures)
        self.assertEqual(1, maximum_active, "sessions sharing an output directory must not overlap saves")
        final = TrajectoryInstruction.from_path(session_a.trajectory_path)
        expected_overlay = editor.render_overlay(session_a.image_bytes, final)
        self.assertEqual(
            hashlib.sha256(expected_overlay).hexdigest(),
            hashlib.sha256(session_a.overlay_path.read_bytes()).hexdigest(),
        )

    def test_output_lock_covers_post_commit_pair_checks_across_sessions(self) -> None:
        import videoactagent.trajectory_editor as editor

        session_a = self._session()
        session_b = self._session()
        session_a.save(_payload())
        payload_a = _payload()
        payload_a["tracks"][0]["points"][1]["x"] = 0.61  # type: ignore[index]
        payload_b = _payload()
        payload_b["tracks"][0]["points"][1]["x"] = 0.73  # type: ignore[index]
        a_released_lock = threading.Event()
        b_reached_partial_pair = threading.Event()
        allow_b_to_finish = threading.Event()
        a_finished = threading.Event()
        real_release = editor._release_output_lock
        real_replace = os.replace
        failures: dict[str, BaseException] = {}

        def release_then_pause(path, ownership):
            real_release(path, ownership)
            if threading.current_thread().name == "session-a":
                a_released_lock.set()
                if not b_reached_partial_pair.wait(timeout=5):
                    raise TimeoutError("session B did not reach its partial-pair window")

        def pause_before_b_second_backup(source, destination):
            source_path = Path(source)
            destination_path = Path(destination)
            if (
                threading.current_thread().name == "session-b"
                and source_path == session_b.overlay_path
                and destination_path.suffix == ".bak"
            ):
                b_reached_partial_pair.set()
                if not allow_b_to_finish.wait(timeout=5):
                    raise TimeoutError("test did not release session B")
            return real_replace(source, destination)

        def save_a():
            try:
                session_a.save(payload_a)
            except BaseException as exc:
                failures["a"] = exc
            finally:
                a_finished.set()

        def save_b():
            try:
                if not a_released_lock.wait(timeout=5):
                    raise TimeoutError("session A did not release its lock")
                session_b.save(payload_b)
            except BaseException as exc:
                failures["b"] = exc

        with mock.patch.object(editor, "_release_output_lock", side_effect=release_then_pause), mock.patch(
            "videoactagent.trajectory_editor.os.replace", side_effect=pause_before_b_second_backup
        ):
            thread_b = threading.Thread(target=save_b, name="session-b")
            thread_a = threading.Thread(target=save_a, name="session-a")
            thread_b.start()
            thread_a.start()
            try:
                self.assertTrue(b_reached_partial_pair.wait(timeout=5))
                self.assertTrue(a_finished.wait(timeout=5), "session A must finish while B still holds the lock")
                self.assertNotIn("a", failures, failures)
            finally:
                allow_b_to_finish.set()
                thread_a.join(timeout=5)
                thread_b.join(timeout=5)

        self.assertFalse(thread_a.is_alive())
        self.assertFalse(thread_b.is_alive())
        self.assertFalse(failures, failures)
        self.assertTrue(session_a.trajectory_path.is_file())
        self.assertTrue(session_a.overlay_path.is_file())

    def test_subprocess_lock_holder_causes_bounded_fail_closed_timeout_for_save_and_load(self) -> None:
        import videoactagent.trajectory_editor as editor

        session = self._session()
        session.save(_payload())
        lock_path = session.output_dir.parent / f".{session.output_dir.name}.trajectory.lock"
        ready_path = self.workspace / "lock-ready"
        release_path = self.workspace / "lock-release"
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                """
import json, os, sys, time
from pathlib import Path
from videoactagent.trajectory_editor import _lock_open_flags, _try_advisory_lock, _unlock_advisory_lock, _write_persistent_lock_metadata
lock_path, ready_path, release_path = map(Path, sys.argv[1:])
record = {
    "pid": os.getpid(),
    "timestamp": time.time(),
    "output_identity": ["subprocess-holder"],
    "token": "subprocess-holder-token",
}
descriptor = os.open(lock_path, _lock_open_flags(writable=True) | os.O_CREAT, 0o600)
try:
    if not _try_advisory_lock(descriptor):
        raise RuntimeError("subprocess could not acquire advisory lock")
    data = (json.dumps(record) + "\\n").encode("utf-8")
    _write_persistent_lock_metadata(descriptor, data)
    ready_path.write_text("ready", encoding="utf-8")
    deadline = time.monotonic() + 10
    while not release_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
finally:
    _unlock_advisory_lock(descriptor)
    os.close(descriptor)
""",
                str(lock_path),
                str(ready_path),
                str(release_path),
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 5
            while not ready_path.exists() and holder.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            if not ready_path.exists():
                stdout, stderr = holder.communicate(timeout=1)
                self.fail(f"lock holder did not become ready: stdout={stdout!r}, stderr={stderr!r}")

            with mock.patch.object(editor, "OUTPUT_LOCK_TIMEOUT_SECONDS", 0.1, create=True), mock.patch.object(
                editor, "OUTPUT_LOCK_RETRY_SECONDS", 0.01, create=True
            ):
                for operation in (lambda: session.save(_payload()), session.load_trajectory_bytes):
                    with self.subTest(operation=operation), self.assertRaisesRegex(
                        TimeoutError, "subprocess-holder"
                    ):
                        operation()
            self.assertTrue(lock_path.exists(), "foreign/stale locks must be retained for diagnosis")
        finally:
            release_path.touch()
            try:
                holder.wait(timeout=5)
            except subprocess.TimeoutExpired:
                holder.terminate()
                holder.wait(timeout=5)
            holder.communicate(timeout=1)

    def test_failed_save_releases_only_its_owned_lock_with_durable_metadata(self) -> None:
        import videoactagent.trajectory_editor as editor

        session = self._session()
        lock_path = session.output_dir.parent / f".{session.output_dir.name}.trajectory.lock"
        observed: dict[str, object] = {}

        def inspect_lock_then_fail(*args, **kwargs):
            nonlocal observed
            self.assertTrue(lock_path.is_file())
            observed = json.loads(editor._read_lock_diagnostic(lock_path))
            self.assertEqual(os.getpid(), observed["pid"])
            self.assertIsInstance(observed["timestamp"], (int, float))
            self.assertEqual(list(session.output_directory_identity), observed["output_identity"])
            self.assertEqual(str(session.output_dir), observed["output_directory"])
            self.assertRegex(str(observed["token"]), r"^[0-9a-f]{32}$")
            raise OSError("injected commit failure after lock acquisition")

        with mock.patch.object(editor, "_commit_pair", side_effect=inspect_lock_then_fail):
            with self.assertRaisesRegex(OSError, "injected commit failure"):
                session.save(_payload())
        self.assertTrue(lock_path.is_file(), "the persistent lock path is never removed")
        session.save(_payload())

    def test_save_does_not_delete_a_replacement_lock_it_does_not_own(self) -> None:
        import videoactagent.trajectory_editor as editor

        session = self._session()
        lock_path = session.output_dir.parent / f".{session.output_dir.name}.trajectory.lock"
        replacement = b'{"token":"replacement-owner"}\n'
        displaced = lock_path.with_name(lock_path.name + ".displaced")
        real_close = os.close
        swapped = False

        def close_then_replace(descriptor):
            nonlocal swapped
            real_close(descriptor)
            if not swapped and lock_path.is_file():
                swapped = True
                os.replace(lock_path, displaced)
                lock_path.write_bytes(replacement)

        with mock.patch.object(editor.os, "close", side_effect=close_then_replace):
            session.save(_payload())
        self.assertTrue(swapped)
        self.assertEqual(replacement, lock_path.read_bytes())

    def test_release_never_removes_even_an_empty_replacement_lock_file(self) -> None:
        import videoactagent.trajectory_editor as editor

        session = self._session()
        lock_path = session.output_dir.parent / f".{session.output_dir.name}.trajectory.lock"
        displaced = lock_path.with_name(lock_path.name + ".displaced-empty-window")
        real_close = os.close
        swapped = False

        def close_then_replace_with_empty_lock(descriptor):
            nonlocal swapped
            real_close(descriptor)
            if not swapped and lock_path.is_file():
                swapped = True
                os.replace(lock_path, displaced)
                lock_path.touch()

        with mock.patch.object(editor.os, "close", side_effect=close_then_replace_with_empty_lock):
            session.save(_payload())

        self.assertTrue(swapped, "the regression must inject immediately after closing the held descriptor")
        self.assertTrue(lock_path.is_file())
        self.assertEqual(b"", lock_path.read_bytes())

    def test_lock_fstat_failure_preserves_original_error_and_does_not_leak_advisory_lock(self) -> None:
        import videoactagent.trajectory_editor as editor

        session = self._session()
        lock_path = session.output_dir.parent / f".{session.output_dir.name}.trajectory.lock"
        real_fstat = os.fstat
        injected = False

        def fail_first_lock_fstat(descriptor):
            nonlocal injected
            if lock_path.is_file() and not injected:
                injected = True
                raise OSError("injected persistent lock fstat failure")
            return real_fstat(descriptor)

        with mock.patch.object(editor.os, "fstat", side_effect=fail_first_lock_fstat):
            with self.assertRaisesRegex(OSError, "injected persistent lock fstat failure"):
                session.save(_payload())
        self.assertTrue(injected)
        self.assertTrue(lock_path.is_file())
        session.save(_payload())

    def test_lock_metadata_write_failure_preserves_original_error_and_does_not_leak_advisory_lock(self) -> None:
        import videoactagent.trajectory_editor as editor

        session = self._session()
        lock_path = session.output_dir.parent / f".{session.output_dir.name}.trajectory.lock"
        with mock.patch.object(editor, "_write_all", side_effect=OSError("injected lock metadata write failure")):
            with self.assertRaisesRegex(OSError, "injected lock metadata write failure"):
                session.save(_payload())
        self.assertTrue(lock_path.is_file())
        session.save(_payload())

    def test_crashed_subprocess_releases_os_lock_and_persistent_file_is_reacquired(self) -> None:
        session = self._session()
        lock_path = session.output_dir.parent / f".{session.output_dir.name}.trajectory.lock"
        ready_path = self.workspace / "crash-lock-ready"
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                """
import os, sys
from pathlib import Path
from videoactagent.trajectory_editor import _lock_open_flags, _try_advisory_lock, _write_persistent_lock_metadata
lock_path, ready_path = map(Path, sys.argv[1:])
descriptor = os.open(lock_path, _lock_open_flags(writable=True) | os.O_CREAT, 0o600)
if not _try_advisory_lock(descriptor):
    raise RuntimeError("crash holder could not acquire advisory lock")
_write_persistent_lock_metadata(descriptor, b'{"token":"crash-holder"}\\n')
ready_path.write_text("ready", encoding="utf-8")
sys.stdin.buffer.read(1)
os._exit(23)
""",
                str(lock_path),
                str(ready_path),
            ],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 5
            while not ready_path.exists() and holder.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            if not ready_path.exists():
                stdout, stderr = holder.communicate(timeout=1)
                self.fail(f"crash holder did not become ready: stdout={stdout!r}, stderr={stderr!r}")
            assert holder.stdin is not None
            holder.stdin.write(b"x")
            holder.stdin.flush()
            holder.wait(timeout=5)
            self.assertEqual(23, holder.returncode)
            session.save(_payload())
            self.assertTrue(lock_path.is_file())
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.wait(timeout=5)
            for stream in (holder.stdin, holder.stdout, holder.stderr):
                if stream is not None:
                    stream.close()

    def test_lock_diagnostic_is_single_file_and_strictly_byte_bounded(self) -> None:
        import videoactagent.trajectory_editor as editor

        session = self._session()
        lock_path = session.output_dir.parent / f".{session.output_dir.name}.trajectory.lock"
        lock_path.write_bytes(b"A" * (editor.LOCK_DIAGNOSTIC_MAX_BYTES + 4096))
        reads: list[int] = []
        real_read = os.read

        def bounded_read(descriptor, count):
            reads.append(count)
            return real_read(descriptor, count)

        with mock.patch.object(Path, "glob", side_effect=AssertionError("diagnostics must not glob")), mock.patch.object(
            editor.os, "read", side_effect=bounded_read
        ):
            diagnostic = editor._read_lock_diagnostic(lock_path)
        self.assertEqual([editor.LOCK_DIAGNOSTIC_MAX_BYTES], reads)
        self.assertLessEqual(len(diagnostic.encode("utf-8")), editor.LOCK_DIAGNOSTIC_MAX_BYTES)

    def test_rejects_unsafe_persistent_lock_directory_and_hardlink(self) -> None:
        session = self._session()
        lock_path = session.output_dir.parent / f".{session.output_dir.name}.trajectory.lock"
        lock_path.mkdir()
        with self.assertRaisesRegex((OSError, ValueError), "lock|regular"):
            session.save(_payload())
        lock_path.rmdir()

        source = self.workspace / "foreign-lock-source"
        source.write_bytes(b"foreign")
        os.link(source, lock_path)
        with self.assertRaisesRegex((OSError, ValueError), "hardlink|link count"):
            session.save(_payload())

    def test_rejects_persistent_lock_symlink_without_touching_target(self) -> None:
        session = self._session()
        lock_path = session.output_dir.parent / f".{session.output_dir.name}.trajectory.lock"
        target = self.workspace / "foreign-lock-target"
        original = b"foreign-lock-target"
        target.write_bytes(original)
        try:
            lock_path.symlink_to(target)
        except OSError:
            self.skipTest("symlink creation is unavailable")
        with self.assertRaisesRegex((OSError, ValueError), "lock|regular|symlink"):
            session.save(_payload())
        self.assertEqual(original, target.read_bytes())

    def test_double_rollback_failure_keeps_new_file_and_both_old_backups(self) -> None:
        session = self._session()
        session.save(_payload())
        old_json = session.trajectory_path.read_bytes()
        old_overlay = session.overlay_path.read_bytes()
        changed = _payload()
        changed["tracks"][0]["points"][1]["x"] = 0.63  # type: ignore[index]
        real_replace = os.replace
        install_failed = False

        def fail_overlay_install_and_both_restores(source, destination):
            nonlocal install_failed
            source_path, destination_path = Path(source), Path(destination)
            if destination_path == session.overlay_path and source_path.suffix == ".tmp":
                install_failed = True
                raise OSError("injected overlay install failure")
            if install_failed and source_path.suffix == ".bak":
                raise OSError(f"injected restore failure for {destination_path.name}")
            return real_replace(source, destination)

        with mock.patch("videoactagent.trajectory_editor.os.replace", side_effect=fail_overlay_install_and_both_restores):
            with self.assertRaisesRegex(OSError, "backup.*preserved") as raised:
                session.save(changed)
        backups = list(session.output_dir.glob(".*.bak"))
        self.assertEqual(2, len(backups))
        backup_data = {path.name.split(".")[1]: path.read_bytes() for path in backups}
        self.assertEqual(old_json, backup_data["trajectory"])
        self.assertEqual(old_overlay, backup_data["trajectory_overlay"])
        self.assertTrue(session.trajectory_path.exists(), "installed JSON must not be unlinked before restore")
        self.assertIn("trajectory.json", str(raised.exception))
        self.assertIn("trajectory_overlay.png", str(raised.exception))

    def test_replaced_output_directory_identity_is_rejected(self) -> None:
        session = self._session()
        session.save(_payload())
        original_json = session.trajectory_path.read_bytes()
        displaced = session.output_dir.with_name("s01-displaced")
        os.replace(session.output_dir, displaced)
        session.output_dir.mkdir()
        try:
            with self.assertRaisesRegex(ValueError, "identity"):
                session.save(_payload())
            self.assertEqual(original_json, (displaced / "trajectory.json").read_bytes())
            self.assertFalse(session.trajectory_path.exists())
        finally:
            shutil.rmtree(session.output_dir, ignore_errors=True)
            os.replace(displaced, session.output_dir)

    def test_directory_identity_change_during_commit_preserves_old_backups(self) -> None:
        import videoactagent.trajectory_editor as editor

        session = self._session()
        session.save(_payload())
        real_replace = os.replace
        real_identity_check = editor.EditorSession._assert_output_directory_identity
        backups_moved = 0
        compromised = False

        def replace_then_mark_compromised(source, destination):
            nonlocal backups_moved, compromised
            result = real_replace(source, destination)
            if Path(destination).suffix == ".bak":
                backups_moved += 1
                if backups_moved == 2:
                    compromised = True
            return result

        def detect_compromise(current_session):
            if compromised:
                raise ValueError("output directory identity changed")
            return real_identity_check(current_session)

        with mock.patch("videoactagent.trajectory_editor.os.replace", side_effect=replace_then_mark_compromised), mock.patch.object(
            editor.EditorSession, "_assert_output_directory_identity", new=detect_compromise
        ):
            with self.assertRaisesRegex(OSError, "backup.*preserved"):
                session.save(_payload())
        self.assertEqual(2, len(list(session.output_dir.glob(".*.bak"))))
        journals = list(session.output_dir.parent.glob(f".{session.output_dir.name}.trajectory_txn.*.json"))
        self.assertEqual(1, len(journals), "unrecoverable rollback must retain a transaction journal")
        record = json.loads(journals[0].read_bytes())
        self.assertEqual("rollback_failed", record["phase"])
        self.assertEqual(list(session.output_directory_identity), record["original_directory_identity"])

    def test_first_install_postcheck_failure_leaves_no_unrecorded_half_pair(self) -> None:
        import videoactagent.trajectory_editor as editor

        session = self._session()
        real_replace = os.replace
        real_identity_check = editor.EditorSession._assert_output_directory_identity
        first_install_mutated = False

        def mutate_then_mark(source, destination):
            nonlocal first_install_mutated
            result = real_replace(source, destination)
            if Path(destination) == session.trajectory_path and Path(source).suffix == ".tmp":
                first_install_mutated = True
            return result

        def fail_postcheck(current_session):
            if first_install_mutated:
                raise ValueError("output directory identity changed after first install")
            return real_identity_check(current_session)

        with mock.patch("videoactagent.trajectory_editor.os.replace", side_effect=mutate_then_mark), mock.patch.object(
            editor.EditorSession, "_assert_output_directory_identity", new=fail_postcheck
        ):
            with self.assertRaises(OSError):
                session.save(_payload())

        journals = list(session.output_dir.parent.glob(f".{session.output_dir.name}.trajectory_txn.*.json"))
        no_pair = not session.trajectory_path.exists() and not session.overlay_path.exists()
        self.assertTrue(no_pair or journals, "a half-installed pair must have a durable recovery journal")
        if journals:
            record = json.loads(journals[0].read_bytes())
            self.assertEqual("rollback_failed", record["phase"])
            self.assertIn(str(session.trajectory_path), record["potentially_installed"])

    def test_success_and_complete_rollback_remove_transaction_journal(self) -> None:
        session = self._session()
        session.save(_payload())
        pattern = f".{session.output_dir.name}.trajectory_txn.*.json"
        self.assertEqual([], list(session.output_dir.parent.glob(pattern)))
        old_json = session.trajectory_path.read_bytes()
        changed = _payload()
        changed["tracks"][0]["points"][1]["x"] = 0.66  # type: ignore[index]
        real_replace = os.replace

        def fail_overlay_install(source, destination):
            if Path(destination) == session.overlay_path and Path(source).suffix == ".tmp":
                raise OSError("injected install failure")
            return real_replace(source, destination)

        with mock.patch("videoactagent.trajectory_editor.os.replace", side_effect=fail_overlay_install):
            with self.assertRaisesRegex(OSError, "install failure"):
                session.save(changed)
        self.assertEqual(old_json, session.trajectory_path.read_bytes())
        self.assertEqual([], list(session.output_dir.parent.glob(pattern)))

    def test_usage_guide_exposes_the_offline_whole_story_entry(self) -> None:
        guide = (ROOT / "docs" / "USAGE.md").read_text(encoding="utf-8")
        for token in (
            "python run.py configs/whole_story_suite.json",
            "8 个独立故事",
            "无需配置 API 密钥",
            "prompt_only",
            "source_video",
            "incomplete",
            "不得继续计算人物轨迹或相机控制分数",
        ):
            with self.subTest(token=token):
                self.assertIn(token, guide)
        self.assertNotIn("-m videoactagent.trajectory_editor", guide)


class RealStage2TrajectoryEditorTests(unittest.TestCase):
    def test_persisted_browser_acceptance_artifacts_match_recorded_hashes(self) -> None:
        from videoactagent.trajectory import TrajectoryInstruction

        output = ROOT / "runs" / "trajectory" / "s01" / "browser_polyline"
        trajectory = output / "trajectory.json"
        overlay = output / "trajectory_overlay.png"
        self.assertTrue(trajectory.is_file(), f"missing actual browser artifact: {trajectory}")
        self.assertTrue(overlay.is_file(), f"missing actual browser artifact: {overlay}")
        self.assertEqual(
            "856f7b1e92587a9ddb85f68004e23bd555318ae8318f062c52585f9529468bb8",
            hashlib.sha256(trajectory.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            "5f04d30ea04a0c1ceccbb2340d8063dc1b9271c81811247097553c252e9adbf5",
            hashlib.sha256(overlay.read_bytes()).hexdigest(),
        )
        instruction = TrajectoryInstruction.from_path(trajectory)
        instruction.validate_identity("station_platform", "s01")
        with Image.open(overlay) as image:
            self.assertEqual((960, 540), image.size)
            self.assertEqual("RGB", image.mode)

    def test_saves_visible_overlay_on_real_stage2_first_frame(self) -> None:
        from videoactagent.trajectory_editor import EditorSession

        self.assertTrue(REAL_PREVIEW.is_file(), f"missing real preview: {REAL_PREVIEW}")
        relative_output = Path("runs") / "test_trajectory_editor" / next(tempfile._get_candidate_names())
        session = EditorSession.from_paths(
            workspace=ROOT,
            image=REAL_PREVIEW.relative_to(ROOT),
            shotscript=REAL_SHOTSCRIPT.relative_to(ROOT),
            shot_id="s01",
            output_dir=relative_output,
        )
        try:
            result = session.save(_payload())
            with Image.open(REAL_PREVIEW) as source, Image.open(session.overlay_path) as overlay:
                self.assertEqual((960, 540), overlay.size)
                self.assertIsNotNone(ImageChops.difference(source.convert("RGB"), overlay.convert("RGB")).getbbox())
            self.assertEqual(
                result["overlay_sha256"],
                hashlib.sha256(session.overlay_path.read_bytes()).hexdigest(),
            )
        finally:
            shutil.rmtree(ROOT / relative_output, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
