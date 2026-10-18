"""Pixel-only stitching; deliberately independent of Qt and the desktop.

Frames are RGB uint8 arrays. Only validated downward translations advance the
reference frame. Rejected frames can never damage an already accepted capture.
"""

from dataclasses import dataclass
from enum import Enum
import math

import cv2
import numpy as np


class Status(str, Enum):
    ACCEPTED = "accepted"
    UNCHANGED = "unchanged"
    REJECTED = "rejected"
    LIMIT = "limit"


@dataclass(frozen=True)
class StitchResult:
    status: Status
    added_rows: int
    total_rows: int
    message: str
    replaced_rows: int = 0  # Trailing accepted rows refreshed from the new frame.


def frame_is_stable(previous: np.ndarray, current: np.ndarray) -> bool:
    """Allow tiny local animation, but wait for scroll movement to settle."""
    if previous.shape != current.shape:
        return False
    diff = np.abs(previous[::3, ::3].astype(np.int16) - current[::3, ::3])
    return bool(np.mean(np.max(diff, axis=2) > 12) < 0.015)


class VerticalStitcher:
    def __init__(self, *, max_pixels: int = 50_000_000, max_height: int = 50_000):
        self.max_pixels = max_pixels
        self.max_height = max_height
        self._last: np.ndarray | None = None
        self._strips: list[np.ndarray] = []
        self.height = 0
        self.width = 0

    def _result(self, status: Status, message: str, added: int = 0, replaced: int = 0) -> StitchResult:
        return StitchResult(status, added, self.height, message, replaced)

    def _trim_tail(self, rows: int) -> None:
        index = len(self._strips) - 1
        while rows >= len(self._strips[index]):
            rows -= len(self._strips[index])
            index -= 1
        # Copy before changing the list. A view here would retain an entire old
        # overlap allocation for each tiny scroll step and grow memory rapidly.
        kept = self._strips[index][:-rows].copy() if rows else None
        del self._strips[index + 1:]
        if kept is not None:
            self._strips[index] = kept

    def append(self, frame: np.ndarray) -> StitchResult:
        if frame.dtype != np.uint8 or frame.ndim != 3 or frame.shape[2] != 3:
            raise ValueError("Expected an RGB uint8 image")
        h, w = frame.shape[:2]
        if min(h, w) < 64:
            raise ValueError("Capture area must be at least 64 × 64 physical pixels")
        if self._last is None:
            if h > self.max_height or w * h > self.max_pixels:
                return self._result(Status.LIMIT, "Selected area exceeds the capture size limit.")
            self._last = frame.copy()
            self._strips = [self._last]
            self.height, self.width = h, w
            return self._result(Status.ACCEPTED, "Ready — scroll downward slowly.", h)
        if frame.shape != self._last.shape:
            return self._result(Status.REJECTED, "Capture dimensions changed. Finish and start again.")

        old = self._last
        if frame_is_stable(old, frame):
            return self._result(Status.UNCHANGED, "Scroll downward; keep at least 25% overlap.")

        # Ignore narrow edge bands in alignment (e.g. a scrollbar). Output retains
        # every pixel. Reduce width only, preserving exact vertical offsets.
        margin = max(2, min(12, w // 20))
        gray_old = cv2.cvtColor(old[:, margin:-margin], cv2.COLOR_RGB2GRAY)
        gray_new = cv2.cvtColor(frame[:, margin:-margin], cv2.COLOR_RGB2GRAY)
        match_width = min(gray_old.shape[1], 320)
        old_small = cv2.resize(gray_old, (match_width, h), interpolation=cv2.INTER_AREA)
        new_small = cv2.resize(gray_new, (match_width, h), interpolation=cv2.INTER_AREA)
        min_overlap = max(16, math.ceil(h * 0.25))
        edge_guard = max(2, min(16, h // 16))
        template_height = min_overlap - 2 * edge_guard
        template_start = h - edge_guard - template_height
        template = old_small[template_start:h - edge_guard]
        if float(template.std()) < 3:
            return self._result(Status.REJECTED, "Too little detail to align. Scroll back to the last captured content.")

        # matchTemplate is a one-dimensional search because widths are equal.
        scores = cv2.matchTemplate(new_small[edge_guard:h - edge_guard], template, cv2.TM_CCOEFF_NORMED).ravel()
        position = int(np.argmax(scores))
        score = float(scores[position])
        shift = template_start - (edge_guard + position)
        alternatives = scores.copy()
        alternatives[max(0, position - 3): position + 4] = -1
        if score < 0.94 or score - float(alternatives.max(initial=-1)) < 0.025:
            return self._result(Status.REJECTED, "Cannot align safely. Scroll back, then downward in smaller steps.")
        if shift <= 0:
            return self._result(Status.REJECTED, "No downward movement found. Keep the app and selection still.")

        # Validate the overlap interior. Viewport borders, clipping shadows and
        # rounded corners at the top/bottom are not scrolling document content.
        overlap = h - shift
        a = old[shift + edge_guard:h - edge_guard, margin:-margin].astype(np.int16)
        b = frame[edge_guard:overlap - edge_guard, margin:-margin].astype(np.int16)
        difference = np.max(np.abs(a - b), axis=2)
        for band in np.array_split(difference, 3, axis=0):
            if np.mean(band <= 20) < 0.96 or np.mean(band) > 5:
                return self._result(Status.REJECTED, "Content moved or changed. Return to overlapping content and try again.")
        if self.height + shift > self.max_height or (self.height + shift) * w > self.max_pixels:
            return self._result(Status.LIMIT, "Capture size limit reached. Your accepted image is ready to export.")

        # Join inside a matching band near the overlap's center. Replace the old
        # tail so its viewport edge cannot be stamped into every join. Never
        # blend pixels: that would hide alignment errors by blurring text.
        window = max(3, min(12, len(difference) // 4))
        errors = np.convolve(difference.mean(axis=1), np.ones(window) / window, mode="valid")
        centers = np.arange(len(errors)) + edge_guard + window // 2
        seam = int(centers[np.argmin(errors + np.abs(centers - overlap / 2) * 0.0001)])
        replaced = overlap - seam
        new_strip = frame[seam:].copy()
        new_reference = frame.copy()
        self._trim_tail(replaced)
        self._strips.append(new_strip)
        self._last = new_reference
        self.height += shift
        return self._result(Status.ACCEPTED, "Capturing — scroll downward slowly.", shift, replaced)

    def image(self) -> np.ndarray:
        if not self._strips:
            raise ValueError("No frames captured")
        return np.concatenate(self._strips, axis=0)
