"""Capture feedback kept entirely outside the sampled rectangle."""

from PySide6.QtCore import QObject, QRect, Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget


PANEL_FLAGS = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
               | Qt.WindowType.NoDropShadowWindowHint | Qt.WindowType.WindowStaysOnTopHint
               | Qt.WindowType.WindowDoesNotAcceptFocus)


def boundary_rects(selection: QRect, screen: QRect, thickness: int = 3) -> list[QRect]:
    """Four solid strips outside the selection; clip at the screen edges."""
    x, y, w, h = selection.x(), selection.y(), selection.width(), selection.height()
    candidates = [
        QRect(x - thickness, y - thickness, w + thickness * 2, thickness),
        QRect(x - thickness, y + h, w + thickness * 2, thickness),
        QRect(x - thickness, y, thickness, h),
        QRect(x + w, y, thickness, h),
    ]
    return [clipped for rect in candidates if not (clipped := rect.intersected(screen)).isEmpty()]


class CaptureBoundary(QObject):
    """Separate input-transparent windows work even without a compositor."""

    def __init__(self, selection: QRect, screen: QRect, parent=None):
        super().__init__(parent)
        self.strips = []
        self.paused = False
        for rect in boundary_rects(selection, screen):
            strip = QWidget(None, PANEL_FLAGS | Qt.WindowType.WindowTransparentForInput)
            strip.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            strip.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            strip.setWindowTitle("Capture boundary")
            strip.setStyleSheet("background: #19b887;")
            strip.setGeometry(rect)
            self.strips.append(strip)

    def show(self):
        for strip in self.strips:
            strip.show()

    def set_paused(self, paused):
        if self.paused == paused:
            return
        self.paused = paused
        color = "#d59724" if paused else "#19b887"
        for strip in self.strips:
            strip.setStyleSheet(f"background: {color};")

    def close(self):
        for strip in self.strips:
            strip.hide()
            strip.deleteLater()
        self.strips.clear()


class CapturePanel(QWidget):
    finish_requested = Signal()
    cancel_requested = Signal()
    pause_requested = Signal()

    def __init__(self):
        super().__init__(None, PANEL_FLAGS)
        self.setObjectName("capturePanel")
        self.setStyleSheet("#capturePanel { background: #f6faf8; border: 1px solid #18695d; border-radius: 6px; }")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowTitle("Scrolling capture")
        self.preview_image = QImage()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        self.preview_title = QLabel("Live stitched preview")
        self.preview_title.setStyleSheet("font-weight: 600; font-size: 15px; color: #18695d;")
        layout.addWidget(self.preview_title)
        self.dimensions = QLabel("Starting capture…")
        self.dimensions.setStyleSheet("font-weight: 600;")
        layout.addWidget(self.dimensions)
        self.preview_scroll = QScrollArea()
        self.preview_scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.preview_scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.preview_scroll.setStyleSheet("QScrollArea { background: #dfe8e3; border: 1px solid #c5d6ce; }")
        self.preview_label = QLabel("Waiting for the first frame…")
        self.preview_label.setStyleSheet("background: white;")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_scroll.setWidget(self.preview_label)
        layout.addWidget(self.preview_scroll, 1)
        self.follow_button = QPushButton("Follow latest")
        self.follow_button.setCheckable(True)
        self.follow_button.setChecked(True)
        self.follow_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.follow_button.setToolTip("Turn off to review earlier content; turn on to follow new rows.")
        self.follow_button.toggled.connect(self._follow_latest)
        layout.addWidget(self.follow_button)
        scrollbar = self.preview_scroll.verticalScrollBar()
        scrollbar.rangeChanged.connect(self._follow_latest)
        scrollbar.actionTriggered.connect(lambda _action: self.follow_button.setChecked(False))
        scrollbar.sliderPressed.connect(lambda: self.follow_button.setChecked(False))
        self.message = QLabel("Scroll downward slowly; keep at least 25% overlap.")
        self.message.setWordWrap(True)
        self.message.setMinimumHeight(44)
        layout.addWidget(self.message)
        buttons = QHBoxLayout()
        self.pause_button = QPushButton("Pause")
        self.pause_button.clicked.connect(self.pause_requested)
        self.finish_button = QPushButton("Finish · Enter")
        self.finish_button.clicked.connect(self.finish_requested)
        self.cancel_button = QPushButton("Cancel · Esc")
        self.cancel_button.clicked.connect(self.cancel_requested)
        for button in (self.pause_button, self.finish_button, self.cancel_button):
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.set_compact(False)

    def set_compact(self, compact):
        self.preview_scroll.setVisible(not compact)
        self.preview_title.setVisible(not compact)
        self.follow_button.setVisible(not compact)
        self.setFixedSize(420, 190 if compact else 580)

    def update_preview(self, image: QImage):
        self.preview_image = image
        self.preview_label.setPixmap(QPixmap.fromImage(image))
        self.preview_label.adjustSize()
        self._follow_latest()

    def _follow_latest(self, *_args):
        if self.follow_button.isChecked():
            scrollbar = self.preview_scroll.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

    def set_paused(self, paused):
        self.pause_button.setText("Resume" if paused else "Pause")
        self.preview_title.setText("Captured so far · paused" if paused else "Live stitched preview")

    def update_progress(self, message, rows, status):
        self.dimensions.setText(f"{rows:,} pixels captured vertically")
        self.message.setText(message)
        self.message.setStyleSheet("color: #9b392f;" if status == "rejected" else "")

    def closeEvent(self, event):
        event.ignore()
        self.finish_requested.emit()
