from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Translation:
    dx: float
    dy: float
    confidence: float


def estimate_translation(reference: np.ndarray, observed: np.ndarray) -> Translation:
    reference = np.asarray(reference, dtype=np.float64)
    observed = np.asarray(observed, dtype=np.float64)
    if reference.ndim != 2 or observed.ndim != 2:
        raise ValueError("phase correlation inputs must be 2D grayscale arrays")
    if reference.shape != observed.shape:
        raise ValueError("phase correlation inputs must have equal dimensions")

    height, width = reference.shape
    window = np.outer(np.hanning(height), np.hanning(width))
    reference_windowed = (reference - reference.mean()) * window
    observed_windowed = (observed - observed.mean()) * window
    reference_fft = np.fft.fft2(reference_windowed)
    observed_fft = np.fft.fft2(observed_windowed)
    cross_power = observed_fft * np.conj(reference_fft)
    magnitude = np.abs(cross_power)
    cross_power /= np.maximum(magnitude, np.finfo(np.float64).eps)
    correlation = np.abs(np.fft.ifft2(cross_power))

    peak_y, peak_x = np.unravel_index(np.argmax(correlation), correlation.shape)
    dy = float(peak_y if peak_y <= height // 2 else peak_y - height)
    dx = float(peak_x if peak_x <= width // 2 else peak_x - width)
    flattened = correlation.ravel()
    if flattened.size > 1:
        top_two = np.partition(flattened, -2)[-2:]
        confidence = float(top_two.max() / max(top_two.min(), 1e-12))
    else:
        confidence = 1.0
    return Translation(dx=dx, dy=dy, confidence=confidence)
