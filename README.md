# Scroll Capture

A small native screenshot app for **Linux X11**. Select the visible scrolling
content in any desktop app, scroll downward yourself, and export one tall image.
Includes ordinary region screenshots and a basic annotation editor.

## Run

Requires Python 3.10 or newer and an X11 desktop session.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
./launch.sh
```

The launcher stays available in the system tray. Closing the launcher hides it;
**Quit** exits the app. On desktops without a tray, the launcher remains available
and closing it exits. Autostart is optional and is not enabled by installation.

```bash
./launch.sh --scroll   # start a scrolling capture
./launch.sh --capture  # start an ordinary region capture
./launch.sh --version
```

Commands reuse the existing app instance on the same X11 display.
The launch script works from any working directory and forwards all arguments.

If Qt reports a missing `xcb` library on Ubuntu/Mint, install the system runtime
libraries, commonly `libxcb-cursor0`, `libxkbcommon-x11-0`, and `libxcb-xinerama0`.
The Python dependencies use the Qt Essentials package, without a browser engine.

## Capture

1. Start **Scrolling capture**, or press **Ctrl+Alt+Shift+S**. Change the global
   shortcut in Settings if it conflicts with another app.
2. Drag a rectangle **inside the moving content**. Exclude sticky headers,
   footers, fixed sidebars and scrollbars. Select at least 64 × 64 physical pixels
   on one monitor; either monitor is supported.
3. Scroll downward in small steps. Keep at least a quarter of the previous view
   visible and briefly let each movement settle. Keep the window stationary and
   other windows out of the rectangle.
   A green outline marks the capture region, and a live stitched preview shows
   what has actually been accepted. Use **Pause** to inspect it and **Resume** to
   continue; the outline turns amber while paused. You can also pause from the
   tray menu. Keep overlap with the last accepted view when resuming.
   Scrolling back through the preview turns off **Follow latest**; turn it on
   again to follow newly captured rows.
4. Press **Enter** (main keyboard or keypad) to finish, or **Escape** to cancel.
   The floating controls and tray menu also provide Finish and Cancel. These
   temporary shortcuts are released when the capture ends.
5. In the preview, zoom or pan, then add arrows, rectangles or text. Select Crop
   and drag to crop. **Ctrl+Z** undoes changes, **Ctrl+Shift+Z** redoes them,
   **Ctrl+C** copies the image, and **Ctrl+S** saves a PNG. Text size follows the
   Size control. Copying and saving keep the preview open.

The outline and preview stay outside the capture rectangle and are excluded from
the exported image. The outline does not block clicks or scrolling in your app.
When space is limited, the panel switches to compact controls without a preview;
if no controls fit, use the tray menu and keyboard. With no tray, leave room
beside the selection. Edges flush with a monitor boundary have no outline there.
When a match is uncertain, the accepted image is preserved: scroll back until
there is overlap with the last captured view and retry in smaller steps. Finishing
exports only accepted content. The tool waits for you to finish even at the bottom.

The clipboard image remains available while the application is running; some X11
desktops require a clipboard manager to retain it after quitting.

## Desktop launcher

After installing into `.venv`, you can optionally register a launcher:

```bash
.venv/bin/python packaging/install_desktop.py
```

The installer registers the launcher and its Region/Scrolling actions with the
absolute path to your installed command. It writes only to your per-user data
directory; moving the virtual environment requires running it again.

Alternatively, set a desktop keyboard shortcut to the absolute path of
`.venv/bin/scroll-capture --scroll`. The app does not modify existing shortcuts.

## Build an installable .deb

Build on the Debian/Ubuntu machine or release you want to target. The package
bundles the app and its Python dependencies and declares native library
dependencies for APT. Installation does not need pip, a virtual environment or
this repository. It requires the same Python minor version and architecture as
the build machine.

```bash
sudo apt install dpkg-dev binutils python3-pip python3-venv
./build-deb.sh
sudo apt install ./dist/scroll-capture_0.1.0-1_amd64.deb
```

The resulting file is in `dist/`; its version and architecture are detected
automatically. The build needs internet access to fetch Python wheels and an
X11 desktop's native Qt libraries installed on the build host. It runs without
root and does not install packages on the build host. It uses `.venv/bin/python`
when available, otherwise `python3`.

```bash
./build-deb.sh /tmp/packages          # choose an output directory
PYTHON_BIN=/usr/bin/python3 ./build-deb.sh  # choose the build interpreter
```

After installation, launch **Scroll Capture** from the application menu or run
`scroll-capture`, `scroll-capture --scroll` or `scroll-capture --capture`.
Remove it with `sudo apt remove scroll-capture`. The build uses binary Python
wheels and does not package the project's development environment or tests.

## Limits

- Linux X11 only; Wayland, Windows and macOS are not supported in this version.
- Manual downward scrolling only. Fixed elements are not automatically removed.
- Ambiguous repeated patterns, blank regions, large animations, missed overlap,
  zoom changes and horizontal movement may prevent alignment. Rejected frames
  never replace the last accepted reference.
- A capture stops at 50 megapixels or 50,000 pixels tall. The accepted image is
  retained for export. Display layout changes also end the capture safely.
- Captures stay in memory until explicitly saved or copied. No network services
  are used. Settings are stored in Qt's per-user configuration location.

## Development

```bash
.venv/bin/python -m pytest tests/test_stitching.py
xvfb-run -a -s '-screen 0 1280x1024x24' .venv/bin/python -m pytest
```

Tests exercise exact reconstruction, viewport borders and shadows, uncertain joins
and limits, real X11 region capture, cancellation, global keys, crop/annotation
undo, clipboard and PNG export.
GUI integration tests run on Xvfb so they do not interact with your desktop.

The code separates the pixel-only stitching engine, Qt/X11 capture, hotkey
registration, editor and application lifecycle. Image data is RGB `uint8`; capture
regions use screen-local logical coordinates and exports retain physical pixels.
Joins use matching content inside the overlap and replace the previous frame's
tail, so narrow viewport borders and clipping shadows do not repeat at each join.
The live preview applies those same overlap corrections.

## License

MIT License. See [LICENSE](LICENSE).
