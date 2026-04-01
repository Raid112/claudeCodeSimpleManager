/**
 * Main app — orchestrates sidebar, tabs, and terminal instances.
 */

class App {
    constructor() {
        this.terminals = {};  // { sessionId: { instance: TerminalInstance, groupName, container } }
        this.activeTerminalId = null;
        this.wsPort = null;

        this.sidebarEl = document.getElementById('sidebar-groups');
        this.tabBarEl = document.getElementById('tab-bar');
        this.terminalContainerEl = document.getElementById('terminal-container');
        this.welcomeEl = document.getElementById('welcome');

        this.sidebar = new Sidebar(this.sidebarEl, this);
        this.tabBar = new TabBar(this.tabBarEl, this);

        // Add group button
        document.getElementById('add-group-btn').addEventListener('click', () => this.addGroup());

        // Resize handler
        window.addEventListener('resize', () => this._fitActiveTerminal());

        // Status polling
        setInterval(() => this.refreshStatus(), 2000);
    }

    async init() {
        this.wsPort = await window.pywebview.api.get_ws_port();
        await this.sidebar.render();
        this._updateWelcome();

        // Restore previous sessions
        await this._restoreSessions();

        // Periodic session save (every 10s) to survive crashes
        setInterval(() => this._saveSessions(), 10000);
    }

    async openTerminal(groupName, path, continueSession = false, claudeSessionId = null) {
        const result = await window.pywebview.api.open_terminal(groupName, path, 120, 30, continueSession, claudeSessionId);
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
            customName: null,
        };

        // Switch to this terminal
        this.switchToTerminal(sessionId);
        await this.sidebar.render();
    }

    switchToTerminal(sessionId) {
        if (!this.terminals[sessionId]) return;

        this.activeTerminalId = sessionId;

        // Hide all, show active
        for (const [id, info] of Object.entries(this.terminals)) {
            info.container.classList.toggle('active', id === sessionId);
        }

        this._updateWelcome();
        this.tabBar.render();
        this.sidebar.render();

        // Focus and fit with retry — pywebview layout may not be settled immediately
        const active = this.terminals[sessionId];
        setTimeout(() => {
            active.instance.fit();
            active.instance.focus();
        }, 100);
        // Retry after layout is definitely settled
        setTimeout(() => active.instance.fit(), 500);
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
            }
        }

        // Always re-render to pick up ready/running transitions based on idle time
        this.tabBar.render();
        await this.sidebar.render();
    }

    async _restoreSessions() {
        try {
            const saved = await window.pywebview.api.load_sessions();
            if (!saved || !saved.tabs || saved.tabs.length === 0) return;

            this._restoring = true;
            for (const tab of saved.tabs) {
                try {
                    if (tab.claude_session_id) {
                        // Resume specific session by ID
                        await this.openTerminal(tab.group_name, tab.path, false, tab.claude_session_id);
                    } else {
                        // Fallback: continue most recent session
                        await this.openTerminal(tab.group_name, tab.path, true);
                    }
                    // Restore custom name if saved
                    if (tab.custom_name) {
                        const lastId = Object.keys(this.terminals).pop();
                        if (lastId) this.terminals[lastId].customName = tab.custom_name;
                    }
                } catch (e) {
                    console.warn(`Failed to restore tab ${tab.group_name}, trying fresh`, e);
                    try {
                        await this.openTerminal(tab.group_name, tab.path, false);
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
                custom_name: info.customName || null,
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
