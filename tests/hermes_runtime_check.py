"""Checks for local Hermes runtime credential resolution."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from terminal.hermes_runtime import read_hermes_token


def test_explicit_environment_token_wins_over_token_file() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        token_path = Path(temp_dir) / "hermes-token"
        token_path.write_text("file-token\n", encoding="utf-8")

        token = read_hermes_token(
            {"CLAUDEMANAGER_HERMES_TOKEN": "env-token", "CLAUDEMANAGER_HERMES_TOKEN_FILE": str(token_path)}
        )

        assert token == "env-token"


def test_token_file_is_used_when_environment_token_is_absent() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        token_path = Path(temp_dir) / "hermes-token"
        token_path.write_text("file-token\n", encoding="utf-8")

        token = read_hermes_token({"CLAUDEMANAGER_HERMES_TOKEN_FILE": str(token_path)})

        assert token == "file-token"


def test_missing_or_blank_credentials_disable_hermes_authentication() -> None:
    assert read_hermes_token({"CLAUDEMANAGER_HERMES_TOKEN": "  "}) is None
    assert read_hermes_token({"CLAUDEMANAGER_HERMES_TOKEN_FILE": os.devnull}) is None


if __name__ == "__main__":
    test_explicit_environment_token_wins_over_token_file()
    test_token_file_is_used_when_environment_token_is_absent()
    test_missing_or_blank_credentials_disable_hermes_authentication()
    print("hermes runtime checks passed")
