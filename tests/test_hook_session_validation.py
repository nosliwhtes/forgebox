"""Untrusted discovery metadata must not authorize path or journal rewrites."""
import json
from pathlib import Path

import pytest

from forgebox import hooks


@pytest.mark.parametrize('discovery', ['target', 'workspace', 'explicit'])
@pytest.mark.parametrize('nested', [False, True])
def test_forged_home_worktree_is_ignored(git_repo, monkeypatch, discovery, nested):
    home = Path.home()  # The fixture supplies an isolated temporary HOME.
    victim = home / '.bashrc'
    victim.write_text('keep me\n')
    metadata = {'id': 'abcdef012345', 'repo_root': str(git_repo),
                'worktree_path': str(home)}
    (git_repo / '.forgebox').mkdir()
    state = git_repo / '.forgebox/active.json'
    state.write_text(json.dumps({'snapshot': metadata} if nested else metadata))
    monkeypatch.chdir(git_repo if discovery == 'workspace' else git_repo.parent)
    kwargs = {'snapshot': metadata} if discovery == 'explicit' else {}
    args = {'path': str(git_repo / '.bashrc'), 'content': 'attacker content'}
    result = hooks.pre_tool_call(tool_name='write_file', args=args, **kwargs)
    assert result is None
    hooks.post_tool_call(tool_name='write_file', args=args, **kwargs)
    hooks.post_approval_response(**kwargs)
    assert victim.read_text() == 'keep me\n'
    assert not list((git_repo / '.forgebox').rglob('journal.jsonl'))


@pytest.mark.parametrize('damage', ['missing-ref', 'wrong-ref', 'wrong-owner',
                                   'unregistered-worktree', 'symlink-storage',
                                   'outside-worktree', 'journal-symlink', 'journal-directory-symlink'])
def test_invalid_session_is_ignored(cli_session, git, monkeypatch, damage):
    repo, worktree = cli_session
    state = repo / '.forgebox/active.json'
    data = json.loads(state.read_text())
    ref = f"refs/forgebox/snapshots/{data['id']}"
    if damage == 'missing-ref':
        git(repo, 'update-ref', '-d', ref)
    elif damage == 'wrong-ref':
        git(repo, 'update-ref', ref, 'HEAD')
    elif damage == 'wrong-owner':
        other = repo.parent / 'other'
        other.mkdir()
        (other / '.forgebox').mkdir()
        (other / '.forgebox/active.json').write_text(state.read_text())
        monkeypatch.chdir(other)
        repo = other
    elif damage == 'outside-worktree':
        data['worktree_path'] = str(Path.home())
        state.write_text(json.dumps(data))
    elif damage == 'journal-symlink':
        (Path.home() / '.bashrc').write_text('keep me\n')
        directory = repo / '.forgebox' / data['id']
        directory.mkdir(exist_ok=True)
        (directory / 'journal.jsonl').symlink_to(Path.home() / '.bashrc')
    elif damage == 'journal-directory-symlink':
        directory = repo / '.forgebox' / data['id']
        if directory.exists():
            directory.rename(repo / '.forgebox/saved-journal')
        directory.symlink_to(Path.home(), target_is_directory=True)
    elif damage == 'unregistered-worktree':
        git(repo, 'worktree', 'remove', '--force', str(worktree))
        worktree.mkdir()
        git(worktree, 'init', '--quiet')
    elif damage == 'symlink-storage':
        storage = repo / '.forgebox/wt'
        moved = repo / '.forgebox/moved'
        storage.rename(moved)
        storage.symlink_to(moved, target_is_directory=True)
    before = {p: p.read_bytes() for p in (repo / '.forgebox').rglob('journal.jsonl')}
    assert hooks.pre_tool_call(tool_name='write_file', args={
        'path': str(repo / 'tracked.txt'), 'content': 'no'}) is None
    assert {p: p.read_bytes() for p in (repo / '.forgebox').rglob('journal.jsonl')} == before


def test_valid_explicit_snapshot_still_redirects(session):
    snapshot, worktree = session
    result = hooks.pre_tool_call(tool_name='write_file',
        args={'path': str(snapshot.repo_root / 'tracked.txt')}, snapshot=snapshot)
    assert result['args']['path'] == str(worktree / 'tracked.txt')


def test_nested_metadata_and_worktree_cwd_still_redirect(cli_session, monkeypatch):
    repo, worktree = cli_session
    state = repo / '.forgebox/active.json'
    state.write_text(json.dumps({'snapshot': json.loads(state.read_text())}))
    monkeypatch.chdir(worktree)
    assert hooks.pre_tool_call(tool_name='write_file', args={
        'path': str(repo / 'tracked.txt')})['args']['path'] == str(worktree / 'tracked.txt')
