"""Forgebox's keyword-argument Hermes hook entry points.

Session discovery uses .forgebox/active.json in the task workspace or an
ancestor of a supported file tool's target, even when the process cwd differs.
The file contains Snapshot fields, optionally nested under ``snapshot``. Hosts
may instead pass ``snapshot`` as a keyword; ``repo_root`` (or FORGEBOX_REPO_ROOT)
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
    "workdir", "working_directory", "destination", "dest",
    "source_path", "destination_path", "output_path", "input_path", "repo_root",
}
_COMMAND_KEYS = {"command", "cmd", "shell_command"}
_CWD_KEYS = ("workdir", "cwd", "working_directory")
# Hermes 0.21.6 file-tool schemas. Do not infer path semantics for arbitrary
# plugin fields (e.g. search_files.target is an enum, not a file).
_TOOL_PATH_KEYS = {name: {"path"} for name in
                   ("read_file", "write_file", "patch", "search_files")}
class _OutsideSandbox(ValueError):
    pass


def _workspace_cwd(params: dict) -> Path:
    """Use Hermes's per-task cwd without making the standalone CLI depend on it."""
    try:
        from agent.runtime_cwd import resolve_agent_cwd
    except ModuleNotFoundError as exc:
        if exc.name not in {"agent", "agent.runtime_cwd"}:
            raise
        return Path.cwd().resolve()

    task_id = params.get("task_id") or params.get("session_id")
    if task_id:
        from tools.terminal_tool import get_session_cwd, resolve_task_overrides

        # A completed terminal cd takes precedence over the initial workspace.
        raw = get_session_cwd(task_id) or resolve_task_overrides(task_id).get("cwd")
        if isinstance(raw, (str, os.PathLike)) and raw:
            path = Path(raw).expanduser()
            if path.is_absolute():
                return path.resolve()
    return resolve_agent_cwd().resolve()


def _requested_base(args: dict, caller: Path) -> Path:
    raw = next((args.get(key) for key in _CWD_KEYS
                if isinstance(args.get(key), (str, os.PathLike)) and args.get(key)), None)
    if raw is None:
        return caller
    path = Path(raw).expanduser()
    return (path if path.is_absolute() else caller / path).resolve()


def _session_locations(params: dict) -> list[Path]:
    caller = _workspace_cwd(params)
    location = params.get("repo_root") or os.environ.get("FORGEBOX_REPO_ROOT")
    if location:
        path = Path(location).expanduser()
        return [(path if path.is_absolute() else caller / path).resolve()]
    args = params.get("args") or {}
    base = _requested_base(args, caller)
    locations = [base, caller]
    for key in _TOOL_PATH_KEYS.get(params.get("tool_name"), ()):
        value = args.get(key)
        if isinstance(value, (str, os.PathLike)) and value:
            path = Path(value).expanduser()
            path = path if path.is_absolute() else base / path
            # Starting at a file would raise NotADirectoryError before we
            # reached its repository. New files may have missing parents.
            locations.append(path if path.is_dir() else path.parent)
    return locations


def _get_session(params: dict) -> tuple[Any, Path, Path, Journal] | None:
    snapshot = params.get("snapshot")
    if snapshot is None:
        seen = set()
        for start in _session_locations(params):
            for root in (start, *start.parents):
                if root in seen:
                    continue
                seen.add(root)
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
            if snapshot is not None:
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


def _caller_base(params: dict, repo: Path, worktree: Path) -> Path:
    """Resolve an explicit relative cwd against the caller, not against itself."""
    cwd = _workspace_cwd(params)
    if params.get("snapshot") or params.get("repo_root") or os.environ.get("FORGEBOX_REPO_ROOT"):
        if not cwd.is_relative_to(repo) and not cwd.is_relative_to(worktree):
            cwd = repo
    return _requested_base(params.get("args") or {}, cwd)


def _rewrite(value: Any, repo: Path, worktree: Path, base: Path, key: str = "") -> Any:
    # Only recognized top-level tool arguments are paths. Content dictionaries
    # may themselves contain keys named path/source and must remain literal.
    if isinstance(value, dict):
        return {k: _rewrite(v, repo, worktree, base, str(k).lower())
                if not isinstance(v, dict) else v for k, v in value.items()}
    if key not in _PATH_KEYS:
        return value
    if isinstance(value, (list, tuple)):
        rewritten = [_rewrite(item, repo, worktree, base, key)
                     if isinstance(item, (str, os.PathLike)) else item for item in value]
        return tuple(rewritten) if isinstance(value, tuple) else rewritten
    if isinstance(value, (str, os.PathLike)) and value:
        if isinstance(value, str) and "://" in value:
            return value
        return _rewrite_path(value, repo, worktree, base)
    return value


def _paths(value: Any, repo: Path, worktree: Path, base: Path, key: str = "") -> list[str]:
    """Best-effort path hints, normalized back to the base repository."""
    found: list[str] = []
    if isinstance(value, dict):
        for child_key, child in value.items():
            found.extend(_paths(child, repo, worktree, base, str(child_key).lower()))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.extend(_paths(child, repo, worktree, base, "path" if key == "changed_files" else key))
    elif isinstance(value, (str, os.PathLike)) and (key in _PATH_KEYS or key == "changed_files"):
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = base / path
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
    command = args.get("command", args.get("cmd", params.get("tool_name"))) if isinstance(args, dict) else params.get("tool_name")
    base = _caller_base(params, repo, worktree)
    path_args = {key: str(base) if key in _CWD_KEYS else value for key, value in args.items()}
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(), "event": event,
        "task_id": params.get("task_id"), "name": params.get("tool_name"),
        "command": command, "exit_code": exit_code,
        "changed_files": sorted(set(_paths(path_args, repo, worktree, base) + _paths(result, repo, worktree, base))),
    }
    if isinstance(result, dict) and "tests_run" in result:
        entry["tests_run"] = result["tests_run"]
    entry.update(extra)
    journal.append(entry)


def pre_tool_call(tool_name: str = "", args: dict | None = None,
                  session_id: str = "", task_id: str = "", **kwargs) -> dict | None:
    params = {**kwargs, "tool_name": tool_name, "args": args or {},
              "session_id": session_id, "task_id": task_id}
    session = _get_session(params)
    if session is None:
        return None
    _, repo, worktree, _ = session
    original = args or {}
    try:
        base = _caller_base(params, repo, worktree)
        # Cwd fields are relative to the caller, while other paths are relative
        # to the requested cwd. Do not flatten a requested subdirectory.
        if tool_name == "patch" and original.get("mode") == "patch":
            result = {"action": "block", "message":
                      "Forgebox: multi-file patch mode is not redirected; use patch replace mode or write_file"}
            _record(session, params, "pre_tool_call", aborted=True, reason=result["message"])
            return result
        path_keys = _TOOL_PATH_KEYS.get(tool_name, set())
        path_args = {k: v for k, v in original.items() if k in path_keys}
        if tool_name == "search_files" and not path_args.get("path"):
            path_args["path"] = "."
        rewritten = {**original, **_rewrite(path_args, repo, worktree, base)}
        for key in set(_CWD_KEYS).intersection(original):
            rewritten[key] = _rewrite_path(base, repo, worktree, repo)
        if tool_name == "terminal" and any(key in original for key in _COMMAND_KEYS):
            cwd_key = "workdir" if tool_name == "terminal" else "cwd"
            rewritten[cwd_key] = _rewrite_path(base, repo, worktree, repo)
    except (_OutsideSandbox, OSError, RuntimeError, ValueError):
        result = {"action": "block", "message": "Forgebox: path outside sandbox"}
        _record(session, params, "pre_tool_call", aborted=True, reason=result["message"])
        return result
    _record(session, params, "pre_tool_call")
    return {"action": "modify", "args": rewritten} if rewritten != original else None


def post_tool_call(tool_name: str = "", args: dict | None = None, result: Any = None,
                   session_id: str = "", task_id: str = "", **kwargs) -> None:
    params = {**kwargs, "tool_name": tool_name, "args": args or {}, "result": result,
              "session_id": session_id, "task_id": task_id}
    session = _get_session(params)
    if session is not None:
        _record(session, params, "post_tool_call")
    return None


def post_approval_response(choice: str = "", session_key: str = "",
                           command: str = "", **kwargs) -> None:
    """Observe approval decisions only; applying requires CLI/dashboard action."""
    params = {**kwargs, "args": {"command": command}, "session_key": session_key}
    session = _get_session(params)
    if session is not None:
        _record(session, params, "post_approval_response", choice=choice,
                decided_by=kwargs.get("decided_by"), session_key=session_key)
    return None
