"""Forgebox's dict-in/dict-out Hermes hook entry points.

Session discovery uses .forgebox/active.json in the current repo or an ancestor.
The file contains Snapshot fields, optionally nested under ``snapshot``. Hosts
may instead pass ``snapshot`` in params; ``repo_root`` (or FORGEBOX_REPO_ROOT)
selects a repo when the host's working directory is elsewhere. No active
snapshot/worktree means no action. Shell argument inspection is advisory, not
an OS sandbox: dynamically generated paths cannot be proved safe here.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .journal import Journal


_PATH_KEYS = {
    "path", "paths", "file", "files", "filename", "filenames", "file_path",
    "file_paths", "filepath", "directory", "directories", "dir", "cwd",
    "workdir", "working_directory", "source", "destination", "src", "dest",
    "target", "output_path", "input_path", "repo_root", "root",
}
_COMMAND_KEYS = {"command", "cmd", "shell_command"}
# Characters that mark a shell word as something other than a bare path:
# assignments, expressions, globs, redirections, control operators, etc.
_SHELL_SPECIAL = set("=()$`;&|<>*?[]{}!#")


class _OutsideSandbox(ValueError):
    pass


def _get_session(params: dict) -> tuple[Any, Path, Path, Journal] | None:
    snapshot = params.get("snapshot")
    if snapshot is None:
        location = params.get("repo_root") or os.environ.get("FORGEBOX_REPO_ROOT") or Path.cwd()
        start = Path(location).expanduser().resolve()
        for root in (start, *start.parents):
            state = root / ".forgebox" / "active.json"
            try:
                metadata = json.loads(state.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                return None
            if not isinstance(metadata, dict):
                return None
            snapshot = metadata.get("snapshot", metadata)
            break
    if snapshot is None:
        return None
    if isinstance(snapshot, dict):
        snapshot = SimpleNamespace(**snapshot)
    try:
        if not snapshot.worktree_path:
            return None
        repo = Path(snapshot.repo_root).expanduser().resolve()
        worktree = Path(snapshot.worktree_path).expanduser().resolve()
        session_id = str(snapshot.id)
        # Never let an on-disk session identifier choose an arbitrary journal.
        if not session_id or session_id in {".", ".."} or any(c in session_id for c in "/\\"):
            return None
        if not worktree.is_dir():
            return None
    except (AttributeError, TypeError, ValueError, OSError):
        return None
    # Core's contract uses Path fields, including for persisted sessions.
    if isinstance(snapshot, SimpleNamespace):
        snapshot.repo_root = repo
        snapshot.worktree_path = worktree
    return snapshot, repo, worktree, Journal(repo / ".forgebox" / session_id / "journal.jsonl")


def _bare_pathlike(token: str) -> bool:
    """A bare shell word that looks like a path, not an expression/flag."""
    if not token or any(c in _SHELL_SPECIAL for c in token):
        return False
    if "://" in token:
        return False
    return token.startswith(("/", "~/", "./", "../")) or "/" in token


def _split_option(token: str) -> tuple[str, str]:
    """Split a leading option word from an attached path (-I./inc -> ('-I', './inc'))."""
    i, n = 0, len(token)
    if token.startswith("-"):
        i = 1
        if n > 1 and token[1] == "-":
            i = 2
        while i < n and (token[i].isalnum() or token[i] in "-_"):
            i += 1
        if i >= n:
            return "", token
        if token[i] == "=":
            i += 1
        return token[:i], token[i:]
    return "", token


def _shell_tokens(command: str) -> list[tuple[str, bool]]:
    """Split a POSIX-ish command into (token, was_quoted) pairs.

    Quotes are consumed but remembered: a token that was quoted (even partially)
    is left untouched by path rewriting, since quoting signals a literal
    argument (a sed script, a pattern, a string constant, ...) rather than a
    filesystem reference.
    """
    tokens = []
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        if c in " \t\r\n":
            i += 1
            continue
        start = i
        quoted = False
        while i < n:
            c = command[i]
            if c in ("'", '"'):
                quote = c
                i += 1
                quoted = True
                while i < n and command[i] != quote:
                    if quote == '"' and command[i] == "\\" and i + 1 < n:
                        i += 2
                    else:
                        i += 1
                if i < n:
                    i += 1
                continue
            if c in " \t\r\n":
                break
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            i += 1
        tokens.append((command[start:i], quoted))
    return tokens


def _rewrite_path(value: str | os.PathLike, repo: Path, worktree: Path, base: Path) -> str:
    path = Path(value).expanduser()
    trailing = isinstance(value, str) and value.endswith("/")
    if not path.is_absolute():
        path = base / path
    path = path.resolve()
    if path.is_relative_to(worktree):
        return str(path) + ("/" if trailing else "")
    if not path.is_relative_to(repo):
        raise _OutsideSandbox("path outside sandbox")
    target = worktree / path.relative_to(repo)
    # Resolve the destination as well: a worktree symlink can escape even if
    # the original real-repo path did not.
    if not target.resolve().is_relative_to(worktree):
        raise _OutsideSandbox("path outside sandbox")
    return str(target) + ("/" if trailing else "")


def _rewrite_command(command: str, repo: Path, worktree: Path, base: Path) -> str:
    rebuilt = []
    for token, quoted in _shell_tokens(command):
        if quoted:
            rebuilt.append(token)
            continue
        prefix, rest = _split_option(token)
        if prefix and _bare_pathlike(rest):
            rebuilt.append(prefix + _rewrite_path(rest, repo, worktree, base))
        elif _bare_pathlike(token):
            rebuilt.append(_rewrite_path(token, repo, worktree, base))
        else:
            rebuilt.append(token)
    return " ".join(rebuilt)


def _caller_base(args: dict, repo: Path) -> Path:
    """Anchor relative paths to the caller's cwd when it is inside the repo."""
    raw = args.get("cwd") or args.get("workdir") or args.get("working_directory")
    if raw is not None and isinstance(raw, (str, os.PathLike)):
        return Path(raw).expanduser().resolve()
    cwd = Path.cwd().resolve()
    return cwd if cwd.is_relative_to(repo) else repo


def _rewrite(value: Any, repo: Path, worktree: Path, base: Path, key: str = "") -> Any:
    if isinstance(value, dict):
        return {k: _rewrite(v, repo, worktree, base, str(k).lower()) for k, v in value.items()}
    if isinstance(value, list):
        return [_rewrite(item, repo, worktree, base, key) for item in value]
    if isinstance(value, tuple):
        return tuple(_rewrite(item, repo, worktree, base, key) for item in value)
    if isinstance(value, os.PathLike):
        return _rewrite_path(value, repo, worktree, base)
    if not isinstance(value, str) or not value:
        return value
    if key in _COMMAND_KEYS:
        return _rewrite_command(value, repo, worktree, base)
    if key in _PATH_KEYS or key.endswith(("_path", "_paths", "_file", "_dir")):
        # A source/destination field can be a URL rather than a local path.
        if "://" in value:
            return value
        return _rewrite_path(value, repo, worktree, base)
    return value


def _paths(value: Any, repo: Path, worktree: Path, key: str = "") -> list[str]:
    """Best-effort path hints, normalized back to the base repository."""
    found: list[str] = []
    if isinstance(value, dict):
        for child_key, child in value.items():
            found.extend(_paths(child, repo, worktree, str(child_key).lower()))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.extend(_paths(child, repo, worktree, "path" if key == "changed_files" else key))
    elif isinstance(value, (str, os.PathLike)) and (key in _PATH_KEYS or key == "changed_files"):
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = repo / path
        path = path.resolve()
        if path.is_relative_to(worktree):
            found.append(str(path.relative_to(worktree)))
        elif path.is_relative_to(repo):
            found.append(str(path.relative_to(repo)))
    return found


def _record(session: tuple, params: dict, event: str, **extra: Any) -> None:
    _, repo, worktree, journal = session
    args = params.get("args") or {}
    result = params.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            result = None
    exit_code = result.get("exit_code", result.get("returncode")) if isinstance(result, dict) else None
    command = args.get("command", args.get("cmd", params.get("name"))) if isinstance(args, dict) else params.get("name")
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(), "event": event,
        "task_id": params.get("task_id"), "name": params.get("name"),
        "command": command, "exit_code": exit_code,
        "changed_files": sorted(set(_paths(args, repo, worktree) + _paths(result, repo, worktree))),
    }
    if isinstance(result, dict) and "tests_run" in result:
        entry["tests_run"] = result["tests_run"]
    entry.update(extra)
    journal.append(entry)


def pre_tool_call(params: dict) -> dict | None:
    session = _get_session(params)
    if session is None:
        return None
    _, repo, worktree, _ = session
    args = params.get("args") or {}
    try:
        args = {**args}
        cwd_key = "workdir" if params.get("name") == "terminal" else "cwd"
        provided = args.get("cwd") or args.get("workdir") or args.get("working_directory")
        base = _caller_base(args, repo)
        if provided is not None and isinstance(provided, (str, os.PathLike)):
            if Path(provided).expanduser().resolve().is_relative_to(repo):
                # A command requested from inside the repo runs in the sandbox.
                args[cwd_key] = str(worktree)
                base = worktree
        elif any(k in args for k in _COMMAND_KEYS):
            # Default execution cwd for shell tools becomes the sandbox worktree.
            args[cwd_key] = str(worktree)
            base = worktree
        rewritten = _rewrite(args, repo, worktree, base)
    except (_OutsideSandbox, OSError, RuntimeError, ValueError):
        result = {"abort": True, "reason": "path outside sandbox"}
        _record(session, params, "pre_tool_call", aborted=True, reason=result["reason"])
        return result
    _record(session, params, "pre_tool_call")
    return {"args": rewritten} if rewritten != args else None


def post_tool_call(params: dict) -> dict | None:
    session = _get_session(params)
    if session is not None:
        _record(session, params, "post_tool_call")
    return None


def post_approval_response(params: dict) -> dict | None:
    session = _get_session(params)
    if session is None:
        return None
    if params.get("approved") is True and params.get("pending") == "apply":
        from .core.apply import apply

        snapshot, repo, worktree, _ = session
        result = apply(repo, snapshot, worktree)
        _record(session, params, "post_approval_response", approved=True, pending="apply", result=result)
        return result
    _record(session, params, "post_approval_response", approved=params.get("approved"), pending=params.get("pending"))
    return None
