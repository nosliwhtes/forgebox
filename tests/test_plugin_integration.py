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
from agent.runtime_cwd import reset_session_cwd, set_session_cwd


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


def test_absolute_file_path_discovers_session_outside_repo(
    cli_session, plugin_manager, tmp_path, monkeypatch,
):
    repo, worktree = cli_session
    launch = tmp_path / "unrelated launch directory"
    launch.mkdir()
    monkeypatch.chdir(launch)
    original = (repo / "tracked.txt").read_text()
    args = {"path": str(repo / "tracked.txt"), "content": "sandbox edit\n"}
    task_id = "integration-absolute-discovery"
    token = set_session_cwd(str(launch))
    try:
        result = plugin_manager.invoke_hook(
            "pre_tool_call", tool_name="write_file", args=args,
            session_id="hermes-session", task_id=task_id,
        )
        assert result == [{"action": "modify", "args": {
            **args, "path": str(worktree / "tracked.txt"),
        }}]
        rewritten = result[0]["args"]
        Path(rewritten["path"]).write_text(rewritten["content"])
        assert plugin_manager.invoke_hook(
            "post_tool_call", tool_name="write_file", args=rewritten,
            result={"success": True}, session_id="hermes-session", task_id=task_id,
        ) == []
    finally:
        reset_session_cwd(token)
    assert (repo / "tracked.txt").read_text() == original
    assert (worktree / "tracked.txt").read_text() == args["content"]
    assert not (launch / "tracked.txt").exists()
    assert [entry["event"] for entry in entries(repo) if entry.get("task_id") == task_id] == [
        "pre_tool_call", "post_tool_call",
    ]


@pytest.mark.parametrize("workspace_source", ["runtime-context", "task-override", "task-record"])
def test_relative_file_path_uses_nested_hermes_workspace(
    cli_session, plugin_manager, tmp_path, monkeypatch, workspace_source,
):
    repo, worktree = cli_session
    launch = tmp_path / "unrelated launch directory"
    launch.mkdir()
    (launch / "new.txt").write_text("launch file must stay unchanged\n")
    monkeypatch.chdir(launch)
    nested = repo / "nested"
    original = (nested / "new.txt").read_text()
    task_id = f"integration-relative-{workspace_source}"
    task_workspace = workspace_source != "runtime-context"
    if task_workspace:
        from tools.terminal_tool import (
            clear_task_env_overrides, record_session_cwd, register_task_env_overrides,
        )
    args = {"path": "new.txt", "content": "nested sandbox edit\n"}
    token = set_session_cwd(str(repo if task_workspace else nested))
    try:
        if task_workspace:
            initial = repo if workspace_source == "task-record" else nested
            register_task_env_overrides(task_id, {"cwd": str(initial)})
            if workspace_source == "task-record":
                record_session_cwd(task_id, str(nested))
        result = plugin_manager.invoke_hook(
            "pre_tool_call", tool_name="write_file", args=args,
            session_id="hermes-session", task_id=task_id,
        )
        assert result == [{"action": "modify", "args": {
            **args, "path": str(worktree / "nested" / "new.txt"),
        }}]
        rewritten = result[0]["args"]
        Path(rewritten["path"]).write_text(rewritten["content"])
        assert plugin_manager.invoke_hook(
            "post_tool_call", tool_name="write_file", args=rewritten,
            result={"success": True}, session_id="hermes-session", task_id=task_id,
        ) == []
    finally:
        if task_workspace:
            clear_task_env_overrides(task_id)
        reset_session_cwd(token)
    assert (nested / "new.txt").read_text() == original
    assert (worktree / "nested" / "new.txt").read_text() == args["content"]
    assert (launch / "new.txt").read_text() == "launch file must stay unchanged\n"
    assert not (repo / "new.txt").exists()
    assert not (worktree / "new.txt").exists()
    journal = [entry for entry in entries(repo) if entry.get("task_id") == task_id]
    assert [entry["event"] for entry in journal] == [
        "pre_tool_call", "post_tool_call",
    ]
    assert all(entry["changed_files"] == ["nested/new.txt"] for entry in journal)


def test_workspace_without_session_does_not_inherit_process_session(
    cli_session, plugin_manager, tmp_path,
):
    repo, _ = cli_session
    workspace = tmp_path / "other workspace"
    workspace.mkdir()
    args = {"path": "file.txt", "content": "ordinary edit"}
    before = entries(repo)
    token = set_session_cwd(str(workspace))
    try:
        assert plugin_manager.invoke_hook(
            "pre_tool_call", tool_name="write_file", args=args, task_id="other-workspace",
        ) == []
    finally:
        reset_session_cwd(token)
    assert entries(repo) == before
