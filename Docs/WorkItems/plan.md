# Plano — WorkItems Batch 1 (camada de dados, sem UI)

> Escopo: persistência + logs. ZERO frontend. Entregável = API no Bridge chamável e testável,
> event log gravando sozinho via hook existente. UI é batch 2.
> Fonte das decisões: `grill-tracker.md` (Q1–Q11).

## Princípio de corte

Batch 1 não desenha nada. Se no fim dele a sidebar não mudou mas `python -m ...` cria work item,
vincula sessão, e o `events-*.jsonl` engorda ao usar um terminal — passou. O risco está no modelo
de dado e no enxerto do log, não em pixel. UI valida depois em cima de dado que já existe.

## Modelo de dados (congelado no grill)

Três arquivos, responsabilidades opostas:

| Arquivo | Onde | Tipo | Escreve quando | Retenção |
|---|---|---|---|---|
| `work_items.json` | `%LOCALAPPDATA%\ClaudeManager\` | snapshot | muda algo | nunca |
| `work_log.jsonl` | `%LOCALAPPDATA%\ClaudeManager\` | append (1 arquivo, baixo volume) | ação de work item | **nunca** |
| `events\events-YYYY-MM-DD-<sid>.jsonl` | `%LOCALAPPDATA%\ClaudeManager\events\` | append (1 por sessão/dia) | cada hook | **60 dias** |

**Correção pós-review (local):** `work_items.json` vai pro `get_data_dir()` (LOCALAPPDATA), NÃO na raiz.
Motivo: `bridge.py:12-13` usa `Path(__file__).parent.parent` pra raiz, que quebra sob PyInstaller onefile
(`_MEIPASS` temp). Fatos duráveis não podem morar em caminho volátil. Fica junto do `state\` de hoje.

**Correção pós-review (partição do event log):** um arquivo por sessão por dia (`-<sid>` no nome), não um
arquivo compartilhado. Motivo empírico (Fable testou): append multi-processo no Windows NÃO é atômico — o
CRT faz seek+write em 2 passos, e hooks paralelos de sessões diferentes perderam 27% das linhas + corromperam.
Um arquivo por sessão = cada processo de hook escreve só no seu = zero contenção, mesmo padrão do `state\{sid}.json`
atual. Leitura de análise faz glob + merge. Leitor SEMPRE tolera linha corrompida (skip em json.loads falho).

### work_items.json (schema)

```json
{
  "version": 1,
  "items": [
    {
      "id": "wi_<uuid8>",
      "source": "jira|teams|manual",
      "external_key": "DS-201" | null,
      "external_url": "https://mautomacao.atlassian.net/browse/DS-201" | null,
      "title": "Eliminar o fluxo de Seguir com a Stone",
      "status": "To Do" | null,          // coluna Jira; null pra teams/manual
      "duedate": "2026-07-21" | null,    // date-only (Jira) OU ISO c/ hora (teams/manual)
      "duedate_has_time": false,          // distingue "só data" de "data+hora"
      "person": "Gustavo" | null,         // só teams
      "sort_order": 1000,                 // float; reorder global (Q10)
      "done": false,
      "created_at": 1784600000.0,
      "closed_at": null,
      "jira_last_seen": {                 // só source=jira; SEMEADO na criação (evita falso-diff)
        "status": "To Do", "duedate": "2026-07-21", "ts": 1784600000.0
      }
    }
  ],
  "session_links": {
    "<claude_session_id>": { "wi_id": "wi_ab12cd34", "archived": false, "ts": 1784600000.0 }
  }
}
```

**Correção pós-review (o vínculo NÃO mora em sessions.json):** o `sessions.json` é reescrito inteiro a cada
10s pelo frontend (`app.js:165` → `bridge.py:211`) com um set fixo de 7 campos — qualquer `work_item_id`/`archived`
gravado ali é apagado no tick seguinte, e `archived` de aba fechada some de vez (o arquivo é projeção das abas VIVAS).
Então o vínculo sessão→item e a flag `archived` moram no `session_links` do `work_items.json` (durável, não clobbado),
keyed por **`claude_session_id`** — a mesma chave que o hook usa no event log e o work_log usa no campo `session`. Um só join.

`version: 1` no topo — migração de schema no batch 2/3 é certa (furos 3/5), migrar sem versão é adivinhação.

**Limite conhecido do batch 1:** só sessão com `claude_session_id` vincula. Codex (`pty_manager.py:119`) e `--continue`
(`pty_manager.py:126-127`) têm id null — o app nunca aprende a chave. Vincular esses fica pro batch 2 (precisa capturar
o id via hook UserPromptSubmit, que ecoa o session_id real). Item→N sessões, sessão→1 item (single FK).

### work_log.jsonl (uma linha por ação)

```json
{"ts": 1784600000.0, "kind": "link",     "wi": "wi_ab12cd34", "session": "<uuid>", "detail": {"source":"jira","key":"DS-201"}}
{"ts": 1784600100.0, "kind": "unlink",   "wi": "wi_ab12cd34", "session": "<uuid>"}
{"ts": 1784600200.0, "kind": "archive",  "session": "<uuid>"}
{"ts": 1784600300.0, "kind": "complete", "wi": "wi_ab12cd34"}
{"ts": 1784600400.0, "kind": "duedate",  "wi": "wi_ab12cd34", "detail": {"from":"2026-07-21","to":"2026-07-27"}}
{"ts": 1784600500.0, "kind": "status",   "wi": "wi_ab12cd34", "detail": {"from":"To Do","to":"In Review"}}
```

Alimenta daily (kind∈{link,complete}), promessa esquecida (item sem sessão há N dias), prazo empurrado
(kind=duedate). NÃO expira.

### events-YYYY-MM-DD.jsonl (uma linha por hook)

```json
{"ts": 1784600000.123, "session": "<uuid>", "event": "PreToolUse", "tool": "Bash"}
```

Só metadado (Q7). Enxertado em `hook_state.write_event` — o hook já chega lá. Partição por sessão+dia (Q8 + furo
de concorrência). GC apaga arquivo cujo dia no nome > 60 dias (Q11). `tool` = null quando o evento não é de tool.
**Data do nome = hora LOCAL** (a daily raciocina em "hoje/ontem" local; virada às 21h UTC-3 partiria a análise).

## Arquivos e mudanças

### NOVO: `terminal/work_items.py`

Store puro, sem dependência de webview. Espelha o estilo de `hook_state.py` (funções módulo, escrita atômica).

```
# snapshot (load lê {version, items, session_links}; tolera JSONDecodeError → estrutura vazia)
load_store() -> dict                             # {"version":1,"items":[],"session_links":{}}
save_store(store: dict) -> None                  # atomic, temp+os.replace (espelha _atomic_write)
new_item(source, title, **kw) -> dict            # gera id, sort_order (max+1000), created_at;
                                                 # source=jira → SEMEIA jira_last_seen do status/duedate dados
complete_item(wi_id) -> None                     # done=True, closed_at=now; loga complete
reorder(ordered_ids: list[str]) -> None          # reescreve sort_order; NÃO loga (view pref)

# vínculo sessão→item (mora no session_links, NÃO em sessions.json)
link(claude_session_id, wi_id) -> None           # session_links[sid]={wi_id,archived:False}; loga link
unlink(claude_session_id) -> None                # remove; loga unlink
set_archived(claude_session_id, bool) -> None    # session_links[sid].archived; loga archive

# work log (baixo volume, nunca expira)
log_action(kind, wi=None, session=None, detail=None) -> None   # append work_log.jsonl

# event log (chamado pelo hook; 1 arquivo por sessão/dia)
log_event(session_id, event_name, tool_name=None, ts=None) -> None  # append events-<dia-local>-<sid>.jsonl
gc_events(max_age_days=60) -> None               # apaga partições cujo dia no nome > 60d
read_events(since=None, until=None) -> list[dict] # glob + merge + skip linha corrompida (pra análise/testes)

# jira refresh diff (sem rede aqui; recebe o dado já buscado pela UI/bridge)
apply_jira_snapshot(wi_id, status, duedate, ts) -> None   # compara com jira_last_seen (semeado na criação,
                                                          # então nunca falso-diff no 1º refresh),
                                                          # loga status/duedate se mudou, atualiza last_seen
```

Todos os locais via `hook_state.get_data_dir()` (reusa) — inclusive `work_items.json` (LOCALAPPDATA, não raiz).

### EDIT: `terminal/hook_state.py`

Uma linha no fim de `write_event`, best-effort (nunca levanta — é hook):

```python
def write_event(session_id, event_name, ts=None, tool_name=None):
    ...
    if status is None:
        # ainda assim registra o batimento cru pro event log
        _safe_log_event(session_id, event_name, tool_name, ts)
        return
    _atomic_write(...)
    _safe_log_event(session_id, event_name, tool_name, ts)
```

`_safe_log_event` faz try/except em volta de `work_items.log_event`. **Import lazy** dentro da função
(evita ciclo hook_state↔work_items). `tool_name` vem do payload do hook em `main.py --hook-notify`
(PreToolUse/PostToolUse têm `tool_name`; passar adiante — ver EDIT main.py).

### EDIT: `main.py` (branch `--hook-notify` + startup)

- `--hook-notify`: extrair `tool_name` do payload (`payload.get("tool_name")`, já parseado em `main.py:44-45`)
  e repassar a `write_event`. PreToolUse/PostToolUse carregam `tool_name`; os outros → null.
- **startup**: chamar `work_items.gc_events()` junto do `gc_orphans()` existente (`main.py:103`). Sem esse
  caller a retenção de 60d (Q11) nunca roda — furo apontado no review.

### EDIT: `api/bridge.py`

Métodos novos, thin wrappers sobre `work_items.py` (chamáveis por JS no batch 2; testáveis já). **Recebem
`claude_session_id`, não o pty session_id** — é a chave durável e a de join com os logs:

```
list_work_items() -> dict                    # {items, session_links} pro front montar sidebar
create_work_item(source, title, external_key=None, external_url=None,
                 status=None, duedate=None, duedate_has_time=False, person=None) -> dict
link_session(claude_session_id, wi_id) -> None
unlink_session(claude_session_id) -> None
complete_work_item(wi_id) -> None
reorder_work_items(ordered_ids) -> None
archive_session(claude_session_id, archived=True) -> None
```

**NÃO toca em `sessions.json`.** O vínculo e o `archived` moram no `session_links` do `work_items.json`
(furo 2 do review: sessions.json é volátil, reescrito a cada 10s). Bridge não precisa de `_load/_save_sessions` aqui.

## Fora do escopo do batch 1 (anotado pra não vazar)

- Qualquer HTML/CSS/JS. Sidebar, cards, popover, cerimônia — batch 2.
- Busca Jira via MCP (a UI que dispara; `apply_jira_snapshot` só recebe o dado pronto).
- Poll/refetch de prazo (batch 2 decide gatilho: abrir app + focar aba).
- Cache-timer por sessão (batch 2, é visual).
- Análise (daily, promessa esquecida, tendência) — batch 3, lê os logs já acumulados.

## Testes (`tests/work_items_check.py`, estilo *_check.py do teams-copilot)

Plano tem teste, não é framework — asserts + prints, roda com `python tests/work_items_check.py`.
Node test de JS não cobre isto (é Python). Casos:

1. **snapshot roundtrip** — new_item → save → load → mesmos campos; sort_order cresce; version preservada.
2. **complete** — done=True, closed_at setado, work_log ganha linha `complete`.
3. **reorder não loga** — reorder muda sort_order, work_log inalterado.
4. **link durável** — link(sid,wi) grava em session_links; load relê; unlink remove. Nada em sessions.json.
5. **event log por sessão/dia** — log_event de 2 sessões → 2 arquivos distintos; linha é só metadado (sem input).
6. **event log tolera corrupção** — arquivo com 1 linha meia-escrita → read_events pula, retorna as boas.
7. **gc_events** — cria `events-<hoje-70d>-x.jsonl` fake, gc apaga; mantém o de hoje.
8. **jira diff** — apply com status novo → loga `status`; mesmo status → não loga; 1º refresh (last_seen semeado) não loga.
9. **duedate empurrado** — apply com duedate diferente → loga `duedate` com from/to.
10. **hook enxerto isolado** — write_event com tool_name grava event log E não quebra se work_items falhar
    (monkeypatch log_event pra levantar; write_event não propaga; status ainda persiste).
11. **atomic** — sem .tmp sobrando após save_store.

## Ordem de implementação

1. `work_items.py` (store + logs + gc) — núcleo, testável sozinho.
2. `tests/work_items_check.py` — casos 1–7, 9. Roda verde antes de tocar no hook.
3. Enxerto em `hook_state.write_event` + `main.py` (tool_name) — caso 8.
4. Métodos no `bridge.py` (thin wrappers; vínculo via session_links, NÃO sessions.json).
5. Smoke manual: roda o app, usa um terminal, confirma `events\events-<hoje>-<sid>.jsonl` engordando.

## Riscos

| Risco | Mitigação |
|---|---|
| **Append multi-processo NÃO é atômico no Windows** (review provou: 27% perda) | 1 arquivo por sessão/dia (`-<sid>` no nome) → cada processo de hook escreve só no seu, zero contenção. Leitor tolera linha corrompida |
| **sessions.json clobbado a cada 10s** apaga vínculo | vínculo mora no `session_links` do work_items.json, não em sessions.json |
| Ciclo de import hook_state↔work_items | import lazy dentro de `_safe_log_event`. (Review: não há ciclo real hoje, mas lazy mantém o fast-path do hook leve — cada hook é processo novo, import top-level é pago por evento) |
| Hook levanta e trava sessão | `_safe_log_event` engole tudo; write_event nunca propaga. `work_items.py` estritamente stdlib, zero trabalho no import |
| `tool_name` ausente em hook não-tool | null quando ausente (`payload.get`) |
| jira_last_seen falso-diff no 1º refresh | semeado em `new_item` quando source=jira |
| gc_events nunca roda | caller no startup (main.py:103, junto do gc_orphans) |
| Fuso na partição/GC | dia do nome = hora LOCAL, explícito |
| work_items.json corrompe | load tolera JSONDecodeError → `{version,items:[],session_links:{}}` |
| PyInstaller: raiz não-gravável | work_items.json em `get_data_dir()` (LOCALAPPDATA), não na raiz via `__file__` |
```
