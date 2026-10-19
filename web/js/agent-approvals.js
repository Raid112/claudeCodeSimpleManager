/**
 * UI-only review surface for Hermes proposals (prompt-first, Task 3).
 *
 * Design notes (see .hermes/plans/20260803_133016-claude-manager-hermes-approval-events.md,
 * Task 3, and review-notes.md findings #8/#9):
 *
 * - The prompt is the primary content: full text, multiline, selectable, never
 *   truncated. Session/work item/risk/expiry sit in a compact header; hash,
 *   ids, trace and version lineage live behind a <details> section.
 * - The 2s poll in app.js used to blow away and rebuild the whole DOM tree
 *   every tick, destroying text selection and scroll position inside a long
 *   prompt. render() is now keyed by decision_id: existing card nodes are
 *   reused and only their changed fields are patched. On top of that, a poll-
 *   triggered patch is SKIPPED entirely for a card that currently holds a
 *   non-collapsed text selection or DOM focus inside it -- but a render
 *   triggered by the user's own action on that exact card (clicking Enviar/
 *   Reenviar/Dispensar) always forces the patch through, or the card would
 *   never show its own receipt (the click leaves the button focused).
 * - Since host-owned dispatch (Task 2) means approval *is* the trigger, the
 *   click return value is the only place the execution receipt is ever seen.
 *   Receipts are kept in a Map keyed by decision_id, independent of whatever
 *   list_pending_agent_decisions returns next poll: HOST_WRITE_ACCEPTED shows
 *   briefly then auto-dismisses; WRITE_FAILED/PROVIDER_PROCESSING_UNKNOWN stay
 *   pinned -- including after the decision itself leaves the pending list --
 *   until the operator dismisses them.
 * - Every piece of server/proposal text is written via textContent, never
 *   innerHTML; the prompt is the highest-risk string this app renders.
 */

/** Portuguese label for the terminal-ish receipt states (Task 2). */
function aaReceiptLabel(receipt) {
    if (!receipt) return '';
    switch (receipt.state) {
        case 'HOST_WRITE_ACCEPTED':
            return 'Prompt enviado ao Claude.';
        case 'PROVIDER_PROCESSING_UNKNOWN':
            return 'Enviado — resultado do provedor é desconhecido.';
        case 'WRITE_FAILED':
            return 'Falha ao enviar o prompt.';
        default:
            return receipt.state || '';
    }
}

const AA_REASON_LABELS = {
    session_not_found: 'Sessão/aba não encontrada.',
    unsupported_provider: 'Provedor não suporta envio direto.',
    not_approved: 'A proposta não está aprovada.',
    invalid_input: 'O texto foi rejeitado pelo protocolo de entrada.',
    emergency_stop_active: 'Parada de emergência está ativa.',
    execution_conflict: 'Já existe outra execução para esta decisão.',
    proposal_expired: 'A proposta expirou.',
    proposal_mismatch: 'A proposta não confere mais com o hash aprovado.',
    unexpected_error: 'Erro inesperado no host.',
    client_error: 'Erro de comunicação com o backend.',
};

/** Portuguese label for a WRITE_FAILED/PROVIDER_PROCESSING_UNKNOWN reason code. */
function aaReasonLabel(reason) {
    if (!reason) return '';
    return AA_REASON_LABELS[reason] || reason;
}

class AgentApprovals {
    constructor(app, root) {
        this.app = app;
        this.root = root;
        this.decisions = [];
        this.control = { emergency_stopped: false };
        this.publisherStatus = null;

        // decision_id -> latest known view-model item (kept even after the
        // decision leaves `decisions`, so a pinned receipt still has session/
        // prompt context to show).
        this._cardCache = new Map();
        // decision_id -> DOM node (with .refs attached), reused across renders.
        this._cardNodes = new Map();
        // decision_id -> receipt object returned by approve/retry.
        this._receipts = new Map();
        // decision_id -> setTimeout handle for HOST_WRITE_ACCEPTED auto-dismiss.
        this._dismissTimers = new Map();
        // decision_ids currently mid-dispatch (approve/retry in flight).
        this._pendingClick = new Set();
        // decision_id -> verify_proposal_hash verdict (cached: the proposal is
        // immutable once created, so the verdict never changes).
        this._hashVerdicts = new Map();
        // Overridable by tests; real HOST_WRITE_ACCEPTED cards linger this long.
        this.autoDismissMs = 4000;

        this._shell = null;
    }

    /** True while an approve/retry call is in flight. app.js's 2s poll skips a
     * refresh while this is true so it cannot race the dispatch (Task 3 step 9). */
    get dispatchInFlight() {
        return this._pendingClick.size > 0;
    }

    async refresh() {
        if (!this.root || !window.pywebview?.api) return;
        if (this.dispatchInFlight) return;
        try {
            this.decisions = await window.pywebview.api.list_pending_agent_decisions();
            this.control = await window.pywebview.api.get_agent_control_state();
            // Optional: an app running an older backend has no such method, and a
            // missing publisher status must never blank the approvals panel.
            try {
                this.publisherStatus = await window.pywebview.api.get_hermes_publisher_status();
            } catch (error) {
                this.publisherStatus = null;
            }
        } catch (error) {
            console.warn('agent approvals refresh failed', error);
            return; // keep whatever was last rendered rather than blank the panel
        }
        this.render();
    }

    // ---------- shell (header / notices), built once ----------
    _ensureShell() {
        if (this._shell || !this.root) return this._shell;
        this.root.textContent = '';
        const header = document.createElement('div');
        header.className = 'agent-approvals-header';
        const title = document.createElement('span');
        title.textContent = 'Hermes · aprovações';
        const stopBtn = document.createElement('button');
        stopBtn.type = 'button';
        stopBtn.className = 'agent-stop-btn';
        stopBtn.addEventListener('click', () => this._toggleStop());
        header.append(title, stopBtn);

        const stoppedNotice = document.createElement('div');
        stoppedNotice.className = 'agent-stop-state';
        stoppedNotice.textContent = 'Emergency stop ativo — novas ações estão bloqueadas.';
        stoppedNotice.hidden = true;

        // A publisher that disabled itself (wrong secret, missing route) never
        // recovers on its own — being visibly off is the whole point of not
        // retrying forever, so it needs a surface. Silent when healthy or when
        // deliberately unconfigured.
        const publisherNotice = document.createElement('div');
        publisherNotice.className = 'agent-publisher-state';
        publisherNotice.hidden = true;

        const emptyNotice = document.createElement('div');
        emptyNotice.className = 'agent-approvals-empty';
        emptyNotice.textContent = 'Nenhuma proposta aguardando decisão.';
        emptyNotice.hidden = true;

        this.root.append(header, stoppedNotice, publisherNotice, emptyNotice);
        this._shell = { header, stopBtn, stoppedNotice, publisherNotice, emptyNotice };
        return this._shell;
    }

    async _toggleStop() {
        try {
            const capability = await window.pywebview.api.get_emergency_capability();
            if (this.control.emergency_stopped) {
                await window.pywebview.api.unlock_emergency_stop(capability);
            } else {
                await window.pywebview.api.emergency_stop(capability);
            }
        } catch (error) {
            console.warn('toggle emergency stop failed', error);
        }
        await this.refresh();
    }

    /** Show the wake-up publisher only when it needs an operator: `disabled`
     * (401/403/404 — a wrong secret or missing route, which no retry can fix) or
     * `unknown`. Healthy, backing off (transient, self-healing) and deliberately
     * unconfigured stay silent. */
    _renderPublisherState(shell) {
        const status = this.publisherStatus;
        const state = status && status.state;
        if (state !== 'disabled' && state !== 'unknown') {
            shell.publisherNotice.hidden = true;
            return;
        }
        const reason = (status && (status.reason || status.last_error)) || 'desconhecido';
        shell.publisherNotice.textContent =
            `Publisher de eventos ${state === 'disabled' ? 'desativado' : 'em estado desconhecido'} `
            + `(${reason}) — wake-ups do Hermes não estão sendo entregues.`;
        shell.publisherNotice.hidden = false;
    }

    // ---------- incremental render ----------
    /** @param {{forceIds?: Set<string>}} [opts] forceIds bypass selection/focus
     * suspension -- used for the render that follows the user's own action on
     * that exact card (approve/retry/dismiss), never for the periodic poll. */
    render(opts = {}) {
        const forceIds = opts.forceIds || new Set();
        const shell = this._ensureShell();
        if (!shell) return;

        shell.stopBtn.textContent = this.control.emergency_stopped ? 'Desbloquear' : 'Parar agente';
        shell.stoppedNotice.hidden = !this.control.emergency_stopped;
        this._renderPublisherState(shell);

        const liveMap = new Map(this.decisions.map((d) => [d.decision_id, d]));
        const receiptIds = new Set(this._receipts.keys());
        const allIds = new Set([...liveMap.keys(), ...receiptIds]);

        for (const [id, node] of Array.from(this._cardNodes)) {
            if (!allIds.has(id)) {
                node.remove();
                this._cardNodes.delete(id);
                this._cardCache.delete(id);
                this._hashVerdicts.delete(id);
            }
        }

        shell.emptyNotice.hidden = allIds.size > 0;

        // Stable append-only order: live decisions in server order, then any
        // receipt-only survivor (a pinned card whose decision already left the
        // pending list) that isn't live anymore. Existing cards are never moved.
        const order = [...liveMap.keys()];
        for (const id of receiptIds) if (!liveMap.has(id)) order.push(id);

        for (const id of order) {
            const decision = liveMap.get(id) || this._cardCache.get(id);
            if (!decision) continue;
            this._cardCache.set(id, decision);
            let node = this._cardNodes.get(id);
            if (!node) {
                node = this._createCard(id);
                this._cardNodes.set(id, node);
                this.root.appendChild(node);
                this._patchCard(id);
                continue;
            }
            if (forceIds.has(id) || !this._isSuspended(node)) this._patchCard(id);
        }
    }

    /** A card is suspended from poll-triggered patching while it holds a real
     * (non-collapsed) text selection or DOM focus. A collapsed caret (e.g. from
     * merely clicking into the prompt) does not count -- only an actual range. */
    _isSuspended(node) {
        const active = typeof document !== 'undefined' ? document.activeElement : null;
        if (active && active !== document.body && node.contains(active)) return true;
        if (typeof document.getSelection === 'function') {
            const sel = document.getSelection();
            if (sel && !sel.isCollapsed && sel.anchorNode && node.contains(sel.anchorNode)) return true;
        }
        return false;
    }

    _setText(el, value) {
        if (el.textContent !== value) el.textContent = value;
    }

    // ---------- card DOM (created once per decision_id, then patched) ----------
    _createCard(id) {
        const card = document.createElement('article');
        card.className = 'agent-approval-card';
        card.dataset.decisionId = id;

        const head = document.createElement('div');
        head.className = 'aa-card-head';
        const session = document.createElement('span');
        session.className = 'aa-session';
        const wi = document.createElement('span');
        wi.className = 'aa-wi';
        const risk = document.createElement('span');
        risk.className = 'aa-risk';
        const expiry = document.createElement('span');
        expiry.className = 'aa-expiry';
        head.append(session, wi, risk, expiry);

        const promptWrap = document.createElement('div');
        promptWrap.className = 'aa-prompt-wrap';
        const copyBtn = document.createElement('button');
        copyBtn.type = 'button';
        copyBtn.className = 'aa-copy';
        copyBtn.textContent = 'Copiar';
        copyBtn.addEventListener('click', () => this._copyPrompt(id));
        const prompt = document.createElement('pre');
        prompt.className = 'aa-prompt';
        prompt.tabIndex = 0;
        promptWrap.append(copyBtn, prompt);

        const hashWarning = document.createElement('div');
        hashWarning.className = 'aa-hash-warning';
        hashWarning.hidden = true;

        const receiptBox = document.createElement('div');
        receiptBox.className = 'aa-receipt';
        receiptBox.hidden = true;
        const receiptLabel = document.createElement('span');
        receiptLabel.className = 'aa-receipt-label';
        const receiptReason = document.createElement('span');
        receiptReason.className = 'aa-receipt-reason';
        const retryBtn = document.createElement('button');
        retryBtn.type = 'button';
        retryBtn.className = 'aa-retry';
        retryBtn.textContent = 'Reenviar';
        retryBtn.hidden = true;
        retryBtn.addEventListener('click', () => this._retry(id));
        const dismissBtn = document.createElement('button');
        dismissBtn.type = 'button';
        dismissBtn.className = 'aa-dismiss';
        dismissBtn.textContent = 'Dispensar';
        dismissBtn.hidden = true;
        dismissBtn.addEventListener('click', () => this._dismiss(id));
        receiptBox.append(receiptLabel, receiptReason, retryBtn, dismissBtn);

        const actionsBox = document.createElement('div');
        actionsBox.className = 'agent-approval-actions';
        const approveBtn = document.createElement('button');
        approveBtn.type = 'button';
        approveBtn.textContent = 'Enviar para Claude';
        approveBtn.addEventListener('click', () => this._approve(id));
        const rejectBtn = document.createElement('button');
        rejectBtn.type = 'button';
        rejectBtn.textContent = 'Rejeitar / pedir ajuste';
        rejectBtn.addEventListener('click', () => this._reject(id));
        actionsBox.append(approveBtn, rejectBtn);

        const details = document.createElement('details');
        details.className = 'aa-details';
        const summary = document.createElement('summary');
        summary.textContent = 'Detalhes técnicos ▸';
        const detailsBody = document.createElement('div');
        detailsBody.className = 'aa-details-body';
        details.append(summary, detailsBody);

        card.append(head, promptWrap, hashWarning, receiptBox, actionsBox, details);

        card.refs = {
            session, wi, risk, expiry, prompt, copyBtn, hashWarning,
            receiptBox, receiptLabel, receiptReason, retryBtn, dismissBtn,
            actionsBox, approveBtn, rejectBtn, detailsBody,
        };
        return card;
    }

    _patchCard(id) {
        const node = this._cardNodes.get(id);
        const decision = this._cardCache.get(id);
        if (!node || !decision) return;
        const refs = node.refs;

        this._setText(refs.session, decision.session_label || 'Sessão desconhecida');
        this._setText(refs.wi, decision.work_item_title ? `Work item: ${decision.work_item_title}` : '');
        refs.wi.hidden = !decision.work_item_title;
        this._setText(refs.risk, decision.risk_level ? `Risco: ${decision.risk_level}` : '');
        refs.risk.className = `aa-risk aa-risk-${decision.risk_level || 'unknown'}`;
        this._setText(refs.expiry, decision.expires_at ? `Expira: ${decision.expires_at}` : '');

        this._setText(refs.prompt, decision.prompt_text || '');

        this._renderDetails(refs.detailsBody, decision);
        this._verifyHashOnce(id, decision);
        this._renderReceiptAndActions(node, id, decision);
    }

    _renderDetails(container, decision) {
        const rows = [
            ['ID da decisão', decision.decision_id],
            ['Hash', decision.proposal_hash],
            ['Trace', decision.trace_id],
            ['Linhagem', `${decision.parent_decision_id || 'v1'} · versão ${decision.version}`],
            ['Comprimento do prompt', decision.prompt_length != null ? String(decision.prompt_length) : null],
        ];
        if (!container.childNodes.length) {
            for (const _row of rows) {
                const rowEl = document.createElement('div');
                rowEl.className = 'agent-approval-field';
                const labelEl = document.createElement('span');
                labelEl.className = 'agent-approval-label';
                const valueEl = document.createElement('span');
                valueEl.className = 'aa-details-value';
                rowEl.append(labelEl, valueEl);
                container.appendChild(rowEl);
            }
        }
        rows.forEach(([label, value], i) => {
            const rowEl = container.childNodes[i];
            this._setText(rowEl.firstChild, `${label}: `);
            this._setText(rowEl.lastChild, value == null ? '—' : value);
        });
    }

    _renderReceiptAndActions(node, id, decision) {
        const refs = node.refs;
        const receipt = this._receipts.get(id);
        const dispatching = this._pendingClick.has(id);

        if (receipt) {
            refs.receiptBox.hidden = false;
            refs.receiptBox.className = `aa-receipt aa-receipt-${(receipt.state || '').toLowerCase()}`;
            this._setText(refs.receiptLabel, aaReceiptLabel(receipt));
            this._setText(refs.receiptReason, receipt.reason ? `Motivo: ${aaReasonLabel(receipt.reason)}` : '');
            refs.retryBtn.hidden = receipt.retryable !== true;
            refs.dismissBtn.hidden = false;
            refs.actionsBox.hidden = true;
        } else if (decision.pending_reason === 'awaiting_dispatch') {
            // Approved in a previous session (no local receipt) or the receipt
            // was dismissed without the underlying dispatch ever succeeding: the
            // only forward path left is a manual retry.
            refs.receiptBox.hidden = false;
            refs.receiptBox.className = 'aa-receipt aa-receipt-awaiting';
            this._setText(refs.receiptLabel, 'Aprovado — aguardando envio.');
            this._setText(refs.receiptReason, '');
            refs.retryBtn.hidden = false;
            refs.dismissBtn.hidden = true;
            refs.actionsBox.hidden = true;
        } else {
            refs.receiptBox.hidden = true;
            refs.actionsBox.hidden = false;
        }

        refs.approveBtn.disabled = dispatching;
        refs.rejectBtn.disabled = dispatching;
        refs.retryBtn.disabled = dispatching;
        this._setText(refs.approveBtn, dispatching ? 'Enviando…' : 'Enviar para Claude');
        this._setText(refs.retryBtn, dispatching ? 'Enviando…' : 'Reenviar');
    }

    /** Recompute the hash server-side (never in JS -- see review finding #9) and
     * cache the verdict: the proposal is immutable once created, so this only
     * ever needs to run once per decision_id. */
    async _verifyHashOnce(id, decision) {
        if (this._hashVerdicts.has(id)) {
            this._applyHashVerdict(id);
            return;
        }
        if (!window.pywebview?.api?.verify_proposal_hash) return;
        this._hashVerdicts.set(id, null); // in-flight marker; avoids duplicate calls
        let verdict;
        try {
            verdict = await window.pywebview.api.verify_proposal_hash(
                decision.decision_id, decision.prompt_text);
        } catch (error) {
            this._hashVerdicts.delete(id);
            return;
        }
        this._hashVerdicts.set(id, verdict);
        this._applyHashVerdict(id);
    }

    _applyHashVerdict(id) {
        const node = this._cardNodes.get(id);
        const verdict = this._hashVerdicts.get(id);
        if (!node || !verdict) return;
        const mismatch = verdict.found && (verdict.hash_ok === false || verdict.text_ok === false);
        node.refs.hashWarning.hidden = !mismatch;
        if (mismatch) {
            this._setText(node.refs.hashWarning,
                'Aviso: o texto exibido pode não corresponder ao hash aprovado — não envie sem revisar.');
        }
    }

    // ---------- actions ----------
    async _approve(id) {
        const decision = this._cardCache.get(id); // live snapshot at click time
        if (!decision || this._pendingClick.has(id)) return;
        this._pendingClick.add(id);
        this.render({ forceIds: new Set([id]) });
        try {
            const receipt = await window.pywebview.api.approve_agent_proposal(
                decision.decision_id, decision.proposal_hash, 'local-user', decision.operator_capability);
            this._setReceipt(id, receipt);
        } catch (error) {
            console.warn('approve_agent_proposal failed', error);
            this._setReceipt(id, {
                decision_id: id, state: 'WRITE_FAILED', retryable: false, reason: 'client_error',
            });
        } finally {
            this._pendingClick.delete(id);
            this.render({ forceIds: new Set([id]) });
        }
    }

    async _retry(id) {
        const decision = this._cardCache.get(id);
        if (!decision || this._pendingClick.has(id)) return;
        this._pendingClick.add(id);
        this.render({ forceIds: new Set([id]) });
        try {
            const receipt = await window.pywebview.api.retry_prompt_dispatch(
                decision.decision_id, decision.proposal_hash, decision.operator_capability);
            this._setReceipt(id, receipt);
        } catch (error) {
            console.warn('retry_prompt_dispatch failed', error);
            this._setReceipt(id, {
                decision_id: id, state: 'WRITE_FAILED', retryable: false, reason: 'client_error',
            });
        } finally {
            this._pendingClick.delete(id);
            this.render({ forceIds: new Set([id]) });
        }
    }

    async _reject(id) {
        const decision = this._cardCache.get(id);
        if (!decision) return;
        const comment = window.prompt('Feedback para o replanejamento:');
        if (!comment || !comment.trim()) return;
        try {
            await window.pywebview.api.reject_agent_proposal(
                decision.decision_id, decision.proposal_hash, 'local-user',
                decision.operator_capability, 'wrong_scope', comment.trim(), [comment.trim()]);
        } catch (error) {
            console.warn('reject_agent_proposal failed', error);
        }
        await this.refresh();
    }

    async _copyPrompt(id) {
        const decision = this._cardCache.get(id);
        if (!decision) return;
        try {
            if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
                await navigator.clipboard.writeText(decision.prompt_text || '');
            }
        } catch (error) {
            console.warn('copy prompt failed', error);
        }
    }

    /** Store the latest receipt and (re)schedule its auto-dismiss timer. Any
     * previous timer for this id is cleared first: a retry that supersedes a
     * pinned WRITE_FAILED with a fresh HOST_WRITE_ACCEPTED must not leave the
     * old timer alive to delete the new receipt at the wrong moment (or, if the
     * new receipt is itself pinned, to delete it before the operator dismisses
     * it). */
    _setReceipt(id, receipt) {
        const existingTimer = this._dismissTimers.get(id);
        if (existingTimer) {
            clearTimeout(existingTimer);
            this._dismissTimers.delete(id);
        }
        this._receipts.set(id, receipt);
        if (receipt && receipt.state === 'HOST_WRITE_ACCEPTED') {
            const timer = setTimeout(() => this._dismiss(id), this.autoDismissMs);
            this._dismissTimers.set(id, timer);
        }
    }

    /** Clear a receipt (auto-dismiss timer firing, or the operator clicking
     * Dispensar) and force-render just that card so it disappears immediately
     * rather than waiting for the next poll. */
    _dismiss(id) {
        const timer = this._dismissTimers.get(id);
        if (timer) {
            clearTimeout(timer);
            this._dismissTimers.delete(id);
        }
        this._receipts.delete(id);
        this.render({ forceIds: new Set([id]) });
    }
}

if (typeof window !== 'undefined') window.AgentApprovals = AgentApprovals;
if (typeof module !== 'undefined' && module.exports) {
    module.exports = { AgentApprovals, aaReceiptLabel, aaReasonLabel };
}
