"""Public main(argv) integration tests; no installed console script required."""

import json

import pytest


@pytest.mark.parametrize("argv", [
    ["--help"],
    ["begin", "--help"],
    ["status", "--help"],
    ["log", "--help"],
    ["diff", "--help"],
    ["preflight", "--help"],
    ["apply", "--help"],
    ["discard", "--help"],
])
def test_help_exits_zero(cli, argv):
    code, out, err = cli(*argv)
    assert code == 0, (out, err)
    assert "usage:" in out.lower()


def test_cli_begin_status_diff_preflight_apply_discard_happy_path(cli_session, cli, git):
    repo, worktree = cli_session
    session_id = worktree.name
    code, out, err = cli("status", "--repo", str(repo))
    assert code == 0, (out, err)
    assert session_id in out

    (worktree / "tracked.txt").write_text("cli sandbox result\n")
    (worktree / "cli-new.txt").write_text("cli new file\n")
    code, out, err = cli("diff", "--repo", str(repo))
    assert code == 0, (out, err)
    assert "tracked.txt" in out
    assert "+cli sandbox result" in out
    assert "+cli new file" in out
    assert (repo / "tracked.txt").read_text() == "dirty tracked\n"
    assert not (repo / "cli-new.txt").exists()

    code, out, err = cli("preflight", "--repo", str(repo))
    assert code == 0, (out, err)
    assert "clean" in out.lower()

    code, out, err = cli("apply", "--repo", str(repo), "--json")
    assert code == 0, (out, err)
    result = json.loads(out)
    assert result["conflicts"] == []
    assert set(result["applied"]) == {"tracked.txt", "cli-new.txt"}
    assert result["commit_sha"] is None
    assert (repo / "tracked.txt").read_text() == "cli sandbox result\n"
    assert (repo / "cli-new.txt").read_text() == "cli new file\n"

    code, out, err = cli("discard", "--repo", str(repo))
    assert code == 0, (out, err)
    assert not worktree.exists()
    ref = f"refs/forgebox/snapshots/{session_id}"
    assert git(repo, "show-ref", "--verify", "--quiet", ref, check=False).returncode != 0
    assert (repo / "tracked.txt").read_text() == "cli sandbox result\n"


def test_cli_begin_uses_explicit_repo_when_cwd_is_elsewhere(git_repo, cli, git):
    code, out, err = cli("begin", "--repo", str(git_repo))
    try:
        assert code == 0, (out, err)
        assert out.strip()
        refs = git(git_repo, "for-each-ref", "--format=%(refname)", "refs/forgebox/snapshots/").stdout
        assert len(refs.splitlines()) == 1
        assert (git_repo / "tracked.txt").read_text() == "dirty tracked\n"
    finally:
        cli("discard", "--repo", str(git_repo))


def test_cli_log_json_is_valid_json(cli_session, cli):
    repo, _ = cli_session
    code, out, err = cli("log", "--repo", str(repo), "--json")
    assert code == 0, (out, err)
    assert isinstance(json.loads(out), (list, dict))


def test_cli_preflight_and_apply_return_one_on_conflict(cli_session, cli):
    repo, worktree = cli_session
    (worktree / "tracked.txt").write_text("cli proposed overwrite\n")
    (worktree / "aaa-new.txt").write_text("must not partially land\n")
    (repo / "tracked.txt").write_text("real user change\n")
    code, out, err = cli("preflight", "--repo", str(repo))
    assert code == 1, (out, err)
    assert "conflict" in out.lower()
    assert "tracked.txt" in out
    code, out, err = cli("apply", "--repo", str(repo), "--json")
    assert code == 1, (out, err)
    result = json.loads(out)
    assert "tracked.txt" in result["conflicts"]
    assert result["applied"] == []
    assert (repo / "tracked.txt").read_text() == "real user change\n"
    assert not (repo / "aaa-new.txt").exists()


def test_cli_force_apply_overwrites_conflict(cli_session, cli):
    repo, worktree = cli_session
    (worktree / "tracked.txt").write_text("forced cli result\n")
    (repo / "tracked.txt").write_text("conflicting user change\n")
    code, out, err = cli("apply", "--repo", str(repo), "--force", "--json")
    assert code == 0, (out, err)
    result = json.loads(out)
    assert "tracked.txt" in result["applied"]
    assert (repo / "tracked.txt").read_text() == "forced cli result\n"


def test_cli_apply_commit_message_creates_commit(cli_session, cli, git):
    repo, worktree = cli_session
    (worktree / "tracked.txt").write_text("cli committed result\n")
    code, out, err = cli(
        "apply", "--repo", str(repo), "--commit-message", "CLI transaction", "--json"
    )
    assert code == 0, (out, err)
    result = json.loads(out)
    assert result["commit_sha"] == git(repo, "rev-parse", "HEAD").stdout.strip()
    assert git(repo, "show", "-s", "--format=%s", "HEAD").stdout.strip() == "CLI transaction"
    assert git(repo, "show", "HEAD:tracked.txt").stdout == "cli committed result\n"


def test_cli_discard_without_apply_keeps_original_dirty_tree(cli_session, cli):
    repo, worktree = cli_session
    (worktree / "tracked.txt").write_text("throw away\n")
    (worktree / "throw-away.txt").write_text("never land\n")
    code, out, err = cli("discard", "--repo", str(repo))
    assert code == 0, (out, err)
    assert not worktree.exists()
    assert (repo / "tracked.txt").read_text() == "dirty tracked\n"
    assert not (repo / "throw-away.txt").exists()
