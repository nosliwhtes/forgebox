"""Conflict-aware application and disposal of isolated worktree changes."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import stat
import shutil
import tempfile
import warnings
from typing import Iterator

from .diff import _between, _build_diff
from .snapshot import (Snapshot, SnapshotError, _git, _index, _tree_entries,
                       _validate, _worktree_path, _sparse_entries)


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
    sparse = _sparse_entries(root)
    deletions = {item['path'] for item in files if item['status'] == 'D'}
    conflicts = []
    for item in files:
        path = item['path']
        state = _state(root, path, mode_hint=baseline.get(path, ('', ''))[0])
        if path in sparse and item['status'] == 'D':
            raise SnapshotError(
                f'Cannot delete skip-worktree path {path!r} while preserving the index; '
                'include it in the sparse checkout before applying.'
            )
        if state is None and path in sparse:
            state = sparse[path]
        # Git trees do not store directories. A directory becoming a file is
        # clean only when all its existing leaves are covered by deletions.
        if state == ('040000', '') and path not in baseline:
            leaves = set()
            for directory, dirs, names in os.walk(root / path, followlinks=False):
                if not dirs and not names and Path(directory) != root / path:
                    leaves.add(Path(directory).relative_to(root).as_posix())
                for name in names + [d for d in dirs if (Path(directory) / d).is_symlink()]:
                    leaves.add((Path(directory) / name).relative_to(root).as_posix())
            if leaves <= deletions:
                state = None
        if state != baseline.get(path):
            conflicts.append(path)
    return ApplyReport('conflict' if conflicts else 'clean', conflicts, patch, files), tree


def preflight(repo_root: Path, snap: Snapshot, worktree: Path) -> ApplyReport:
    """Detect drift on every touched path, including additions and deletions."""
    return _prepare(repo_root, snap, worktree)[0]


@contextmanager
def _apply_lock(root: Path) -> Iterator[None]:
    common = Path(os.fsdecode(_git(root, 'rev-parse', '--git-common-dir').removesuffix(b'\n')))
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
    sparse = _sparse_entries(root)
    with _index(root, snap.commit_sha) as env:
        records = []
        for item in files:
            path = item['path']
            state = _state(root, path, write=True,
                           mode_hint=baseline.get(path, ('', ''))[0])
            if state is None and path in sparse:
                state = sparse[path]
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


def _transaction_roots(root: Path, paths: list[str]) -> list[str]:
    """Collapse overlapping paths and include any missing parent directories."""
    candidates = []
    for path in paths:
        actual = _safe_path(root, path)
        while actual.parent != root and not actual.parent.exists():
            actual = actual.parent
        candidates.append(actual.relative_to(root).as_posix())
    selected = []
    for path in sorted(set(candidates), key=lambda p: (len(PurePosixPath(p).parts), p)):
        if not any(path == parent or path.startswith(parent + '/') for parent in selected):
            selected.append(path)
    return selected


def _transactional_patch(root: Path, payload: bytes, paths: list[str]) -> None:
    """Stage first, then rename with backups; retain recovery data on failure.

    This protects against ordinary I/O errors, not process crashes, power loss,
    or concurrent writers outside Forgebox's lock.
    """
    roots = _transaction_roots(root, paths)
    storage = root / '.forgebox'
    if storage.is_symlink():
        raise SnapshotError('Transaction storage cannot be a symlink')
    storage.mkdir(exist_ok=True)
    transaction = Path(tempfile.mkdtemp(prefix='apply-', dir=storage))
    staged, backup = transaction / 'staged', transaction / 'backup'
    staged.mkdir()
    backup.mkdir()
    operations = []
    retain = False
    installed_all = False
    try:
        for path in roots:
            source, destination = _safe_path(root, path), staged / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_symlink():
                destination.symlink_to(os.readlink(source))
            elif source.is_dir():
                shutil.copytree(source, destination, symlinks=True)
            elif source.exists():
                shutil.copy2(source, destination)
        for path, (mode, sha) in _sparse_entries(root).items():
            actual = _safe_path(root, path)
            if path not in paths or actual.exists() or actual.is_symlink():
                continue
            destination = staged / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            if mode == '120000':
                destination.symlink_to(os.fsdecode(_git(root, 'cat-file', 'blob', sha)))
            else:
                destination.write_bytes(_git(root, 'cat-file', '--filters', '--path=' + path, sha))
                destination.chmod(0o755 if mode == '100755' else 0o644)
        # Apply with the real repository's configuration and attribute context,
        # but an isolated worktree AND index. --no-index loses EOL/filter rules
        # and cannot apply canonical LF patches to CRLF working files.
        attributes = {Path('.gitattributes')}
        for path in paths:
            for parent in PurePosixPath(path).parents:
                attributes.add(Path(parent) / '.gitattributes')
        for relative in attributes:
            source, destination = root / relative, staged / relative
            if source.is_file() and not source.is_symlink() and not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
        git_dir = os.fsdecode(_git(root, 'rev-parse', '--absolute-git-dir').removesuffix(b'\n'))
        index_path = Path(os.fsdecode(_git(
            root, 'rev-parse', '--path-format=absolute', '--git-path', 'index'
        ).removesuffix(b'\n')))
        apply_env = {'GIT_DIR': git_dir, 'GIT_WORK_TREE': str(staged),
                     'GIT_INDEX_FILE': str(transaction / 'index')}
        if index_path.exists():
            shutil.copy2(index_path, transaction / 'index')
        else:
            _git(staged, 'read-tree', 'HEAD', env=apply_env)
        _git(staged, 'apply', '--check', '--binary', '--whitespace=nowarn',
             input=payload, env=apply_env)
        _git(staged, 'apply', '--binary', '--whitespace=nowarn',
             input=payload, env=apply_env)
        for path in roots:
            source, destination, saved = staged / path, _safe_path(root, path), backup / path
            saved.parent.mkdir(parents=True, exist_ok=True)
            operation = [path, False, False]
            operations.append(operation)
            if destination.exists() or destination.is_symlink():
                os.replace(destination, saved)
                operation[1] = True
            if source.exists() or source.is_symlink():
                os.replace(source, destination)
                operation[2] = True
        installed_all = True
    except (OSError, SnapshotError, KeyboardInterrupt) as exc:
        errors = []
        for path, saved_original, installed in reversed(operations):
            destination, saved = root / path, backup / path
            try:
                if installed:
                    # Preserve the staged result too; never delete a live path
                    # during recovery, even if it changed after installation.
                    recovered = transaction / 'recovered' / path
                    recovered.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(destination, recovered)
                if saved_original:
                    os.replace(saved, destination)
            except OSError as recovery_error:
                errors.append(f'{path}: {recovery_error}')
        if errors:
            retain = True
            raise SnapshotError(
                f'Apply failed ({exc}); rollback incomplete: {"; ".join(errors)}. '
                f'Original files and recovery data retained at {transaction}'
            ) from exc
        raise SnapshotError(f'Apply failed; original files restored: {exc}') from exc
    finally:
        if not retain:
            try:
                shutil.rmtree(transaction)
            except OSError as cleanup_error:
                outcome = 'Patch applied' if installed_all else 'Original files restored'
                warnings.warn(
                    f'{outcome}, but transaction cleanup failed: {cleanup_error}. '
                    f'Remaining temporary data: {transaction}', RuntimeWarning,
                )


def apply(repo_root: Path, snap: Snapshot, worktree: Path, *,
          commit_message: str | None = None, force: bool = False) -> dict:
    """Check then apply all hunks; never use --reject or mutate the real index.

    Only an explicitly requested commit stages the touched paths. The lock
    serializes Forgebox writers, not arbitrary editors or other Git processes.
    Staging and rename-backed rollback protect against I/O failures; this is
    not crash-atomic storage or isolation from concurrent external writers.
    """
    root = _validate(snap, repo_root)
    with _apply_lock(root):
        report, target = _prepare(root, snap, worktree)
        if report.status == 'noop':
            return {'applied': [], 'conflicts': [], 'commit_sha': None}
        if report.conflicts and not force:
            return {'applied': [], 'conflicts': report.conflicts, 'commit_sha': None}
        patch = (_force_patch(root, snap, report.files, target)
                 if force and report.conflicts else report.patch)
        paths = [item['path'] for item in report.files]
        if patch:
            payload = patch.encode('utf-8', 'surrogateescape')
            _transactional_patch(root, payload, paths)
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

