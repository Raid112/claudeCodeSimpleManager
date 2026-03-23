# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is this project?

ClaudeManager is a Windows desktop application that provides a multi-terminal GUI for running Claude Code CLI sessions. Built with Python (pywebview) backend and vanilla HTML/CSS/JS frontend, using xterm.js for terminal emulation.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the application
python main.py

# Build executable with PyInstaller
pyinstaller ClaudeCodeLauncher.spec
```

There is no test suite or linter configured.

## Architecture

**Multi-process design with WebSocket bridge:**

1. **main.py** — Entry point. Creates a pywebview window loading `web/index.html`, starts the WebSocket server on a background thread, and exposes the Python API bridge.

2. **api/bridge.py** — `Bridge` class exposed to JavaScript via pywebview's JS API. Handles: adding/removing project groups, opening/closing terminals, listing sessions, and persisting config to `config.json`.

3. **terminal/pty_manager.py** — `PtyManager` creates and tracks `PtySession` instances. Each session spawns a PowerShell process (with `claude` CLI) via pywinpty. Sessions are keyed by `session_id`.

4. **terminal/ws_server.py** — Async WebSocket server (default port 8765). Each terminal tab connects via WebSocket. Handles bidirectional I/O: browser keystrokes → PTY stdin, PTY stdout → browser display. Also handles terminal resize messages (JSON with `type: "resize"`).

5. **web/js/app.js** — Main frontend controller. Orchestrates sidebar, tabs, and terminal instances. Polls backend every 2 seconds for session status updates.

6. **web/js/terminal.js** — `TerminalInstance` wraps xterm.js, manages WebSocket connection per terminal, and handles attach/detach lifecycle.

7. **web/js/sidebar.js** — Renders project groups from `config.json`. Each group lists directories as launchable terminal entries.

8. **web/js/tabs.js** — Tab bar management for switching between active terminal sessions.

**Data flow:** User input → xterm.js → WebSocket → pywinpty → Claude CLI → pywinpty → WebSocket → xterm.js

## Key constraints

- **Windows-only**: Uses pywinpty, PowerShell, VBS scripts. Not cross-platform.
- **config.json**: Stores project groups and paths. Mutated at runtime by the Bridge API.
- **web/vendor/**: Contains vendored xterm.js libraries (minified). Do not modify.
- **No framework**: Frontend is vanilla JS with no build step, no bundler, no npm.
