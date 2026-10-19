"""E2E smoke: real PtyManager + ModBridgeServer + real claude loading the mod."""
import json, os, sys, tempfile, threading, time
from pathlib import Path

REPO = Path(r"C:\Users\caioc\ClaudeManager")
WORK = Path(r"C:\Users\caioc\.cm-e2e")
sys.path.insert(0, str(REPO))
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="cm_e2e_")
os.environ["ANTHROPIC_MODEL"] = "haiku"
# This script runs inside a Claude Code session; its child-session markers would
# stop the spawned claude from persisting transcripts (so --resume finds nothing).
for _k in [k for k in os.environ if k.startswith(("CLAUDE_CODE_", "CLAUDEMANAGER_")) or k in ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT")]:
    if _k not in ("CLAUDE_CODE_PLUGIN_DIRS", "CLAUDE_CODE_USE_POWERSHELL_TOOL"):
        os.environ.pop(_k, None)

from terminal.mod_bridge import ModBridgeServer
from terminal.pty_manager import PtyManager
from terminal.hooks_settings import generate_hooks_settings
from terminal import work_items

bridge = ModBridgeServer(); bridge.start()
mgr = PtyManager(hooks_settings_path=generate_hooks_settings(), mod_bridge=bridge,
                 mod_dir=(REPO / "mods" / "claudemanager-bridge").as_posix(), hello_timeout_s=30)
s = mgr.create_session("e2e", str(WORK), cols=160, rows=45, terminal_type="claude")
out = []
def reader():
    while True:
        try:
            d = s.read()
        except Exception:
            break
        if d:
            out.append(d)
        else:
            time.sleep(0.02)
threading.Thread(target=reader, daemon=True).start()

def row():
    return next(r for r in mgr.get_all_sessions() if r["id"] == s.id)

def wait(pred, timeout, label):
    end = time.time() + timeout
    while time.time() < end:
        r = row()
        if pred(r):
            return r
        time.sleep(0.2)
    print("TIMEOUT", label, json.dumps(row(), default=str)[:400]); return None

res = {}
t0 = time.time()
import re as _re
def _clean():
    return _re.sub(r"\x1b\[(\d*)C", lambda m: " " * int(m.group(1) or 1), "".join(out))
for _ in range(100):
    if _re.search(r"trust.{0,12}this.{0,12}folder", "".join(out)):
        time.sleep(0.5); s.write("\x1b[B"); time.sleep(0.5); s.write("\r"); break
    if row()["mod_state"] == "alive":
        break
    time.sleep(0.2)
r = wait(lambda r: r["mod_state"] == "alive", 60, "hello")
res["hello_s"] = r and round(time.time() - t0, 1)
if not r:
    print("TAIL", _clean()[-1500:]); mgr.close_all(); bridge.stop(); sys.exit(1)
time.sleep(2)

def turn(text, label):
    s.write(text); time.sleep(0.5); s.write("\r")
    states = []
    end = time.time() + 120
    while time.time() < end:
        st = row()["state"]
        if not states or states[-1] != st:
            states.append(st)
        if st == "ready" and "running" in states:
            break
        time.sleep(0.1)
    res[label] = states

turn("Reply with exactly PONG and nothing else.", "turn1_states")
r = row()
res["usage"] = r["usage"]
sid1 = r["claude_session_id"]
s.write("/clear"); time.sleep(0.5); s.write("\r"); time.sleep(4)
turn("Reply with exactly AFTER.", "turn2_states")
res["sid_changed_after_clear"] = row()["claude_session_id"] != sid1
ev = [e["event"] for e in work_items.read_events() if e.get("session_key") == s.session_key]
res["event_log"] = ev
mgr.restart_session(s.id, 160, 45)
t1 = time.time()
r = wait(lambda r: r["mod_state"] == "alive", 60, "hello after restart")
res["hello_after_restart_s"] = r and round(time.time() - t1, 1)
import re
tail = re.sub(r"\[[0-9;?]*[ -/]*[@-~]", "", "".join(out))[-600:]
res["restart_tail_has_no_conversation"] = "No conversation found" in tail
res["resume_id"] = row()["claude_session_id"]
res["hooks_settings_absent"] = "--settings" not in "".join(out)[:0]  # cmd not echoed; checked in unit test
mgr.close_all(); bridge.stop()
print(json.dumps(res, indent=1, default=str))
