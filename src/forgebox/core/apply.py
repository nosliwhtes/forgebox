"""Conflict-aware application and disposal of isolated worktree changes."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Iterator

from .diff import _between, _build_diff
from .snapshot import (Snapshot, SnapshotError, _git, _index, _tree_entries,
                       _validate, _worktree_path)


@dataclass
class ApplyReport:
    status: str
    conflicts: list[str]
    patch: str
    files: list[dict]


def _safe_path(root: Path, path: str) -> Path:
    relative = PurePosixPath(path)
    if (relative.is_absolute() or not relative.parts or '..' in relative.parts
            or relative.parts[0] in {'.git', '.forgebox'}):
        raise SnapshotError(f'Unsafe patch path: {path!r}')
    current = root
    for component in relative.parts[:-1]:
        current = current / component
        if current.is_symlink():
            raise SnapshotError(f'Patch traverses a symlink: {path!r}')
    return root / path


def _state(root: Path, path: str, *, write: bool = False,
           mode_hint: str | None = None) -> tuple[str, str] | None:
    """Hash actual content, not HEAD/the index; retain symlink and executable modes."""
    actual = _safe_path(root, path)
    try:
        info = actual.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    flags = ['-w'] if write else []
    if stat.S_ISLNK(info.st_mode):
        content = os.fsencode(os.readlink(actual))
        sha = _git(root, 'hash-object', *flags, '--stdin', input=content)
        return '120000', sha.strip().decode()
    if stat.S_ISREG(info.st_mode):
        # --path applies the same clean filters as snapshot's git add.
        sha = _git(root, 'hash-object', *flags, '--path=' + path, '--stdin',
                   input=actual.read_bytes())
        mode = '100755' if info.st_mode & stat.S_IXUSR else '100644'
        if mode_hint in {'100644', '100755'} and _git(
                root, 'config', '--bool', '--default=true', '--get', 'core.filemode'
        ).strip() == b'false':
            mode = mode_hint
        return mode, sha.strip().decode()
    if stat.S_ISDIR(info.st_mode):
        return '040000', ''
    raise SnapshotError(f'Unsupported filesystem object: {path!r}')


def _prepare(repo_root: Path, snap: Snapshot,
             worktree: Path) -> tuple[ApplyReport, str]:
    root = _validate(snap, repo_root)
    patch, files, tree = _build_diff(snap, worktree)
    if not patch:
        return ApplyReport('noop', [], '', []), tree
    baseline = _tree_entries(root, snap.commit_sha)
    conflicts = [item['path'] for item in files
                 if _state(root, item['path'],
                           mode_hint=baseline.get(item['path'], ('', ''))[0])
                 != baseline.get(item['path'])]
    return ApplyReport('conflict' if conflicts else 'clean', conflicts, patch, files), tree


def preflight(repo_root: Path, snap: Snapshot, worktree: Path) -> ApplyReport:
    """Detect drift on every touched path, including additions and deletions."""
    return _prepare(repo_root, snap, worktree)[0]


@contextmanager
def _apply_lock(root: Path) -> Iterator[None]:
    common = Path(os.fsdecode(_git(root, 'rev-parse', '--git-common-dir').rstrip(b'\n')))
    if not common.is_absolute():
        common = root / common
    lock = common / 'forgebox-apply.lock'
    try:
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise SnapshotError(f'Another Forgebox apply is active: {lock}') from exc
    try:
        os.close(descriptor)
        yield
    finally:
        lock.unlink()


def _force_patch(root: Path, snap: Snapshot, files: list[dict], target: str) -> str:
    """Rebase only touched paths onto their actual state, without resetting files.

    Rewriting real files to snapshot contents before --check would lose edits on
    failure. Instead construct a virtual current tree in a disposable index.
    """
    baseline = _tree_entries(root, snap.commit_sha)
    with _index(root, snap.commit_sha) as env:
        records = []
        for item in files:
            path = item['path']
            state = _state(root, path, write=True,
                           mode_hint=baseline.get(path, ('', ''))[0])
            if state is None:
                mode, sha = '0', '0' * len(snap.commit_sha)
            else:
                mode, sha = state
                if mode == '040000':
                    raise SnapshotError(f'Cannot force overwrite a directory: {path!r}')
            records.append(f'{mode} {sha}\t'.encode() + os.fsencode(path) + b'\0')
        _git(root, 'update-index', '-z', '--index-info', input=b''.join(records), env=env)
        current = _git(root, 'write-tree', env=env).strip().decode()
    return _between(root, current, target)


def apply(repo_root: Path, snap: Snapshot, worktree: Path, *,
          commit_message: str | None = None, force: bool = False) -> dict:
    """Check then apply all hunks; never use --reject or mutate the real index.

    Only an explicitly requested commit stages the touched paths. The lock
    serializes Forgebox writers, not arbitrary editors or other Git processes.
    Git's patch validation is all-or-nothing; this is not crash-atomic storage.
    """
    root = _validate(snap, repo_root)
    with _apply_lock(root):
        report, target = _prepare(root, snap, worktree)
        if report.status == 'noop':
            return {'applied': [], 'conflicts': [], 'commit_sha': None}
        if report.conflicts and not force:
            return {'applied': [], 'conflicts': report.conflicts, 'commit_sha': None}
        patch = _force_patch(root, snap, report.files, target) if force else report.patch
        if patch:
            payload = patch.encode('utf-8', 'surrogateescape')
            _git(root, 'apply', '--check', '--binary', '--whitespace=nowarn', input=payload)
            _git(root, 'apply', '--binary', '--whitespace=nowarn', input=payload)
        paths = [item['path'] for item in report.files]
        commit_sha = None
        if commit_message is not None and patch:
            try:
                _git(root, 'add', '-A', '--', *paths)
                # --only keeps unrelated pre-existing staged changes out of the commit.
                _git(root, 'commit', '--only', '-m', commit_message, '--', *paths)
                commit_sha = _git(root, 'rev-parse', 'HEAD').strip().decode()
            except SnapshotError as exc:
                raise SnapshotError(f'Patch applied, but requested commit failed: {exc}') from exc
        # Conflicts in the result are unresolved conflicts; force resolved them.
        return {'applied': paths, 'conflicts': [], 'commit_sha': commit_sha}


def discard(repo_root: Path, snap: Snapshot) -> None:
    """Drop the detached worktree and its private snapshot reference."""
    root = _validate(snap, repo_root)
    worktree = _worktree_path(snap)
    # Missing directories may still have a registered worktree. Prune first in
    # that case; do not fall back to deleting arbitrary filesystem directories.
    if worktree.exists():
        _git(root, 'worktree', 'remove', '--force', str(worktree))
    _git(root, 'worktree', 'prune', '--expire=now')
    reference = f'refs/forgebox/snapshots/{snap.id}'
    refs = _git(root, 'for-each-ref', '--format=%(refname)', reference).splitlines()
    if reference.encode() in refs:
        _git(root, 'update-ref', '-d', reference, snap.commit_sha)
    snap.worktree_path = None
