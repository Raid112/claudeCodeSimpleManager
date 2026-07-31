/**
 * Main app — orchestrates sidebar, tabs, and terminal instances.
 */

/**
 * Splitter — drags the divider between terminal-pane and composer-pane.
 * Persists ratio to config.json via bridge (debounced).
 */
class Splitter {
    constructor(splitAreaEl, splitterEl, terminalPaneEl, composerPaneEl) {
        this.splitArea = splitAreaEl;
        this.splitter = splitterEl;
        this.terminalPane = terminalPaneEl;
        this.composerPane = composerPaneEl;
        this._dragging = false;
        this._saveTimer = null;
        this.onResize = null; // callback for re-fitting terminal

        this.splitter.addEventListener('mousedown', (e) => this._onDown(e));
        document.addEventListener('mousemove', (e) => this._onMove(e));
        document.addEventListener('mouseup', () => this._onUp());

        // Double-click splitter → toggle composer collapse
        this.splitter.addEventListener('dblclick', () => this.toggleCollapse());
    }

    setRatio(ratio) {
        ratio = Math.max(0.1, Math.min(0.95, ratio));
        this.terminalPane.style.flexBasis = `${ratio * 100}%`;
        this.composerPane.style.flexBasis = `${(1 - ratio) * 100}%`;
        this._currentRatio = ratio;
        if (this.onResize) this.onResize();
    }

    setCollapsed(collapsed) {
        this.composerPane.classList.toggle('collapsed', collapsed);
        // Keep splitter visible even when collapsed — it's the only way back via mouse.
        this.splitter.classList.toggle('collapsed-handle', collapsed);
        this.splitter.title = collapsed
            ? 'Composer minimizado — clique duplo ou Ctrl+\\ para reabrir'
            : 'Arraste para redimensionar (duplo-clique minimiza)';
        this.terminalPane.style.flexBasis = collapsed ? '100%' : `${(this._currentRatio || 0.6) * 100}%`;
        this._collapsed = collapsed;
        if (this.onResize) this.onResize();
    }

    toggleCollapse() {
        this.setCollapsed(!this._collapsed);
        this._persist();
    }

    _onDown(e) {
        // When collapsed, mousedown on the handle opens the composer back instead of dragging.
        if (this._collapsed) {
            this.setCollapsed(false);
            this._persist();
            e.preventDefault();
            return;
        }
        this._dragging = true;
        this.splitter.classList.add('dragging');
        this.splitArea.classList.add('resizing');
        e.preventDefault();
    }

    _onMove(e) {
        if (!this._dragging) return;
        const rect = this.splitArea.getBoundingClientRect();
        const offset = e.clientY - rect.top;
        const ratio = offset / rect.height;
        this.setRatio(ratio);
    }

    _onUp() {
        if (!this._dragging) return;
        this._dragging = false;
        this.splitter.classList.remove('dragging');
        this.splitArea.classList.remove('resizing');
        this._persist();
    }

    _persist() {
        if (this._saveTimer) clearTimeout(this._saveTimer);
        this._saveTimer = setTimeout(() => {
            window.pywebview.api.set_layout({
                split_ratio: this._currentRatio,
                composer_collapsed: !!this._collapsed,
            });
        }, 300);
    }
}

class App {
    constructor() {
        this.terminals = {};  // { sessionId: { instance: TerminalInstance, groupName, container } }
        this.activeTerminalId = null;
        this.wsPort = null;
        this._isRenaming = false;

        // Work-items snapshot ({version, items, session_links}), cached and refreshed only on
        // mutation (create/link/complete/archive/reorder/jira-refresh) — NOT on the 2s status
        // poll. Live state (running/ready) is joined in from get_terminals at render time.
        this.workItems = { items: [], session_links: {} };

        // Gaveta: id do work item travado (só as abas dele ficam expandidas). null = nada travado.
        this.lockedWiId = null;
        this.sidebarWidth = 260;

        this.sidebarEl = document.getElementById('sidebar-groups');
        this.tabBarEl = document.getElementById('tab-bar');
        this.terminalContainerEl = document.getElementById('terminal-container');
        this.welcomeEl = document.getElementById('welcome');

        this.sidebar = new Sidebar(this.sidebarEl, this);
        this.tabBar = new TabBar(this.tabBarEl, this);

        // Splitter for terminal/composer panes
        this.splitter = new Splitter(
            document.getElementById('split-area'),
            document.getElementById('pane-splitter'),
            document.getElementById('terminal-pane'),
            document.getElementById('composer-pane'),
        );
        this.splitter.onResize = () => this._fitActiveTerminal();

        // Composer (bottom message editor)
        this.composer = new Composer(this);

        // Work-items overlays (link popover, close ceremony, daily digest)
        this.workItemsUI = new WorkItemsUI(this);
        this.agentApprovals = new AgentApprovals(this, document.getElementById('agent-approvals'));
        const dailyBtn = document.getElementById('daily-btn');
        if (dailyBtn) dailyBtn.addEventListener('click', () => this.workItemsUI.openDaily());

        // Add group is now the "＋ pasta" chip in the sidebar's Nova aba section.
        const addGroupBtn = document.getElementById('add-group-btn');
        if (addGroupBtn) addGroupBtn.addEventListener('click', () => this.addGroup());

        // Resize handler
        window.addEventListener('resize', () => this._fitActiveTerminal());

        // Ctrl+\ → toggle composer collapse. Capture phase so we run BEFORE xterm
        // intercepts the keystroke and forwards it to the PTY.
        window.addEventListener('keydown', (e) => {
            if (e.ctrlKey && (e.key === '\\' || e.code === 'Backslash' || e.keyCode === 220)) {
                e.preventDefault();
                e.stopPropagation();
                this.splitter.toggleCollapse();
            }
        }, true);

        // Status polling
        setInterval(() => this.refreshStatus(), 2000);
        setInterval(() => this.agentApprovals.refresh(), 2000);

        // Cache-timer tick — re-render tab bar each 15s so MM:SS counts down visibly
        // without coupling to the heavier 2s status poll.
        setInterval(() => {
            if (Object.keys(this.terminals).length > 0) this.tabBar.render();
        }, 15000);

        this._bindSidebarResize();
    }

    // ---------- sidebar resize ----------
    /** Arrastar a borda direita da sidebar. O handle mora em .sidebar (não em
     * #sidebar-groups, que é reescrito inteiro a cada 2s e mataria o arrasto).
     * O fit do xterm só roda no mouseup: cada fit manda um resize do PTY pelo WebSocket. */
    _bindSidebarResize() {
        const handle = document.getElementById('sidebar-resizer');
        const el = document.getElementById('sidebar');
        if (!handle || !el) return;
        let dragging = false;
        handle.addEventListener('mousedown', (e) => {
            e.preventDefault();
            dragging = true;
            document.body.classList.add('col-resizing');
        });
        const stop = () => {
            if (!dragging) return;
            dragging = false;
            document.body.classList.remove('col-resizing');
            this._fitActiveTerminal();
            try { window.pywebview.api.set_layout({ sidebar_width: this.sidebarWidth }); } catch (e) {}
        };
        document.addEventListener('mousemove', (e) => {
            if (!dragging) return;
            // Soltar o botão fora da janela não dispara mouseup — e o handle fica na borda
            // esquerda, onde arrastar pra fora é fácil. e.buttons===0 = já soltou.
            if (e.buttons === 0) { stop(); return; }
            this.setSidebarWidth(e.clientX - el.getBoundingClientRect().left);
        });
        document.addEventListener('mouseup', stop);
    }

    setSidebarWidth(px) {
        this.sidebarWidth = Math.max(180, Math.min(600, Math.round(px)));
        document.documentElement.style.setProperty('--sidebar-w', `${this.sidebarWidth}px`);
    }

    /** Trava/destrava a gaveta de um work item. Travado = só as abas dele expandidas na barra;
     * clicar no mesmo card de novo destrava (e nada colapsa). */
    async toggleLockedItem(wiId) {
        this.lockedWiId = (this.lockedWiId === wiId) ? null : wiId;
        this.tabBar.render();
        await this.sidebar.render();
        await this.agentApprovals.refresh();
        try { await window.pywebview.api.set_layout({ locked_wi_id: this.lockedWiId }); } catch (e) {}
    }

    async init() {
        this.wsPort = await window.pywebview.api.get_ws_port();

        // Apply saved layout before sessions render so xterm fits the right size on first paint
        try {
            const layout = await window.pywebview.api.get_layout();
            if (layout) {
                this.splitter.setRatio(layout.split_ratio || 0.6);
                this.splitter.setCollapsed(!!layout.composer_collapsed);
                this.setSidebarWidth(layout.sidebar_width || 260);
                this.lockedWiId = layout.locked_wi_id || null;
            }
        } catch (e) { /* defaults remain */ }

        // Load work-items snapshot before first sidebar paint so links render immediately.
        try {
            this.workItems = await window.pywebview.api.list_work_items();
        } catch (e) { /* empty default remains */ }

        await this.sidebar.render();
        this._updateWelcome();

        // Restore previous sessions
        await this._restoreSessions();

        // Periodic session save (every 10s) to survive crashes
        setInterval(() => this._saveSessions(), 10000);
    }

    async openTerminal(groupName, path, continueSession = false, claudeSessionId = null, terminalType = null, agentSessionId = null, sessionKey = null) {
        const result = await window.pywebview.api.open_terminal(
            groupName, path, 120, 30, terminalType, continueSession, claudeSessionId,
            agentSessionId, sessionKey);
        const sessionId = result.session_id;
        // Fresh claude sessions get their claude_session_id at spawn — capture it so the
        // work-item link is available immediately (no waiting for the 2s poll to backfill).
        if (!claudeSessionId && result.claude_session_id) {
            claudeSessionId = result.claude_session_id;
        }

        // Create container for this terminal
        const wrapper = document.createElement('div');
        wrapper.className = 'terminal-wrapper';
        wrapper.id = `term-${sessionId}`;
        this.terminalContainerEl.appendChild(wrapper);

        // Create xterm instance
        const instance = new TerminalInstance(sessionId, this.wsPort, wrapper, result.ws_capability);

        this.terminals[sessionId] = {
            instance,
            groupName,
            path,
            container: wrapper,
            claudeSessionId: claudeSessionId,
            agentSessionId: result.agent_session_id || agentSessionId || claudeSessionId || null,
            sessionKey: result.session_key || sessionKey || null,
            terminalType: result.terminal_type || terminalType || 'claude',
            customName: null,
        };

        // Switch to this terminal
        this.switchToTerminal(sessionId);
        await this.sidebar.render();
    }

    switchToTerminal(sessionId) {
        if (!this.terminals[sessionId]) return;

        // Save scroll position of the outgoing terminal
        if (this.activeTerminalId && this.terminals[this.activeTerminalId]) {
            const outgoing = this.terminals[this.activeTerminalId].instance;
            const buf = outgoing.term.buffer.active;
            outgoing._savedViewportY = buf.viewportY;
            outgoing._savedAutoFollow = outgoing.autoFollow;
        }

        this.activeTerminalId = sessionId;

        // Hide all, show active. ResizeObserver on the wrapper will fire once layout settles
        // and call fit() — no double-fit hack needed.
        for (const [id, info] of Object.entries(this.terminals)) {
            info.container.classList.toggle('active', id === sessionId);
        }

        this._updateWelcome();
        this.tabBar.render();
        this.sidebar.render();
        if (this.composer) this.composer.refreshTarget();

        // Focus the terminal so its interactive menus (arrows/Enter/Esc, plan mode,
        // selection prompts) get the keyboard immediately. The composer takes focus
        // only when the user clicks into it — otherwise arrows would drive composer
        // history instead of the menu and the tab would feel "stuck".
        const active = this.terminals[sessionId].instance;
        active.focus();
        if (active._savedAutoFollow === false && typeof active._savedViewportY === 'number') {
            // Defer one frame so xterm has rendered after display flip
            requestAnimationFrame(() => {
                try {
                    active.term.scrollToLine(active._savedViewportY);
                    active.autoFollow = false;
                    active._updateScrollBtn();
                } catch (e) { /* ignore */ }
            });
        }
    }

    async closeTerminal(sessionId) {
        const info = this.terminals[sessionId];
        if (!info) return;

        // Dispose xterm + close WS
        info.instance.dispose();
        info.container.remove();

        // Kill PTY on server
        await window.pywebview.api.close_terminal(sessionId);

        delete this.terminals[sessionId];

        // Switch to another tab or show welcome
        const remaining = Object.keys(this.terminals);
        if (remaining.length > 0) {
            this.switchToTerminal(remaining[remaining.length - 1]);
        } else {
            this.activeTerminalId = null;
            this._updateWelcome();
            this.tabBar.render();
        }

        await this.sidebar.render();
        this._saveSessions();
    }

    async addGroup() {
        const result = await window.pywebview.api.add_group();
        if (result) {
            await this.sidebar.render();
        }
    }

    async refreshStatus() {
        // Update alive status from backend
        const serverTerminals = await window.pywebview.api.get_terminals();
        const serverMap = {};
        let sessionMetadataChanged = false;
        for (const t of serverTerminals) {
            serverMap[t.id] = t;
        }

        for (const [id, info] of Object.entries(this.terminals)) {
            const serverData = serverMap[id];
            if (serverData !== undefined) {
                info.instance.alive = serverData.is_alive;
                // Sync the Claude session id from the backend — and FOLLOW it when it
                // changes (user ran /clear or /resume, backend reconciled + migrated the
                // link). Not just the first-detect case, or restore would resume the old id.
                if (serverData.claude_session_id && serverData.claude_session_id !== info.claudeSessionId) {
                    info.claudeSessionId = serverData.claude_session_id;
                }
                if (serverData.agent_session_id && serverData.agent_session_id !== info.agentSessionId) {
                    info.agentSessionId = serverData.agent_session_id;
                    sessionMetadataChanged = true;
                }
                if (serverData.session_key && !info.sessionKey) {
                    info.sessionKey = serverData.session_key;
                    sessionMetadataChanged = true;
                }
                if (serverData.terminal_type && !info.terminalType) {
                    info.terminalType = serverData.terminal_type;
                }
                // Authoritative hook state (claude only; null otherwise).
                info.instance.backendState = serverData.state || null;
                info.instance.backendStateTs = serverData.state_ts ? serverData.state_ts * 1000 : null;
                this._maybePlayStateSound(id, info.instance, serverData.state, serverData.state_ts);
            }
        }

        // Persist provider IDs as soon as Codex/OpenCode discovery completes; a
        // quick app close must not lose the only information needed for resume.
        if (sessionMetadataChanged) this._saveSessions();

        // Skip re-render while user is editing a tab name
        if (this._isRenaming) return;

        // Always re-render to pick up ready/running transitions based on idle time
        this.tabBar.render();
        await this.sidebar.render();
        if (this.composer) this.composer.refreshTarget();
    }

    /**
     * Edge-trigger transition sounds from the authoritative hook state.
     * Driven by state_ts (each hook event bumps it) so it fires once per real
     * transition, not once per 2s poll. First observation sets a baseline with
     * no sound, so restoring sessions with pre-existing state stays silent.
     */
    _maybePlayStateSound(id, instance, state, stateTs) {
        const ts = stateTs || 0;
        if (instance._lastStateTs === undefined) {
            instance._lastStateTs = ts;
            return;
        }
        if (ts > instance._lastStateTs) {
            instance._lastStateTs = ts;
            if (state === 'ready') {
                TerminalInstance.playReadySound();
            } else if (state === 'tooluse' || state === 'waiting') {
                TerminalInstance.playToolUseSound();
            }
            // Toast only when the window is in the background — sound covers the
            // foreground case and the toast would be redundant noise.
            if ((state === 'ready' || state === 'tooluse') && !document.hasFocus()) {
                try {
                    window.pywebview.api.notify_state(id, this.getDisplayName(id), state);
                } catch (e) { /* notifications are best-effort */ }
            }
        }
    }

    async _restoreSessions() {
        try {
            const saved = await window.pywebview.api.load_sessions();
            if (!saved || !saved.tabs || saved.tabs.length === 0) return;

            this._restoring = true;
            for (const tab of saved.tabs) {
                try {
                    const tType = tab.terminal_type || null;
                    const providerSessionId = tab.agent_session_id || tab.claude_session_id || null;
                    if (providerSessionId) {
                        await this.openTerminal(tab.group_name, tab.path, false, tab.claude_session_id, tType, providerSessionId, tab.session_key || null);
                    } else {
                        await this.openTerminal(tab.group_name, tab.path, tType === 'claude', null, tType, null, tab.session_key || null);
                    }
                    // Restore custom name + cache timer if saved
                    const lastId = Object.keys(this.terminals).pop();
                    if (lastId) {
                        if (tab.custom_name) this.terminals[lastId].customName = tab.custom_name;
                        if (tab.last_message_time) {
                            this.terminals[lastId].instance.lastUserMessageTime = tab.last_message_time;
                        }
                    }
                } catch (e) {
                    console.warn(`Failed to restore tab ${tab.group_name}, trying fresh`, e);
                    try {
                        await this.openTerminal(tab.group_name, tab.path, false, null, tab.terminal_type || null, null, tab.session_key || null);
                    } catch (e2) {
                        console.error(`Failed to open terminal for ${tab.group_name}`, e2);
                    }
                }
            }
            this._restoring = false;

            // Switch to previously active tab
            const terminalIds = Object.keys(this.terminals);
            const activeIdx = saved.active_tab_index || 0;
            if (terminalIds.length > 0 && activeIdx < terminalIds.length) {
                this.switchToTerminal(terminalIds[activeIdx]);
            }
        } catch (e) {
            console.error('Session restore failed:', e);
            this._restoring = false;
        }
    }

    _saveSessions() {
        if (this._restoring) return;

        const tabs = [];
        let activeIndex = 0;
        let i = 0;

        for (const [id, info] of Object.entries(this.terminals)) {
            tabs.push({
                group_name: info.groupName,
                path: info.path,
                tab_order: i,
                claude_session_id: info.claudeSessionId || null,
                agent_session_id: info.agentSessionId || null,
                session_key: info.sessionKey || null,
                terminal_type: info.terminalType || 'claude',
                custom_name: info.customName || null,
                last_message_time: info.instance.lastUserMessageTime || null,
            });
            if (id === this.activeTerminalId) {
                activeIndex = i;
            }
            i++;
        }

        if (tabs.length > 0) {
            window.pywebview.api.save_sessions(tabs, activeIndex);
        } else {
            window.pywebview.api.clear_sessions();
        }
    }

    getDisplayName(sessionId) {
        const info = this.terminals[sessionId];
        if (!info) return sessionId;
        if (info.customName) return info.customName;
        return `${info.groupName} #${sessionId.slice(0, 4)}`;
    }

    /** Reorder tabs by moving draggedId to targetId's slot. Rebuilds this.terminals
     *  in the new key order — every Object.entries() consumer follows it automatically. */
    reorderTab(draggedId, targetId) {
        const ids = Object.keys(this.terminals);
        const from = ids.indexOf(draggedId);
        const to = ids.indexOf(targetId);
        if (from < 0 || to < 0 || from === to) return;
        ids.splice(from, 1);
        ids.splice(to, 0, draggedId);
        const reordered = {};
        for (const id of ids) reordered[id] = this.terminals[id];
        this.terminals = reordered;
        this.tabBar.render();
        this._saveSessions();
    }

    renameTerminal(sessionId, newName) {
        this._isRenaming = false;
        const info = this.terminals[sessionId];
        if (!info) return;
        info.customName = newName && newName.trim() ? newName.trim() : null;
        this.tabBar.render();
        this.sidebar.render();
        this._saveSessions();
    }

    // ---------- work items ----------
    /** claude_session_id for a pty session (the durable join key; null for codex/--continue). */
    claudeSessionIdOf(sessionId) {
        const info = this.terminals[sessionId];
        return info ? (info.claudeSessionId || null) : null;
    }

    sessionLinkKeyOf(sessionId) {
        const info = this.terminals[sessionId];
        if (!info) return null;
        if (info.terminalType === 'powershell') return null;
        // Keep the existing Claude work-item keys intact; other providers use
        // the durable manager tab key because their IDs may be discovered later.
        if (info.terminalType === 'claude') return info.claudeSessionId || info.sessionKey || null;
        return info.sessionKey || info.agentSessionId || info.claudeSessionId || null;
    }

    activeClaudeSessionId() {
        return this.activeTerminalId ? this.claudeSessionIdOf(this.activeTerminalId) : null;
    }

    /** The work item a pty session is linked to (via session_links), or null. */
    workItemForSession(sessionId) {
        const info = this.terminals[sessionId];
        const csid = info ? (info.claudeSessionId || null) : null;
        const key = this.sessionLinkKeyOf(sessionId);
        if (!key) return null;
        const link = this.workItems.session_links[key] || (csid && this.workItems.session_links[csid]);
        if (!link || link.archived || !link.wi_id) return null;
        return this.workItems.items.find(it => it.id === link.wi_id) || null;
    }

    /** Refetch the work-items snapshot and re-render the sidebar/tabs. Call after mutations. */
    async refreshWorkItems() {
        try {
            this.workItems = await window.pywebview.api.list_work_items();
        } catch (e) {
            console.warn('list_work_items failed', e);
        }
        await this.sidebar.render();
        this.tabBar.render();
    }

    async linkSessionToItem(sessionId, wiId) {
        const key = this.sessionLinkKeyOf(sessionId);
        if (!key) {
            console.warn('cannot link: session has no stable session key');
            return false;
        }
        const info = this.terminals[sessionId];
        await window.pywebview.api.link_session(
            key, wiId, info.groupName, info.path, this.getDisplayName(sessionId));
        await this.refreshWorkItems();
        return true;
    }

    async createWorkItem(source, title, opts = {}) {
        const it = await window.pywebview.api.create_work_item(
            source, title,
            opts.external_key || null, opts.external_url || null,
            opts.status || null, opts.duedate || null,
            !!opts.duedate_has_time, opts.person || null);
        await this.refreshWorkItems();
        return it;
    }

    /** Create an item from the popover and link the active tab to it in one gesture. */
    async createAndLinkActive(source, title, opts = {}) {
        const sessionId = this.activeTerminalId;
        const it = await this.createWorkItem(source, title, opts);
        if (sessionId && it) await this.linkSessionToItem(sessionId, it.id);
        return it;
    }

    /** Rename a work item's title (manual TODOs). Reflects on every tab/sidebar row that reads it. */
    async renameWorkItem(wiId, title) {
        this._isRenaming = false;
        const t = (title || '').trim();
        if (!t) return;
        try { await window.pywebview.api.rename_work_item(wiId, t); } catch (e) { console.warn('rename_work_item failed', e); }
        await this.refreshWorkItems();
    }

    async completeWorkItem(wiId) {
        await window.pywebview.api.complete_work_item(wiId);
        await this.refreshWorkItems();
    }

    async reopenWorkItem(wiId) {
        await window.pywebview.api.reopen_work_item(wiId);
        await this.refreshWorkItems();
    }

    async setWorkItemWaiting(wiId, waiting = true) {
        const matching = Object.entries(this.terminals)
            .filter(([ptyId, info]) => {
                const key = this.sessionLinkKeyOf(ptyId);
                const legacy = info.claudeSessionId || null;
                const link = (key && this.workItems.session_links[key])
                    || (legacy && this.workItems.session_links[legacy]);
                return link && link.wi_id === wiId;
            })
            .map(([ptyId]) => ptyId);

        if (waiting && matching.includes(this.activeTerminalId)) {
            const next = Object.keys(this.terminals).find(id => !matching.includes(id));
            if (next) this.switchToTerminal(next);
        }
        await window.pywebview.api.set_work_item_waiting(wiId, waiting);
        await this.refreshWorkItems();
        if (!waiting && matching.length) this.switchToTerminal(matching[0]);
    }

    async archiveSession(sessionKey, archived = true) {
        await window.pywebview.api.archive_session(sessionKey, archived);
        await this.refreshWorkItems();
    }

    /** Archive/unarchive a whole work item. Its tabs cascade-hide (backend flags their links),
     * so if the active tab belongs to it, jump focus to a still-visible tab first. */
    async archiveWorkItem(wiId, archived = true) {
        if (archived && this.activeTerminalId) {
            const hidden = new Set();
            for (const [ptyId, info] of Object.entries(this.terminals)) {
                const key = this.sessionLinkKeyOf(ptyId);
                const link = key ? this.workItems.session_links[key] : null;
                if (link && link.wi_id === wiId) hidden.add(ptyId);
            }
            if (hidden.has(this.activeTerminalId)) {
                const next = Object.keys(this.terminals).find(id => !hidden.has(id));
                if (next) this.switchToTerminal(next);
            }
        }
        await window.pywebview.api.archive_work_item(wiId, archived);
        await this.refreshWorkItems();
    }

    async reorderWorkItems(orderedIds) {
        await window.pywebview.api.reorder_work_items(orderedIds);
        await this.refreshWorkItems();
    }

    /** Open a new terminal for an existing work item, reusing a folder it was used in. */
    async openTerminalForItem(wiId) {
        // Prefer the directory of any session already linked to this item.
        let group = null, path = null;
        for (const link of Object.values(this.workItems.session_links)) {
            if (link.wi_id === wiId && link.group_name && link.path) {
                group = link.group_name; path = link.path;
            }
        }
        // Fall back to the active terminal's folder.
        if (!group && this.activeTerminalId) {
            const info = this.terminals[this.activeTerminalId];
            group = info.groupName; path = info.path;
        }
        // Item criado pelo ＋ (sem aba) e nenhum terminal aberto: não há pasta a herdar.
        // Usa a favorita (clone) ou a primeira do config — abrir na pasta errada custa um
        // fechar-aba; não abrir nada é um botão morto.
        if (!group || !path) {
            const groups = await window.pywebview.api.get_groups();
            const g = groups.find(x => /clone/i.test(x.name)) || groups[0];
            if (g) { group = g.name; path = g.path; }
        }
        if (!group || !path) {
            console.warn('openTerminalForItem: no folder known for item', wiId);
            return;
        }
        await this.openTerminal(group, path);
        if (this.activeTerminalId) await this.linkSessionToItem(this.activeTerminalId, wiId);
    }

    _fitActiveTerminal() {
        if (this.activeTerminalId && this.terminals[this.activeTerminalId]) {
            this.terminals[this.activeTerminalId].instance.fit();
        }
    }

    _updateWelcome() {
        const hasTerminals = Object.keys(this.terminals).length > 0;
        this.welcomeEl.style.display = hasTerminals ? 'none' : 'flex';
    }
}

// Boot
window.addEventListener('pywebviewready', () => {
    window.app = new App();
    window.app.init();
});
