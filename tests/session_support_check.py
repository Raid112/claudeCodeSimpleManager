"""Checks for provider session discovery and resume command construction."""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from terminal.session_support import (
    build_resume_command,
    find_codex_session_id,
    find_opencode_session_id,
    list_opencode_session_ids,
)


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"  ok   {name}")


def main() -> int:
    check("Claude fresh keeps hooks", build_resume_command("claude", hooks_path="hooks.json") == 'claude --settings "hooks.json"')
    check("Claude new session keeps owned id", build_resume_command("claude", session_id="claude-1", resume_session=False) == "claude --session-id claude-1")
    check("Claude resume keeps id", build_resume_command("claude", session_id="claude-1") == "claude --resume claude-1")
    check(
        "Codex fresh disables paste burst detection",
        build_resume_command("codex") == "codex -c disable_paste_burst=true",
    )
    check(
        "Codex resume disables paste burst detection",
        build_resume_command("codex", session_id="codex-1")
        == "codex -c disable_paste_burst=true resume codex-1",
    )
    check("OpenCode resumes by id", build_resume_command("opencode", session_id="ses_1") == "opencode -s ses_1")
    check("PowerShell remains a plain terminal", build_resume_command("powershell") == "")

    with tempfile.TemporaryDirectory(prefix="session_support_") as raw:
        root = Path(raw) / "sessions"
        day = root / "2026" / "07" / "23"
        day.mkdir(parents=True)
        old = day / "rollout-2026-07-23T10-00-00-old.jsonl"
        old.write_text(json.dumps({"type": "session_meta", "payload": {"session_id": "old"}}) + "\n", encoding="utf-8")
        before = {old}
        fresh = day / "rollout-2026-07-23T11-00-00-fresh.jsonl"
        fresh.write_text(json.dumps({"type": "session_meta", "payload": {"session_id": "fresh"}}) + "\n", encoding="utf-8")
        check("Codex ignores pre-existing sessions", find_codex_session_id(root, before) == "fresh")

        db = Path(raw) / "opencode.db"
        con = sqlite3.connect(db)
        con.execute("create table session (id text primary key, directory text, time_created integer, time_updated integer)")
        con.execute("insert into session values (?, ?, ?, ?)", ("ses_old", "C:/repo", 100, 100))
        con.execute("insert into session values (?, ?, ?, ?)", ("ses_new", "C:/repo", 200, 250))
        con.execute("insert into session values (?, ?, ?, ?)", ("ses_other", "C:/other", 300, 300))
        con.commit()
        con.close()
        check("OpenCode lists IDs read-only", list_opencode_session_ids(db) == {"ses_old", "ses_new", "ses_other"})
        check("OpenCode filters directory and timestamp", find_opencode_session_id(db, "C:/repo", 150, {"ses_old"}) == "ses_new")

    print("\nsession support checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
