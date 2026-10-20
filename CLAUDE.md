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

15. **terminal/mod_bridge.py + mods/claudemanager-bridge/** — Claude Code mod path (opt-in: `config.json` → `"claude_mod": {"enabled": true, "hello_timeout_s": 30}`, app restart). A claude tab then spawns with `--plugin-dir mods/claudemanager-bridge` and **without** the `--settings` hooks; the mod translates native engine events (`turn.start` → UserPromptSubmit, `tool.call` → Pre/PostToolUse, `tool.check` ask → PermissionAsk, `turn.complete` → Stop/StopFailure, `session.end` → SessionEnd) and POSTs them to `ModBridgeServer` (loopback, `127.0.0.1:0`, one token per tab in `X-CM-Tab-Token`, injected as `CLAUDEMANAGER_BRIDGE_URL/TOKEN`). `PtyManager.on_event` feeds the same `hook_state.write_event`, so `_derive_status`, the event log and the Hermes publisher are unchanged. `get_terminals()` adds `mod_state` (`pending`/`alive`/`missing`), `state_flags` and `usage` (context, rate limits, cost). A tab whose mod never says hello shows a "mod off" badge that restarts it in hooks mode (`restart_terminal(..., mode="hooks")`). Details, status rules and debugging: section **Claude Code mod** below.

**Data flow:** User input → xterm.js → WebSocket → pywinpty → Claude CLI → pywinpty → WebSocket → xterm.js

**State flow:** Claude hook event → `main.py --hook-notify` (or, in mod mode, mod event → `ModBridgeServer` → `PtyManager.on_event`) → `hook_state.write_event` → state JSON in `%LOCALAPPDATA%\ClaudeManager\state` → `get_terminals()` → 2s poll in `app.js` → tab status + sounds/toasts.

**Work-item lifecycle:** one field, `workflow_state ∈ {active, waiting}` plus `done`. There is NO item-level "archived" — it was a second name for `waiting`, and `load_store()` migrates it (`_migrate_item_archived`). `waiting` = "I answered, nothing is expected from me until they come back": the item's tabs stay alive but leave the tab bar, and `set_waiting` writes nothing onto `session_links`. Tabs have NO state of their own: every tab of a task follows the task (active → top tab bar, waiting → listed under the task in the sidebar, where clicking one resumes the whole task; done → all its tabs are closed by `app.completeWorkItem`). The old per-tab `link.archived` ("Abas escondidas") is gone and `load_store()` drops it. Unlinked tabs (Avulso) are just open or closed. A parked Teams item unparks itself: `Bridge.poll_wake` (called from `app.js`'s 5s tick, throttled to one Graph read per 120s) feeds `teams_graph.list_recent()` into `work_items.wake_on_reply()`, which unparks any item whose chat got a message from the other side after `waiting_since` and tags it `woke_by` (sidebar badge, cleared by `ack_work_item_wake`). It rides the frontend tick instead of a background thread on purpose: `save_store` rewrites the whole file, so an unattended timer could bury a change the user just made. Every mutator in `work_items.py` is `@_serialized` on one process-wide RLock for the same reason. Jira/manual parked items have no reply signal — they stay manual-unpark.

## Key constraints

- **Windows-only**: Uses pywinpty, PowerShell, VBS scripts. Not cross-platform.
- **config.json**: Stores project groups and paths. Mutated at runtime by the Bridge API.
- **web/vendor/**: Contains vendored xterm.js libraries (minified). Do not modify.
- **No framework**: Frontend is vanilla JS with no build step, no bundler, no npm.

## Claude Code mod

**Caminho principal das abas claude** (ligado no `config.json` do Caio: `claude_mod.enabled = true`). É o que decide a cor da aba, o som, o toast e o que o Hermes recebe, então mudança aqui é de alto impacto: rode os gates e confira contra uma sessão real antes de dar por pronto. Plano: `Docs/plans/2026-10-19-ClaudeManagerMod.md`. O mod fala com o manager de dentro da sessão (estado exato, uso por aba; prompt sem PTY e aprovação pela UI ainda são plano), no lugar dos hooks `--settings` + heurística de output.

### Como o status sai

| Evento nativo (mod) | Evento enviado | Status |
|---|---|---|
| `turn.start` (loop principal) | `UserPromptSubmit` | `running` |
| `tool.call` início / fim (inclui subagente) | `PreToolUse` / `PostToolUse` | `running` |
| `tool.check` = `ask`, tool ainda parada 3s depois | `PermissionAsk` | `tooluse` (amarelo), **só se o manager confirmar** |
| `turn.complete` (loop principal) | `Stop` / `StopFailure` | `ready` |
| `session.end` | `SessionEnd` | inalterado |

- **`PermissionAsk` tem duas travas.** (1) Mod: grace de 3s (`ASK_GRACE_MS` em `register.ts`), porque `tool.check` roda antes do modo decidir. (2) Manager: `PtyManager.on_event` só aplica se a saída do PTY desde o `PreToolUse` daquele `tool_use_id` desenhou um diálogo (`shows_permission_dialog`). A trava 2 existe porque no modo `auto` (o default do Caio) o classificador segura a tool 5-30s sem diálogo nenhum: só com a grace, toda Bash ficava amarela, tocava som e acordava o Hermes. `PermissionAsk` não confirmado não muda estado nem entra no event log (o Hermes lê dali), só gera `mod.ask.unconfirmed` no ui log.
- **Limite conhecido:** o `PermissionAsk` sai uma vez só. Um diálogo que aparece depois dele (o auto desistindo e perguntando) não acende amarelo. Se isso morder, o conserto é o mod reenviar o ask enquanto ele estiver pendente.
- **Sem `waiting` (roxo) no modo mod:** não há `Notification(idle_prompt)` nativo. Com `mod_state === "alive"`, o `terminal.js` usa só o `backendState`, sem as correções por output.
- **A API não expõe o modo de permissão efetivo** nem um evento de "diálogo aberto" (o diálogo de permissão não é `RenderComponent`; `decider` só diz `host`/`person`; `ui.notify` é só de plugin). Por isso a confirmação é pela saída do PTY. Não gaste tempo procurando de novo sem um build novo.
- **O TUI separa palavras com `ESC[nG` (pula pra coluna), não com espaço.** Regex sobre a saída crua tem que trocar `ESC[nG`/`ESC[nC` por espaço antes (feito em `shows_permission_dialog`). O `_classifyIdleStatus` do frontend (abas sem mod) ainda não faz isso, e por isso não reconhece o diálogo.

### Diagnosticar status errado

1. Estado atual por aba: `%LOCALAPPDATA%\ClaudeManager\state\<tab_id>.json` (`event`, `status`, `ts`, `session_id`).
2. Sequência de eventos: `%LOCALAPPDATA%\ClaudeManager\events\events-YYYY-MM-DD-<session_key>.jsonl` (o `session_key` está no state). Imprimir `ts`, delta, `event`, `tool` das últimas linhas mostra o padrão na hora (foi assim que o bug do amarelo apareceu: `PermissionAsk` 4-12s depois de cada `PreToolUse` de Bash).
3. `%LOCALAPPDATA%\ClaudeManager\logs\ui-*.jsonl`: `mod.ask.unconfirmed`, `pty.*`, `ws.*`.
4. Pra ver o que o TUI desenha de verdade, capture um `claude` num PTY descartável: `winpty.PtyProcess.spawn("claude --permission-mode <modo>", cwd=<dir já confiado>, env=<env sem CLAUDE_CODE_*/CLAUDECODE/CLAUDEMANAGER*>)`. Detalhes que custaram tentativas: `pty.read()` bloqueia (leia numa thread); espere ~25s pelo boot; submeta com `prepare_composer_message(texto, bracketed_paste_mode=True, provider="claude")` e, se não entrar, mande `\r` depois; um dir novo trava no diálogo de trust. Um `mkdir "$TEMP/x"` força diálogo no modo `default` e passa pelo classificador no `auto`.

### Regras

- **O mod mora neste repo:** `mods/claudemanager-bridge/` (decidido; não criar em `~/.claude/dev-mods` nem em `clones/.claude/mods`). O `pty_manager` carrega ele com `--plugin-dir` no comando da aba. Append em `CLAUDE_CODE_PLUGIN_DIRS` no env **não funciona**: o `env` do `~/.claude/settings.json` sobrescreve (spike S1).
- **Lado manager:** `terminal/mod_bridge.py` (HTTP loopback com token por aba). Separado do `agent_gateway`: Hermes nunca aprova tool.
- **Exclusivo:** aba em modo mod sobe sem o `--settings` do manager (os dois juntos duplicam eventos). Flag `config.json` → `claude_mod.enabled`; desligada = caminho atual.
- **API de mods muda por build:** escrito contra 2.1.303; em 2026-10-20 a instalação já roda 2.1.304. O engine reescreve `mods/claudemanager-bridge/.claude-plugin/types/` a cada load (gitignored: inclui a lista de MCPs do usuário), então a referência da API é sempre o `index.d.ts` de lá; o `hello` do mod manda `engine_version`. Gates do mod: `claude plugin validate mods/claudemanager-bridge`, `claude plugin test mods/claudemanager-bridge`, `npx -p typescript tsc -p mods/claudemanager-bridge` (num clone novo o `tsc` só funciona depois que o engine carregou o mod uma vez). Lado manager: `python tests/mod_bridge_check.py`.
- **Teste de dentro de uma aba mod:** a sessão herda `CLAUDEMANAGER_BRIDGE_URL/TOKEN`, e dois checks do `mod_bridge_check.py` falham por isso. Rode com `env -u CLAUDEMANAGER_BRIDGE_TOKEN -u CLAUDEMANAGER_BRIDGE_URL`.
- **Mudança no lado Python só vale depois de reiniciar o app.** Se `CLAUDEMANAGER_TAB` está no env, reiniciar mata a própria sessão: peça pro Caio reiniciar. Mudança no mod vale pra abas abertas depois (ou reiniciadas).
- **Teste de ponta a ponta rodado de dentro de uma sessão Claude Code:** limpar `CLAUDE_CODE_*`/`CLAUDECODE` do env antes de spawnar, senão a aba filha herda `CLAUDE_CODE_CHILD_SESSION=1`, não grava transcript e o `--resume` falha.
- **Só eventos nativos** (`turn.*`, `tool.call`, `tool.check`, `session.*`): hooks `classic.*` não disparam nesta instalação (Claude Code gerenciado pela organização). Tradução pro vocabulário do `hook_state` na seção 4 do plano.
- Codex/opencode/powershell não mudam (seguem PTY + heurística).
