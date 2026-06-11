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

There is a small test suite (no linter configured):

```bash
# Run the unit tests (plain Node, no test framework)
node tests/terminal-input.test.js
```

`tests/terminal-input.test.js` covers `TerminalInput.prepareTerminalPaste()` and `prepareComposerMessage()` from `web/js/terminal-input.js`.

## Architecture

**Multi-process design with WebSocket bridge:**

1. **main.py** — Entry point. Creates a pywebview window loading `web/index.html`, starts the WebSocket server on a background thread, and exposes the Python API bridge.

2. **api/bridge.py** — `Bridge` class exposed to JavaScript via pywebview's JS API. Handles: adding/removing project groups, opening/closing terminals, listing sessions, and persisting config to `config.json`.

3. **terminal/pty_manager.py** — `PtyManager` creates and tracks `PtySession` instances. Each session spawns a PowerShell process (with `claude` CLI) via pywinpty. Sessions are keyed by `session_id`. Builds the `claude` command line, including `--resume`/`--session-id`/`--continue` and the `--settings` hooks path.

4. **terminal/ws_server.py** — Async WebSocket server (default port 8765). Each terminal tab connects via WebSocket. Handles bidirectional I/O: browser keystrokes → PTY stdin, PTY stdout → browser display. Also handles terminal resize messages (JSON with `type: "resize"`).

5. **terminal/hook_state.py** — Authoritative per-session state store. Claude Code hooks call back into this app (`main.py --hook-notify`), which writes `{event, status, ts}` JSON to `%LOCALAPPDATA%\ClaudeManager\state\{claude_session_id}.json`. `_derive_status()` maps hook events to states: `running`, `ready`, `tooluse`, `waiting`. `get_terminals()` reads these files so the frontend poll can pick up state.

6. **terminal/hooks_settings.py** — Generates the per-session `--settings` file that wires Claude Code hooks (UserPromptSubmit/Stop/PreToolUse/PostToolUse/Notification) back to `main.py --hook-notify`.

7. **terminal/input_debug.py** — Optional input-logging helper for debugging terminal keystroke handling.

8. **web/js/app.js** — Main frontend controller. Orchestrates sidebar, tabs, and terminal instances. Polls backend every 2 seconds for session status updates (`refreshStatus`), and edge-triggers per-transition sounds/notifications via `_maybePlayStateSound`.

9. **web/js/terminal.js** — `TerminalInstance` wraps xterm.js, manages WebSocket connection per terminal, handles attach/detach lifecycle, and computes the `status` getter (reads the authoritative `backendState`).

10. **web/js/sidebar.js** — Renders project groups from `config.json`. Each group lists directories as launchable terminal entries.

11. **web/js/tabs.js** — Tab bar management for switching between active terminal sessions. `render()` applies per-state CSS classes (`status-${state}`) driven by each instance's status.

12. **web/js/composer.js** — Multi-line message composer panel that sends input to the active terminal.

13. **web/js/terminal-input.js** — Pure input-preparation helpers (`prepareTerminalPaste`, `prepareComposerMessage`); covered by `tests/terminal-input.test.js`.

**Data flow:** User input → xterm.js → WebSocket → pywinpty → Claude CLI → pywinpty → WebSocket → xterm.js

**State flow:** Claude hook event → `main.py --hook-notify` → `hook_state.write_event` → state JSON in `%LOCALAPPDATA%\ClaudeManager\state` → `get_terminals()` → 2s poll in `app.js` → tab status + sounds/toasts.

## Key constraints

- **Windows-only**: Uses pywinpty, PowerShell, VBS scripts. Not cross-platform.
- **config.json**: Stores project groups and paths. Mutated at runtime by the Bridge API.
- **web/vendor/**: Contains vendored xterm.js libraries (minified). Do not modify.
- **No framework**: Frontend is vanilla JS with no build step, no bundler, no npm.
