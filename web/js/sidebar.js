/**
 * Sidebar — work-item-centric. Renders four sections:
 *   Trabalho  — work items (jira/teams/manual) with their live sessions nested
 *   Avulso    — live terminals not linked to any work item (incl. codex/--continue)
 *   Nova aba  — folder chips that launch a terminal (the old group launcher)
 *   Arquivado — session_links flagged archived (collapsed by default)
 *
 * Structure comes from app.workItems ({items, session_links}); live state (running/ready)
 * is joined in from get_terminals + the frontend TerminalInstance status. The join key is
 * claude_session_id (session_links) ↔ terminal.claudeSessionId.
 */

const SRC_COLOR = { jira: 'var(--t-issue)', teams: 'var(--t-msg)', manual: 'var(--t-todo)' };

function wiWaitingElapsed(tsSeconds, nowMs = Date.now()) {
    if (!tsSeconds) return 'agora';
    const mins = Math.max(0, Math.floor((nowMs - tsSeconds * 1000) / 60000));
    if (mins < 60) return `${mins}m`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours}h`;
    return `${Math.floor(hours / 24)}d`;
}

function wiPartitionWorkItems(items) {
    const open = (items || []).filter(it => !it.done && !it.merged_into);
    return {
        active: open.filter(it => !it.archived && it.workflow_state !== 'waiting'),
        waiting: open.filter(it => !it.archived && it.workflow_state === 'waiting'),
        archived: open.filter(it => it.archived),
    };
}

class Sidebar {
    constructor(container, app) {
        this.container = container;
        this.app = app;
        this.expandedGroups = new Set();      // legacy group expand (unused in chip mode)
        this._terminalType = 'claude';
        this._archivedOpen = false;
        this._moreGroups = false;             // "Nova aba" chips: show all vs favourites
        this._dragId = null;                  // work item id being dragged (reorder)
    }

    // ---------- status / due helpers ----------
    _sessionStatus(ptyId) {
        const info = this.app.terminals[ptyId];
        return info ? info.instance.status : 'stopped';
    }

    _dotClass(status) {
        return ({
            ready: 'd-ready', running: 'd-active', tooluse: 'd-tooluse',
            waiting: 'd-waiting', stopped: 'd-stopped',
        })[status] || 'd-stopped';
    }

    /** Aggregate a work item's dot from its live sessions (attention wins). */
    _itemDot(statuses) {
        if (statuses.includes('waiting')) return 'd-waiting';
        if (statuses.includes('tooluse')) return 'd-tooluse';
        if (statuses.includes('running')) return 'd-active';
        if (statuses.includes('ready')) return 'd-ready';
        return 'd-stopped';
    }

    _dueInfo(item) {
        const raw = item.duedate;
        if (!raw) return { cls: 'sem', txt: 'sem prazo' };
        let d;
        if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) {
            const [y, m, day] = raw.split('-').map(Number);
            d = new Date(y, m - 1, day);
        } else {
            d = new Date(raw);
        }
        if (isNaN(d)) return { cls: 'sem', txt: 'sem prazo' };
        const now = new Date();
        const d0 = new Date(d.getFullYear(), d.getMonth(), d.getDate());
        const t0 = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        const diff = Math.round((d0 - t0) / 86400000);
        const dm = `${String(d.getDate()).padStart(2, '0')}/${String(d.getMonth() + 1).padStart(2, '0')}`;
        const hm = item.duedate_has_time
            ? ` ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}` : '';
        if (diff < 0) return { cls: 'venc', txt: `venceu ${dm}` };
        if (diff === 0) return { cls: 'hoje', txt: `hoje${hm}` };
        if (diff === 1) return { cls: 'amanha', txt: `amanhã${hm}` };
        return { cls: 'depois', txt: `${dm}${hm}` };
    }

    /** Key label per source (DS-201 / GUSTAVO / TODO). */
    _keyLabel(item) {
        if (item.source === 'jira') return item.external_key || 'JIRA';
        if (item.source === 'teams') return (((item.person || '?').trim().split(/\s+/)[0]) || '?').toUpperCase();
        return 'TODO';
    }

    _esc(s) {
        return (s || '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
    }

    /** Timer de cache da sessão (mesmo dado das abas). '' quando não há instância viva
     * (linha arquivada) ou nenhuma mensagem foi enviada ainda. */
    _cacheTimer(ptyId) {
        const info = this.app.terminals[ptyId];
        if (!info) return '';
        const ms = info.instance.cacheRemainingMs;
        if (ms === null) return '';
        const urgency = info.instance.cacheUrgency;
        let label, title;
        if (urgency === 'expired') {
            label = 'exp';
            title = 'Cache expirado — envie qualquer coisa para renovar';
        } else {
            const totalSec = Math.floor(ms / 1000);
            const m = Math.floor(totalSec / 60);
            label = m >= 10 ? `${m}m` : `${m}:${String(totalSec % 60).padStart(2, '0')}`;
            title = `Cache renova em ${label}`;
        }
        return `<span class="cache-timer ${urgency}" title="${title}">${label}</span>`;
    }

    _waitingCacheSummary(sessions) {
        const values = (sessions || []).map(s => {
            const info = this.app.terminals[s.ptyId];
            if (!info || info.instance.cacheRemainingMs === null) return null;
            return {
                ms: info.instance.cacheRemainingMs,
                urgency: info.instance.cacheUrgency,
            };
        }).filter(Boolean).sort((a, b) => a.ms - b.ms);
        if (!values.length) return '';
        const min = values[0];
        if (min.urgency === 'expired' || min.ms <= 0) {
            return '<span class="wait-cache expired">cache exp</span>';
        }
        const totalMin = Math.max(0, Math.floor(min.ms / 60000));
        const label = totalMin >= 60
            ? `${Math.floor(totalMin / 60)}h${String(totalMin % 60).padStart(2, '0')}`
            : `${totalMin}m`;
        return `<span class="wait-cache ${min.urgency}" title="Menor cache entre as abas">cache ${label}</span>`;
    }

    // ---------- render ----------
    async render() {
        const groups = await window.pywebview.api.get_groups();
        try { this._terminalType = await window.pywebview.api.get_terminal_type(); } catch (e) {}

        const links = this.app.workItems.session_links || {};
        const allItems = this.app.workItems.items || [];
        const partition = wiPartitionWorkItems(allItems);
        const items = partition.active;
        const waitingItems = partition.waiting;
        const archivedItems = partition.archived;
        items.sort((a, b) => (a.sort_order || 0) - (b.sort_order || 0));
        waitingItems.sort((a, b) => (a.sort_order || 0) - (b.sort_order || 0));

        // Map claude_session_id -> pty session id (live terminals only)
        const liveByCsid = {};
        for (const [ptyId, info] of Object.entries(this.app.terminals)) {
            const key = this.app.sessionLinkKeyOf(ptyId);
            if (key) liveByCsid[key] = ptyId;
            if (info.claudeSessionId) liveByCsid[info.claudeSessionId] = ptyId;
        }

        // Group live sessions: linked to an ACTIVE (non-done) item vs avulso.
        // A session linked to a done item, or whose item no longer exists, falls to avulso
        // (never orphaned off-screen). Archived sessions belong only to the Arquivado section.
        const activeItemIds = new Set(items.map(it => it.id));
        const waitingItemIds = new Set(waitingItems.map(it => it.id));
        const sessionsByItem = {};   // wi_id -> [{ptyId, csid}]
        const sessionsByWaiting = {};
        const avulso = [];           // {ptyId, csid|null}
        for (const [ptyId, info] of Object.entries(this.app.terminals)) {
            const csid = this.app.sessionLinkKeyOf(ptyId);
            const legacyCsid = info.claudeSessionId || null;
            const link = (csid && links[csid]) || (legacyCsid && links[legacyCsid]);
            if (link && link.wi_id && waitingItemIds.has(link.wi_id)) {
                (sessionsByWaiting[link.wi_id] = sessionsByWaiting[link.wi_id] || [])
                    .push({ ptyId, csid: csid || legacyCsid });
                continue;
            }
            if (link && link.archived) continue;  // shown only under Arquivado
            if (link && link.wi_id && activeItemIds.has(link.wi_id)) {
                (sessionsByItem[link.wi_id] = sessionsByItem[link.wi_id] || []).push({ ptyId, csid });
            } else {
                avulso.push({ ptyId, csid: csid || legacyCsid });
            }
        }

        let html = '';
        html += this._renderWorkSection(items, sessionsByItem);
        html += this._renderWaiting(waitingItems, sessionsByWaiting);
        html += this._renderAvulso(avulso);
        html += this._renderNovaAba(groups);
        html += this._renderArchived(archivedItems, waitingItemIds, links, liveByCsid);

        this.container.innerHTML = html;
        this._renderTypeSelector();
        this._bindEvents();
    }

    _renderWorkSection(items, sessionsByItem) {
        // ＋ depois do .count (que usa margin-left:auto) para o contador seguir alinhado à direita.
        let html = `<div class="sec-head"><span class="caret">▼</span>Trabalho<span class="count">${items.length}</span>`
                 + `<span class="sec-add" data-newwi="1" title="criar work item (issue, mensagem ou TODO) sem abrir aba">＋</span></div>`;
        if (items.length === 0) {
            html += `<div class="empty-hint">Crie um work item no ＋ ou vincule uma aba a uma issue, mensagem ou TODO</div>`;
            return html;
        }
        items.forEach((it, idx) => {
            const color = SRC_COLOR[it.source] || 'var(--t-todo)';
            const sess = sessionsByItem[it.id] || [];
            const statuses = sess.map(s => this._sessionStatus(s.ptyId));
            const hasRunning = statuses.includes('running') || statuses.includes('tooluse');
            const dot = this._itemDot(statuses);
            const due = this._dueInfo(it);
            const statusPill = it.source === 'jira' && it.status
                ? `<span class="st">${this._esc(it.status)}</span>` : '';
            const titleLine = it.source === 'teams'
                ? this._esc(it.title)
                : this._esc(it.title);

            const isLocked = this.app.lockedWiId === it.id;
            html += `<div class="wi ${hasRunning ? 'has-running' : ''} ${isLocked ? 'locked' : ''}" data-wi="${it.id}" draggable="true" style="--c:${color}">`;
            html += `<div class="wi-top" data-lockwi="${it.id}" title="${isLocked ? 'clique para destravar (mostra todas as abas)' : 'clique para travar: só as abas deste item ficam expandidas'}">`;
            html += `<span class="pri" title="arraste para repriorizar">${idx + 1}</span>`;
            html += `<div class="wi-body">`;
            html += `<div class="l1"><span class="wi-key">${this._esc(this._keyLabel(it))}</span>${statusPill}`;
            html += `<span class="due ${due.cls}">${due.txt}</span></div>`;
            const renameAttr = (it.source === 'manual' || it.source === 'teams')
                ? ` data-renamewi="${it.id}" title="duplo-clique para renomear"` : '';
            html += `<div class="wi-title"${renameAttr}>${titleLine}</div>`;
            html += `</div><svg class="wi-wait" viewBox="0 0 20 20" title="aguardar resposta (esconde as abas, mantém o cache)" data-waitwi="${it.id}"><use href="#i-wait"/></svg>`;
            html += `<svg class="wi-arch" viewBox="0 0 20 20" title="arquivar (esconde o item e suas abas)" data-archwi="${it.id}"><use href="#i-arch"/></svg>`;
            html += `<svg class="wi-done" viewBox="0 0 20 20" title="concluir work item" data-complete="${it.id}"><use href="#i-check"/></svg>`;
            html += `<span class="wi-dot ${dot}"></span></div>`;

            for (const s of sess) {
                const st = this._sessionStatus(s.ptyId);
                const active = this.app.activeTerminalId === s.ptyId;
                html += `<div class="sess ${active ? 'on' : ''}" data-id="${s.ptyId}">`;
                html += `<span class="dot ${this._dotClass(st)}"></span>`;
                html += `<span class="nm" data-id="${s.ptyId}">${this._esc(this.app.getDisplayName(s.ptyId))}</span>`;
                html += this._cacheTimer(s.ptyId);
                html += `<svg class="arch" viewBox="0 0 20 20" data-arch="${s.csid}" title="arquivar aba"><use href="#i-arch"/></svg></div>`;
            }
            const label = it.source === 'jira' ? '＋ nova aba nesta issue' : '＋ nova aba';
            html += `<div class="addSess" data-additem="${it.id}">${label}</div>`;
            html += `</div>`;
        });
        return html;
    }

    _renderWaiting(items, sessionsByItem) {
        if (!items.length) return '';
        let html = `<div class="sec-head waiting-head"><span class="caret">◆</span>Aguardando`
            + `<span class="count">${items.length}</span></div>`;
        for (const it of items) {
            const color = SRC_COLOR[it.source] || 'var(--t-todo)';
            const sess = sessionsByItem[it.id] || [];
            const since = wiWaitingElapsed(it.waiting_since);
            const tabs = sess.length ? `${sess.length} aba${sess.length === 1 ? '' : 's'}` : 'sem aba viva';
            html += `<div class="wi wi-waiting" data-wi="${it.id}" style="--c:${color}">`;
            html += `<div class="wi-top">`;
            html += `<span class="wait-pulse d-waiting"></span>`;
            html += `<div class="wi-body"><div class="l1"><span class="wi-key">${this._esc(this._keyLabel(it))}</span>`;
            html += `<span class="wait-label">aguardando</span><span class="wait-since">há ${since}</span></div>`;
            html += `<div class="wi-title">${this._esc(it.title)}</div>`;
            html += `<div class="wait-meta"><span>${tabs}</span>${this._waitingCacheSummary(sess)}</div></div>`;
            html += `<svg class="wi-resume" viewBox="0 0 20 20" title="retomar e revelar abas" data-resumewi="${it.id}"><use href="#i-play"/></svg>`;
            html += `<svg class="wi-arch" viewBox="0 0 20 20" title="arquivar de verdade" data-archwi="${it.id}"><use href="#i-arch"/></svg>`;
            html += `</div></div>`;
        }
        return html;
    }

    _renderAvulso(avulso) {
        if (avulso.length === 0) return '';
        let html = `<div class="sec-head"><span class="caret">▼</span>Avulso<span class="count">${avulso.length}</span></div>`;
        html += `<div class="wi" style="--c:#2f2f38">`;
        avulso.forEach((s, i) => {
            const st = this._sessionStatus(s.ptyId);
            const active = this.app.activeTerminalId === s.ptyId;
            const linkable = !!s.csid;
            html += `<div class="sess ${active ? 'on' : ''}" data-id="${s.ptyId}" style="${i === 0 ? 'border-top:none;' : ''}padding-left:14px">`;
            html += `<span class="dot ${this._dotClass(st)}"></span>`;
            html += `<span class="nm" data-id="${s.ptyId}">${this._esc(this.app.getDisplayName(s.ptyId))}</span>`;
            if (linkable) {
                html += `<svg class="arch" viewBox="0 0 20 20" data-link="${s.ptyId}" title="vincular a work item"><use href="#i-link"/></svg>`;
            }
            html += `</div>`;
        });
        html += `</div>`;
        return html;
    }

    _renderNovaAba(groups) {
        // Favourite = "clones" first; the rest behind "＋ outra".
        const fav = groups.filter(g => /clone/i.test(g.name));
        const rest = groups.filter(g => !/clone/i.test(g.name));
        const shown = this._moreGroups ? rest : rest.slice(0, 7);
        let html = `<div class="sec-head"><span class="caret">▼</span>Nova aba</div><div class="chips">`;
        for (const g of fav) {
            html += `<span class="chip fav" data-group="${this._esc(g.name)}" data-path="${this._esc(g.path)}">${this._esc(g.name)}</span>`;
        }
        for (const g of shown) {
            html += `<span class="chip" data-group="${this._esc(g.name)}" data-path="${this._esc(g.path)}">${this._esc(g.name)}</span>`;
        }
        if (!this._moreGroups && rest.length > 7) {
            html += `<span class="chip more" data-moregroups="1">＋ outra…</span>`;
        }
        html += `<span class="chip more" data-addgroup="1">＋ pasta</span>`;
        html += `</div>`;
        return html;
    }

    _renderArchived(archivedItems, waitingItemIds, links, liveByCsid) {
        const archIds = new Set(archivedItems.map(it => it.id));
        // Sessões arquivadas soltas = arquivadas individualmente (não via item arquivado, que já
        // aparece como card). Evita mostrar a mesma aba duas vezes.
        const looseSessions = Object.entries(links).filter(([, l]) =>
            l.archived
            && !(l.wi_id && archIds.has(l.wi_id))
            && !(l.wi_id && waitingItemIds.has(l.wi_id)));
        const count = archivedItems.length + looseSessions.length;
        if (count === 0) return '';
        let html = `<div class="sec-head" data-togglearch="1"><span class="caret">${this._archivedOpen ? '▼' : '▶'}</span>Arquivado<span class="count">${count}</span></div>`;
        if (this._archivedOpen) {
            // work items arquivados como cards — desarquivar traz de volta o item e suas abas
            for (const it of archivedItems) {
                const color = SRC_COLOR[it.source] || 'var(--t-todo)';
                html += `<div class="wi" style="--c:${color}">`;
                html += `<div class="wi-top"><span class="pri" style="opacity:.35">▤</span><div class="wi-body">`;
                html += `<div class="l1"><span class="wi-key">${this._esc(this._keyLabel(it))}</span></div>`;
                html += `<div class="wi-title">${this._esc(it.title)}</div></div>`;
                html += `<svg class="wi-arch on" viewBox="0 0 20 20" title="desarquivar" data-unarchwi="${it.id}"><use href="#i-arch"/></svg>`;
                html += `<span class="wi-dot d-stopped"></span></div>`;
            }
            // sessões arquivadas individualmente (fluxo antigo), soltas
            if (looseSessions.length) {
                html += `<div class="wi" style="--c:#2f2f38">`;
                for (const [csid, l] of looseSessions) {
                    const live = liveByCsid[csid];
                    html += `<div class="sess" ${live ? `data-id="${live}"` : ''} style="padding-left:14px">`;
                    html += `<span class="dot d-stopped"></span>`;
                    html += `<span class="nm">${this._esc(l.name || csid.slice(0, 8))}</span>`;
                    html += `<svg class="arch" viewBox="0 0 20 20" data-unarch="${csid}" title="desarquivar"><use href="#i-arch"/></svg></div>`;
                }
                html += `</div>`;
            }
        }
        return html;
    }

    // ---------- events ----------
    _bindEvents() {
        const q = sel => this.container.querySelectorAll(sel);

        // switch to a session tab
        q('.sess[data-id]').forEach(el => el.addEventListener('click', (e) => {
            if (e.target.closest('svg')) return;
            this.app.switchToTerminal(el.dataset.id);
        }));

        // rename via right-click on session name
        q('.sess .nm[data-id]').forEach(el => el.addEventListener('contextmenu', (e) => {
            e.preventDefault(); e.stopPropagation();
            this._startRename(el, el.dataset.id);
        }));

        // rename a manual TODO: double-click (discoverable) or right-click on its title
        q('.wi-title[data-renamewi]').forEach(el => {
            const start = (e) => { e.preventDefault(); e.stopPropagation(); this._startRenameItem(el, el.dataset.renamewi); };
            el.addEventListener('dblclick', start);
            el.addEventListener('contextmenu', start);
        });

        // complete work item (hover check)
        q('.wi-done[data-complete]').forEach(el => el.addEventListener('click', (e) => {
            e.stopPropagation();
            this.app.completeWorkItem(el.dataset.complete);
        }));

        q('.wi-wait[data-waitwi]').forEach(el => el.addEventListener('click', (e) => {
            e.stopPropagation();
            this.app.setWorkItemWaiting(el.dataset.waitwi, true);
        }));
        q('.wi-resume[data-resumewi]').forEach(el => el.addEventListener('click', (e) => {
            e.stopPropagation();
            this.app.setWorkItemWaiting(el.dataset.resumewi, false);
        }));

        // archive / unarchive a session
        q('.arch[data-arch]').forEach(el => el.addEventListener('click', (e) => {
            e.stopPropagation(); this.app.archiveSession(el.dataset.arch, true);
        }));
        q('.arch[data-unarch]').forEach(el => el.addEventListener('click', (e) => {
            e.stopPropagation(); this.app.archiveSession(el.dataset.unarch, false);
        }));

        // archive / unarchive a whole work item (cascades to its tabs)
        q('.wi-arch[data-archwi]').forEach(el => el.addEventListener('click', (e) => {
            e.stopPropagation(); this.app.archiveWorkItem(el.dataset.archwi, true);
        }));
        q('.wi-arch[data-unarchwi]').forEach(el => el.addEventListener('click', (e) => {
            e.stopPropagation(); this.app.archiveWorkItem(el.dataset.unarchwi, false);
        }));

        // link an avulso session
        q('.arch[data-link]').forEach(el => el.addEventListener('click', (e) => {
            e.stopPropagation();
            if (window.app.workItemsUI) window.app.workItemsUI.openLinkPopover(el.dataset.link);
        }));

        // clique no cabeçalho do card = trava/destrava a gaveta desse work item.
        // Ignora cliques nos ícones (arquivar/concluir) e no título (renomear).
        q('.wi-top[data-lockwi]').forEach(el => el.addEventListener('click', (e) => {
            // .pri é a alça de arraste (repriorizar); svg são arquivar/concluir; .wi-title renomeia.
            if (e.target.closest('svg') || e.target.closest('.wi-title') || e.target.closest('.pri')) return;
            this.app.toggleLockedItem(el.dataset.lockwi);
        }));

        // ＋ do cabeçalho Trabalho: cria um work item sem aba alguma (mesmo popover, sem alvo)
        q('.sec-add[data-newwi]').forEach(el => el.addEventListener('click', (e) => {
            e.stopPropagation();
            if (this.app.workItemsUI) this.app.workItemsUI.openLinkPopover(null);
        }));

        // new tab on an item
        q('.addSess[data-additem]').forEach(el => el.addEventListener('click', (e) => {
            e.stopPropagation(); this.app.openTerminalForItem(el.dataset.additem);
        }));

        // group chips -> launch terminal
        q('.chip[data-group]').forEach(el => el.addEventListener('click', () => {
            this.app.openTerminal(el.dataset.group, el.dataset.path);
        }));
        q('.chip[data-moregroups]').forEach(el => el.addEventListener('click', () => {
            this._moreGroups = true; this.render();
        }));
        q('.chip[data-addgroup]').forEach(el => el.addEventListener('click', () => this.app.addGroup()));

        // archived section toggle
        q('.sec-head[data-togglearch]').forEach(el => el.addEventListener('click', () => {
            this._archivedOpen = !this._archivedOpen; this.render();
        }));

        this._bindDrag();
    }

    /** Reorder work items by dragging a card. sort_order is the priority axis (≠ tab order). */
    _bindDrag() {
        this.container.querySelectorAll('.wi[data-wi]').forEach(card => {
            card.addEventListener('dragstart', (e) => {
                this._dragId = card.dataset.wi;
                e.dataTransfer.effectAllowed = 'move';
            });
            card.addEventListener('dragover', (e) => { e.preventDefault(); card.classList.add('drop-hint'); });
            card.addEventListener('dragleave', () => card.classList.remove('drop-hint'));
            card.addEventListener('drop', (e) => {
                e.preventDefault(); card.classList.remove('drop-hint');
                const targetId = card.dataset.wi;
                if (!this._dragId || this._dragId === targetId) return;
                const ids = (this.app.workItems.items || []).filter(it => !it.done)
                    .sort((a, b) => (a.sort_order || 0) - (b.sort_order || 0)).map(it => it.id);
                const from = ids.indexOf(this._dragId), to = ids.indexOf(targetId);
                if (from < 0 || to < 0) return;
                ids.splice(from, 1); ids.splice(to, 0, this._dragId);
                this._dragId = null;
                this.app.reorderWorkItems(ids);
            });
        });
    }

    _startRename(nameEl, sessionId) {
        this.app._isRenaming = true;
        const currentName = this.app.getDisplayName(sessionId);
        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'sidebar-rename-input';
        input.value = currentName;
        nameEl.textContent = '';
        nameEl.appendChild(input);
        input.focus();
        input.select();
        const finish = () => this.app.renameTerminal(sessionId, input.value.trim() || null);
        input.addEventListener('blur', finish);
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
            else if (e.key === 'Escape') { e.preventDefault(); input.value = ''; input.blur(); }
        });
        input.addEventListener('click', (e) => e.stopPropagation());
    }

    /** Open inline-rename for a manual item by id, once its card is in the DOM. */
    renameItemById(wiId) {
        requestAnimationFrame(() => {
            const el = document.querySelector(`.wi-title[data-renamewi="${wiId}"]`);
            if (el) this._startRenameItem(el, wiId);
        });
    }

    /** Inline-rename a manual work item's title from the sidebar card. */
    _startRenameItem(titleEl, wiId) {
        this.app._isRenaming = true;
        const it = (this.app.workItems.items || []).find(x => x.id === wiId);
        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'sidebar-rename-input';
        input.value = it ? (it.title || '') : '';
        titleEl.textContent = '';
        titleEl.appendChild(input);
        input.focus();
        input.select();
        let done = false;
        const finish = () => {
            if (done) return;
            done = true;
            const v = input.value.trim();
            if (v) this.app.renameWorkItem(wiId, v);
            else { this.app._isRenaming = false; this.render(); }
        };
        input.addEventListener('blur', finish);
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
            else if (e.key === 'Escape') { e.preventDefault(); done = true; this.app._isRenaming = false; this.render(); }
        });
        input.addEventListener('click', (e) => e.stopPropagation());
    }

    _renderTypeSelector() {
        const footer = this.container.parentElement.querySelector('.sidebar-footer');
        if (!footer) return;
        let selector = footer.querySelector('.terminal-type-selector');
        if (!selector) {
            selector = document.createElement('div');
            selector.className = 'terminal-type-selector';
            selector.innerHTML = `
                <label class="terminal-type-label">Tipo de terminal</label>
                <select id="terminal-type-select" class="terminal-type-select">
                    <option value="claude">Claude Code</option>
                    <option value="codex">Codex</option>
                    <option value="opencode">OpenCode</option>
                    <option value="powershell">PowerShell</option>
                </select>`;
            footer.insertBefore(selector, footer.firstChild);
        }
        const select = selector.querySelector('select');
        select.value = this._terminalType;
        select.onchange = async () => {
            this._terminalType = select.value;
            await window.pywebview.api.set_terminal_type(select.value);
        };
    }
}

if (typeof module !== 'undefined' && module.exports) {
    module.exports = { wiWaitingElapsed, wiPartitionWorkItems, Sidebar };
}
