"""
Self-check for terminal/ui_log.py — plain asserts + prints, no framework.
Run: python tests/ui_log_check.py

Redirects %LOCALAPPDATA% to a temp folder BEFORE importing, so it never writes into the
real log directory. What matters here is the contract the log is built on: it never
raises (a diagnostics failure must not break the app it observes), it keeps the caller's
own timestamp, it caps what one call can write, and old days are dropped.
"""

import os
import sys
import json
import time
import tempfile
import shutil
from pathlib import Path
from datetime import datetime, timedelta

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
    tmp = Path(tempfile.mkdtemp(prefix="ui_log_check_"))
    os.environ["LOCALAPPDATA"] = str(tmp)

    from terminal import ui_log

    print(f"log dir -> {ui_log.log_dir()}\n")
    try:
        today = ui_log.log_dir() / f"ui-{datetime.now().strftime('%Y-%m-%d')}.jsonl"

        # 1. um lote do frontend vira uma linha por registro, com o ts DO EVENTO (não o do
        # flush): o batch chega até 2s depois e o arquivo tem que datar o acontecimento.
        event_ts = time.time() - 1.5
        written = ui_log.append([
            {"event": "fit.suspect", "ts": event_ts, "session": "s1", "cols": 8, "rows": 3},
            {"event": "resize.dropped", "ts": event_ts + 0.2, "session": "s1", "ws": 3},
        ])
        lines = [json.loads(l) for l in today.read_text(encoding="utf-8").splitlines()]
        check("1 escreve uma linha por registro", written == 2 and len(lines) == 2)
        check("1 preserva o ts do evento", abs(lines[0]["ts"] - event_ts) < 0.01)
        check("1 campos livres passam", lines[0]["cols"] == 8 and lines[1]["ws"] == 3)
        check("1 src default = ui", lines[0]["src"] == "ui")

        # 2. o lado Python escreve no MESMO arquivo — é o que permite cruzar o resize que o
        # frontend mandou com o que o PTY aceitou.
        ui_log.log("pty.resize.failed", src="py", session="s1", cols=8, rows=3, error="boom")
        lines = [json.loads(l) for l in today.read_text(encoding="utf-8").splitlines()]
        check("2 py e ui no mesmo arquivo", len(lines) == 3 and lines[2]["src"] == "py")

        # 3. nada de lixo derruba a chamada: registro não-dict é pulado, objeto não
        # serializável vira texto, string gigante é truncada (não descartada).
        before = len(today.read_text(encoding="utf-8").splitlines())
        written = ui_log.append([
            "não é dict",
            {"event": "js.error", "stack": "x" * 5000, "obj": object()},
        ])
        lines = [json.loads(l) for l in today.read_text(encoding="utf-8").splitlines()]
        check("3 registro inválido é pulado", written == 1 and len(lines) == before + 1)
        check("3 string longa é truncada, não perdida",
              len(lines[-1]["stack"]) == ui_log.MAX_FIELD_CHARS)
        check("3 valor não serializável vira texto", isinstance(lines[-1]["obj"], str))

        # 4. um loop no frontend não pode encher o disco numa chamada só.
        flood = [{"event": "fit", "i": i} for i in range(ui_log.MAX_RECORDS + 50)]
        check("4 lote é limitado", ui_log.append(flood) == ui_log.MAX_RECORDS)

        # 5. nunca levanta, mesmo com o diretório impossível de criar.
        raised = ""
        try:
            broken = tmp / "broken"
            broken.write_text("sou um arquivo, não uma pasta", encoding="utf-8")
            os.environ["LOCALAPPDATA"] = str(broken)
            ui_log.append([{"event": "x"}])
            ui_log.log("y")
            ui_log.gc()
        except Exception as exc:
            raised = f"{type(exc).__name__}: {exc}"
        finally:
            os.environ["LOCALAPPDATA"] = str(tmp)
        check("5 nunca levanta com diretório inválido", raised == "", raised)

        # 6. gc guarda a janela e apaga o resto (o nome do arquivo é a data).
        old = ui_log.log_dir() / f"ui-{(datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')}.jsonl"
        old.write_text('{"event":"velho"}\n', encoding="utf-8")
        recent = ui_log.log_dir() / f"ui-{(datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d')}.jsonl"
        recent.write_text('{"event":"recente"}\n', encoding="utf-8")
        ui_log.gc()
        check("6 gc apaga o antigo", not old.exists())
        check("6 gc mantém a janela", recent.exists() and today.exists())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
