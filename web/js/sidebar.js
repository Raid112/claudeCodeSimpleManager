/**
 * Sidebar — renders project groups and terminal list.
 */

class Sidebar {
    constructor(container, app) {
        this.container = container;
        this.app = app;
        this.expandedGroups = new Set();
    }

    async render() {
        const groups = await window.pywebview.api.get_groups();
        const terminals = await window.pywebview.api.get_terminals();

        let html = '';

        for (const group of groups) {
            const isExpanded = this.expandedGroups.has(group.name);
            const groupTerminals = terminals.filter(t => t.group_name === group.name);
            const activeCount = groupTerminals.filter(t => t.is_alive || this._getTerminalStatus(t.id) === 'ready').length;
            const totalCount = groupTerminals.length;

            html += `<div class="group ${isExpanded ? 'expanded' : ''}" data-name="${group.name}" data-path="${group.path}">`;
            html += `<div class="group-header" data-name="${group.name}">`;
            html += `<span class="group-arrow">▶</span>`;
            html += `<span class="group-icon">📁</span>`;
            html += `<span class="group-name">${group.name}</span>`;

            if (totalCount > 0) {
                const hasRunning = groupTerminals.some(t => this._getTerminalStatus(t.id) === 'running');
                const hasToolUse = groupTerminals.some(t => this._getTerminalStatus(t.id) === 'tooluse');
                const badgeClass = hasRunning ? 'active' : (hasToolUse ? 'tooluse' : (activeCount > 0 ? 'ready' : 'stopped'));
                html += `<span class="group-badge ${badgeClass}">${totalCount}</span>`;
            }

            html += `<span class="group-add-btn" data-name="${group.name}" data-path="${group.path}" title="Novo terminal">+</span>`;
            html += `</div>`;

            html += `<div class="group-terminals">`;
            if (groupTerminals.length === 0) {
                html += `<div class="empty-hint">Nenhum terminal</div>`;
            } else {
                for (const t of groupTerminals) {
                    const dotClass = this._getTerminalStatus(t.id);
                    const isActive = this.app.activeTerminalId === t.id;
                    html += `<div class="terminal-item ${isActive ? 'active' : ''}" data-id="${t.id}">`;
                    html += `<span class="terminal-dot ${dotClass}"></span>`;
                    html += `<span class="terminal-name">${group.name} #${t.id.slice(0, 4)}</span>`;
                    html += `</div>`;
                }
            }
            html += `</div>`;
            html += `</div>`;
        }

        this.container.innerHTML = html;
        this._bindEvents();
    }

    _bindEvents() {
        // Group header click -> toggle expand
        this.container.querySelectorAll('.group-header').forEach(el => {
            el.addEventListener('click', (e) => {
                if (e.target.classList.contains('group-add-btn')) return;
                const name = el.dataset.name;
                if (this.expandedGroups.has(name)) {
                    this.expandedGroups.delete(name);
                } else {
                    this.expandedGroups.add(name);
                }
                this.render();
            });
        });

        // Add terminal button
        this.container.querySelectorAll('.group-add-btn').forEach(el => {
            el.addEventListener('click', (e) => {
                e.stopPropagation();
                const name = el.dataset.name;
                const path = el.dataset.path;
                this.expandedGroups.add(name);
                this.app.openTerminal(name, path);
            });
        });

        // Terminal item click -> switch to tab
        this.container.querySelectorAll('.terminal-item').forEach(el => {
            el.addEventListener('click', () => {
                this.app.switchToTerminal(el.dataset.id);
            });
        });
    }

    _getTerminalStatus(id) {
        const info = this.app.terminals[id];
        if (info) return info.instance.status;
        return 'stopped';
    }

    toggleGroup(name) {
        if (this.expandedGroups.has(name)) {
            this.expandedGroups.delete(name);
        } else {
            this.expandedGroups.add(name);
        }
        this.render();
    }
}
