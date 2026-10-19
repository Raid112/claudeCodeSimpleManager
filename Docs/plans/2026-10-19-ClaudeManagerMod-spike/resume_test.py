import json, sys, time
import driver as d
sid = sys.argv[1]
since = d.mark("resume")
t = time.time()
c = d.Claude("--model haiku --permission-mode default --resume " + sid, env_dirs=None, use_flag=True)
h = d.wait_event(lambda e: e["kind"] == "hello", 90, since)
print("hello_after_s", h and round(h["rt"] - (t - d.T0), 2), "sid", h and h.get("session_id"))
c.submit("Reply with exactly RESUMED.")
st = d.idle(c, 90, since)
print("turn", st and st.get("text"), st and st.get("session_id"))
c.kill()
