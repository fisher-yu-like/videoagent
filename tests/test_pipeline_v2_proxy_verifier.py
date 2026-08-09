import json
from pathlib import Path
import shutil

import pytest

from pipeline_v2.proxy_verifier import FEEDBACK_CATEGORIES, ProxyVerifierError, verify_proxy
from pipeline_v2.revision_manager import (
    RevisionError,
    allocate_revision,
    build_handoff,
    record_feedback,
    record_approval,
    snapshot_bundle,
)


ROOT = Path(__file__).resolve().parents[1]
REAL_RUN = ROOT / "pipeline_v2/runs/module3_station_20260808_019"
REAL_WORLD = ROOT / "pipeline_v2/runs/module2_openai_station_20260808_004/world_state.json"
REAL_PLAN = ROOT / "pipeline_v2/runs/module2_openai_station_20260808_004/director_plan_normalized.json"
REAL_OUTPUT = REAL_RUN / "blender_run_001"
REAL_EVIDENCE = REAL_RUN / "evidence.json"
LEGACY_OUTPUT = ROOT / "pipeline_v2/runs/module3_station_20260808_011/blender_run_001"
LEGACY_RUN = ROOT / "pipeline_v2/runs/module3_station_20260808_011"


def real_ffprobe(path: Path) -> dict:
    # The integration test uses the same real ffprobe binary used for the
    # acceptance audit; no synthetic video metadata is injected.
    import subprocess

    ffprobe = Path(r"D:\ACLOS\Cross\recorder-release\ffprobe.exe")
    result = subprocess.run(
        [str(ffprobe), "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=nb_frames,r_frame_rate,width,height,duration", "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)["streams"][0]


def test_real_station_proxy_report_checks_all_plan_layers_and_media() -> None:
    report = verify_proxy(
        world_state_path=REAL_WORLD,
        render_output_dir=REAL_OUTPUT,
        director_plan_path=REAL_PLAN,
        code_agent_evidence_path=REAL_RUN / "evidence.json",
        generated_script_path=REAL_RUN / "generated_blender.py",
        probe_video=real_ffprobe,
    )

    assert report["schema_version"] == "proxy-verifier-1.0"
    assert report["verdict"] == "pending_review"
    assert report["required_human_review"] is True
    assert len(report["checks"]) >= 8
    assert all(check["status"] != "failed" for check in report["checks"])
    assert {item["camera_id"] for item in report["media"]["videos"]} == {
        "camera_1", "camera_2", "camera_3"
    }


def test_real_station_invalid_manifest_hash_is_a_failed_provenance_check() -> None:
    copied = ROOT / "pipeline_v2/runs/_test_verifier_manifest/run"
    shutil.rmtree(copied.parent, ignore_errors=True)
    try:
        shutil.copytree(REAL_OUTPUT, copied)
        manifest_path = copied / "render_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["world_state_hash"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        report = verify_proxy(world_state_path=REAL_WORLD, render_output_dir=copied)

        failed = [check for check in report["checks"] if check["status"] == "failed"]
        assert any(check["check_id"] == "provenance.world_state_hash" for check in failed)
        assert report["verdict"] == "fail"
    finally:
        shutil.rmtree(copied.parent, ignore_errors=True)


def test_legacy_real_station_logs_are_normalized_instead_of_crashing() -> None:
    report = verify_proxy(
        world_state_path=REAL_WORLD,
        render_output_dir=LEGACY_OUTPUT,
        director_plan_path=REAL_PLAN,
        code_agent_evidence_path=LEGACY_RUN / "evidence.json",
        generated_script_path=LEGACY_RUN / "generated_blender.py",
        probe_video=real_ffprobe,
    )
    assert report["verdict"] in {"fail", "pending_review"}
    assert any(check["check_id"] == "scene.entities" for check in report["checks"])


def test_feedback_categories_are_closed_and_route_only_appearance_to_edit_prompt() -> None:
    assert set(FEEDBACK_CATEGORIES) == {
        "scene_structure", "character_trajectory", "object_trajectory",
        "camera_trajectory", "physical_event", "appearance_only",
    }
    assert build_handoff({"category": "appearance_only", "message": "keep motion"})["route"] == "appearance_only_edit_prompt"
    assert build_handoff({"category": "camera_trajectory", "message": "reframe"})["route"] == "director_revision"
    with pytest.raises(RevisionError, match="category"):
        build_handoff({"category": "unknown", "message": "bad"})


def test_revision_manager_allocates_without_overwriting_and_persists_feedback() -> None:
    root = ROOT / "pipeline_v2/runs/_test_revision_manager"
    shutil.rmtree(root, ignore_errors=True)
    try:
        first = allocate_revision(root)
        second = allocate_revision(root, parent_revision=first.name)
        assert first.name == "revision_000"
        assert second.name == "revision_001"
        assert json.loads((second / "revision.json").read_text(encoding="utf-8"))["parent_revision"] == first.name

        feedback = {"category": "character_trajectory", "message": "traveler starts too far left"}
        record_feedback(second, feedback)
        assert json.loads((second / "feedback.json").read_text(encoding="utf-8")) == feedback
        with pytest.raises(RevisionError, match="already exists"):
            allocate_revision(root, revision_id="revision_001")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_snapshot_bundle_copies_real_proxy_outputs_and_hashes() -> None:
    root = ROOT / "pipeline_v2/runs/_test_revision_snapshot"
    shutil.rmtree(root, ignore_errors=True)
    try:
        revision = snapshot_bundle(root, REAL_OUTPUT)
        assert revision.name == "revision_000"
        assert (revision / "videos/camera_1.mp4").is_file()
        source_manifest = json.loads((revision / "source_manifest.json").read_text(encoding="utf-8"))
        assert source_manifest["files"]["videos/camera_1.mp4"]["bytes"] > 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_record_approval_persists_human_decision_without_overwriting_revision() -> None:
    root = ROOT / "pipeline_v2/runs/_test_human_approval"
    shutil.rmtree(root, ignore_errors=True)
    try:
        revision = allocate_revision(root)
        approval = record_approval(revision, reviewer="human", notes="station proxy approved")
        assert approval["status"] == "approved"
        assert approval["reviewer"] == "human"
        assert json.loads((revision / "approval.json").read_text(encoding="utf-8"))["notes"] == "station proxy approved"
        with pytest.raises(RevisionError, match="already exists"):
            record_approval(revision, reviewer="human", notes="second decision")
    finally:
        shutil.rmtree(root, ignore_errors=True)
