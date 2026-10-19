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

**Desktop shortcut:** `Claude Code Launcher.lnk` (área de trabalho) → `wscript.exe ClaudeCodeLauncher.vbs` → `python main.py` (workdir = repo root). O `.vbs` roda o código-fonte direto, **não** um `.exe` buildado — então mudanças em `web/` e `terminal/` aparecem só reiniciando o app pelo atalho (não precisa rebuild). Frontend (`web/`) é servido do disco: basta reabrir. Backend (`.py`): fechar e reabrir o app.

There is a small test suite (no linter configured):

```bash
# Run the unit tests (plain Node / plain python, no test framework)
node tests/terminal-input.test.js
node tests/teams-search.test.js
node tests/uilog.test.js
python tests/work_items_check.py
python tests/ui_log_check.py
# ...and the rest: every tests/*_check.py (python) and tests/*.test.js (node) is standalone
```

`tests/terminal-input.test.js` covers `TerminalInput.prepareTerminalPaste()` and `prepareComposerMessage()` from `web/js/terminal-input.js`.

`tests/teams-search.test.js` covers the message-picker helpers in `web/js/workitems.js` (search normalization, time labels, person aggregation, snippet) plus a stubbed-DOM smoke test of `_rows()` on the Mensagens segment.

`tests/uilog.test.js` covers the `UiLog` buffer in `web/js/uilog.js` (nothing is lost before the bridge exists, a failed flush is re-queued in order).

## Architecture

**Multi-process design with WebSocket bridge:**

1. **main.py** — Entry point. Creates a pywebview window loading `web/index.html`, starts the WebSocket server on a background thread, and exposes the Python API bridge.

2. **api/bridge.py** — `Bridge` class exposed to JavaScript via pywebview's JS API. Handles: adding/removing project groups, opening/closing terminals, listing sessions, and persisting config to `config.json`.

3. **terminal/pty_manager.py** — `PtyManager` creates and tracks `PtySession` instances. Each session spawns a PowerShell process (with `claude` CLI) via pywinpty. Sessions are keyed by `session_id`. Builds the `claude` command line, including `--resume`/`--session-id`/`--continue` and the `--settings` hooks path.

4. **terminal/ws_server.py** — Async WebSocket server (default port 8765). Each terminal tab connects via WebSocket. Handles bidirectional I/O: browser keystrokes → PTY stdin, PTY stdout → browser display. Also handles terminal resize messages (JSON with `type: "resize"`).

5. **terminal/hook_state.py** — Authoritative per-session state store. Claude Code hooks call back into this app (`main.py --hook-notify`), which writes `{event, status, ts, session_id}` JSON to `%LOCALAPPDATA%\ClaudeManager\state\{tab_id}.json`. State is keyed by **tab_id** (the managed pty tab, passed to the hook via the `CLAUDEMANAGER_TAB` env var injected at spawn), NOT by session_id — because the user can swap the claude session_id under the same tab via `/clear` or `/resume`. The current session_id rides along as a field; `PtyManager.get_all_sessions()` follows the switch (reconciles `session.claude_session_id` + migrates the work-item link). `_derive_status()` maps hook events to states: `running`, `ready`, `tooluse`, `waiting`. `get_terminals()` reads these files so the frontend poll can pick up state.

6. **terminal/hooks_settings.py** — Generates the per-session `--settings` file that wires Claude Code hooks (UserPromptSubmit/Stop/PreToolUse/PostToolUse/Notification) back to `main.py --hook-notify`.

7. **terminal/input_debug.py** — Optional input-logging helper for debugging terminal keystroke handling.

8. **web/js/app.js** — Main frontend controller. Orchestrates sidebar, tabs, and terminal instances. Polls backend every 2 seconds for session status updates (`refreshStatus`), and edge-triggers per-transition sounds/notifications via `_maybePlayStateSound`.

9. **web/js/terminal.js** — `TerminalInstance` wraps xterm.js, manages WebSocket connection per terminal, handles attach/detach lifecycle, and computes the `status` getter (reads the authoritative `backendState`).

10. **web/js/sidebar.js** — Renders project groups from `config.json`. Each group lists directories as launchable terminal entries.

11. **web/js/tabs.js** — Tab bar management for switching between active terminal sessions. `render()` applies per-state CSS classes (`status-${state}`) driven by each instance's status.

12. **web/js/composer.js** — Multi-line message composer panel that sends input to the active terminal.

13. **web/js/terminal-input.js** — Pure input-preparation helpers (`prepareTerminalPaste`, `prepareComposerMessage`); covered by `tests/terminal-input.test.js`.

14. **terminal/ui_log.py + web/js/uilog.js** — Diagnostics log at `%LOCALAPPDATA%\ClaudeManager\logs\ui-YYYY-MM-DD.jsonl` (7 days). The frontend batches events (2s/50) through `Bridge.ui_log`; `PtySession.resize` writes from the Python side into the same file. Logged: `fit`/`fit.suspect` (geometry, container box, active/hidden), `resize.sent`/`resize.dropped`, `pty.resize`/`pty.resize.failed`, `ws.*`, `js.error`, `js.rejection`, window focus/visibility. This is the trail for the "terminal layout breaks until I switch tabs" bug. DevTools no longer opens on launch (set `CLAUDEMANAGER_DEBUG=1` for that); the 🩺 logs button in the title bar opens the folder.

**Data flow:** User input → xterm.js → WebSocket → pywinpty → Claude CLI → pywinpty → WebSocket → xterm.js

**State flow:** Claude hook event → `main.py --hook-notify` → `hook_state.write_event` → state JSON in `%LOCALAPPDATA%\ClaudeManager\state` → `get_terminals()` → 2s poll in `app.js` → tab status + sounds/toasts.

**Work-item lifecycle:** one field, `workflow_state ∈ {active, waiting}` plus `done`. There is NO item-level "archived" — it was a second name for `waiting`, and `load_store()` migrates it (`_migrate_item_archived`). `waiting` = "I answered, nothing is expected from me until they come back": the item's tabs stay alive but leave the tab bar, and `set_waiting` writes nothing onto `session_links`. Tabs have NO state of their own: every tab of a task follows the task (active → top tab bar, waiting → listed under the task in the sidebar, where clicking one resumes the whole task; done → all its tabs are closed by `app.completeWorkItem`). The old per-tab `link.archived` ("Abas escondidas") is gone and `load_store()` drops it. Unlinked tabs (Avulso) are just open or closed. A parked Teams item unparks itself: `Bridge.poll_wake` (called from `app.js`'s 5s tick, throttled to one Graph read per 120s) feeds `teams_graph.list_recent()` into `work_items.wake_on_reply()`, which unparks any item whose chat got a message from the other side after `waiting_since` and tags it `woke_by` (sidebar badge, cleared by `ack_work_item_wake`). It rides the frontend tick instead of a background thread on purpose: `save_store` rewrites the whole file, so an unattended timer could bury a change the user just made. Every mutator in `work_items.py` is `@_serialized` on one process-wide RLock for the same reason. Jira/manual parked items have no reply signal — they stay manual-unpark.

## Key constraints

- **Windows-only**: Uses pywinpty, PowerShell, VBS scripts. Not cross-platform.
- **config.json**: Stores project groups and paths. Mutated at runtime by the Bridge API.
- **web/vendor/**: Contains vendored xterm.js libraries (minified). Do not modify.
- **No framework**: Frontend is vanilla JS with no build step, no bundler, no npm.

## Claude Code mod (em andamento)

Plano: `Docs/plans/2026-10-19-ClaudeManagerMod.md`. Abas claude passam a carregar um mod do Claude Code que fala com o manager de dentro da sessão (estado exato, prompt sem PTY, aprovação de tool pela UI, uso por aba), no lugar dos hooks `--settings` + heurística de output.

- **O mod mora neste repo:** `mods/claudemanager-bridge/` (decidido; não criar em `~/.claude/dev-mods` nem em `clones/.claude/mods`). O `pty_manager` aponta pra ele fazendo append em `CLAUDE_CODE_PLUGIN_DIRS` no env da aba, sem sobrescrever o valor do pai (o usuário já carrega o `harness-band` por ali).
- **Lado manager:** `terminal/mod_bridge.py` (HTTP loopback com token por aba). Separado do `agent_gateway`: Hermes nunca aprova tool.
- **Exclusivo:** aba em modo mod sobe sem o `--settings` do manager (os dois juntos duplicam eventos). Flag `config.json` → `claude_mod.enabled`; desligada = caminho atual.
- **API de mods muda por build:** referência 2.1.303; tipos fixados em `mods/claudemanager-bridge/.claude-plugin/types/`. Gates do mod: `claude plugin validate`, `tsc -p`, `claude plugin test`.
- Codex/opencode/powershell não mudam (seguem PTY + heurística).
