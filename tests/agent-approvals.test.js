/**
 * Prompt-first approval UI (agent-approvals.js).
 *
 * There is no real browser here (no build step, no npm, no jsdom dependency),
 * so this is a minimal hand-rolled DOM stub -- just enough of
 * createElement/append/remove/contains/textContent/activeElement/getSelection
 * to exercise the incremental render, selection-suspend, and receipt logic for
 * real, in the style of tests/teams-search.test.js's stubbed-DOM smoke test.
 *
 * Covers (see .hermes/plans/20260803_133016-claude-manager-hermes-approval-events.md
 * Task 3 step 11):
 *   1. incremental render keeps the same node for an unchanged decision_id;
 *   2. a card holding a selection is not patched by a poll-triggered render;
 *   3. each receipt state renders, and only retryable:true shows Reenviar;
 *   4. a pinned WRITE_FAILED receipt survives the decision leaving the pending
 *      list, and a retry that then succeeds replaces it cleanly;
 *   5. HOST_WRITE_ACCEPTED auto-dismisses;
 *   6. accents and line breaks survive round-trip;
 *   7. a very long prompt is not truncated.
 */

const assert = require('node:assert/strict');

// ---------- minimal DOM stub ----------
class FakeNode {
    constructor(tag) {
        this.tagName = tag;
        this.children = [];
        this.parentNode = null;
        this._text = '';
        this.className = '';
        this.hidden = false;
        this.disabled = false;
        this.dataset = {};
        this.tabIndex = -1;
        this.type = '';
        this._listeners = {};
    }

    get textContent() { return this._text; }
    set textContent(value) {
        this._text = value == null ? '' : String(value);
        for (const child of this.children) child.parentNode = null;
        this.children = [];
    }

    get childNodes() { return this.children; }
    get firstChild() { return this.children[0] || null; }
    get lastChild() { return this.children[this.children.length - 1] || null; }

    appendChild(child) {
        child.parentNode = this;
        this.children.push(child);
        return child;
    }

    append(...nodes) {
        for (const node of nodes) this.appendChild(node);
    }

    remove() {
        if (this.parentNode) {
            const idx = this.parentNode.children.indexOf(this);
            if (idx >= 0) this.parentNode.children.splice(idx, 1);
        }
        this.parentNode = null;
    }

    contains(node) {
        let cur = node;
        while (cur) {
            if (cur === this) return true;
            cur = cur.parentNode;
        }
        return false;
    }

    addEventListener(type, fn) {
        (this._listeners[type] = this._listeners[type] || []).push(fn);
    }

    dispatch(type) {
        for (const fn of this._listeners[type] || []) fn({});
    }

    focus() { global.document.activeElement = this; }
}

function makeDocument() {
    return {
        activeElement: null,
        _selection: null,
        createElement: (tag) => new FakeNode(tag),
        getSelection() {
            return this._selection || { isCollapsed: true, anchorNode: null };
        },
        body: new FakeNode('body'),
    };
}

global.document = makeDocument();
global.window = { pywebview: { api: {} }, prompt: () => 'motivo de teste' };
global.navigator = {};

const { AgentApprovals, aaReceiptLabel, aaReasonLabel } = require('../web/js/agent-approvals.js');

// ---------- fixtures ----------
function makeDecision(overrides = {}) {
    return Object.assign({
        decision_id: 'd-default',
        proposal_hash: 'h'.repeat(64),
        pending_reason: 'awaiting_approval',
        prompt_text: 'Prompt de teste',
        prompt_length: 15,
        intent: 'Discovery read-only',
        expected_outcome: 'Relatório do problema',
        risk_level: 'low',
        expires_at: '2026-01-01T00:00:00Z',
        version: 1,
        parent_decision_id: null,
        trace_id: 't-1',
        session_key: 'session-1',
        session_label: 'clones #abcd',
        work_item_id: 'wi-1',
        work_item_title: 'New Seeds',
        operator_capability: 'cap-1',
        raw: {},
    }, overrides);
}

function api() { return global.window.pywebview.api; }

function resetApi() {
    global.window.pywebview.api = {
        list_pending_agent_decisions: async () => [],
        get_agent_control_state: async () => ({ emergency_stopped: false }),
        approve_agent_proposal: async () => { throw new Error('not stubbed'); },
        retry_prompt_dispatch: async () => { throw new Error('not stubbed'); },
        reject_agent_proposal: async () => ({ status: 'replan_requested' }),
        verify_proposal_hash: async () => (
            { found: true, hash_ok: true, text_ok: true, prompt_length: 0, proposal_hash: 'x' }),
        get_emergency_capability: async () => 'emergency-cap',
        emergency_stop: async () => true,
        unlock_emergency_stop: async () => true,
        get_hermes_publisher_status: async () => ({ state: 'running', reason: null }),
    };
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

(async () => {
    resetApi();
    global.document.activeElement = null;
    global.document._selection = null;
    const ui = new AgentApprovals(null, new FakeNode('section'));

    // ---------- 1) incremental render keeps the same node ----------
    api().list_pending_agent_decisions = async () => [makeDecision({ decision_id: 'unchanged' })];
    await ui.refresh();
    const nodeFirst = ui._cardNodes.get('unchanged');
    assert.ok(nodeFirst, 'card must exist after first refresh');
    await ui.refresh();
    const nodeSecond = ui._cardNodes.get('unchanged');
    assert.strictEqual(nodeFirst, nodeSecond, 'the same decision_id must reuse the same DOM node');

    // ---------- 2) a card holding a selection is not patched ----------
    api().list_pending_agent_decisions = async () => [
        makeDecision({ decision_id: 'selected', prompt_text: 'texto original' }),
    ];
    await ui.refresh();
    const selectedNode = ui._cardNodes.get('selected');
    assert.equal(selectedNode.refs.prompt.textContent, 'texto original');

    global.document._selection = { isCollapsed: false, anchorNode: selectedNode.refs.prompt };
    api().list_pending_agent_decisions = async () => [
        makeDecision({ decision_id: 'selected', prompt_text: 'texto mudou durante a selecao' }),
    ];
    await ui.refresh();
    assert.equal(selectedNode.refs.prompt.textContent, 'texto original',
        'a poll-triggered render must not patch a card with an active selection inside it');

    // A merely-collapsed caret (no real selection) must NOT suspend the patch.
    global.document._selection = { isCollapsed: true, anchorNode: selectedNode.refs.prompt };
    await ui.refresh();
    assert.equal(selectedNode.refs.prompt.textContent, 'texto mudou durante a selecao',
        'a collapsed caret must not suspend the patch, only a real selection range');
    global.document._selection = null;

    // ---------- 3) receipt states + retryable gates the Reenviar button ----------
    const receiptCase = async (decisionId, receipt) => {
        api().list_pending_agent_decisions = async () => [makeDecision({ decision_id: decisionId })];
        await ui.refresh();
        api().approve_agent_proposal = async () => receipt;
        await ui._approve(decisionId);
        return ui._cardNodes.get(decisionId);
    };

    let node = await receiptCase('accepted', {
        decision_id: 'accepted', request_id: 'r-1', state: 'HOST_WRITE_ACCEPTED',
        retryable: false, execution: { state: 'SENT' },
    });
    assert.equal(node.refs.receiptBox.hidden, false);
    assert.equal(node.refs.actionsBox.hidden, true, 'approve/reject must hide once dispatched');
    assert.equal(node.refs.retryBtn.hidden, true, 'HOST_WRITE_ACCEPTED is never retryable');
    assert.equal(node.refs.receiptLabel.textContent, aaReceiptLabel({ state: 'HOST_WRITE_ACCEPTED' }));

    node = await receiptCase('unknown-with-execution', {
        decision_id: 'unknown-with-execution', request_id: 'r-2', state: 'PROVIDER_PROCESSING_UNKNOWN',
        retryable: false, execution: { state: 'UNKNOWN' },
    });
    assert.equal(node.refs.retryBtn.hidden, true, 'PROVIDER_PROCESSING_UNKNOWN (execution shape) is not retryable');

    node = await receiptCase('unknown-with-reason', {
        decision_id: 'unknown-with-reason', request_id: 'r-3', state: 'PROVIDER_PROCESSING_UNKNOWN',
        retryable: false, reason: 'unexpected_error',
    });
    assert.equal(node.refs.retryBtn.hidden, true, 'PROVIDER_PROCESSING_UNKNOWN (reason shape) is not retryable');
    assert.equal(node.refs.receiptReason.textContent, `Motivo: ${aaReasonLabel('unexpected_error')}`);

    node = await receiptCase('write-failed-retryable', {
        decision_id: 'write-failed-retryable', request_id: 'r-4', state: 'WRITE_FAILED',
        retryable: true, reason: 'session_not_found',
    });
    assert.equal(node.refs.retryBtn.hidden, false, 'WRITE_FAILED with retryable:true must show Reenviar');

    node = await receiptCase('write-failed-not-retryable', {
        decision_id: 'write-failed-not-retryable', request_id: 'r-5', state: 'WRITE_FAILED',
        retryable: false, reason: 'proposal_expired',
    });
    assert.equal(node.refs.retryBtn.hidden, true,
        'WRITE_FAILED with retryable:false must NOT show Reenviar, regardless of reason');

    // ---------- 4) pinned failure survives disappearance, then retry replaces it ----------
    api().list_pending_agent_decisions = async () => [makeDecision({ decision_id: 'pinned' })];
    await ui.refresh();
    api().approve_agent_proposal = async () => ({
        decision_id: 'pinned', request_id: 'r-6', state: 'WRITE_FAILED',
        retryable: true, reason: 'session_not_found',
    });
    await ui._approve('pinned');
    assert.ok(ui._cardNodes.has('pinned'));

    api().list_pending_agent_decisions = async () => []; // decision no longer pending
    await ui.refresh();
    assert.ok(ui._cardNodes.has('pinned'),
        'a pinned WRITE_FAILED receipt must survive the decision leaving the pending list');
    assert.equal(ui._cardNodes.get('pinned').refs.receiptBox.hidden, false);

    ui.autoDismissMs = 5;
    api().retry_prompt_dispatch = async () => ({
        decision_id: 'pinned', request_id: 'r-6', state: 'HOST_WRITE_ACCEPTED',
        retryable: false, execution: { state: 'SENT' },
    });
    await ui._retry('pinned');
    assert.equal(ui._receipts.get('pinned').state, 'HOST_WRITE_ACCEPTED',
        'retry must replace the pinned receipt with the new one');
    await sleep(20);
    assert.ok(!ui._receipts.has('pinned'), 'the NEW receipt auto-dismisses on its own timer');
    assert.ok(!ui._cardNodes.has('pinned'), 'card is gone once the receipt clears and it is not in `decisions`');

    // ---------- 5) HOST_WRITE_ACCEPTED auto-dismisses ----------
    ui.autoDismissMs = 5;
    api().list_pending_agent_decisions = async () => [makeDecision({ decision_id: 'accepted-2' })];
    await ui.refresh();
    api().approve_agent_proposal = async () => ({
        decision_id: 'accepted-2', request_id: 'r-7', state: 'HOST_WRITE_ACCEPTED',
        retryable: false, execution: { state: 'SENT' },
    });
    await ui._approve('accepted-2');
    assert.ok(ui._receipts.has('accepted-2'));

    api().list_pending_agent_decisions = async () => []; // terminal execution: gone from the poll
    await ui.refresh();
    assert.ok(ui._cardNodes.has('accepted-2'), 'receipt keeps the card up through the poll transition');
    await sleep(20);
    assert.ok(!ui._receipts.has('accepted-2'));
    assert.ok(!ui._cardNodes.has('accepted-2'), 'HOST_WRITE_ACCEPTED auto-dismisses once its timer fires');

    // ---------- 6) accents and line breaks survive round-trip ----------
    const accented = 'Faça um discovery read-only.\nInclua critérios de aceitação, coração, ações.\nÇãêîõü';
    api().list_pending_agent_decisions = async () => [
        makeDecision({ decision_id: 'accented', prompt_text: accented, prompt_length: accented.length }),
    ];
    await ui.refresh();
    assert.equal(ui._cardNodes.get('accented').refs.prompt.textContent, accented);

    // ---------- 7) a very long prompt is not truncated ----------
    const long = 'Linha de prompt bem longa com contexto adicional. '.repeat(400);
    assert.ok(long.length > 10000, 'fixture must actually be long to be a meaningful test');
    api().list_pending_agent_decisions = async () => [
        makeDecision({ decision_id: 'long', prompt_text: long, prompt_length: long.length }),
    ];
    await ui.refresh();
    const longText = ui._cardNodes.get('long').refs.prompt.textContent;
    assert.equal(longText.length, long.length);
    assert.equal(longText, long);

    // ---- publisher state notice: only surfaces what needs an operator ----
    {
        resetApi();
        const ui = new AgentApprovals(null, new FakeNode('section'));
        await ui.refresh();
        assert.equal(ui._shell.publisherNotice.hidden, true, 'running publisher stays silent');

        api().get_hermes_publisher_status = async () => ({ state: 'off', reason: 'not_configured' });
        await ui.refresh();
        assert.equal(ui._shell.publisherNotice.hidden, true, 'deliberately off stays silent');

        api().get_hermes_publisher_status = async () => (
            { state: 'backing_off', reason: null, last_error: 'connection refused' });
        await ui.refresh();
        assert.equal(ui._shell.publisherNotice.hidden, true, 'transient backoff stays silent');

        api().get_hermes_publisher_status = async () => (
            { state: 'disabled', reason: 'auth_rejected' });
        await ui.refresh();
        assert.equal(ui._shell.publisherNotice.hidden, false, 'disabled must be visible');
        assert.ok(ui._shell.publisherNotice.textContent.includes('auth_rejected'));

        // An older backend has no such method: must not blank the panel.
        api().get_hermes_publisher_status = async () => { throw new Error('no such method'); };
        api().list_pending_agent_decisions = async () => [makeDecision({ decision_id: 'still-here' })];
        await ui.refresh();
        assert.equal(ui._shell.publisherNotice.hidden, true);
        assert.ok(ui._cardNodes.has('still-here'), 'decisions still render without publisher status');
    }

    console.log('agent-approvals tests passed');
})().catch((error) => {
    console.error(error);
    process.exit(1);
});
