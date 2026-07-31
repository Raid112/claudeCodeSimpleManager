# Claude Code Simple Manager

A lightweight Windows desktop app for running multiple [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI sessions side by side in a tabbed terminal interface.

![Python](https://img.shields.io/badge/python-3.10+-blue)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)

## What it does

- **Multi-terminal tabs** — Open several Claude Code sessions at once, each in its own xterm.js terminal
- **Project groups** — Organize projects into groups via the sidebar; each group launches Claude in that directory
- **Live status indicators** — See at a glance which terminals are running, idle (ready), waiting for tool-use approval, or stopped
- **Audio notifications** — Hear a chime when Claude finishes responding or needs tool approval
- **Copy/Paste** — Ctrl+C (with selection) to copy, Ctrl+V to paste, just like a normal terminal
- **Scroll-to-bottom button** — Appears when you scroll up; click to jump back to latest output

## Prerequisites

- **Windows 10/11**
- **Python 3.10+** (added to PATH)
- **Claude Code CLI** installed and authenticated (`npm install -g @anthropic-ai/claude-code`)

## Installation

```bash
git clone https://github.com/Raid112/claudeCodeSimpleManager.git
cd claudeCodeSimpleManager
pip install -r requirements.txt
```

## Usage

```bash
python main.py
```

Or double-click `ClaudeCodeLauncher.vbs` to launch without a console window.

### Desktop shortcut

Run `create_shortcut.vbs` to create a desktop shortcut with the app icon. You can then pin it to the taskbar.

## Architecture

```
User input → xterm.js → WebSocket → pywinpty → Claude CLI → pywinpty → WebSocket → xterm.js
```

| Component | Role |
|---|---|
| `main.py` | Entry point — creates pywebview window + starts WebSocket server |
| `api/bridge.py` | Python↔JS bridge — manages groups, terminals, config |
| `terminal/pty_manager.py` | Spawns and manages pywinpty PTY sessions |
| `terminal/ws_server.py` | Async WebSocket server bridging PTY I/O to the browser |
| `web/js/app.js` | Main frontend controller |
| `web/js/terminal.js` | xterm.js wrapper with status detection and audio |
| `web/js/sidebar.js` | Project group sidebar |
| `web/js/tabs.js` | Tab bar management |

## Configuration

Project groups are saved in `config.json` (created automatically on first use). Example:

```json
{
  "groups": [
    { "name": "my-project", "path": "C:/Users/you/projects/my-project" }
  ]
}
```

## Hermes Orchestrator (local MVP)

ClaudeManager can host the native Hermes CLI/Desktop coordinator through the
authenticated stdio MCP adapter in `integrations/hermes/mcp_server.py`. Hermes
receives only redacted context and semantic tools: it must submit a proposal and
wait for approval in the ClaudeManager UI before opening a session or sending the
exact approved prompt.

Setup and the shared CLI/Desktop configuration are documented in:

- [`integrations/hermes/README.md`](integrations/hermes/README.md)
- [`integrations/hermes/claudemanager-orchestrator.md`](integrations/hermes/claudemanager-orchestrator.md)
- [`integrations/hermes/config.example.yaml`](integrations/hermes/config.example.yaml)

The gateway stays on `127.0.0.1`; tokens are supplied through the environment or
a protected token file. The adapter never exposes PTY IDs, paths, WebSocket
access, PowerShell, or operator capabilities. WSL/VPS, public listeners, Docker
as the main path, and external chat channels are outside this MVP.

## License

MIT
