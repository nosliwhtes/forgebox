"""I/O failures must never silently leave a partially applied experiment."""
import os
import shutil

import pytest


def test_late_install_failure_restores_files_and_index(session, git_repo, apply_api, monkeypatch):
    snap, worktree = session
    (worktree / 'tracked.txt').write_text('new tracked\n')
    (worktree / 'stable.txt').write_text('new stable\n')
    index = (git_repo / '.git/index').read_bytes()
    replace = os.replace
    failed = False

    def fail_later(source, destination):
        nonlocal failed
        if str(destination) == str(git_repo / 'tracked.txt') and 'staged' in str(source) and not failed:
            failed = True
            raise PermissionError('injected late install failure')
        return replace(source, destination)

    monkeypatch.setattr(apply_api.os, 'replace', fail_later)
    with pytest.raises(apply_api.SnapshotError, match='original files restored'):
        apply_api.apply(git_repo, snap, worktree)
    assert failed
    assert (git_repo / 'stable.txt').read_text() == 'unchanged baseline\n'
    assert (git_repo / 'tracked.txt').read_text() == 'dirty tracked\n'
    assert (git_repo / '.git/index').read_bytes() == index
    assert not list((git_repo / '.forgebox').glob('apply-*'))


def test_rollback_failure_preserves_original_backup(session, git_repo, apply_api, monkeypatch):
    snap, worktree = session
    (worktree / 'tracked.txt').write_text('new tracked\n')
    replace = os.replace

    def fail_install_and_restore(source, destination):
        if str(destination) == str(git_repo / 'tracked.txt'):
            raise PermissionError('injected persistent permission error')
        return replace(source, destination)

    monkeypatch.setattr(apply_api.os, 'replace', fail_install_and_restore)
    with pytest.raises(apply_api.SnapshotError, match='rollback incomplete.*retained at'):
        apply_api.apply(git_repo, snap, worktree)
    backups = list((git_repo / '.forgebox').glob('apply-*/backup/tracked.txt'))
    assert len(backups) == 1
    assert backups[0].read_text() == 'dirty tracked\n'


@pytest.mark.parametrize('direction', ['directory-to-file', 'file-to-directory'])
def test_apply_type_transitions(session, git_repo, apply_api, direction):
    snap, worktree = session
    if direction == 'directory-to-file':
        shutil.rmtree(worktree / 'nested')
        (worktree / 'nested').write_text('replacement file\n')
        expected = git_repo / 'nested'
    else:
        (worktree / 'tracked.txt').unlink()
        (worktree / 'tracked.txt').mkdir()
        (worktree / 'tracked.txt/child').write_text('replacement file\n')
        expected = git_repo / 'tracked.txt/child'
    assert apply_api.preflight(git_repo, snap, worktree).status == 'clean'
    assert apply_api.apply(git_repo, snap, worktree)['conflicts'] == []
    assert expected.read_text() == 'replacement file\n'


def test_directory_to_file_refuses_untracked_concurrent_child(session, git_repo, apply_api):
    snap, worktree = session
    shutil.rmtree(worktree / 'nested')
    (worktree / 'nested').write_text('replacement file\n')
    (git_repo / 'nested/unrelated').write_text('keep me\n')
    result = apply_api.apply(git_repo, snap, worktree)
    assert 'nested' in result['conflicts']
    assert (git_repo / 'nested/unrelated').read_text() == 'keep me\n'


def test_failure_removes_new_directories(session, git_repo, apply_api, monkeypatch):
    snap, worktree = session
    (worktree / 'aaa-new').mkdir()
    (worktree / 'aaa-new/file').write_text('new\n')
    (worktree / 'tracked.txt').write_text('new tracked\n')
    replace = os.replace
    failed = False

    def fail_later(source, destination):
        nonlocal failed
        if str(destination) == str(git_repo / 'tracked.txt') and 'staged' in str(source) and not failed:
            failed = True
            raise PermissionError('later install failed')
        return replace(source, destination)

    monkeypatch.setattr(apply_api.os, 'replace', fail_later)
    with pytest.raises(apply_api.SnapshotError):
        apply_api.apply(git_repo, snap, worktree)
    assert not (git_repo / 'aaa-new').exists()
    assert (git_repo / 'tracked.txt').read_text() == 'dirty tracked\n'


def test_sparse_absent_touched_file_materializes_without_index_changes(git_repo, git, snapshot_api, apply_api):
    git(git_repo, 'update-index', '--skip-worktree', 'stable.txt')
    (git_repo / 'stable.txt').unlink()
    index = (git_repo / '.git/index').read_bytes()
    snap = snapshot_api.create_snapshot(git_repo)
    worktree = snapshot_api.create_worktree(snap)
    (worktree / 'stable.txt').write_text('sparse result\n')
    assert apply_api.preflight(git_repo, snap, worktree).status == 'clean'
    assert apply_api.apply(git_repo, snap, worktree)['conflicts'] == []
    assert (git_repo / 'stable.txt').read_text() == 'sparse result\n'
    assert (git_repo / '.git/index').read_bytes() == index


def test_sparse_delete_refused_without_mutation(git_repo, git, snapshot_api, apply_api):
    git(git_repo, 'update-index', '--skip-worktree', 'stable.txt')
    (git_repo / 'stable.txt').unlink()
    index = (git_repo / '.git/index').read_bytes()
    snap = snapshot_api.create_snapshot(git_repo)
    worktree = snapshot_api.create_worktree(snap)
    (worktree / 'stable.txt').unlink()
    (worktree / 'tracked.txt').write_text('must not land\n')
    with pytest.raises(apply_api.SnapshotError, match='include it in the sparse checkout'):
        apply_api.apply(git_repo, snap, worktree)
    assert (git_repo / 'tracked.txt').read_text() == 'dirty tracked\n'
    assert not (git_repo / 'stable.txt').exists()
    assert (git_repo / '.git/index').read_bytes() == index


def test_sparse_install_failure_restores_absence(git_repo, git, snapshot_api, apply_api, monkeypatch):
    git(git_repo, 'update-index', '--skip-worktree', 'stable.txt')
    (git_repo / 'stable.txt').unlink()
    snap = snapshot_api.create_snapshot(git_repo)
    worktree = snapshot_api.create_worktree(snap)
    (worktree / 'stable.txt').write_text('sparse result\n')
    (worktree / 'tracked.txt').write_text('other result\n')
    replace = os.replace
    failed = False

    def fail_later(source, destination):
        nonlocal failed
        if str(destination) == str(git_repo / 'tracked.txt') and 'staged' in str(source) and not failed:
            failed = True
            raise PermissionError('late failure')
        return replace(source, destination)

    monkeypatch.setattr(apply_api.os, 'replace', fail_later)
    with pytest.raises(apply_api.SnapshotError, match='original files restored'):
        apply_api.apply(git_repo, snap, worktree)
    assert not (git_repo / 'stable.txt').exists()
    assert (git_repo / 'tracked.txt').read_text() == 'dirty tracked\n'


def test_apply_binary_symlink_and_executable(session, git_repo, apply_api):
    snap, worktree = session
    (worktree / 'binary').write_bytes(b'\x00\xff\x01')
    (worktree / 'link').symlink_to('tracked.txt')
    (worktree / 'run').write_text('#!/bin/sh\nexit 0\n')
    (worktree / 'run').chmod(0o755)
    assert apply_api.apply(git_repo, snap, worktree)['conflicts'] == []
    assert (git_repo / 'binary').read_bytes() == b'\x00\xff\x01'
    assert os.readlink(git_repo / 'link') == 'tracked.txt'
    assert (git_repo / 'run').stat().st_mode & 0o111


def test_sparse_staged_baseline_applies_without_rewriting_index(git_repo, git, snapshot_api, apply_api):
    (git_repo / 'stable.txt').write_text('staged sparse baseline\n')
    git(git_repo, 'add', 'stable.txt')
    git(git_repo, 'update-index', '--skip-worktree', 'stable.txt')
    (git_repo / 'stable.txt').unlink()
    index = (git_repo / '.git/index').read_bytes()
    snap = snapshot_api.create_snapshot(git_repo)
    worktree = snapshot_api.create_worktree(snap)
    assert (worktree / 'stable.txt').read_text() == 'staged sparse baseline\n'
    (worktree / 'stable.txt').write_text('updated staged sparse baseline\n')
    assert apply_api.apply(git_repo, snap, worktree)['conflicts'] == []
    assert (git_repo / 'stable.txt').read_text() == 'updated staged sparse baseline\n'
    assert (git_repo / '.git/index').read_bytes() == index


def test_cleanup_failure_reports_applied_outcome(session, git_repo, apply_api, monkeypatch):
    snap, worktree = session
    (worktree / 'tracked.txt').write_text('landed\n')

    rmtree = shutil.rmtree

    def fail_cleanup(path, *args, **kwargs):
        if str(path).startswith(str(git_repo / '.forgebox/apply-')):
            raise PermissionError('cleanup blocked')
        return rmtree(path, *args, **kwargs)

    monkeypatch.setattr(apply_api.shutil, 'rmtree', fail_cleanup)
    with pytest.warns(RuntimeWarning, match='Patch applied, but transaction cleanup failed'):
        result = apply_api.apply(git_repo, snap, worktree)
    assert result['applied'] == ['tracked.txt']
    assert (git_repo / 'tracked.txt').read_text() == 'landed\n'


def test_apply_repo_path_with_trailing_newline(git_repo, git, snapshot_api, apply_api):
    moved = git_repo.with_name(git_repo.name + ' \n')
    git_repo.rename(moved)
    snap = snapshot_api.create_snapshot(moved)
    worktree = snapshot_api.create_worktree(snap)
    (worktree / 'tracked.txt').write_text('newline root result\n')
    assert apply_api.apply(moved, snap, worktree)['conflicts'] == []
    assert (moved / 'tracked.txt').read_text() == 'newline root result\n'


@pytest.mark.parametrize('configuration', ['attributes', 'nested-attributes', 'autocrlf'])
def test_apply_preserves_crlf_conversion(git_repo, git, snapshot_api, apply_api, configuration):
    relative = 'eol.txt'
    if configuration == 'attributes':
        (git_repo / '.gitattributes').write_text('*.txt text eol=crlf\n')
        git(git_repo, 'add', '.gitattributes')
    elif configuration == 'nested-attributes':
        relative = 'nested/eol.txt'
        (git_repo / 'nested/.gitattributes').write_text('*.txt text eol=crlf\n')
        git(git_repo, 'add', 'nested/.gitattributes')
    else:
        git(git_repo, 'config', 'core.autocrlf', 'true')
    (git_repo / relative).write_bytes(b'before\r\n')
    git(git_repo, 'add', relative)
    git(git_repo, 'commit', '-m', 'CRLF fixture')
    index = (git_repo / '.git/index').read_bytes()
    snap = snapshot_api.create_snapshot(git_repo)
    worktree = snapshot_api.create_worktree(snap)
    (worktree / relative).write_bytes(b'after\r\n')
    assert apply_api.preflight(git_repo, snap, worktree).status == 'clean'
    assert apply_api.apply(git_repo, snap, worktree)['conflicts'] == []
    assert (git_repo / relative).read_bytes() == b'after\r\n'
    assert (git_repo / '.git/index').read_bytes() == index
