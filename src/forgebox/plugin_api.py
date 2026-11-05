"""Forgebox desktop API: small JSON-only adapters over the public core API.

Handlers are importable both from the installed package and from Hermes' copied
``dashboard/plugin_api.py``. Core imports are deliberately deferred until use.
Session discovery reads Git snapshot refs, not a second mutable session registry.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from functools import wraps
import json
import os
from pathlib import Path
import subprocess
from typing import Any


def _json_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _handler(function):
    @wraps(function)
    def safe(params=None):
        try:
            if params is None:
                params = {}
            if not isinstance(params, dict):
                raise ValueError("params must be a JSON object")
            result = _json_value(function(params))
            if not isinstance(result, dict):
                raise TypeError("handler did not return an object")
            # Fail here, inside the exception boundary, rather than in transport.
            json.dumps(result, allow_nan=False)
            return result
        except Exception as exc:
            return {"ok": False, "error": str(exc), "code": type(exc).__name__}
    return safe


def _git(repo: Path, *args: str) -> str:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        errors="replace", timeout=30, check=False, env=environment,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Git command failed")
    return result.stdout.strip()


def _repo_root(path: Path) -> Path:
    """Return the main worktree root even when started from a linked worktree.

    ``--show-toplevel`` echoes the enclosing checkout; inside a linked
    worktree that is the linked checkout, not the repository root that owns
    the ``refs/forgebox`` namespace.
    """
    top = Path(_git(path, "rev-parse", "--show-toplevel")).resolve()
    if (top / ".git").is_file():
        common = Path(_git(path, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        if common.name == ".git" and (common.parent / ".git").is_dir():
            return common.parent.resolve()
    return top


def _repo(params: dict) -> Path:
    raw = params.get("repo") or params.get("repo_root") or params.get("cwd")
    if raw is not None and not isinstance(raw, str):
        raise ValueError("repo must be a path string")
    path = Path(raw.strip()).expanduser().resolve() if raw else Path.cwd()
    return _repo_root(path)


def _sessions(repo: Path) -> list[dict]:
    refs = _git(
        repo, "for-each-ref", "--sort=-creatordate",
        "--format=%(refname)\t%(objectname)\t%(creatordate:iso-strict)",
        "refs/forgebox/snapshots/",
    )
    # Match registered worktrees even if the experiment has made new commits.
    worktrees = {}
    for record in _git(repo, "worktree", "list", "--porcelain", "-z").split("\0\0"):
        fields = record.strip("\0").split("\0")
        location = next((f[9:] for f in fields if f.startswith("worktree ")), None)
        if location:
            worktrees[str(Path(location).resolve())] = True
    sessions = []
    for line in refs.splitlines():
        ref, commit_sha, created_at = line.split("\t", 2)
        session_id = ref.removeprefix("refs/forgebox/snapshots/")
        # Snapshot IDs are short hex, never user-controlled path fragments.
        if not session_id or any(c not in "0123456789abcdef" for c in session_id):
            continue
        worktree = repo / ".forgebox" / "wt" / session_id
        active = str(worktree.resolve()) in worktrees and worktree.is_dir()
        sessions.append({
            "id": session_id, "repo": str(repo), "created_at": created_at,
            "status": "active" if active else "snapshot",
            "commit_sha": commit_sha,
            "worktree_path": str(worktree) if active else None,
        })
    return sessions


def _select(params: dict, *, mutation: bool = False) -> tuple[Path, dict]:
    repo = _repo(params)
    session_id = params.get("session_id", params.get("id"))
    if mutation and not session_id:
        raise ValueError("An explicit session_id is required for apply/discard")
    if session_id is not None and not isinstance(session_id, str):
        raise ValueError("session_id must be a string")
    sessions = _sessions(repo)
    session = next((s for s in sessions if s["id"] == session_id), None) if session_id else next(iter(sessions), None)
    if session is None:
        raise ValueError("Forgebox session not found" if session_id else "No Forgebox snapshots in this repository; run forgebox begin")
    return repo, session


def _snapshot(repo: Path, session: dict):
    from forgebox.core.snapshot import Snapshot

    parents = _git(repo, "show", "-s", "--format=%P", session["commit_sha"]).split()
    return Snapshot(
        id=session["id"], repo_root=repo, commit_sha=session["commit_sha"],
        base_head_sha=parents[0] if parents else _git(repo, "rev-parse", "HEAD"),
        created_at=session["created_at"],
        worktree_path=Path(session["worktree_path"]) if session["worktree_path"] else None,
    )


def _active(params: dict, *, mutation: bool = False):
    repo, session = _select(params, mutation=mutation)
    if not session["worktree_path"]:
        raise ValueError("Session has no registered worktree; only discard is available")
    snap = _snapshot(repo, session)
    return repo, session, snap, Path(session["worktree_path"])


@_handler
def list_sessions(params=None):
    """Return snapshot sessions scoped to params.repo (the gateway cwd by default)."""
    repo = _repo(params or {})
    return {"ok": True, "repo": str(repo), "sessions": _sessions(repo)}


@_handler
def status(params=None):
    repo, session = _select(params or {})
    from forgebox.journal import Journal

    journal_path = repo / ".forgebox" / session["id"] / "journal.jsonl"
    summary = Journal(journal_path).summary() if journal_path.is_file() else {
        "entries": 0, "commands": 0, "last_exit": None, "files": [],
    }
    return {**summary, "ok": True, "session": session, "summary": summary}


@_handler
def diff(params=None):
    _, session, snap, worktree = _active(params or {})
    from forgebox.core.diff import build_patch, changed_files

    files = changed_files(snap, worktree)
    return {
        "ok": True, "session_id": session["id"], "patch": build_patch(snap, worktree),
        "files": files, "summary": f"{len(files)} changed file(s)",
    }


@_handler
def preflight(params=None):
    repo, session, snap, worktree = _active(params or {})
    from forgebox.core.apply import preflight as core_preflight

    report = _json_value(core_preflight(repo, snap, worktree))
    return {**report, "ok": True, "session_id": session["id"]}


@_handler
def apply(params=None):
    params = params or {}
    repo, session, snap, worktree = _active(params, mutation=True)
    from forgebox.core.apply import apply as core_apply

    message = params.get("commit_message")
    if message is not None and not isinstance(message, str):
        raise ValueError("commit_message must be a string or null")
    force = params.get("force", False)
    if not isinstance(force, bool):
        raise ValueError("force must be a boolean")
    result = core_apply(repo, snap, worktree, commit_message=message, force=force)
    return {**result, "ok": not bool(result.get("conflicts")), "session_id": session["id"]}


@_handler
def discard(params=None):
    repo, session = _select(params or {}, mutation=True)
    from forgebox.core.apply import discard as core_discard

    core_discard(repo, _snapshot(repo, session))
    if any(item["id"] == session["id"] for item in _sessions(repo)):
        raise RuntimeError("Discard returned but the snapshot ref still exists")
    active = repo / ".forgebox" / "active.json"
    if active.is_file():
        metadata = json.loads(active.read_text(encoding="utf-8"))
        if metadata.get("id") == session["id"] and Path(metadata.get("repo_root", "")).resolve() == repo:
            active.unlink()
    return {"ok": True, "session_id": session["id"]}


HANDLERS = {
    "list_sessions": list_sessions,
    "status": status,
    "diff": diff,
    "preflight": preflight,
    "apply": apply,
    "discard": discard,
}


# Hermes mounts an APIRouter, while CLI/tests need only the stdlib HANDLERS.
# Resolve it on demand so copying/importing this file never requires FastAPI.
_router = None


def __getattr__(name: str):
    global _router
    if name != "router":
        raise AttributeError(name)
    if _router is None:
        from fastapi import APIRouter, Body  # supplied by the Hermes host

        router = APIRouter()

        def endpoint_for(handler):
            # A sync endpoint lets FastAPI run blocking Git work in its thread pool.
            def endpoint(params: dict = Body(default={})):
                return handler(params)
            return endpoint

        for action, handler in HANDLERS.items():
            router.add_api_route(
                f"/{action}", endpoint_for(handler), methods=["POST"],
                name=f"forgebox_{action}",
            )
        _router = router
    return _router
