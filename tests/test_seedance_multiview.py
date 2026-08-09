from __future__ import annotations


def test_multiview_request_contains_stable_reference_video_roles() -> None:
    from videoactagent.backends.jd import build_seedance_multiview_reference_video

    request = build_seedance_multiview_reference_video(
        "[Video1] shared-world front view; [Video2] side view; [Video3] reverse view.",
        [
            "https://media.volccdn.com/camera_1.mp4",
            "https://media.volccdn.com/camera_2.mp4",
            "https://media.volccdn.com/camera_3.mp4",
        ],
        model="Doubao-Seedance-2.5",
        duration=5,
    )

    assert [item["video_url"]["url"] for item in request["content"][1:]] == [
        "https://media.volccdn.com/camera_1.mp4",
        "https://media.volccdn.com/camera_2.mp4",
        "https://media.volccdn.com/camera_3.mp4",
    ]
    assert all(item["type"] == "video_url" for item in request["content"][1:])
    assert all(item["role"] == "reference_video" for item in request["content"][1:])


def test_multiview_request_rejects_duplicate_or_missing_urls() -> None:
    import pytest
    from videoactagent.backends.jd import build_seedance_multiview_reference_video

    with pytest.raises(ValueError, match="exactly 3"):
        build_seedance_multiview_reference_video(
            "prompt",
            ["https://media.example.org/camera_1.mp4"],
            model="Doubao-Seedance-2.5",
            duration=5,
        )

    with pytest.raises(ValueError, match="duplicate"):
        build_seedance_multiview_reference_video(
            "prompt",
            [
                "https://media.volccdn.com/camera_1.mp4",
                "https://media.volccdn.com/camera_1.mp4",
                "https://media.volccdn.com/camera_3.mp4",
            ],
            model="Doubao-Seedance-2.5",
            duration=5,
        )
