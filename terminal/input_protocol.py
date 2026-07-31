"""Safe semantic text-to-PTY input preparation."""

from __future__ import annotations

import re


BRACKETED_PASTE_START = "\x1b[200~"
BRACKETED_PASTE_END = "\x1b[201~"
DEFAULT_MAX_BYTES = 12 * 1024
_LINE_ENDINGS = re.compile(r"\r\n|\r")


class InputProtocolError(ValueError):
    pass


def normalize_line_endings(text: str) -> str:
    if not isinstance(text, str):
        raise InputProtocolError("text must be a string")
    return _LINE_ENDINGS.sub("\n", text)


def _validate_text(text: str, max_bytes: int) -> str:
    normalized = normalize_line_endings(text)
    if max_bytes <= 0:
        raise InputProtocolError("max_bytes must be positive")
    encoded = normalized.encode("utf-8")
    if len(encoded) > max_bytes:
        raise InputProtocolError("text exceeds UTF-8 byte limit")
    for character in normalized:
        code = ord(character)
        if code == 0 or code == 0x1B or code == 0x7F or (code < 0x20 and code not in {0x09, 0x0A}):
            raise InputProtocolError("text contains an unsupported control character")
    return normalized


def _provider_supports_bracketed(provider: str) -> bool:
    return provider in {"claude", "codex", "opencode"}


def _to_terminal_line_endings(text: str) -> str:
    return text.replace("\n", "\r")


def prepare_terminal_paste(text: str, *, bracketed_paste_mode: bool = False,
                           provider: str = "claude", max_bytes: int = DEFAULT_MAX_BYTES) -> str:
    normalized = _validate_text(text, max_bytes)
    payload = _to_terminal_line_endings(normalized)
    if bracketed_paste_mode and _provider_supports_bracketed(provider):
        payload = f"{BRACKETED_PASTE_START}{payload}{BRACKETED_PASTE_END}"
    return payload


def prepare_composer_message(text: str, *, bracketed_paste_mode: bool = False,
                             provider: str = "claude", max_bytes: int = DEFAULT_MAX_BYTES) -> str:
    normalized = _validate_text(text, max_bytes)
    if "\n" in normalized and bracketed_paste_mode and _provider_supports_bracketed(provider):
        payload = f"{BRACKETED_PASTE_START}{_to_terminal_line_endings(normalized)}{BRACKETED_PASTE_END}"
    else:
        payload = normalized if "\n" in normalized and not bracketed_paste_mode else normalized
    return payload + "\r"
