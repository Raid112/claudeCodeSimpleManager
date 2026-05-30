"""
Claude Code Launcher — Manage multiple Claude Code instances with embedded terminals.
Uses pywebview + xterm.js + pywinpty.
"""

import sys
import os

# Ensure imports work from project root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _read_hook_stdin() -> str:
    """Read the whole hook payload from fd 0.

    We read the raw file descriptor instead of sys.stdin because a windowed
    PyInstaller build (console=False) sets sys.stdin to None even when the
    parent (claude) piped the payload in — fd 0 still carries it.
    """
    chunks = []
    try:
        while True:
            chunk = os.read(0, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    except Exception:
        pass
    return b"".join(chunks).decode("utf-8", "replace")


def _run_hook_notify():
    """Claude Code hook entry point.

    Spawned by the generated --settings hooks (see terminal/hooks_settings.py).
    Reads the hook payload on stdin and records the session's authoritative state.
    MUST NOT fail loudly: a non-zero exit from a UserPromptSubmit hook BLOCKS the
    user's claude turn (validated empirically).
    """
    try:
        import json
        from terminal.hook_state import write_event

        raw = _read_hook_stdin()
        payload = json.loads(raw) if raw.strip() else {}
        write_event(payload.get("session_id"), payload.get("hook_event_name"))
    except Exception:
        pass
    sys.exit(0)


# Handle the hook invocation before importing webview / heavy modules so it stays
# fast and never boots the GUI when frozen (sys.executable is the GUI exe).
if "--hook-notify" in sys.argv:
    _run_hook_notify()


import webview
from terminal.pty_manager import PtyManager
from terminal.ws_server import WebSocketServer
from terminal.hooks_settings import generate_hooks_settings
from terminal.hook_state import gc_orphans
from api.bridge import Bridge


def main():
    # Generate the per-session hooks settings (points at this executable) and
    # sweep stale hook-state files left by crashed/old sessions. Never let this
    # setup take down the app: on failure, claude just spawns without --settings
    # and falls back to the output heuristic for status.
    try:
        hooks_settings_path = generate_hooks_settings()
    except Exception as e:
        print(f"Hook settings setup failed; status falls back to heuristic: {e}")
        hooks_settings_path = None
    try:
        gc_orphans()
    except Exception:
        pass

    # Initialize PTY manager and WebSocket server
    pty_manager = PtyManager(hooks_settings_path=hooks_settings_path)
    ws_server = WebSocketServer(pty_manager, host="127.0.0.1", port=0)
    ws_server.start()

    print(f"WebSocket server running on port {ws_server.actual_port}")

    # Create bridge (JS API)
    bridge = Bridge(pty_manager, ws_server)

    # Create pywebview window
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
    window = webview.create_window(
        "Claude Code Launcher",
        url=os.path.join(web_dir, "index.html"),
        js_api=bridge,
        width=1200,
        height=800,
        min_size=(900, 500),
        background_color="#0a0a0a",
        text_select=True,
    )

    bridge.set_window(window)

    def on_closing():
        try:
            bridge.save_sessions_from_backend()
        except Exception:
            pass
        pty_manager.close_all()

    window.events.closing += on_closing

    # Start pywebview (blocks until window is closed)
    webview.start(debug=False)


if __name__ == "__main__":
    main()
