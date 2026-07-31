"""Provider-neutral session discovery and resume command helpers.

The PTY manager owns the process, while this module only deals with provider
specific session identifiers. Keeping this logic pure/read-only makes it safe
to test without starting an interactive CLI.
"""

import json
import sqlite3
from pathlib import Path


def build_resume_command(
    terminal_type: str,
    session_id: str | None = None,
    continue_session: bool = False,
    hooks_path: str | None = None,
    resume_session: bool = True,
) -> str:
    """Build the provider command used inside the PowerShell PTY."""
    if terminal_type == "claude":
        if session_id:
            command = f"claude --resume {session_id}" if resume_session else f"claude --session-id {session_id}"
        elif continue_session:
            command = "claude --continue"
        else:
            command = "claude"
        if hooks_path:
            command += f' --settings "{hooks_path}"'
        return command
    if terminal_type == "codex":
        command = "codex -c disable_paste_burst=true"
        return f"{command} resume {session_id}" if session_id else command
    if terminal_type == "opencode":
        return f"opencode -s {session_id}" if session_id else "opencode"
    return ""


def _codex_id_from_file(path: Path) -> str | None:
    """Read the authoritative session_meta record from a Codex JSONL file."""
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if record.get("type") != "session_meta":
                    continue
                payload = record.get("payload") or {}
                return payload.get("session_id") or payload.get("id")
    except (OSError, UnicodeError):
        return None
    return None


def find_codex_session_id(root: Path, known_files: set[Path]) -> str | None:
    """Find the newest new Codex session below ``root``.

    ``known_files`` is captured immediately before spawning Codex. Files are
    ignored until the provider has written the session metadata, so a session
    created lazily after the first prompt is handled by later polling.
    """
    try:
        candidates = [
            path for path in root.rglob("*.jsonl")
            if path not in known_files
        ]
    except OSError:
        return None
    for path in sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True):
        session_id = _codex_id_from_file(path)
        if session_id:
            return session_id
    return None


def _same_directory(left: str, right: str) -> bool:
    return left.replace("\\", "/").rstrip("/").casefold() == right.replace("\\", "/").rstrip("/").casefold()


def find_opencode_session_id(
    database_path: Path,
    directory: str,
    started_at_ms: int,
    known_ids: set[str],
) -> str | None:
    """Find a newly-created OpenCode session for a directory, read-only."""
    if not database_path.exists():
        return None
    connection = None
    try:
        uri = f"file:{database_path.resolve().as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        rows = connection.execute(
            "select id, directory from session "
            "where time_created >= ? order by time_updated desc",
            (started_at_ms,),
        ).fetchall()
    except (OSError, sqlite3.Error):
        return None
    finally:
        if connection is not None:
            connection.close()
    for session_id, session_directory in rows:
        if session_id not in known_ids and _same_directory(session_directory or "", directory):
            return session_id
    return None


def list_opencode_session_ids(database_path: Path) -> set[str]:
    """Return known OpenCode IDs without mutating or checkpointing its database."""
    if not database_path.exists():
        return set()
    connection = None
    try:
        uri = f"file:{database_path.resolve().as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        return {row[0] for row in connection.execute("select id from session").fetchall()}
    except (OSError, sqlite3.Error):
        return set()
    finally:
        if connection is not None:
            connection.close()
