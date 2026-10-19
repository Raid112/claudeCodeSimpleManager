"""Fase 0 spike driver: echo server + claude in a real PTY (pywinpty).

Usage: python driver.py <scenario> [--plugin-flag]
Writes events.jsonl (server) and screen.log (stripped PTY output) next to itself.
"""
import json, os, queue, re, sys, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

import winpty

HERE = Path(__file__).resolve().parent
MOD = (HERE / "cm-spike").as_posix()
WORK = HERE / "work"
OUT = HERE / "out"
OUT.mkdir(exist_ok=True)

T0 = time.time()
events: list[dict] = []
ev_lock = threading.Lock()
inbox: "queue.Queue[dict]" = queue.Queue()
perm_policy = {"hold_s": 0.0, "behavior": None}


def rel() -> float:
    return round(time.time() - T0, 2)


def record(kind: str, body: dict):
    rec = {"rt": rel(), "kind": kind, **body}
    with ev_lock:
        events.append(rec)
    with open(OUT / "events.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlsplit(self.path)
        if u.path == "/inbox":
            wait = float(parse_qs(u.query).get("wait", ["0"])[0])
            items = []
            try:
                items.append(inbox.get(timeout=wait))
                while True:
                    items.append(inbox.get_nowait())
            except queue.Empty:
                pass
            if items:
                record("inbox.delivered", {"ids": [i["id"] for i in items]})
            return self._send({"items": items})
        self._send({})

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        body["_token"] = self.headers.get("x-cm-tab-token")
        path = urlsplit(self.path).path.strip("/")
        record(path, body)
        if path == "permission":
            time.sleep(perm_policy["hold_s"])
            return self._send({"behavior": perm_policy["behavior"]})
        self._send({"ok": True})


ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\x1b[@-Z\\-_]")


def _ansi_repl(m):
    t = m.group(0)
    mm = re.fullmatch(r"\[(\d*)C", t)
    if mm:
        return " " * int(mm.group(1) or 1)
    return ""


class Claude:
    def __init__(self, args: str, env_dirs: str | None, use_flag: bool):
        self.pty = winpty.PTY(160, 45)
        env = dict(os.environ)
        env["CM_SPIKE_URL"] = URL
        env["CLAUDEMANAGER_TAB"] = "spike01"
        if env_dirs is not None:
            env["CLAUDE_CODE_PLUGIN_DIRS"] = env_dirs
        cmd = f"claude {args}"
        if use_flag:
            cmd += f' --plugin-dir "{MOD}"'
        block = "\0".join(f"{k}={v}" for k, v in env.items() if not k.startswith("=")) + "\0"
        self.pty.spawn(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
                       cmdline=f"-NoLogo -NoProfile -Command {cmd}", cwd=str(WORK), env=block)
        self.buf = ""
        self.alive = True
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        with open(OUT / "screen.log", "a", encoding="utf-8") as f:
            while self.alive:
                try:
                    d = self.pty.read()
                except Exception:
                    break
                if d:
                    clean = ANSI.sub(_ansi_repl, d)
                    self.buf += clean
                    self.buf = self.buf[-20000:]
                    f.write(f"\n[{rel()}] " + clean)
                    f.flush()
                else:
                    if not self.pty.isalive():
                        break
                    time.sleep(0.02)

    def tail(self, n=3000) -> str:
        return self.buf[-n:]

    def wait_screen(self, pattern: str, timeout: float) -> bool:
        rx = re.compile(pattern, re.I)
        end = time.time() + timeout
        while time.time() < end:
            if rx.search(self.tail(4000)):
                return True
            time.sleep(0.2)
        return False

    def type(self, text: str):
        self.pty.write(text)

    def submit(self, text: str):
        self.pty.write(text)
        time.sleep(0.4)
        self.pty.write("\r")

    def kill(self):
        self.alive = False
        try:
            import subprocess
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.pty.pid)], capture_output=True)
        except Exception:
            pass


def wait_event(pred, timeout: float, since: int = 0):
    end = time.time() + timeout
    while time.time() < end:
        with ev_lock:
            for e in events[since:]:
                if pred(e):
                    return e
        time.sleep(0.1)
    return None


def mark(name: str):
    record("driver", {"step": name})
    with ev_lock:
        return len(events)


def handle_trust(c: Claude):
    if c.wait_screen(r"trust\s*this\s*folder", 25):
        mark("trust-dialog")
        time.sleep(0.5)
        c.type("\x1b[B")
        time.sleep(0.5)
        c.type("\r")


def idle(c: Claude, timeout=180, since=0):
    return wait_event(lambda e: e["kind"] == "event" and e.get("event") == "turn.complete" and not e.get("agentId"), timeout, since)


server = ThreadingHTTPServer(("127.0.0.1", 0), H)
URL = f"http://127.0.0.1:{server.server_address[1]}"
threading.Thread(target=server.serve_forever, daemon=True).start()
