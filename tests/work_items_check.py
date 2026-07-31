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
from datetime import datetime, timedelta

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
        work_items.set_archived("sid-AAA", True)
        check("4 archived flag", work_items.load_store()["session_links"]["sid-AAA"]["archived"] is True)
        work_items.unlink("sid-AAA")
        check("4 unlink remove", "sid-AAA" not in work_items.load_store()["session_links"])

        # 5. event log: 2 sessões -> 2 arquivos; metadado só
        work_items.log_event("sid-1", "PreToolUse", tool_name="Bash")
        work_items.log_event("sid-2", "Stop")
        files = list(work_items._events_dir().glob("events-*.jsonl"))
        check("5 dois arquivos por sessão", len(files) == 2, str([f.name for f in files]))
        evs = work_items.read_events()
        check("5 metadado só (sem input)", all(set(e.keys()) == {"ts", "session", "event", "tool"} for e in evs))

        # 6. tolera linha corrompida
        f1 = next(f for f in files)
        with open(f1, "a", encoding="utf-8") as fh:
            fh.write('{"ts": 1, "event": "broke"\n')  # truncada
        good = work_items.read_events()
        check("6 pula corrompida, mantém boas", len(good) >= 2)

        # 7. gc_events apaga velho, mantém hoje
        old_day = (datetime.now() - timedelta(days=70)).strftime("%Y-%m-%d")
        old = work_items._events_dir() / f"events-{old_day}-sidX.jsonl"
        old.write_text('{"ts":0,"session":"sidX","event":"Stop","tool":null}\n', encoding="utf-8")
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

        # 10. hook enxerto: write_event grava no event log (metadado + tool)
        hook_state.write_event("sid-hook", "PreToolUse", tool_name="Edit")
        hooked = [e for e in work_items.read_events() if e["session"] == "sid-hook"]
        check("10 write_event grava evento", len(hooked) == 1)
        check("10 tool_name propagado", hooked and hooked[0]["tool"] == "Edit")

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

        # 13. Aguardando é estado do item, não arquivo: esconde suas abas (preserva PTY/cache)
        # e retomar revela as mesmas sessões.
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
        check("13 abas ocultas sem remover vínculo",
              s["session_links"]["sid-wait"]["wi_id"] == waiting["id"]
              and s["session_links"]["sid-wait"]["archived"] is True)

        work_items.set_waiting(waiting["id"], False)
        s = work_items.load_store()
        waiting_now = next(i for i in s["items"] if i["id"] == waiting["id"])
        check("13 retomar volta active",
              waiting_now["workflow_state"] == "active"
              and waiting_now["waiting_since"] is None)
        check("13 retomar revela mesmas abas",
              s["session_links"]["sid-wait"]["archived"] is False)

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

        # 16. bridge expõe o lifecycle sem duplicar regra de negócio no frontend.
        check("16 bridge expõe waiting", hasattr(Bridge, "set_work_item_waiting"))
        check("16 bridge expõe reopen", hasattr(Bridge, "reopen_work_item"))

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

        print(f"\n{_passed} passed, {_failed} failed")
        return 1 if _failed else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
