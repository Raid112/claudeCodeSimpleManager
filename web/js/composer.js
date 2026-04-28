/**
 * Composer — bottom-pane message editor.
 *
 * Behavior:
 *   - Enter (no shift)              -> send (text + \r) to active terminal, clear textarea, push to history
 *   - Shift+Enter                   -> insert literal newline
 *   - ArrowUp at start / ArrowDown  -> navigate sent-history
 *   - Disabled when status === 'tooluse' to avoid accidentally confirming a tool-use prompt
 *
 * History persists via bridge.set_composer_history (capped at 50 entries).
 */

class Composer {
    constructor(app) {
        this.app = app;
        this.textareaEl = document.getElementById('composer-textarea');
        this.sendBtnEl = document.getElementById('composer-send-btn');
        this.targetEl = document.getElementById('composer-target');
        this.statusEl = document.getElementById('composer-status');

        this.history = [];           // last 50 sent messages
        this.historyIdx = null;      // null = editing fresh; number = browsing history
        this._draft = '';            // saves the in-progress text when entering history mode
        this._saveTimer = null;

        this._bindEvents();
        this._loadHistory();
    }

    async _loadHistory() {
        try {
            const h = await window.pywebview.api.get_composer_history();
            if (Array.isArray(h)) this.history = h;
        } catch (e) { /* defaults */ }
    }

    _bindEvents() {
        this.textareaEl.addEventListener('keydown', (e) => this._onKeyDown(e));
        this.sendBtnEl.addEventListener('click', () => this._send());
    }

    _onKeyDown(e) {
        // Enter (no shift) → send
        if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.altKey) {
            e.preventDefault();
            this._send();
            return;
        }

        // History navigation: only when at boundaries to avoid hijacking normal cursor moves
        if (e.key === 'ArrowUp' && this._isCursorAtStart()) {
            if (this.history.length === 0) return;
            e.preventDefault();
            if (this.historyIdx === null) {
                this._draft = this.textareaEl.value;
                this.historyIdx = this.history.length - 1;
            } else if (this.historyIdx > 0) {
                this.historyIdx -= 1;
            } else {
                return;
            }
            this.textareaEl.value = this.history[this.historyIdx];
            this._cursorToEnd();
            return;
        }

        if (e.key === 'ArrowDown' && this._isCursorAtEnd()) {
            if (this.historyIdx === null) return;
            e.preventDefault();
            if (this.historyIdx < this.history.length - 1) {
                this.historyIdx += 1;
                this.textareaEl.value = this.history[this.historyIdx];
            } else {
                this.historyIdx = null;
                this.textareaEl.value = this._draft;
            }
            this._cursorToEnd();
            return;
        }

        // Any other key cancels history-browsing mode (treats current text as fresh draft)
        if (this.historyIdx !== null && e.key.length === 1) {
            this.historyIdx = null;
            this._draft = '';
        }
    }

    _isCursorAtStart() {
        return this.textareaEl.selectionStart === 0 && this.textareaEl.selectionEnd === 0;
    }
    _isCursorAtEnd() {
        const len = this.textareaEl.value.length;
        return this.textareaEl.selectionStart === len && this.textareaEl.selectionEnd === len;
    }
    _cursorToEnd() {
        const len = this.textareaEl.value.length;
        this.textareaEl.setSelectionRange(len, len);
    }

    _send() {
        const text = this.textareaEl.value;
        if (!text || !text.trim()) return;

        const sessionId = this.app.activeTerminalId;
        const info = sessionId ? this.app.terminals[sessionId] : null;
        if (!info || !info.instance.alive) return;

        const status = info.instance.status;
        if (status === 'tooluse') {
            this._flashStatus('Bloqueado: terminal aguardando confirmação de tool-use', true);
            return;
        }

        const ws = info.instance.ws;
        if (!ws || ws.readyState !== WebSocket.OPEN) {
            this._flashStatus('WebSocket desconectado', true);
            return;
        }

        // Convert any local CRLF/CR to LF first, then convert each LF to \r (PTYs use CR for "Enter").
        // For multi-line: send all lines joined by \r so the agent receives one message with literal newlines
        // — Claude Code uses \r as message-end; embedded \n keeps line breaks within the message.
        const normalized = text.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
        ws.send(normalized + '\r');

        // Push to history (no consecutive duplicates)
        if (this.history[this.history.length - 1] !== text) {
            this.history.push(text);
            if (this.history.length > 50) this.history = this.history.slice(-50);
            this._persistHistory();
        }

        this.textareaEl.value = '';
        this.historyIdx = null;
        this._draft = '';
        this._flashStatus(`Enviado para ${this.app.getDisplayName(sessionId)}`, false);

        // Re-focus the textarea (don't yank focus to terminal)
        this.textareaEl.focus();

        // Mark user-message time on the terminal — used by Phase 4 cache timer
        info.instance.lastUserMessageTime = Date.now();
    }

    _flashStatus(text, isWarn) {
        this.statusEl.textContent = text;
        this.statusEl.classList.toggle('warn', !!isWarn);
        if (this._statusTimer) clearTimeout(this._statusTimer);
        this._statusTimer = setTimeout(() => {
            this.statusEl.textContent = '';
            this.statusEl.classList.remove('warn');
        }, 2500);
    }

    _persistHistory() {
        if (this._saveTimer) clearTimeout(this._saveTimer);
        this._saveTimer = setTimeout(() => {
            try { window.pywebview.api.set_composer_history(this.history); } catch (e) {}
        }, 400);
    }

    /** Called by App when active terminal changes or status updates. */
    refreshTarget() {
        const sessionId = this.app.activeTerminalId;
        if (!sessionId || !this.app.terminals[sessionId]) {
            this.targetEl.textContent = 'Sem terminal ativo';
            this.targetEl.className = 'composer-target stopped';
            this.textareaEl.disabled = true;
            this.sendBtnEl.disabled = true;
            return;
        }
        const info = this.app.terminals[sessionId];
        const name = this.app.getDisplayName(sessionId);
        const status = info.instance.status;

        if (!info.instance.alive) {
            this.targetEl.textContent = `→ ${name} (parado)`;
            this.targetEl.className = 'composer-target stopped';
            this.textareaEl.disabled = true;
            this.sendBtnEl.disabled = true;
            return;
        }

        if (status === 'tooluse') {
            this.targetEl.textContent = `→ ${name} (aguardando tool-use)`;
            this.targetEl.className = 'composer-target tooluse';
            this.textareaEl.disabled = true;
            this.sendBtnEl.disabled = true;
            return;
        }

        this.targetEl.textContent = `→ ${name}`;
        this.targetEl.className = 'composer-target';
        this.textareaEl.disabled = false;
        this.sendBtnEl.disabled = false;
    }

    focus() {
        this.textareaEl.focus();
    }
}
