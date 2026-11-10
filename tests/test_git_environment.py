"""Git and repository hooks must not inherit the host's provider secrets."""

import importlib
import os

import pytest


@pytest.mark.parametrize("module_name", [
    "forgebox.core.snapshot", "forgebox.plugin_api",
])
def test_git_children_receive_only_allowlisted_host_environment(
    git_repo, monkeypatch, tmp_path, module_name,
):
    module = importlib.import_module(module_name)
    # Probe a real Git child rather than only inspecting subprocess arguments.
    monkeypatch.setenv("FORGEBOX_TEST_HOST_SECRET", "must-not-reach-git")
    monkeypatch.setenv("OPENAI_API_KEY", "sentinel-not-a-real-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "another-test-sentinel")
    monkeypatch.setenv("PYTHONPATH", "/host-only-import-path")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "wrong-git-directory"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "wrong-index"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "alias.forgebox-env")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "!exit 99")
    monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "1")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("LANG", "C")
    monkeypatch.setenv("LC_CTYPE", "C")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test Author")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "author@example.invalid")
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-11-10T00:00:00Z")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test Committer")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "committer@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-11-10T00:00:01Z")

    # Limit probe output so a regression cannot log unrelated real host secrets.
    names = ("FORGEBOX_TEST_HOST_SECRET", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
             "PYTHONPATH", "GIT_DIR", "GIT_INDEX_FILE", "GIT_CONFIG_COUNT",
             "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "GIT_OPTIONAL_LOCKS",
             "PATH", "HOME", "XDG_CONFIG_HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR",
             "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
             "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE")
    probe = "!env | grep -E '^(" + "|".join(names) + ")='"
    output = module._git(git_repo, "-c", "alias.forgebox-env=" + probe, "forgebox-env")
    if isinstance(output, bytes):
        output = output.decode()
    child = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    for key in ("FORGEBOX_TEST_HOST_SECRET", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                "PYTHONPATH", "GIT_DIR", "GIT_INDEX_FILE", "GIT_CONFIG_COUNT",
                "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
        assert key not in child
    # Git prefixes its own executable directory when invoking shell aliases.
    assert child["PATH"].endswith(os.environ["PATH"])
    for key in ("HOME", "XDG_CONFIG_HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR",
                "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE"):
        assert child[key] == os.environ[key]
    assert child["GIT_OPTIONAL_LOCKS"] == "0"


def test_apply_commit_hook_does_not_receive_host_secrets(
    session, git_repo, apply_api, monkeypatch,
):
    snap, worktree = session
    monkeypatch.setenv("FORGEBOX_TEST_HOST_SECRET", "must-not-reach-hook")
    monkeypatch.setenv("OPENAI_API_KEY", "sentinel-not-a-real-key")
    hook = git_repo / ".git/hooks/pre-commit"
    hook.write_text(
        '#!/bin/sh\nprintf "%s\\n" "${FORGEBOX_TEST_HOST_SECRET-unset}" '
        '"${OPENAI_API_KEY-unset}" "$GIT_OPTIONAL_LOCKS" '
        '> .git/forgebox-hook-environment\n', encoding="utf-8",
    )
    hook.chmod(0o755)
    (worktree / "tracked.txt").write_text("reviewed experiment\n", encoding="utf-8")

    result = apply_api.apply(git_repo, snap, worktree, commit_message="Test apply hook")

    assert result["commit_sha"]
    environment = (git_repo / ".git/forgebox-hook-environment").read_text()
    assert environment.splitlines() == ["unset", "unset", "0"]


def test_core_git_preserves_explicit_temporary_index(
    git_repo, snapshot_api, tmp_path, monkeypatch,
):
    original = (git_repo / ".git/index").read_bytes()
    temporary_index = tmp_path / "private-index"
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "ambient-index"))
    snapshot_api._git(git_repo, "read-tree", "HEAD", env={"GIT_INDEX_FILE": str(temporary_index)})
    assert temporary_index.is_file()
    assert not (tmp_path / "ambient-index").exists()
    assert (git_repo / ".git/index").read_bytes() == original


@pytest.mark.parametrize("ignore_source", ["xdg-ignore", "xdg-config"])
def test_snapshot_preserves_xdg_global_ignore_rules(
    git_repo, git, snapshot_api, apply_api, tmp_path, monkeypatch, ignore_source,
):
    # An ignored untracked file must stay out of both Git objects and the
    # experiment, even when the user keeps Git settings outside ~/.config.
    config_home = tmp_path / "custom config home"
    git_config = config_home / "git"
    git_config.mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    if ignore_source == "xdg-ignore":
        (git_config / "ignore").write_text("secret.key\n", encoding="utf-8")
    else:
        excludes = tmp_path / "custom-excludes"
        excludes.write_text("secret.key\n", encoding="utf-8")
        git(git_repo, "config", "--file", str(git_config / "config"),
            "core.excludesFile", str(excludes))
    (git_repo / "secret.key").write_text("test sentinel, not a credential\n", encoding="utf-8")
    (git_repo / "visible.txt").write_text("include this untracked file\n", encoding="utf-8")
    assert git(git_repo, "check-ignore", "secret.key").stdout.strip() == "secret.key"

    snap = snapshot_api.create_snapshot(git_repo)
    try:
        files = git(git_repo, "ls-tree", "--name-only", "-r", snap.commit_sha).stdout.splitlines()
        assert "secret.key" not in files
        assert "visible.txt" in files
        worktree = snapshot_api.create_worktree(snap)
        assert not (worktree / "secret.key").exists()
        assert (worktree / "visible.txt").read_text() == "include this untracked file\n"
    finally:
        apply_api.discard(git_repo, snap)
