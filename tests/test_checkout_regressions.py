"""Checkout ownership, exact paths, and real sparse-checkout regressions."""
from dataclasses import replace
from pathlib import Path
import json

import pytest

from forgebox import plugin_api


def test_subdirectory_cli_and_api_use_checkout_root(git_repo, cli, monkeypatch):
    subdir = git_repo / "nested"
    monkeypatch.chdir(subdir)
    code, out, err = cli("begin")
    assert code == 0, (out, err)
    try:
        assert (git_repo / ".forgebox/active.json").exists()
        assert not (subdir / ".forgebox").exists()
        listing = plugin_api.list_sessions({"repo": str(subdir)})
        assert listing["ok"] and listing["repo"] == str(git_repo)
        assert listing["sessions"][0]["id"] == out.strip()
        assert cli("status")[0] == 0
    finally:
        assert cli("discard")[0] == 0


def test_linked_checkout_sessions_are_scoped_and_mutate_their_owner(
    git_repo, git, cli, tmp_path
):
    linked = tmp_path / "linked checkout"
    git(git_repo, "worktree", "add", "--detach", str(linked))
    main_id = linked_id = None
    try:
        code, out, err = cli("begin", "--repo", str(git_repo))
        assert code == 0, (out, err)
        main_id = out.strip()
        code, out, err = cli("begin", "--repo", str(linked))
        assert code == 0, (out, err)
        linked_id = out.strip()
        main_sessions = plugin_api.list_sessions({"repo": str(git_repo)})["sessions"]
        linked_sessions = plugin_api.list_sessions({"repo": str(linked)})["sessions"]
        assert [s["id"] for s in main_sessions] == [main_id]
        assert [s["id"] for s in linked_sessions] == [linked_id]
        worktree = Path(linked_sessions[0]["worktree_path"])
        (worktree / "tracked.txt").write_text("linked change\n")
        denied = plugin_api.apply({"repo": str(git_repo), "session_id": linked_id})
        assert denied["ok"] is False
        result = plugin_api.apply({"repo": str(linked), "session_id": linked_id})
        assert result["ok"], result
        assert (linked / "tracked.txt").read_text() == "linked change\n"
        assert (git_repo / "tracked.txt").read_text() == "dirty tracked\n"
        assert plugin_api.list_sessions({"repo": str(worktree)})["repo"] == str(linked)
        assert plugin_api.discard({"repo": str(git_repo), "session_id": main_id})["ok"]
        main_id = None
        assert [s["id"] for s in plugin_api.list_sessions({"repo": str(linked)})["sessions"]] == [linked_id]
    finally:
        if main_id:
            cli("discard", "--repo", str(git_repo))
        if linked_id:
            cli("discard", "--repo", str(linked))
        git(git_repo, "worktree", "remove", "--force", str(linked), check=False)


def test_snapshot_only_owner_and_cross_checkout_validation(git_repo, git, tmp_path, snapshot_api):
    linked = tmp_path / "linked"
    git(git_repo, "worktree", "add", "--detach", str(linked))
    snap = snapshot_api.create_snapshot(linked)
    try:
        assert plugin_api.list_sessions({"repo": str(git_repo)})["sessions"] == []
        listing = plugin_api.list_sessions({"repo": str(linked)})
        assert listing["sessions"][0]["id"] == snap.id
        assert listing["sessions"][0]["status"] == "snapshot"
        with pytest.raises(snapshot_api.SnapshotError, match="different checkout"):
            snapshot_api._validate(replace(snap, repo_root=git_repo))
    finally:
        git(git_repo, "update-ref", "-d", f"refs/forgebox/snapshots/{snap.id}")
        git(git_repo, "worktree", "remove", "--force", str(linked))


@pytest.mark.parametrize("name", [" repo with trailing space ", "repo with newline\n"])
def test_api_and_cli_preserve_exact_repository_path(tmp_path, git, cli, name):
    repo = tmp_path / name
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "a").write_text("one\n")
    git(repo, "add", "a")
    git(repo, "commit", "-m", "initial")
    code, out, err = cli("begin", "--repo", str(repo))
    assert code == 0, (out, err)
    try:
        listing = plugin_api.list_sessions({"repo": str(repo)})
        assert listing["ok"], listing
        assert listing["repo"] == str(repo)
        assert listing["sessions"][0]["id"] == out.strip()
    finally:
        assert cli("discard", "--repo", str(repo))[0] == 0


def test_sparse_absent_path_preserves_staged_blob(git_repo, git, snapshot_api):
    path = git_repo / "stable.txt"
    path.write_text("staged but sparse\n")
    git(git_repo, "add", "stable.txt")
    git(git_repo, "update-index", "--skip-worktree", "stable.txt")
    path.unlink()
    before_index = (git_repo / ".git/index").read_bytes()
    snap = snapshot_api.create_snapshot(git_repo)
    assert git(git_repo, "show", f"{snap.commit_sha}:stable.txt").stdout == "staged but sparse\n"
    assert (git_repo / ".git/index").read_bytes() == before_index
    assert not path.exists()


def test_real_cone_sparse_checkout_has_complete_clean_worktree(tmp_path, git, snapshot_api, diff_api):
    repo = tmp_path / "sparse"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    for name in ("included/a", "omitted/b"):
        path = repo / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("original\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    git(repo, "sparse-checkout", "init", "--cone", "--sparse-index")
    git(repo, "sparse-checkout", "set", "included")
    before_index = (repo / ".git/index").read_bytes()
    snap = snapshot_api.create_snapshot(repo)
    worktree = snapshot_api.create_worktree(snap)
    assert (worktree / "omitted/b").read_text() == "original\n"
    assert diff_api.build_patch(snap, worktree) == ""
    assert not (repo / "omitted/b").exists()
    assert (repo / ".git/index").read_bytes() == before_index
