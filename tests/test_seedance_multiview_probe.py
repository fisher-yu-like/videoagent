from __future__ import annotations

import pytest


def test_multiview_probe_accepts_three_to_eight_without_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    from videoactagent.backends import jd
    import videoactagent.seedance_reference as reference

    monkeypatch.setattr(reference, "validate_remote_video_asset", lambda value: value)
    urls = [f"https://media.volccdn.com/camera_{i}.mp4" for i in range(1, 9)]
    request = jd.build_seedance_multiview_reference_videos(
        "shared world",
        urls,
        model="Doubao-Seedance-2.0",
    )
    assert [item["video_url"]["url"] for item in request["content"][1:]] == urls


def test_multiview_probe_rejects_two_or_nine(monkeypatch: pytest.MonkeyPatch) -> None:
    from videoactagent.backends import jd
    import videoactagent.seedance_reference as reference

    monkeypatch.setattr(reference, "validate_remote_video_asset", lambda value: value)
    for count in (2, 9):
        with pytest.raises(ValueError, match="between 3 and 8"):
            jd.build_seedance_multiview_reference_videos(
                "shared world",
                [f"https://media.volccdn.com/camera_{i}.mp4" for i in range(count)],
                model="Doubao-Seedance-2.0",
            )


def test_identity_anchor_request_accepts_two_explicit_video_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    from videoactagent.backends import jd
    import videoactagent.seedance_reference as reference

    monkeypatch.setattr(reference, "validate_remote_video_asset", lambda value: value)
    request = jd.build_seedance_reference_videos(
        "identity anchor plus assigned camera control",
        [
            "https://media.volccdn.com/canonical_identity.mp4",
            "https://media.volccdn.com/camera_1.mp4",
        ],
        model="Doubao-Seedance-2.0",
    )
    assert len(request["content"]) == 3
    assert [item["role"] for item in request["content"][1:]] == ["reference_video", "reference_video"]
