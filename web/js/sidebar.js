/**
 * Sidebar — renders project groups and terminal list.
 */

class Sidebar {
    constructor(container, app) {
        this.container = container;
        this.app = app;
        this.expandedGroups = new Set();
        this._terminalType = 'claude';
    }

    async render() {
        const groups = await window.pywebview.api.get_groups();
        const terminals = await window.pywebview.api.get_terminals();

        try {
            this._terminalType = await window.pywebview.api.get_terminal_type();
        } catch (e) {}

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
                const hasWaiting = groupTerminals.some(t => this._getTerminalStatus(t.id) === 'waiting');
                const badgeClass = hasRunning ? 'active'
                    : (hasToolUse ? 'tooluse'
                    : (hasWaiting ? 'waiting'
                    : (activeCount > 0 ? 'ready' : 'stopped')));
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
                    html += `<span class="terminal-name" data-id="${t.id}">${this.app.getDisplayName(t.id)}</span>`;
                    html += `</div>`;
                }
            }
            html += `</div>`;
            html += `</div>`;
        }

        this.container.innerHTML = html;
        this._renderTypeSelector();
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

        // Right-click on terminal name -> rename
        this.container.querySelectorAll('.terminal-name').forEach(el => {
            el.addEventListener('contextmenu', (e) => {
                e.preventDefault();
                e.stopPropagation();
                this._startRename(el, el.dataset.id);
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

        const finish = () => {
            const newName = input.value.trim();
            this.app.renameTerminal(sessionId, newName || null);
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
        input.addEventListener('click', (e) => e.stopPropagation());
    }

    _renderTypeSelector() {
        const footer = this.container.parentElement.querySelector('.sidebar-footer');
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
                </select>
            `;
            footer.insertBefore(selector, footer.firstChild);
        }
        const select = selector.querySelector('select');
        select.value = this._terminalType;
        select.onchange = async () => {
            this._terminalType = select.value;
            await window.pywebview.api.set_terminal_type(select.value);
        };
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
