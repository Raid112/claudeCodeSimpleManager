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
    }

    async openTerminal(groupName, path) {
        const result = await window.pywebview.api.open_terminal(groupName, path, 120, 30);
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
            container: wrapper,
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
            serverMap[t.id] = t.is_alive;
        }

        for (const [id, info] of Object.entries(this.terminals)) {
            const serverAlive = serverMap[id];
            if (serverAlive !== undefined) {
                info.instance.alive = serverAlive;
            }
        }

        // Always re-render to pick up ready/running transitions based on idle time
        this.tabBar.render();
        await this.sidebar.render();
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
