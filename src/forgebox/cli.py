"""Forgebox command line interface.

The active session is a flat Snapshot JSON object in .forgebox/active.json;
its journal lives at .forgebox/<id>/journal.jsonl. Core imports stay lazy.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import NoReturn


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stdout)
        self.exit(2, f"{self.prog}: error: {message}\n")

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        if message:
            print(message, end="", file=sys.stdout)
        raise SystemExit(status)


def _load_session(repo: Path):
    from forgebox.core.snapshot import Snapshot

    path = repo / ".forgebox" / "active.json"
    if not path.exists():
        raise ValueError("No active Forgebox session. Run 'forgebox begin' first.")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["repo_root"] = Path(data["repo_root"])
    data["worktree_path"] = (
        Path(data["worktree_path"]) if data.get("worktree_path") else None
    )
    snap = Snapshot(**data)
    if snap.repo_root.resolve() != repo:
        raise ValueError("Active session belongs to a different repository.")
    return snap


def _worktree(snap) -> Path:
    if snap.worktree_path is None or not snap.worktree_path.is_dir():
        raise ValueError("Active session worktree is missing; discard the session.")
    return snap.worktree_path


def _journal(repo: Path, snap):
    from forgebox.journal import Journal

    return Journal(repo / ".forgebox" / snap.id / "journal.jsonl")


def _begin(repo: Path, args: argparse.Namespace) -> int:
    from forgebox.core.snapshot import create_snapshot, create_worktree

    active = repo / ".forgebox" / "active.json"
    if active.exists():
        raise ValueError("A Forgebox session is already active; discard it first.")
    snap = create_snapshot(repo)
    temporary = None
    try:
        snap.worktree_path = create_worktree(snap)
        active.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=active.parent,
            prefix=".active-", suffix=".json", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(asdict(snap), stream, default=str, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(active)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        from forgebox.core.apply import discard

        discard(repo, snap)
        raise
    print(snap.id)
    return 0


def _status(repo: Path, args: argparse.Namespace) -> int:
    if not (repo / ".forgebox" / "active.json").exists():
        print("No active Forgebox session.")
        return 0
    snap = _load_session(repo)
    print(f"Session: {snap.id}\nRepository: {snap.repo_root}")
    print(f"Worktree: {snap.worktree_path}\nCreated: {snap.created_at}")
    for key, value in _journal(repo, snap).summary().items():
        print(f"{key}: {value}")
    return 0


def _log(repo: Path, args: argparse.Namespace) -> int:
    entries = _journal(repo, _load_session(repo)).read()
    if args.json:
        print(json.dumps(entries, indent=2))
    elif not entries:
        print("No journal entries.")
    else:
        for entry in entries:
            print(" | ".join(f"{key}: {value}" for key, value in entry.items()))
    return 0


def _diff(repo: Path, args: argparse.Namespace) -> int:
    from forgebox.core.diff import DiffCard, build_patch, changed_files

    snap = _load_session(repo)
    worktree = _worktree(snap)
    patch = build_patch(snap, worktree)
    files = changed_files(snap, worktree)
    card = DiffCard(patch, files, f"{len(files)} file(s) changed")
    print(card.summary)
    if card.patch:
        print(card.patch, end="" if card.patch.endswith("\n") else "\n")
    return 0


def _preflight(repo: Path, args: argparse.Namespace) -> int:
    from forgebox.core.apply import preflight

    snap = _load_session(repo)
    report = preflight(repo, snap, _worktree(snap))
    print(f"Preflight: {report.status}")
    for path in report.conflicts:
        print(f"Conflict: {path}")
    for item in report.files:
        print(f"{item['status']} {item['path']}")
    return int(report.status == "conflict" or bool(report.conflicts))


def _apply(repo: Path, args: argparse.Namespace) -> int:
    from forgebox.core.apply import apply

    snap = _load_session(repo)
    result = apply(
        repo, snap, _worktree(snap),
        commit_message=args.commit_message, force=args.force,
    )
    if args.json:
        print(json.dumps(result, indent=2))
    elif result.get("conflicts"):
        print("Apply blocked by conflicts:")
        for path in result["conflicts"]:
            print(f"  {path}")
    else:
        paths = result.get("applied", [])
        print(f"Applied {len(paths)} file(s)." if paths else "No changes to apply.")
        for path in paths:
            print(f"  {path}")
        if result.get("commit_sha"):
            print(f"Commit: {result['commit_sha']}")
    return int(bool(result.get("conflicts")))


def _discard(repo: Path, args: argparse.Namespace) -> int:
    from forgebox.core.apply import discard

    snap = _load_session(repo)
    discard(repo, snap)
    (repo / ".forgebox" / "active.json").unlink(missing_ok=True)
    print(f"Discarded session {snap.id}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run a command; return 0, 1 for operational errors, or 2 for bad usage."""
    parser = _Parser(prog="forgebox", description="Try changes in a disposable Git worktree.")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="repository path (default: cwd)")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, handler, help_text in (
        ("begin", _begin, "Create a snapshot and start a session"),
        ("status", _status, "Show the active session and journal summary"),
        ("log", _log, "Show journal entries"),
        ("diff", _diff, "Show the unified session patch"),
        ("preflight", _preflight, "Check for conflicts in the real repository"),
        ("apply", _apply, "Apply session changes to the real repository"),
        ("discard", _discard, "Remove the session worktree and snapshot"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--repo", type=Path, default=argparse.SUPPRESS, help="repository path")
        command.set_defaults(handler=handler)
        if name in {"log", "apply"}:
            command.add_argument("--json", action="store_true", help="output JSON")
        if name == "apply":
            command.add_argument("--commit-message", help="commit applied changes with this message")
            command.add_argument("--force", action="store_true", help="overwrite conflicting paths")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    try:
        return args.handler(args.repo.expanduser().resolve(), args)
    except Exception as exc:
        if getattr(args, "json", False):
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
