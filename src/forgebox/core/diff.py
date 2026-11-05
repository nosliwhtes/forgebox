"""Binary-safe snapshot-to-working-tree diffs, including untracked files."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from .snapshot import Snapshot, _capture_tree, _check_worktree, _git, _tree_entries


@dataclass
class DiffCard:
    patch: str
    files: list[dict]
    summary: str


def _between(root: Path, before: str, after: str) -> str:
    return _git(root, 'diff', '--binary', '--full-index', '--no-ext-diff',
                '--no-textconv', '--no-renames', '--src-prefix=a/', '--dst-prefix=b/',
                before, after, '--').decode('utf-8', 'surrogateescape')


def _build_diff(snap: Snapshot, worktree: Path) -> tuple[str, list[dict], str]:
    root = _check_worktree(snap, worktree)
    tree = _capture_tree(root, snap.commit_sha)
    patch = _between(root, snap.commit_sha, tree)
    if not patch:
        return '', [], tree
    before = _tree_entries(root, snap.commit_sha)
    after = _tree_entries(root, tree)
    statistics = _git(root, 'diff', '--numstat', '-z', '--no-renames',
                      '--no-ext-diff', '--no-textconv', snap.commit_sha, tree, '--')
    files = []
    for record in statistics.split(b'\0'):
        if not record:
            continue
        added, deleted, raw_path = record.split(b'\t', 2)
        path = os.fsdecode(raw_path)
        status = 'A' if path not in before else 'D' if path not in after else 'M'
        files.append({'path': path, 'status': status,
                      'lines_added': int(added) if added != b'-' else 0,
                      'lines_deleted': int(deleted) if deleted != b'-' else 0})
    return patch, files, tree


def build_patch(snap: Snapshot, worktree: Path) -> str:
    """Return a Git binary/unified patch against the initial dirty snapshot.

    A temporary index provides untracked-file hunks without staging anything in
    the worktree's index. Comparing trees also includes commits made in it.
    """
    return _build_diff(snap, worktree)[0]


def changed_files(snap: Snapshot, worktree: Path) -> list[dict]:
    """Return A/M/D statuses and textual line counts (binary counts are zero)."""
    return _build_diff(snap, worktree)[1]
