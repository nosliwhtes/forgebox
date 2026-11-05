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
        "tool_name": "write_file",
        "args": {"path": str(repo / "tracked.txt"), "content": "sandbox content"},
        "task_id": "rewrite-test",
    }
    original = deepcopy(params)
    result = hooks.pre_tool_call(**params)
    assert result is not None
    assert result["action"] == "modify"
    assert result["args"]["path"] == str(worktree / "tracked.txt")
    assert result["args"]["content"] == "sandbox content"
    assert params == original, "rewrites must use a mutable copy, not mutate caller args"
    assert (repo / "tracked.txt").read_text() == "dirty tracked\n"


def test_pre_tool_call_rewrites_command_cwd(cli_session, hooks):
    repo, worktree = cli_session
    result = hooks.pre_tool_call(**{
        "tool_name": "terminal",
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
    result = hooks.pre_tool_call(**{
        "tool_name": "write_file",
        "args": {"path": str(targets[escape]), "content": "must not write"},
        "task_id": "escape-test",
    })
    assert result is not None
    assert result["action"] == "block"
    assert "outside" in result.get("message", "").lower()
    assert not targets[escape].exists()


def test_pre_tool_call_blocks_symlink_escape(cli_session, hooks):
    repo, _ = cli_session
    outside = repo.parent / "outside-directory"
    outside.mkdir()
    (repo / "escape-link").symlink_to(outside, target_is_directory=True)
    result = hooks.pre_tool_call(**{
        "tool_name": "write_file",
        "args": {"path": str(repo / "escape-link" / "escaped.txt"), "content": "blocked"},
        "task_id": "symlink-test",
    })
    assert result is not None
    assert result["action"] == "block"
    assert not (outside / "escaped.txt").exists()


def test_hooks_are_noops_without_an_active_session(git_repo, hooks, monkeypatch):
    monkeypatch.chdir(git_repo)
    params = {
        "tool_name": "write_file",
        "args": {"path": str(git_repo / "tracked.txt"), "content": "untouched"},
        "task_id": "no-session-test",
    }
    original = deepcopy(params)
    assert hooks.pre_tool_call(**params) is None
    assert hooks.post_tool_call(**{**params, "result": {"exit_code": 0}}) is None
    assert params == original
    assert (git_repo / "tracked.txt").read_text() == "dirty tracked\n"
    assert _journal_entries(git_repo) == []


def test_pre_tool_call_journals_rewrite(cli_session, hooks):
    repo, _ = cli_session
    before = _journal_entries(repo)
    hooks.pre_tool_call(**{
        "tool_name": "read_file",
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
    hooks.post_tool_call(**{
        "tool_name": "terminal",
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



@pytest.mark.parametrize("command", [
    "printf 'a/b\\n'\ncat <<'EOF'\nfoo/bar\nEOF\n",
    "python -c 'print(\"/tmp/literal\")'",
    "sed -i 's/old/path/new/path/' file.txt",
    "printf '%s' a/b  > out.txt\n",
])
def test_shell_source_is_preserved_verbatim(cli_session, hooks, command):
    repo, worktree = cli_session
    result = hooks.pre_tool_call(tool_name="terminal", args={"command": command})
    assert result == {"action": "modify", "args": {
        "command": command, "workdir": str(worktree)}}


def test_command_cwd_only_change_is_returned(cli_session, hooks):
    _, worktree = cli_session
    result = hooks.pre_tool_call(tool_name="terminal", args={"command": "pwd"})
    assert result["action"] == "modify"
    assert result["args"]["workdir"] == str(worktree)


def test_subdirectory_and_relative_paths_keep_their_meaning(cli_session, hooks):
    repo, worktree = cli_session
    result = hooks.pre_tool_call(tool_name="write_file", args={
        "cwd": "nested", "path": "new.txt", "content": "a/b"})
    assert result["args"]["cwd"] == str(worktree / "nested")
    assert result["args"]["path"] == str(worktree / "nested/new.txt")
    result = hooks.pre_tool_call(tool_name="terminal", args={
        "workdir": str(repo / "nested"), "command": "cat new.txt"})
    assert result["args"]["workdir"] == str(worktree / "nested")


def test_nested_content_is_not_rewritten(cli_session, hooks):
    repo, worktree = cli_session
    content = {"path": "/tmp/literal", "nested": [{"file": "x/y"}]}
    args = {"path": str(repo / "file.json"), "content": content}
    result = hooks.pre_tool_call(tool_name="write_file", args=args)
    assert result["args"]["content"] == content
    assert result["args"]["path"] == str(worktree / "file.json")


def test_approval_observes_choice_and_never_applies(cli_session, hooks, monkeypatch):
    repo, worktree = cli_session
    apply_api = importlib.import_module("forgebox.core.apply")
    def forbidden(*args, **kwargs):
        pytest.fail("an approval observer must never apply a session")
    monkeypatch.setattr(apply_api, "apply", forbidden)
    (worktree / "tracked.txt").write_text("experimental\n")
    assert hooks.post_approval_response(choice="once", command="echo hi",
        session_key="test", approved=True, pending="apply") is None
    assert (repo / "tracked.txt").read_text() == "dirty tracked\n"
    assert _journal_entries(repo)[-1]["choice"] == "once"


def test_worktree_symlink_escape_is_blocked(cli_session, hooks):
    repo, worktree = cli_session
    outside = repo.parent / "outside"
    outside.mkdir()
    (worktree / "escape").symlink_to(outside, target_is_directory=True)
    result = hooks.pre_tool_call(tool_name="write_file", args={
        "path": str(repo / "escape/file.txt"), "content": "no"})
    assert result["action"] == "block"


def test_ambient_subdirectory_is_preserved(cli_session, hooks, monkeypatch):
    repo, worktree = cli_session
    monkeypatch.chdir(repo / "nested")
    result = hooks.pre_tool_call(tool_name="terminal", args={"command": "pwd"})
    assert result["args"]["workdir"] == str(worktree / "nested")
    result = hooks.pre_tool_call(tool_name="read_file", args={"path": "new.txt"})
    assert result["args"]["path"] == str(worktree / "nested/new.txt")


def test_external_command_cwd_is_blocked(cli_session, hooks):
    repo, _ = cli_session
    result = hooks.pre_tool_call(tool_name="terminal", args={
        "command": "pwd", "workdir": str(repo.parent)})
    assert result["action"] == "block"


def test_ambiguous_source_is_not_treated_as_a_path(cli_session, hooks):
    _, worktree = cli_session
    source = "print('/tmp/literal')"
    result = hooks.pre_tool_call(tool_name="python", args={"source": source})
    assert result is None


def test_session_discovered_from_terminal_workdir(cli_session, hooks, monkeypatch):
    repo, worktree = cli_session
    monkeypatch.chdir(repo.parent)
    result = hooks.pre_tool_call(tool_name="terminal", args={
        "workdir": str(repo / "nested"), "command": "pwd"})
    assert result["action"] == "modify"
    assert result["args"]["workdir"] == str(worktree / "nested")


def test_search_enum_and_pattern_remain_literal(cli_session, hooks):
    _, worktree = cli_session
    result = hooks.pre_tool_call(tool_name="search_files", args={
        "target": "files", "pattern": "src/*.py"})
    assert result["args"] == {"target": "files", "pattern": "src/*.py", "path": str(worktree)}


def test_patch_replace_preserves_content(cli_session, hooks):
    _, worktree = cli_session
    result = hooks.pre_tool_call(tool_name="patch", args={
        "path": "tracked.txt", "old_string": "a/b", "new_string": "/tmp/literal"})
    assert result["args"] == {"path": str(worktree / "tracked.txt"),
                              "old_string": "a/b", "new_string": "/tmp/literal"}


def test_unsupported_multi_file_patch_is_blocked(cli_session, hooks):
    result = hooks.pre_tool_call(tool_name="patch", args={"mode": "patch",
        "patch": "*** Begin Patch\n*** Add File: x\n+hi\n*** End Patch"})
    assert result["action"] == "block"


def test_unknown_tool_path_fields_are_not_guessed(cli_session, hooks):
    args = {"path": "a/b", "target": "content", "source": "print('hi')"}
    assert hooks.pre_tool_call(tool_name="custom_plugin_tool", args=args) is None
