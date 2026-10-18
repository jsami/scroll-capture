"""Keep Qt and desktop integration tests out of the user's configuration."""

import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolated_desktop_settings(tmp_path_factory):
    root = tmp_path_factory.mktemp("desktop-environment")
    updates = {"GSETTINGS_BACKEND": "memory"}
    for variable, name in (("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"), ("XDG_RUNTIME_DIR", "runtime")):
        directory = root / name
        directory.mkdir(mode=0o700)
        updates[variable] = str(directory)
    previous = {key: os.environ.get(key) for key in updates}
    os.environ.update(updates)
    yield
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
