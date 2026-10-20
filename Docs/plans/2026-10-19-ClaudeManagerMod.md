# ClaudeManagerMod: plano de implementação

**Objetivo:** trocar o canal de fora pra dentro (PTY + hooks `--settings` + heurística de output) por um mod do Claude Code dentro de cada aba claude, com 4 entregas: (1) estado exato, (2) prompt sem PTY, (3) aprovação de tool pela UI, (4) uso por aba.

**Build de referência:** Claude Code 2.1.303. A API de mods muda por build: o engine reescreve `mods/claudemanager-bridge/.claude-plugin/types/` a cada load (gitignored, porque inclui os MCPs do usuário), e o `hello` do mod manda `$.session.version()`, pra o manager avisar quando a build mudar.

**Fora do escopo:** abas codex/opencode/powershell (seguem no PTY e na heurística). Hermes aprovando tool (nunca). Regras "sempre permitir" criadas pela UI (v1 não).

---

## 0. Pré-requisito: fronteira de commit

Feito em 2026-10-19: o WIP virou 11 commits (`6e1db18`..`b4e8cbd`). Implementação em branch `feat/claude-mod` a partir do HEAD.

> **Revisão pós-spike (2026-10-19):** D5, D6, D7, D9 e as seções 4, 6 e 7 foram reescritas com os resultados da seção 8. O que mudou: os hooks `classic.*` **não disparam nesta instalação** (Claude Code gerenciado pela organização), então o mod usa só eventos nativos. O mod carrega por `--plugin-dir`, e o env não serve. O diálogo de permissão só aparece depois que o `tool.check` resolve. Prompt de plugin aparece rotulado pro modelo e é descartado depois de um Esc.

---

## 1. Decisões

| # | Decisão | Por quê | Alternativa descartada |
|---|---|---|---|
| D1 | Mod → manager por `$.http.fetch` num listener HTTP novo em loopback (`terminal/mod_bridge.py`, `ThreadingHTTPServer` em `127.0.0.1:0`) | A API não tem websocket. O `agent_gateway` é a fronteira do Hermes e tem outro token e outra confiança: não misturar | Rota no `agent_gateway` (mistura confiança); escrever direto em `%LOCALAPPDATA%` via `$.fs` (duplica `hook_state` em TS) |
| D2 | Auth: token por aba, emitido no spawn, mandado no header `X-CM-Tab-Token`. A aba vem do token, nunca do body | Outro processo local não consegue falsificar outra aba. Mesmo modelo do `WsCapabilityStore`. Não usar `init.auth`: `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` recusa request de plugin com `auth` | Token global (uma aba poderia escrever o estado de outra) |
| D3 | Manager → mod por inbox que o mod puxa (`GET /v1/inbox?wait=20`, long-poll), num loop iniciado no `session.start` | O mod não escuta porta. Um `$` em voo não conta no budget de 10s, então o long-poll é barato (confirmar no S5) | Poll de 1s (fallback se o S5 falhar) |
| D4 | Identidade via `$.env.get("CLAUDEMANAGER_TAB")`, `CLAUDEMANAGER_SESSION_KEY`, `CLAUDEMANAGER_BRIDGE_URL`, `CLAUDEMANAGER_BRIDGE_TOKEN`, todos injetados por `build_child_env`. Sem essas vars, o mod não faz nada | Sessão claude fora do manager não pode ser afetada | |
| D5 | Carregamento: `build_resume_command` adiciona `--plugin-dir "<repo>/mods/claudemanager-bridge"` no comando da aba claude | S1: o `CLAUDE_CODE_PLUGIN_DIRS` do usuário vem do bloco `env` do `~/.claude/settings.json`, que sobrescreve o valor passado no env do PTY (o mod não carregou). Com `--plugin-dir`, o mod carrega e o harness-band continua | Append em `CLAUDE_CODE_PLUGIN_DIRS` no env (refutado no S1) |
| D6 | Aba claude em modo mod sobe **sem** o `--settings` do manager. Exclusivo: ou mod, ou hooks | Os command hooks do `--settings` rodam (são hooks de settings, não de plugin), então rodar os dois duplica cada evento (event log duplicado, Hermes publica `task_completed` duas vezes) | Manter os dois e deduplicar (frágil) |
| D7 | Fallback por aba: sem `hello` em `claude_mod.hello_timeout_s` (default 30s; S1 mediu 8.4s em sessão nova, `--resume` não foi medido) após o spawn, a aba fica `mod_state: "missing"`, o status volta pra heurística de output e a UI mostra o badge "mod off" com o botão "reiniciar em modo hooks" (`restart_session` com `--settings`) | Build nova pode quebrar o mod. A aba não pode ficar cega | |
| D8 | Flag global `config.json` → `"claude_mod": {"enabled": false}`. Começa desligada, liga depois dos spikes. Desligada = caminho atual intacto | Rollback = mudar a flag | |
| D9 | O mod **traduz eventos nativos** pro vocabulário do `--hook-notify` (`{event, session_id, tool_name, ts}`), e o manager chama o mesmo `hook_state.write_event`. Tabela na seção 4 | S3: `classic.*` nunca dispara aqui (o validate avisa: em instalação gerenciada pela org, esses eventos pulam plugins do usuário). `_derive_status`, `work_items.log_event` e `hermes_events.normalize_event` seguem iguais | Hooks `classic.*` (refutado no S3); vocabulário novo (reescreve 3 consumidores) |
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
- `build_child_env(base, tab_id, session_key, bridge=None)`: com `bridge`, adiciona `CLAUDEMANAGER_BRIDGE_URL` e `CLAUDEMANAGER_BRIDGE_TOKEN`. Não mexe em `CLAUDE_CODE_PLUGIN_DIRS` (D5).
- `build_resume_command(..., mod_dir=None)`: em modo mod, `--plugin-dir "<mod_dir>"` e `hooks_path=None` (D6).
- `PtySession.restart`: re-emite o token (processo novo, env novo) e revoga o anterior. `close_session` revoga.
- `PtySession.mode ∈ {"mod", "hooks"}` por aba: hoje o `restart` reusa `self.hooks_settings_path` sempre. Com o modo por aba, o "reiniciar em modo hooks" do D7 é `restart(mode="hooks")`.
- `PtyManager`: `mod_state` por aba (`pending` no spawn → `alive` no hello → `missing` depois de `hello_timeout_s`); entra no `get_all_sessions` como `mod_state`.
- `main.py`: sobe o `ModBridgeServer` antes do `PtyManager` quando `claude_mod.enabled`; se falhar ao subir, segue no modo hooks e loga.
- Testes: atualizar `tests/pty_env_check.py` (vars novas, sem bridge = env igual ao de hoje), `tests/session_support_check.py` (`--plugin-dir` e sem `--settings` em modo mod), `tests/pty_manager_check.py` (transições do `mod_state`, revoke no restart/close).

### T1.3 Esqueleto do mod `mods/claudemanager-bridge/`
- `.claude-plugin/plugin.json`, `hooks/hooks.json`, `hooks/register.ts`, `hooks/client.ts` (fetch com header, timeout 2s, fila serial com `seq`, erro engolido = fail-open), `types/index.d.ts` (`PluginState['claudemanager-bridge']`: `seq`, `submitted: string[]`).
- `session.start`: lê as env vars; sem elas, retorna `next(e)` e não registra mais nada de efeito. Com elas, manda `hello` e inicia o loop do inbox.
- Gates: `claude plugin validate`, `tsc -p`, `claude plugin test` (o `$.http.fetch` e o `$.env.get` são eventos, então o teste responde por baixo do plugin: `on('http.fetch', ...)`, `on('env.get', ...)`).

---

## 4. Fase 2: item 1 (estado exato)

### T2.1 Mod: tradução de eventos nativos (D9)
Cada hook faz `await next(e)` primeiro (não altera o comportamento) e depois `POST /v1/event`. Payload sem input de tool nem texto de prompt (mantém a regra de privacidade do `log_event`). Ordem observada no S3: `turn.start` → `prompt.submit` resolve → `tool.check` → `tool.call` → `turn.complete`.

| Evento nativo | Vira (`event` no POST) | Observação |
|---|---|---|
| `turn.start` sem `agentId` | `UserPromptSubmit` | Também cobre turno iniciado por `$.prompt.submit` (que o `prompt.submit` do próprio plugin não vê, por causa da regra de re-entrada) |
| `tool.call` antes do `next` | `PreToolUse` (+ `tool_use_id`, `agentId`) | |
| `tool.call` depois do `next` | `PostToolUse` | `isError` / `deny` vão junto |
| `tool.check` com veredito `ask`, a tool **ainda parada 3s depois** | `PermissionAsk` (novo) | O `tool.check` roda antes do modo decidir o `ask`: no `auto`/`acceptEdits` o classificador ou o modo muitas vezes libera sem diálogo. A API não expõe o modo efetivo, então o mod arma um timer de 3s por `tool_use_id` e cancela quando o `tool.call` resolve. Diálogo real é reportado até 3s atrasado; liberação rápida do modo nunca acende `tooluse` (nem bloqueia composer, toca som ou acorda o Hermes). **Medido em 2026-10-20:** no `auto` o classificador segura 5-30s, então a grace sozinha não basta; o manager só aplica o ask se o PTY desenhou o diálogo (`PtyManager.on_event` → `shows_permission_dialog`) |
| `turn.complete` sem `agentId`, `reason: answer` | `Stop` | |
| `turn.complete`, `reason: aborted` | `Stop` + `aborted: true` | Esc do usuário |
| `turn.complete`, `reason: error \| refusal` | `StopFailure` (novo) | Hoje a aba fica presa em `running` |
| `session.end` | `SessionEnd` (novo) | No `/clear` o `session.start` **não** dispara de novo; só vem `session.end` (S3) |
| troca de `$.session.id()` | campo `session_id` de todo POST | Depois do `/clear`, o id novo aparece no turno seguinte (S3). O mod checa `$.session.id()` em cada `turn.start` e manda junto |

Não há equivalente de `Notification(idle_prompt)` (`waiting`). O manager deriva `waiting` de `ready` + N segundos sem input, se quiser manter.

### T2.2 `hook_state._derive_status`
| Evento | Hoje | Novo |
|---|---|---|
| `PermissionAsk` | não existe | `tooluse` (exato no modo default) |
| `Stop` com `aborted` | `ready` | `ready` + `aborted: true` no state (o composer avisa: prompt de plugin vai ser descartado, ver S6) |
| `StopFailure` | não existe | `ready` + `error: true` |
| `SessionEnd` | não existe | sem mudança de status; marca `session_ending` pro `get_all_sessions` esperar o id novo |

`write_event` ganha `aborted`, `error`, `agent_id` opcionais. O caminho `--hook-notify` não muda.

### T2.3 `work_items.log_event` + `hermes_events.normalize_event`
- `PermissionAsk` → `permission_requested`. No modo mod não existe `Notification`, então não há publicação dupla.
- `StopFailure` → só log por default (`task_failed` publicado depende de combinar o contrato com o Hermes).
- `Stop` com `aborted` → `task_completed` com `aborted: true` (o Hermes precisa saber que o usuário interrompeu, e que prompt automático vai cair, ver T4.3).

### T2.4 Frontend
- `terminal.js status`: com `mod_state === "alive"`, o `backendState` vale sozinho (sem o desvio "waiting + output recente = running", que era remendo da ambiguidade). Badge "mod off" quando `missing`.
- `CLAUDE.md`: atualizar as seções 3, 5 e 6, a linha "State flow" e adicionar `mod_bridge.py` + `mods/claudemanager-bridge/`.
- Testes: `tests/hook_state_check.py` (tabela acima), `tests/hermes_events_check.py`, e teste do mod cobrindo cada linha da tabela T2.1 (o `claude plugin test` levanta `turn.*`/`tool.*` e responde o `http.fetch` por baixo do plugin).

---

## 5. Fase 3: item 4 (uso por aba)

- Mod: `POST /v1/usage` no `turn.complete` e no `session.measure`, com throttle de 1 por 5s (o S7 viu 10 `session.measure` numa sessão curta). `$.session.usage()` sem args é grátis.
- Shape real (S7): `{ startedAt, context: { tokens, window, percent }, rateLimits: [{ kind: "five_hour" | "seven_day", percentUsed, resetsAt }], cost: { usd } }`.
- Manager: `get_all_sessions` inclui `usage`. UI: badge de contexto na aba (verde < 60%, amarelo < 85%, vermelho acima), tooltip com custo. Barra global com 5h/semana (o rate limit é da conta, então vale o mais recente entre as abas).
- O `cacheRemainingMs` do `terminal.js` passa a contar do `ts` real do último `UserPromptSubmit` em vez do envio pelo composer (pega prompt digitado direto no terminal).
- Testes: payload com `rateLimits` vazio (fora de assinatura) e sem `cost`.

---

## 6. Fase 4: item 2 (prompt sem PTY)

### T4.1 Inbox no manager
- `PromptInbox` por aba: item `{id, kind: "prompt", text, source: "hermes", request_id?, decision_id?}`. Estados: `queued` → `claimed` (mod pegou) → `started` (o `$.prompt.submit` resolveu = turno começou) | `dropped` (motivo do engine) | `failed`.
- Re-entrega: nunca automática. `claimed` sem `started` depois de 120s → `unknown` (mesma semântica do `EXECUTION_UNKNOWN` de hoje).

### T4.2 Mod
- O loop do inbox, para cada item: grava `id` em `$.state.submitted` (session-scope, sobrevive ao reload), manda `ack claimed` e agenda o submit num dispatch próprio (`$.clock.after(0, ...)`), que faz `await $.prompt.submit({ text })`. Resultado com `drop` → `ack dropped` + motivo; senão `ack started`. O loop nunca espera o submit (S6: numa sessão ocupada ele só resolve quando o turno começa). Um `id` que já está em `submitted` recebe só o ack e não é reenviado.

### T4.3 Quem usa (revisto pelo S6)
- **Hermes** (`pty_manager.send_prompt` / `send_owned_prompt`): aba claude com mod → inbox em vez do `session.write`. Ganhos medidos: o texto chega inteiro (multi-linha, unicode, 20KB), espera o turno livre, e `started` dá ao Hermes `provider_processed: true` (hoje é sempre `false`). O modelo lê o prompt embrulhado como "The claudemanager-bridge plugin sent a message: … " (~180 chars de wrapper): pro Hermes isso é proveniência e fica. **Depois de um Esc, o engine descarta prompt de plugin** (`{drop: "the session was stopped and nobody has sent input since"}`) até a pessoa digitar. O recibo vira `dropped`, sem fallback pro PTY: o usuário interrompeu e o Hermes não deve furar isso.
- **Composer: continua no PTY.** Motivos do S6: o modelo leria a mensagem do usuário como vinda de um plugin, e ela seria descartada depois de qualquer Esc. Melhoria opcional: `$.prompt.fill` (texto exato na caixa) + `\r` via PTY, no lugar do bracketed paste.
- `prepare_composer_message` / bracketed paste continuam pro composer e pra codex/opencode.

### T4.4 Testes
`tests/prompt_inbox_check.py` (estados, `dropped`, sem re-entrega, timeout → unknown), `tests/approval_flow_check.py` (send_prompt via inbox mantém hash e idempotência), teste do mod (item duplicado não submete duas vezes, `drop` vira `ack dropped`).

---

## 7. Fase 5: item 3 (aprovar tool pela UI) (revisto pelo S4)

Fatos do S4 (modo default):
- O diálogo do terminal **só aparece depois** que o `tool.check` resolve. Enquanto o hook espera, o terminal mostra só o spinner (a variante A, corrida, está descartada).
- O hook pode decidir: `allow` roda a tool sem diálogo; `deny` vira erro pro modelo e o turno continua.
- Esc durante a espera aborta o hook (`next.signal`) e **o turno inteiro** (`reason: aborted`), não só a tool.
- Esperar 8 a 20s num `$.http.fetch` não estourou o budget.

| Variante | Como | Custo |
|---|---|---|
| **C: detecção + tecla** (default) | O `tool.check` devolve o `ask` na hora: o diálogo abre no terminal, e o manager recebe `PermissionAsk` com `tool_use_id` e resumo. Os botões do card escrevem a tecla do diálogo no PTY (`1` = sim, Esc = não) | Depende do layout do diálogo (opção 1 = Yes); valida só com `tooluse` ativo pro mesmo `tool_use_id` |
| **R: modo remoto** (toggle por aba) | Com o toggle ligado, o `tool.check` segura o `ask` e faz long-poll em `/v1/permission` até a UI decidir; `allow`/`deny` voltam pelo hook, sem tecla. Terminal mostra `$.ui.status("aguardando aprovação no ClaudeManager")` | Quem está no terminal não consegue responder ali; o único escape é Esc, que aborta o turno |

Recomendação: C como padrão e R como toggle "estou longe do terminal" (o caso real de aprovar pela UI). B (janela curta) foi descartada: faz quem olha o terminal esperar a janela por nada.

Em qualquer variante:
- Card na aba: tool, resumo curto (Bash: primeiros 200 chars do comando; Edit/Write: path; MCP: nome) só em memória, nunca persistido. Botões **Permitir uma vez** / **Negar** (+ **Responder no terminal** no modo R, que solta o `ask`).
- A decisão vale só pro `tool_use_id` exato; card velho some sozinho no `PostToolUse` / `Stop`.
- Sem `updatedPermissions` (sem "sempre permitir") na v1.
- D11 fail-open: manager fora no modo R → devolve o `ask` (diálogo no terminal). D12 só humano: teste prova que nenhuma rota do `agent_gateway` chega em `/v1/permission` nem no `Bridge.decide_permission`.
- Som e notificação: `_maybePlayStateSound` já dispara em `tooluse`; agora dispara no instante certo.
- **Pendente S4b:** comportamento no modo `auto` (o padrão do usuário). Não rodei: o classificador do modo auto bloqueou eu subir um agente em modo auto. Teste manual: abrir uma aba com o spike, pedir um comando ambíguo e ver se o `ask` vira diálogo ou se o classificador resolve.

---

## 8. Resultados dos spikes (2026-10-19, Claude Code 2.1.303)

Spike reproduzível em `Docs/plans/2026-10-19-ClaudeManagerMod-spike/` (mod `cm-spike`, `driver.py` com servidor eco + `claude` num PTY real via pywinpty, `run_all.py`). Modelo haiku, `--permission-mode default`.

| Spike | Resultado | Decisão tomada |
|---|---|---|
| S1 | Append em `CLAUDE_CODE_PLUGIN_DIRS` no env do PTY: mod **não carrega** (o `env` do `~/.claude/settings.json` sobrescreve; o mod via `$.env.get` só via o harness-band). `--plugin-dir`: carrega, harness-band continua, sem prompt de hot reload. `hello` 8.4s após o spawn (sessão nova). `--resume` não medido (o projeto do spike não guardou transcript) | D5 → `--plugin-dir`; D7 timeout default 30s |
| S2 | `$.env.get` lê as vars da aba; `$.http.fetch` pra `127.0.0.1` com header custom funciona; `$.session.version()` = 2.1.303 | D1, D2, D4 confirmados |
| S3 | `classic.*` **nunca dispara** (instalação gerenciada pela org; o validate avisa). Nativos disparam: `turn.start`, `prompt.submit`, `tool.check`, `tool.call`, `turn.complete`, `session.end`, `session.measure`. `/clear`: só `session.end`, sem `session.start`; o id novo aparece em `$.session.id()` no turno seguinte | D9 → tradução de nativos (T2.1) |
| S4 | Diálogo só depois do `tool.check` resolver; `allow` do hook roda sem diálogo; `deny` vira erro; Esc na espera aborta o hook e o turno | Item 3 → C + toggle R |
| S5 | Long-poll de 20s em timer encadeado: 6+ ciclos, `waitedMs` ~20000, 0 erros, nenhuma linha de hook recusado | D3 confirmado |
| S6 | Texto chega inteiro (multi-linha, unicode, 20KB); espera o turno livre (`queued_until_idle`); o modelo lê o wrapper "The cm-spike plugin sent a message: …"; depois de um Esc, o submit volta `drop` até a pessoa digitar; o `prompt.submit` do próprio plugin não vê o próprio submit | Composer fica no PTY; Hermes vai pro inbox com estado `dropped` |
| S7 | `$.session.usage()`: `{startedAt, context{tokens,window,percent}, rateLimits[{kind,percentUsed,resetsAt}], cost{usd}}`; `session.measure` frequente | Throttle de 5s |
| S4b | Não rodado (modo auto bloqueado pelo classificador pra agente) | Teste manual pendente |

---

## 9. Ordem, gates e rollback

**Estado em 2026-10-19:** Fases 1 e 2 implementadas na branch `feat/claude-mod`, com 8 testes do mod, 45 checks em `tests/mod_bridge_check.py` e um e2e com `claude` real (`Docs/plans/2026-10-19-ClaudeManagerMod-spike/e2e_mod.py`: hello, running → ready, uso, troca de id no `/clear`, restart retomando). Flag `claude_mod.enabled` ainda desligada. **Gate pra ligar:** o S4b manual no modo `auto` (uma aba, um Bash que o classificador avalia; o estado não pode piscar `tooluse` nem o publisher do Hermes publicar `permission_requested`).

1. Fase 0 (spikes): feita, gate passou (S1 via `--plugin-dir`, S2 ok, S3 via eventos nativos). Falta o S4b manual.
2. Fase 1 + Fase 2 juntas (o transporte sem consumidor não se prova) → liga `claude_mod.enabled` só numa aba de teste. Critério: uma semana sem aba presa em estado errado; o Hermes recebe o mesmo número de `task_completed` que antes.
3. Fase 3 (barata, mesmo transporte).
4. Fase 4.
5. Fase 5 (variante C + toggle R).
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
| Abas dobram o trabalho do watch de mods do usuário | Cada aba carrega 2 mods (harness-band + bridge); medir RAM/CPU com 6 abas no smoke da Fase 1 |
| Organização muda a política de plugins | Os eventos nativos usados não têm a restrição dos `classic.*`; `hello` reporta a versão; fallback do D7 |
