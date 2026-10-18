"""Explicit RGB copies avoid dangling NumPy/QImage buffers."""

import numpy as np
from PySide6.QtGui import QImage


def image_to_array(image: QImage) -> np.ndarray:
    image = image.convertToFormat(QImage.Format.Format_RGB888)
    pixels = np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.bytesPerLine())
    return pixels[:, :image.width() * 3].reshape(image.height(), image.width(), 3).copy()


def array_to_image(pixels: np.ndarray) -> QImage:
    pixels = np.ascontiguousarray(pixels, dtype=np.uint8)
    h, w = pixels.shape[:2]
    return QImage(pixels.data, w, h, pixels.strides[0], QImage.Format.Format_RGB888).copy()
