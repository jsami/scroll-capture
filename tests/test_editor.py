import os

import numpy as np
import pytest
from PySide6.QtCore import QPoint, QPointF, QRectF, QSettings, Qt, QTimer
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QFileDialog, QLineEdit

from scroll_capture import editor as editor_module
from scroll_capture.editor import Annotation, EditorWindow
from scroll_capture.images import array_to_image, image_to_array

pytestmark = pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="Run GUI tests under xvfb-run")


@pytest.fixture
def editor(qtbot, tmp_path):
    image = QImage(320, 240, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = EditorWindow(image, settings)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    return window


def test_draw_crop_undo_redo_and_png(editor, qtbot, tmp_path):
    canvas = editor.canvas
    editor.tool_buttons["arrow"].click()
    qtbot.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))
    qtbot.mouseMove(canvas, QPoint(180, 100))
    qtbot.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(180, 100))
    assert len(canvas.annotations) == 1
    annotated = image_to_array(canvas.flattened())
    assert np.any(annotated != 255)
    assert np.all(annotated[200:, :] == 255)

    canvas.crop(QRectF(20, 20, 200, 140))
    assert (canvas.image.width(), canvas.image.height()) == (200, 140)
    assert canvas.annotations[0].start == QPointF(10, 10)
    np.testing.assert_array_equal(image_to_array(canvas.flattened()), annotated[20:160, 20:220])
    editor.undo_stack.undo()
    assert canvas.image.width() == 320
    editor.undo_stack.undo()
    assert not canvas.annotations
    editor.undo_stack.redo()
    editor.undo_stack.redo()
    output = tmp_path / "capture.png"
    editor.save_to(str(output))
    saved = QImage(str(output))
    np.testing.assert_array_equal(image_to_array(saved), image_to_array(canvas.flattened()))
    assert editor.isVisible()


def test_rectangle_text_and_clipboard(editor, qapp):
    canvas = editor.canvas
    canvas.add_annotation(Annotation("rectangle", QPointF(20, 20), QPointF(200, 120), "#18695d", 3))
    canvas.add_annotation(Annotation("text", QPointF(30, 140), QPointF(30, 140), "#ed4b35", 4, "A long capture\nSecond line"))
    expected = image_to_array(canvas.flattened())
    assert np.any(expected[140:, :] != 255)
    editor.copy_image()
    np.testing.assert_array_equal(image_to_array(qapp.clipboard().image()), expected)
    assert editor.isVisible()


def test_save_error_is_recoverable(editor, tmp_path):
    with pytest.raises(OSError):
        editor.save_to(str(tmp_path / "missing-directory" / "image.png"))
    assert editor.isVisible()
    editor.save_to(str(tmp_path / "valid.png"))


def test_zoom_does_not_change_export(editor):
    expected = image_to_array(editor.canvas.flattened())
    editor.set_zoom(0.25)
    assert editor.canvas.width() == 80
    editor.set_zoom(2)
    assert editor.canvas.width() == 640
    np.testing.assert_array_equal(image_to_array(editor.canvas.flattened()), expected)


def test_rgb_conversion_retains_odd_row_width():
    pixels = np.random.default_rng(2).integers(0, 256, (71, 103, 3), dtype=np.uint8)
    np.testing.assert_array_equal(image_to_array(array_to_image(pixels)), pixels)


def test_save_dialog_adds_png_suffix_before_accepting(editor, tmp_path, qapp, qtbot, monkeypatch):
    class QtFileDialog(QFileDialog):
        def __init__(self, *args):
            super().__init__(*args)
            self.setOption(QFileDialog.Option.DontUseNativeDialog)
    monkeypatch.setattr(editor_module, "QFileDialog", QtFileDialog)
    editor.settings.setValue("export/directory", str(tmp_path))
    def accept_dialog():
        dialog = qapp.activeModalWidget()
        assert isinstance(dialog, QFileDialog)
        filename = dialog.findChild(QLineEdit, "fileNameEdit")
        assert filename is not None
        filename.selectAll()
        qtbot.keyClicks(filename, "without-extension")
        dialog.accept()
    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(lambda: qapp.activeModalWidget().reject() if qapp.activeModalWidget() else None)
    watchdog.start(5000)
    QTimer.singleShot(100, accept_dialog)
    editor.save_image()
    watchdog.stop()
    output = tmp_path / "without-extension.png"
    assert output.exists()
    assert not QImage(str(output)).isNull()
