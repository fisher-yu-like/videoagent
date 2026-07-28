"""
JD Cloud AI Gateway — Kling 视频生成调试脚本
文档: 视频生成API.pdf
用法:
    python kling_demo.py t2v                 # Kling-V2-5-Turbo 文生视频
    python kling_demo.py i2v <img_url>       # Kling-V2-5-Turbo 图生视频
    python kling_demo.py v3_t2v              # Kling-V3-omni 文生视频
    python kling_demo.py v3_i2v <img_url>    # Kling-V3-omni 图生视频
    python kling_demo.py omni <img_url>      # Kling-V3-omni 图片主体生成 (object_creation)
    python kling_demo.py download <task_id>  # 下载已完成任务的视频到本地
生成物默认下载到 ~/Downloads/kling/
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
# 可通过 JD_KLING_BASE 切换网关域名。默认走外网；
# 内网可用 http://ai-api.jdcloud.com
BASE = os.environ.get("JD_KLING_BASE", "https://modelservice.jdcloud.com").rstrip("/")
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
    # 注意：文档写的是 POST，实测查询接口是 GET。
    url = QUERY_URL_TMPL.format(task_id=task_id)
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = _request(url, "GET")
        status = resp.get("task_status") or (resp.get("result") or {}).get("task_status")
        print(f"[poll] status = {status}")
        if status in ("success", "failed", "cancelled"):
            return resp
        time.sleep(interval)
    raise TimeoutError(f"task {task_id} not finished within {timeout}s")


# ---------- 三种任务模板 ----------

def payload_t2v() -> dict:
    return {
        "model": "Kling-V2-5-Turbo",
        "content": [
            {"type": "text", "text": "生成两只跳跃的青蛙"},
            {"type": "negative_text", "text": "两只黑青蛙打架, 流血了"},
        ],
        "parameters": {"duration": 5, "mode": "std", "aspect_ratio": "16:9"},
    }


def payload_i2v(image_url: str) -> dict:
    return {
        "model": "Kling-V2-5-Turbo",
        "content": [
            {"type": "text", "text": "镜头转起来"},
            {"type": "image_url", "image_url": {"url": image_url}},
        ],
        "parameters": {"duration": 5, "mode": "pro"},
    }


def payload_omni(image_url: str) -> dict:
    # V3-omni 图片主体生成 (object_creation)
    return {
        "model": "Kling-V3-omni",
        "content": [
            {
                "type": "image_url",
                "image_url": {"url": image_url},
                "role": "object_creation",
            }
        ],
        "parameters": {
            "element_name": "女孩角色1",
            "element_description": "白衣服的女孩",
        },
    }


def payload_v3_t2v() -> dict:
    # Kling-V3-omni 文生视频（omni_video 场景）
    return {
        "model": "Kling-V3-omni",
        "content": [
            {"type": "text", "text": "视频中的人跳舞", "role": "omni_video"},
        ],
        "parameters": {
            "mode": "pro",
            "aspect_ratio": "16:9",
            "duration": "5",
        },
    }


def payload_v3_i2v(image_url: str) -> dict:
    # Kling-V3-omni 图生视频
    return {
        "model": "Kling-V3-omni",
        "content": [
            {"type": "text", "text": "镜头缓慢推进", "role": "omni_video"},
            {"type": "image_url", "image_url": {"url": image_url}},
        ],
        "parameters": {
            "mode": "pro",
            "aspect_ratio": "16:9",
            "duration": "5",
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
        # 生成物 URL 已带签名，直接 GET，无需鉴权头
        ext = ".mp4"
        fname = f"{task_id}_{idx}_{item_id}{ext}"
        path = os.path.join(DOWNLOAD_DIR, fname)
        print(f"[download] {url}\n        -> {path}")
        urllib.request.urlretrieve(url, path)
        size_mb = os.path.getsize(path) / 1024 / 1024
        print(f"[saved  ] {path}  ({size_mb:.2f} MB)")
        saved.append(path)
    return saved


def main():
    args = sys.argv[1:]
    dry_run = False
    if args and args[0] == "--dry-run":
        dry_run = True
        args = args[1:]
    mode = args[0] if args else "t2v"

    if mode == "download":
        if len(args) < 2:
            sys.exit("usage: python kling_demo.py download <task_id>")
        task_id = args[1]
        final = _request(QUERY_URL_TMPL.format(task_id=task_id), "GET")
        status = final.get("task_status")
        if status != "success":
            sys.exit(f"task not ready: status={status}")
        urls = _extract_video_urls(final)
        if not urls:
            sys.exit("no video url in response")
        download(urls, task_id)
        return

    if mode == "t2v":
        payload = payload_t2v()
    elif mode == "i2v":
        if len(args) < 2:
            sys.exit("usage: python kling_demo.py i2v <image_url>")
        payload = payload_i2v(args[1])
    elif mode == "v3_t2v":
        payload = payload_v3_t2v()
    elif mode == "v3_i2v":
        if len(args) < 2:
            sys.exit("usage: python kling_demo.py v3_i2v <image_url>")
        payload = payload_v3_i2v(args[1])
    elif mode == "omni":
        if len(args) < 2:
            sys.exit("usage: python kling_demo.py omni <image_url>")
        payload = payload_omni(args[1])
    else:
        sys.exit(f"unknown mode: {mode!r} (t2v | i2v | v3_t2v | v3_i2v | omni | download)")

    if dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

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
