"""Application lifecycle, tray controls, settings, and single-instance CLI."""

import argparse
import hashlib
import os
from pathlib import Path
import signal
import sys
import tempfile

from PySide6.QtCore import QObject, QLockFile, QPoint, QRect, QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QIcon, QKeySequence
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QHBoxLayout, QKeySequenceEdit, QLabel,
    QMenu, QMessageBox, QPushButton, QSystemTrayIcon, QVBoxLayout, QWidget,
)

from . import __version__
from .capture import RegionSelector, ScrollingCapture
from .capture_ui import CaptureBoundary, CapturePanel
from .editor import EditorWindow
from .hotkeys import HotkeyError, Hotkeys


DEFAULT_SHORTCUT = "Ctrl+Alt+Shift+S"
ICON = Path(__file__).parent / "assets" / "icon.svg"


def panel_position(selection: QRect, screens: list[QRect], size: QSize) -> QPoint | None:
    """Find space for controls without covering a single selected pixel."""
    w, h, gap = size.width(), size.height(), 12
    near = [
        QPoint(selection.left(), selection.top() - h - gap),
        QPoint(selection.left(), selection.bottom() + gap),
        QPoint(selection.right() + gap, selection.top()),
        QPoint(selection.left() - w - gap, selection.top()),
    ]
    corners = []
    for screen in screens:
        corners.extend([
            screen.topLeft() + QPoint(gap, gap),
            QPoint(screen.right() - w - gap, screen.top() + gap),
            QPoint(screen.left() + gap, screen.bottom() - h - gap),
            QPoint(screen.right() - w - gap, screen.bottom() - h - gap),
        ])
    for point in near + corners:
        rect = QRect(point, size)
        if not rect.intersects(selection) and any(screen.contains(rect) for screen in screens):
            return point
    return None


class InstanceServer(QObject):
    requested = Signal(str)

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.server.newConnection.connect(self._accept)
        # The caller owns the process lock before removing a stale socket.
        QLocalServer.removeServer(path)
        if not self.server.listen(path):
            raise RuntimeError(self.server.errorString())

    def _accept(self):
        while self.server.hasPendingConnections():
            client = self.server.nextPendingConnection()
            client.setParent(self)
            client.disconnected.connect(client.deleteLater)
            client.readyRead.connect(lambda c=client: self._read(c))
            timeout = QTimer(client)
            timeout.setSingleShot(True)
            timeout.timeout.connect(client.disconnectFromServer)
            timeout.start(2000)
            if client.bytesAvailable():
                self._read(client)

    def _read(self, client):
        if client.bytesAvailable() > 128:
            client.disconnectFromServer()
            return
        if not client.canReadLine():
            return
        command = bytes(client.readLine(128)).decode("ascii", errors="replace").strip()
        if command in ("show", "capture", "scroll"):
            self.requested.emit(command)
            client.write(b"ok\n")
            client.flush()
        client.disconnectFromServer()


def forward_request(path: str, command: str) -> bool:
    socket = QLocalSocket()
    socket.connectToServer(path)
    if not socket.waitForConnected(1500):
        return False
    socket.write((command + "\n").encode("ascii"))
    socket.flush()
    if not socket.bytesAvailable() and not socket.waitForReadyRead(2000):
        return False
    return bytes(socket.readAll()).strip() == b"ok"


class Launcher(QWidget):
    capture_requested = Signal(bool)
    settings_requested = Signal()
    quit_requested = Signal()

    def __init__(self, has_tray: bool):
        super().__init__()
        self.has_tray = has_tray
        self.setWindowTitle("Scroll Capture")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 28, 30, 24)
        layout.setSpacing(16)
        heading = QLabel("Scroll Capture")
        heading.setStyleSheet("font-size: 27px; font-weight: 700; color: #18695d;")
        layout.addWidget(heading)
        description = QLabel("A screenshot that keeps going.")
        description.setStyleSheet("font-size: 16px;")
        layout.addWidget(description)
        instructions = QLabel("Select the scrolling content, then scroll downward slowly.\nPress Enter to finish or Escape to cancel.")
        instructions.setWordWrap(True)
        layout.addWidget(instructions)
        self.scroll_button = QPushButton("Scrolling capture")
        self.scroll_button.setStyleSheet("background: #18695d; color: white; padding: 13px; font-weight: 600; border-radius: 6px;")
        self.scroll_button.clicked.connect(lambda: self.capture_requested.emit(True))
        layout.addWidget(self.scroll_button)
        self.region_button = QPushButton("Region capture")
        self.region_button.setMinimumHeight(40)
        self.region_button.clicked.connect(lambda: self.capture_requested.emit(False))
        layout.addWidget(self.region_button)
        self.shortcut_label = QLabel()
        self.shortcut_label.setWordWrap(True)
        layout.addWidget(self.shortcut_label)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #9b392f;")
        self.status.hide()
        layout.addWidget(self.status)
        row = QHBoxLayout()
        settings = QPushButton("Settings")
        settings.clicked.connect(self.settings_requested)
        row.addWidget(settings)
        row.addStretch()
        quit_button = QPushButton("Quit")
        quit_button.clicked.connect(self.quit_requested)
        row.addWidget(quit_button)
        layout.addLayout(row)

    def closeEvent(self, event: QCloseEvent):
        event.ignore()
        if self.has_tray:
            self.hide()
        else:
            self.quit_requested.emit()


class ApplicationController(QObject):
    def __init__(self, application: QApplication):
        super().__init__(application)
        self.application = application
        self.settings = QSettings()
        self.shortcut = str(self.settings.value("capture/shortcut", DEFAULT_SHORTCUT))
        self.state = "idle"
        self.quitting = False
        self.settings_open = False
        self.selector = None
        self.capture = None
        self.panel = None
        self.boundary = None
        self.region = None
        self.editors = {}
        self.hidden_windows = []
        self.generation = 0
        self.last_error = ""
        self.has_tray = QSystemTrayIcon.isSystemTrayAvailable()
        self.launcher = Launcher(self.has_tray)
        self.launcher.capture_requested.connect(self.begin_capture)
        self.launcher.settings_requested.connect(self.show_settings)
        self.launcher.quit_requested.connect(self.quit)
        self.tray = QSystemTrayIcon(QIcon(str(ICON)), self)
        self.tray.setToolTip("Scroll Capture")
        self._build_tray()
        if self.has_tray:
            self.tray.show()
        self.hotkeys = None
        try:
            self.hotkeys = Hotkeys(self)
            self.hotkeys.register("start", self.shortcut, lambda: self.begin_capture(True))
        except Exception as exc:
            self._set_error(str(exc))
        self._update_shortcut()
        application.screenRemoved.connect(self._display_changed)
        application.screenAdded.connect(self._screen_added)
        for screen in application.screens():
            self._screen_added(screen)
        application.aboutToQuit.connect(self._cleanup)

    def _build_tray(self):
        self.menu = QMenu()
        self.scroll_action = self.menu.addAction("Scrolling capture")
        self.scroll_action.triggered.connect(lambda: self.begin_capture(True))
        self.region_action = self.menu.addAction("Region capture")
        self.region_action.triggered.connect(lambda: self.begin_capture(False))
        self.menu.addSeparator()
        self.pause_action = self.menu.addAction("Pause capture")
        self.pause_action.triggered.connect(self.toggle_pause)
        self.pause_action.setEnabled(False)
        self.finish_action = self.menu.addAction("Finish capture")
        self.finish_action.triggered.connect(self.finish_capture)
        self.finish_action.setEnabled(False)
        self.cancel_action = self.menu.addAction("Cancel capture")
        self.cancel_action.triggered.connect(self.cancel_capture)
        self.cancel_action.setEnabled(False)
        self.menu.addSeparator()
        self.menu.addAction("Open Scroll Capture", self.show_launcher)
        self.menu.addAction("Settings", self.show_settings)
        self.menu.addAction("Quit", self.quit)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(lambda reason: self.show_launcher() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)

    def request(self, command):
        if command == "show":
            self.show_launcher()
        else:
            self.begin_capture(command == "scroll")

    def show_launcher(self):
        # Never place our own window over a live capture.
        if self.state != "idle":
            return
        self.launcher.show()
        self.launcher.raise_()
        self.launcher.activateWindow()

    def _set_error(self, text):
        self.launcher.status.setText(text)
        self.launcher.status.setVisible(bool(text))

    def _update_shortcut(self):
        self.launcher.shortcut_label.setText(f"Scrolling shortcut: {self.shortcut}\nSelect only moving content; leave fixed headers and scrollbars outside.")

    def _set_state(self, state):
        self.state = state
        idle = state == "idle"
        self.scroll_action.setEnabled(idle)
        self.region_action.setEnabled(idle)
        self.launcher.scroll_button.setEnabled(idle)
        self.launcher.region_button.setEnabled(idle)
        self.cancel_action.setEnabled(not idle and state != "stopping")
        self.finish_action.setEnabled(state == "capturing")
        self.pause_action.setEnabled(state == "capturing")
        if idle:
            self.pause_action.setText("Pause capture")

    def begin_capture(self, scrolling=True):
        if self.state != "idle" or self.quitting or self.settings_open:
            return
        self.last_error = ""
        self.generation += 1
        generation = self.generation
        if self.hotkeys:
            try:
                self.hotkeys.register("cancel", "Esc", self.cancel_capture)
            except HotkeyError:
                # Overlay key events and right-click still allow cancellation.
                pass
        self.hidden_windows = [window for window in [self.launcher, *self.editors.values()] if window.isVisible()]
        for window in self.hidden_windows:
            window.hide()
        self._set_state("selecting")
        def select():
            if self.state != "selecting" or generation != self.generation:
                return
            self.selector = RegionSelector(scrolling, self)
            self.selector.selected.connect(lambda region: self._selected(region, scrolling, generation))
            self.selector.cancelled.connect(self._selection_cancelled)
            self.selector.show()
        QTimer.singleShot(200, select)

    def _selected(self, region, scrolling, generation):
        self.selector.deleteLater()
        self.selector = None
        self.region = region
        self._set_state("arming")
        def arm():
            if self.state != "arming" or generation != self.generation:
                return
            if scrolling:
                self._start_scrolling(region)
            else:
                try:
                    image = region.grab()
                except Exception as exc:
                    self._selection_cancelled()
                    self._set_error(str(exc))
                    self.show_launcher()
                    return
                self._set_state("idle")
                self._hide_controls()
                self._open_result(image)
        QTimer.singleShot(220, arm)

    def _start_scrolling(self, region):
        self.capture = ScrollingCapture(region, self)
        self.capture.image_ready.connect(self._open_result)
        self.capture.progress.connect(self._progress)
        self.capture.failed.connect(self._capture_error)
        self.capture.completed.connect(self._capture_completed)
        self.panel = CapturePanel()
        self.panel.finish_requested.connect(self.finish_capture)
        self.panel.cancel_requested.connect(self.cancel_capture)
        self.panel.pause_requested.connect(self.toggle_pause)
        self.capture.preview_updated.connect(self.panel.update_preview)
        available = [s.availableGeometry() for s in self.application.screens()]
        point = panel_position(region.global_rect(), available, self.panel.size())
        if point is None:
            self.panel.set_compact(True)
            point = panel_position(region.global_rect(), available, self.panel.size())
        if point is not None:
            self.panel.move(point)
            self.panel.show()
        elif not self.has_tray:
            self.panel.deleteLater()
            self.panel = None
            self.capture.deleteLater()
            self.capture = None
            self._selection_cancelled()
            self._set_error("Leave some space outside the capture area for Finish and Cancel controls.")
            self.show_launcher()
            return
        if self.hotkeys:
            for name, sequence, callback in (("finish", "Return", self.finish_capture), ("finish-keypad", "Enter", self.finish_capture), ("cancel", "Esc", self.cancel_capture)):
                try:
                    self.hotkeys.register(name, sequence, callback)
                except HotkeyError as exc:
                    self.last_error = str(exc)
                    self.tray.setToolTip(f"{exc}\nUse Finish/Cancel in the tray menu.")
        self.boundary = CaptureBoundary(region.global_rect(), region.screen.geometry(), self)
        self.boundary.show()
        self._set_state("capturing")
        self.capture.start()

    def _progress(self, message, rows, status):
        if status == "limit" and not rows:
            self.last_error = message
        if self.panel:
            self.panel.update_progress(message, rows, status)
            self.panel.set_paused(self.capture.paused)
        if self.boundary:
            self.boundary.set_paused(self.capture.paused)
        self.pause_action.setText("Resume capture" if self.capture.paused else "Pause capture")
        self.tray.setToolTip(f"Scroll Capture · {rows:,} pixels\n{message}\nEnter: finish · Escape: cancel")

    def toggle_pause(self):
        if self.capture and self.state == "capturing":
            self.capture.set_paused(not self.capture.paused)

    def finish_capture(self):
        if self.capture and self.state == "capturing":
            self._set_state("stopping")
            self._hide_controls()
            self.capture.stop()

    def cancel_capture(self):
        if self.capture:
            if self.state != "stopping":
                self._set_state("stopping")
                self._hide_controls()
                self.capture.stop(cancel=True)
        elif self.selector:
            self.selector.cancel()
        elif self.state != "idle":
            self._selection_cancelled()

    def _selection_cancelled(self):
        self.generation += 1
        self._hide_controls()
        if self.selector:
            self.selector.deleteLater()
            self.selector = None
        self._set_state("idle")
        self._restore_windows()

    def _hide_controls(self):
        if self.boundary:
            self.boundary.close()
            self.boundary.deleteLater()
            self.boundary = None
        if self.panel:
            self.panel.hide()
            self.panel.deleteLater()
            self.panel = None
        if self.hotkeys:
            for name in ("finish", "finish-keypad", "cancel"):
                self.hotkeys.unregister(name)

    def _capture_error(self, message):
        self.last_error = message

    def _capture_completed(self):
        self._hide_controls()
        if self.capture:
            self.capture.deleteLater()
            self.capture = None
        self._set_state("idle")
        self.tray.setToolTip("Scroll Capture")
        self._restore_windows()
        if self.last_error and not self.quitting:
            self._set_error(self.last_error)
            self.show_launcher()
        if self.quitting:
            self.application.quit()

    def _restore_windows(self):
        if self.quitting:
            return
        for window in self.hidden_windows:
            if window is not self.launcher or not self.has_tray:
                window.show()
        if not self.has_tray:
            self.launcher.show()
        self.hidden_windows = []

    def _open_result(self, image):
        if self.quitting:
            return
        self._restore_windows()
        editor = EditorWindow(image, self.settings)
        identity = id(editor)
        self.editors[identity] = editor
        editor.destroyed.connect(lambda _obj=None, key=identity: self.editors.pop(key, None))
        editor.show()
        editor.raise_()
        editor.activateWindow()
        QTimer.singleShot(0, editor.fit_width)

    def show_settings(self):
        if self.state != "idle" or self.settings_open:
            return
        self.settings_open = True
        if self.hotkeys:
            self.hotkeys.unregister("start")
        dialog = QDialog(self.launcher)
        dialog.setWindowTitle("Scroll Capture settings")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Global shortcut for scrolling capture"))
        shortcut = QKeySequenceEdit(QKeySequence(self.shortcut))
        shortcut.setMaximumSequenceLength(1)
        layout.addWidget(shortcut)
        error_label = QLabel()
        error_label.setWordWrap(True)
        error_label.setStyleSheet("color: #9b392f;")
        layout.addWidget(error_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(dialog.reject)
        def save():
            value = shortcut.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
            if not any(part in value.split("+")[:-1] for part in ("Ctrl", "Alt", "Meta")):
                error_label.setText("Include Ctrl, Alt or Meta so typing does not start a capture.")
                return
            if self.hotkeys is None:
                error_label.setText("Global shortcuts could not connect to your X11 display. Restart the app and try again.")
                return
            try:
                self.hotkeys.register("start", value, lambda: self.begin_capture(True))
            except HotkeyError as exc:
                error_label.setText(str(exc))
                return
            self.shortcut = value
            self.settings.setValue("capture/shortcut", value)
            self._set_error("")
            self._update_shortcut()
            dialog.accept()
        buttons.accepted.connect(save)
        layout.addWidget(buttons)
        try:
            dialog.exec()
        finally:
            self.settings_open = False
            dialog.deleteLater()
            if not self.quitting and self.hotkeys and "start" not in self.hotkeys.bindings:
                try:
                    self.hotkeys.register("start", self.shortcut, lambda: self.begin_capture(True))
                except HotkeyError as exc:
                    self._set_error(str(exc))

    def _screen_added(self, screen):
        screen.geometryChanged.connect(self._display_changed)
        screen.logicalDotsPerInchChanged.connect(self._display_changed)
        self._display_changed()

    def _display_changed(self, *_args):
        if self.capture:
            self.last_error = "Display layout changed. The accepted part of your capture has been preserved."
            self.finish_capture()
        elif self.state != "idle":
            self.cancel_capture()

    def quit(self):
        self.quitting = True
        if self.capture:
            self.cancel_capture()
        else:
            self.cancel_capture()
            self.application.quit()

    def _cleanup(self):
        self.tray.hide()
        if self.hotkeys:
            self.hotkeys.close()
        # Also handle an external Qt quit while a worker is still running.
        if self.capture and self.capture.thread.isRunning():
            self.capture.timer.stop()
            self.capture.thread.quit()
            self.capture.thread.wait()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Capture a region or manually stitch a scrolling screenshot (Linux X11).")
    parser.add_argument("--version", action="version", version=f"Scroll Capture {__version__}")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--capture", action="store_true", help="select an ordinary screenshot region")
    mode.add_argument("--scroll", action="store_true", help="select a manual scrolling capture region")
    args = parser.parse_args(argv)
    if not os.environ.get("DISPLAY") or os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
        print("Scroll Capture requires a Linux X11 desktop session. Wayland is not supported in this version.", file=sys.stderr)
        return 1
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    application = QApplication([sys.argv[0]])
    application.setOrganizationName("ScrollCapture")
    application.setApplicationName("ScrollCapture")
    application.setApplicationVersion(__version__)
    application.setWindowIcon(QIcon(str(ICON)))
    application.setQuitOnLastWindowClosed(False)
    application.setStyle("Fusion")
    application.setStyleSheet("QToolBar { spacing: 5px; padding: 8px; } QToolButton { padding: 7px; } QToolButton:checked { background: #bfe4d8; border-radius: 4px; } QToolTip { padding: 6px; }")
    identity = hashlib.sha256(f"{os.getuid()}:{os.environ.get('DISPLAY', '')}".encode()).hexdigest()[:16]
    base = str(Path(tempfile.gettempdir()) / f"scroll-capture-{identity}")
    lock = QLockFile(base + ".lock")
    lock.setStaleLockTime(0)
    command = "scroll" if args.scroll else "capture" if args.capture else "show"
    if not lock.tryLock(0):
        if forward_request(base + ".sock", command):
            return 0
        print("Scroll Capture is already starting or did not respond. Try again shortly.", file=sys.stderr)
        return 1
    try:
        server = InstanceServer(base + ".sock")
        controller = ApplicationController(application)
        server.requested.connect(controller.request)
        QTimer.singleShot(0, lambda: controller.request(command))
        signal.signal(signal.SIGINT, lambda *_: controller.quit())
        signal.signal(signal.SIGTERM, lambda *_: controller.quit())
        signal_timer = QTimer()
        signal_timer.start(250)
        signal_timer.timeout.connect(lambda: None)
        result = application.exec()
        server.server.close()
        return result
    finally:
        lock.unlock()


if __name__ == "__main__":
    raise SystemExit(main())
