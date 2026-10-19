# ClaudeManagerMod: plano de implementação

**Objetivo:** trocar o canal de fora pra dentro (PTY + hooks `--settings` + heurística de output) por um mod do Claude Code dentro de cada aba claude, com 4 entregas: (1) estado exato, (2) prompt sem PTY, (3) aprovação de tool pela UI, (4) uso por aba.

**Build de referência:** Claude Code 2.1.303. A API de mods muda por build: os tipos ficam fixados em `mods/claudemanager-bridge/.claude-plugin/types/` e o `hello` do mod manda `$.session.version()`, pra o manager avisar quando a build mudar.

**Fora do escopo:** abas codex/opencode/powershell (seguem no PTY e na heurística). Hermes aprovando tool (nunca). Regras "sempre permitir" criadas pela UI (v1 não).

---

## 0. Pré-requisito: fronteira de commit

O working tree tem 36 arquivos modificados (+4.4k linhas, trabalho Hermes em andamento) justo em `pty_manager.py`, `bridge.py`, `app.js`, `terminal.js`, `hook_state.py`, `work_items.py`. Nada deste plano começa antes desse trabalho ser commitado (ou ir pra branch própria). Depois disso: branch `feat/claude-mod` a partir do HEAD.

---

## 1. Decisões

| # | Decisão | Por quê | Alternativa descartada |
|---|---|---|---|
| D1 | Mod → manager por `$.http.fetch` num listener HTTP novo em loopback (`terminal/mod_bridge.py`, `ThreadingHTTPServer` em `127.0.0.1:0`) | A API não tem websocket. O `agent_gateway` é a fronteira do Hermes e tem outro token e outra confiança: não misturar | Rota no `agent_gateway` (mistura confiança); escrever direto em `%LOCALAPPDATA%` via `$.fs` (duplica `hook_state` em TS) |
| D2 | Auth: token por aba, emitido no spawn, mandado no header `X-CM-Tab-Token`. A aba vem do token, nunca do body | Outro processo local não consegue falsificar outra aba. Mesmo modelo do `WsCapabilityStore`. Não usar `init.auth`: `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` recusa request de plugin com `auth` | Token global (uma aba poderia escrever o estado de outra) |
| D3 | Manager → mod por inbox que o mod puxa (`GET /v1/inbox?wait=20`, long-poll), num loop iniciado no `session.start` | O mod não escuta porta. Um `$` em voo não conta no budget de 10s, então o long-poll é barato (confirmar no S5) | Poll de 1s (fallback se o S5 falhar) |
| D4 | Identidade via `$.env.get("CLAUDEMANAGER_TAB")`, `CLAUDEMANAGER_SESSION_KEY`, `CLAUDEMANAGER_BRIDGE_URL`, `CLAUDEMANAGER_BRIDGE_TOKEN`, todos injetados por `build_child_env`. Sem essas vars, o mod não faz nada | Sessão claude fora do manager não pode ser afetada | |
| D5 | Carregamento: `build_child_env` faz **append** da pasta do mod em `CLAUDE_CODE_PLUGIN_DIRS` (separador `;` no Windows), preservando o valor do pai | O env do pai já tem `C:/Users/caioc/clones/.claude/mods/harness-band`; sobrescrever mata a banda nas abas | `--plugin-dir` no cmdline (funciona, mas não compõe com o env do usuário) |
| D6 | Aba claude em modo mod sobe **sem** o `--settings` do manager. Exclusivo: ou mod, ou hooks | `classic.*` dispara no mod com ou sem settings hook, então rodar os dois duplica cada evento (event log duplicado, Hermes publica `task_completed` duas vezes). Engolir o hook classic sem `next` também não serve: mataria os hooks globais do usuário em `~/.claude/settings.json`, que rodam como "core" na mesma chain | Manter os dois e deduplicar (frágil) |
| D7 | Fallback por aba: sem `hello` em `claude_mod.hello_timeout_s` (default 15s, ajustado pelo tempo medido no S1 com `--resume` de transcript grande) após o spawn, a aba fica `mod_state: "missing"`, o status volta pra heurística de output e a UI mostra o badge "mod off" com o botão "reiniciar em modo hooks" (`restart_session` com `--settings`) | Build nova pode quebrar o mod. A aba não pode ficar cega | |
| D8 | Flag global `config.json` → `"claude_mod": {"enabled": false}`. Começa desligada, liga depois dos spikes. Desligada = caminho atual intacto | Rollback = mudar a flag | |
| D9 | O mod repassa os eventos com o **mesmo vocabulário** do `--hook-notify` (`{event, session_id, tool_name, ts}`), e o manager chama o mesmo `hook_state.write_event` | `_derive_status`, `work_items.log_event` e `hermes_events.normalize_event` continuam funcionando. Só entram eventos novos | Vocabulário novo (reescreve 3 consumidores) |
| D10 | Mod mora no repo: `ClaudeManager/mods/claudemanager-bridge/` | `pty_manager` aponta pra ele; versiona junto com o protocolo | `~/.claude/dev-mods` (sessão), `clones/.claude/mods` (outro repo) |
| D11 | Falha do mod é sempre fail-open: erro de fetch ou manager fora → `next(e)`, comportamento padrão do Claude Code. Nunca `deny` por erro | Manager caído não pode negar todas as tools | `.catch(() => ({decision:"deny"}))` da doc (serve pra guard de segurança, não pra conveniência) |
| D12 | Aprovação só por clique humano via `Bridge` (pywebview JS API). Nenhuma rota do `agent_gateway` toca permissão | Invariante de segurança: Hermes propõe prompt, nunca aprova tool | |

---

## 2. Fase 0: spikes (bloqueiam o resto)

Mod descartável `cm-spike` + servidor eco em Python (`scratchpad/spike_server.py`, loga cada request com timestamp). Rodar dentro de uma aba do ClaudeManager e também num `claude` puro. Resultado de cada spike vai na seção 8 deste arquivo.

| Spike | Pergunta | Passa se | Se falhar |
|---|---|---|---|
| S1 | O mod carrega via `CLAUDE_CODE_PLUGIN_DIRS` appendado no env do PTY, sem prompt de hot-reload travando a CLI, e o harness-band continua? Tempo até o `hello` em sessão nova e em `--resume` de transcript grande? | Os dois mods aparecem, a aba sobe limpa, tempos anotados | `--plugin-dir` no cmdline |
| S2 | `$.env.get` lê `CLAUDEMANAGER_TAB`? `$.http.fetch("http://127.0.0.1:<port>")` com header custom passa pela política de web-fetch? Latência? | Lê a var; fetch < 20ms | Sem fetch não tem projeto: cair pra `$.fs.write` num spool dir que o manager observa |
| S3 | Paridade de eventos sem `--settings`: `classic.UserPromptSubmit/PreToolUse/PostToolUse/Stop/Notification/PermissionRequest/SessionStart/StopFailure` disparam? Quais valores de `notification_type`? `SessionStart.source` em `/clear` e `/resume`? Subagente vem com `agent_id`? | Todos disparam; tabela de valores anotada | Usar `tool.check` + `turn.*` pros que faltarem |
| S4 | Com um hook em `classic.PermissionRequest` esperando um fetch de 30s: o diálogo do terminal aparece durante a espera? Responder no terminal aborta o hook (`next.signal`)? Uma decisão `allow` do hook fecha o diálogo? Idem em `tool.check` | Tabela de comportamento anotada | Decide a variante do item 3 (seção 6) |
| S5 | Long-poll de 20s dentro de um timer `$.clock.after` encadeado: o budget de 10s não estoura? O que acontece no reload do módulo (timer some, loop recomeça no `session.start`)? | 10 ciclos sem a dim line de hook falho | Poll de 1s com `$.clock.every` |
| S6 | `$.prompt.submit`: fidelidade com multi-linha, unicode e texto de 20KB; enfileira com a sessão ocupada; resolve no início do turno; o que o modelo lê de `origin` (aparece "plugin claudemanager"?); comportamento durante diálogo de permissão; slash command no texto; um `$.prompt.submit` aguardado dentro de `$.clock.after(0)` sobrevive ao fim do dispatch do loop? | Texto chega idêntico; origin anotado; submit não é abortado | Composer: `$.prompt.fill` + `\r` via PTY |
| S7 | `$.session.usage()`: shape real de `context`, `rateLimits` e `cost`; frequência do `session.measure` | Campos anotados | |

Saída da fase: seção 8 preenchida + decisão do item 3. **Gate:** sem S1, S2 e S3 verdes, o plano para aqui.

---

## 3. Fase 1: transporte (base pros 4 itens)

### T1.1 `terminal/mod_bridge.py`
- `TabTokenStore`: `issue(tab_id) -> token`, `resolve(token) -> tab_id | None`, `revoke(tab_id)`. Token `secrets.token_urlsafe(32)`, comparado com `hmac.compare_digest`, nunca logado.
- `ModBridgeServer(pty_manager, sink)`: `ThreadingHTTPServer` em `127.0.0.1:0`, body JSON ≤ 64KB, 401 sem token válido, 404 em rota desconhecida.
- Rotas v1 (todas com `X-CM-Tab-Token`):

| Rota | Body / resposta | Efeito |
|---|---|---|
| `POST /v1/hello` | `{session_id, source, plugin_version, engine_version, cwd}` | `mod_state = "alive"`, guarda as versões e avisa se mudou a build |
| `POST /v1/event` | `{seq, event, session_id, tool_name?, notification_type?, source?, agent_id?, tool_use_id?, ts}` | `hook_state.write_event(...)` com tab e session_key derivados do token; `seq` descarta duplicata e fora de ordem |
| `POST /v1/usage` | `{context, rateLimits, cost?, startedAt, ts}` | Dict em memória `tab → usage` |
| `GET /v1/inbox?wait=20` | → `{items: [...]}` | Long-poll: devolve na hora se tem item, senão segura até `wait` |
| `POST /v1/ack` | `{id, state: "claimed" \| "started" \| "failed", reason?}` | Atualiza o recibo do item |
| `POST /v1/permission` | `{tool_use_id, tool, summary}` → `{decision?}` | Só no item 3 (seção 6) |

- Testes: `tests/mod_bridge_check.py` (token errado → 401, token da aba A não escreve na aba B, `seq` duplicado ignorado, long-poll devolve item que chega no meio da espera, body grande → 413).

### T1.2 Spawn
- `build_child_env(base, tab_id, session_key, bridge=None)`: com `bridge`, adiciona `CLAUDEMANAGER_BRIDGE_URL`, `CLAUDEMANAGER_BRIDGE_TOKEN` e faz append de `MOD_DIR` em `CLAUDE_CODE_PLUGIN_DIRS` (`;`, sem duplicar).
- `build_resume_command`: em modo mod, `hooks_path=None` (D6).
- `PtySession.restart`: re-emite o token (processo novo, env novo) e revoga o anterior. `close_session` revoga.
- `PtySession.mode ∈ {"mod", "hooks"}` por aba: hoje o `restart` reusa `self.hooks_settings_path` sempre. Com o modo por aba, o "reiniciar em modo hooks" do D7 é `restart(mode="hooks")`.
- `PtyManager`: `mod_state` por aba (`pending` no spawn → `alive` no hello → `missing` depois de 15s); entra no `get_all_sessions` como `mod_state`.
- `main.py`: sobe o `ModBridgeServer` antes do `PtyManager` quando `claude_mod.enabled`; se falhar ao subir, segue no modo hooks e loga.
- Testes: atualizar `tests/pty_env_check.py` (vars novas, append preserva valor do pai, sem bridge = env igual ao de hoje), `tests/session_support_check.py` (sem `--settings` em modo mod), `tests/pty_manager_check.py` (transições do `mod_state`, revoke no restart/close).

### T1.3 Esqueleto do mod `mods/claudemanager-bridge/`
- `.claude-plugin/plugin.json`, `hooks/hooks.json`, `hooks/register.ts`, `hooks/client.ts` (fetch com header, timeout 2s, fila serial com `seq`, erro engolido = fail-open), `types/index.d.ts` (`PluginState['claudemanager-bridge']`: `seq`, `submitted: string[]`).
- `session.start`: lê as env vars; sem elas, retorna `next(e)` e não registra mais nada de efeito. Com elas, manda `hello` e inicia o loop do inbox.
- Gates: `claude plugin validate`, `tsc -p`, `claude plugin test` (o `$.http.fetch` e o `$.env.get` são eventos, então o teste responde por baixo do plugin: `on('http.fetch', ...)`, `on('env.get', ...)`).

---

## 4. Fase 2: item 1 (estado exato)

### T2.1 Mod
Hooks em `classic.UserPromptSubmit`, `classic.PreToolUse`, `classic.PostToolUse`, `classic.Stop`, `classic.StopFailure`, `classic.Notification`, `classic.PermissionRequest`, `classic.SessionStart`. Cada um faz `await next(e)` primeiro (não altera o comportamento), depois `POST /v1/event`. Sem input de tool nem texto de prompt no payload (mantém a regra de privacidade do `log_event`).

### T2.2 `hook_state._derive_status`
| Evento | Hoje | Novo |
|---|---|---|
| `PermissionRequest` | não existe | `tooluse` (exato) |
| `Notification` com `notification_type` | ambíguo, decidido pelo evento anterior | `permission_prompt` → `tooluse`; `idle_prompt` → `waiting`; outro → regra antiga |
| `StopFailure` | não existe (aba presa em `running` quando a API falha) | `ready` + `error: true` no state |
| `SessionStart` | ignorado | `startup`/`clear`/`resume`/`fork` → `ready` + `source` (o `session_id` novo já entra no state e o `get_all_sessions` segue a troca; `fork` também troca o id). `compact` → `None` (dispara no meio do turno; mudar pra `ready` deixaria idle uma aba ocupada) |

`write_event` ganha `notification_type`, `source`, `agent_id` e `error` opcionais. O caminho `--hook-notify` não muda (não manda esses campos e cai na regra antiga).

### T2.3 `work_items.log_event` + `hermes_events.normalize_event`
- O log grava `notification_type` (metadado, sem texto).
- `PermissionRequest` → `permission_requested`. `Notification(permission_prompt)` com evento anterior `PermissionRequest` → só log, sem publicar (senão publica duas vezes). `StopFailure` → `task_failed` (evento novo, publicado) ou só log: decidir com o contrato do Hermes, e o default é só log.

### T2.4 Frontend
- `terminal.js status`: com `mod_state === "alive"`, o `backendState` vale sozinho (sem o desvio "waiting + output recente = running", que era remendo da ambiguidade). Badge "mod off" quando `missing`.
- `CLAUDE.md`: atualizar as seções 3, 5 e 6, a linha "State flow" e adicionar `mod_bridge.py` + `mods/claudemanager-bridge/`.
- Testes: `tests/hook_state_check.py` (tabela acima, `compact` sem mudança de status), `tests/hermes_events_check.py` (sem publicação dupla), teste do mod que cobre cada `classic.*` virando o POST esperado.

---

## 5. Fase 3: item 4 (uso por aba)

- Mod: `POST /v1/usage` no `turn.complete` e no `session.measure`, com throttle de 1 por 5s. `$.session.usage()` sem args é grátis.
- Manager: `get_all_sessions` inclui `usage`. UI: badge de contexto na aba (verde < 60%, amarelo < 85%, vermelho acima), tooltip com custo. Barra global com 5h/semana (o rate limit é da conta, então vale o mais recente entre as abas).
- O `cacheRemainingMs` do `terminal.js` passa a contar do `ts` real do último `UserPromptSubmit` em vez do envio pelo composer (pega prompt digitado direto no terminal).
- Testes: shape do payload com `rateLimits` vazio (fora de assinatura) e sem `cost`.

---

## 6. Fase 4: item 2 (prompt sem PTY)

### T4.1 Inbox no manager
- `PromptInbox` por aba: item `{id, kind: "prompt", text, source: "composer" | "hermes", request_id?, decision_id?}`. Estados: `queued` → `claimed` (mod pegou) → `started` (o `$.prompt.submit` resolveu = turno começou) | `failed`.
- Re-entrega: nunca automática. `claimed` sem `started` depois de 120s → `unknown` (mesma semântica do `EXECUTION_UNKNOWN` de hoje).

### T4.2 Mod
- O loop do inbox, para cada item: grava `id` em `$.state.submitted` (session-scope, sobrevive ao reload), manda `ack claimed` e agenda o submit num dispatch próprio (`$.clock.after(0, ...)`), que faz `await $.prompt.submit({ text })` e manda `ack started`. O loop nunca espera o submit: numa sessão ocupada ele só resolve quando o turno começa (minutos) e travaria o inbox inteiro, decisões de permissão incluídas. Um `id` que já está em `submitted` recebe só o ack e não é reenviado (cobre perda do worker).

### T4.3 Quem usa
- **Composer** (`composer.js _send`): aba claude com `mod_state === "alive"` e texto que não começa com `/` → `Bridge.submit_prompt(tab_id, text)`. Slash command e abas sem mod seguem no PTY. O bloqueio `tooluse` vira estado visível "na fila (1)": o prompt espera o turno livre, nunca entra no meio de um turno rodando. **Muda o comportamento:** hoje, digitar com a aba ocupada cai na fila de input da CLI.
- **Hermes** (`pty_manager.send_prompt` / `send_owned_prompt`): aba claude com mod → enfileira no inbox em vez do `session.write`. O recibo `EXECUTION_SENT` passa a significar "mod aceitou", e `started` adiciona `provider_processed: true` (hoje é sempre `false`). Ganho real: o Hermes passa a saber que o turno começou. A idempotência do `agent_decisions` não muda: `request_id` = `id` do item.
- **Origin:** depende do S6. Se o modelo lê "plugin claudemanager", o Hermes mantém (é proveniência), e o composer vai pro fallback `$.prompt.fill` + `\r` (texto exato na caixa, Enter via PTY).
- `prepare_composer_message` / bracketed paste continuam só pra codex/opencode e pro fallback.

### T4.4 Testes
`tests/prompt_inbox_check.py` (estados, sem re-entrega, timeout → unknown), `tests/approval_flow_check.py` (send_prompt via inbox mantém hash e idempotência), teste do mod (item duplicado não submete duas vezes, falha do submit → `ack failed`), `tests/terminal-input.test.js` intacto.

---

## 7. Fase 5: item 3 (aprovar tool pela UI)

Restrição dura: budget de 10s por dispatch. Um `$` em voo não conta, mas o que manda é o que o S4 mostrar sobre o diálogo.

| Variante | Como | Quando usar |
|---|---|---|
| **A: corrida** | `classic.PermissionRequest` faz long-poll em `/v1/permission`. O diálogo do terminal aparece ao mesmo tempo, e quem responder primeiro ganha; resposta no terminal aborta o hook via `next.signal` | S4: o diálogo renderiza durante a espera e o `allow` do hook fecha o diálogo |
| **B: janela curta** | `tool.check` espera ≤ 8s por decisão no inbox; sem decisão → `next(e)` e o diálogo aparece | S4: o diálogo só aparece depois que o hook resolve |
| **C: detecção + tecla** | Sem esperar no hook. `PermissionRequest` marca `tooluse` exato com `tool_use_id`; os botões da UI escrevem a tecla do diálogo no PTY | Sempre funciona; fallback de A e B |

**Recomendação:** A se o S4 confirmar a corrida (melhor UX, sem custo pra quem olha o terminal). Senão C, não B: em B, quem olha o terminal espera 8s por nada.

Em qualquer variante:
- Card na aba: tool, resumo curto (Bash: primeiros 200 chars do comando; Edit/Write: path; MCP: nome) só em memória, nunca persistido. Botões **Permitir uma vez** / **Negar** / **Responder no terminal**.
- A decisão vale só pro `tool_use_id` exato; card velho (o tool_use já passou) some sozinho no `PostToolUse` / `Stop`.
- Sem `updatedPermissions` (sem "sempre permitir") na v1.
- D11 fail-open e D12 só humano. Teste que prova que nenhuma rota do `agent_gateway` chega em `/v1/permission` nem no `Bridge.decide_permission`.
- Som e notificação: `_maybePlayStateSound` já dispara em `tooluse`; agora dispara no instante certo.

---

## 8. Resultados dos spikes

_(preencher na Fase 0)_

| Spike | Resultado | Decisão tomada |
|---|---|---|
| S1 | | |
| S2 | | |
| S3 | | |
| S4 | | |
| S5 | | |
| S6 | | |
| S7 | | |

---

## 9. Ordem, gates e rollback

1. Fase 0 (spikes) → gate S1/S2/S3.
2. Fase 1 + Fase 2 juntas (o transporte sem consumidor não se prova) → liga `claude_mod.enabled` só numa aba de teste. Critério: uma semana sem aba presa em estado errado; o Hermes recebe o mesmo número de `task_completed` que antes.
3. Fase 3 (barata, mesmo transporte).
4. Fase 4.
5. Fase 5 (variante definida pelo S4).
6. Opcional depois: push em vez do poll de 2s (`window.evaluate_js` direto quando chega um evento), pra transição instantânea.

Gates por tarefa: manager → todos os `tests/*_check.py` e `tests/*.test.js` passando; mod → `claude plugin validate` + `tsc -p` + `claude plugin test`. Smoke manual no fim de cada fase: abrir aba, prompt, tool com permissão, `/clear`, `/resume`, restart da aba, fechar a aba.

Rollback: `claude_mod.enabled = false` volta tudo pro caminho atual (hooks `--settings` + PTY). Por aba: badge "mod off" → "reiniciar em modo hooks".

## 10. Riscos

| Risco | Mitigação |
|---|---|
| Build nova do Claude Code quebra a API | `hello` manda `engine_version`; a UI avisa quando ela muda; D7 fallback por aba; tipos fixados |
| Manager reinicia e as abas antigas apontam pra porta morta | As abas morrem com o app (são filhas do PTY). Abas restauradas recebem env novo no spawn |
| Mod trava a sessão | Todo hook faz `next(e)` primeiro e só depois o fetch, com timeout de 2s e erro engolido. Nenhum hook decide nada fora do item 3 |
| Prompt duplicado | `$.state.submitted` + nunca re-entregar automaticamente |
| Vazamento de dado de tool | Payload sem input/prompt; resumo de permissão só em memória e truncado |
| Abas dobram o trabalho do watch de mods do usuário | Medir no S1: custo de carregar 2 mods em N abas |
