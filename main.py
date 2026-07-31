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
        # CLAUDEMANAGER_TAB is injected into the pty env at spawn (pty_manager) and
        # inherited down powershell -> claude -> this hook. It identifies the tab
        # regardless of how many /clear or /resume swapped the session_id underneath.
        tab_id = os.environ.get("CLAUDEMANAGER_TAB")
        # tool_name is only present on Pre/PostToolUse payloads; None elsewhere.
        write_event(payload.get("session_id"), payload.get("hook_event_name"),
                    tool_name=payload.get("tool_name"), tab_id=tab_id)
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
from terminal.agent_gateway import AgentGateway
from terminal.hooks_settings import generate_hooks_settings
from terminal.hook_state import gc_orphans
from terminal.hermes_runtime import read_hermes_token
from terminal import work_items, jira_client, teams_graph
from api.bridge import Bridge, _load_config


def _patch_clipboard_permissions():
    """pywebview never wires up WebView2's PermissionRequested event, so
    Windows silently denies clipboard access (no prompt, no error) —
    see github.com/r0x0r/pywebview/issues/1561. Wrap on_webview_ready to
    auto-allow ClipboardRead so terminal copy/paste actually reaches the
    OS clipboard."""
    try:
        from webview.platforms.edgechromium import EdgeChrome
    except ImportError:
        return

    original_on_webview_ready = EdgeChrome.on_webview_ready

    def patched_on_webview_ready(self, sender, args):
        original_on_webview_ready(self, sender, args)
        if args.IsSuccess:
            def on_permission_requested(_, perm_args):
                if perm_args.PermissionKind == perm_args.PermissionKind.ClipboardRead:
                    perm_args.State = perm_args.State.Allow
            sender.CoreWebView2.PermissionRequested += on_permission_requested

    EdgeChrome.on_webview_ready = patched_on_webview_ready


def main():
    _patch_clipboard_permissions()

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
    try:
        work_items.gc_events()  # drop event partitions older than 60 days
    except Exception:
        pass

    # Warm up the Teams Graph token off the main thread so a fresh machine (no MSAL cache)
    # gets its one-time device-code login without blocking app boot. With a valid cache this
    # is a silent refresh; with none it prints the device-flow prompt to the console. No-op
    # when Graph creds are absent. list_recent() stays best-effort regardless.
    try:
        import threading
        from terminal import teams_graph
        if teams_graph.available():
            def _warm_teams():
                teams_graph.ensure_auth()
                try:
                    teams_graph.list_recent()  # warm the link-popover cache so first open is instant
                except Exception:
                    pass
            threading.Thread(target=_warm_teams, daemon=True).start()
    except Exception:
        pass

    # Initialize PTY manager and WebSocket server
    pty_manager = PtyManager(hooks_settings_path=hooks_settings_path)
    ws_server = WebSocketServer(pty_manager, host="127.0.0.1", port=0)
    ws_server.start()

    print(f"WebSocket server running on port {ws_server.actual_port}")

    # The Hermes boundary is a separate, authenticated, read-only host service.  It
    # receives live dependencies rather than importing process globals and never shares
    # the browser WebSocket or PTY identifiers.  Missing credentials generate an
    # unreachable in-process token so the integration remains fail-closed until an
    # operator configures the environment; credentials are never printed.
    agent_gateway = None
    try:
        gateway_port = int(os.environ.get("CLAUDEMANAGER_AGENT_GATEWAY_PORT", "8787"))
        agent_gateway = AgentGateway(
            pty_manager,
            work_items,
            jira_client,
            teams_graph,
            host="127.0.0.1",
            port=gateway_port,
            hermes_token=read_hermes_token(),
            operator_token=os.environ.get("CLAUDEMANAGER_OPERATOR_CAPABILITY"),
            groups=_load_config().get("groups", []),
        )
        agent_gateway.start()
        print(f"Agent gateway running on port {agent_gateway.actual_port}")
    except Exception as exc:
        # The existing desktop app must remain usable while the agent path is disabled.
        # Do not log the token, headers, or environment values.
        print(f"Agent gateway unavailable; Hermes integration disabled: {exc}")
        agent_gateway = None

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
        if agent_gateway is not None:
            try:
                agent_gateway.stop()
            except Exception:
                pass
        try:
            ws_server.stop()
        except Exception:
            pass
        pty_manager.close_all()

    window.events.closing += on_closing

    # Start pywebview (blocks until window is closed)
    webview.start(debug=True)


if __name__ == "__main__":
    main()
