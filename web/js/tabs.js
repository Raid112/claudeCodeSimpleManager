/**
 * Tab bar — manages terminal tabs UI.
 */

class TabBar {
    constructor(container, app) {
        this.container = container;
        this.app = app;
    }

    render() {
        let html = '';

        for (const [id, info] of Object.entries(this.app.terminals)) {
            const isActive = this.app.activeTerminalId === id;
            const dotClass = info.instance.status;  // 'running', 'ready', or 'stopped'
            const displayName = this.app.getDisplayName(id);

            html += `<div class="tab ${isActive ? 'active' : ''} status-${dotClass}" data-id="${id}">`;
            html += `<span class="tab-dot ${dotClass}"></span>`;
            html += `<span class="tab-label" data-id="${id}">${displayName}</span>`;
            html += `<span class="tab-close" data-id="${id}" title="Fechar">×</span>`;
            html += `</div>`;
        }

        this.container.innerHTML = html;
        this._bindEvents();
    }

    _startRename(labelEl, sessionId) {
        const currentName = this.app.getDisplayName(sessionId);
        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'tab-rename-input';
        input.value = currentName;

        labelEl.textContent = '';
        labelEl.appendChild(input);
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
        // Prevent tab switch when clicking the input
        input.addEventListener('click', (e) => e.stopPropagation());
    }

    _bindEvents() {
        // Tab click -> switch
        this.container.querySelectorAll('.tab').forEach(el => {
            el.addEventListener('click', (e) => {
                if (e.target.classList.contains('tab-close')) return;
                this.app.switchToTerminal(el.dataset.id);
            });
        });

        // Right-click on label -> rename
        this.container.querySelectorAll('.tab-label').forEach(el => {
            el.addEventListener('contextmenu', (e) => {
                e.preventDefault();
                e.stopPropagation();
                this._startRename(el, el.dataset.id);
            });
        });

        // Close button
        this.container.querySelectorAll('.tab-close').forEach(el => {
            el.addEventListener('click', (e) => {
                e.stopPropagation();
                this.app.closeTerminal(el.dataset.id);
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
    }
}
