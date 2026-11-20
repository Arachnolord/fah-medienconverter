import importlib
import os
import sys
import json
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(ROOT, "app")


@pytest.fixture(scope="session")
def dirs(tmp_path_factory):
    base = tmp_path_factory.mktemp("mc")
    d = {"config": str(base / "config"), "progress": str(base / "progress"), "media": str(base / "media")}
    for p in d.values():
        os.makedirs(p, exist_ok=True)
    return d


@pytest.fixture(scope="session")
def legacy_history(dirs):
    """Old-format history.json placed BEFORE import so migration runs at import time."""
    entries = [
        {"name": "a.mkv", "path": "/media/x/a.mkv", "size_before": 1000, "size_after": 400,
         "method": "GPU", "timestamp": time.mktime((2026, 8, 10, 13, 2, 0, 0, 0, -1))},
        {"name": "b.mkv", "path": "/media/x/b.mkv", "size_before": 2000, "size_after": 900,
         "method": "GPU", "timestamp": time.mktime((2026, 8, 10, 14, 0, 0, 0, 0, -1))},
        {"name": "c.mp4", "path": "/media/y/c.mp4", "size_before": 3000, "size_after": 1000,
         "method": "CPU", "timestamp": time.mktime((2026, 8, 12, 9, 0, 0, 0, 0, -1))},
    ]
    with open(os.path.join(dirs["config"], "history.json"), "w") as f:
        json.dump(entries, f)
    return entries


@pytest.fixture(scope="session")
def server(dirs, legacy_history):
    os.environ["MC_CONFIG_DIR"] = dirs["config"]
    os.environ["MC_PROGRESS_DIR"] = dirs["progress"]
    os.environ["MC_MEDIA_ROOT"] = dirs["media"]
    os.environ["COOKIE_SECURE"] = "0"
    if APP_DIR not in sys.path:
        sys.path.insert(0, APP_DIR)
    sys.modules.pop("server", None)
    mod = importlib.import_module("server")
    mod.app.config["TESTING"] = True
    return mod


@pytest.fixture
def client(server):
    server.login_guard.__init__()          # fresh limiter per test
    with server.app.test_client() as c:
        yield c


@pytest.fixture
def logged_in(client):
    r = client.post("/api/login", json={"username": "admin", "password": "admin"})
    assert r.status_code == 200, r.get_json()
    return client
