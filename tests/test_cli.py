import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import pytest


def test_version_without_display():
    env = os.environ.copy()
    env.pop("DISPLAY", None)
    result = subprocess.run([sys.executable, "-m", "scroll_capture", "--version"], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert "Scroll Capture 0.1.0" in result.stdout


def test_wayland_has_a_clear_error():
    env = dict(os.environ, DISPLAY=":999", XDG_SESSION_TYPE="wayland")
    result = subprocess.run([sys.executable, "-m", "scroll_capture", "--scroll"], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert "X11" in result.stderr and "Wayland" in result.stderr


@pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="Run under xvfb-run")
def test_second_cli_reuses_first_instance_and_shutdown_is_clean(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    env = dict(os.environ, XDG_SESSION_TYPE="x11", XDG_CONFIG_HOME=str(tmp_path / "config"), XDG_RUNTIME_DIR=str(runtime))
    command = [sys.executable, "-m", "scroll_capture"]
    first = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        identity = hashlib.sha256(f"{os.getuid()}:{env['DISPLAY']}".encode()).hexdigest()[:16]
        socket_path = Path(tempfile.gettempdir()) / f"scroll-capture-{identity}.sock"
        for _ in range(100):
            assert first.poll() is None
            if socket_path.exists():
                break
            time.sleep(0.05)
        assert socket_path.exists(), "First instance did not start listening"
        second = subprocess.run(command + ["--scroll"], env=env, capture_output=True, text=True, timeout=10)
        assert second.returncode == 0, second.stderr
        assert first.poll() is None
    finally:
        first.terminate()
        stdout, stderr = first.communicate(timeout=10)
    assert first.returncode == 0, stdout + stderr
    assert "Traceback" not in stderr
