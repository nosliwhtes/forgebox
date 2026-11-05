"""Black-box lifecycle checks against ARCH.md, using real Git repositories."""

from datetime import datetime
import re
import subprocess

import pytest


def test_snapshot_captures_working_tree_not_only_head_or_index(git_repo, git, snapshot_api):
    head = git(git_repo, "rev-parse", "HEAD").stdout.strip()
    snap = snapshot_api.create_snapshot(git_repo)
    expected = {
        "tracked.txt": "dirty tracked\n",
        "staged.txt": "working version after staging\n",
        "stable.txt": "unchanged baseline\n",
        "untracked note.txt": "untracked baseline\n",
        "nested/new.txt": "nested baseline\n",
    }
    for path, content in expected.items():
        assert git(git_repo, "show", f"{snap.commit_sha}:{path}").stdout == content
    names = git(git_repo, "ls-tree", "-r", "--name-only", snap.commit_sha).stdout.splitlines()
    assert "delete-me.txt" not in names
    assert snap.base_head_sha == head
    assert snap.repo_root.resolve() == git_repo.resolve()
    assert re.fullmatch(r"[0-9a-f]+", snap.id)
    assert snap.commit_sha.startswith(snap.id)
    assert datetime.fromisoformat(snap.created_at.replace("Z", "+00:00"))
    ref = f"refs/forgebox/snapshots/{snap.id}"
    assert git(git_repo, "rev-parse", ref).stdout.strip() == snap.commit_sha
    assert git(git_repo, "rev-parse", "HEAD").stdout.strip() == head


def test_snapshot_does_not_change_real_index_or_dirty_files(git_repo, git, snapshot_api):
    index = git_repo / ".git" / "index"
    before_index = index.read_bytes()
    before_staged = git(git_repo, "diff", "--cached", "--binary").stdout
    before_files = {
        str(path.relative_to(git_repo)): path.read_bytes()
        for path in git_repo.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(git_repo).parts
    }
    snapshot_api.create_snapshot(git_repo)
    assert index.read_bytes() == before_index, "snapshot must use a temporary Git index"
    assert git(git_repo, "diff", "--cached", "--binary").stdout == before_staged
    for path, content in before_files.items():
        assert (git_repo / path).read_bytes() == content
    assert not (git_repo / "delete-me.txt").exists()


def test_snapshot_rejects_non_repository(tmp_path, snapshot_api):
    with pytest.raises(snapshot_api.SnapshotError):
        snapshot_api.create_snapshot(tmp_path)


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_snapshot_captures_actual_contents_despite_real_index_flags(
    git_repo, git, snapshot_api, flag
):
    git(git_repo, "update-index", flag, "stable.txt")
    (git_repo / "stable.txt").write_text("hidden dirty working contents\n")
    before_index = (git_repo / ".git" / "index").read_bytes()
    snap = snapshot_api.create_snapshot(git_repo)
    assert git(git_repo, "show", f"{snap.commit_sha}:stable.txt").stdout == "hidden dirty working contents\n"
    assert (git_repo / ".git" / "index").read_bytes() == before_index


def test_snapshot_captures_intent_to_add_without_rewriting_index(git_repo, git, snapshot_api):
    (git_repo / "intent-to-add.txt").write_text("intent-to-add contents\n")
    git(git_repo, "add", "--intent-to-add", "intent-to-add.txt")
    before_index = (git_repo / ".git" / "index").read_bytes()
    snap = snapshot_api.create_snapshot(git_repo)
    assert git(git_repo, "show", f"{snap.commit_sha}:intent-to-add.txt").stdout == "intent-to-add contents\n"
    assert (git_repo / ".git" / "index").read_bytes() == before_index


def test_snapshot_preserves_sparse_absent_tracked_blob(git_repo, git, snapshot_api):
    # skip-worktree marks omitted sparse paths; their absence is not a deletion.
    git(git_repo, "update-index", "--skip-worktree", "stable.txt")
    (git_repo / "stable.txt").unlink()
    before_index = (git_repo / ".git" / "index").read_bytes()
    snap = snapshot_api.create_snapshot(git_repo)
    assert git(git_repo, "show", f"{snap.commit_sha}:stable.txt").stdout == "unchanged baseline\n"
    assert not (git_repo / "stable.txt").exists()
    assert (git_repo / ".git" / "index").read_bytes() == before_index


def test_worktree_is_detached_and_contains_dirty_snapshot(session, git_repo, git):
    snap, worktree = session
    assert worktree.resolve() == (git_repo / ".forgebox" / "wt" / snap.id).resolve()
    assert git(worktree, "rev-parse", "HEAD").stdout.strip() == snap.commit_sha
    assert git(worktree, "symbolic-ref", "-q", "HEAD", check=False).returncode != 0
    assert (worktree / "tracked.txt").read_text() == "dirty tracked\n"
    assert (worktree / "staged.txt").read_text() == "working version after staging\n"
    assert (worktree / "untracked note.txt").read_text() == "untracked baseline\n"
    assert not (worktree / "delete-me.txt").exists()


def test_build_patch_is_empty_for_unchanged_snapshot(session, diff_api):
    snap, worktree = session
    assert diff_api.build_patch(snap, worktree) == ""
    assert diff_api.changed_files(snap, worktree) == []


def test_build_patch_includes_untracked_new_files(session, diff_api, git_repo, git):
    snap, worktree = session
    (worktree / "brand new.txt").write_text("new sandbox content\n")
    patch = diff_api.build_patch(snap, worktree)
    assert "brand new.txt" in patch
    assert "new file mode" in patch
    assert "+new sandbox content" in patch
    # A patch that only mentions the path is insufficient: real Git must accept it.
    checked = subprocess.run(
        ["git", "-C", str(git_repo), "apply", "--check", "-"],
        input=patch, text=True, capture_output=True, timeout=30,
    )
    assert checked.returncode == 0, checked.stderr
    assert not (git_repo / "brand new.txt").exists()


def test_changed_files_reports_add_modify_delete_and_line_counts(session, diff_api):
    snap, worktree = session
    (worktree / "tracked.txt").write_text("replacement\nextra line\n")
    (worktree / "added.txt").write_text("added line\n")
    (worktree / "stable.txt").unlink()
    files = {entry["path"]: entry for entry in diff_api.changed_files(snap, worktree)}
    assert set(files) == {"tracked.txt", "added.txt", "stable.txt"}
    for path, status, added, deleted in [
        ("tracked.txt", "M", 2, 1),
        ("added.txt", "A", 1, 0),
        ("stable.txt", "D", 0, 1),
    ]:
        assert files[path]["status"] == status
        assert files[path]["lines_added"] == added
        assert files[path]["lines_deleted"] == deleted


def test_patch_includes_committed_and_uncommitted_worktree_changes(session, diff_api, git):
    snap, worktree = session
    (worktree / "tracked.txt").write_text("committed sandbox edit\n")
    git(worktree, "add", "tracked.txt")
    git(worktree, "commit", "--quiet", "-m", "sandbox-only commit")
    (worktree / "stable.txt").write_text("uncommitted sandbox edit\n")
    patch = diff_api.build_patch(snap, worktree)
    assert "+committed sandbox edit" in patch
    assert "+uncommitted sandbox edit" in patch


def test_preflight_is_noop_without_changes(session, git_repo, apply_api):
    snap, worktree = session
    report = apply_api.preflight(git_repo, snap, worktree)
    assert report.status == "noop"
    assert report.conflicts == []
    assert report.patch == ""
    assert report.files == []


def test_preflight_compares_against_dirty_snapshot_not_head(session, git_repo, apply_api):
    snap, worktree = session
    (worktree / "tracked.txt").write_text("sandbox edit\n")
    report = apply_api.preflight(git_repo, snap, worktree)
    assert report.status == "clean"
    assert report.conflicts == []
    assert "+sandbox edit" in report.patch
    assert {entry["path"] for entry in report.files} == {"tracked.txt"}
    assert (git_repo / "tracked.txt").read_text() == "dirty tracked\n"


@pytest.mark.parametrize("change", ["modify", "delete", "new-file-collision"])
def test_preflight_detects_real_repo_changes_to_touched_paths(
    session, git_repo, apply_api, change
):
    snap, worktree = session
    path = "collision.txt" if change == "new-file-collision" else "tracked.txt"
    (worktree / path).write_text("sandbox result\n")
    if change == "delete":
        (git_repo / path).unlink()
    else:
        (git_repo / path).write_text("concurrent user edit\n")
    report = apply_api.preflight(git_repo, snap, worktree)
    assert report.status == "conflict"
    assert path in report.conflicts
    if change != "delete":
        assert (git_repo / path).read_text() == "concurrent user edit\n"


def test_preflight_ignores_concurrent_edits_to_untouched_files(session, git_repo, apply_api):
    snap, worktree = session
    (worktree / "tracked.txt").write_text("sandbox result\n")
    (git_repo / "stable.txt").write_text("unrelated user edit\n")
    assert apply_api.preflight(git_repo, snap, worktree).status == "clean"


def test_apply_lands_add_modify_delete_without_touching_user_index(
    session, git_repo, git, apply_api
):
    snap, worktree = session
    before_index = (git_repo / ".git" / "index").read_bytes()
    head = git(git_repo, "rev-parse", "HEAD").stdout.strip()
    (worktree / "tracked.txt").write_text("landed sandbox result\n")
    (worktree / "added.txt").write_text("new landed file\n")
    (worktree / "stable.txt").unlink()
    result = apply_api.apply(git_repo, snap, worktree)
    assert result["conflicts"] == []
    assert set(result["applied"]) == {"tracked.txt", "added.txt", "stable.txt"}
    assert result["commit_sha"] is None
    assert (git_repo / "tracked.txt").read_text() == "landed sandbox result\n"
    assert (git_repo / "added.txt").read_text() == "new landed file\n"
    assert not (git_repo / "stable.txt").exists()
    assert (git_repo / "staged.txt").read_text() == "working version after staging\n"
    assert (git_repo / "untracked note.txt").read_text() == "untracked baseline\n"
    assert (git_repo / ".git" / "index").read_bytes() == before_index
    assert git(git_repo, "rev-parse", "HEAD").stdout.strip() == head


def test_apply_conflict_refuses_entire_patch_without_partial_landing(
    session, git_repo, apply_api
):
    snap, worktree = session
    (worktree / "aaa-clean-add.txt").write_text("must not land\n")
    (worktree / "tracked.txt").write_text("sandbox overwrite\n")
    (worktree / "stable.txt").unlink()
    (git_repo / "tracked.txt").write_text("concurrent real edit\n")
    result = apply_api.apply(git_repo, snap, worktree)
    assert "tracked.txt" in result["conflicts"]
    assert result["applied"] == []
    assert result["commit_sha"] is None
    assert not (git_repo / "aaa-clean-add.txt").exists()
    assert (git_repo / "stable.txt").read_text() == "unchanged baseline\n"
    assert (git_repo / "tracked.txt").read_text() == "concurrent real edit\n"


def test_force_apply_overwrites_conflict_but_preserves_unrelated_user_edits(
    session, git_repo, apply_api
):
    snap, worktree = session
    (worktree / "tracked.txt").write_text("forced sandbox result\n")
    (worktree / "added.txt").write_text("forced new file\n")
    (git_repo / "tracked.txt").write_text("conflicting real edit\n")
    (git_repo / "stable.txt").write_text("unrelated real edit\n")
    result = apply_api.apply(git_repo, snap, worktree, force=True)
    assert set(result["applied"]) == {"tracked.txt", "added.txt"}
    assert (git_repo / "tracked.txt").read_text() == "forced sandbox result\n"
    assert (git_repo / "added.txt").read_text() == "forced new file\n"
    assert (git_repo / "stable.txt").read_text() == "unrelated real edit\n"


def test_apply_with_commit_message_creates_real_commit(session, git_repo, git, apply_api):
    snap, worktree = session
    (worktree / "tracked.txt").write_text("committed landed result\n")
    result = apply_api.apply(git_repo, snap, worktree, commit_message="Land sandbox result")
    assert result["conflicts"] == []
    assert result["commit_sha"] == git(git_repo, "rev-parse", "HEAD").stdout.strip()
    assert git(git_repo, "show", "-s", "--format=%s", "HEAD").stdout.strip() == "Land sandbox result"
    assert git(git_repo, "show", "HEAD:tracked.txt").stdout == "committed landed result\n"


def test_apply_noop_does_not_change_head(session, git_repo, git, apply_api):
    snap, worktree = session
    before = git(git_repo, "rev-parse", "HEAD").stdout
    result = apply_api.apply(git_repo, snap, worktree)
    assert result == {"applied": [], "conflicts": [], "commit_sha": None}
    assert git(git_repo, "rev-parse", "HEAD").stdout == before


def test_discard_removes_dirty_worktree_and_snapshot_ref(session, git_repo, git, apply_api):
    snap, worktree = session
    (worktree / "scratch.txt").write_text("disposable\n")
    apply_api.discard(git_repo, snap)
    assert not worktree.exists()
    ref = f"refs/forgebox/snapshots/{snap.id}"
    assert git(git_repo, "show-ref", "--verify", "--quiet", ref, check=False).returncode != 0
    assert str(worktree) not in git(git_repo, "worktree", "list", "--porcelain").stdout
    assert (git_repo / "tracked.txt").read_text() == "dirty tracked\n"
    assert (git_repo / "untracked note.txt").read_text() == "untracked baseline\n"
