"""Exercise a catalog-style copy without pip installing Forgebox.

The child interpreter ignores PYTHONPATH, site initialization, editable-install
hooks, and the checkout cwd. Only the host's dependency directories are exposed;
the plugin itself must make its copied source tree importable.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("loader", ["shim", "hermes"])
@pytest.mark.parametrize("action", ["apply", "discard"])
def test_catalog_dashboard_without_installed_package(tmp_path, git_repo, action, loader):
    hermes_source = os.environ.get("HERMES_SOURCE", "")
    if loader == "hermes":
        if not hermes_source:
            pytest.skip("Set HERMES_SOURCE to exercise the real Hermes dashboard loader")
        hermes_source = str(Path(hermes_source).resolve())
        assert (Path(hermes_source) / "hermes_cli" / "web_server_dashboard.py").is_file()
    # Copy only the runtime plugin files, as a catalog installer would. Never
    # rely on the checkout, its editable distribution metadata, or test fixtures
    # having imported forgebox in the child process.
    plugin = tmp_path / "hermes home" / "plugins" / "forgebox"
    plugin.mkdir(parents=True)
    for name in ("dashboard", "src", "desktop"):
        shutil.copytree(
            ROOT / name, plugin / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "*.pyc"),
        )
    for name in ("plugin.yaml", "__init__.py"):
        shutil.copy2(ROOT / name, plugin / name)
    dependencies = sorted({sysconfig.get_path("purelib"), sysconfig.get_path("platlib")})
    script = textwrap.dedent(r'''
        import importlib.util
        import json
        import os
        from pathlib import Path
        import sys

        plugin, repo, action = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
        # -I -S skips site, .pth processing, cwd and PYTHONPATH. Adding dependency
        # directories directly makes FastAPI available, not editable installs.
        sys.path.extend(json.loads(sys.argv[4]))
        assert importlib.util.find_spec("forgebox") is None, sys.path
        assert "forgebox" not in sys.modules
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        manifest = json.loads((plugin / "dashboard" / "manifest.json").read_text())
        assert manifest == {
            "name": "forgebox", "api": "plugin_api.py", "tab": {"hidden": True},
        }
        app = FastAPI()
        if sys.argv[5] == "hermes":
            os.environ["HERMES_HOME"] = str(plugin.parent.parent)
            sys.path.insert(0, sys.argv[6])
            from hermes_cli.web_server_dashboard import (
                _discover_dashboard_plugins, _mount_one_plugin_api,
            )
            discovered = [entry for entry in _discover_dashboard_plugins()
                          if entry["name"] == "forgebox"]
            assert len(discovered) == 1, discovered
            entry = discovered[0]
            assert entry["source"] == "user" and entry["tab"]["hidden"] is True
            assert Path(entry["_dir"]) == plugin / "dashboard"
            # Installed is not enabled: verify the real trust gate before import.
            assert not _mount_one_plugin_api(app, entry, set(), set())
            assert importlib.util.find_spec("forgebox") is None
            assert not _mount_one_plugin_api(app, entry, {"forgebox"}, {"forgebox"})
            assert _mount_one_plugin_api(app, entry, {"forgebox"}, set())
        else:
            spec = importlib.util.spec_from_file_location(
                "catalog_forgebox_dashboard", plugin / "dashboard" / manifest["api"],
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            # Reimporting the shim must not accumulate source-root entries.
            spec.loader.exec_module(module)
            app.include_router(module.router, prefix="/api/plugins/" + manifest["name"])
        assert sys.path.count(str(plugin / "src")) == 1
        assert "forgebox.core.snapshot" not in sys.modules
        client = TestClient(app)
        prefix = "/api/plugins/forgebox/"

        def post(endpoint, **params):
            response = client.post(prefix + endpoint, json={"repo": str(repo), **params})
            assert response.status_code == 200, response.text
            value = response.json()
            assert value["ok"] is True, value
            return value

        # This is the original regression: importing the router succeeds, but its
        # first request fails if the shim omitted the source path for core imports.
        assert post("list_sessions")["sessions"] == []
        from forgebox.core import snapshot
        assert Path(snapshot.__file__).is_relative_to(plugin / "src")
        snap = snapshot.create_snapshot(repo)
        worktree = Path(snapshot.create_worktree(snap))
        try:
            (worktree / "tracked.txt").write_text("catalog experiment\n")
            sessions = post("list_sessions")["sessions"]
            assert len(sessions) == 1 and sessions[0]["id"] == snap.id
            assert post("status", session_id=snap.id)["session"]["id"] == snap.id
            difference = post("diff", session_id=snap.id)
            assert "catalog experiment" in difference["patch"]
            assert post("preflight", session_id=snap.id)["conflicts"] == []
            # Both destructive routes must still refuse an implicit session.
            for endpoint in ("apply", "discard"):
                denied = client.post(prefix + endpoint, json={"repo": str(repo)}).json()
                assert denied["ok"] is False and denied["code"] == "ValueError", denied
            result = post(action, session_id=snap.id)
            if action == "apply":
                assert "tracked.txt" in result["applied"]
                assert (repo / "tracked.txt").read_text() == "catalog experiment\n"
                post("discard", session_id=snap.id)
            else:
                assert (repo / "tracked.txt").read_text() == "dirty tracked\n"
            assert post("list_sessions")["sessions"] == []
            assert not worktree.exists()
        finally:
            from forgebox.core.apply import discard
            discard(repo, snap)
    ''')
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script, str(plugin), str(git_repo),
         action, json.dumps(dependencies), loader, hermes_source],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
