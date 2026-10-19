import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from terminal import session_context, workspace_snapshot


def test_context_sanitizes_paths_secrets_and_summarizes_tool_use():
    item, refs = session_context._normalize_entry({
        "type": "assistant",
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Changed C:/Users/caio/repo/a.py token=secret123"},
            {"type": "tool_use", "name": "Edit", "input": {"file": "a.py"}},
        ]},
    })
    assert item["text"].startswith("Changed <path>")
    assert "[REDACTED]" in item["text"]
    assert "tool_use:Edit" in item["text"]
    assert refs == []


def test_workspace_snapshot_is_bounded_to_configured_git_root():
    with tempfile.TemporaryDirectory() as root:
        path = Path(root)
        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=path, check=True)
        (path / "base.py").write_text("print('base')", encoding="utf-8")
        subprocess.run(["git", "add", "base.py"], cwd=path, check=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base"], cwd=path, check=True)
        (path / "changed.py").write_text("print('x')", encoding="utf-8")
        session = type("Session", (), {"path": str(path)})()
        result = workspace_snapshot.snapshot_workspace(session, [str(path)])
        assert result["branch"]
        assert result["dirty"] is True
        assert "changed.py" in result["changed_files"]
        assert result["cwd_label"] == path.name


def test_workspace_snapshot_denies_unconfigured_root(tmp_path):
    session = type("Session", (), {"path": str(tmp_path)})()
    result = workspace_snapshot.snapshot_workspace(session, [])
    assert result["freshness"] == "unknown"
    assert result["error"] == "workspace_not_configured"
