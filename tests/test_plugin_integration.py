"""Exercise Forgebox registration and hooks through the real Hermes manager.

CI supplies the pinned upstream checkout via HERMES_SOURCE. For local runs:
HERMES_SOURCE=/path/to/hermes-agent PYTHONPATH=src pytest tests/test_plugin_integration.py
No manager, context, dispatch, or hook implementation is mocked.
"""

from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest


HERMES_SOURCE = os.environ.get("HERMES_SOURCE")
if HERMES_SOURCE:
    source = Path(HERMES_SOURCE).resolve()
    if not (source / "hermes_cli" / "plugins.py").is_file():
        raise RuntimeError(f"HERMES_SOURCE does not contain Hermes: {source}")
    sys.path.insert(0, str(source))
elif importlib.util.find_spec("hermes_cli") is None:
    pytest.skip("Set HERMES_SOURCE to run real Hermes integration tests", allow_module_level=True)

from hermes_cli.plugins import PluginContext, PluginManager, parse_manifest_file


@pytest.fixture
def plugin_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    root = Path(__file__).resolve().parents[1]
    manifest = parse_manifest_file(root / "plugin.yaml", root, "user", "")
    assert manifest is not None
    manager = PluginManager()
    spec = importlib.util.spec_from_file_location(
        "_forgebox_integration_entry", root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.register(PluginContext(manifest, manager))
    for name in ("pre_tool_call", "post_tool_call", "post_approval_response"):
        assert manager.has_hook(name), f"Plugin entry point did not register {name}"
    yield manager
    manager.unload(manifest)


def entries(repo):
    return [json.loads(line) for path in (repo / ".forgebox").rglob("journal.jsonl")
            for line in path.read_text().splitlines() if line.strip()]


def test_real_manager_rewrites_paths_and_terminal_cwd(cli_session, plugin_manager):
    repo, worktree = cli_session
    args = {"path": str(repo / "tracked.txt"), "content": "literal ./example"}
    before = deepcopy(args)
    results = plugin_manager.invoke_hook(
        "pre_tool_call", tool_name="write_file", args=args,
        session_id="hermes-session", task_id="integration-write", future_field=True,
    )
    assert results == [{"action": "modify", "args": {
        **args, "path": str(worktree / "tracked.txt"),
    }}]
    assert args == before
    results = plugin_manager.invoke_hook(
        "pre_tool_call", tool_name="terminal", args={"command": "pwd"},
        session_id="hermes-session", task_id="integration-terminal",
    )
    assert len(results) == 1 and results[0]["action"] == "modify"
    assert results[0]["args"]["workdir"] == str(worktree)
    assert results[0]["args"]["command"] == "pwd"


def test_real_manager_blocks_escape(cli_session, plugin_manager):
    repo, _ = cli_session
    result = plugin_manager.invoke_hook(
        "pre_tool_call", tool_name="write_file",
        args={"path": str(repo.parent / "escape.txt"), "content": "blocked"},
        session_id="hermes-session", task_id="integration-block",
    )
    assert result == [{"action": "block", "message": "Forgebox: path outside sandbox"}]
    assert not (repo.parent / "escape.txt").exists()


def test_real_manager_records_result_and_only_observes_approval(cli_session, plugin_manager):
    repo, worktree = cli_session
    original = (repo / "tracked.txt").read_bytes()
    (worktree / "tracked.txt").write_text("experiment must not auto-apply\n")
    assert plugin_manager.invoke_hook(
        "post_tool_call", tool_name="terminal", args={"command": "pytest -q"},
        result={"exit_code": 3}, session_id="hermes-session", task_id="integration-result",
    ) == []
    assert plugin_manager.invoke_hook(
        "post_approval_response", choice="approve", decided_by="user",
        command="pytest -q", session_key="hermes-session", surface="cli",
        # Legacy-looking payload must not regain the removed apply side effect.
        approved=True, pending="apply",
    ) == []
    assert (repo / "tracked.txt").read_bytes() == original
    assert (repo / ".forgebox" / "active.json").exists()
    journal = entries(repo)
    assert any(e.get("event") == "post_tool_call" and e.get("exit_code") == 3 for e in journal)
    assert any(e.get("event") == "post_approval_response" and e.get("choice") == "approve"
               for e in journal)
