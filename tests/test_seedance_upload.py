from __future__ import annotations

import hashlib
from pathlib import Path

import pytest


def test_sha256_file_returns_digest_for_local_media(tmp_path: Path) -> None:
    from videoactagent.seedance_upload import sha256_file

    path = tmp_path / "proxy.mp4"
    path.write_bytes(b"real-test-bytes")

    assert sha256_file(path) == hashlib.sha256(b"real-test-bytes").hexdigest()


def test_load_upload_config_accepts_volcengine_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    from videoactagent.seedance_upload import load_upload_config

    monkeypatch.setenv("VOLC_ACCESSKEY", "ak")
    monkeypatch.setenv("VOLC_SECRETKEY", "sk")
    monkeypatch.setenv("TOS_BUCKET", "bucket")
    monkeypatch.setenv("TOS_ENDPOINT", "tos-cn-beijing.volces.com")
    monkeypatch.setenv("TOS_REGION", "cn-beijing")

    config = load_upload_config()

    assert config.access_key == "ak"
    assert config.secret_key == "sk"
    assert config.bucket == "bucket"
    assert config.endpoint == "tos-cn-beijing.volces.com"
    assert config.region == "cn-beijing"


def test_validate_seedance_media_accepts_normalized_proxy() -> None:
    from videoactagent.seedance_upload import validate_seedance_media

    validate_seedance_media(
        {"width": 854, "height": 480, "fps": 24.0, "duration": 5.0, "bytes": 10_000}
    )


def test_validate_seedance_media_rejects_640x360_proxy() -> None:
    from videoactagent.seedance_upload import SeedanceUploadError, validate_seedance_media

    with pytest.raises(SeedanceUploadError, match="pixel"):
        validate_seedance_media(
            {"width": 640, "height": 360, "fps": 24.0, "duration": 5.0, "bytes": 10_000}
        )


def test_validate_https_asset_url_rejects_localhost() -> None:
    from videoactagent.seedance_upload import SeedanceUploadError, validate_https_asset_url

    with pytest.raises(SeedanceUploadError, match="public HTTPS"):
        validate_https_asset_url("http://localhost/proxy.mp4")


def test_choose_video_encoder_uses_available_h264_encoder() -> None:
    from videoactagent.seedance_upload import choose_video_encoder

    output = " V..... libo264rt O264rt H.264 / AVC\n V....D mpeg4 MPEG-4 part 2\n"

    assert choose_video_encoder(output) == "libo264rt"


def test_temp_upload_sends_browser_user_agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import io

    from videoactagent.seedance_upload import NormalizedMedia, UploadConfig, _upload_with_tmpfiles

    path = tmp_path / "proxy.mp4"
    path.write_bytes(b"proxy")
    media = NormalizedMedia(
        source_path="source.mp4",
        normalized_path=str(path),
        source_sha256="a" * 64,
        normalized_sha256="b" * 64,
        metadata={"width": 854, "height": 480, "fps": 24.0, "duration": 5.0, "bytes": 5},
    )

    class Response:
        def __init__(self, body: bytes, url: str):
            self.body = body
            self._url = url

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return self.body

        def geturl(self):
            return self._url

    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        assert request.get_header("User-agent") == "Mozilla/5.0"
        if len(calls) == 1:
            return Response(
                b'{"status":"success","data":{"url":"https://tmpfiles.org/id/proxy.mp4"}}',
                "https://tmpfiles.org/id/proxy.mp4",
            )
        return Response(
            b'<a class="download" href="https://tmpfiles.org/dl/id/proxy.mp4">Download</a>',
            "https://tmpfiles.org/id/proxy.mp4",
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    asset = _upload_with_tmpfiles(media, UploadConfig(temp_upload_enabled=True))
    assert asset.url.endswith("/dl/id/proxy.mp4")


def test_0x0_upload_parses_plain_text_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from videoactagent.seedance_upload import NormalizedMedia, UploadConfig, _upload_with_0x0

    path = tmp_path / "proxy.mp4"
    path.write_bytes(b"proxy")
    media = NormalizedMedia(
        source_path="source.mp4",
        normalized_path=str(path),
        source_sha256="a" * 64,
        normalized_sha256="b" * 64,
        metadata={"width": 854, "height": 480, "fps": 24.0, "duration": 5.0, "bytes": 5},
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b"https://0x0.st/abc.mp4\n"

    def fake_urlopen(request, timeout):
        assert request.full_url == "https://0x0.st"
        assert request.get_header("User-agent") == "videoactagent-research-uploader/1.0"
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    asset = _upload_with_0x0(media, UploadConfig(temp_upload_enabled=True))
    assert asset.provider == "0x0"
    assert asset.url == "https://0x0.st/abc.mp4"


def test_uguu_upload_parses_json_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from videoactagent.seedance_upload import NormalizedMedia, UploadConfig, _upload_with_uguu

    path = tmp_path / "proxy.mp4"
    path.write_bytes(b"proxy")
    media = NormalizedMedia(
        source_path="source.mp4",
        normalized_path=str(path),
        source_sha256="a" * 64,
        normalized_sha256="b" * 64,
        metadata={"width": 854, "height": 480, "fps": 24.0, "duration": 5.0, "bytes": 5},
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{"success":true,"files":[{"url":"https://d.uguu.se/proxy.mp4"}]}'

    def fake_urlopen(request, timeout):
        assert request.full_url == "https://uguu.se/upload.php"
        assert request.get_header("User-agent") == "videoactagent-research-uploader/1.0"
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    asset = _upload_with_uguu(media, UploadConfig(temp_upload_enabled=True))
    assert asset.provider == "uguu"
    assert asset.url == "https://d.uguu.se/proxy.mp4"
