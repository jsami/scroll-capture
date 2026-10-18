"""X11 passive key grabs integrated into the Qt event loop."""

from collections.abc import Callable

from PySide6.QtCore import QObject, QSocketNotifier
from PySide6.QtGui import QKeySequence
from Xlib import X, XK, display, error


class HotkeyError(RuntimeError):
    pass


class Hotkeys(QObject):
    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.closed = False
        self.connection = display.Display()
        self.root = self.connection.screen().root
        self.bindings: dict[str, tuple[int, int, Callable[[], None]]] = {}
        num_code = self.connection.keysym_to_keycode(XK.string_to_keysym("Num_Lock"))
        num_mask = 0
        for index, codes in enumerate(self.connection.get_modifier_mapping()):
            if num_code and num_code in codes:
                num_mask |= 1 << index
        self.lock_mask = X.LockMask | num_mask
        self.lock_variants = sorted({0, X.LockMask, num_mask, self.lock_mask})
        self.notifier = QSocketNotifier(self.connection.fileno(), QSocketNotifier.Type.Read, self)
        self.notifier.activated.connect(self._drain)

    def _parse(self, sequence: str) -> tuple[int, int]:
        key_sequence = QKeySequence.fromString(sequence, QKeySequence.SequenceFormat.PortableText)
        if key_sequence.isEmpty() or key_sequence.count() != 1:
            raise HotkeyError("Choose one key combination, such as Ctrl+Alt+Shift+S.")
        parts = key_sequence.toString(QKeySequence.SequenceFormat.PortableText).split("+")
        modifier_names = {"Ctrl": X.ControlMask, "Alt": X.Mod1Mask, "Shift": X.ShiftMask, "Meta": X.Mod4Mask}
        modifiers = 0
        for part in parts[:-1]:
            if part not in modifier_names:
                raise HotkeyError(f"Unsupported shortcut modifier: {part}")
            modifiers |= modifier_names[part]
        key = parts[-1]
        aliases = {"Enter": "KP_Enter", "Return": "Return", "Esc": "Escape", "Space": "space", "Del": "Delete", "Ins": "Insert", "PgUp": "Prior", "PgDown": "Next", "Backspace": "BackSpace"}
        symbol = XK.string_to_keysym(aliases.get(key, key.lower() if len(key) == 1 else key))
        code = self.connection.keysym_to_keycode(symbol) if symbol else 0
        if not code:
            raise HotkeyError(f"Cannot register the key '{key}' on this keyboard.")
        return code, modifiers

    def register(self, name: str, sequence: str, callback: Callable[[], None]) -> None:
        code, modifiers = self._parse(sequence)
        for existing_name, (existing_code, existing_modifiers, _) in self.bindings.items():
            if existing_name != name and (existing_code, existing_modifiers) == (code, modifiers):
                raise HotkeyError("This shortcut is already used by Scroll Capture.")
        self.unregister(name)
        errors = []
        for locks in self.lock_variants:
            self.root.grab_key(code, modifiers | locks, False, X.GrabModeAsync, X.GrabModeAsync, onerror=lambda e, _r: errors.append(e))
        self.connection.sync()
        if errors:
            for locks in self.lock_variants:
                self.root.ungrab_key(code, modifiers | locks)
            self.connection.sync()
            raise HotkeyError(f"Shortcut '{sequence}' is in use by another app. Choose another shortcut or use the tray menu.")
        self.bindings[name] = (code, modifiers, callback)

    def unregister(self, name: str) -> None:
        binding = self.bindings.pop(name, None)
        if binding:
            code, modifiers, _ = binding
            for locks in self.lock_variants:
                self.root.ungrab_key(code, modifiers | locks)
            self.connection.flush()

    def _drain(self, *_args) -> None:
        try:
            while self.connection.pending_events():
                event = self.connection.next_event()
                if event.type == X.MappingNotify:
                    self.connection.refresh_keyboard_mapping(event)
                if event.type != X.KeyPress:
                    continue
                state = event.state & ~self.lock_mask
                for code, modifiers, callback in list(self.bindings.values()):
                    if code == event.detail and modifiers == state:
                        callback()
                        break
        except (error.ConnectionClosedError, OSError):
            self.notifier.setEnabled(False)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.notifier.setEnabled(False)
        for name in list(self.bindings):
            self.unregister(name)
        self.connection.close()
