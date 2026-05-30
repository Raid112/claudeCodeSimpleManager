"""
Shared opt-in logging for terminal input/output boundaries.
"""

import os


DEBUG_INPUT = os.getenv("CLAUDE_MANAGER_DEBUG_INPUT", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def log_input_boundary(boundary: str, data: str, **extra):
    if not DEBUG_INPUT:
        return

    text = data if isinstance(data, str) else str(data)
    preview = (
        text.replace("\x1b", "\\x1b")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )[:160]

    details = ", ".join(
        [f"chars={len(text)}", f"preview='{preview}'"] +
        [f"{key}={value!r}" for key, value in extra.items()]
    )
    print(f"[input-debug] {boundary}: {details}")
