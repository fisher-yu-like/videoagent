"""
JD Cloud AI Gateway — Doubao-Seedance 视频生成调试脚本
文档: 视频生成API.pdf（Doubao-Seedance-2.0 / 1.5-pro / 1-0-pro 系列）

用法:
    python seedance_demo.py t2v                      # 文生视频 (默认 Seedance-2.0)
    python seedance_demo.py i2v <image_url>          # 单图生视频
    python seedance_demo.py ff <first_url> <last_url>  # 首尾帧
    python seedance_demo.py multi <img_url> [audio_url]  # 多模态(仅 2.0)
    python seedance_demo.py download <task_id>       # 下载已完成任务

环境变量:
    JD_KLING_KEY   API key (默认已内置)
    JD_KLING_BASE  网关域名 (默认外网 https://modelservice.jdcloud.com)
    SEEDANCE_MODEL 模型名 (默认 Doubao-Seedance-2.0)
                   可选: Doubao-Seedance-2.0 / Doubao-Seedance-1.5-pro /
                        Doubao-seedance-1-0-pro-250528

生成视频默认下载到 ~/Downloads/kling/
"""
import json
import os
import sys
import time
import uuid
from typing import Optional
import urllib.request
import urllib.error

API_KEY = os.environ.get("JD_KLING_KEY", "").strip()
BASE = os.environ.get("JD_KLING_BASE", "https://modelservice.jdcloud.com").rstrip("/")
MODEL = os.environ.get("SEEDANCE_MODEL", "Doubao-Seedance-2.0")

SUBMIT_URL = f"{BASE}/v1/task/submit"
QUERY_URL_TMPL = f"{BASE}/v1/task/{{task_id}}"
DOWNLOAD_DIR = os.path.expanduser("~/Downloads/kling")


def _headers():
    if not API_KEY:
        raise RuntimeError("JD_KLING_KEY is required for API execution")
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
        "Trace-id": uuid.uuid4().hex,
    }


def _request(url: str, method: str = "POST", payload: Optional[dict] = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"[HTTP {e.code}] {body}")
        raise
    print(f"<< {method} {url}\n{body}\n")
    return json.loads(body)


def submit(payload: dict) -> str:
    print(f">> POST {SUBMIT_URL}\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n")
    resp = _request(SUBMIT_URL, "POST", payload)
    if resp.get("error"):
        raise RuntimeError(f"submit failed: {resp['error']}")
    task_id = resp["result"]["task_id"]
    print(f"[submitted] task_id = {task_id}")
    return task_id


def poll(task_id: str, interval: int = 15, timeout: int = 1800) -> dict:
    # 文档写的是 POST，实测查询接口是 GET。
    url = QUERY_URL_TMPL.format(task_id=task_id)
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = _request(url, "GET")
        status = resp.get("task_status")
        print(f"[poll] status = {status}")
        if status in ("success", "failed", "cancelled"):
            return resp
        time.sleep(interval)
    raise TimeoutError(f"task {task_id} not finished within {timeout}s")


# ---------- 任务模板 ----------

def payload_t2v() -> dict:
    return {
        "model": MODEL,
        "content": [
            {
                "type": "text",
                "text": "写实风格，晴朗的蓝天之下，一大片白色的雏菊花田，"
                        "镜头逐渐拉近，最终定格在一朵雏菊花的特写上，"
                        "花瓣上有几颗晶莹的露珠",
            }
        ],
        "parameters": {
            "ratio": "16:9",     # 或 adaptive / 4:3 / 1:1 / 3:4 / 9:16 / 21:9
            "resolution": "720p", # 480p / 720p / 1080p
            "duration": 5,        # 2 ~ 12 秒
            "watermark": False,
        },
    }


def payload_i2v(image_url: str) -> dict:
    return {
        "model": MODEL,
        "content": [
            {"type": "text", "text": "镜头缓慢推进，人物自然呼吸"},
            {"type": "image_url", "image_url": {"url": image_url}},
        ],
        "parameters": {
            "ratio": "adaptive",
            "resolution": "720p",
            "duration": 5,
            "watermark": False,
        },
    }


def payload_first_last(first_url: str, last_url: str) -> dict:
    return {
        "model": MODEL,
        "content": [
            {"type": "text", "text": "从首帧过渡到尾帧，运镜平滑"},
            {
                "type": "image_url",
                "image_url": {"url": first_url},
                "role": "first_frame",
            },
            {
                "type": "image_url",
                "image_url": {"url": last_url},
                "role": "last_frame",
            },
        ],
        "parameters": {
            "ratio": "adaptive",
            "resolution": "720p",
            "duration": 5,
            "watermark": False,
        },
    }


def payload_multimodal(image_url: str, audio_url: Optional[str] = None) -> dict:
    """多模态：仅 Doubao-Seedance-2.0 支持。图片作为 reference_image，
    可选叠加参考音频。"""
    content = [
        {"type": "text", "text": "根据参考素材生成一段视频"},
        {
            "type": "image_url",
            "image_url": {"url": image_url},
            "role": "reference_image",
        },
    ]
    if audio_url:
        content.append(
            {
                "type": "audio_url",
                "audio_url": {"url": audio_url},
                "role": "reference_audio",
            }
        )
    return {
        "model": "Doubao-Seedance-2.0",
        "content": content,
        "parameters": {
            "ratio": "16:9",
            "resolution": "720p",
            "duration": 5,
            "generate_audio": bool(audio_url),
            "watermark": False,
        },
    }


# ---------- 下载 ----------

def _extract_video_urls(final: dict) -> list:
    urls = []
    for item in final.get("content") or []:
        u = (item.get("video_url") or {}).get("url")
        if u:
            urls.append((item.get("id") or "video", u))
    return urls


def download(urls, task_id: str) -> list:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    saved = []
    for idx, (item_id, url) in enumerate(urls):
        fname = f"{task_id}_{idx}_{item_id}.mp4"
        path = os.path.join(DOWNLOAD_DIR, fname)
        print(f"[download] {url}\n        -> {path}")
        urllib.request.urlretrieve(url, path)
        size_mb = os.path.getsize(path) / 1024 / 1024
        print(f"[saved  ] {path}  ({size_mb:.2f} MB)")
        saved.append(path)
    return saved


def main():
    args = sys.argv[1:]
    mode = args[0] if args else "t2v"

    if mode == "download":
        if len(args) < 2:
            sys.exit("usage: python seedance_demo.py download <task_id>")
        task_id = args[1]
        final = _request(QUERY_URL_TMPL.format(task_id=task_id), "GET")
        if final.get("task_status") != "success":
            sys.exit(f"task not ready: status={final.get('task_status')}")
        urls = _extract_video_urls(final)
        if not urls:
            sys.exit("no video url in response")
        download(urls, task_id)
        return

    if mode == "t2v":
        payload = payload_t2v()
    elif mode == "i2v":
        if len(args) < 2:
            sys.exit("usage: python seedance_demo.py i2v <image_url>")
        payload = payload_i2v(args[1])
    elif mode == "ff":
        if len(args) < 3:
            sys.exit("usage: python seedance_demo.py ff <first_url> <last_url>")
        payload = payload_first_last(args[1], args[2])
    elif mode == "multi":
        if len(args) < 2:
            sys.exit("usage: python seedance_demo.py multi <image_url> [audio_url]")
        payload = payload_multimodal(args[1], args[2] if len(args) > 2 else None)
    else:
        sys.exit(f"unknown mode: {mode!r} (t2v | i2v | ff | multi | download)")

    task_id = submit(payload)
    final = poll(task_id)
    urls = _extract_video_urls(final)
    for _, u in urls:
        print(f"[result] {u}")
    err = final.get("error") or {}
    if err.get("message"):
        print(f"[error] {err}")
    if urls:
        download(urls, task_id)


if __name__ == "__main__":
    main()
