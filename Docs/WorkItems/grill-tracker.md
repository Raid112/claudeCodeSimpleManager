# Grill Tracker — WorkItems (camada de work items no ClaudeManager)

> Captura das perguntas do grill e gaps detectados no plano.
> Escopo: quais dados coletar e por quê, antes de fechar o esquema.
> Output: input pra grill-review (fase 3).

## Decision tree a resolver

- [ ] A — Granularidade do histórico (evento? conteúdo? o que basta pro resumo da daily)
- [ ] B — Snapshot vs log (persistência entre reinícios vs auditoria)
- [ ] C — Ciclo de vida: quando item some da lista (done/arquivado/stale)
- [ ] D — Tempo: medir? pra quê (worklog no Jira? auditoria de workflow?)
- [ ] E — Interrupção: mensagem que vira trabalho no meio de uma issue
- [ ] F — Retenção/privacidade (dado corporativo em log local)
- [ ] G — Reordenação/prioridade e prazo editável

## Findings

| # | Pergunta | Resolução | Tipo | Origem do gap |
|---|----------|-----------|------|---------------|
| 1 | Resumo da daily precisa de conteúdo de sessão ou só eventos? | **Só eventos (a)**, sem conteúdo, por enquanto. Transcript do Claude fica no disco e pode ser lido sob demanda depois, sem copiar texto pro log. | gap | não havia esquema de log definido — decisão de granularidade inexistente |
| 2 | O que conta como "tempo numa issue" (aba aberta / ativo com corte / foco)? | **Nenhum — análise de tempo cortada do escopo.** Caio não viu utilidade em custo de interrupção, fragmentação, gargalo, worklog. Consequência: log deixa de precisar de batimento por hook e vira **ciclo de vida de work item** (volume baixo). | esclarecimento | — |
| 3 | Resumo da daily: quão detalhado? | **Só o grosso** — lista de itens tocados por dia (hoje/ontem). Sem totais de tempo. Na hora ele lembra do resto. | esclarecimento | — |
| 4 | Guardar histórico de `duedate` (prazo empurrado) é viável? | **Sim, e é barato** — o refetch de status já acontece; comparar `duedate` com o último visto e só logar na mudança. Não é mecanismo novo, é diff no refetch. | gap | plano não tinha política de refetch definida |
| 5 | Análise de tempo: dentro ou fora? (revisão da Q2) | **Dentro** — os 3 (interrupção, fragmentação, gargalo/waiting-vs-tooluse) se triviais. Consequência: volta a precisar de batimento por evento → log de work item + stream de eventos de hook coexistem, join por claude_session_id. | gap | Q2 tinha cortado; revisão trouxe de volta — esquema precisa dos dois logs |
| 6 | Quando work item sai da lista? | **Marcar concluído manual nos 3 tipos.** Sem automação por status Jira (Caio não age em Code Review; QA é eventual). Mensagem: ao concluir, oferecer aviso "avisar a pessoa" **não-bloqueante** (some se ignorado; abre chat/copia rascunho, não envia — só tem Chat.Read). | gap | ciclo de vida não estava no plano; cada tipo tem sinal diferente |
| 7 | events.jsonl grava conteúdo/input de tool? | **Só metadado** — {ts, session_id, event, tool_name}. Sem input. Mantém o log inócuo (planilha de horários se vazar). | esclarecimento | — |
| 8 | Medo: events.jsonl cresce demais e fica lento/ruim de analisar? | **Válido, e resolvido por rotação diária** — arquivo por dia (`events-YYYY-MM-DD.jsonl`). Análise carrega só a janela que precisa (daily = hoje+ontem = 2 arquivos). Sem DB, sem índice. work_log.jsonl não roda (volume ínfimo). | gap | plano não tinha política de retenção/particionamento do log de alto volume |
| 9 | Cache-timer: por aba ou global? | **Por aba, e visível MESMO fora da aba** — cada sessão tem sua janela de 5h; precisa ver o tempo de todas na sidebar/tab sem focar cada uma. Corrige premissa do mockup (timer único no titlebar). Vira badge por aba/sessão; card mostra o menor tempo restante entre suas abas. | gap | mockup assumiu cache global; é por-sessão e é informação de primeira classe |
| 10 | Reordenação de prioridade: global ou por tipo? | **Global** — fila única cruzando tipo (mensagem pode ficar acima de issue). Campo `sort_order` no work item, salvo no snapshot no drop. Sem log de evento (é preferência de view, não fato de trabalho). | esclarecimento | — |
| 11 | Retenção do event log velho: pra sempre / N dias / rollup? | **(B) reter 60 dias, apagar o resto.** Cobre daily (2 dias) e tendência de ~2 meses. events.jsonl particionado por dia → job apaga arquivos > 60 dias. work_log.jsonl e work_items.json NÃO expiram (fatos de trabalho + snapshot). Trade aceito: sem análise de tendência > 60 dias. | gap | plano não tinha política de expiração; decisão do dono contra recomendação (a=pra sempre) |

## Grill-Review

(preenchido na fase 3)

### Causa Raiz dos Gaps
| Cluster | Gaps | Causa Raiz |
|---|---|---|

### Wins
-

### Mudanças estruturais aplicadas
-

### Mudanças adiadas
-
