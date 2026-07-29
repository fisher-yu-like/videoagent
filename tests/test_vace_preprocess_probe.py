"""Stage 6 VACE source-preprocessing probe contract/security tests.

Run: ``& $PY -m unittest tests.test_vace_preprocess_probe -v`` (see
``docs/DEBUGGING.md``). The function/CLI entry is
``videoactagent.vace_preprocess_probe``; real job input is
``runs/stage6_vace_inputs/s01/vace_job.json`` and a real server run writes
``source_validation.json`` plus a separate ``vace_job.validated.json``. Tests
use controlled modules/processors for mechanics and do not themselves execute
the upstream VACE processor on CUDA, so they are not source-validation evidence.
"""

from __future__ import annotations

import copy
import inspect
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock

import videoactagent.vace_inputs as vace_inputs
import videoactagent.vace_preprocess_probe as probe
from videoactagent.vace_inputs import ProvenanceError, VACE_COMMIT
from videoactagent.vace_preprocess_probe import (
    PROCESSOR_KWARGS,
    _resolve_inputs,
    atomic_write_json,
    build_processor,
    normalize_and_binarize_mask,
)


ROOT = Path(__file__).resolve().parents[1]
REAL_JOB = ROOT / "runs" / "stage6_vace_inputs" / "s01" / "vace_job.json"
REAL_BUNDLE = ROOT / "runs" / "stage2_control_bridge" / "control_bundle.json"


class SourceValidationSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.job = json.loads(REAL_JOB.read_text(encoding="utf-8"))

    def test_handwritten_report_has_no_public_job_upgrade_api(self) -> None:
        handwritten = {
            "status": "passed",
            "tensors": {"source": {"shape": [3, 13, 480, 832]}},
        }
        self.assertTrue(handwritten)
        self.assertFalse(hasattr(vace_inputs, "merge_source_validation"))
        self.assertNotIn("report", inspect.signature(probe.run_probe).parameters)

    def test_run_probe_requires_a_separate_validated_job_output(self) -> None:
        parameters = inspect.signature(probe.run_probe).parameters
        self.assertIn("validated_job_output", parameters)

    def test_output_preflight_rejects_input_aliases_and_hardlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_path = root / "vace_job.json"
            bundle_path = root / "control_bundle.json"
            proxy_path = root / "proxy.mp4"
            mask_path = root / "src_mask.mp4"
            for path in (job_path, bundle_path, proxy_path, mask_path):
                path.write_bytes(path.name.encode("utf-8"))
            inputs = {
                "job": job_path,
                "source_bundle": bundle_path,
                "src_video": proxy_path,
                "src_mask": mask_path,
            }

            for label, input_path in inputs.items():
                with self.subTest(kind="same-path", input=label):
                    with self.assertRaisesRegex(ProvenanceError, label):
                        probe._validate_output_paths(
                            job_path,
                            input_path,
                            root / "vace_job.validated.json",
                            inputs,
                        )

                hardlink = root / f"{label}.hardlink.json"
                try:
                    hardlink.hardlink_to(input_path)
                except OSError as exc:  # pragma: no cover - platform capability
                    self.skipTest(f"hard links unavailable: {exc}")
                with self.subTest(kind="hardlink", input=label):
                    with self.assertRaisesRegex(ProvenanceError, label):
                        probe._validate_output_paths(
                            job_path,
                            hardlink,
                            root / "vace_job.validated.json",
                            inputs,
                        )

    def test_output_preflight_rejects_escape_and_output_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "run"
            root.mkdir()
            job_path = root / "vace_job.json"
            job_path.write_text("{}", encoding="utf-8")
            inputs = {"job": job_path}
            unsafe_cases = (
                (Path(tmp) / "outside.json", root / "validated.json", "report"),
                (root / "report.json", Path(tmp) / "outside.json", "validated"),
                (root / "same.json", root / "same.json", "distinct"),
            )
            for report_path, validated_path, message in unsafe_cases:
                with self.subTest(message=message):
                    with self.assertRaisesRegex(ProvenanceError, message):
                        probe._validate_output_paths(
                            job_path, report_path, validated_path, inputs
                        )

    def test_cli_alias_rejection_does_not_overwrite_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_path = root / "vace_job.json"
            shutil.copy2(REAL_JOB, job_path)
            shutil.copy2(REAL_JOB.parent / "src_mask.mp4", root / "src_mask.mp4")
            original = job_path.read_bytes()
            validated = root / "vace_job.validated.json"
            result = probe.main(
                [
                    "--job",
                    str(job_path),
                    "--vace-root",
                    str(ROOT / "third_party" / "VACE"),
                    "--output",
                    str(job_path),
                    "--validated-job-output",
                    str(validated),
                ]
            )
            self.assertEqual(result, 2)
            self.assertEqual(job_path.read_bytes(), original)

    def test_run_probe_rejects_first_and_last_frame_hardlink_outputs_before_write(self) -> None:
        job = json.loads(REAL_JOB.read_text(encoding="utf-8"))
        for control_name in ("first_frame", "last_frame"):
            with self.subTest(control=control_name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                job_path = root / "vace_job.json"
                shutil.copy2(REAL_JOB, job_path)
                shutil.copy2(REAL_JOB.parent / "src_mask.mp4", root / "src_mask.mp4")
                control_relative = Path(
                    job["source"]["controls"][control_name]["path"]
                )
                control_path = REAL_BUNDLE.parent / Path(*control_relative.parts[1:])
                report_path = root / "source_validation.json"
                try:
                    report_path.hardlink_to(control_path)
                except OSError as exc:  # pragma: no cover - platform capability
                    self.skipTest(f"hard links unavailable: {exc}")
                validated_path = root / "vace_job.validated.json"

                with mock.patch.object(probe, "atomic_write_json") as writer, mock.patch.object(
                    probe, "_git_head"
                ) as git_head:
                    with self.assertRaisesRegex(ProvenanceError, control_name):
                        probe.run_probe(
                            job_path,
                            ROOT / "third_party" / "VACE",
                            report_path,
                            validated_path,
                        )

                writer.assert_not_called()
                git_head.assert_not_called()

    def test_input_resolution_rejects_self_consistent_proxy_substitution(self) -> None:
        bundle = json.loads(REAL_BUNDLE.read_text(encoding="utf-8"))
        alternate_proxy = bundle["shots"][1]["controls"]["proxy_video"]
        tampered = copy.deepcopy(self.job)
        tampered["mapping"]["src_video"] = (
            "source/" + alternate_proxy["path"]
        )
        tampered["source"]["controls"]["proxy_video"] = {
            "path": "source/" + alternate_proxy["path"],
            "bytes": alternate_proxy["bytes"],
            "sha256": alternate_proxy["sha256"],
        }

        with tempfile.TemporaryDirectory() as tmp:
            job_path = Path(tmp) / "vace_job.json"
            job_path.write_text(
                json.dumps(tampered, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            shutil.copy2(REAL_JOB.parent / "src_mask.mp4", job_path.parent)
            with self.assertRaisesRegex(ProvenanceError, "proxy_video|mapping"):
                _resolve_inputs(job_path, tampered)


class ProbeMechanicsTests(unittest.TestCase):
    def test_post_publication_cuda_cleanup_cannot_turn_success_into_failure(self) -> None:
        class FailingCuda:
            @staticmethod
            def empty_cache():
                raise RuntimeError("injected cleanup failure")

        probe._best_effort_cuda_cleanup(SimpleNamespace(cuda=FailingCuda()))

    def test_git_fixture_declares_python_text_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._git_repo_with_processor(root, b"normalized\n")
            completed = subprocess.run(
                [
                    "git",
                    "check-attr",
                    "text",
                    "--",
                    "vace/models/utils/preprocessor.py",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(
                completed.stdout.strip(),
                "vace/models/utils/preprocessor.py: text: set",
            )

    def test_expected_frame_ids_match_real_upstream_server_observation(self) -> None:
        self.assertEqual(
            probe.EXPECTED_FRAME_IDS,
            [0, 1, 2, 3, 4, 6, 7, 8, 9, 10, 12, 13, 14],
        )

    def test_frame_id_mismatch_reports_expected_and_actual_sequences(self) -> None:
        actual = [0, 1, 2, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14]
        with self.assertRaisesRegex(
            ProvenanceError,
            r"expected.*\[0, 1, 2, 3.*actual.*\[0, 1, 2, 4",
        ):
            probe._validate_exact_frame_ids(actual)

    def _git_repo_with_processor(self, root: Path, contents: bytes) -> tuple[Path, str]:
        module_path = root / "vace" / "models" / "utils" / "preprocessor.py"
        module_path.parent.mkdir(parents=True)
        (root / ".gitattributes").write_bytes(b"*.py text\n")
        module_path.write_bytes(contents)
        commands = (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "probe@example.invalid"],
            ["git", "config", "user.name", "Probe Test"],
            [
                "git",
                "add",
                ".gitattributes",
                "vace/models/utils/preprocessor.py",
            ],
            ["git", "commit", "-q", "-m", "fixture"],
        )
        for command in commands:
            completed = subprocess.run(
                command, cwd=root, capture_output=True, text=True, check=False
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
        blob = subprocess.run(
            ["git", "rev-parse", "HEAD:vace/models/utils/preprocessor.py"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return module_path, blob

    def test_processor_blob_gate_accepts_crlf_worktree_for_same_git_blob(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module_path, blob = self._git_repo_with_processor(
                root, b"line_one\nline_two\n"
            )
            module_path.write_bytes(b"line_one\r\nline_two\r\n")
            self.assertEqual(probe._verify_processor_blob(root, blob), blob)

    def test_processor_blob_gate_rejects_wrong_committed_blob(self) -> None:
        with tempfile.TemporaryDirectory() as left, tempfile.TemporaryDirectory() as right:
            _, expected_blob = self._git_repo_with_processor(
                Path(left), b"expected\n"
            )
            self._git_repo_with_processor(Path(right), b"different\n")
            with self.assertRaisesRegex(ProvenanceError, "blob"):
                probe._verify_processor_blob(Path(right), expected_blob)

    def test_processor_blob_gate_rejects_tampered_worktree_with_unchanged_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module_path, expected_blob = self._git_repo_with_processor(
                root, b"trusted_processor = True\n"
            )
            module_path.write_bytes(b"tampered_processor = True\n")
            head_blob = subprocess.run(
                [
                    "git",
                    "rev-parse",
                    "HEAD:vace/models/utils/preprocessor.py",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(head_blob, expected_blob)
            with self.assertRaisesRegex(ProvenanceError, "worktree.*blob"):
                probe._verify_processor_blob(root, expected_blob)

    def test_snapshot_copy_rejects_bytes_that_do_not_match_the_job_hash(self) -> None:
        source = ROOT / "runs" / "stage2_control_bridge" / "shots" / "s01" / "proxy.mp4"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ProvenanceError, "snapshot.*sha256"):
                probe._copy_verified_snapshot(source, Path(tmp) / "proxy.mp4", "0" * 64)

    def test_run_probe_consumes_snapshots_and_publishes_separate_validated_job(self) -> None:
        job = json.loads(REAL_JOB.read_text(encoding="utf-8"))
        original_proxy = REAL_BUNDLE.parent / "shots" / "s01" / "proxy.mp4"
        original_mask = REAL_JOB.parent / "src_mask.mp4"
        loaded_paths: list[Path] = []

        class FakeResult:
            def __init__(self, value):
                self.value = value

            def all(self):
                return self

            def item(self):
                return self.value

        class FakeTensor:
            def __init__(self, shape, low, high):
                self.shape = shape
                self.low = low
                self.high = high
                self.dtype = "torch.float32"
                self.device = "cpu"
                self.transfers = 0

            def to(self, device):
                self.transfers += 1
                self.device = str(device)
                return self

            def isfinite(self):
                return FakeResult(True)

            def min(self):
                return FakeResult(self.low)

            def max(self):
                return FakeResult(self.high)

        source_tensor = FakeTensor((3, 13, 480, 832), -1.0, 1.0)
        mask_tensor = FakeTensor((3, 13, 480, 832), 1.0, 1.0)
        normalized = FakeTensor((1, 13, 480, 832), 1.0, 1.0)
        binary = FakeTensor((1, 13, 480, 832), 1.0, 1.0)

        class FakeProcessor:
            def __init__(self, **_kwargs):
                pass

            def load_video_pair(self, video_path, mask_path):
                video_path = Path(video_path)
                mask_path = Path(mask_path)
                loaded_paths.extend((video_path, mask_path))
                self_outer.assertNotEqual(video_path.resolve(), original_proxy.resolve())
                self_outer.assertNotEqual(mask_path.resolve(), original_mask.resolve())
                self_outer.assertEqual(
                    probe.sha256_file(video_path),
                    job["source"]["controls"]["proxy_video"]["sha256"],
                )
                self_outer.assertEqual(
                    probe.sha256_file(mask_path), job["mask"]["sha256"]
                )
                return (
                    source_tensor,
                    mask_tensor,
                    [0, 1, 2, 3, 4, 6, 7, 8, 9, 10, 12, 13, 14],
                    (480, 832),
                    2.689655,
                )

        class FakeUnique:
            def detach(self):
                return self

            def cpu(self):
                return self

            def tolist(self):
                return [1.0]

        class FakeCuda:
            def __init__(self):
                self.empty_cache_calls = 0

            @staticmethod
            def is_available():
                return True

            @staticmethod
            def set_device(_device):
                pass

            def empty_cache(self):
                self.empty_cache_calls += 1

            @staticmethod
            def reset_peak_memory_stats(_device):
                pass

            @staticmethod
            def synchronize(_device):
                pass

            @staticmethod
            def get_device_properties(_device):
                return SimpleNamespace(total_memory=40 * 1024**3)

            @staticmethod
            def get_device_name(_device):
                return "NVIDIA A100-PCIE-40GB"

            @staticmethod
            def max_memory_allocated(_device):
                return 1000

            @staticmethod
            def max_memory_reserved(_device):
                return 2000

        fake_torch = ModuleType("torch")
        fake_torch.__version__ = "2.5.1+cu124"
        fake_torch.version = SimpleNamespace(cuda="12.4")
        fake_torch.cuda = FakeCuda()
        fake_torch.device = lambda value: value
        fake_torch.unique = lambda _tensor: FakeUnique()
        self_outer = self

        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp)
            job_path = run_root / "vace_job.json"
            shutil.copy2(REAL_JOB, job_path)
            shutil.copy2(REAL_JOB.parent / "src_mask.mp4", run_root / "src_mask.mp4")
            report_path = run_root / "source_validation.json"
            validated_path = run_root / "vace_job.validated.json"
            module_path = (
                ROOT
                / "third_party"
                / "VACE"
                / "vace"
                / "models"
                / "utils"
                / "preprocessor.py"
            )
            with mock.patch.object(
                probe, "_git_head", return_value=VACE_COMMIT
            ), mock.patch.object(
                probe,
                "_load_processor_class",
                return_value=(
                    FakeProcessor,
                    module_path,
                    "a0788111a7b79fda3070a2ab8372956c0726af26",
                ),
            ), mock.patch.object(
                probe, "_nvidia_driver", return_value="580.126.20"
            ), mock.patch.object(
                probe,
                "normalize_and_binarize_mask",
                return_value=(normalized, binary),
            ), mock.patch.dict(sys.modules, {"torch": fake_torch}):
                report = probe.run_probe(
                    job_path,
                    ROOT / "third_party" / "VACE",
                    report_path,
                    validated_path,
                )

            validated = json.loads(validated_path.read_text(encoding="utf-8"))
            self.assertEqual(
                {
                    key: value
                    for key, value in validated.items()
                    if key != "evidence"
                },
                {key: value for key, value in job.items() if key != "evidence"},
            )
            report_binding = validated["evidence"]["source_validation_report"]
            self.assertEqual(report_binding["path"], "source_validation.json")
            self.assertEqual(report_binding["bytes"], report_path.stat().st_size)
            self.assertEqual(report_binding["sha256"], probe.sha256_file(report_path))
            self.assertEqual(report_binding["summary"]["status"], "passed")
            self.assertEqual(
                report_binding["summary"]["claim_boundary"],
                "source_preprocessing_and_cuda_transfer_only",
            )
            self.assertEqual(
                report_binding["summary"]["job_sha256"],
                probe.sha256_file(job_path),
            )
            self.assertEqual(
                report_binding["summary"]["vace_commit"], VACE_COMMIT
            )
            self.assertEqual(
                report_binding["summary"]["processor_blob_id"],
                "a0788111a7b79fda3070a2ab8372956c0726af26",
            )
            self.assertEqual(
                report_binding["summary"]["source_shape"], [3, 13, 480, 832]
            )
            self.assertEqual(
                report_binding["summary"]["mask_shape"], [3, 13, 480, 832]
            )
            self.assertEqual(
                report_binding["summary"]["frame_ids"], probe.EXPECTED_FRAME_IDS
            )
            self.assertTrue(validated["evidence"]["source_validation_passed"])
            self.assertFalse(validated["evidence"]["inference_success"])
            self.assertEqual(
                report["inputs"]["src_video"]["original_path"],
                str(original_proxy.resolve()),
            )
            self.assertEqual(
                report["inputs"]["src_video"]["snapshot_sha256"],
                job["source"]["controls"]["proxy_video"]["sha256"],
            )
            self.assertEqual(
                report["inputs"]["src_mask"]["snapshot_sha256"],
                job["mask"]["sha256"],
            )
            self.assertEqual(
                report["processor"]["module_blob_id"],
                "a0788111a7b79fda3070a2ab8372956c0726af26",
            )
            self.assertEqual(
                report["processor"]["module_file_sha256"],
                probe.sha256_file(module_path),
            )
            self.assertEqual(source_tensor.transfers, 1)
            self.assertEqual(mask_tensor.transfers, 1)
            self.assertGreaterEqual(fake_torch.cuda.empty_cache_calls, 2)
        self.assertEqual(len(loaded_paths), 2)
        self.assertTrue(all(not path.exists() for path in loaded_paths))

    def test_failure_replaces_old_true_validated_job_with_bound_false_state(self) -> None:
        job = copy.deepcopy(self._load_real_job())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report_path = root / "source_validation.json"
            validated_path = root / "vace_job.validated.json"
            stale = copy.deepcopy(job)
            stale["evidence"]["source_validation_passed"] = True
            validated_path.write_text(json.dumps(stale), encoding="utf-8")

            failure = probe._failure_report(RuntimeError("current attempt failed"), 1.25)
            probe._publish_failed_validation(
                job,
                report_path,
                validated_path,
                failure,
                root,
            )

            published_report = json.loads(report_path.read_text(encoding="utf-8"))
            published_job = json.loads(validated_path.read_text(encoding="utf-8"))
            self.assertEqual(published_report["status"], "failed")
            self.assertFalse(published_job["evidence"]["source_validation_passed"])
            binding = published_job["evidence"]["source_validation_report"]
            self.assertEqual(binding["path"], "source_validation.json")
            self.assertEqual(binding["bytes"], report_path.stat().st_size)
            self.assertEqual(binding["sha256"], probe.sha256_file(report_path))
            self.assertEqual(binding["summary"]["status"], "failed")
            self.assertEqual(
                binding["summary"]["error"], "current attempt failed"
            )

    def test_started_marker_replace_failure_archives_old_true_with_record(self) -> None:
        job = copy.deepcopy(self._load_real_job())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report_path = root / "source_validation.json"
            validated_path = root / "vace_job.validated.json"
            stale = copy.deepcopy(job)
            stale["evidence"]["source_validation_passed"] = True
            validated_path.write_text(json.dumps(stale), encoding="utf-8")
            stale_sha256 = probe.sha256_file(validated_path)
            real_atomic_write = probe.atomic_write_json

            def fail_validated_target(path, value):
                if Path(path).resolve() == validated_path.resolve():
                    raise OSError("injected validated replace failure")
                return real_atomic_write(path, value)

            with mock.patch.object(
                probe, "atomic_write_json", side_effect=fail_validated_target
            ):
                with self.assertRaisesRegex(OSError, "validated replace failure"):
                    probe._mark_validation_started(
                        job,
                        report_path,
                        validated_path,
                        root,
                    )

            self.assertFalse(validated_path.exists())
            archived = list(root.glob("vace_job.validated.json.*.stale"))
            records = list(root.glob("vace_job.validated.json.*.stale.failure.json"))
            self.assertEqual(len(archived), 1)
            self.assertEqual(len(records), 1)
            self.assertEqual(probe.sha256_file(archived[0]), stale_sha256)
            failure_record = json.loads(records[0].read_text(encoding="utf-8"))
            self.assertEqual(failure_record["status"], "stale")
            self.assertEqual(failure_record["archived_sha256"], stale_sha256)
            self.assertIn("validated replace failure", failure_record["reason"])

    @staticmethod
    def _load_real_job() -> dict:
        return json.loads(REAL_JOB.read_text(encoding="utf-8"))

    def test_build_processor_uses_exact_pinned_1_3b_configuration(self) -> None:
        class RecordingProcessor:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        processor = build_processor(RecordingProcessor)
        self.assertEqual(processor.kwargs, PROCESSOR_KWARGS)
        self.assertEqual(
            PROCESSOR_KWARGS,
            {
                "downsample": (4, 16, 16),
                "min_area": 480 * 832,
                "max_area": 480 * 832,
                "min_fps": 16,
                "max_fps": 16,
                "zero_start": True,
                "seq_len": 32760,
                "keep_last": True,
            },
        )

    def test_mask_normalization_reproduces_upstream_formula_and_threshold(self) -> None:
        class ScalarMask:
            def __init__(self, channels):
                self.channels = channels

            def __getitem__(self, key):
                self.asserted_key = key
                return ScalarMask(self.channels[:1])

            def __add__(self, value):
                return ScalarMask([[element + value for element in self.channels[0]]])

            def __truediv__(self, value):
                return ScalarMask([[element / value for element in self.channels[0]]])

        class Backend:
            @staticmethod
            def clamp(mask, min, max):
                return ComparableScalarMask(
                    [[min if x < min else max if x > max else x for x in mask.channels[0]]]
                )

            @staticmethod
            def where(condition, one, zero):
                return ComparableScalarMask(
                    [[one if x else zero for x in condition]]
                )

        class ComparableScalarMask(ScalarMask):
            def __gt__(self, value):
                return [x > value for x in self.channels[0]]

            def __add__(self, value):
                return ComparableScalarMask(
                    [[element + value for element in self.channels[0]]]
                )

            def __truediv__(self, value):
                return ComparableScalarMask(
                    [[element / value for element in self.channels[0]]]
                )

            def __getitem__(self, key):
                return ComparableScalarMask(self.channels[:1])

        mask = ComparableScalarMask([[-1.0, 0.0, 0.1, 1.0], [1.0]])
        normalized, binary = normalize_and_binarize_mask(mask, Backend)

        self.assertEqual(normalized.channels, [[0.0, 0.5, 0.55, 1.0]])
        self.assertEqual(binary.channels, [[0.0, 0.0, 1.0, 1.0]])

    def test_atomic_writer_publishes_complete_json_without_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "source_validation.json"
            atomic_write_json(target, {"status": "passed"})
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")), {"status": "passed"}
            )
            self.assertEqual(list(target.parent.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
