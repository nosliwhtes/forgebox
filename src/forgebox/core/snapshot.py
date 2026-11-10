"""Dirty-tree snapshots without ever borrowing the user's Git index."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import os
import json
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Iterator
import uuid

from .git_env import git_environment


class SnapshotError(Exception):
    """A Git operation failed or a snapshot cannot be handled safely."""


@dataclass
class Snapshot:
    id: str
    repo_root: Path
    commit_sha: str
    base_head_sha: str
    created_at: str
    worktree_path: Path | None = None


def _git(root: Path, *args: str, input: bytes | None = None,
         env: dict[str, str] | None = None) -> bytes:
    """Run Git with allowlisted host settings and explicit internal overrides."""
    environment = git_environment()
    if env:
        environment.update(env)
    literal = [] if environment.get('GIT_LITERAL_PATHSPECS') == '0' else ['--literal-pathspecs']
    command = ['git', *literal, '-c', 'core.sparseCheckout=false',
               '-c', 'core.sparseCheckoutCone=false', '-C', str(root), *args]
    try:
        result = subprocess.run(command, input=input, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=environment, check=False)
    except OSError as exc:
        raise SnapshotError(f'Cannot execute Git: {exc}') from exc
    if result.returncode:
        detail = result.stderr.decode('utf-8', 'replace').strip()
        raise SnapshotError(f'git {args[0]} failed ({result.returncode}): {detail}')
    return result.stdout


def _root(path: Path) -> Path:
    path = Path(path).resolve()
    if _git(path, 'rev-parse', '--is-inside-work-tree').strip() != b'true':
        raise SnapshotError(f'Not a Git working tree: {path}')
    return Path(os.fsdecode(_git(path, 'rev-parse', '--show-toplevel').removesuffix(b'\n'))).resolve()


def _validate(snap: Snapshot, repo_root: Path | None = None) -> Path:
    if not re.fullmatch(r'[0-9a-f]{7,64}', snap.id):
        raise SnapshotError('Invalid snapshot id')
    if not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', snap.commit_sha):
        raise SnapshotError('Invalid snapshot commit')
    root = _root(snap.repo_root)
    owner = _snapshot_owner(root, snap.commit_sha)
    if owner is not None and owner != root:
        raise SnapshotError('Snapshot belongs to a different checkout')
    if repo_root is not None and _root(repo_root) != root:
        raise SnapshotError('Snapshot belongs to a different repository')
    return root


@contextmanager
def _index(root: Path, base: str) -> Iterator[dict[str, str]]:
    with tempfile.TemporaryDirectory(prefix='forgebox-index-') as temporary:
        env = {'GIT_INDEX_FILE': str(Path(temporary) / 'index')}
        _git(root, 'read-tree', base, env=env)
        yield env


def _tree_entries(root: Path, tree: str) -> dict[str, tuple[str, str]]:
    entries = {}
    for record in _git(root, 'ls-tree', '-r', '-z', tree).split(b'\0'):
        if record:
            metadata, path = record.split(b'\t', 1)
            mode, _kind, sha = metadata.split()
            entries[os.fsdecode(path)] = (mode.decode(), sha.decode())
    return entries


def _sparse_entries(root: Path) -> dict[str, tuple[str, str]]:
    """Return omitted stage-zero entries from the actual index, not HEAD.

    A sparse path can have a staged change. Its absence on disk must preserve
    that staged blob in the snapshot without changing the real index flags.
    """
    entries = {}
    for record in _git(root, 'ls-files', '-t', '--stage', '-z').split(b'\0'):
        if not record.startswith(b'S '):
            continue
        metadata, raw_path = record[2:].split(b'\t', 1)
        mode, sha, stage = metadata.split()
        if stage == b'0':
            entries[os.fsdecode(raw_path)] = (mode.decode(), sha.decode())
    return entries


def _snapshot_owner(root: Path, commit: str) -> Path | None:
    """Read checkout ownership recorded in new snapshot commits."""
    message = _git(root, 'show', '-s', '--format=%B', commit).decode('utf-8')
    for line in message.splitlines():
        if line.startswith('Forgebox-Owner: '):
            try:
                owner = json.loads(line.removeprefix('Forgebox-Owner: '))
            except ValueError as exc:
                raise SnapshotError('Invalid snapshot owner metadata') from exc
            if not isinstance(owner, str) or not Path(owner).is_absolute():
                raise SnapshotError('Invalid snapshot owner metadata')
            return Path(owner).resolve()
    return None


def _session_root(path: Path) -> Path:
    """Anchor to this checkout, or the owner of a managed experiment."""
    top = _root(path)
    if top.parent.name == 'wt' and top.parent.parent.name == '.forgebox':
        owner = top.parent.parent.parent
        if _root(owner) == owner and re.fullmatch(r'[0-9a-f]{7,64}', top.name):
            reference = f'refs/forgebox/snapshots/{top.name}'
            refs = _git(owner, 'for-each-ref', '--format=%(refname)', reference).splitlines()
            if reference.encode() in refs:
                recorded = _snapshot_owner(owner, reference)
                if recorded is None or recorded == owner:
                    return owner
    return top


def _capture_tree(root: Path, base: str) -> str:
    """Seed HEAD, clear index flags by construction, then stage actual files.

    .forgebox is reserved runtime storage and must never recurse into snapshots.
    Ignored, untracked files are intentionally not copied (no secret allowlist).
    Explicitly tracked ignored additions are included via the real index listing.
    """
    with _index(root, base) as env:
        # Pathspec magic is needed only here, so override literal-pathspec mode.
        _git(root, 'add', '-A', '--', '.', ':(top,exclude).forgebox',
             env={**env, 'GIT_LITERAL_PATHSPECS': '0'})
        tracked = _git(root, 'ls-files', '-z').split(b'\0')
        force_paths = []
        for raw in tracked:
            if not raw:
                continue
            path = os.fsdecode(raw)
            if path == '.forgebox' or path.startswith('.forgebox/'):
                continue
            actual = root / path
            if actual.is_symlink() or actual.is_file():
                force_paths.append(raw)
        if force_paths:
            _git(root, 'add', '-f', '--pathspec-from-file=-', '--pathspec-file-nul',
                 input=b'\0'.join(force_paths) + b'\0', env=env)
        # Sparse absent files are not deletions; preserve their recorded blobs.
        for path, (mode, sha) in _sparse_entries(root).items():
            if path == '.forgebox' or path.startswith('.forgebox/'):
                continue
            if not (root / path).exists() and not (root / path).is_symlink():
                _git(root, 'update-index', '--add', '--cacheinfo',
                     mode, sha, path, env=env)
        return _git(root, 'write-tree', env=env).strip().decode()


def _worktree_path(snap: Snapshot) -> Path:
    root = _validate(snap)
    expected = root / '.forgebox' / 'wt' / snap.id
    for path in (root / '.forgebox', root / '.forgebox' / 'wt', expected):
        if path.is_symlink():
            raise SnapshotError(f'Worktree storage cannot be a symlink: {path}')
    if snap.worktree_path is not None and Path(snap.worktree_path).absolute() != expected:
        raise SnapshotError('Unexpected snapshot worktree path')
    return expected


def _check_worktree(snap: Snapshot, worktree: Path) -> Path:
    expected = _worktree_path(snap)
    if Path(worktree).resolve() != expected or _root(expected) != expected:
        raise SnapshotError('Worktree does not belong to this snapshot')
    return expected


def create_snapshot(repo_root: Path) -> Snapshot:
    root = _root(repo_root)
    head = _git(root, 'rev-parse', '--verify', 'HEAD^{commit}').strip().decode()
    created_at = datetime.now(timezone.utc).isoformat()
    tree = _capture_tree(root, head)
    # A gitlink records a commit, not the submodule's dirty/untracked contents.
    # Refuse rather than falsely claim an isolated faithful snapshot.
    if any(mode == '160000' for mode, _sha in _tree_entries(root, tree).values()):
        raise SnapshotError('Submodules are not supported by isolated snapshots')
    identity = {'GIT_AUTHOR_NAME': 'Forgebox', 'GIT_AUTHOR_EMAIL': 'forgebox@localhost',
                'GIT_COMMITTER_NAME': 'Forgebox', 'GIT_COMMITTER_EMAIL': 'forgebox@localhost'}
    message = (f'Forgebox snapshot {created_at} {uuid.uuid4().hex}\n\n'
               f'Forgebox-Owner: {json.dumps(str(root))}\n').encode()
    commit = _git(root, 'commit-tree', tree, '-p', head, input=message,
                  env=identity).strip().decode()
    identifier = commit[:12]
    _git(root, 'update-ref', f'refs/forgebox/snapshots/{identifier}', commit,
         '0' * len(commit))
    return Snapshot(identifier, root, commit, head, created_at, None)


def create_worktree(snap: Snapshot) -> Path:
    path = _worktree_path(snap)
    if path.exists():
        raise SnapshotError(f'Worktree already exists: {path}')
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(snap.repo_root, 'worktree', 'add', '--detach', str(path), snap.commit_sha)
    snap.worktree_path = path
    return path


