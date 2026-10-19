"""
Self-check for terminal/hermes_events.py — plain asserts + prints, no framework.
Run: python tests/hermes_events_check.py

Redirects %LOCALAPPDATA% and the agent-decisions journal dir to fresh temp folders
so it never touches real ClaudeManager data. Uses a stdlib http.server on port 0 as
a fake Hermes gateway — NEVER touches the real webhook port 8644. Mirrors the
*_check.py style from tests/work_items_check.py / tests/hook_state_check.py.
"""

import os
import sys
import json
import time
import uuid
import shutil
import hashlib
import hmac as hmac_lib
import tempfile
import threading
import subprocess
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


SECRET = "sekrit-test-value"


class _FakeHermes(BaseHTTPRequestHandler):
    """Stdlib fake Hermes gateway. Class-level so the test can steer it between
    checks: `responses` is a queue of (status, body_dict) consumed one per POST
    (falls back to 202 accepted once empty); `health_status` toggles /health;
    `received` records every POST for signature/body inspection."""
    responses: list = []
    health_status = "ok"
    received: list = []

    def log_message(self, *_a):
        return

    def do_GET(self):
        if self.path == "/health":
            if _FakeHermes.health_status == "not_ready":
                self._send(503, {"status": "starting"})
            else:
                self._send(200, {"status": "ok", "platform": "webhook"})
        else:
            self._send(404, {"error": "not_found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        _FakeHermes.received.append({"path": self.path, "headers": dict(self.headers), "body": body})
        if _FakeHermes.responses:
            status, resp_body = _FakeHermes.responses.pop(0)
        else:
            status, resp_body = 202, {"status": "accepted"}
        self._send(status, resp_body)

    def _send(self, status, body):
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except OSError:
            pass


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="hermes_events_check_"))
    os.environ["LOCALAPPDATA"] = str(tmp)  # before any import touches get_data_dir()
    journal_dir = tmp / "agent-decisions"
    os.environ["CLAUDEMANAGER_AGENT_DECISIONS_DIR"] = str(journal_dir)

    from terminal import hermes_events, work_items, agent_decisions

    print(f"data dir -> {work_items.get_data_dir()}")
    print(f"journal dir -> {journal_dir}\n")

    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHermes)
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    port = server.server_address[1]
    webhook_url = f"http://127.0.0.1:{port}/webhooks/claudemanager-events"
    health_url = f"http://127.0.0.1:{port}/health"

    NOW = time.time()

    try:
        # 0. cursor ausente (primeiro start) começa em "agora", não em 0/None — senão
        # o primeiro start replaya até 60 dias de partições retidas (read_events
        # tolera since=None). Precisa rodar antes de qualquer tick() criar o arquivo.
        fresh_cursor = hermes_events._load_cursor()
        check("0 cursor ausente começa em 'agora'", abs(fresh_cursor["last_ts"] - NOW) < 5,
              str(fresh_cursor))

        # 1. tabela de mapeamento
        def rec(event, ts, tool=None, session_key="skey-1", claude_sid="csid-1"):
            return {"ts": ts, "session_key": session_key, "event": event, "tool": tool,
                     "claude_session_id": claude_sid}

        r = hermes_events.normalize_event(rec("Stop", 100.0), None)
        check("1 Stop -> task_completed/ready, publicado",
              r is not None and r["event_type"] == "task_completed" and r["status"] == "ready", str(r))

        r = hermes_events.normalize_event(rec("Notification", 101.0), "PreToolUse")
        check("1 Notification após PreToolUse -> permission_requested/tooluse",
              r is not None and r["event_type"] == "permission_requested" and r["status"] == "tooluse", str(r))

        r = hermes_events.normalize_event(rec("Notification", 102.0), "Stop")
        check("1 Notification após Stop -> logged only (None)", r is None, str(r))

        for ev in ("UserPromptSubmit", "PreToolUse", "PostToolUse"):
            r = hermes_events.normalize_event(rec(ev, 103.0), None)
            check(f"1 {ev} -> logged only (None)", r is None, str(r))

        r = hermes_events.normalize_event(rec(hermes_events.PROCESS_EXITED_EVENT, 104.0), None)
        check("1 ProcessExited -> process_exited/exited, publicado",
              r is not None and r["event_type"] == "process_exited" and r["status"] == "exited"
              and r["source"] == "pty_process", str(r))

        r = hermes_events.normalize_event(
            {"ts": 105.0, "session_key": None, "event": "Stop", "tool": None, "claude_session_id": "sidX"}, None)
        check("1 session_key None (registro pré-rekey) nunca publica", r is None, str(r))

        # 2. event_id determinístico e sensível a cada campo
        id_a = hermes_events.event_id_for("skey-1", "Stop", 100.0, None)
        id_a2 = hermes_events.event_id_for("skey-1", "Stop", 100.0, None)
        check("2 event_id determinístico", id_a == id_a2)
        variants = [
            hermes_events.event_id_for("skey-2", "Stop", 100.0, None),
            hermes_events.event_id_for("skey-1", "Notification", 100.0, None),
            hermes_events.event_id_for("skey-1", "Stop", 100.5, None),
            hermes_events.event_id_for("skey-1", "Stop", 100.0, "Bash"),
        ]
        check("2 muda com cada campo isoladamente", all(v != id_a for v in variants), str(variants))

        # 3. vetor HMAC independente (não usa o mesmo helper duas vezes)
        body_v = b'{"a":1}'
        ts_v = "1700000000"
        expected_sig = hmac_lib.new(SECRET.encode(), ts_v.encode() + b"." + body_v, hashlib.sha256).hexdigest()
        got_sig = hermes_events.sign_v2(SECRET, ts_v, body_v)
        check("3 HMAC V2 bate com vetor calculado independentemente", got_sig == expected_sig,
              f"{got_sig} != {expected_sig}")

        # 4. campos opcionais são "" e nunca None
        r = hermes_events.normalize_event(
            {"ts": 200.0, "session_key": "skey-4", "event": "Stop", "tool": None, "claude_session_id": None}, None)
        check("4 tool_name vazio, não None", r["tool_name"] == "")
        check("4 claude_session_id vazio, não None", r["claude_session_id"] == "")
        check("4 work_item_id vazio", r["work_item_id"] == "")
        check("4 last_hermes_write_age_s vazio por padrão (sem journal)", r["last_hermes_write_age_s"] == "")
        check("4 nenhum campo é None no payload", all(v is not None for v in r.values()), str(r))

        # 5. cursor: persistência + resume sem duplicar nem pular entre registros com
        # o mesmo ts. Publisher "1" processa; publisher "2" simula um restart lendo
        # o cursor persistido; um evento novo no mesmo ts pós-resume não é perdido.
        T5 = NOW + 1000
        work_items.log_event("skey-cursor-a", "Stop", ts=T5)
        work_items.log_event("skey-cursor-b", "Stop", ts=T5)

        pub1 = hermes_events.HermesEventsPublisher(
            SECRET, webhook_url=webhook_url, health_url=health_url, timeout_s=2.0)
        pub1.tick()
        n_after_first = len(_FakeHermes.received)
        check("5 primeiro tick publica os 2 eventos no mesmo ts", n_after_first == 2, str(n_after_first))

        first_req = _FakeHermes.received[0]
        sig_hdr = first_req["headers"].get("X-Webhook-Signature-V2")
        ts_hdr = first_req["headers"].get("X-Webhook-Timestamp")
        expected = hmac_lib.new(SECRET.encode(), ts_hdr.encode() + b"." + first_req["body"], hashlib.sha256).hexdigest()
        check("5 assinatura de um POST real bate com HMAC recalculado à parte",
              sig_hdr == expected, f"{sig_hdr} != {expected}")
        check("5 rota é a esperada", first_req["path"] == "/webhooks/claudemanager-events")
        body_json = json.loads(first_req["body"])
        check("5 corpo é um evento único achatado (event_type no topo)",
              isinstance(body_json, dict) and "event_type" in body_json, str(body_json))

        pub2 = hermes_events.HermesEventsPublisher(  # simula restart: relê o cursor do disco
            SECRET, webhook_url=webhook_url, health_url=health_url, timeout_s=2.0)
        pub2.tick()
        check("5 resume não duplica eventos já publicados",
              len(_FakeHermes.received) == n_after_first, str(len(_FakeHermes.received)))

        work_items.log_event("skey-cursor-c", "Stop", ts=T5)  # novo evento no MESMO ts pós-resume
        pub2.tick()
        check("5 evento novo no mesmo ts (pós-resume) não é pulado",
              len(_FakeHermes.received) == n_after_first + 1, str(len(_FakeHermes.received)))

        # 6. probe de saúde não pronto -> nada é consumido (cursor intacto, sem POST)
        T6 = NOW + 2000
        work_items.log_event("skey-notready", "Stop", ts=T6 + 1)
        _FakeHermes.health_status = "not_ready"
        n_before = len(_FakeHermes.received)
        pub3 = hermes_events.HermesEventsPublisher(
            SECRET, webhook_url=webhook_url, health_url=health_url, timeout_s=2.0)
        pub3._cursor = {"last_ts": T6, "recent_ids": {}}
        pub3.tick()
        check("6 health não pronto: não publica", len(_FakeHermes.received) == n_before)
        check("6 health não pronto: cursor intacto", pub3._cursor["last_ts"] == T6)
        check("6 health não pronto: entra em backing_off", pub3.status()["state"] == "backing_off")
        _FakeHermes.health_status = "ok"

        # 7. tabela código de resposta -> ação
        T7 = NOW + 3000
        work_items.log_event("skey-202", "Stop", ts=T7 + 1)
        pub4 = hermes_events.HermesEventsPublisher(
            SECRET, webhook_url=webhook_url, health_url=health_url, timeout_s=2.0)

        pub4._cursor = {"last_ts": T7, "recent_ids": {}}
        _FakeHermes.responses = [(202, {"status": "accepted"})]
        pub4.tick()
        check("7 202 avança o cursor", pub4._cursor["last_ts"] == T7 + 1)
        check("7 202 conta em published", pub4.status()["counters"]["published"] >= 1)

        pub4._cursor = {"last_ts": T7, "recent_ids": {}}
        _FakeHermes.responses = [(429, {"error": "rate_limited"})]
        pub4.tick()
        check("7 429 (rate limit): cursor não avança", pub4._cursor["last_ts"] == T7)
        check("7 429: entra em backing_off", pub4.status()["state"] == "backing_off")

        pub4._cursor = {"last_ts": T7, "recent_ids": {}}
        pub4._current_backoff_s = 0.0
        _FakeHermes.responses = [(502, {"error": "delivery_failed"})]
        pub4.tick()
        check("7 502 (deliver_only falhou): cursor não avança", pub4._cursor["last_ts"] == T7)

        dead = hermes_events.HermesEventsPublisher(  # conexão recusada: webhook morto, health ok
            SECRET, webhook_url="http://127.0.0.1:1/webhooks/x", health_url=health_url, timeout_s=1.0)
        dead._cursor = {"last_ts": T7, "recent_ids": {}}
        dead.tick()
        check("7 conexão recusada: cursor não avança", dead._cursor["last_ts"] == T7)
        check("7 conexão recusada: entra em backing_off", dead.status()["state"] == "backing_off")

        pub4._cursor = {"last_ts": T7, "recent_ids": {}}
        pub4._current_backoff_s = 0.0
        _FakeHermes.responses = [(400, {"error": "invalid"})]
        pub4.tick()
        check("7 400: pula o evento ruim e avança mesmo assim", pub4._cursor["last_ts"] == T7 + 1)
        check("7 400: conta em skipped_bad", pub4.status()["counters"]["skipped_bad"] >= 1)

        pub4._cursor = {"last_ts": T7, "recent_ids": {}}
        _FakeHermes.responses = [(401, {"error": "unauthorized"})]
        pub4.tick()
        st = pub4.status()
        check("7 401: desabilita com motivo machine-readable",
              st["state"] == "disabled" and st["reason"] == "auth_rejected", str(st))
        n_before = len(_FakeHermes.received)
        pub4.tick()
        check("7 desabilitado: não tenta mais nada em ticks seguintes",
              len(_FakeHermes.received) == n_before)

        # 8. campos de recursion-guard, via journal sintético mínimo (sem passar por
        # create_proposal — só os dois eventos que o guard lê).
        decision_id = str(uuid.uuid4())
        agent_decisions._append_event("proposal_created", {
            "decision_id": decision_id,
            "action": {"target": {"session_key": "skey-guard"}},
        })
        agent_decisions._append_event("execution_result", {
            "decision_id": decision_id, "request_id": "req-1", "state": "SENT", "status": "executed",
        })
        write_moment = time.time()  # ~ o ts real que _append_event acabou de gravar

        pub_guard = hermes_events.HermesEventsPublisher(
            SECRET, webhook_url=webhook_url, health_url=health_url)
        pub_guard._refresh_journal_cache()
        check("8 journal cache carregou", pub_guard._journal_cache_ok is True)

        # read_audit_events (terminal/agent_decisions.py) é o acessor público que
        # _refresh_journal_cache usa agora em vez de tocar o lock privado --
        # checado aqui, antes da corrupção deliberada do journal mais abaixo.
        audit_events = agent_decisions.read_audit_events()
        check("8 read_audit_events é lista e enxerga proposal_created",
              isinstance(audit_events, list)
              and any(ev.get("kind") == "proposal_created" for ev in audit_events),
              str(audit_events[:1]))

        ev_recent = hermes_events.normalize_event(
            {"ts": write_moment + 30, "session_key": "skey-guard", "event": "Stop",
             "tool": None, "claude_session_id": "c"}, None)
        pub_guard._stamp_guard_fields(ev_recent)
        check("8 write recente (30s) -> hermes_initiated True",
              ev_recent["hermes_initiated"] is True
              and isinstance(ev_recent["last_hermes_write_age_s"], int)
              and ev_recent["last_hermes_write_age_s"] < hermes_events.HERMES_INITIATED_THRESHOLD_S,
              str(ev_recent))

        ev_old = hermes_events.normalize_event(
            {"ts": write_moment + 300, "session_key": "skey-guard", "event": "Stop",
             "tool": None, "claude_session_id": "c"}, None)
        pub_guard._stamp_guard_fields(ev_old)  # cache ainda fresco (TTL), não relê o journal
        check("8 write antigo (300s) -> hermes_initiated False",
              ev_old["hermes_initiated"] is False
              and ev_old["last_hermes_write_age_s"] >= hermes_events.HERMES_INITIATED_THRESHOLD_S,
              str(ev_old))

        ev_before_write = hermes_events.normalize_event(
            {"ts": write_moment - 10, "session_key": "skey-guard", "event": "Stop",
             "tool": None, "claude_session_id": "c"}, None)
        pub_guard._stamp_guard_fields(ev_before_write)
        check("8 write posterior ao evento -> False, idade vazia (não negativa)",
              ev_before_write["hermes_initiated"] is False and ev_before_write["last_hermes_write_age_s"] == "",
              str(ev_before_write))

        journal_files = list(journal_dir.glob("events-*.jsonl"))
        check("8 fixture: arquivo de journal existe", len(journal_files) >= 1, str(journal_files))
        with open(journal_files[0], "a", encoding="utf-8") as f:
            f.write("not json at all\n")  # corrompe a cadeia de hash
        pub_guard2 = hermes_events.HermesEventsPublisher(
            SECRET, webhook_url=webhook_url, health_url=health_url)
        pub_guard2._refresh_journal_cache()
        check("8 journal corrompido -> cache marcado inválido", pub_guard2._journal_cache_ok is False)
        ev_after_corrupt = hermes_events.normalize_event(
            {"ts": write_moment + 30, "session_key": "skey-guard", "event": "Stop",
             "tool": None, "claude_session_id": "c"}, None)
        pub_guard2._stamp_guard_fields(ev_after_corrupt)
        check("8 journal corrompido: degrada com segurança (False, idade vazia)",
              ev_after_corrupt["hermes_initiated"] is False and ev_after_corrupt["last_hermes_write_age_s"] == "",
              str(ev_after_corrupt))

        # 9. publisher sobrevive a uma exceção do normalizer num registro e segue
        T9 = NOW + 4000
        work_items.log_event("skey-boom", "Stop", ts=T9 + 1)
        work_items.log_event("skey-fine", "Stop", ts=T9 + 2)
        _FakeHermes.responses = []

        orig_normalize = hermes_events.normalize_event

        def _boom(record, prev_event):
            if record.get("session_key") == "skey-boom":
                raise RuntimeError("normalizer kaboom")
            return orig_normalize(record, prev_event)

        hermes_events.normalize_event = _boom
        try:
            pub5 = hermes_events.HermesEventsPublisher(
                SECRET, webhook_url=webhook_url, health_url=health_url, timeout_s=2.0)
            pub5._cursor = {"last_ts": T9, "recent_ids": {}}
            n_before = len(_FakeHermes.received)
            pub5.tick()  # não deve levantar
            check("9 tick não propaga exceção do normalizer", True)
            check("9 evento após o que quebrou ainda foi publicado",
                  any(json.loads(r["body"]).get("session_key") == "skey-fine"
                      for r in _FakeHermes.received[n_before:]))
            check("9 cursor avança passando pelo registro quebrado", pub5._cursor["last_ts"] == T9 + 2)
        finally:
            hermes_events.normalize_event = orig_normalize

        # 10. script de filtro do lado Hermes, como subprocesso isolado, "HERMES_HOME"
        # simulado sob tmp — nunca toca no HERMES_HOME real.
        filter_script = Path(__file__).resolve().parent.parent / \
            "integrations" / "hermes" / "claudemanager_event_filter.py"
        fake_home = tmp / "fake_hermes_home"
        fake_scripts = fake_home / "scripts"
        fake_scripts.mkdir(parents=True, exist_ok=True)
        deployed = fake_scripts / "claudemanager_event_filter.py"
        shutil.copy(filter_script, deployed)

        def run_filter(payload: dict):
            proc = subprocess.run(
                [sys.executable, str(deployed)],
                input=json.dumps(payload).encode("utf-8"),
                capture_output=True, timeout=10)
            return proc.returncode, proc.stdout.decode("utf-8", "replace").strip()

        p1 = {"event_id": "evid-1", "session_key": "skey-f1", "event_type": "task_completed"}
        rc, out = run_filter(p1)
        check("10 primeiro evento passa", rc == 0 and out != "[SILENT]" and json.loads(out)["event_id"] == "evid-1", out)

        rc, out = run_filter(p1)
        check("10 event_id duplicado -> [SILENT]", rc == 0 and out == "[SILENT]", out)

        p2 = {"event_id": "evid-2", "session_key": "skey-f1", "event_type": "task_completed"}
        rc, out = run_filter(p2)
        check("10 cooldown por sessão+tipo -> [SILENT]", rc == 0 and out == "[SILENT]", out)

        dedup_state = fake_home / "claudemanager_event_dedup.json"
        dedup_state.write_text("{not valid json", encoding="utf-8")
        p3 = {"event_id": "evid-3", "session_key": "skey-f2", "event_type": "process_exited"}
        rc, out = run_filter(p3)
        check("10 state corrompido -> passa (fail open)",
              rc == 0 and out != "[SILENT]" and json.loads(out)["event_id"] == "evid-3", out)

        # 11. work_item_id join (Task 4 passo 3): session_links carregado uma vez
        # por batch (_refresh_session_links_cache), resolve_work_item_id aplicado
        # por evento (_stamp_work_item_id) -- mesma função pura que a rota
        # GET /v1/events usa.
        work_items.link("skey-wi-1", "wi_test123")
        pub_wi = hermes_events.HermesEventsPublisher(
            SECRET, webhook_url=webhook_url, health_url=health_url, timeout_s=2.0)
        pub_wi._refresh_session_links_cache()
        check("11 session_links cache carregado", "skey-wi-1" in pub_wi._session_links_cache)

        ev_wi = hermes_events.normalize_event(
            {"ts": time.time(), "session_key": "skey-wi-1", "event": "Stop",
             "tool": None, "claude_session_id": None}, None)
        pub_wi._stamp_work_item_id(ev_wi)
        check("11 work_item_id resolvido via session_key", ev_wi["work_item_id"] == "wi_test123", str(ev_wi))

        ev_wi_unknown = hermes_events.normalize_event(
            {"ts": time.time(), "session_key": "skey-sem-link", "event": "Stop",
             "tool": None, "claude_session_id": None}, None)
        pub_wi._stamp_work_item_id(ev_wi_unknown)
        check("11 sessão sem link -> work_item_id ''", ev_wi_unknown["work_item_id"] == "", str(ev_wi_unknown))

        # 11b. fim a fim via tick(): o corpo publicado já carrega o work_item_id.
        T11 = NOW + 5000
        work_items.log_event("skey-wi-1", "Stop", ts=T11 + 1)
        pub_wi._cursor = {"last_ts": T11, "recent_ids": {}}
        _FakeHermes.responses = []
        n_before = len(_FakeHermes.received)
        pub_wi.tick()
        check("11b tick publica o evento", len(_FakeHermes.received) == n_before + 1)
        if len(_FakeHermes.received) > n_before:
            body = json.loads(_FakeHermes.received[-1]["body"])
            check("11b corpo publicado carrega work_item_id", body.get("work_item_id") == "wi_test123", str(body))

        print(f"\n{_passed} passed, {_failed} failed")
        return 1 if _failed else 0
    finally:
        server.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
