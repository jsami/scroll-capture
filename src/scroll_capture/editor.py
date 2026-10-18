"""A small raster preview with undoable vector annotations and cropping."""

from dataclasses import dataclass, replace
from datetime import datetime
import math
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSaveFile, QSettings, QStandardPaths, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QImage, QKeySequence, QPainter, QPen, QPolygonF, QUndoCommand, QUndoStack
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QColorDialog, QFileDialog, QInputDialog,
    QMainWindow, QMessageBox, QPushButton, QScrollArea, QSpinBox, QToolBar,
    QToolButton, QWidget,
)


@dataclass(frozen=True)
class Annotation:
    kind: str
    start: QPointF
    end: QPointF
    color: str
    width: int
    text: str = ""


def draw_annotation(painter: QPainter, annotation: Annotation):
    a = annotation
    color = QColor(a.color)
    painter.setPen(QPen(color, a.width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    if a.kind == "rectangle":
        painter.drawRect(QRectF(a.start, a.end).normalized())
    elif a.kind == "arrow":
        painter.drawLine(a.start, a.end)
        angle = math.atan2(a.end.y() - a.start.y(), a.end.x() - a.start.x())
        size = max(12, a.width * 4)
        points = [a.end]
        for offset in (-math.pi / 6, math.pi / 6):
            points.append(QPointF(a.end.x() - size * math.cos(angle + offset), a.end.y() - size * math.sin(angle + offset)))
        painter.setBrush(color)
        painter.drawPolygon(QPolygonF(points))
    elif a.kind == "text":
        font = QFont("Sans Serif")
        font.setPixelSize(max(14, a.width * 6))
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        for index, line in enumerate(a.text.splitlines()):
            painter.drawText(a.start + QPointF(0, metrics.ascent() + index * metrics.height()), line)


class EditCommand(QUndoCommand):
    def __init__(self, canvas, title, after):
        super().__init__(title)
        self.canvas = canvas
        self.before = (canvas.image, canvas.annotations)
        self.after = after

    def _apply(self, state):
        self.canvas.image, self.canvas.annotations = state
        self.canvas.resize_canvas()
        self.canvas.changed.emit()

    def undo(self):
        self._apply(self.before)

    def redo(self):
        self._apply(self.after)


class AnnotationCanvas(QWidget):
    changed = Signal()
    tool_finished = Signal()

    def __init__(self, image: QImage, undo_stack: QUndoStack, parent=None):
        super().__init__(parent)
        self.image = image
        self.annotations: list[Annotation] = []
        self.undo_stack = undo_stack
        self.zoom = 1.0
        self.tool = "pan"
        self.color = "#ed4b35"
        self.stroke = 4
        self.anchor: QPointF | None = None
        self.current: QPointF | None = None
        self.pan_origin = None
        self.scroll_area: QScrollArea | None = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.set_tool("pan")
        self.resize_canvas()

    def resize_canvas(self):
        self.setFixedSize(max(1, round(self.image.width() * self.zoom)), max(1, round(self.image.height() * self.zoom)))
        self.update()

    def set_tool(self, tool):
        self.tool = tool
        self.anchor = self.current = None
        self.setCursor(Qt.CursorShape.OpenHandCursor if tool == "pan" else Qt.CursorShape.CrossCursor)
        self.update()

    def _point(self, event):
        point = event.position() / self.zoom
        return QPointF(max(0, min(self.image.width(), point.x())), max(0, min(self.image.height(), point.y())))

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.scale(self.zoom, self.zoom)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, self.zoom < 1)
        painter.drawImage(0, 0, self.image)
        for annotation in self.annotations:
            draw_annotation(painter, annotation)
        if self.anchor is not None and self.current is not None:
            if self.tool == "crop":
                painter.setPen(QPen(QColor("#18695d"), 1 / self.zoom, Qt.PenStyle.DashLine))
                painter.setBrush(QColor(24, 105, 93, 40))
                painter.drawRect(QRectF(self.anchor, self.current).normalized())
            elif self.tool in ("arrow", "rectangle"):
                draw_annotation(painter, Annotation(self.tool, self.anchor, self.current, self.color, self.stroke))

    def mousePressEvent(self, event):
        if event.button() not in (Qt.MouseButton.LeftButton, Qt.MouseButton.MiddleButton):
            return
        if self.tool == "pan" or event.button() == Qt.MouseButton.MiddleButton:
            if self.scroll_area:
                self.pan_origin = (event.globalPosition(), self.scroll_area.horizontalScrollBar().value(), self.scroll_area.verticalScrollBar().value())
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        point = self._point(event)
        if self.tool == "text":
            text, accepted = QInputDialog.getMultiLineText(self, "Add text", "Text:")
            if accepted and text.strip():
                self.add_annotation(Annotation("text", point, point, self.color, self.stroke, text))
            return
        self.anchor = self.current = point
        self.update()

    def mouseMoveEvent(self, event):
        if self.pan_origin is not None and self.scroll_area:
            origin, h_value, v_value = self.pan_origin
            movement = event.globalPosition() - origin
            self.scroll_area.horizontalScrollBar().setValue(h_value - round(movement.x()))
            self.scroll_area.verticalScrollBar().setValue(v_value - round(movement.y()))
        elif self.anchor is not None:
            self.current = self._point(event)
            self.update()

    def mouseReleaseEvent(self, event):
        if self.pan_origin is not None:
            self.pan_origin = None
            self.setCursor(Qt.CursorShape.OpenHandCursor if self.tool == "pan" else Qt.CursorShape.CrossCursor)
            return
        if event.button() != Qt.MouseButton.LeftButton or self.anchor is None:
            return
        self.current = self._point(event)
        start, end = self.anchor, self.current
        self.anchor = self.current = None
        if self.tool == "crop":
            self.crop(QRectF(start, end).normalized())
            self.tool_finished.emit()
        elif (end - start).manhattanLength() >= 3:
            self.add_annotation(Annotation(self.tool, start, end, self.color, self.stroke))
        self.update()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.anchor = self.current = None
            self.tool_finished.emit()
            self.update()
        else:
            super().keyPressEvent(event)

    def add_annotation(self, annotation: Annotation):
        self.undo_stack.push(EditCommand(self, f"Add {annotation.kind}", (self.image, self.annotations + [annotation])))

    def crop(self, rect: QRectF):
        rect = rect.toAlignedRect().intersected(self.image.rect())
        if rect.width() < 2 or rect.height() < 2 or rect == self.image.rect():
            return
        offset = QPointF(rect.topLeft())
        annotations = [replace(a, start=a.start - offset, end=a.end - offset) for a in self.annotations]
        self.undo_stack.push(EditCommand(self, "Crop", (self.image.copy(rect), annotations)))

    def flattened(self) -> QImage:
        result = QImage(self.image.size(), QImage.Format.Format_RGB32)
        result.fill(Qt.GlobalColor.white)
        painter = QPainter(result)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.drawImage(0, 0, self.image)
        for annotation in self.annotations:
            draw_annotation(painter, annotation)
        painter.end()
        return result


class EditorWindow(QMainWindow):
    def __init__(self, image: QImage, settings: QSettings | None = None):
        super().__init__()
        self.settings = settings if settings is not None else QSettings()
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle("Capture — Scroll Capture")
        self.resize(1060, 780)
        self.undo_stack = QUndoStack(self)
        self.undo_stack.setUndoLimit(30)
        self.canvas = AnnotationCanvas(image, self.undo_stack)
        self.canvas.color = str(self.settings.value("editor/color", "#ed4b35"))
        self.canvas.stroke = int(self.settings.value("editor/stroke", 4))
        self.scroll = QScrollArea()
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll.setWidget(self.canvas)
        self.canvas.scroll_area = self.scroll
        self.scroll.setStyleSheet("QScrollArea { background: #dbe1df; border: 0; }")
        self.setCentralWidget(self.scroll)
        self.canvas.changed.connect(self._update_status)
        self._build_toolbar()
        self.canvas.tool_finished.connect(lambda: self.tool_buttons["pan"].click())
        self._update_status()

    def _build_toolbar(self):
        toolbar = QToolBar("Capture tools", self)
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(toolbar)
        self.tool_group = QButtonGroup(self)
        self.tool_group.setExclusive(True)
        self.tool_buttons = {}
        for tool, label in (("pan", "Pan"), ("crop", "Crop"), ("arrow", "Arrow"), ("rectangle", "Rectangle"), ("text", "Text")):
            button = QToolButton()
            button.setText(label)
            button.setCheckable(True)
            button.setChecked(tool == "pan")
            button.clicked.connect(lambda _checked, t=tool: self.canvas.set_tool(t))
            self.tool_group.addButton(button)
            self.tool_buttons[tool] = button
            toolbar.addWidget(button)
        toolbar.addSeparator()
        self.color_button = QPushButton("Color")
        self.color_button.clicked.connect(self._choose_color)
        self._update_color_button()
        toolbar.addWidget(self.color_button)
        stroke = QSpinBox()
        stroke.setRange(1, 16)
        stroke.setPrefix("Size ")
        stroke.setValue(self.canvas.stroke)
        stroke.setToolTip("Stroke width; also controls text size")
        stroke.valueChanged.connect(self._set_stroke)
        toolbar.addWidget(stroke)
        toolbar.addSeparator()
        undo = self.undo_stack.createUndoAction(self, "Undo")
        undo.setShortcut(QKeySequence.StandardKey.Undo)
        redo = self.undo_stack.createRedoAction(self, "Redo")
        redo.setShortcuts([QKeySequence("Ctrl+Shift+Z"), QKeySequence("Ctrl+Y")])
        toolbar.addAction(undo)
        toolbar.addAction(redo)
        toolbar.addSeparator()
        for label, shortcut, action in (("−", "Ctrl+-", lambda: self.set_zoom(self.canvas.zoom / 1.25)), ("+", "Ctrl++", lambda: self.set_zoom(self.canvas.zoom * 1.25)), ("Fit width", "Ctrl+0", self.fit_width), ("Copy", "Ctrl+C", self.copy_image), ("Save PNG", "Ctrl+S", self.save_image)):
            item = QAction(label, self)
            item.setShortcut(QKeySequence(shortcut))
            item.triggered.connect(action)
            toolbar.addAction(item)

    def _choose_color(self):
        color = QColorDialog.getColor(QColor(self.canvas.color), self, "Annotation color")
        if color.isValid():
            self.canvas.color = color.name()
            self.settings.setValue("editor/color", color.name())
            self._update_color_button()

    def _update_color_button(self):
        self.color_button.setStyleSheet(f"border-bottom: 4px solid {self.canvas.color}; padding: 5px 10px;")

    def _set_stroke(self, value):
        self.canvas.stroke = value
        self.settings.setValue("editor/stroke", value)

    def set_zoom(self, zoom):
        old = self.canvas.zoom
        zoom = max(0.1, min(4.0, zoom))
        center_x = (self.scroll.horizontalScrollBar().value() + self.scroll.viewport().width() / 2) / old
        center_y = (self.scroll.verticalScrollBar().value() + self.scroll.viewport().height() / 2) / old
        self.canvas.zoom = zoom
        self.canvas.resize_canvas()
        self.scroll.horizontalScrollBar().setValue(round(center_x * zoom - self.scroll.viewport().width() / 2))
        self.scroll.verticalScrollBar().setValue(round(center_y * zoom - self.scroll.viewport().height() / 2))
        self._update_status()

    def fit_width(self):
        self.set_zoom(min(1.0, (self.scroll.viewport().width() - 32) / self.canvas.image.width()))
        self.scroll.verticalScrollBar().setValue(0)

    def _update_status(self):
        self.statusBar().showMessage(f"{self.canvas.image.width():,} × {self.canvas.image.height():,} pixels    ·    {self.canvas.zoom:.0%}    ·    Drag to annotate; use Pan to move around")

    def copy_image(self):
        QApplication.clipboard().setImage(self.canvas.flattened())
        self.statusBar().showMessage("Image copied to clipboard", 5000)

    def save_to(self, path: str):
        output = QSaveFile(path)
        if not output.open(QSaveFile.OpenModeFlag.WriteOnly):
            raise OSError(output.errorString())
        if not self.canvas.flattened().save(output, "PNG"):
            output.cancelWriting()
            raise OSError("The PNG encoder could not write this image.")
        if not output.commit():
            raise OSError(output.errorString())
        self.settings.setValue("export/directory", str(Path(path).resolve().parent))
        self.statusBar().showMessage(f"Saved {path}", 5000)

    def save_image(self):
        default_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation) or str(Path.home())
        directory = str(self.settings.value("export/directory", default_dir))
        suggested = str(Path(directory) / f"Capture-{datetime.now():%Y-%m-%d-%H%M%S}.png")
        dialog = QFileDialog(self, "Save capture", suggested, "PNG images (*.png)")
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setFileMode(QFileDialog.FileMode.AnyFile)
        dialog.setDefaultSuffix("png")
        accepted = dialog.exec() == QFileDialog.DialogCode.Accepted
        path = dialog.selectedFiles()[0] if accepted else ""
        dialog.deleteLater()
        if not path:
            return
        try:
            self.save_to(path)
        except (OSError, MemoryError) as exc:
            QMessageBox.warning(self, "Could not save capture", str(exc))
