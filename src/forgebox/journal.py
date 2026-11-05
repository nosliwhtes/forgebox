"""Durable, append-only JSONL journals for Forgebox sessions."""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Iterator

try:
    import fcntl
except ImportError:  # POSIX is the supported runtime; retain a basic fallback.
    fcntl = None


@contextmanager
def _locked(fd: int, exclusive: bool) -> Iterator[None]:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    try:
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)


class Journal:
    """A JSON object per line; writers serialize and fsync before returning.

    A missing journal reads as empty. A truncated final record (for example,
    after a process crash) is ignored; corruption in a completed record raises
    ValueError rather than silently losing audit history.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    def append(self, entry: dict) -> None:
        if not isinstance(entry, dict):
            raise TypeError("journal entry must be a dict")
        record = dict(entry)
        record.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        data = (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with _locked(fd, True):
                # Preserve an interrupted tail as a visibly corrupt record;
                # never concatenate a fresh object onto it.
                end = os.lseek(fd, 0, os.SEEK_END)
                if end:
                    os.lseek(fd, -1, os.SEEK_END)
                    if os.read(fd, 1) != b"\n":
                        raise ValueError("journal has an incomplete final record")
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    if written == 0:
                        raise OSError("short journal write")
                    view = view[written:]
                os.fsync(fd)
        finally:
            os.close(fd)

    def read(self) -> list[dict]:
        try:
            stream = self.path.open("rb")
        except FileNotFoundError:
            return []
        with stream:
            with _locked(stream.fileno(), False):
                data = stream.read()
        entries = []
        for line in data.splitlines(keepends=True):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                if not line.endswith(b"\n"):
                    break
                raise ValueError("invalid JSONL journal record") from None
            if not isinstance(value, dict):
                raise ValueError("journal record must be a JSON object")
            entries.append(value)
        return entries

    def summary(self) -> dict:
        entries = self.read()
        commands = 0
        pending: Counter = Counter()
        files: set[str] = set()
        last_exit = None
        for entry in entries:
            # Count paired pre/post records once, and post-only observations
            # once too (a host may attach the journal after a call starts).
            event = entry.get("event")
            command = entry.get("command", entry.get("name"))
            if command is not None and event != "post_approval_response":
                key = (entry.get("task_id"), entry.get("name"),
                       json.dumps(command, sort_keys=True))
                if event == "post_tool_call" and pending[key]:
                    pending[key] -= 1
                else:
                    commands += 1
                    if event == "pre_tool_call":
                        pending[key] += 1
            if entry.get("exit_code") is not None:
                last_exit = entry["exit_code"]
            paths = entry.get("changed_files", entry.get("files", [])) or []
            if isinstance(paths, (str, dict)):
                paths = [paths]
            for item in paths:
                path = item.get("path") if isinstance(item, dict) else item
                if path is not None:
                    files.add(str(path))
        return {"entries": len(entries), "commands": commands,
                "last_exit": last_exit, "files": sorted(files)}
