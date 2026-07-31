# Conexões — Jira & Teams (pré-req do batch 2)

> Batch 1 (dados) não usa nada disto. Isto destrava a UI que busca (batch 2).
> Decisões: API token pro Jira, cópia enxuta do graph.py pro Teams (AskUserQuestion 2026-07-21).

## Princípio: referência congelada, não espelho

Decidido no grill. O app NÃO sincroniza em background. Busca sob demanda:
- Jira: 1 JQL ao abrir o popover de vínculo (issues abertas do usuário); 1 refetch por foco de aba.
- Teams: últimas N mensagens ao abrir o popover (one-shot, não o poller infinito do teams-copilot).

Sem `.graph_seen.json`, sem dedup persistente, sem daemon. O poller do teams-copilot resolve outro
problema (sugerir resposta a cada msg nova); aqui só preciso listar as recentes pra você escolher.

## Jira

- **Auth:** API token + basic auth (`email:token`) via httpx. Token gerado manualmente pelo Caio em
  https://id.atlassian.com/manage-profile/security/api-tokens — revogável, 1 usuário.
- **Onde mora o token:** `%LOCALAPPDATA%\ClaudeManager\secrets.json` (fora do git, fora do config versionado).
  NUNCA em config.json (que é versionado/mutado em runtime). Ler no arranque, manter em memória.
- **Base:** `https://mautomacao.atlassian.net`, cloudId `5574e58b-e70b-4fe9-a7e2-b5be1dc6e2a7` (já resolvido).
- **Chamadas (batch 2):**
  - listar issues abertas: `GET /rest/api/3/search` (ou o novo `/search/jql`), JQL
    `assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC`,
    fields `summary,status,duedate,issuetype,priority` (só o que o work item guarda — evita o payload
    de 131KB que estourou o token limit quando pedi *all).
  - refetch de 1 issue: `GET /rest/api/3/issue/{key}?fields=status,duedate`.
  - transições (fechar aba → transicionar): `GET /rest/api/3/issue/{key}/transitions` (lista real, varia por
    workflow — 7 colunas no DS) → `POST .../transitions` com o id escolhido. Escopo do token cobre write.
- **NOVO módulo:** `terminal/jira_client.py` — httpx fino, stdlib+httpx. Sem SDK.

## Teams

- **Auth:** reusa MSAL device-code do teams-copilot. Mesmo `GRAPH_CLIENT_ID`/`GRAPH_TENANT_ID` → mesma app
  registration → mesmo consent → o `.msal_cache.json` existente vale (nenhum re-login se o refresh vier).
- **Onde moram os ids:** `secrets.json` (junto do token Jira). Cache MSAL: copiar/apontar pro
  `.msal_cache.json` do teams-copilot no 1º run, depois CM mantém o seu em `get_data_dir()`.
- **NOVO módulo:** `terminal/teams_graph.py` — cópia ENXUTA do `teams-copilot/teams_copilot/graph.py`.
  Fica: `_token` (auth silenciosa + device-flow no arranque), `_get`, `parse_message`, `me_id`.
  **Sai:** o generator `message_source`/`_poll_once`, o dedup `.graph_seen.json` (era do poller).
  **Entra:** `list_recent(top=20)` — one-shot: `GET /me/chats?$expand=lastMessagePreview&$orderby=...&$top=N`
  → devolve [{person, preview, chat_id, chat_type, ts}] pro popover. Sem estado entre chamadas.
- **Escopo:** `Chat.Read` + `User.Read` (o que já tem). Enviar mensagem exigiria `ChatMessage.Send` (consent
  novo, TI) — por isso o "avisar a pessoa" no fechamento ABRE o chat / copia rascunho, não envia.

## secrets.json (schema, fora do git)

```json
{
  "jira": { "email": "caioraid@gmail.com", "token": "<manual>" },
  "graph": { "client_id": "<do teams-copilot>", "tenant_id": "<do teams-copilot>" }
}
```

`.gitignore` do ClaudeManager precisa listar `secrets.json` antes de qualquer commit. Loader tolera ausência
(sem secrets → Jira/Teams desligados, app funciona só com TODO manual).

## Ordem

1. Batch 1 (dados) — NÃO depende disto. Implementa já.
2. Caio gera o token Jira → cola em secrets.json.
3. `jira_client.py` + `teams_graph.py` (batch 2, junto da UI que os chama).
