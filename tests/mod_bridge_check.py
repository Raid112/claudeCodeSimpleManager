"""Checks for terminal/mod_bridge.py and its PtyManager wiring (plan
Docs/plans/2026-10-19-ClaudeManagerMod.md, Fases 1-2). Real loopback HTTP
against the bridge, a fake winpty.PTY (no process is spawned) and a temp
%LOCALAPPDATA% so state files and the event log never touch real data.
Run: python tests/mod_bridge_check.py
"""

import base64
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

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


class FakePTY:
    def __init__(self, cols, rows):
        self.alive = True

    pid = None

    def spawn(self, appname, cmdline=None, cwd=None, env=None):
        self.cmdline = cmdline
        self.env = env
        return True

    def isalive(self):
        return self.alive

    def read(self):
        return ""

    def write(self, data):
        return True

    def set_size(self, cols, rows):
        pass


class _FakeWinptyModule:
    PTY = FakePTY


class Recorder:
    def __init__(self):
        self.calls = []

    def on_hello(self, tab_id, body):
        self.calls.append(("hello", tab_id, body))

    def on_event(self, tab_id, body):
        if body.get("event") == "boom":
            raise ValueError("boom")
        self.calls.append(("event", tab_id, body))

    def on_usage(self, tab_id, body):
        self.calls.append(("usage", tab_id, body))


def post(url: str, path: str, body, token: str | None, raw: bytes | None = None) -> int:
    data = raw if raw is not None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    if token is not None:
        req.add_header("X-CM-Tab-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def decode_ps(cmdline: str) -> str:
    return base64.b64decode(cmdline.split("-EncodedCommand ")[1]).decode("utf-16le")


def env_of(pty) -> dict:
    return dict(item.split("=", 1) for item in pty.env.split("\0") if "=" in item)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mod_bridge_check_"))
    os.environ["LOCALAPPDATA"] = str(tmp)

    from terminal.mod_bridge import ModBridgeServer, TabModState, TabTokenStore
    from terminal.session_support import build_resume_command
    from terminal import hermes_events, hook_state, work_items
    import terminal.pty_manager as pty_manager_module
    from terminal.pty_manager import PtyManager

    # 1. token store
    store = TabTokenStore()
    t1 = store.issue("tab-a")
    check("1 issued token resolves to its tab", store.resolve(t1) == "tab-a")
    t2 = store.issue("tab-a")
    check("1 re-issue revokes the previous token", store.resolve(t1) is None and store.resolve(t2) == "tab-a")
    store.revoke("tab-a")
    check("1 revoke drops the tab's token", store.resolve(t2) is None)
    check("1 empty / missing token never resolves", store.resolve("") is None and store.resolve(None) is None)

    # 2. HTTP surface with a recorder sink
    rec = Recorder()
    server = ModBridgeServer(rec)
    server.start()
    try:
        url = server.url
        tok_a = server.tokens.issue("tab-a")
        tok_b = server.tokens.issue("tab-b")
        check("2 no token -> 401", post(url, "/v1/event", {"event": "Stop"}, None) == 401)
        check("2 wrong token -> 401", post(url, "/v1/event", {"event": "Stop"}, "nope") == 401)
        check("2 unknown route -> 404", post(url, "/v1/nope", {}, tok_a) == 404)
        check("2 bad json -> 400", post(url, "/v1/event", None, tok_a, raw=b"{not json") == 400)
        check("2 non-object json -> 400", post(url, "/v1/event", None, tok_a, raw=b"[1,2]") == 400)
        check("2 oversized body -> 413", post(url, "/v1/event", {"x": "y" * 70000}, tok_a) == 413)
        check("2 sink error -> 500", post(url, "/v1/event", {"event": "boom"}, tok_a) == 500)
        rec.calls.clear()
        status = post(url, "/v1/event", {"event": "Stop", "tab": "tab-b"}, tok_a)
        check("2 tab comes from the token, never the body",
              status == 200 and rec.calls == [("event", "tab-a", {"event": "Stop", "tab": "tab-b"})], str(rec.calls))
        post(url, "/v1/hello", {"session_id": "s"}, tok_b)
        post(url, "/v1/usage", {"context": {}}, tok_b)
        check("2 hello and usage route to the sink", [c[0] for c in rec.calls[1:]] == ["hello", "usage"])
    finally:
        server.stop()

    # 3. per-tab mod state
    st = TabModState(hello_timeout_s=30, now=1000.0)
    check("3 pending before the timeout", st.state(now=1010.0) == "pending")
    check("3 missing after the timeout", st.state(now=1031.0) == "missing")
    st.hello = {"ts": 1}
    check("3 alive once it said hello", st.state(now=5000.0) == "alive")
    check("3 newer seq applies", st.accept_seq(1) and st.accept_seq(5))
    check("3 older / repeated seq does not", not st.accept_seq(5) and not st.accept_seq(3))
    check("3 missing seq applies (no ordering info)", st.accept_seq(None))

    # 4. command line
    cmd = build_resume_command("claude", session_id="c-1", resume_session=False, plugin_dir="C:/m/bridge")
    check("4 mod mode loads the mod with --plugin-dir and no --settings",
          cmd == 'claude --session-id c-1 --plugin-dir "C:/m/bridge"', cmd)
    check("4 hooks mode unchanged",
          build_resume_command("claude", hooks_path="h.json") == 'claude --settings "h.json"')

    # 5. PtyManager wiring, end to end over HTTP
    original_winpty = pty_manager_module.winpty
    pty_manager_module.winpty = _FakeWinptyModule
    bridge = ModBridgeServer()
    bridge.start()
    try:
        mgr = PtyManager(hooks_settings_path="C:/hooks.json", mod_bridge=bridge,
                         mod_dir="C:/repo/mods/claudemanager-bridge", hello_timeout_s=30)
        check("5 manager becomes the bridge sink", bridge.sink is mgr)
        s = mgr.create_session("g", str(tmp), terminal_type="claude")
        ps = decode_ps(s.pty.cmdline)
        env = env_of(s.pty)
        check("5 claude tab spawns in mod mode", s.mode == "mod")
        check("5 spawn passes --plugin-dir, not --settings",
              '--plugin-dir "C:/repo/mods/claudemanager-bridge"' in ps and "--settings" not in ps, ps)
        check("5 env carries the bridge url and a live token",
              env.get("CLAUDEMANAGER_BRIDGE_URL") == bridge.url
              and bridge.tokens.resolve(env.get("CLAUDEMANAGER_BRIDGE_TOKEN")) == s.id)
        token = env["CLAUDEMANAGER_BRIDGE_TOKEN"]

        row = lambda: next(r for r in mgr.get_all_sessions() if r["id"] == s.id)
        check("5 mod_state pending before hello", row()["mod_state"] == "pending")
        post(bridge.url, "/v1/hello", {"session_id": s.claude_session_id, "engine_version": "2.1.303"}, token)
        check("5 hello marks the mod alive", row()["mod_state"] == "alive")

        sid = s.claude_session_id
        post(bridge.url, "/v1/event", {"seq": 1, "event": "UserPromptSubmit", "session_id": sid}, token)
        check("5 UserPromptSubmit -> running", row()["state"] == "running")
        post(bridge.url, "/v1/event", {"seq": 2, "event": "PermissionAsk", "session_id": sid, "tool_name": "Bash"}, token)
        check("5 PermissionAsk -> tooluse", row()["state"] == "tooluse")
        post(bridge.url, "/v1/event", {"seq": 4, "event": "Stop", "session_id": sid, "aborted": True}, token)
        r = row()
        check("5 Stop with aborted -> ready + flag", r["state"] == "ready" and r["state_flags"] == {"aborted": True}, str(r))
        post(bridge.url, "/v1/event", {"seq": 3, "event": "PreToolUse", "session_id": sid, "tool_name": "Bash"}, token)
        check("5 a late (older seq) event does not move status back", row()["state"] == "ready")
        logged = [e["event"] for e in work_items.read_events() if e.get("session_key") == s.session_key]
        check("5 ...but it is still in the event log", logged == ["UserPromptSubmit", "PermissionAsk", "Stop", "PreToolUse"], str(logged))
        check("5 unknown event is refused", post(bridge.url, "/v1/event", {"seq": 9, "event": "Rm", "session_id": sid}, token) == 500)

        post(bridge.url, "/v1/event", {"seq": 10, "event": "UserPromptSubmit", "session_id": "swapped-id"}, token)
        check("5 a new session id (after /clear) is followed", row()["claude_session_id"] == "swapped-id")

        post(bridge.url, "/v1/usage", {"context": {"tokens": 5, "window": 10, "percent": 50, "junk": "x"},
                                       "rateLimits": [{"kind": "five_hour", "percentUsed": 52, "resetsAt": "z", "extra": 1}],
                                       "cost": {"usd": 0.1}}, token)
        u = row()["usage"]
        check("5 usage kept, trimmed to known fields",
              u and u["context"] == {"tokens": 5, "window": 10, "percent": 50}
              and u["rate_limits"] == [{"kind": "five_hour", "percentUsed": 52, "resetsAt": "z"}]
              and u["cost_usd"] == 0.1, str(u))

        s.restart(120, 30)
        new_token = env_of(s.pty)["CLAUDEMANAGER_BRIDGE_TOKEN"]
        check("5 restart re-issues the token; the old process can no longer report",
              new_token != token and post(bridge.url, "/v1/event", {"event": "Stop", "session_id": sid}, token) == 401)
        check("5 restart starts the mod state over", row()["mod_state"] == "pending")

        s.restart(120, 30, mode="hooks")
        ps = decode_ps(s.pty.cmdline)
        check("5 restart in hooks mode drops the mod",
              s.mode == "hooks" and "--settings" in ps and "--plugin-dir" not in ps
              and "CLAUDEMANAGER_BRIDGE_TOKEN" not in env_of(s.pty) and row()["mod_state"] is None, ps)

        cx = mgr.create_session("g", str(tmp), terminal_type="codex")
        check("5 codex tab never runs the mod", cx.mode == "hooks" and "--plugin-dir" not in decode_ps(cx.pty.cmdline))

        s2 = mgr.create_session("g", str(tmp), terminal_type="claude")
        tok2 = env_of(s2.pty)["CLAUDEMANAGER_BRIDGE_TOKEN"]
        mgr.close_session(s2.id)
        check("5 closing a tab revokes its token", bridge.tokens.resolve(tok2) is None)

        plain = PtyManager(hooks_settings_path="C:/hooks.json")
        p = plain.create_session("g", str(tmp), terminal_type="claude")
        check("5 no bridge configured -> hooks mode, env unchanged",
              p.mode == "hooks" and "--settings" in decode_ps(p.pty.cmdline)
              and "CLAUDEMANAGER_BRIDGE_URL" not in env_of(p.pty))
    finally:
        pty_manager_module.winpty = original_winpty
        bridge.stop()

    # 6. Hermes mapping of the new events
    norm = hermes_events.normalize_event({"ts": time.time(), "session_key": "k", "event": "PermissionAsk", "tool": "Bash"}, "PreToolUse")
    check("6 PermissionAsk publishes permission_requested", norm and norm["event_type"] == "permission_requested" and norm["status"] == "tooluse")
    check("6 StopFailure is logged only", hermes_events.normalize_event({"ts": time.time(), "session_key": "k", "event": "StopFailure"}, None) is None)
    check("6 StopFailure -> ready in hook_state", hook_state._derive_status("StopFailure", None) == "ready")
    check("6 SessionEnd leaves status untouched", hook_state._derive_status("SessionEnd", "Stop") is None)

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
