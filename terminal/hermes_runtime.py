"""Small helpers for resolving credentials used by the local Hermes runtime."""

import os
from collections.abc import Mapping
from pathlib import Path


def read_hermes_token(environ: Mapping[str, str] | None = None) -> str | None:
    """Return the configured Hermes token without exposing it in logs.

    An explicit environment token takes precedence over the protected token file.
    Empty, missing, unreadable, and whitespace-only values disable authentication
    rather than causing application startup to fail.
    """
    values = os.environ if environ is None else environ
    direct_token = values.get("CLAUDEMANAGER_HERMES_TOKEN", "").strip()
    if direct_token:
        return direct_token

    token_file = values.get("CLAUDEMANAGER_HERMES_TOKEN_FILE", "").strip()
    if not token_file:
        return None

    try:
        file_token = Path(token_file).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return file_token or None
