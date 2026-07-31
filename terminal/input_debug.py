r"""
Terminal input/output boundary logging.

Input boundaries (`ws->pty`, `pty-write`) are ALWAYS logged to a rotating file —
they only fire on user keystrokes, so the volume is tiny and it's exactly what we
need to diagnose input bugs (e.g. arrow-key escape sequences).

The output boundary (`pty->ws`) fires on every PTY output chunk (spinner, redraws,
full stream) and would flood the log, so it stays opt-in behind an env var.

Log file: %LOCALAPPDATA%\ClaudeManager\logs\terminal-io.log  (rotates to .old at 5 MB)
"""

import os
import time


def _truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


# Output (pty->ws) floods the log; keep it opt-in. Input is cheap → always on.
LOG_OUTPUT = _truthy("CLAUDE_MANAGER_DEBUG_OUTPUT")
# Escape hatch to silence input logging too, if ever needed.
LOG_INPUT = not _truthy("CLAUDE_MANAGER_DEBUG_OFF")

_LOG_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "ClaudeManager",
    "logs",
)
_LOG_FILE = os.path.join(_LOG_DIR, "terminal-io.log")
_MAX_BYTES = 5 * 1024 * 1024  # rotate at 5 MB


def _write_line(line: str):
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        if os.path.exists(_LOG_FILE) and os.path.getsize(_LOG_FILE) > _MAX_BYTES:
            old = _LOG_FILE + ".old"
            try:
                if os.path.exists(old):
                    os.remove(old)
                os.replace(_LOG_FILE, old)
            except OSError:
                pass
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


_CTRL_NAMES = {0x0d: "\\r", 0x0a: "\\n", 0x09: "\\t", 0x08: "\\b", 0x7f: "\\x7f"}


def _redact(text: str) -> str:
    """Show control/escape sequences literally; redact printable text to a count.

    Arrow keys, Esc, Enter, Ctrl+X and paste markers stay visible (e.g. `\\x1b[A`,
    `\\x1b[200~<42c>\\x1b[201~`), but whatever the user types/pastes as message
    content is reduced to `<N c>` so it never lands in the log.
    """
    out = []
    run = 0
    esc = 0  # remaining bytes of an in-progress escape sequence to show verbatim

    def flush():
        nonlocal run
        if run:
            out.append(f"<{run}c>")
            run = 0

    for ch in text:
        o = ord(ch)
        if o == 0x1b:
            flush()
            out.append("\\x1b")
            esc = 8
        elif o < 0x20 or o == 0x7f:
            flush()
            out.append(_CTRL_NAMES.get(o, f"\\x{o:02x}"))
            esc = 0
        elif esc > 0:
            out.append(ch)
            was_introducer = esc == 8  # first byte after ESC (`[`, `O`, …) never terminates
            esc -= 1
            if not was_introducer and (ch.isalpha() or ch == "~"):  # CSI/SS3 final byte
                esc = 0
        else:
            run += 1
    flush()
    return "".join(out)


def log_input_boundary(boundary: str, data: str, **extra):
    is_output = boundary == "pty->ws"
    if is_output and not LOG_OUTPUT:
        return
    if not is_output and not LOG_INPUT:
        return

    text = data if isinstance(data, str) else str(data)
    preview = _redact(text)[:200]

    details = ", ".join(
        [f"chars={len(text)}", f"preview='{preview}'"]
        + [f"{key}={value!r}" for key, value in extra.items()]
    )
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_line(f"{ts} [{boundary}] {details}")
