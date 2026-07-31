/** UI-only review surface for Hermes proposals. External text is always textContent. */
class AgentApprovals {
    constructor(app, root) {
        this.app = app;
        this.root = root;
        this.decisions = [];
        this.control = { emergency_stopped: false };
    }

    async refresh() {
        if (!this.root || !window.pywebview?.api) return;
        try {
            this.decisions = await window.pywebview.api.list_pending_agent_decisions();
            this.control = await window.pywebview.api.get_agent_control_state();
            this.render();
        } catch (error) {
            this.root.textContent = 'Aprovações indisponíveis';
        }
    }

    _text(parent, label, value) {
        const row = document.createElement('div');
        row.className = 'agent-approval-field';
        const title = document.createElement('span');
        title.className = 'agent-approval-label';
        title.textContent = `${label}: `;
        const content = document.createElement('span');
        content.textContent = value == null ? '—' : String(value);
        row.append(title, content);
        parent.appendChild(row);
    }

    render() {
        this.root.textContent = '';
        const header = document.createElement('div');
        header.className = 'agent-approvals-header';
        const title = document.createElement('span');
        title.textContent = 'Hermes · aprovações';
        header.appendChild(title);
        const stop = document.createElement('button');
        stop.className = 'agent-stop-btn';
        stop.textContent = this.control.emergency_stopped ? 'Desbloquear' : 'Parar agente';
        stop.addEventListener('click', async () => {
            const capability = await window.pywebview.api.get_emergency_capability();
            if (this.control.emergency_stopped) {
                await window.pywebview.api.unlock_emergency_stop(capability);
            } else {
                await window.pywebview.api.emergency_stop(capability);
            }
            await this.refresh();
        });
        header.appendChild(stop);
        this.root.appendChild(header);

        if (this.control.emergency_stopped) {
            const stopped = document.createElement('div');
            stopped.className = 'agent-stop-state';
            stopped.textContent = 'Emergency stop ativo — novas ações estão bloqueadas.';
            this.root.appendChild(stopped);
        }
        if (!this.decisions.length) {
            const empty = document.createElement('div');
            empty.className = 'agent-approvals-empty';
            empty.textContent = 'Nenhuma proposta aguardando decisão.';
            this.root.appendChild(empty);
            return;
        }
        for (const decision of this.decisions) this._renderDecision(decision);
    }

    _renderDecision(decision) {
        const proposal = decision.proposal || {};
        const action = proposal.action || {};
        const card = document.createElement('article');
        card.className = 'agent-approval-card';
        this._text(card, 'Ação', action.type);
        this._text(card, 'Alvo', JSON.stringify(action.target || {}));
        this._text(card, 'Intenção', proposal.intent);
        this._text(card, 'Resultado esperado', action.expected_outcome);
        this._text(card, 'Risco', (proposal.risk || {}).level);
        this._text(card, 'Hash', decision.proposal_hash);
        this._text(card, 'Linhas', `${decision.parent_decision_id || 'v1'} · versão ${decision.version}`);
        this._text(card, 'Expira', proposal.expires_at);

        const actions = document.createElement('div');
        actions.className = 'agent-approval-actions';
        const approve = document.createElement('button');
        approve.textContent = 'Aprovar';
        approve.addEventListener('click', async () => {
            await window.pywebview.api.approve_agent_proposal(
                decision.decision_id, decision.proposal_hash, 'local-user', decision.operator_capability);
            await this.refresh();
        });
        const reject = document.createElement('button');
        reject.textContent = 'Rejeitar / pedir ajuste';
        reject.addEventListener('click', async () => {
            const comment = window.prompt('Feedback para o replanejamento:');
            if (!comment || !comment.trim()) return;
            await window.pywebview.api.reject_agent_proposal(
                decision.decision_id, decision.proposal_hash, 'local-user',
                decision.operator_capability, 'wrong_scope', comment.trim(), [comment.trim()]);
            await this.refresh();
        });
        actions.append(approve, reject);
        card.appendChild(actions);
        this.root.appendChild(card);
    }
}

if (typeof window !== 'undefined') window.AgentApprovals = AgentApprovals;
