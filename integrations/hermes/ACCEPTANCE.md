# Checklist de aceite — integração ClaudeManager ↔ Hermes

Documento operador-primeiro: comando ou clique exato, resultado esperado, o que
uma falha significa. Nada aqui está verificado ponta-a-ponta ainda — a suíte
automatizada passa, e a cadeia do webhook foi provada por round-trips
assinados, mas nenhum PTY write real, nenhuma UI e nenhum ciclo completo
hook→log→publisher→Hermes foi clicado de verdade. Este checklist é o único
caminho até "verificado", e quem roda é o operador, sozinho, sem agente.

## 0. Reinicie primeiro — leia isto antes de qualquer item abaixo

O app rodando agora carregou `terminal/agent_gateway.py` **antes** das edições
desta sessão. O arquivo em disco hoje exige exatamente `{request_id, group_id}`
para criar sessão (`terminal/agent_gateway.py:361-363`, 400
`{"error": "session_creation_contract"}` se o corpo não bater); o processo
antigo, em memória, ainda espera o contrato de 4 campos anterior
(`{decision_id, proposal_hash, request_id, group_id}`). Até reiniciar, todo
`open_session` do Hermes falha com `400 session_creation_contract` — isso não
é bug, é o processo rodando código velho.

**O caminho de wake-up entra em operação no instante do restart, não quando
você chegar no item 3.2.** Na subida, `main.py` lê o segredo, inicia o publisher,
e o cursor ausente começa em `time.time()` — ele **não** replaya o backlog de 44k
registros. Mas o gateway do Hermes está de pé, então o primeiro hook que disparar
depois do restart vira um turno real do Hermes em segundos, sem você estar
olhando. Isso é o comportamento correto; só não deve ser surpresa. Se preferir
subir com o wake-up desligado, use o interruptor de §5 **antes** de reiniciar.

**Reinicie pelo atalho da área de trabalho:** `Claude Code Launcher.lnk` →
`wscript.exe ClaudeCodeLauncher.vbs` → `python main.py` (workdir = raiz do
repo). O `.vbs` roda o código-fonte direto — sem rebuild. Isso mata a sessão
atual (ela roda dentro de uma aba gerenciada pelo próprio app), então rode
este checklist depois, numa sessão nova, fora de uma aba do Claude Code
gerenciada por este app.

## 1. O que já está verificado

**Suíte automatizada completa** (todos os comandos abaixo, executados nesta
sessão, 0 falhas):

```
node tests/agent-approvals.test.js
node tests/session-sync.test.js
node tests/teams-search.test.js
node tests/terminal-input.test.js
node tests/terminal-reconnect.test.js
node tests/workitem-agent-state.test.js
```

```
python tests/agent_contracts_check.py        (10 checks)
python tests/agent_decisions_check.py        (9 checks)
python tests/agent_gateway_check.py          (14 checks)
python tests/agent_sources_check.py          (2 checks)
python tests/approval_flow_check.py          (27 checks)
python tests/bridge_ws_capability_check.py   (2 checks)
python tests/hermes_events_check.py          (57 checks)
python tests/hermes_runtime_check.py
python tests/hook_state_check.py             (15 checks)
python tests/input_protocol_check.py         (4 checks)
python tests/pty_env_check.py
python tests/pty_manager_check.py            (15 checks)
python tests/session_support_check.py
python tests/work_items_check.py             (52 checks)
python tests/workitem_agent_state_check.py   (19 checks)
python tests/ws_auth_check.py                (6 checks)
python integrations/hermes/tests/mcp_server_check.py  (7 checks)
```

Mais `node --check` nos arquivos JS tocados, `python -m compileall -q terminal
api integrations main.py` e `python -c "import main"` — todos OK.

**A cadeia do webhook**, provada por round-trips assinados contra o gateway
real (`python integrations/hermes/webhook_roundtrip.py`), executados pela sessão
que fez a ativação do lado Hermes (não pela sessão que escreveu o publisher —
essa nunca teve permissão de postar na 8644):

| `--mode`   | Resultado observado | Prova |
|---|---|---|
| `badroute` | `404` | resolução de rota |
| `badsig`   | `401` | verificação HMAC |
| `filtered` | `200 {"status":"ignored","reason":"filter"}` | HMAC + rota + perfil ok; chegou até avaliar o allowlist de `event_type` e a guarda de recursão declarativa (`payload.hermes_initiated equals: false`) — rejeitado por carregar `hermes_initiated: true` de propósito |
| `accepted` | `202 {"status":"accepted"}` | com a skill `claudemanager-bridge` injetada, um turno real do agente rodou e **não abriu nada** (zero eventos `automatic_action` no journal) — prova que a ativação acorda o Hermes sem gastar uma ação autônoma à toa |
| `accepted` repetido dentro de 60s | `200 {"status":"ignored","reason":"script"}` | dedup/cooldown do `claudemanager_event_filter.py` (`COOLDOWN_SECONDS = 60`, por `session_key`+`event_type`) |

Todo esse resultado veio de HTTP **200/202** — só o `badsig`/`badroute` fogem
disso. O corpo da resposta é o sinal real, nunca só o status.

## 2. O que NÃO está verificado

- Todo write real de PTY (aprovar → o texto realmente aparecer na aba do Claude).
- O ciclo real hook → log de eventos → publisher → Hermes, de ponta a ponta.
  O publisher **não está rodando** no processo atual, e isso não é ambíguo: o
  segredo entrou em `secrets.json` depois de o app já ter subido, então
  `main.py` leu `None` e nunca instanciou o publisher. Resolve no restart.
- A UI inteira: aprovar, rejeitar, o painel de Work Item deste Task 6 — nada
  disso foi clicado de verdade ainda.
- `open_session` automático fim-a-fim (hoje bloqueado pelo próprio contrato
  desatualizado no processo em memória — §0).
- `task_completed` acordando o Hermes exatamente uma vez, em uso real.
- Um evento de permissão chegando ao Hermes sem auto-aprovar nada no Claude.
- Aprovar um prompt → dispatch imediato + recibo lido pelo Hermes via
  `get_execution_receipt` + zero chamada de escrita MCP, em uso real.
- Rejeitar um prompt → nenhuma escrita na PTY, em uso real.
- Saída de processo acordando o Hermes uma vez e marcando a aba como finalizada.
- Eventos duplicados não criando turnos duplicados no Hermes, num cenário real
  (só o round-trip artificial foi testado).
- O ciclo Hermes→Claude→hook→Hermes parando na guarda de recursão, ponta-a-ponta.
- Prompts multilinha longos com acentos permanecendo intactos byte a byte.
- `hermes gateway` caindo no meio do uso real e a entrega retomando sozinha.

## 3. Checklist item a item

Rode cada item na ordem. Pare e investigue antes de seguir se algum item de
segurança (4, 5, 8) falhar.

### 3.1 Abertura automática de sessão, sem aprovação
- **Ação:** no Hermes (`hermes chat` ou Hermes Desktop), peça para abrir uma
  sessão num grupo configurado.
- **Esperado:** `POST /v1/sessions` (porta 8787) com `{request_id, group_id}`;
  `201 {"session": {...}}`; nenhum card na tela de aprovações do ClaudeManager
  (é automático — não passa por proposta/aprovação); no journal só aparece um
  evento `automatic_action`, nunca um `proposal_created`.
- **Falha:** `400 session_creation_contract` → app não foi reiniciado (§0).
  `403 hermes_read_token_required` → token do Hermes ausente/errado.

### 3.2 `task_completed` acorda o Hermes exatamente uma vez
- **Ação:** numa sessão aberta e linkada a um work item, mande um prompt curto
  e espere o Claude terminar (hook `Stop`).
- **Esperado:** um único turno novo no Hermes referenciando esse evento;
  `GET /v1/events?since=<ts antes do prompt>` (porta 8787, ver §4) mostra
  exatamente um registro `event_type: task_completed` para essa `session_key`.
- **Falha:** zero turnos → publisher desligado (ver §4) ou gateway do Hermes
  fora do ar. Mais de um turno → cursor/dedup quebrado — não esperado, reportar.

### 3.3 Evento de permissão chega ao Hermes sem auto-aprovar nada no Claude
- **Ação:** na sessão vinculada, dispare uma ferramenta que peça permissão no
  próprio Claude Code (hook `Notification` após `PreToolUse`).
- **Esperado:** o Hermes recebe `permission_requested`; o prompt de permissão
  do Claude Code continua esperando decisão humana normal — nada no
  ClaudeManager aprova sozinho. Verificar: nenhuma entrada `approval_recorded`
  nova no journal (`agent_decisions`) sem o operador ter clicado na UI.
- **Falha:** uma aprovação aparece no journal sem clique do operador → crítico,
  parar e investigar antes de liberar qualquer uso real.

### 3.4 Aprovar um prompt → dispatch imediato, recibo, zero escrita MCP
- **Ação:** com uma proposta pendente na UI de aprovações, clique
  "Enviar para Claude".
- **Esperado:** o texto aparece na aba do Claude no mesmo clique (sem round-trip
  pro Hermes); o card mostra "Prompt enviado ao Claude"
  (`HOST_WRITE_ACCEPTED`); o Hermes, chamando `get_execution_receipt(decision_id)`
  via MCP, lê o mesmo estado sem ter feito nenhuma chamada de escrita — não
  existe tool MCP que escreva na PTY (`open_session`/`link_work_item` são as
  únicas automáticas, nenhuma delas toca no terminal).
- **Falha:** texto não aparece → ver `reason` no receipt (`WRITE_FAILED` +
  código, rótulos em `web/js/agent-approvals.js`'s `AA_REASON_LABELS`).
  `get_execution_receipt` não bate com o que apareceu na tela → bug de
  journal/hash — parar de aprovar até investigar.

### 3.5 Rejeitar um prompt → nenhuma escrita na PTY
- **Ação:** clique "Rejeitar / pedir ajuste", informe um comentário.
- **Esperado:** nada é escrito na aba do Claude; `get_replan` no Hermes retorna
  `isError=true` com o feedback; um `decision_id` filho (versão+1) nasce quando
  o Hermes replaneja.
- **Falha:** qualquer texto aparece na aba → falha grave (bypass do boundary de
  aprovação) — parar tudo, não é um item que se investiga depois.

### 3.6 Saída de processo acorda o Hermes uma vez e marca a aba finalizada
- **Ação:** feche uma aba vinculada a um work item (ou deixe o processo do
  Claude terminar sozinho).
- **Esperado (duas coisas, duas superfícies diferentes):**
  1. Um único evento `process_exited` (status `exited`) chega ao Hermes; no
     painel de Work Item (botão de resumo diário, `openDaily()` em
     `web/js/workitems.js`) a linha do item mostra `encerrado <tempo>` na
     linha compacta de eventos.
  2. **A aba em si** muda de estado na barra de abas — para o estilo de
     finalizada/encerrada, sem mais indicar atividade (`web/js/tabs.js`
     `render()` aplica a classe `status-*`; o getter de status em
     `web/js/terminal.js` lê o `backendState` que o poll de 2s de
     `app.js`/`refreshStatus`/`get_terminals` atualiza). Isso é a "aba
     finalizada" do item do plano — não é a mesma coisa que o evento chegar
     ao Hermes, e fica numa tela diferente do painel de Work Item.
- **Falha:** nenhum evento no Hermes → produtor de `ProcessExited` (Task 4, em
  `terminal/pty_manager.py`) não disparou. Mais de um evento → duplicata, ver
  cursor. Evento chegou mas a aba continua com estado vivo na barra de abas →
  o `process_exited` não virou `backendState` na UI — bug de UI/polling, não
  do publisher.

### 3.7 Eventos duplicados não criam turnos duplicados
- **Ação:** `python integrations/hermes/webhook_roundtrip.py --mode accepted`
  duas vezes seguidas, com menos de 60s entre as duas.
- **Esperado:** primeira → `202 accepted`. Segunda → `200
  {"status":"ignored","reason":"script"}` (cooldown por `session_key`+
  `event_type` em `claudemanager_event_filter.py`, que imprime `[SILENT]` no
  stdout do script quando pega dedup ou cooldown).
- **Falha:** a segunda também vira `202` → cooldown quebrado, ou o script
  copiado pra `HERMES_HOME/scripts/claudemanager_event_filter.py` (passo 4 do
  `README.md`) ficou desatualizado em relação à cópia deste repo.

### 3.8 Ciclo Hermes→Claude→hook→Hermes para na guarda de recursão
- **Ação:** aprove um prompt (dispatch real), deixe o Claude responder e
  disparar `Stop` dentro de 120s (`HERMES_INITIATED_THRESHOLD_S` em
  `terminal/hermes_events.py`).
- **Esperado:** o `task_completed` gerado por esse `Stop` carrega
  `hermes_initiated: true`; o filtro declarativo do Hermes
  (`payload.hermes_initiated equals: false`) rejeita →
  `200 {"status":"ignored","reason":"filter"}`; nenhum turno novo nasce
  desse `Stop`.
- **Falha:** o Hermes reage e gera um turno novo a partir desse `Stop` → a
  guarda de recursão falhou — parar de aprovar prompts até investigar
  `_stamp_guard_fields`/o journal de `automatic_action`/`execution_result`.

### 3.9 Prompts multilinha longos com acentos ficam intactos
- **Ação:** aprove uma proposta cujo `prompt_text` tenha várias linhas, acentos
  e emoji (ex.: colar um texto real em português, com `ã, ç, é` e quebras de
  linha). Antes de aprovar, clique **Copiar** no card (botão já existente,
  `_copyPrompt` em `web/js/agent-approvals.js`) e cole num arquivo de
  rascunho — essa é a referência byte a byte pra comparar depois.
- **Esperado:** o texto que chega na aba do Claude é idêntico, caractere por
  caractere e quebra por quebra, ao que você colou no rascunho. Checagem
  mecânica alternativa/complementar: chamar
  `verify_proposal_hash(decision_id, rendered_text)` com o texto renderizado
  na tela — `text_ok: true` confirma que bate com o que foi aprovado (isso
  prova consistência entre proposta e tela, não substitui olhar o terminal).
- **Falha:** qualquer caractere trocado, mojibake ou quebra de linha perdida
  entre o rascunho copiado e o que apareceu na aba → problema de encoding em
  `terminal/input_protocol.py`/escrita na PTY — achar antes de liberar
  prompts reais de usuário.

### 3.10 Gateway do Hermes cai: acumula, back-off, nada degrada, retoma
- **Ação:** `hermes gateway stop` (verificado funcionando no Windows: responde
  `✓ Gateway stopped (drained cleanly)`); espere alguns ciclos de poll
  (`poll_interval_s` default 2s); confirme que o app ClaudeManager continua
  normal (terminais, UI, sem erro visível); depois `hermes gateway start`
  (verificado no Windows: sobe por spawn direto, não por systemd, apesar do
  texto do `--help`; o `/health` na 8644 volta em ~6s).
- **Esperado:** nenhum erro no app; os eventos continuam sendo logados
  localmente (`work_items.read_events` cresce); ao subir o gateway de novo, a
  entrega retoma sozinha, **sem** reiniciar o ClaudeManager.
- **Falha:** o app trava/mostra erro → o publisher não está isolado de falha
  de rede (bug — `tick()`/`_run()` deveriam capturar tudo). Eventos somem →
  o log local não estava sendo escrito, checar os hooks.
- **Antes de seguir para qualquer outro item:** confirme
  `hermes gateway status` mostrando `✓ Gateway process running`. Todo item
  seguinte no-opa em silêncio com o gateway parado — se você abandonar o
  checklist no meio deste item, fica sem wake-up nenhum até o próximo login.

## 4. Comandos de diagnóstico exatos

Lembrete que vale para todos: **toda rejeição de filtro/script/allowlist
devolve HTTP 200** — o sinal é o corpo (`reason`/`event`), nunca o status.

- **`python integrations/hermes/webhook_roundtrip.py --mode {filtered,accepted,badsig,badroute}`**
  — ver tabela em §1. Sai com código 2 se `hermes_webhook.secret` não estiver
  em `secrets.json`, ou se `/health` do Hermes estiver inalcançável.

- **`%LOCALAPPDATA%\hermes\logs\gateway.log`** — abra e olhe a entrada mais
  recente depois de cada round-trip; é onde o motivo de um filtro/script
  aparece por extenso. (Confirmado existir e ter escrita recente ao redigir
  este documento.)

- **Estado do publisher, na própria UI.** O publisher se **desabilita
  permanentemente** em `401`/`403`/`404` em vez de retentar (segredo errado ou
  rota inexistente não se conserta com backoff), e isso só é melhor que
  retentar em silêncio se você conseguir ver. O painel "Hermes · aprovações"
  mostra uma linha de aviso âmbar quando o estado é `disabled` ou `unknown`,
  com o motivo. Estado saudável, `backing_off` (transiente, se resolve sozinho)
  e `off` (sem segredo — o interruptor de §5) ficam silenciosos de propósito.
  Por trás disso: `bridge.get_hermes_publisher_status()` →
  `HermesEventsPublisher.status()` → `{state, reason, last_error, counters:
  {published, skipped_bad, logged_only, backoff_cycles}}`.

- **Proxy de baixo nível:** `%LOCALAPPDATA%\ClaudeManager\hermes_events_cursor.json`
  (`last_ts`, `recent_ids`) é gravado a cada `tick()` que chega a rodar — mas
  `tick()` retorna **antes** disso quando o health check do Hermes falha. Se o
  arquivo não existe, é (a) publisher nunca instanciado (sem
  `hermes_webhook.secret` quando o processo subiu — o caso de hoje, o segredo
  entrou depois do app já estar rodando) ou (b) health check nunca passou.
  Prefira a linha na UI; este arquivo é para quando a UI não sobe.

- **`GET /v1/events?since=<epoch>`** no gateway do ClaudeManager — porta
  **8787** (não 8644, que é o webhook do Hermes), header
  `Authorization: Bearer <token>` com o token de
  `%LOCALAPPDATA%\ClaudeManager\secrets\hermes_gateway_token` (confirmado
  existir). Resposta: `{"events": [...], "next_since": <ts>, "truncated": bool,
  "cap": 500}`. `400 {"error":"invalid_since"}` se `since` não for um float válido.

- **`verify_audit_chain()`** (`terminal/agent_decisions.py`) — rode
  pontualmente:
  ```
  python -c "from terminal import agent_decisions; print(agent_decisions.verify_audit_chain())"
  ```
  Devolve `True` se a cadeia de hash bate. Levanta `AuditIntegrityError` (linha
  corrompida, sequência fora de ordem, hash de payload ou de evento quebrado)
  se não — nesse caso não confie em nenhum estado de decisão até resolver.

## 5. O interruptor de desligar

- **Lado ClaudeManager (publisher):** apague a chave `hermes_webhook` de
  `%LOCALAPPDATA%\ClaudeManager\secrets.json` → o publisher fica desligado,
  fail-closed por design (`main.py:182-195`: sem secret, `hermes_publisher`
  fica `None` e só imprime um aviso). Confirmado: essa chave existe hoje
  nesse arquivo.

- **Lado Hermes (recepção):** o interruptor real é `WEBHOOK_ENABLED=false`
  em `%LOCALAPPDATA%\hermes\.env` (`integrations/hermes/README.md:97-105`).
  **Correção em relação ao que foi repassado nesta tarefa:** não existe uma
  chave `platforms.webhook.enabled` no `config.yaml` atual — essa forma
  aparece só num rascunho descartado nas notas de decisão
  (`.hermes/plans/20260803_133016-review-notes.md:67-74`); o `README.md`
  deste repo (mesma data) documenta que `config.yaml` só define a rota
  estática (`platforms.webhook.extra.routes.claudemanager-events`), e quem
  liga/desliga a plataforma inteira é o `.env`. Desligar `WEBHOOK_ENABLED`
  não perde a rota configurada — só para de recebê-la.

## 6. Onde estão os backups

- `%LOCALAPPDATA%\hermes\config.pre-claudemanager-webhook-20260803.yaml` —
  confirmado presente.
- `%LOCALAPPDATA%\ClaudeManager\secrets.pre-hermes-webhook-20260803.json` —
  confirmado presente.
- Cópia do `.env` do Hermes anterior à alteração: guardada no scratchpad da
  sessão que fez a configuração (task5a-publisher) — não é um caminho fixo
  neste repo. O `.env` real do Hermes foi só **apendado** (3 chaves
  `WEBHOOK_*` novas no fim do arquivo), nunca reescrito — reverter é remover
  essas 3 linhas, não restaurar um arquivo inteiro.

## 7. Estado atual do autostart do gateway

`hermes gateway status` (comando somente leitura, seguro de rodar a qualquer
momento) confirma, agora mesmo:

```
✓ Windows login item installed: C:\Users\caioc\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\Hermes_Gateway.vbs
✓ Gateway process running (PID: 42004)
```

Isso é um item de login na pasta **Startup**, não uma Scheduled Task.
`hermes gateway install` caiu nesse fallback porque criar a Scheduled Task
pediu aprovação de UAC e ela foi recusada.

**Consequência prática:** o gateway sobe sozinho no login, mas **não tem
restart automático se cair no meio da sessão** — um crash, ou uma atualização
do Windows, deixam o gateway parado até o próximo login manual.

**Upgrade disponível:** `hermes gateway install --force` (`--force` = "Force
reinstall", confirmado em `hermes gateway install --help`), aceitando o
prompt de UAC dessa vez, promove para uma Scheduled Task de verdade (gatilho
de logon, reinício automático a cada 1 min até 999 vezes).
