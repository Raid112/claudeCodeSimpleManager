# Work Items Waiting, Deduplication, and Daily Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Keep one prioritized work-item queue while preventing duplicate Jira/Teams items, parking response-dependent work under an `Aguardando` label without losing live terminal cache, and expanding Daily with the smallest useful operational overview.

**Architecture:** `terminal/work_items.py` remains the source of truth. Work items gain a backward-compatible `workflow_state` (`active` or `waiting`) plus `waiting_since`; Teams messages gain a stable `teams:<chat_id>:<msg_id>` external key. Waiting reuses the existing per-session hidden flag to remove tabs from the bar while keeping their PTY processes alive, but the sidebar renders those items in a dedicated compact section instead of `Arquivado`.

**Tech Stack:** Python stdlib data layer, pywebview bridge, vanilla JavaScript, CSS, plain Python/Node assertion tests.

---

### Task 1: Backend work-item lifecycle

**Files:**
- Modify: `terminal/work_items.py`
- Modify: `api/bridge.py`
- Test: `tests/work_items_check.py`

**Steps:**
1. Add failing tests for backward-compatible state defaults, entering waiting, resuming, preserving session processes through hidden link flags, reopening completed items, external-key deduplication, and Daily overview waiting metadata.
2. Run `python tests/work_items_check.py` and verify the new assertions fail because the APIs do not exist.
3. Add `workflow_state`, `waiting_since`, `set_waiting`, `reopen_item`, external-key lookup/deduplication, and `work_overview`.
4. Expose thin bridge methods.
5. Re-run the Python self-check and verify it passes.

### Task 2: Stable Teams message identity

**Files:**
- Modify: `terminal/teams_graph.py`
- Modify: `web/js/workitems.js`
- Test: `tests/teams-search.test.js`

**Steps:**
1. Add a failing Node assertion proving a selected Teams row preserves `chat_id` and `msg_id` and produces a stable external key.
2. Run `node tests/teams-search.test.js` and verify the assertion fails because recent message rows discard `msg_id`.
3. Preserve `msg_id` in `_chat_messages`, `_fetch_recent`, and `_teamsRow`.
4. Reuse an existing work item by source/external key; fall back to normalized person plus message text only for legacy Teams items without a key.
5. For a completed match, ask before reopening; for waiting/archived matches, reactivate and reuse it.
6. Re-run the Node test.

### Task 3: Waiting label and parked terminals

**Files:**
- Modify: `web/js/app.js`
- Modify: `web/js/sidebar.js`
- Modify: `web/js/workitems.js`
- Modify: `web/css/theme.css`
- Modify: `web/index.html`

**Steps:**
1. Add app methods to enter/resume waiting state while moving focus away from tabs that will be hidden.
2. Render normal active work in the existing flat priority list.
3. Render waiting work in a compact `Aguardando` section below it, showing elapsed wait, latest activity, and the minimum live cache remaining.
4. Add an explicit wait action to active cards and a resume action to waiting cards.
5. Keep true archived items under `Arquivado`; exclude waiting links from loose archived sessions.
6. Preserve the current single-list drag priority model and avoid Kanban columns or free-form labels.

### Task 4: Daily 80/20 overview

**Files:**
- Modify: `terminal/work_items.py`
- Modify: `api/bridge.py`
- Modify: `web/js/workitems.js`
- Modify: `web/css/theme.css`
- Test: `tests/work_items_check.py`

**Steps:**
1. Return today/yesterday activity plus counts and waiting items from one backend overview call.
2. Expand the existing Daily overlay with a compact summary strip and `Aguardando` list before the day-by-day activity.
3. Show last activity and waiting duration; do not add time-tracking charts.

### Task 5: Verification and existing-data consolidation

**Files:**
- Test: `tests/work_items_check.py`
- Test: `tests/teams-search.test.js`
- Test: `tests/terminal-input.test.js`
- Test: `tests/session_support_check.py`

**Steps:**
1. Run all four self-checks.
2. Run `python -m compileall -q terminal api main.py`.
3. Run `node --check` on modified JavaScript files.
4. Run `git diff --check`.
5. Inspect the exact duplicate groups in the live `%LOCALAPPDATA%\ClaudeManager\work_items.json`.
6. Before changing live data, create a timestamped backup and consolidate only confirmed duplicate groups, preserving every session link and work-log history.

## Explicitly out of scope

- Kanban columns.
- Arbitrary user-defined labels.
- Full Jira or Teams client behavior.
- Sending Teams messages or Jira comments automatically.
- LLM-generated summaries/checkpoints.
- Time-tracking dashboards or productivity scoring.
