import os

import numpy as np
import pytest
from PySide6.QtCore import QPoint, QRect, QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QKeySequence, QPainter
from PySide6.QtWidgets import QAbstractSlider, QDialogButtonBox, QKeySequenceEdit, QWidget
from Xlib import X, XK, display
from Xlib.ext import xtest

from scroll_capture import app as app_module
from scroll_capture.app import ApplicationController, panel_position
from scroll_capture.capture import ScreenRegion, StitchWorker
from scroll_capture.capture_ui import CapturePanel, boundary_rects
from scroll_capture.hotkeys import HotkeyError, Hotkeys
from scroll_capture.images import array_to_image, image_to_array

pytestmark = pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="Run GUI tests under xvfb-run")


class ScrollableContent(QWidget):
    def __init__(self, pixels):
        super().__init__(None, Qt.WindowType.FramelessWindowHint)
        self.image = array_to_image(pixels)
        self.offset = 0
        self.viewport_border = 0
        self.setGeometry(80, 180, pixels.shape[1], 280)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.drawImage(0, -self.offset, self.image)
        if self.viewport_border:
            painter.fillRect(0, self.height() - self.viewport_border, self.width(), self.viewport_border, QColor(230, 230, 230))

    def wheelEvent(self, event):
        self.offset = max(0, min(self.image.height() - self.height(), self.offset - event.angleDelta().y() // 2))
        self.update()


@pytest.fixture
def controller(qapp, qtbot, monkeypatch, tmp_path):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(app_module, "QSettings", lambda: settings)
    result = ApplicationController(qapp)
    yield result
    result.cancel_capture()
    qtbot.waitUntil(lambda: result.capture is None, timeout=5000)
    result._cleanup()
    qapp.aboutToQuit.disconnect(result._cleanup)
    for editor in list(result.editors.values()):
        editor.close()
    result.launcher.hide()
    result.launcher.deleteLater()
    result.deleteLater()


@pytest.fixture
def target(qtbot):
    pixels = np.random.default_rng(118).integers(0, 256, (1300, 320, 3), dtype=np.uint8)
    widget = ScrollableContent(pixels)
    qtbot.addWidget(widget)
    widget.show()
    qtbot.waitExposed(widget)
    return widget, pixels


def select_target(controller, target, qtbot, scrolling):
    controller.begin_capture(scrolling)
    qtbot.waitUntil(lambda: controller.selector is not None)
    screen = target.screen()
    overlay = next(o for o in controller.selector.overlays if o.target_screen == screen)
    top_left = target.mapToGlobal(QPoint()) - screen.geometry().topLeft()
    bottom_right = top_left + QPoint(target.width() - 1, target.height() - 1)
    qtbot.mousePress(overlay, Qt.MouseButton.LeftButton, pos=top_left)
    qtbot.mouseMove(overlay, bottom_right)
    qtbot.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=bottom_right)


def press_key(symbol):
    connection = display.Display()
    code = connection.keysym_to_keycode(XK.string_to_keysym(symbol))
    xtest.fake_input(connection, X.KeyPress, code)
    xtest.fake_input(connection, X.KeyRelease, code)
    connection.sync()
    connection.close()


def test_actual_region_capture_matches_screen(qtbot, target):
    widget, pixels = target
    qtbot.wait(80)
    rect = QRect(widget.mapToGlobal(QPoint()) - widget.screen().geometry().topLeft(), widget.size())
    image = ScreenRegion(widget.screen(), rect).grab()
    np.testing.assert_array_equal(image_to_array(image), pixels[:280])


def test_scrolling_workflow_selection_wheel_finish_and_export(controller, target, qtbot, tmp_path):
    widget, pixels = target
    select_target(controller, widget, qtbot, scrolling=True)
    qtbot.waitUntil(lambda: controller.capture is not None and controller.capture.rows == 280)
    assert controller.state == "capturing"
    assert controller.boundary is not None
    for strip in controller.boundary.strips:
        assert strip.isVisible()
        assert not strip.geometry().intersects(controller.region.global_rect())
        assert strip.windowFlags() & Qt.WindowType.WindowTransparentForInput
    if controller.panel and controller.panel.isVisible():
        assert not controller.panel.frameGeometry().intersects(controller.region.global_rect())
    connection = display.Display()
    point = widget.mapToGlobal(QPoint(100, 100))
    xtest.fake_input(connection, X.MotionNotify, x=point.x(), y=point.y())
    connection.sync()
    try:
        for index in range(1, 5):
            # Real X11 wheel events go to the underlying application.
            xtest.fake_input(connection, X.ButtonPress, 5)
            xtest.fake_input(connection, X.ButtonRelease, 5)
            connection.sync()
            qtbot.waitUntil(lambda: widget.offset == index * 60)
            qtbot.waitUntil(lambda: controller.capture.rows == 280 + index * 60)
            np.testing.assert_array_equal(image_to_array(controller.panel.preview_image), pixels[:280 + index * 60])
    finally:
        connection.close()
    press_key("Return")
    qtbot.waitUntil(lambda: controller.state == "idle" and bool(controller.editors))
    editor = next(iter(controller.editors.values()))
    np.testing.assert_array_equal(image_to_array(editor.canvas.image), pixels[:520])
    output = tmp_path / "scroll.png"
    editor.save_to(str(output))
    assert output.is_file()
    assert "finish" not in controller.hotkeys.bindings
    assert "cancel" not in controller.hotkeys.bindings
    assert controller.boundary is None


def test_escape_cancels_without_result_and_releases_grabs(controller, target, qtbot):
    widget, _ = target
    select_target(controller, widget, qtbot, scrolling=True)
    qtbot.waitUntil(lambda: controller.capture is not None and controller.capture.rows > 0)
    press_key("Escape")
    qtbot.waitUntil(lambda: controller.capture is None)
    assert controller.state == "idle"
    assert not controller.editors
    assert list(controller.hotkeys.bindings) == ["start"]
    assert controller.boundary is None


def test_selection_cancel_and_ordinary_capture(controller, target, qtbot):
    widget, pixels = target
    controller.begin_capture(True)
    qtbot.waitUntil(lambda: controller.selector is not None)
    press_key("Escape")
    qtbot.waitUntil(lambda: controller.state == "idle")
    assert "cancel" not in controller.hotkeys.bindings
    select_target(controller, widget, qtbot, scrolling=False)
    qtbot.waitUntil(lambda: bool(controller.editors))
    editor = next(iter(controller.editors.values()))
    np.testing.assert_array_equal(image_to_array(editor.canvas.image), pixels[:280])
    assert controller.capture is None


def test_display_change_preserves_capture(controller, target, qtbot):
    widget, pixels = target
    select_target(controller, widget, qtbot, scrolling=True)
    qtbot.waitUntil(lambda: controller.capture is not None and controller.capture.rows > 0)
    controller._display_changed()
    qtbot.waitUntil(lambda: controller.capture is None)
    editor = next(iter(controller.editors.values()))
    np.testing.assert_array_equal(image_to_array(editor.canvas.image), pixels[:280])
    assert "Display layout changed" in controller.launcher.status.text()


def test_shortcut_conflict_and_release(qapp):
    first = Hotkeys()
    second = Hotkeys()
    try:
        first.register("test", "Ctrl+Alt+F8", lambda: None)
        with pytest.raises(HotkeyError, match="in use"):
            second.register("test", "Ctrl+Alt+F8", lambda: None)
        first.unregister("test")
        second.register("test", "Ctrl+Alt+F8", lambda: None)
    finally:
        first.close()
        second.close()


def test_global_shortcut_fires(qtbot, qapp):
    hotkeys = Hotkeys()
    connection = display.Display()
    fired = []
    try:
        hotkeys.register("test", "Ctrl+Alt+Shift+S", lambda: fired.append(True))
        keys = [connection.keysym_to_keycode(XK.string_to_keysym(name)) for name in ("Control_L", "Alt_L", "Shift_L", "s")]
        for key in keys:
            xtest.fake_input(connection, X.KeyPress, key)
        for key in reversed(keys):
            xtest.fake_input(connection, X.KeyRelease, key)
        connection.sync()
        qtbot.waitUntil(lambda: bool(fired))
    finally:
        hotkeys.close()
        connection.close()


def test_panel_placement_handles_fullscreen_and_negative_monitor_positions():
    screen = QRect(0, 0, 1280, 1024)
    size = QSize(460, 154)
    assert panel_position(screen, [screen], size) is None
    other = QRect(-1280, 0, 1280, 1024)
    point = panel_position(screen, [screen, other], size)
    assert point is not None
    assert other.contains(QRect(point, size))


def test_settings_can_record_shortcut_and_cancel_restores_it(controller, qtbot, qapp):
    def save_dialog():
        dialog = qapp.activeModalWidget()
        assert dialog is not None
        assert "start" not in controller.hotkeys.bindings
        controller.begin_capture(True)
        assert controller.state == "idle"
        dialog.findChild(QKeySequenceEdit).setKeySequence(QKeySequence("Ctrl+Alt+K"))
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).click()
    QTimer.singleShot(50, save_dialog)
    controller.show_settings()
    assert controller.shortcut == "Ctrl+Alt+K"
    assert controller.settings.value("capture/shortcut") == "Ctrl+Alt+K"
    assert "start" in controller.hotkeys.bindings
    QTimer.singleShot(50, lambda: qapp.activeModalWidget().reject())
    controller.show_settings()
    assert "start" in controller.hotkeys.bindings
    assert not controller.settings_open


def test_pause_freezes_capture_and_resume_keeps_the_reference(controller, target, qtbot):
    widget, pixels = target
    select_target(controller, widget, qtbot, scrolling=True)
    qtbot.waitUntil(lambda: controller.capture is not None and controller.capture.rows == 280 and not controller.capture.busy)
    controller.panel.pause_button.click()
    assert controller.capture.paused
    assert controller.panel.pause_button.text() == "Resume"
    assert controller.pause_action.text() == "Resume capture"
    assert not controller.capture.timer.isActive()
    widget.offset = 60
    widget.update()
    qtbot.wait(350)
    assert controller.capture.rows == 280
    np.testing.assert_array_equal(image_to_array(controller.panel.preview_image), pixels[:280])
    controller.panel.pause_button.click()
    qtbot.waitUntil(lambda: controller.capture.rows == 340)
    np.testing.assert_array_equal(image_to_array(controller.panel.preview_image), pixels[:340])
    controller.panel.pause_button.click()
    controller.panel.finish_button.click()
    qtbot.waitUntil(lambda: controller.capture is None and bool(controller.editors))
    editor = next(iter(controller.editors.values()))
    np.testing.assert_array_equal(image_to_array(editor.canvas.image), pixels[:340])


def test_rejected_join_does_not_change_the_live_preview(controller, target, qtbot):
    widget, pixels = target
    select_target(controller, widget, qtbot, scrolling=True)
    qtbot.waitUntil(lambda: controller.capture is not None and controller.capture.rows == 280)
    widget.offset = 500
    widget.update()
    qtbot.waitUntil(lambda: "Cannot align" in controller.panel.message.text())
    assert controller.capture.rows == 280
    np.testing.assert_array_equal(image_to_array(controller.panel.preview_image), pixels[:280])
    widget.offset = 60
    widget.update()
    qtbot.waitUntil(lambda: controller.capture.rows == 340)
    np.testing.assert_array_equal(image_to_array(controller.panel.preview_image), pixels[:340])


def test_preview_can_review_earlier_rows_without_jumping_back(qtbot):
    panel = CapturePanel()
    qtbot.addWidget(panel)
    panel.show()
    pixels = np.full((1000, 320, 3), 220, np.uint8)
    panel.update_preview(array_to_image(pixels))
    bar = panel.preview_scroll.verticalScrollBar()
    qtbot.waitUntil(lambda: bar.maximum() > 0 and bar.value() == bar.maximum())
    bar.triggerAction(QAbstractSlider.SliderAction.SliderToMinimum)
    assert not panel.follow_button.isChecked()
    panel.update_preview(array_to_image(np.full((1200, 320, 3), 220, np.uint8)))
    qtbot.wait(30)
    assert bar.value() == 0
    panel.follow_button.click()
    assert bar.value() == bar.maximum()


def test_boundary_clips_at_screen_edges_and_handles_negative_coordinates():
    screen = QRect(-1280, 0, 1280, 1024)
    selection = QRect(-1280, 0, 320, 280)
    rects = boundary_rects(selection, screen)
    assert len(rects) == 2
    for rect in rects:
        assert screen.contains(rect)
        assert not rect.intersects(selection)
    assert boundary_rects(screen, screen) == []


def test_preview_is_bounded_without_resizing_the_export(qapp):
    pixels = np.random.default_rng(41).integers(0, 256, (5000, 160, 3), dtype=np.uint8)
    worker = StitchWorker()
    for offset in range(0, 4201, 300):
        frame = pixels[offset:offset + 400]
        worker.process(frame)
        worker.process(frame)  # Let the frame settle, as it would during capture.
    assert worker.preview.width() <= 320
    assert worker.preview.height() == 4096
    np.testing.assert_array_equal(worker.stitcher.image(), pixels[:4600])


def test_viewport_edge_is_replaced_in_live_preview_and_export(controller, target, qtbot):
    widget, pixels = target
    widget.viewport_border = 6
    widget.update()
    select_target(controller, widget, qtbot, scrolling=True)
    qtbot.waitUntil(lambda: controller.capture is not None and controller.capture.rows == 280)
    for offset in (60, 120, 180, 240):
        widget.offset = offset
        widget.update()
        qtbot.waitUntil(lambda: controller.capture.rows == 280 + offset)
        expected = pixels[:280 + offset].copy()
        expected[-6:] = 230
        np.testing.assert_array_equal(image_to_array(controller.panel.preview_image), expected)
    controller.finish_capture()
    qtbot.waitUntil(lambda: controller.capture is None and bool(controller.editors))
    editor = next(iter(controller.editors.values()))
    np.testing.assert_array_equal(image_to_array(editor.canvas.image), expected)
