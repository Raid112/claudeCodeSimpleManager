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

            html += `<div class="tab ${isActive ? 'active' : ''} status-${dotClass}" data-id="${id}">`;
            html += `<span class="tab-dot ${dotClass}"></span>`;
            html += `<span class="tab-label">${info.groupName} #${id.slice(0, 4)}</span>`;
            html += `<span class="tab-close" data-id="${id}" title="Fechar">×</span>`;
            html += `</div>`;
        }

        this.container.innerHTML = html;
        this._bindEvents();
    }

    _bindEvents() {
        // Tab click -> switch
        this.container.querySelectorAll('.tab').forEach(el => {
            el.addEventListener('click', (e) => {
                if (e.target.classList.contains('tab-close')) return;
                this.app.switchToTerminal(el.dataset.id);
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
