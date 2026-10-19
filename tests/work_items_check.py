"""
Self-check for terminal/work_items.py — plain asserts + prints, no framework.
Run: python tests/work_items_check.py

Redirects the data dir to a fresh temp folder so it never touches real %LOCALAPPDATA%.
Mirrors the *_check.py style from teams-copilot.
"""

import os
import sys
import json
import time
import tempfile
import shutil
from pathlib import Path
from datetime import datetime, timedelta, timezone

# import from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from terminal import hook_state, work_items
from api.bridge import Bridge

_passed = 0
_failed = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {name}")
    else:
        _failed += 1
        print(f"  FAIL {name}  {extra}")


def _read_work_log() -> list[dict]:
    p = work_items._work_log_path()
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="wi_check_"))
    # force all data under tmp
    os.environ["LOCALAPPDATA"] = str(tmp)
    print(f"data dir -> {work_items.get_data_dir()}\n")

    try:
        # 1. snapshot roundtrip + sort_order grows + version preserved
        a = work_items.new_item("manual", "primeiro todo")
        b = work_items.new_item("manual", "segundo todo")
        store = work_items.load_store()
        check("1 roundtrip: 2 items", len(store["items"]) == 2)
        check("1 sort_order cresce", b["sort_order"] > a["sort_order"])
        check("1 version preservada", store["version"] == work_items.SCHEMA_VERSION)

        # 2. complete
        work_items.complete_item(a["id"])
        it = next(i for i in work_items.list_items() if i["id"] == a["id"])
        check("2 done=True", it["done"] is True and it["closed_at"] is not None)
        check("2 loga complete", any(r["kind"] == "complete" and r.get("wi") == a["id"]
                                     for r in _read_work_log()))

        # 3. reorder não loga
        log_before = len(_read_work_log())
        work_items.reorder([b["id"], a["id"]])
        s = work_items.load_store()
        oa = next(i for i in s["items"] if i["id"] == a["id"])["sort_order"]
        ob = next(i for i in s["items"] if i["id"] == b["id"])["sort_order"]
        check("3 reorder aplica", ob < oa)
        check("3 reorder NÃO loga", len(_read_work_log()) == log_before)

        # 4. link durável em session_links (não sessions.json)
        work_items.link("sid-AAA", b["id"])
        s = work_items.load_store()
        check("4 link em session_links", s["session_links"]["sid-AAA"]["wi_id"] == b["id"])
        check("4 link sem estado próprio de aba", "archived" not in s["session_links"]["sid-AAA"])
        work_items.unlink("sid-AAA")
        check("4 unlink remove", "sid-AAA" not in work_items.load_store()["session_links"])

        # 5. event log: 2 sessões -> 2 arquivos; metadado só; keyed por session_key,
        # claude_session_id junto como campo (Task 4: rekey do event log)
        work_items.log_event("sid-1", "PreToolUse", tool_name="Bash", claude_session_id="csid-1")
        work_items.log_event("sid-2", "Stop")
        files = list(work_items._events_dir().glob("events-*.jsonl"))
        check("5 dois arquivos por sessão", len(files) == 2, str([f.name for f in files]))
        evs = work_items.read_events()
        check("5 metadado só (sem input)",
              all(set(e.keys()) == {"ts", "session_key", "event", "tool", "claude_session_id"}
                  for e in evs))
        check("5 session_key vira nome do arquivo",
              any(f.name.endswith("-sid-1.jsonl") for f in files))
        check("5 claude_session_id propagado",
              any(e["session_key"] == "sid-1" and e["claude_session_id"] == "csid-1" for e in evs))

        # 6. tolera linha corrompida
        f1 = next(f for f in files)
        with open(f1, "a", encoding="utf-8") as fh:
            fh.write('{"ts": 1, "event": "broke"\n')  # truncada
        good = work_items.read_events()
        check("6 pula corrompida, mantém boas", len(good) >= 2)

        # 7. registro de formato antigo ({session}, sem session_key) ainda é lido e
        # normalizado: session vira claude_session_id, session_key fica None
        old_day = (datetime.now() - timedelta(days=70)).strftime("%Y-%m-%d")
        old = work_items._events_dir() / f"events-{old_day}-sidX.jsonl"
        old.write_text('{"ts":0,"session":"sidX","event":"Stop","tool":null}\n', encoding="utf-8")
        old_rec = next(e for e in work_items.read_events() if e.get("claude_session_id") == "sidX")
        check("7 formato antigo normaliza p/ claude_session_id",
              old_rec["claude_session_id"] == "sidX" and old_rec["session_key"] is None)

        # gc_events apaga partição velha (nome de arquivo continua events-YYYY-MM-DD-<chave>,
        # a chave agora é semanticamente session_key mas o parsing do nome não mudou)
        work_items.gc_events(max_age_days=60)
        check("7 gc apaga > 60d", not old.exists())
        check("7 gc mantém hoje", len(list(work_items._events_dir().glob("events-*.jsonl"))) >= 2)

        # 8. jira diff: seed impede falso-diff no 1º refresh; muda status -> loga
        j = work_items.new_item("jira", "issue jira", external_key="DS-201", status="To Do", duedate="2026-07-21")
        n_before = len(_read_work_log())
        work_items.apply_jira_snapshot(j["id"], "To Do", "2026-07-21")  # igual ao semeado
        check("8 1º refresh igual NÃO loga", len(_read_work_log()) == n_before)
        work_items.apply_jira_snapshot(j["id"], "In Review", "2026-07-21")
        check("8 status novo loga", any(r["kind"] == "status" and r.get("wi") == j["id"]
                                        for r in _read_work_log()))

        # 9. duedate empurrado
        work_items.apply_jira_snapshot(j["id"], "In Review", "2026-07-27")
        check("9 duedate empurrado loga from/to",
              any(r["kind"] == "duedate" and r.get("detail", {}).get("to") == "2026-07-27"
                  for r in _read_work_log()))

        # 10. hook enxerto: write_event grava no event log (metadado + tool). Sem
        # session_key (chamada sem CLAUDEMANAGER_SESSION_KEY), cai no fallback:
        # chave = session_id, e claude_session_id = mesmo valor.
        hook_state.write_event("sid-hook", "PreToolUse", tool_name="Edit")
        hooked = [e for e in work_items.read_events() if e["session_key"] == "sid-hook"]
        check("10 write_event grava evento", len(hooked) == 1)
        check("10 tool_name propagado", hooked and hooked[0]["tool"] == "Edit")
        check("10 fallback: claude_session_id == session_key sem CLAUDEMANAGER_SESSION_KEY",
              hooked and hooked[0]["claude_session_id"] == "sid-hook")

        # 11. atomic: sem .tmp sobrando
        tmps = list(work_items.get_data_dir().glob("*.tmp"))
        check("11 sem .tmp sobrando", len(tmps) == 0, str([t.name for t in tmps]))

        # 12. external_key identifica a origem: selecionar a mesma issue/mensagem reutiliza
        # o work item em vez de criar uma segunda cópia.
        before_dedup = len(work_items.list_items())
        same_j = work_items.new_item(
            "jira", "issue jira repetida", external_key="DS-201", status="In Review")
        check("12 external_key reutiliza item", same_j["id"] == j["id"])
        check("12 external_key não duplica snapshot",
              len(work_items.list_items()) == before_dedup)

        # 13. Aguardando é o ÚNICO jeito de parar um item (não existe mais "arquivar item").
        # É estado do item e nada mais: os vínculos das abas ficam intactos — quem esconde as
        # abas é o render (sidebar/tab bar), não um flag copiado pra dentro de session_links.
        waiting = work_items.new_item(
            "teams", "esperando resposta", external_key="teams:chat-1:msg-1")
        work_items.link("sid-wait", waiting["id"], name="investigação")
        wait_ts = time.time() - 60
        work_items.set_waiting(waiting["id"], True, ts=wait_ts)
        s = work_items.load_store()
        waiting_now = next(i for i in s["items"] if i["id"] == waiting["id"])
        check("13 workflow_state=waiting",
              waiting_now["workflow_state"] == "waiting"
              and waiting_now["waiting_since"] == wait_ts)
        check("13 aguardar não escreve em session_links",
              s["session_links"]["sid-wait"]["wi_id"] == waiting["id"]
              and "archived" not in s["session_links"]["sid-wait"]
              and "archived_before_waiting" not in s["session_links"]["sid-wait"])

        work_items.set_waiting(waiting["id"], False)
        s = work_items.load_store()
        waiting_now = next(i for i in s["items"] if i["id"] == waiting["id"])
        check("13 retomar volta active",
              waiting_now["workflow_state"] == "active"
              and waiting_now["waiting_since"] is None)
        check("13 retomar mantém o vínculo das abas",
              s["session_links"]["sid-wait"]["wi_id"] == waiting["id"])

        # 13b. Migração: item com o antigo `archived` vira waiting na leitura, e o antigo
        # estado de aba escondida some (aba segue a task).
        legacy = work_items.new_item("manual", "item legado arquivado")
        work_items.link("sid-legacy", legacy["id"], name="aba legada")
        raw = work_items.load_store()
        next(i for i in raw["items"] if i["id"] == legacy["id"])["archived"] = True
        raw["session_links"]["sid-legacy"]["archived"] = True
        raw["session_links"]["sid-legacy"]["archived_before_waiting"] = False
        work_items.save_store(raw)
        migrated = work_items.load_store()
        legacy_now = next(i for i in migrated["items"] if i["id"] == legacy["id"])
        check("13b archived legado vira waiting",
              legacy_now["workflow_state"] == "waiting"
              and "archived" not in legacy_now
              and legacy_now["waiting_since"])
        check("13b flag de aba escondida some",
              "archived" not in migrated["session_links"]["sid-legacy"]
              and "archived_before_waiting" not in migrated["session_links"]["sid-legacy"])
        work_items.save_store(migrated)
        check("13b migração é idempotente",
              next(i for i in work_items.load_store()["items"]
                   if i["id"] == legacy["id"])["workflow_state"] == "waiting")
        work_items.complete_item(legacy["id"])
        done_now = next(i for i in work_items.load_store()["items"] if i["id"] == legacy["id"])
        check("13c concluir item aguardando limpa o waiting",
              done_now["done"] and done_now["workflow_state"] == "active"
              and done_now["waiting_since"] is None)

        # 13d. Store ilegível: leitor recebe vazio, mas nenhum mutador grava esse vazio por cima.
        store_file = work_items._store_path()
        good = store_file.read_text(encoding="utf-8")
        store_file.write_text("{quebrado", encoding="utf-8")
        try:
            work_items.new_item("manual", "não pode gravar")
            blocked = False
        except OSError:
            blocked = True
        check("13d save recusa sobrescrever store ilegível",
              blocked and store_file.read_text(encoding="utf-8") == "{quebrado")
        store_file.write_text(good, encoding="utf-8")
        check("13d sem .tmp sobrando", not list(store_file.parent.glob("work_items.*.tmp")))

        # 13c. Volta sozinho: um item teams parado acorda quando a OUTRA pessoa responde depois
        # do waiting_since. Mensagem anterior ao waiting (a que criou o item) não acorda nada,
        # e item sem chat teams não é candidato.
        awake = work_items.new_item("teams", "pedi revisão", external_key="teams:chat-9:msg-1")
        manual_parked = work_items.new_item("manual", "todo parado")
        park_ts = time.time() - 300
        work_items.set_waiting(awake["id"], True, ts=park_ts)
        work_items.set_waiting(manual_parked["id"], True, ts=park_ts)
        iso = lambda offset: datetime.fromtimestamp(park_ts + offset, timezone.utc).isoformat()
        old_only = [{"chat_id": "chat-9", "messages": [{"sender_name": "Ana", "text": "oi", "ts": iso(-30)}]}]
        check("13c mensagem anterior ao waiting não acorda",
              work_items.wake_on_reply(old_only) == [])
        woken = work_items.wake_on_reply([
            {"chat_id": "chat-9", "messages": [
                {"sender_name": "Ana", "text": "respondi aqui", "ts": iso(60)},
                {"sender_name": "Ana", "text": "oi", "ts": iso(-30)},
            ]},
        ])
        check("13c resposta depois do waiting acorda o item",
              [i["id"] for i in woken] == [awake["id"]])
        awake_now = next(i for i in work_items.list_items() if i["id"] == awake["id"])
        check("13c item acordado volta active com autoria",
              awake_now["workflow_state"] == "active"
              and awake_now["waiting_since"] is None
              and awake_now["woke_by"] == "Ana")
        check("13c item manual parado não acorda",
              next(i for i in work_items.list_items()
                   if i["id"] == manual_parked["id"])["workflow_state"] == "waiting")
        check("13c acordar é idempotente (item já ativo não é candidato)",
              work_items.wake_on_reply([
                  {"chat_id": "chat-9", "messages": [
                      {"sender_name": "Ana", "text": "de novo", "ts": iso(120)}]}]) == [])
        work_items.ack_wake(awake["id"])
        check("13c ack limpa o aviso sem mexer no estado",
              "woke_at" not in next(i for i in work_items.list_items() if i["id"] == awake["id"]))

        # 13d. Graph às vezes entrega o horário sem offset. Ler isso como hora LOCAL joga a
        # resposta 3h pro passado (UTC-3) e o item nunca acordaria — é UTC, sempre.
        naive = work_items.new_item("teams", "sem offset", external_key="teams:chat-tz:msg-1")
        park_naive = time.time() - 600
        work_items.set_waiting(naive["id"], True, ts=park_naive)
        bare = datetime.fromtimestamp(park_naive + 120, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")
        woke_naive = work_items.wake_on_reply([
            {"chat_id": "chat-tz", "messages": [{"sender_name": "Bruno", "text": "voltei", "ts": bare}]}])
        check("13d ts sem timezone é lido como UTC",
              [i["id"] for i in woke_naive] == [naive["id"]])
        check("13d ts ilegível não acorda nada",
              work_items.wake_on_reply([
                  {"chat_id": "chat-9", "messages": [{"sender_name": "X", "text": "?", "ts": "ontem"}]}]) == [])
        work_items.complete_item(naive["id"])

        # 13e. O store inteiro é reescrito a cada save: concluir um item enquanto o wake roda
        # não pode fazer o snapshot velho do wake ressuscitá-lo (_serialized/_MUTEX).
        import threading as _threading
        racer = work_items.new_item("teams", "corrida", external_key="teams:chat-race:msg-1")
        work_items.set_waiting(racer["id"], True, ts=time.time() - 60)
        others = [work_items.new_item("manual", f"vizinho {i}") for i in range(3)]
        chats = [{"chat_id": "chat-race",
                  "messages": [{"sender_name": "Dani", "text": "respondi",
                                "ts": datetime.now(timezone.utc).isoformat()}]}]
        barrier = _threading.Barrier(2)

        def _wake():
            barrier.wait()
            for _ in range(20):
                work_items.wake_on_reply(chats)

        def _complete():
            barrier.wait()
            for it in others:
                work_items.complete_item(it["id"])

        threads = [_threading.Thread(target=_wake), _threading.Thread(target=_complete)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        final = {i["id"]: i for i in work_items.list_items()}
        check("13e concluir durante o wake não é perdido",
              all(final[it["id"]]["done"] for it in others))
        check("13e o wake também sobrevive à corrida",
              final[racer["id"]]["workflow_state"] == "active")
        work_items.complete_item(racer["id"])
        work_items.complete_item(awake["id"])
        work_items.complete_item(manual_parked["id"])

        # 14. item concluído pode ser reaberto para a mesma origem sem duplicação.
        work_items.complete_item(waiting["id"])
        work_items.reopen_item(waiting["id"])
        reopened = next(i for i in work_items.list_items() if i["id"] == waiting["id"])
        check("14 reopen restaura item",
              reopened["done"] is False and reopened["closed_at"] is None
              and reopened["workflow_state"] == "active")

        # 15. Daily 80/20 inclui o que está esperando e sua atividade mais recente.
        work_items.set_waiting(waiting["id"], True, ts=wait_ts)
        event_ts = wait_ts + 20
        work_items.log_event("sid-wait", "Stop", ts=event_ts)
        overview = work_items.work_overview(days=2)
        waiting_row = next((i for i in overview["waiting"]
                            if i["wi_id"] == waiting["id"]), None)
        check("15 overview preserva daily", len(overview["days"]) == 2)
        check("15 overview conta estados",
              overview["counts"]["waiting"] == 1
              and overview["counts"]["active"] >= 1)
        check("15 waiting mostra desde + última atividade",
              waiting_row is not None
              and waiting_row["waiting_since"] == wait_ts
              and waiting_row["last_activity"] >= event_ts)

        # 15b. regressão do work_overview join (Task 4 passo 4): session_links pode
        # estar keyed por session_key OU por claude_session_id (ver sessionLinkKeyOf
        # em app.js: abas claude preferem claude_session_id). O join precisa tentar
        # os dois campos normalizados do evento, não só session_key. ts no futuro +
        # igualdade exata: nada do work_log (create/link/wait, todos com ts~=now)
        # pode alcançar esse valor, então só o loop de read_events prova o join.
        past = time.time() - 3600
        future = time.time() + 600

        # (a) link keyed por session_key (provider não-claude)
        wi_a = work_items.new_item("manual", "join via session_key", ts=past)
        work_items.set_waiting(wi_a["id"], True, ts=past)
        work_items.link("skey-a", wi_a["id"])
        work_items.log_event("skey-a", "Stop", ts=future)
        row_a = next((i for i in work_items.work_overview(days=2)["waiting"]
                      if i["wi_id"] == wi_a["id"]), None)
        check("15b join via session_key", row_a is not None and row_a["last_activity"] == future)

        # (b) link keyed por claude_session_id (claude terminal); o evento carrega um
        # session_key DIFERENTE — só resolve pelo fallback claude_session_id. Sem o
        # `or session_to_wi.get(rec.get("claude_session_id"))` no join, isto falha.
        wi_b = work_items.new_item("manual", "join via claude_session_id", ts=past)
        work_items.set_waiting(wi_b["id"], True, ts=past)
        work_items.link("csid-b", wi_b["id"])
        work_items.log_event("skey-b-different", "Stop", ts=future, claude_session_id="csid-b")
        row_b = next((i for i in work_items.work_overview(days=2)["waiting"]
                      if i["wi_id"] == wi_b["id"]), None)
        check("15b join via claude_session_id (fallback)",
              row_b is not None and row_b["last_activity"] == future)

        # 16. bridge expõe o lifecycle sem duplicar regra de negócio no frontend.
        check("16 bridge expõe waiting", hasattr(Bridge, "set_work_item_waiting"))
        check("16 bridge expõe reopen", hasattr(Bridge, "reopen_work_item"))
        check("16 bridge expõe o wake (poll + ack)",
              hasattr(Bridge, "poll_wake") and hasattr(Bridge, "ack_work_item_wake"))
        check("16 não existe mais arquivar item", not hasattr(Bridge, "archive_work_item"))

        # 17. item consolidado continua no histórico, mas a Daily resolve suas ações
        # para o canônico em vez de exibir duas linhas iguais.
        merged = work_items.new_item("manual", "cópia consolidada")
        s = work_items.load_store()
        merged_row = next(i for i in s["items"] if i["id"] == merged["id"])
        merged_row["merged_into"] = waiting["id"]
        work_items.save_store(s)
        work_items.log_action("complete", wi=merged["id"])
        today = work_items.daily_digest(1)[0]["items"]
        check("17 daily não mostra id consolidado",
              all(i["wi_id"] != merged["id"] for i in today))
        canonical_daily = next((i for i in today if i["wi_id"] == waiting["id"]), None)
        check("17 histórico consolidado cai no canônico",
              canonical_daily is not None and "complete" in canonical_daily["kinds"])

        # 18. resolve_work_item_id (Task 4 join helper shared by GET /v1/events
        # and the Hermes publisher): tries session_key first, falls back to
        # claude_session_id, degrades to "" -- never raises on garbage input.
        work_items.link("skey-join-a", j["id"])
        links = work_items.load_store()["session_links"]
        check("18 resolves via session_key",
              work_items.resolve_work_item_id(links, "skey-join-a", None) == j["id"])
        check("18 falls back to claude_session_id when session_key misses",
              work_items.resolve_work_item_id(links, "no-such-key", "skey-join-a") == j["id"])
        check("18 session_key wins when both would match (checked via priority, not collision)",
              work_items.resolve_work_item_id(links, "skey-join-a", "does-not-exist") == j["id"])
        check("18 unknown identity degrades to ''",
              work_items.resolve_work_item_id(links, "nope", "nope-either") == "")
        check("18 both identities missing/falsy -> ''",
              work_items.resolve_work_item_id(links, None, None) == "")
        check("18 non-dict session_links never raises",
              work_items.resolve_work_item_id(None, "skey-join-a", None) == "")

        # ---- 16: read_events skips partitions whose day cannot match ----
        # A tailing caller (the wake-up publisher, every 10s) must not re-parse the
        # whole retained log. Correctness first: the filter may never be the reason
        # an event disappears, including for a filename it cannot parse.
        now = time.time()
        work_items.log_event("skey-part-old", "Stop", ts=now - 86400 * 5)
        work_items.log_event("skey-part-new", "Stop", ts=now)
        tail_keys = [r["session_key"] for r in work_items.read_events(since=now - 60)
                     if str(r.get("session_key", "")).startswith("skey-part-")]
        check("16 tail lê só a partição do dia em janela", tail_keys == ["skey-part-new"], tail_keys)

        all_keys = {r["session_key"] for r in work_items.read_events()
                    if str(r.get("session_key", "")).startswith("skey-part-")}
        check("16 sem since lê todas as partições",
              all_keys == {"skey-part-old", "skey-part-new"}, all_keys)

        past = [r["session_key"] for r in
                work_items.read_events(since=now - 86400 * 6, until=now - 86400 * 4)
                if str(r.get("session_key", "")).startswith("skey-part-")]
        check("16 janela só no passado exclui a de hoje", past == ["skey-part-old"], past)

        weird = work_items._events_dir() / "events-nao-uma-data.jsonl"
        weird.write_text(json.dumps({
            "ts": now, "session_key": "skey-part-weird", "event": "Stop", "tool": None,
        }) + "\n", encoding="utf-8")
        check("16 nome de partição inválido é INCLUÍDO, nunca descartado",
              any(r.get("session_key") == "skey-part-weird"
                  for r in work_items.read_events(since=now - 60)))

        print(f"\n{_passed} passed, {_failed} failed")
        return 1 if _failed else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
