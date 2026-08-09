"""Local Proxy normalization and automatic public-URL preparation for Seedance.

This module deliberately stops before model generation.  It records the exact
local file, normalized media metadata, upload provider, URL and hashes so the
existing reference-video adapter can remain deterministic and auditable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import http.client
import json
import mimetypes
import os
from pathlib import Path
import shutil
import socket
import subprocess
import urllib.parse
import urllib.request
import uuid
import re


MIN_PIXELS = 640 * 640
MAX_VIDEO_BYTES = 200 * 1024 * 1024
MIN_DURATION_SECONDS = 2.0
MAX_DURATION_SECONDS = 15.0
MIN_FPS = 24.0
MAX_FPS = 60.0


class SeedanceUploadError(RuntimeError):
    """Raised when a Proxy cannot be made into a valid reference asset."""


@dataclass(frozen=True)
class UploadConfig:
    access_key: str = ""
    secret_key: str = ""
    bucket: str = ""
    endpoint: str = ""
    region: str = ""
    expires_seconds: int = 3600
    temp_upload_enabled: bool = False
    temp_upload_endpoint: str = "https://tmpfiles.org/api/v1/upload"
    temp_upload_provider: str = "tmpfiles"

    @property
    def has_tos_credentials(self) -> bool:
        return all((self.access_key, self.secret_key, self.bucket, self.endpoint, self.region))


@dataclass(frozen=True)
class NormalizedMedia:
    source_path: str
    normalized_path: str
    source_sha256: str
    normalized_sha256: str
    metadata: dict[str, object]


@dataclass(frozen=True)
class UploadedAsset:
    source_path: str
    normalized_path: str
    source_sha256: str
    normalized_sha256: str
    metadata: dict[str, object]
    url: str
    provider: str
    object_key: str
    expires_at: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _first_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def load_upload_config() -> UploadConfig:
    """Load upload settings without requiring credentials at import time."""
    try:
        expires = int(_first_env("TOS_URL_EXPIRES", "VIDEOACTAGENT_UPLOAD_EXPIRES") or "3600")
    except ValueError as exc:
        raise SeedanceUploadError("TOS_URL_EXPIRES must be an integer") from exc
    if expires < 300 or expires > 86400:
        raise SeedanceUploadError("TOS_URL_EXPIRES must be between 300 and 86400 seconds")
    return UploadConfig(
        access_key=_first_env("TOS_ACCESS_KEY", "VOLC_ACCESSKEY"),
        secret_key=_first_env("TOS_SECRET_KEY", "VOLC_SECRETKEY"),
        bucket=_first_env("TOS_BUCKET"),
        endpoint=_first_env("TOS_ENDPOINT") or "tos-cn-beijing.volces.com",
        region=_first_env("TOS_REGION") or "cn-beijing",
        expires_seconds=expires,
        temp_upload_enabled=_first_env("VIDEOACTAGENT_TEMP_UPLOAD").lower() in {"1", "true", "yes"},
        temp_upload_endpoint=_first_env("VIDEOACTAGENT_TEMP_UPLOAD_ENDPOINT")
        or "https://tmpfiles.org/api/v1/upload",
        temp_upload_provider=_first_env("VIDEOACTAGENT_TEMP_UPLOAD_PROVIDER").lower() or "tmpfiles",
    )


def sha256_file(path: Path | str) -> str:
    target = Path(path)
    if not target.is_file():
        raise SeedanceUploadError(f"media file does not exist: {target}")
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_fps(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and "/" in value:
        numerator, denominator = value.split("/", 1)
        try:
            return float(numerator) / float(denominator)
        except (ValueError, ZeroDivisionError) as exc:
            raise SeedanceUploadError(f"invalid FPS value: {value}") from exc
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise SeedanceUploadError(f"invalid FPS value: {value}") from exc


def validate_seedance_media(metadata: dict[str, object]) -> None:
    """Validate the official reference-video constraints used by this adapter."""
    try:
        width = int(metadata["width"])
        height = int(metadata["height"])
        fps = _parse_fps(metadata["fps"])
        duration = float(metadata["duration"])
        size = int(metadata["bytes"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SeedanceUploadError("media metadata is incomplete") from exc
    if width * height < MIN_PIXELS:
        raise SeedanceUploadError(
            f"media pixel count {width * height} is below Seedance minimum {MIN_PIXELS} pixels"
        )
    if not MIN_DURATION_SECONDS <= duration <= MAX_DURATION_SECONDS:
        raise SeedanceUploadError(f"media duration must be between {MIN_DURATION_SECONDS:g} and {MAX_DURATION_SECONDS:g} seconds")
    if not MIN_FPS <= fps <= MAX_FPS:
        raise SeedanceUploadError(f"media FPS must be between {MIN_FPS:g} and {MAX_FPS:g}")
    if size <= 0 or size > MAX_VIDEO_BYTES:
        raise SeedanceUploadError(f"media bytes must be in (0, {MAX_VIDEO_BYTES}]")


def validate_https_asset_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value.strip() if isinstance(value, str) else "")
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host or "." not in host:
        raise SeedanceUploadError("asset URL must use a public HTTPS hostname")
    if host in {"localhost", "example.com", "example.org", "example.net"} or host.endswith((".local", ".test", ".invalid")):
        raise SeedanceUploadError("asset URL must use a public HTTPS hostname")
    try:
        socket.gethostbyname(host)
    except OSError:
        # DNS is not required for deterministic unit validation; IP literals are.
        try:
            import ipaddress
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise SeedanceUploadError("asset URL must use a public HTTPS hostname")
    return value.strip()


def _tool_path(explicit: str | None, env_name: str, defaults: tuple[str, ...]) -> str:
    if explicit:
        return explicit
    configured = os.environ.get(env_name, "").strip()
    if configured:
        return configured
    for candidate in defaults:
        if Path(candidate).is_file():
            return candidate
    found = shutil.which(env_name.lower().replace("_path", ""))
    if found:
        return found
    raise SeedanceUploadError(f"{env_name} executable was not found")


def inspect_media(path: Path | str, *, ffprobe_path: str | None = None) -> dict[str, object]:
    target = Path(path)
    probe = _tool_path(
        ffprobe_path,
        "FFPROBE_PATH",
        ("D:\\ACLOS\\Cross\\recorder-release\\ffprobe.exe", "D:\\tools\\ffmpeg\\bin\\ffprobe.exe"),
    )
    command = [probe, "-v", "error", "-show_streams", "-show_format", "-print_format", "json", str(target)]
    try:
        completed = subprocess.run(command, check=True, capture_output=True, text=True)
        document = json.loads(completed.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise SeedanceUploadError(f"ffprobe failed for {target}: {exc}") from exc
    stream = next((item for item in document.get("streams", []) if item.get("codec_type") == "video"), None)
    if not isinstance(stream, dict):
        raise SeedanceUploadError(f"no video stream found in {target}")
    format_info = document.get("format", {}) if isinstance(document.get("format"), dict) else {}
    duration = float(stream.get("duration") or format_info.get("duration") or 0.0)
    metadata: dict[str, object] = {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "fps": _parse_fps(stream.get("r_frame_rate") or stream.get("avg_frame_rate") or 0),
        "duration": duration,
        "bytes": target.stat().st_size,
        "codec": stream.get("codec_name"),
    }
    return metadata


def choose_video_encoder(encoder_listing: str) -> str:
    """Choose an available H.264 encoder without assuming libx264 exists."""
    for candidate in ("libx264", "libo264rt", "h264_nvenc", "h264_mf"):
        if any(line.split()[1:2] == [candidate] for line in encoder_listing.splitlines()):
            return candidate
    raise SeedanceUploadError("ffmpeg has no usable H.264 encoder")


def normalize_proxy(
    source_path: Path | str,
    destination: Path | str,
    *,
    ffmpeg_path: str | None = None,
    ffprobe_path: str | None = None,
) -> NormalizedMedia:
    source = Path(source_path).resolve(strict=True)
    destination_path = Path(destination).resolve()
    if source == destination_path:
        raise SeedanceUploadError("normalized output must not overwrite the source Proxy")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = _tool_path(
        ffmpeg_path,
        "FFMPEG_PATH",
        ("D:\\ACLOS\\Cross\\recorder-release\\ffmpeg.exe", "D:\\tools\\ffmpeg\\bin\\ffmpeg.exe"),
    )
    try:
        encoder_probe = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"], check=True, capture_output=True, text=True
        )
        video_encoder = choose_video_encoder(encoder_probe.stdout)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SeedanceUploadError(f"cannot inspect ffmpeg encoders: {exc}") from exc
    command = [
        ffmpeg, "-y", "-i", str(source),
        "-vf", "scale=854:480:force_original_aspect_ratio=decrease,pad=854:480:(ow-iw)/2:(oh-ih)/2",
        "-r", "24", "-c:v", video_encoder, "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart",
        str(destination_path),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "")
        raise SeedanceUploadError(f"ffmpeg normalization failed: {detail or exc}") from exc
    metadata = inspect_media(destination_path, ffprobe_path=ffprobe_path)
    validate_seedance_media(metadata)
    return NormalizedMedia(
        source_path=str(source),
        normalized_path=str(destination_path),
        source_sha256=sha256_file(source),
        normalized_sha256=sha256_file(destination_path),
        metadata=metadata,
    )


def _upload_with_tos(media: NormalizedMedia, config: UploadConfig, run_id: str) -> UploadedAsset:
    if not config.has_tos_credentials:
        raise SeedanceUploadError("TOS upload requires TOS/VOLC access key, secret, bucket, endpoint and region")
    try:
        import tos
    except ImportError as exc:
        raise SeedanceUploadError("install the upload extra before using TOS upload: pip install -e .[upload]") from exc
    object_key = f"videoactagent/{run_id}/{media.normalized_sha256}.mp4"
    client = tos.TosClientV2(config.access_key, config.secret_key, config.endpoint, config.region)
    with Path(media.normalized_path).open("rb") as handle:
        client.put_object(config.bucket, object_key, content=handle)
    signed = client.pre_signed_url(
        tos.HttpMethodType.Http_Method_Get,
        bucket=config.bucket,
        key=object_key,
        expires=config.expires_seconds,
    )
    url = validate_https_asset_url(signed.signed_url)
    return UploadedAsset(
        source_path=media.source_path,
        normalized_path=media.normalized_path,
        source_sha256=media.source_sha256,
        normalized_sha256=media.normalized_sha256,
        metadata=media.metadata,
        url=url,
        provider="tos",
        object_key=object_key,
        expires_at=None,
    )


def _upload_with_tmpfiles(media: NormalizedMedia, config: UploadConfig) -> UploadedAsset:
    endpoint = urllib.parse.urlparse(config.temp_upload_endpoint)
    if endpoint.scheme != "https" or not endpoint.netloc:
        raise SeedanceUploadError("temporary upload endpoint must be HTTPS")
    boundary = uuid.uuid4().hex.encode("ascii")
    filename = Path(media.normalized_path).name.encode("utf-8")
    content = Path(media.normalized_path).read_bytes()
    body = b"--" + boundary + b"\r\n"
    body += b'Content-Disposition: form-data; name="file"; filename="' + filename + b'"\r\n'
    body += b"Content-Type: video/mp4\r\n\r\n" + content + b"\r\n--" + boundary + b"--\r\n"
    request = urllib.request.Request(
        config.temp_upload_endpoint,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary.decode()}",
            "Content-Length": str(len(body)),
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SeedanceUploadError(f"temporary upload failed: {exc}") from exc
    candidates: list[str] = []
    def collect(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"url", "download_url", "downloadUrl"} and isinstance(item, str):
                    candidates.append(item)
                else:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
    collect(result)
    if not candidates:
        raise SeedanceUploadError("temporary upload response did not contain a URL")
    # tmpfiles' documented `data.url` is a human-facing HTML page, not the
    # byte-serving URL.  Resolve its download anchor before handing the asset
    # to a model gateway; otherwise the gateway receives `text/html` and
    # reports an opaque `Invalid video_url` task failure.
    page_url = validate_https_asset_url(candidates[0])
    try:
        page_request = urllib.request.Request(
            page_url,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"},
            method="GET",
        )
        with urllib.request.urlopen(page_request, timeout=30) as response:
            html = response.read().decode("utf-8", errors="replace")
            page_url = response.geturl()
    except OSError as exc:
        raise SeedanceUploadError(f"temporary upload page could not be read: {exc}") from exc
    match = re.search(
        r'<a[^>]+class=["\']download["\'][^>]+href=["\']([^"\']+)["\']',
        html,
        flags=re.IGNORECASE,
    )
    if not match:
        raise SeedanceUploadError("temporary upload page did not contain a download URL")
    download_url = urllib.parse.urljoin(page_url, match.group(1))
    url = validate_https_asset_url(download_url)
    return UploadedAsset(
        source_path=media.source_path,
        normalized_path=media.normalized_path,
        source_sha256=media.source_sha256,
        normalized_sha256=media.normalized_sha256,
        metadata=media.metadata,
        url=url,
        provider="tmpfiles",
        object_key=Path(media.normalized_path).name,
        expires_at=None,
    )


def _upload_with_0x0(media: NormalizedMedia, config: UploadConfig) -> UploadedAsset:
    """Upload one normalized Proxy to 0x0.st and parse its plain-text URL.

    This is an explicitly selected research transport only.  It is kept
    separate from the tmpfiles parser because 0x0 returns a URL body rather
    than a JSON page that needs HTML-link resolution.
    """
    endpoint = config.temp_upload_endpoint
    if endpoint == "https://tmpfiles.org/api/v1/upload":
        endpoint = "https://0x0.st"
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.netloc:
        raise SeedanceUploadError("0x0 upload endpoint must be HTTPS")
    content = Path(media.normalized_path).read_bytes()
    boundary = uuid.uuid4().hex.encode("ascii")
    filename = Path(media.normalized_path).name.encode("utf-8")
    body = b"--" + boundary + b"\r\n"
    body += b'Content-Disposition: form-data; name="file"; filename="' + filename + b'"\r\n'
    body += b"Content-Type: video/mp4\r\n\r\n" + content + b"\r\n--" + boundary + b"--\r\n"
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary.decode()}",
            "Content-Length": str(len(body)),
            "User-Agent": "videoactagent-research-uploader/1.0",
            "Accept": "text/plain",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            text = response.read().decode("utf-8", errors="replace").strip()
    except OSError as exc:
        raise SeedanceUploadError(f"0x0 upload failed: {exc}") from exc
    candidate = next((line.strip() for line in text.splitlines() if line.strip().startswith("https://")), "")
    if not candidate:
        raise SeedanceUploadError(f"0x0 upload response did not contain an HTTPS URL: {text[:200]!r}")
    url = validate_https_asset_url(candidate)
    return UploadedAsset(
        source_path=media.source_path,
        normalized_path=media.normalized_path,
        source_sha256=media.source_sha256,
        normalized_sha256=media.normalized_sha256,
        metadata=media.metadata,
        url=url,
        provider="0x0",
        object_key=Path(media.normalized_path).name,
        expires_at=None,
    )


def _upload_with_uguu(media: NormalizedMedia, config: UploadConfig) -> UploadedAsset:
    """Upload one Proxy to Uguu's JSON multipart endpoint (research only)."""
    endpoint = config.temp_upload_endpoint
    if endpoint == "https://tmpfiles.org/api/v1/upload":
        endpoint = "https://uguu.se/upload.php"
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.netloc:
        raise SeedanceUploadError("Uguu upload endpoint must be HTTPS")
    boundary = uuid.uuid4().hex.encode("ascii")
    filename = Path(media.normalized_path).name.encode("utf-8")
    content = Path(media.normalized_path).read_bytes()
    body = b"--" + boundary + b"\r\n"
    body += b'Content-Disposition: form-data; name="files[]"; filename="' + filename + b'"\r\n'
    body += b"Content-Type: video/mp4\r\n\r\n" + content + b"\r\n--" + boundary + b"--\r\n"
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary.decode()}",
            "Content-Length": str(len(body)),
            "User-Agent": "videoactagent-research-uploader/1.0",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SeedanceUploadError(f"Uguu upload failed: {exc}") from exc
    candidates: list[str] = []
    if isinstance(result, dict):
        files = result.get("files")
        if isinstance(files, list):
            for item in files:
                if isinstance(item, dict) and isinstance(item.get("url"), str):
                    candidates.append(item["url"])
    if not candidates:
        raise SeedanceUploadError("Uguu upload response did not contain a URL")
    url = validate_https_asset_url(candidates[0])
    return UploadedAsset(
        source_path=media.source_path,
        normalized_path=media.normalized_path,
        source_sha256=media.source_sha256,
        normalized_sha256=media.normalized_sha256,
        metadata=media.metadata,
        url=url,
        provider="uguu",
        object_key=Path(media.normalized_path).name,
        expires_at=None,
    )


def upload_proxy(media: NormalizedMedia, *, run_id: str, config: UploadConfig | None = None) -> UploadedAsset:
    selected = config or load_upload_config()
    if selected.has_tos_credentials:
        return _upload_with_tos(media, selected, run_id)
    if selected.temp_upload_enabled:
        if selected.temp_upload_provider in {"0x0", "0x0.st", "0x0st"}:
            return _upload_with_0x0(media, selected)
        if selected.temp_upload_provider in {"uguu", "uguu.se"}:
            return _upload_with_uguu(media, selected)
        return _upload_with_tmpfiles(media, selected)
    raise SeedanceUploadError(
        "no upload backend is configured; set TOS credentials or explicitly set VIDEOACTAGENT_TEMP_UPLOAD=1"
    )


def write_upload_record(asset: UploadedAsset, output_path: Path | str) -> Path:
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(asset.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination
