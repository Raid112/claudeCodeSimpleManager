"""
Self-check for Bridge.work_item_agent_state() (Task 6) — plain asserts + prints,
no framework. Run: python tests/workitem_agent_state_check.py

Redirects both the work-items data dir and the agent-decisions journal dir to
fresh temp folders so this never touches the real %LOCALAPPDATA%\\ClaudeManager
store (see CLAUDEMANAGER_AGENT_DECISIONS_DIR in terminal/agent_decisions.py).
Mirrors the *_check.py style used by work_items_check.py / approval_flow_check.py.
"""

import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.bridge import Bridge
from terminal import agent_decisions, work_items, hermes_events
from terminal.agent_contracts import proposal_hash

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


class FakePtyManager:
    """work_item_agent_state only needs find_by_session_key for the fallback
    path (session_links miss -> live session's claude_session_id); every
    session_link in this test is looked up directly, so this never gets
    called with a hit, matching how the fixture pty_manager in
    list_pending_agent_decisions' own tests degrades."""

    def find_by_session_key(self, _key):
        return None


def make_bridge() -> Bridge:
    bridge = Bridge.__new__(Bridge)
    bridge.pty_manager = FakePtyManager()
    return bridge


def iso(offset=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=offset)).isoformat()


def make_proposal(session_key: str, text: str = "do the thing", expires_in: int = 300) -> dict:
    """Minimal valid send_prompt proposal targeting `session_key` (copies the
    shape approval_flow_check.py's make_proposal already proves valid)."""
    action = {
        "type": "send_prompt",
        "target": {"session_key": session_key},
        "parameters": {"text": text},
        "expected_outcome": "One managed semantic action",
        "reversible": True,
    }
    payload = {
        "decision_id": str(uuid.uuid4()), "parent_decision_id": None,
        "trace_id": str(uuid.uuid4()), "version": 1,
        "context_refs": [{"source": "teams", "object_id": "chat:msg", "retrieved_at": iso(),
                           "content_hash": "c" * 64, "trust": "external_data"}],
        "intent": "test proposal",
        "action": action,
        "risk": {"level": "low", "factors": []},
        "policy": {"version": "1", "decision": "approval_required", "constraints": []},
        "model": {"provider": "openai-codex", "name": "gpt-5.6-luna", "prompt_version": "v1",
                  "attestation": {"hermes_profile": "test", "hermes_version": "0.19.0",
                                  "config_hash": "d" * 64, "session_id": "hermes-1"}},
        "idempotency_key": str(uuid.uuid4()), "created_at": iso(),
        "expires_at": iso(expires_in),
    }
    payload["proposal_hash"] = proposal_hash(payload)
    return payload


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="wi_agent_state_check_"))
    os.environ["LOCALAPPDATA"] = str(tmp)
    decisions_dir = Path(tempfile.mkdtemp(prefix="wi_agent_state_decisions_"))
    os.environ["CLAUDEMANAGER_AGENT_DECISIONS_DIR"] = str(decisions_dir)
    print(f"data dir -> {work_items.get_data_dir()}")
    print(f"decisions dir -> {decisions_dir}\n")

    bridge = make_bridge()

    try:
        # ---- 0. nothing linked, nothing proposed: empty, not an error ----
        check("0 vazio quando nada linkado", bridge.work_item_agent_state() == {})

        # ---- 1. events only: a full hook sequence for one linked session ----
        wi1 = work_items.new_item("manual", "item com eventos")
        work_items.link("sess-a", wi1["id"])
        t0 = 1_800_000_000.0
        work_items.log_event("sess-a", "UserPromptSubmit", ts=t0)
        work_items.log_event("sess-a", "PreToolUse", tool_name="Bash", ts=t0 + 1)
        work_items.log_event("sess-a", "Notification", ts=t0 + 2)       # -> permission_requested
        work_items.log_event("sess-a", "PostToolUse", tool_name="Bash", ts=t0 + 3)
        work_items.log_event("sess-a", "Stop", ts=t0 + 4)               # -> task_completed
        work_items.log_event("sess-a", hermes_events.PROCESS_EXITED_EVENT, ts=t0 + 5)  # -> process_exited

        state = bridge.work_item_agent_state()
        check("1 item aparece no resultado", wi1["id"] in state)
        events1 = state.get(wi1["id"], {}).get("events", {})
        check("1 task_completed presente", events1.get("task_completed", {}).get("ts") == t0 + 4)
        check("1 task_completed status=ready", events1.get("task_completed", {}).get("status") == "ready")
        check("1 permission_requested presente", events1.get("permission_requested", {}).get("ts") == t0 + 2)
        check("1 permission_requested status=tooluse",
              events1.get("permission_requested", {}).get("status") == "tooluse")
        check("1 process_exited presente", events1.get("process_exited", {}).get("ts") == t0 + 5)
        check("1 process_exited status=exited", events1.get("process_exited", {}).get("status") == "exited")
        check("1 sem pendência (nada proposto ainda)", state[wi1["id"]]["pending_reasons"] == [])

        # ---- 2. pending approval: a fresh send_prompt proposal on the same session ----
        proposal1 = make_proposal("sess-a")
        agent_decisions.create_proposal(proposal1)
        state = bridge.work_item_agent_state()
        check("2 pending_reasons=awaiting_approval",
              state[wi1["id"]]["pending_reasons"] == ["awaiting_approval"])
        # events from step 1 must still be there, untouched by the new proposal
        check("2 eventos preservados", state[wi1["id"]]["events"] == events1)

        # ---- 2b. a "waiting" item still shows its pending approval ----
        # set_waiting() archives EVERY session_link on the item (terminal/work_items.py
        # set_waiting), and the "waiting" list in openDaily() is exactly where this
        # matters most: resolving the pending side must NOT use the archived-sensitive
        # _resolve_work_item join, or a parked item's pending approval goes invisible
        # while its events (resolved via the archived-insensitive resolve_work_item_id)
        # keep showing up -- the same item silently disagreeing with itself.
        work_items.set_waiting(wi1["id"], True)
        state = bridge.work_item_agent_state()
        check("2b pendencia sobrevive a set_waiting (link fica arquivado)",
              state[wi1["id"]]["pending_reasons"] == ["awaiting_approval"])
        check("2b eventos tambem sobrevivem a set_waiting",
              state[wi1["id"]]["events"] == events1)
        work_items.set_waiting(wi1["id"], False)  # undo, keep the rest of the script linear

        # ---- 3. awaiting_dispatch: approved, but no execution ever recorded ----
        wi2 = work_items.new_item("manual", "item so com pendencia")
        work_items.link("sess-b", wi2["id"])
        proposal2 = make_proposal("sess-b")
        agent_decisions.create_proposal(proposal2)
        agent_decisions.approve(proposal2["decision_id"], proposal2["proposal_hash"],
                                "tester", "any-nonempty-capability")
        state = bridge.work_item_agent_state()
        check("3 pending_reasons=awaiting_dispatch",
              state[wi2["id"]]["pending_reasons"] == ["awaiting_dispatch"])
        check("3 sem eventos (nada logado pra sess-b)", state[wi2["id"]]["events"] == {})

        # ---- 4. an item with neither link nor proposal never shows up ----
        wi3 = work_items.new_item("manual", "item solto")
        state = bridge.work_item_agent_state()
        check("4 item sem dados nao aparece", wi3["id"] not in state)

        # ---- 5. never raises even if the agent-decisions journal is unreadable ----
        real_list_pending = agent_decisions.list_pending_decisions
        agent_decisions.list_pending_decisions = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            state = bridge.work_item_agent_state()
            check("5 nao propaga excecao do journal", True)
            check("5 eventos seguem funcionando mesmo com pending quebrado",
                  state.get(wi1["id"], {}).get("events", {}).get("task_completed") is not None)
            check("5 pending degrada pra vazio", state.get(wi1["id"], {}).get("pending_reasons") == [])
        finally:
            agent_decisions.list_pending_decisions = real_list_pending

    finally:
        pass

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
