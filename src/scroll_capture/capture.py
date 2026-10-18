"""Screen selection and bounded, asynchronous manual scrolling capture."""

from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QObject, QPoint, QRect, QRectF, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QImage, QPainter, QPen, QScreen
from PySide6.QtWidgets import QWidget

from .images import array_to_image, image_to_array
from .stitching import Status, VerticalStitcher, frame_is_stable


@dataclass(frozen=True)
class ScreenRegion:
    screen: QScreen
    rect: QRect  # Screen-local logical coordinates, as required by QScreen on X11.

    def grab(self) -> QImage:
        image = self.screen.grabWindow(0, self.rect.x(), self.rect.y(), self.rect.width(), self.rect.height()).toImage()
        if image.isNull():
            raise RuntimeError("The screen could not be captured. Check that you are using an X11 session.")
        image.setDevicePixelRatio(1.0)  # Editor works in native physical pixels.
        return image

    def global_rect(self) -> QRect:
        return self.rect.translated(self.screen.geometry().topLeft())


class SelectionOverlay(QWidget):
    chosen = Signal(object)
    cancelled = Signal()

    def __init__(self, screen: QScreen, snapshot, scrolling: bool):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.target_screen = screen
        self.snapshot = snapshot
        self.scrolling = scrolling
        self.anchor: QPoint | None = None
        self.selection = QRect()
        self.setGeometry(screen.geometry())
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setWindowTitle("Select capture area")

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.drawPixmap(self.rect(), self.snapshot)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 135))
        if not self.selection.isEmpty():
            painter.save()
            painter.setClipRect(self.selection)
            painter.drawPixmap(self.rect(), self.snapshot)
            painter.restore()
            painter.setPen(QPen(QColor("#5ef0ba"), 2))
            painter.drawRect(self.selection.adjusted(0, 0, -1, -1))
        painter.setPen(QColor("white"))
        font = painter.font()
        font.setPointSize(12)
        painter.setFont(font)
        text = "Drag to select · Escape to cancel"
        if self.scrolling:
            text += "\nSelect inside the scrolling content, excluding fixed headers and scrollbars."
        if not self.selection.isEmpty():
            text += f"\n{self.selection.width()} × {self.selection.height()}"
        panel = QRect(24, 24, min(self.width() - 48, 730), 108)
        painter.fillRect(panel, QColor(20, 29, 34, 230))
        painter.drawText(panel.adjusted(16, 12, -16, -12), Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap, text)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self.cancelled.emit()
        elif event.button() == Qt.MouseButton.LeftButton:
            self.anchor = event.position().toPoint()
            self.selection = QRect()

    def mouseMoveEvent(self, event):
        if self.anchor is not None:
            point = event.position().toPoint()
            point.setX(max(0, min(self.width() - 1, point.x())))
            point.setY(max(0, min(self.height() - 1, point.y())))
            self.selection = QRect(self.anchor, point).normalized().intersected(self.rect())
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self.anchor is None:
            return
        self.mouseMoveEvent(event)
        self.anchor = None
        dpr = self.target_screen.devicePixelRatio()
        if self.selection.width() * dpr >= 64 and self.selection.height() * dpr >= 64:
            self.chosen.emit(ScreenRegion(self.target_screen, QRect(self.selection)))
        else:
            self.selection = QRect()
            self.update()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()


class RegionSelector(QObject):
    selected = Signal(object)
    cancelled = Signal()

    def __init__(self, scrolling: bool, parent: QObject | None = None):
        super().__init__(parent)
        # Grab every screen before showing any overlays.
        snapshots = [(screen, screen.grabWindow(0)) for screen in QGuiApplication.screens()]
        self.overlays = [SelectionOverlay(screen, snapshot, scrolling) for screen, snapshot in snapshots]
        self._closed = False
        for overlay in self.overlays:
            overlay.chosen.connect(self._choose)
            overlay.cancelled.connect(self.cancel)

    def show(self):
        for overlay in self.overlays:
            overlay.show()
        under_mouse = QGuiApplication.screenAt(QCursor.pos())
        for overlay in self.overlays:
            if overlay.target_screen == under_mouse:
                overlay.activateWindow()
                overlay.setFocus()

    def _close(self):
        self._closed = True
        for overlay in self.overlays:
            overlay.close()
            overlay.deleteLater()
        self.overlays.clear()

    def _choose(self, region):
        if not self._closed:
            self._close()
            self.selected.emit(region)

    def cancel(self):
        if not self._closed:
            self._close()
            self.cancelled.emit()


class StitchWorker(QObject):
    processed = Signal(object)
    preview_updated = Signal(QImage)
    finished = Signal(object, str)

    def __init__(self):
        super().__init__()
        self.stitcher = VerticalStitcher()
        self.previous: np.ndarray | None = None
        self.closed = False
        self.preview = QImage()
        self.preview_rows = 0

    def _update_preview(self, pixels, result):
        # Build from newly accepted strips rather than assembling the full-sized
        # screenshot on every update. The display copy is bounded to 320 × 4096.
        scale = min(1.0, 320 / pixels.shape[1], 4096 / result.total_rows)
        width = max(1, round(pixels.shape[1] * scale))
        height = max(1, round(result.total_rows * scale))
        kept_rows = result.total_rows - result.added_rows - result.replaced_rows
        previous_height = round(kept_rows * scale)
        preview = QImage(width, height, QImage.Format.Format_RGB32)
        preview.fill(Qt.GlobalColor.white)
        painter = QPainter(preview)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if previous_height and not self.preview.isNull():
            source_height = self.preview.height() * kept_rows / self.preview_rows
            painter.drawImage(QRectF(0, 0, width, previous_height), self.preview,
                              QRectF(0, 0, self.preview.width(), source_height))
        if height > previous_height:
            strip = array_to_image(pixels[-(result.added_rows + result.replaced_rows):])
            painter.drawImage(QRect(0, previous_height, width, height - previous_height), strip)
        painter.end()
        self.preview = preview
        self.preview_rows = result.total_rows
        self.preview_updated.emit(preview)

    @Slot(object)
    def process(self, pixels):
        if self.closed:
            return
        try:
            if self.previous is not None and not frame_is_stable(self.previous, pixels):
                self.previous = pixels
                self.processed.emit(None)
                return
            self.previous = pixels
            result = self.stitcher.append(pixels)
            if result.status == Status.ACCEPTED:
                self._update_preview(pixels, result)
            self.processed.emit(result)
        except Exception as exc:
            self.closed = True
            self.finished.emit(None, f"Capture processing failed: {exc}")

    @Slot(bool)
    def finish(self, cancel):
        if self.closed:
            return
        self.closed = True
        try:
            pixels = None if cancel or not self.stitcher.height else self.stitcher.image()
            self.finished.emit(pixels, "")
        except Exception as exc:
            self.finished.emit(None, f"Could not assemble the image: {exc}")


class ScrollingCapture(QObject):
    frame_requested = Signal(object)
    finish_requested = Signal(bool)
    progress = Signal(str, int, str)
    preview_updated = Signal(QImage)
    image_ready = Signal(QImage)
    failed = Signal(str)
    completed = Signal()

    def __init__(self, region: ScreenRegion, parent: QObject | None = None):
        super().__init__(parent)
        self.region = region
        self.busy = False
        self.stopping = False
        self.paused = False
        self.rows = 0
        self.thread = QThread(self)
        self.worker = StitchWorker()
        self.worker.moveToThread(self.thread)
        self.frame_requested.connect(self.worker.process)
        self.finish_requested.connect(self.worker.finish)
        self.worker.processed.connect(self._processed)
        self.worker.preview_updated.connect(self.preview_updated)
        self.worker.finished.connect(self._finished)
        self.thread.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.completed)
        self.timer = QTimer(self)
        self.timer.setInterval(125)
        self.timer.timeout.connect(self._sample)

    def start(self):
        self.thread.start()
        self.timer.start()
        self._sample()

    def _sample(self):
        if self.busy or self.stopping or self.paused:
            return
        try:
            pixels = image_to_array(self.region.grab())
        except Exception as exc:
            self.failed.emit(str(exc))
            self.stop()
            return
        self.busy = True
        self.frame_requested.emit(pixels)

    @Slot(object)
    def _processed(self, result):
        self.busy = False
        if self.stopping:
            return
        if result is None:
            if self.paused:
                self.progress.emit("Paused — review the captured image, then resume.", self.rows, "paused")
            else:
                self.progress.emit("Waiting for scrolling to settle…", self.rows, "waiting")
            return
        self.rows = result.total_rows
        if self.paused and result.status != Status.LIMIT:
            self.progress.emit("Paused — review the captured image, then resume.", self.rows, "paused")
            return
        self.progress.emit(result.message, result.total_rows, result.status.value)
        if result.status == Status.LIMIT:
            self.stop()

    def set_paused(self, paused: bool):
        if self.stopping or self.paused == paused:
            return
        self.paused = paused
        if paused:
            self.timer.stop()
            self.progress.emit("Paused — review the captured image, then resume.", self.rows, "paused")
        else:
            self.progress.emit("Resumed — scroll downward slowly.", self.rows, "waiting")
            self.timer.start()
            self._sample()

    def stop(self, cancel=False):
        if self.stopping:
            return
        self.stopping = True
        self.timer.stop()
        self.finish_requested.emit(cancel)

    @Slot(object, str)
    def _finished(self, pixels, message):
        self.stopping = True
        self.timer.stop()
        if message:
            self.failed.emit(message)
        if pixels is not None:
            self.image_ready.emit(array_to_image(pixels))
        self.thread.quit()
