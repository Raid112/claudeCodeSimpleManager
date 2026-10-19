"""
Self-check for terminal/hook_state.py — plain asserts + prints, no framework.
Run: python tests/hook_state_check.py

Redirects the data dir to a fresh temp folder so it never touches real %LOCALAPPDATA%.
Mirrors the *_check.py style from tests/work_items_check.py. Does NOT import
api.bridge — hook_state must stay independent of the bridge/webview layer, and
importing it here would risk touching get_data_dir() before the redirect below.
"""

import os
import sys
import tempfile
import shutil
from pathlib import Path

# import from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="hook_state_check_"))
    os.environ["LOCALAPPDATA"] = str(tmp)  # force all data under tmp, before any import touches it

    from terminal import hook_state, work_items

    print(f"data dir -> {hook_state.get_data_dir()}\n")

    try:
        # 1. write_event persiste session_key no state JSON (keyed por tab_id)
        hook_state.write_event(
            "csid-1", "Stop", tab_id="tab-1", session_key="skey-1")
        state = hook_state.read_state("tab-1")
        check("1 state existe", state is not None)
        check("1 session_key persistido", state and state.get("session_key") == "skey-1")
        check("1 session_id (provider) persistido", state and state.get("session_id") == "csid-1")
        check("1 status derivado", state and state.get("status") == "ready")

        # 2. o event log recebe o registro keyed por session_key, com
        # claude_session_id junto como campo
        hook_state.write_event(
            "csid-2", "PreToolUse", tool_name="Bash", tab_id="tab-2", session_key="skey-2")
        evs = [e for e in work_items.read_events() if e.get("session_key") == "skey-2"]
        check("2 evento gravado sob session_key", len(evs) == 1, str(evs))
        check("2 claude_session_id junto",
              evs and evs[0].get("claude_session_id") == "csid-2")
        check("2 tool_name propagado", evs and evs[0].get("tool") == "Bash")
        files = list(work_items._events_dir().glob("events-*-skey-2.jsonl"))
        check("2 partição nomeada pelo session_key", len(files) == 1, str(files))

        # 3. sessão não-gerenciada (sem CLAUDEMANAGER_SESSION_KEY -> session_key=None)
        # ainda loga algo, e o fallback nunca produz "None" literal no nome do arquivo
        hook_state.write_event("csid-unmanaged", "Stop")  # no tab_id, no session_key
        evs_unmanaged = [e for e in work_items.read_events()
                         if e.get("claude_session_id") == "csid-unmanaged"]
        check("3 fallback ainda loga", len(evs_unmanaged) == 1, str(evs_unmanaged))
        check("3 fallback: session_key == provider session_id",
              evs_unmanaged and evs_unmanaged[0].get("session_key") == "csid-unmanaged")
        all_names = [f.name for f in work_items._events_dir().glob("events-*.jsonl")]
        check("3 nenhum arquivo com 'None' na chave",
              not any("None" in n for n in all_names), str(all_names))

        # 4. evento cujo status derivado é None (hook não mapeado, ex. SessionStart)
        # ainda é logado — a filtragem de status não deve pular o log
        n_before = len(work_items.read_events())
        hook_state.write_event(
            "csid-3", "SessionStart", tab_id="tab-3", session_key="skey-3")
        n_after = len(work_items.read_events())
        check("4 status None não bloqueia o log", n_after == n_before + 1)
        check("4 write_event não grava state p/ evento não mapeado",
              hook_state.read_state("tab-3") is None)

        # 5. write_event nunca levanta para lixo de entrada
        garbage_calls = [
            lambda: hook_state.write_event("", "Stop"),
            lambda: hook_state.write_event(None, "Stop"),
            lambda: hook_state.write_event("csid-4", None),
            lambda: hook_state.write_event("csid-4", ""),
            lambda: hook_state.write_event("csid-5", "Stop", ts="not-a-number"),
            lambda: hook_state.write_event("csid-6", "PreToolUse", tool_name=12345),
            lambda: hook_state.write_event(12345, "Stop"),
        ]
        n_raised = 0
        for i, call in enumerate(garbage_calls):
            try:
                call()
            except Exception as e:
                n_raised += 1
                print(f"  ... garbage call {i} raised: {e!r}")
        check("5 nenhuma chamada com lixo levanta", n_raised == 0)

        # 6. atomic: sem .tmp sobrando
        tmps = list(hook_state.get_data_dir().glob("**/*.tmp"))
        check("6 sem .tmp sobrando", len(tmps) == 0, str([t.name for t in tmps]))

        print(f"\n{_passed} passed, {_failed} failed")
        return 1 if _failed else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
