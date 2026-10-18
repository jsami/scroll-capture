"""Discover native dependencies and write metadata for build-deb.sh."""

import hashlib
from importlib.metadata import distributions
from pathlib import Path
import subprocess
import sys


def native_dependencies(runtime: Path, work: Path) -> str:
    qt = runtime / "PySide6" / "Qt"
    candidates = []
    # Inspect the modules used by the app and the X11/SVG plugins they load.
    # Optional Qt modules shipped in the wheel need not pull in unrelated apps.
    for module in ("QtCore", "QtGui", "QtWidgets", "QtNetwork"):
        candidates.extend((runtime / "PySide6").glob(f"{module}*.so"))
    candidates.extend((runtime / "PySide6").glob("libpyside6.abi3.so*"))
    for library in ("Core", "Gui", "Widgets", "Network", "DBus", "XcbQpa", "Svg"):
        candidates.extend((qt / "lib").glob(f"libQt6{library}.so*"))
    for plugin in ("platforms/libqxcb.so", "iconengines/libqsvgicon.so", "imageformats/libqsvg.so"):
        path = qt / "plugins" / plugin
        if not path.is_file():
            raise RuntimeError(f"Required Qt plugin is missing: {path}")
        candidates.append(path)
    for directory in [runtime / "shiboken6", runtime / "cv2", runtime / "numpy", *runtime.glob("*.libs")]:
        candidates.extend(directory.rglob("*.so*"))
    binaries = set()
    for path in candidates:
        if path.is_file():
            with path.open("rb") as stream:
                if stream.read(4) == b"\x7fELF":
                    binaries.add(path.resolve())
    if not binaries:
        raise RuntimeError("No native runtime libraries found")

    # dpkg-shlibdeps reads source-package context, kept outside the payload.
    debian = work / "debian"
    debian.mkdir()
    (debian / "control").write_text(
        "Source: scroll-capture\nSection: graphics\nPriority: optional\n"
        "Maintainer: Local build <noreply@localhost>\n\n"
        "Package: scroll-capture\nArchitecture: any\nDescription: Scrolling screenshot tool\n"
    )
    private_dirs = [qt / "lib", runtime / "PySide6", runtime / "shiboken6", *runtime.glob("*.libs")]
    command = ["dpkg-shlibdeps", "--ignore-missing-info", "--warnings=0", "-O"]
    command.extend(f"-l{path}" for path in private_dirs)
    command.extend(f"-e{path}" for path in sorted(binaries))
    result = subprocess.run(command, cwd=work, text=True, capture_output=True)
    if result.returncode:
        print(result.stderr, file=sys.stderr, end="")
        result.check_returncode()
    for line in result.stdout.splitlines():
        if line.startswith("shlibs:Depends="):
            return line.partition("=")[2]
    raise RuntimeError("dpkg-shlibdeps did not produce native dependencies")


def main(root: Path, python_version: str, architecture: str):
    runtime = root / "opt/scroll-capture/lib"
    packages = sorted((dist.metadata["Name"], dist.version) for dist in distributions(path=[str(runtime)]))
    version = next(version for name, version in packages if name == "scroll-capture") + "-1"
    subprocess.run(["dpkg", "--validate-version", version], check=True)
    # This marker also lets dpkg-shlibdeps resolve private $ORIGIN paths.
    control = root / "DEBIAN"
    control.mkdir()
    dependencies = native_dependencies(runtime, root.parent)
    docs = root / "usr/share/doc/scroll-capture"
    (docs / "bundled-python-packages.txt").write_text("".join(f"{name}=={version}\n" for name, version in packages))
    installed_size = subprocess.check_output(["du", "-sk", str(root)], text=True).split()[0]
    (control / "control").write_text(
        f"Package: scroll-capture\nVersion: {version}\nSection: graphics\nPriority: optional\n"
        f"Architecture: {architecture}\nMaintainer: Local build <noreply@localhost>\n"
        f"Installed-Size: {installed_size}\nDepends: python{python_version}, {dependencies}\n"
        "Description: Region screenshots and manual scrolling capture for X11\n"
        " Select a desktop region, scroll downward and stitch one tall image.\n"
        " Includes a live preview, annotations, crop, clipboard copy and PNG export.\n"
        " Python dependencies are bundled privately under /opt/scroll-capture.\n"
    )
    with (control / "md5sums").open("w") as output:
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink() and control not in path.parents:
                digest = hashlib.md5()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                output.write(f"{digest.hexdigest()}  {path.relative_to(root).as_posix()}\n")
    print(version)


if __name__ == "__main__":
    try:
        main(Path(sys.argv[1]).resolve(), sys.argv[2], sys.argv[3])
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Package metadata failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
