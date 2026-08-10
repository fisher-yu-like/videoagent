from __future__ import annotations

from pathlib import Path


def _asset(url: str, camera: str):
    from videoactagent.seedance_upload import UploadedAsset

    return UploadedAsset(
        source_path=f"{camera}.mp4",
        normalized_path=f"normalized/{camera}.mp4",
        source_sha256="a" * 64,
        normalized_sha256="b" * 64,
        metadata={"width": 854, "height": 480, "fps": 24.0, "duration": 5.0, "bytes": 100},
        url=url,
        provider="test",
        object_key=f"{camera}.mp4",
        expires_at=None,
    )


def test_build_multiview_job_binds_prompt_and_three_upload_records(tmp_path: Path) -> None:
    from videoactagent.seedance_auto_chain import build_multiview_job

    assets = [
        _asset("https://media.volccdn.com/camera_1.mp4", "camera_1"),
        _asset("https://media.volccdn.com/camera_2.mp4", "camera_2"),
        _asset("https://media.volccdn.com/camera_3.mp4", "camera_3"),
    ]
    job = build_multiview_job(
        job_id="G1",
        prompt="[Video1] front; [Video2] side; [Video3] reverse",
        assets=assets,
        model="Doubao-Seedance-2.5",
    )

    assert job["job_id"] == "G1"
    assert job["request"]["content"][0]["text"].startswith("[Video1]")
    assert len(job["request"]["content"][1:]) == 3
    assert [item["url"] for item in job["uploads"]] == [asset.url for asset in assets]
    assert job["request_sha256"]


def test_build_single_camera_job_has_exactly_one_reference_video() -> None:
    from videoactagent.seedance_auto_chain import build_single_camera_job

    asset = _asset("https://media.volccdn.com/master.mp4", "master")
    job = build_single_camera_job(
        job_id="scene_master",
        camera_id="master",
        prompt="appearance-only master camera",
        asset=asset,
        model="Doubao-Seedance-2.0",
    )

    assert job["camera_id"] == "master"
    assert len(job["request"]["content"]) == 2
    assert job["request"]["content"][1]["video_url"]["url"] == asset.url
    assert job["request"]["content"][1]["role"] == "reference_video"


def test_build_single_camera_job_can_bind_canonical_identity_anchor() -> None:
    from videoactagent.seedance_auto_chain import build_single_camera_job

    anchor = _asset("https://media.volccdn.com/identity_anchor.mp4", "master")
    camera = _asset("https://media.volccdn.com/lateral.mp4", "lateral")
    job = build_single_camera_job(
        job_id="scene_lateral",
        camera_id="lateral",
        prompt="appearance-only lateral camera with shared identity anchor",
        asset=camera,
        identity_anchor=anchor,
        model="Doubao-Seedance-2.0",
    )

    refs = job["request"]["content"][1:]
    assert [item["video_url"]["url"] for item in refs] == [anchor.url, camera.url]
    assert job["reference_assets"][0]["url"] == anchor.url
    assert job["reference_assets"][1]["url"] == camera.url


def test_build_single_camera_job_can_put_camera_plate_first() -> None:
    from videoactagent.seedance_auto_chain import build_single_camera_job

    anchor = _asset("https://media.volccdn.com/identity_anchor.mp4", "master")
    camera = _asset("https://media.volccdn.com/elevated.mp4", "elevated")
    job = build_single_camera_job(
        job_id="scene_elevated",
        camera_id="elevated",
        prompt="camera-first elevated view",
        asset=camera,
        identity_anchor=anchor,
        identity_anchor_first=False,
        model="Doubao-Seedance-2.0",
    )

    refs = job["request"]["content"][1:]
    assert [item["video_url"]["url"] for item in refs] == [camera.url, anchor.url]


def test_camera_jobs_submit_one_task_per_asset(monkeypatch) -> None:
    import videoactagent.seedance_auto_chain as chain

    assets = [_asset("https://media.volccdn.com/a.mp4", "master"), _asset("https://media.volccdn.com/b.mp4", "lateral")]
    calls = []

    def fake_run(**kwargs):
        calls.append(kwargs["job_id"])
        return {"status": "succeeded", "api_calls": {"submit": 1, "query": 1, "download": 1}, "camera_id": kwargs["camera_id"]}

    monkeypatch.setattr(chain, "run_uploaded_single_camera_job", fake_run)
    result = chain.run_uploaded_camera_jobs(
        job_id="scene",
        prompt="appearance-only",
        assets=assets,
        camera_ids=["master", "lateral"],
        output_root="runs/results/test-camera-jobs",
        api_key="key",
        base_url="https://gateway.example",
        model="Doubao-Seedance-2.0",
    )

    assert calls == ["scene_master", "scene_lateral"]
    assert result["status"] == "succeeded"
    assert result["api_calls"] == {"submit": 2, "query": 2, "download": 2}


def test_camera_jobs_pass_shared_identity_anchor_to_each_task(monkeypatch) -> None:
    import videoactagent.seedance_auto_chain as chain

    assets = [_asset("https://media.volccdn.com/a.mp4", "master"), _asset("https://media.volccdn.com/b.mp4", "lateral")]
    anchor = assets[0]
    received = []

    def fake_run(**kwargs):
        received.append(kwargs["identity_anchor"])
        return {"status": "succeeded", "api_calls": {"submit": 1, "query": 1, "download": 1}, "camera_id": kwargs["camera_id"]}

    monkeypatch.setattr(chain, "run_uploaded_single_camera_job", fake_run)
    chain.run_uploaded_camera_jobs(
        job_id="scene",
        prompt="appearance-only",
        assets=assets,
        camera_ids=["master", "lateral"],
        output_root="runs/results/test-camera-jobs-anchor",
        api_key="key",
        base_url="https://gateway.example",
        model="Doubao-Seedance-2.0",
        identity_anchor=anchor,
    )

    assert received == [anchor, anchor]
