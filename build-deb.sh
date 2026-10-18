#!/usr/bin/env bash
# Build a local Debian/Ubuntu package without installing anything on the host.
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    printf 'Usage: %s [output-directory]\n' "${0##*/}"
    printf 'Default output: %s/dist\n' "$project_dir"
    printf 'Set PYTHON_BIN to choose the build interpreter (defaults to .venv/bin/python or python3).\n'
}

if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
    usage
    exit 0
fi
if (( $# > 1 )) || [[ "${1:-}" == -* ]]; then
    usage >&2
    exit 2
fi

for tool in dpkg dpkg-deb dpkg-shlibdeps objdump; do
    if ! command -v "$tool" >/dev/null; then
        printf 'Missing build tool: %s\nInstall build prerequisites: sudo apt install dpkg-dev binutils python3-pip python3-venv\n' "$tool" >&2
        exit 1
    fi
done

python_bin="${PYTHON_BIN:-python3}"
if [[ -z "${PYTHON_BIN:-}" && -x "$project_dir/.venv/bin/python" ]]; then
    python_bin="$project_dir/.venv/bin/python"
fi
if ! "$python_bin" -m pip --version >/dev/null 2>&1; then
    printf 'The build interpreter needs pip: %s\n' "$python_bin" >&2
    exit 1
fi
python_version="$("$python_bin" -c 'import sys; assert sys.platform == "linux" and sys.implementation.name == "cpython" and sys.version_info >= (3, 10), "Build on Linux with CPython 3.10 or newer"; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
architecture="$(dpkg --print-architecture)"
output_dir="${1:-$project_dir/dist}"
mkdir -p -- "$output_dir" "$project_dir/build/pip-cache"
output_dir="$(cd -- "$output_dir" && pwd)"
work_dir="$(mktemp -d "${TMPDIR:-/tmp}/scroll-capture-deb.XXXXXX")"
trap 'rm -rf -- "$work_dir"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

package_root="$work_dir/package"
runtime="$package_root/opt/scroll-capture/lib"
mkdir -p "$work_dir/wheels" "$runtime" "$package_root/usr/bin" \
    "$package_root/usr/share/applications" \
    "$package_root/usr/share/icons/hicolor/scalable/apps" \
    "$package_root/usr/share/doc/scroll-capture"

printf 'Building the application wheel with Python %s (%s)...\n' "$python_version" "$architecture"
"$python_bin" -m pip wheel --quiet --no-deps --cache-dir "$project_dir/build/pip-cache" \
    --wheel-dir "$work_dir/wheels" "$project_dir"
wheels=("$work_dir"/wheels/scroll_capture-*.whl)
if (( ${#wheels[@]} != 1 )) || [[ ! -f "${wheels[0]}" ]]; then
    printf 'Expected one application wheel.\n' >&2
    exit 1
fi

printf 'Bundling Python dependencies (binary wheels only)...\n'
"$python_bin" -m pip install --quiet --ignore-installed --no-compile --only-binary=:all: \
    --cache-dir "$project_dir/build/pip-cache" --target "$runtime" "${wheels[0]}"
# Vendor tools have build-machine shebangs and are not part of our public CLI.
rm -rf -- "$runtime/bin"

cat > "$package_root/opt/scroll-capture/run.py" <<'PY'
"""Installed entry point; load private dependencies independently of the checkout."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from scroll_capture.app import main

raise SystemExit(main())
PY

cat > "$package_root/usr/bin/scroll-capture" <<EOF
#!/bin/sh
exec /usr/bin/python${python_version} -I -B /opt/scroll-capture/run.py "\$@"
EOF
chmod 755 "$package_root/usr/bin/scroll-capture"
install -m644 "$project_dir/packaging/scroll-capture.desktop" "$package_root/usr/share/applications/scroll-capture.desktop"
install -m644 "$project_dir/src/scroll_capture/assets/icon.svg" "$package_root/usr/share/icons/hicolor/scalable/apps/scroll-capture.svg"
gzip -9 -n -c "$project_dir/README.md" > "$package_root/usr/share/doc/scroll-capture/README.md.gz"
install -m644 "$project_dir/LICENSE" "$package_root/usr/share/doc/scroll-capture/copyright"

printf 'Resolving native library dependencies and writing package metadata...\n'
package_version="$("$python_bin" "$project_dir/packaging/deb_metadata.py" "$package_root" "$python_version" "$architecture")"
chmod -R u=rwX,go=rX "$package_root"
if command -v desktop-file-validate >/dev/null; then
    desktop-file-validate "$package_root/usr/share/applications/scroll-capture.desktop"
fi

artifact="$output_dir/scroll-capture_${package_version}_${architecture}.deb"
dpkg-deb --root-owner-group -Zxz -z3 --threads-max=2 --build "$package_root" "$work_dir/package.deb"
mv -- "$work_dir/package.deb" "$artifact"
printf '\nBuilt: %s\nInstall with:\n  sudo apt install %q\n' "$artifact" "$artifact"
