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

        // Add group button
        document.getElementById('add-group-btn').addEventListener('click', () => this.addGroup());

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

        // Cache-timer tick — re-render tab bar each 15s so MM:SS counts down visibly
        // without coupling to the heavier 2s status poll.
        setInterval(() => {
            if (Object.keys(this.terminals).length > 0) this.tabBar.render();
        }, 15000);
    }

    async init() {
        this.wsPort = await window.pywebview.api.get_ws_port();

        // Apply saved layout before sessions render so xterm fits the right size on first paint
        try {
            const layout = await window.pywebview.api.get_layout();
            if (layout) {
                this.splitter.setRatio(layout.split_ratio || 0.6);
                this.splitter.setCollapsed(!!layout.composer_collapsed);
            }
        } catch (e) { /* defaults remain */ }

        await this.sidebar.render();
        this._updateWelcome();

        // Restore previous sessions
        await this._restoreSessions();

        // Periodic session save (every 10s) to survive crashes
        setInterval(() => this._saveSessions(), 10000);
    }

    async openTerminal(groupName, path, continueSession = false, claudeSessionId = null, terminalType = null) {
        const result = await window.pywebview.api.open_terminal(groupName, path, 120, 30, terminalType, continueSession, claudeSessionId);
        const sessionId = result.session_id;

        // Create container for this terminal
        const wrapper = document.createElement('div');
        wrapper.className = 'terminal-wrapper';
        wrapper.id = `term-${sessionId}`;
        this.terminalContainerEl.appendChild(wrapper);

        // Create xterm instance
        const instance = new TerminalInstance(sessionId, this.wsPort, wrapper);

        this.terminals[sessionId] = {
            instance,
            groupName,
            path,
            container: wrapper,
            claudeSessionId: claudeSessionId,
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

        // Focus composer (primary input now lives there). Terminal stays read-first.
        const active = this.terminals[sessionId].instance;
        if (this.composer) {
            this.composer.focus();
        } else {
            active.focus();
        }
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
        for (const t of serverTerminals) {
            serverMap[t.id] = t;
        }

        for (const [id, info] of Object.entries(this.terminals)) {
            const serverData = serverMap[id];
            if (serverData !== undefined) {
                info.instance.alive = serverData.is_alive;
                // Sync Claude session ID once detected by backend
                if (serverData.claude_session_id && !info.claudeSessionId) {
                    info.claudeSessionId = serverData.claude_session_id;
                }
                if (serverData.terminal_type && !info.terminalType) {
                    info.terminalType = serverData.terminal_type;
                }
                // Authoritative hook state (claude only; null otherwise).
                info.instance.backendState = serverData.state || null;
                this._maybePlayStateSound(info.instance, serverData.state, serverData.state_ts);
            }
        }

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
    _maybePlayStateSound(instance, state, stateTs) {
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
                    if (tab.claude_session_id) {
                        await this.openTerminal(tab.group_name, tab.path, false, tab.claude_session_id, tType);
                    } else {
                        await this.openTerminal(tab.group_name, tab.path, true, null, tType);
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
                        await this.openTerminal(tab.group_name, tab.path, false, null, tab.terminal_type || null);
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

    renameTerminal(sessionId, newName) {
        this._isRenaming = false;
        const info = this.terminals[sessionId];
        if (!info) return;
        info.customName = newName && newName.trim() ? newName.trim() : null;
        this.tabBar.render();
        this.sidebar.render();
        this._saveSessions();
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
