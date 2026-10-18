#!/usr/bin/env python3
"""Install an optional per-user launcher using this interpreter's entry point."""

import argparse
import os
from pathlib import Path
import shutil
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")), help="XDG data directory (default: your per-user data directory)")
    args = parser.parse_args()
    executable = Path(sys.executable).absolute().parent / "scroll-capture"
    if not executable.is_file():
        parser.error("Run this script using the Python environment where scroll-capture is installed.")
    source = Path(__file__).resolve().parent
    template = (source / "scroll-capture.desktop").read_text()
    # Desktop Entry Exec quoting: protect reserved shell characters and field codes.
    quoted = str(executable).replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$").replace("%", "%%")
    content = template.replace("Exec=scroll-capture", f'Exec="{quoted}"')
    applications = args.data_dir / "applications"
    icons = args.data_dir / "icons/hicolor/scalable/apps"
    applications.mkdir(parents=True, exist_ok=True)
    icons.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source.parent / "src/scroll_capture/assets/icon.svg", icons / "scroll-capture.svg")
    target = applications / "scroll-capture.desktop"
    target.write_text(content)
    print(f"Installed {target}")


if __name__ == "__main__":
    main()
