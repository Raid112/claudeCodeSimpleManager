"""Safe, bounded Git metadata snapshots for managed workspaces."""

from __future__ import annotations

import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

_SAFE_REF = re.compile(r"^[^\x00\r\n]{1,240}$")
_TEST_LINE = re.compile(r"(?i)(pytest|npm\s+(?:test|run\s+test)|cargo\s+test|go\s+test|dotnet\s+test).{0,180}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_git(cwd: Path, args: list[str], timeout: float = 4.0) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            ["git", *args], cwd=str(cwd), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", shell=False, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return completed.returncode, completed.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return 124, ""


def _allowed_workspace(raw_path: str, configured_roots: list[str]) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("workspace_path_missing")
    candidate = Path(raw_path).resolve(strict=True)
    if not candidate.is_dir():
        raise ValueError("workspace_not_directory")
    roots: list[Path] = []
    for raw_root in configured_roots:
        try:
            root = Path(raw_root).resolve(strict=True)
        except (OSError, ValueError):
            continue
        if root.is_dir():
            roots.append(root)
    if not any(candidate == root or root in candidate.parents for root in roots):
        raise ValueError("workspace_not_configured")
    return candidate


def _status_paths(status: str) -> list[str]:
    paths: list[str] = []
    for line in status.splitlines():
        if len(line) < 4:
            continue
        value = line[3:].strip()
        if " -> " in value:
            value = value.rsplit(" -> ", 1)[-1]
        if _SAFE_REF.fullmatch(value):
            paths.append(value.replace("\\", "/")[:240])
    return paths


def _numstat(text: str) -> dict:
    files = insertions = deletions = 0
    for line in text.splitlines():
        columns = line.split("\t")
        if len(columns) < 3:
            continue
        files += 1
        try:
            insertions += int(columns[0]) if columns[0].isdigit() else 0
            deletions += int(columns[1]) if columns[1].isdigit() else 0
        except ValueError:
            pass
    return {"files": files, "insertions": insertions, "deletions": deletions}


def snapshot_workspace(session, configured_roots: list[str], recent_messages: list[dict] | None = None) -> dict:
    observed = time.time()
    result = {
        "source": "git_workspace",
        "trust": "external_data",
        "observed_at": observed,
        "freshness": "fresh",
        "cwd_label": None,
        "branch": None,
        "head": None,
        "dirty": None,
        "changed_files": [],
        "diffstat": {"files": 0, "insertions": 0, "deletions": 0},
        "last_test": None,
    }
    try:
        cwd = _allowed_workspace(getattr(session, "path", None), configured_roots)
    except (OSError, ValueError) as exc:
        result.update({"freshness": "unknown", "error": str(exc)})
        return result
    result["cwd_label"] = cwd.name or "workspace"
    code, branch = _run_git(cwd, ["rev-parse", "--abbrev-ref", "HEAD"])
    if code != 0 or not branch:
        result.update({"freshness": "unknown", "error": "git_workspace_unavailable"})
        return result
    _, head = _run_git(cwd, ["rev-parse", "--short=12", "HEAD"])
    _, status = _run_git(cwd, ["status", "--short", "--untracked-files=all"])
    _, numstat = _run_git(cwd, ["diff", "--numstat"])
    _, commit = _run_git(cwd, ["log", "-1", "--format=%cI%x00%h%x00%s"])
    result.update({
        "branch": branch[:240],
        "head": head[:32] if head else None,
        "dirty": bool(status),
        "changed_files": _status_paths(status)[:100],
        "diffstat": _numstat(numstat),
    })
    if commit:
        fields = commit.split("\x00", 2)
        if len(fields) == 3:
            result["last_commit"] = {"observed_at": fields[0], "short_sha": fields[1], "subject": fields[2][:240]}
    for message in reversed(recent_messages or []):
        text = message.get("text") if isinstance(message, dict) else None
        if not isinstance(text, str):
            continue
        match = _TEST_LINE.search(text)
        if match:
            lower = text.lower()
            result["last_test"] = {
                "command_label": match.group(0)[:200],
                "status": "failed" if any(word in lower for word in ("failed", "error", "traceback")) else "observed",
                "observed_at": message.get("timestamp"),
            }
            break
    return result
