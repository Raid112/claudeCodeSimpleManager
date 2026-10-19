"""Spike v2: native events only (classic.* does not fire on this org-managed install)."""
import json, os, re, sys, time
import driver as d

ARGS = "--model haiku --permission-mode default"
results: dict = {"S1.env.hello": False}  # run 1: settings.json env overrides the PTY value


def say(k, v):
    results[k] = v
    print(f"{k}: {json.dumps(v, ensure_ascii=False)[:500]}", flush=True)


def dialog_visible(c):
    return bool(re.search(r"Do you want to proceed|1\.\s*Yes", c.tail(2500)))


def evs(since, name):
    with d.ev_lock:
        return [e for e in d.events[since:] if e.get("event") == name]


# ---------- S1/S2 ----------
since = d.mark("S1-flag-spawn")
t_spawn = time.time()
c = d.Claude(ARGS, env_dirs=None, use_flag=True)
d.handle_trust(c)
hello = d.wait_event(lambda e: e["kind"] == "hello", 60, since)
if not hello:
    print(c.tail(3000)); c.kill(); sys.exit(1)
say("S1.flag.hello_after_s", round(hello["rt"] - (t_spawn - d.T0), 2))
say("S2.hello", {k: hello.get(k) for k in ("tab", "dirs", "version", "session_id", "isInteractive", "surface", "_token")})
time.sleep(3)
say("S1.harness_band_visible", bool(re.search(r"semana", c.tail(8000))))

# ---------- S3: plain turn, event order ----------
since = d.mark("S3-plain-turn")
c.submit("Reply with exactly the word PONG and nothing else.")
stop = d.idle(c, 120, since)
with d.ev_lock:
    seq = [(e.get("event") or e["kind"]) for e in d.events[since:] if e["kind"] in ("event", "check")]
say("S3.plain", {"stop": bool(stop), "sequence": seq, "answer": stop and stop.get("text")})

# ---------- S4: permission via tool.check ----------
def perm_round(name, hold, behavior, esc_at=None):
    d.perm_policy.update(hold_s=hold, behavior=behavior)
    since = d.mark(f"S4-{name}")
    fname = f"spike_{name}.txt"
    c.submit(f"Use the Bash tool to run exactly this command and nothing else: touch {fname}")
    req = d.wait_event(lambda e: e["kind"] == "permission", 90, since)
    if not req:
        with d.ev_lock:
            checks = [e.get("verdict") for e in d.events[since:] if e["kind"] == "check"]
        say(f"S4.{name}", {"permission_request": False, "checks": checks, "screen": c.tail(600)})
        d.idle(c, 60, since)
        return
    t_req = time.time()
    seen = False
    while time.time() - t_req < max(hold - 0.5, 1):
        if dialog_visible(c):
            seen = True
            break
        if esc_at is not None and time.time() - t_req >= esc_at:
            c.type("\x1b"); d.mark("S4-esc-during-hold"); esc_at = None
        time.sleep(0.25)
    ans = d.wait_event(lambda e: e["kind"] == "log" and e.get("what") in ("permission.answer", "permission.aborted"), hold + 15, since)
    dialog_after = False
    if behavior is None and esc_at is None and not name.startswith("esc"):
        dialog_after = c.wait_screen(r"Do you want to proceed|1\.\s*Yes", 10)
        if dialog_after:
            c.type("\x1b"); d.mark("S4-esc-dialog")
    stop = d.idle(c, 90, since)
    with d.ev_lock:
        logs = [{k: e.get(k) for k in ("what", "answer", "afterMs", "aborted")} for e in d.events[since:] if e["kind"] == "log" and str(e.get("what", "")).startswith("permission")]
        posts = [e for e in d.events[since:] if e.get("event") == "tool.call.post"]
    say(f"S4.{name}", {
        "dialog_visible_during_hold": seen,
        "dialog_after_hold": dialog_after,
        "hook_logs": logs,
        "file_created": os.path.exists(d.WORK / fname),
        "post": posts and {k: posts[-1].get(k) for k in ("isError", "deny", "ms")},
        "turn_reason": stop and stop.get("reason"),
    })

perm_round("hold-none", 8, None)
perm_round("hold-allow", 6, "allow")
perm_round("hold-deny", 4, "deny")
perm_round("esc-during-hold", 20, None, esc_at=3)
d.perm_policy.update(hold_s=0, behavior=None)
time.sleep(2)

# ---------- S6: prompt.submit ----------
text = ("Linha 1 ação\nLinha 2 — ü 中文 \"aspas\" $HOME `tick`\n\n"
        "Respond with exactly RECEBIDO, then on a new line: if this message carries any attribution, header "
        "or label about who sent it (for example a plugin name), quote it verbatim; otherwise write NONE.")
since = d.mark("S6-idle-submit")
d.inbox.put({"id": "p1", "text": text})
ack = d.wait_event(lambda e: e["kind"] == "ack" and e.get("id") == "p1" and e.get("state") in ("started", "failed"), 60, since)
stop = d.idle(c, 120, since)
ps = evs(since, "prompt.submit")
say("S6.idle", {
    "ack": ack and {k: ack.get(k) for k in ("state", "ms", "err")},
    "prompt_identical": bool(ps) and ps[0].get("text") == text,
    "origin": ps and ps[0].get("origin"),
    "answer": stop and stop.get("text"),
})

since = d.mark("S6-busy-submit")
c.submit("Count from 1 to 80, one number per line, nothing else.")
d.wait_event(lambda e: e.get("event") == "turn.start", 30, since)
time.sleep(1)
d.mark("S6-enqueue-p2")
d.inbox.put({"id": "p2", "text": "Reply with exactly SEGUNDO."})
first = d.idle(c, 120, since)
a2 = d.wait_event(lambda e: e["kind"] == "ack" and e.get("id") == "p2" and e.get("state") == "started", 120, since)
second = d.wait_event(lambda e: e.get("event") == "turn.complete" and e["rt"] > (first or {"rt": 1e9})["rt"], 120, since)
say("S6.busy", {
    "first_done_rt": first and first["rt"], "p2_started_rt": a2 and a2["rt"], "p2_ack_ms": a2 and a2.get("ms"),
    "queued_until_idle": bool(first and a2 and a2["rt"] >= first["rt"] - 0.5),
    "second_answer": second and second.get("text"),
})

big = "x" * 20000 + "\nReply with exactly OK."
since = d.mark("S6-big")
d.inbox.put({"id": "p3", "text": big})
d.idle(c, 120, since)
ps = evs(since, "prompt.submit")
say("S6.big_identical", bool(ps) and ps[0].get("text") == big)

# ---------- S3: /clear ----------
since = d.mark("S3-clear")
sid_before = hello.get("session_id")
c.submit("/clear")
time.sleep(8)
with d.ev_lock:
    hellos = [e for e in d.events[since:] if e["kind"] == "hello"]
    after = [e for e in d.events[since:] if e["kind"] == "event"]
say("S3.clear", {"session_start_again": bool(hellos), "new_session_id": hellos and hellos[0].get("session_id"),
                 "sid_before": sid_before, "events": [(e.get("event"), e.get("command"), e.get("session_id")) for e in after][:6]})
since = d.mark("S3-after-clear-turn")
c.submit("Reply with exactly AFTERCLEAR.")
st = d.idle(c, 90, since)
say("S3.after_clear_session_id", st and st.get("session_id"))

# ---------- S5 ----------
with d.ev_lock:
    ticks = [e for e in d.events if e["kind"] == "log" and e.get("what") == "inbox.tick"]
    errs = [e for e in d.events if e["kind"] == "log" and e.get("what") == "inbox.error"]
say("S5.ticks", {"count": len(ticks), "max_wait_ms": max([t.get("waitedMs", 0) for t in ticks] or [0]), "errors": len(errs)})
say("S5.refusal_lines", re.findall(r"cm-spike:[^\r\n]{0,160}", open(d.OUT / "screen.log", encoding="utf-8").read())[:5])

# ---------- S7 ----------
with d.ev_lock:
    us = [e for e in d.events if e["kind"] == "usage"]
say("S7.usage_count", {"turn.complete": sum(u.get("from") == "turn.complete" for u in us), "session.measure": sum(u.get("from") == "session.measure" for u in us)})

resume_sid = sid_before
c.kill()
time.sleep(3)

# ---------- S1: resume ----------
since = d.mark("S1-resume")
t_spawn = time.time()
c = d.Claude(ARGS + f" --resume {resume_sid}", env_dirs=None, use_flag=True)
h2 = d.wait_event(lambda e: e["kind"] == "hello", 90, since)
say("S1.resume", {"hello_after_s": h2 and round(h2["rt"] - (t_spawn - d.T0), 2), "session_id": h2 and h2.get("session_id"), "expected": resume_sid})
c.kill()

json.dump(results, open(d.OUT / "results.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("DONE")
