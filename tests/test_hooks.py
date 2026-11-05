"""Hook integration through sessions activated by the public CLI."""

from copy import deepcopy
from datetime import datetime
import importlib
import json

import pytest


@pytest.fixture
def hooks():
    return importlib.import_module("forgebox.hooks")


def _journal_entries(repo):
    entries = []
    for journal in sorted((repo / ".forgebox").rglob("*.jsonl")):
        for line in journal.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entries.append(json.loads(line))
    return entries


def test_pre_tool_call_rewrites_in_repo_path_without_mutating_input(cli_session, hooks):
    repo, worktree = cli_session
    params = {
        "name": "write_file",
        "args": {"path": str(repo / "tracked.txt"), "content": "sandbox content"},
        "task_id": "rewrite-test",
    }
    original = deepcopy(params)
    result = hooks.pre_tool_call(params)
    assert result is not None
    assert not result.get("abort", False)
    assert result["args"]["path"] == str(worktree / "tracked.txt")
    assert result["args"]["content"] == "sandbox content"
    assert params == original, "rewrites must use a mutable copy, not mutate caller args"
    assert (repo / "tracked.txt").read_text() == "dirty tracked\n"


def test_pre_tool_call_rewrites_command_cwd(cli_session, hooks):
    repo, worktree = cli_session
    result = hooks.pre_tool_call({
        "name": "terminal",
        "args": {"command": "git status --short", "cwd": str(repo)},
        "task_id": "cwd-test",
    })
    assert result is not None
    assert result["args"]["cwd"] == str(worktree)
    assert result["args"]["command"] == "git status --short"


@pytest.mark.parametrize("escape", ["outside", "prefix-sibling", "parent-traversal"])
def test_pre_tool_call_aborts_paths_outside_sandbox(cli_session, hooks, escape):
    repo, _ = cli_session
    targets = {
        "outside": repo.parent / "outside.txt",
        "prefix-sibling": repo.parent / (repo.name + "-other") / "file.txt",
        "parent-traversal": repo / ".." / "outside.txt",
    }
    result = hooks.pre_tool_call({
        "name": "write_file",
        "args": {"path": str(targets[escape]), "content": "must not write"},
        "task_id": "escape-test",
    })
    assert result is not None
    assert result.get("abort") is True
    assert "outside" in result.get("reason", "").lower()
    assert not targets[escape].exists()


def test_pre_tool_call_blocks_symlink_escape(cli_session, hooks):
    repo, _ = cli_session
    outside = repo.parent / "outside-directory"
    outside.mkdir()
    (repo / "escape-link").symlink_to(outside, target_is_directory=True)
    result = hooks.pre_tool_call({
        "name": "write_file",
        "args": {"path": str(repo / "escape-link" / "escaped.txt"), "content": "blocked"},
        "task_id": "symlink-test",
    })
    assert result is not None
    assert result.get("abort") is True
    assert not (outside / "escaped.txt").exists()


def test_hooks_are_noops_without_an_active_session(git_repo, hooks, monkeypatch):
    monkeypatch.chdir(git_repo)
    params = {
        "name": "write_file",
        "args": {"path": str(git_repo / "tracked.txt"), "content": "untouched"},
        "task_id": "no-session-test",
    }
    original = deepcopy(params)
    assert hooks.pre_tool_call(params) is None
    assert hooks.post_tool_call({**params, "result": {"exit_code": 0}}) is None
    assert params == original
    assert (git_repo / "tracked.txt").read_text() == "dirty tracked\n"
    assert _journal_entries(git_repo) == []


def test_pre_tool_call_journals_rewrite(cli_session, hooks):
    repo, _ = cli_session
    before = _journal_entries(repo)
    hooks.pre_tool_call({
        "name": "read_file",
        "args": {"path": str(repo / "tracked.txt")},
        "task_id": "pre-journal-test",
    })
    after = _journal_entries(repo)
    assert len(after) > len(before)
    assert "read_file" in json.dumps(after[len(before):])
    assert "tracked.txt" in json.dumps(after[len(before):])


def test_post_tool_call_journals_command_exit_paths_and_timestamp(cli_session, hooks):
    repo, worktree = cli_session
    before = _journal_entries(repo)
    hooks.post_tool_call({
        "name": "terminal",
        "args": {
            "command": "pytest -q",
            "cwd": str(worktree),
            "path": str(worktree / "tracked.txt"),
        },
        "result": {"exit_code": 7, "changed_files": ["tracked.txt"]},
        "task_id": "post-journal-test",
    })
    after = _journal_entries(repo)
    assert len(after) > len(before), "post hook must append to the real JSONL journal"
    new_entries = after[len(before):]
    payload = json.dumps(new_entries)
    assert "terminal" in payload
    assert "pytest -q" in payload
    assert "tracked.txt" in payload
    assert any(entry.get("exit_code") == 7 for entry in new_entries), new_entries
    # ARCH.md requires a timestamp, but does not prescribe its representation.
    timestamps = []
    for entry in new_entries:
        for key, value in entry.items():
            if key in {"timestamp", "time", "ts", "at", "created_at"}:
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    timestamps.append(datetime.fromtimestamp(value))
                elif isinstance(value, str):
                    try:
                        timestamps.append(datetime.fromisoformat(value.replace("Z", "+00:00")))
                    except ValueError:
                        pass
    assert timestamps, new_entries
