"""Hermetic Git fixtures; production modules are imported only when exercised."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import subprocess

import pytest


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    """Ignore host Git identity/config and keep every test in its own directory."""
    for key in tuple(os.environ):
        if key.startswith("GIT_") or key.startswith("FORGEBOX_"):
            monkeypatch.delenv(key, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def git():
    """Run a real Git command, retaining stdout/stderr in assertion failures."""
    def run(repo: Path, *args: str, check: bool = True):
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=check,
            timeout=30,
        )

    return run


@pytest.fixture
def git_repo(tmp_path, git):
    """One commit, staged/unstaged edits, a deletion, and untracked paths."""
    repo = tmp_path / "repo with spaces"
    repo.mkdir()
    git(repo, "init", "--quiet")
    git(repo, "config", "user.name", "Forgebox Test")
    git(repo, "config", "user.email", "forgebox@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "config", "core.autocrlf", "false")
    contents = {
        ".gitignore": ".forgebox/\n.ignored/\n.env\n",
        "tracked.txt": "committed tracked\n",
        "staged.txt": "committed staged\n",
        "delete-me.txt": "delete this\n",
        "stable.txt": "unchanged baseline\n",
    }
    for name, content in contents.items():
        (repo / name).write_text(content, encoding="utf-8")
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "-m", "fixture baseline")
    (repo / "tracked.txt").write_text("dirty tracked\n", encoding="utf-8")
    (repo / "staged.txt").write_text("staged version\n", encoding="utf-8")
    git(repo, "add", "staged.txt")
    (repo / "staged.txt").write_text("working version after staging\n", encoding="utf-8")
    (repo / "delete-me.txt").unlink()
    (repo / "untracked note.txt").write_text("untracked baseline\n", encoding="utf-8")
    (repo / "nested").mkdir()
    (repo / "nested" / "new.txt").write_text("nested baseline\n", encoding="utf-8")
    return repo


@pytest.fixture
def snapshot_api():
    return importlib.import_module("forgebox.core.snapshot")


@pytest.fixture
def diff_api():
    return importlib.import_module("forgebox.core.diff")


@pytest.fixture
def apply_api():
    return importlib.import_module("forgebox.core.apply")


@pytest.fixture
def session(git_repo, git, snapshot_api):
    """A real snapshot plus detached worktree, with implementation-free cleanup."""
    snap = snapshot_api.create_snapshot(git_repo)
    worktree = Path(snapshot_api.create_worktree(snap))
    try:
        yield snap, worktree
    finally:
        git(git_repo, "worktree", "remove", "--force", str(worktree), check=False)
        git(git_repo, "worktree", "prune", check=False)
        git(git_repo, "update-ref", "-d", f"refs/forgebox/snapshots/{snap.id}", check=False)


@pytest.fixture
def cli(capsys):
    """Call the public CLI entry point and capture each invocation separately."""
    main = importlib.import_module("forgebox.cli").main

    def run(*argv):
        capsys.readouterr()
        try:
            code = main(list(argv))
        except SystemExit as exc:
            code = exc.code
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return run


@pytest.fixture
def cli_session(git_repo, git, cli, monkeypatch):
    """Activate through the public CLI; do not guess private session-file shapes."""
    monkeypatch.chdir(git_repo)
    code, out, err = cli("begin", "--repo", str(git_repo))
    assert code == 0, (out, err)
    assert out.strip(), "begin must print a session id"
    listed = git(git_repo, "worktree", "list", "--porcelain").stdout
    worktrees = [
        Path(line.removeprefix("worktree "))
        for line in listed.splitlines()
        if line.startswith("worktree ")
    ]
    sandboxes = [path for path in worktrees if path.resolve() != git_repo.resolve()]
    assert len(sandboxes) == 1, listed
    worktree = sandboxes[0]
    try:
        yield git_repo, worktree
    finally:
        cli("discard", "--repo", str(git_repo))
        git(git_repo, "worktree", "remove", "--force", str(worktree), check=False)
        git(git_repo, "worktree", "prune", check=False)
