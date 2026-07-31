/**
 * Tab bar — manages terminal tabs UI.
 */

class TabBar {
    constructor(container, app) {
        this.container = container;
        this.app = app;
    }

    _esc(s) {
        return (s || '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
    }

    _keyLabel(item) {
        if (item.source === 'jira') return item.external_key || 'JIRA';
        // teams: só o primeiro nome (CAROLINA, não CAROLINA VENÂNCIO)
        if (item.source === 'teams') return this._firstName(item.person).toUpperCase();
        // manual: usa o nome que o usuário deu (título); o ellipsis fica com o CSS
        // (.tab-label tem max-width), então o corte aqui é só um teto de sanidade.
        const nm = (item.title || '').trim();
        return nm ? `TODO: ${nm.length > 40 ? nm.slice(0, 40) + '…' : nm}` : 'TODO';
    }

    _firstName(person) {
        return ((person || '?').trim().split(/\s+/)[0]) || '?';
    }

    /** Começo da mensagem vinculada, numa linha, pro rótulo da aba. */
    _msgSnippet(title) {
        const one = (title || '').replace(/\s+/g, ' ').trim();
        return one.length > 60 ? one.slice(0, 60) + '…' : one;
    }

    /** Rótulo curto da aba colapsada: a key do work item, ou um traço para avulsa. */
    _stubLabel(wi) {
        if (!wi) return '·';
        if (wi.source === 'jira') return wi.external_key || 'JIRA';
        if (wi.source === 'teams') return this._firstName(wi.person).toUpperCase();
        const nm = (wi.title || '').trim();
        return nm ? nm.slice(0, 12) : 'TODO';   // o resto o CSS corta (.tab-stub tem max-width)
    }

    render() {
        let html = '';

        // Gaveta: com um work item travado, só as abas DELE ficam expandidas; as outras viram
        // stub (barra de cor + dot + key). Nada some — um clique reabre. A aba ATIVA nunca
        // colapsa, senão você fica olhando pro terminal em que está com o rótulo escondido.
        const lockedId = this.app.lockedWiId || null;
        const wiItems = (this.app.workItems && this.app.workItems.items) || [];
        // Trava sem efeito quando o item sumiu (concluído/arquivado/apagado) OU não tem
        // nenhuma aba viva — travar um item recém-criado pelo ＋ colapsaria a barra inteira
        // pra mostrar uma gaveta vazia.
        const lockAlive = lockedId && wiItems.some(it => it.id === lockedId && !it.done && !it.archived);
        const lockHasTabs = lockAlive && Object.keys(this.app.terminals).some(id => {
            const w = this.app.workItemForSession ? this.app.workItemForSession(id) : null;
            return w && w.id === lockedId;
        });
        const locked = lockHasTabs ? lockedId : null;

        const links = this.app.workItems ? (this.app.workItems.session_links || {}) : {};
        for (const [id, info] of Object.entries(this.app.terminals)) {
            // Aba arquivada (individual ou via item arquivado que cascateou) some da barra.
            const linkKey = this.app.sessionLinkKeyOf ? this.app.sessionLinkKeyOf(id) : info.claudeSessionId;
            const link = (linkKey && links[linkKey]) || (info.claudeSessionId ? links[info.claudeSessionId] : null);
            if (link && link.archived) continue;
            const isActive = this.app.activeTerminalId === id;
            const dotClass = info.instance.status;  // 'running', 'ready', or 'stopped'
            const displayName = this.app.getDisplayName(id);
            const timerHtml = this._renderTimer(info.instance);

            // Linked work item → color bar + key prefix; unlinked → neutral, link icon "live".
            // The link icon only shows for sessions that CAN link (have a claude_session_id);
            // Codex/--continue tabs (null id) get no affordance — matches the sidebar.
            const wi = this.app.workItemForSession ? this.app.workItemForSession(id) : null;
            const linkable = !!(this.app.sessionLinkKeyOf ? this.app.sessionLinkKeyOf(id) : this.app.claudeSessionIdOf(id));
            const color = wi ? (SRC_COLOR[wi.source] || 'var(--t-todo)') : '#2f2f38';
            const keyPrefix = wi
                ? `<span class="tab-key" style="color:${color}">${this._esc(this._keyLabel(wi))}</span> ` : '';
            // TODO manual: aba = só "TODO: <título>" (sem sufixo do diretório).
            // Teams: aba = primeiro nome + começo da mensagem vinculada (não o diretório).
            // Em ambos, um nome próprio da sessão (customName) tem prioridade p/ desambiguar.
            let labelName;
            if (wi && wi.source === 'manual' && !info.customName) labelName = '';
            else if (wi && wi.source === 'teams' && !info.customName) labelName = this._msgSnippet(wi.title);
            else labelName = displayName;
            const linkCls = wi ? 'tab-link' : 'tab-link live';
            const linkTitle = wi ? 'vínculo' : 'vincular';
            const linkHtml = linkable
                ? `<span class="${linkCls}" data-link="${id}" title="${linkTitle}"><svg viewBox="0 0 20 20"><use href="#i-link"/></svg></span>` : '';

            // Colapsa tudo que não é da gaveta travada (menos a aba ativa).
            const stub = !!locked && !isActive && !(wi && wi.id === locked);
            // Tooltip = rótulo inteiro (o stub esconde e o CSS trunca o resto). Para teams,
            // o texto útil é a mensagem vinculada, não o diretório.
            const fullName = (wi && wi.source === 'teams' && !info.customName) ? wi.title : displayName;
            const full = `${wi ? this._keyLabel(wi) + ' · ' : ''}${fullName}`;

            html += `<div class="tab ${isActive ? 'active' : ''} ${stub ? 'stub' : ''} status-${dotClass}"`
                  + ` data-id="${id}" draggable="true" style="--c:${color}" title="${this._esc(full)}">`;
            html += `<span class="tab-bar-c"></span>`;
            html += `<span class="tab-dot ${dotClass}"></span>`;
            if (stub) {
                html += `<span class="tab-stub" data-id="${id}" style="color:${color}">${this._esc(this._stubLabel(wi))}</span>`;
            } else {
                html += `<span class="tab-label" data-id="${id}">${keyPrefix}${this._esc(labelName)}</span>`;
                html += timerHtml;
                html += linkHtml;
                html += `<span class="tab-close" data-id="${id}" title="Fechar">×</span>`;
            }
            html += `</div>`;
        }

        this.container.innerHTML = html;
        this._bindEvents();
    }

    _renderTimer(instance) {
        const ms = instance.cacheRemainingMs;
        if (ms === null) return '';
        const urgency = instance.cacheUrgency;
        let label;
        if (urgency === 'expired') {
            label = 'expirado';
        } else {
            const totalSec = Math.floor(ms / 1000);
            const m = Math.floor(totalSec / 60);
            const s = totalSec % 60;
            label = m >= 10 ? `${m}m` : `${m}:${String(s).padStart(2, '0')}`;
        }
        const title = urgency === 'expired'
            ? 'Cache expirado — envie qualquer coisa para renovar'
            : `Cache renova em ${label}`;
        return `<span class="cache-timer ${urgency}" title="${title}">${label}</span>`;
    }

    _startRename(labelEl, sessionId) {
        this.app._isRenaming = true;
        // Aba de TODO manual: renomear edita o TÍTULO do work item (sincroniza com a sidebar
        // e as abas irmãs). Demais (jira/teams/avulso): renomeia só a sessão (customName).
        const wi = this.app.workItemForSession ? this.app.workItemForSession(sessionId) : null;
        const renameItem = wi && wi.source === 'manual';
        const currentName = renameItem ? (wi.title || '') : this.app.getDisplayName(sessionId);
        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'tab-rename-input';
        input.value = currentName;

        // Disable drag while editing so text selection inside the input works.
        const tabEl = labelEl.closest('.tab');
        if (tabEl) tabEl.draggable = false;

        labelEl.textContent = '';
        labelEl.appendChild(input);
        input.focus();
        input.select();

        const finish = () => {
            const newName = input.value.trim();
            if (renameItem) {
                if (newName) this.app.renameWorkItem(wi.id, newName);
                else { this.app._isRenaming = false; this.render(); }  // vazio: cancela, mantém título
            } else {
                this.app.renameTerminal(sessionId, newName || null);
            }
        };

        input.addEventListener('blur', finish);
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                input.blur();
            } else if (e.key === 'Escape') {
                e.preventDefault();
                input.value = '';
                input.blur();
            }
        });
        // Prevent tab switch when clicking the input
        input.addEventListener('click', (e) => e.stopPropagation());
    }

    _bindEvents() {
        // Tab click -> switch
        this.container.querySelectorAll('.tab').forEach(el => {
            el.addEventListener('click', (e) => {
                if (e.target.closest('.tab-close') || e.target.closest('.tab-link')) return;
                this.app.switchToTerminal(el.dataset.id);
            });
        });

        // Link icon -> open the link popover for this tab
        this.container.querySelectorAll('.tab-link').forEach(el => {
            el.addEventListener('click', (e) => {
                e.stopPropagation();
                if (window.app.workItemsUI) window.app.workItemsUI.openLinkPopover(el.dataset.link);
            });
        });

        // Right-click on label -> rename (stub não tem label; vira aba cheia ao ser clicada)
        this.container.querySelectorAll('.tab-label').forEach(el => {
            el.addEventListener('contextmenu', (e) => {
                e.preventDefault();
                e.stopPropagation();
                this._startRename(el, el.dataset.id);
            });
        });

        // Close button -> close ceremony (transition/comment/archive) for linked tabs,
        // direct close otherwise. Shift-click always closes directly (skip ceremony).
        this.container.querySelectorAll('.tab-close').forEach(el => {
            el.addEventListener('click', (e) => {
                e.stopPropagation();
                const id = el.dataset.id;
                const wi = this.app.workItemForSession ? this.app.workItemForSession(id) : null;
                if (!e.shiftKey && wi && window.app.workItemsUI) {
                    window.app.workItemsUI.openCloseDialog(id, wi);
                } else {
                    this.app.closeTerminal(id);
                }
            });
        });

        // Middle-click on tab -> close (like browsers)
        this.container.querySelectorAll('.tab').forEach(el => {
            el.addEventListener('auxclick', (e) => {
                if (e.button === 1) {
                    e.preventDefault();
                    this.app.closeTerminal(el.dataset.id);
                }
            });
        });

        // Drag to reorder tabs
        this.container.querySelectorAll('.tab').forEach(el => {
            el.addEventListener('dragstart', (e) => {
                e.dataTransfer.setData('text/plain', el.dataset.id);
                e.dataTransfer.effectAllowed = 'move';
                el.classList.add('dragging');
            });
            el.addEventListener('dragend', () => el.classList.remove('dragging'));
            el.addEventListener('dragover', (e) => {
                e.preventDefault();
                e.dataTransfer.dropEffect = 'move';
            });
            el.addEventListener('drop', (e) => {
                e.preventDefault();
                const draggedId = e.dataTransfer.getData('text/plain');
                if (draggedId && draggedId !== el.dataset.id) {
                    this.app.reorderTab(draggedId, el.dataset.id);
                }
            });
        });
    }
}
