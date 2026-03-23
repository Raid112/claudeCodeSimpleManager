"""
Claude Code Launcher — Manage multiple Claude Code instances with embedded terminals.
Uses pywebview + xterm.js + pywinpty.
"""

import sys
import os

# Ensure imports work from project root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import webview
from terminal.pty_manager import PtyManager
from terminal.ws_server import WebSocketServer
from api.bridge import Bridge


def main():
    # Initialize PTY manager and WebSocket server
    pty_manager = PtyManager()
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
        pty_manager.close_all()

    window.events.closing += on_closing

    # Start pywebview (blocks until window is closed)
    webview.start(debug=False)


if __name__ == "__main__":
    main()
