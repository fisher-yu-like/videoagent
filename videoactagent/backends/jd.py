from __future__ import annotations


def build_kling_t2v(prompt: str, duration: int = 5) -> dict:
    return {
        "model": "Kling-V2-5-Turbo",
        "content": [{"type": "text", "text": prompt}],
        "parameters": {
            "duration": duration,
            "mode": "std",
            "aspect_ratio": "16:9",
        },
    }


def build_seedance_t2v(prompt: str, duration: int = 5) -> dict:
    return {
        "model": "Doubao-Seedance-2.0",
        "content": [{"type": "text", "text": prompt}],
        "parameters": {
            "ratio": "16:9",
            "resolution": "720p",
            "duration": duration,
            "watermark": False,
        },
    }


def build_seedance_first_last(
    prompt: str,
    first_url: str,
    last_url: str,
    duration: int = 5,
) -> dict:
    return {
        "model": "Doubao-Seedance-2.0",
        "content": [
            {"type": "text", "text": prompt},
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
            "duration": duration,
            "watermark": False,
        },
    }
