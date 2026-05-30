/**
 * Terminal wrapper — manages xterm.js instance + WebSocket connection to PTY.
 */

const TERMINAL_THEME = {
    background: '#0a0a0a',
    foreground: '#e2e8f0',
    cursor: '#a855f7',
    cursorAccent: '#0a0a0a',
    selectionBackground: 'rgba(168, 85, 247, 0.3)',
    selectionForeground: '#e2e8f0',
    black: '#1e1e2e',
    red: '#f38ba8',
    green: '#a6e3a1',
    yellow: '#f9e2af',
    blue: '#89b4fa',
    magenta: '#cba6f7',
    cyan: '#74c7ec',
    white: '#cdd6f4',
    brightBlack: '#585b70',
    brightRed: '#f38ba8',
    brightGreen: '#a6e3a1',
    brightYellow: '#f9e2af',
    brightBlue: '#89b4fa',
    brightMagenta: '#cba6f7',
    brightCyan: '#74c7ec',
    brightWhite: '#ffffff',
};

class TerminalInstance {
    /** Audio context for notification sounds (shared across instances) */
    static _audioCtx = null;

    static getAudioContext() {
        if (!TerminalInstance._audioCtx) {
            TerminalInstance._audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        }
        return TerminalInstance._audioCtx;
    }

    static playReadySound() {
        try {
            const ctx = TerminalInstance.getAudioContext();
            const now = ctx.currentTime;

            // Two-tone ascending chime
            [440, 660].forEach((freq, i) => {
                const osc = ctx.createOscillator();
                const gain = ctx.createGain();
                osc.type = 'sine';
                osc.frequency.value = freq;
                gain.gain.setValueAtTime(0.18, now + i * 0.12);
                gain.gain.exponentialRampToValueAtTime(0.001, now + i * 0.12 + 0.3);
                osc.connect(gain).connect(ctx.destination);
                osc.start(now + i * 0.12);
                osc.stop(now + i * 0.12 + 0.3);
            });
        } catch (e) { /* ignore audio errors */ }
    }

    static playToolUseSound() {
        try {
            const ctx = TerminalInstance.getAudioContext();
            const now = ctx.currentTime;

            // Single short blip
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            osc.type = 'triangle';
            osc.frequency.value = 520;
            gain.gain.setValueAtTime(0.12, now);
            gain.gain.exponentialRampToValueAtTime(0.001, now + 0.15);
            osc.connect(gain).connect(ctx.destination);
            osc.start(now);
            osc.stop(now + 0.15);
        } catch (e) { /* ignore audio errors */ }
    }

    constructor(sessionId, wsPort, container) {
        this.sessionId = sessionId;
        this.container = container;
        this.ws = null;
        this.alive = true;
        this.lastOutputTime = Date.now();

        // Ring buffer of recent output chunks (cheap append, O(1) eviction).
        // Concatenated only when classification needs it.
        this._recentChunks = [];
        this._recentChunksSize = 0;
        this._RECENT_LIMIT = 5000;

        // Cached classification (recomputed on new output, not on every status read).
        this._cachedIdleStatus = 'ready';
        this._cachedIdleStale = true;

        // Authoritative state pushed from backend hooks (claude only). null until
        // the first hook event fires; while null we fall back to output heuristics.
        // Set by App.refreshStatus from get_terminals().state.
        this.backendState = null;

        // Explicit follow-mode state. true = output rolls to bottom; false = user is reading scrollback.
        // Transitions: user scrolls up → false. User clicks ↓ btn or buffer reaches genuine bottom → true.
        this.autoFollow = true;
        this._suppressScrollEvent = false; // ignore programmatic scrollToBottom in onScroll handler
        this._lastBaseY = 0;

        // Time of last user-originated message — drives the 1h cache countdown shown on the tab.
        // Set by Composer._send and by direct \r input here (filtered by status).
        // null = no message sent yet; timer hidden.
        this.lastUserMessageTime = null;

        // Create xterm.js terminal
        this.term = new Terminal({
            theme: TERMINAL_THEME,
            fontFamily: "'Cascadia Code', 'Consolas', 'Courier New', monospace",
            fontSize: 14,
            cursorBlink: true,
            cursorStyle: 'bar',
            allowProposedApi: true,
            scrollback: 5000,
        });

        // Load addons
        this.fitAddon = new FitAddon.FitAddon();
        this.term.loadAddon(this.fitAddon);
        this.term.loadAddon(new WebLinksAddon.WebLinksAddon((event, uri) => {
            if (window.pywebview && window.pywebview.api) {
                window.pywebview.api.open_url(uri);
            } else {
                window.open(uri, '_blank');
            }
        }));

        // Open terminal in container
        this.term.open(container);
        // Don't fit here — container may be display:none (0 dimensions).
        // fit() will be called by switchToTerminal() or ResizeObserver.

        // Auto-fit when container resizes (ignore zero-dimension callbacks)
        this._resizeObserver = new ResizeObserver((entries) => {
            const entry = entries[0];
            if (entry && entry.contentRect.height > 0 && entry.contentRect.width > 0) {
                this.fit();
            }
        });
        this._resizeObserver.observe(container);

        // "Scroll to bottom" button — appears when user scrolls up
        this._scrollBtn = document.createElement('button');
        this._scrollBtn.className = 'scroll-to-bottom-btn';
        this._scrollBtn.textContent = '↓';
        this._scrollBtn.title = 'Ir para o final';
        this._scrollBtn.style.display = 'none';
        this._scrollBtn.addEventListener('click', () => {
            this.autoFollow = true;
            this._scrollToBottomProgrammatic();
            this.term.focus();
            this._updateScrollBtn();
        });
        container.appendChild(this._scrollBtn);

        // Detect manual scroll to toggle autoFollow off; re-enable only when user reaches genuine bottom.
        this.term.onScroll(() => {
            if (this._suppressScrollEvent) return;
            const buf = this.term.buffer.active;
            if (buf.viewportY < buf.baseY) {
                this.autoFollow = false;
            } else if (buf.viewportY === buf.baseY) {
                this.autoFollow = true;
            }
            this._updateScrollBtn();
        });
        this.term.onLineFeed(() => this._updateScrollBtn());

        // Connect WebSocket
        this._connectWs(wsPort);

        // Intercept Ctrl+V (paste) and Ctrl+C (copy when selection exists)
        this.term.attachCustomKeyEventHandler((ev) => {
            if (ev.type !== 'keydown') return true;
            if (ev.ctrlKey && ev.key === 'v') {
                ev.preventDefault();
                navigator.clipboard.readText().then((text) => {
                    if (text) this.sendPaste(text);
                });
                return false; // prevent xterm from sending \x16
            }
            if (ev.ctrlKey && ev.key === 'c' && this.term.hasSelection()) {
                navigator.clipboard.writeText(this.term.getSelection());
                return false; // prevent sending SIGINT when copying
            }
            return true;
        });

        // Handle input -> send to PTY. Do NOT force scroll here; autoFollow controls following.
        this.term.onData((data) => {
            if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                this.ws.send(data);
            }
            // Reset cache timer on Enter, but only if NOT confirming a tool-use prompt.
            // Tool-use 'y'/'n' + Enter shouldn't be treated as a chat turn.
            if (data.includes('\r') && this.status !== 'tooluse') {
                this.lastUserMessageTime = Date.now();
            }
        });

        // Handle resize
        this.term.onResize(({ cols, rows }) => {
            if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                this.ws.send(JSON.stringify({ type: 'resize', cols, rows }));
            }
        });
    }

    get bracketedPasteModeEnabled() {
        return !!this.term?.modes?.bracketedPasteMode;
    }

    sendPaste(text) {
        if (!text || !this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
        const payload = TerminalInput.prepareTerminalPaste(text, {
            bracketedPasteMode: this.bracketedPasteModeEnabled,
        });
        this.ws.send(payload);
        return true;
    }

    sendComposerText(text) {
        if (!text || !this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
        const payload = TerminalInput.prepareComposerMessage(text, {
            bracketedPasteMode: this.bracketedPasteModeEnabled,
        });
        this.ws.send(payload);
        this.lastUserMessageTime = Date.now();
        return true;
    }

    _connectWs(wsPort) {
        this.ws = new WebSocket(`ws://127.0.0.1:${wsPort}/${this.sessionId}`);

        this.ws.onmessage = (event) => {
            this.lastOutputTime = Date.now();
            this.term.write(event.data);
            this._appendRecent(event.data);
            this._cachedIdleStale = true;

            // Follow only when explicitly in follow mode. No threshold heuristics.
            if (this.autoFollow) {
                this._scrollToBottomProgrammatic();
            } else {
                this._updateScrollBtn();
            }
        };

        this.ws.onclose = () => {
            this.alive = false;
            this.term.write('\r\n\x1b[90m--- Terminal desconectado ---\x1b[0m\r\n');
            if (window.app) window.app.refreshStatus();
        };

        this.ws.onerror = () => {
            this.alive = false;
        };
    }

    /**
     * Returns status: 'running' | 'ready' | 'tooluse' | 'waiting' | 'stopped'.
     * 'tooluse'  = waiting for tool-use permission (composer blocked; answer in terminal).
     * 'waiting'  = idle, waiting for your input (composer free).
     * 'ready'    = finished responding (idle at prompt).
     *
     * For claude with hooks, backendState is authoritative the moment it exists.
     * Otherwise (opencode/codex, or claude before the first hook event) we fall
     * back to the output-text heuristic with a 3s debounce.
     * Sound on transitions is handled in App.refreshStatus, not here — this getter
     * is read multiple times per poll and must stay side-effect free.
     */
    get status() {
        if (!this.alive) return 'stopped';
        if (this.backendState) return this.backendState;

        const idleMs = Date.now() - this.lastOutputTime;
        if (idleMs < 3000) return 'running';
        return this._classifyIdleStatus();
    }

    /** Append output chunk to ring buffer; O(1) eviction by chunk. */
    _appendRecent(data) {
        this._recentChunks.push(data);
        this._recentChunksSize += data.length;
        while (this._recentChunksSize > this._RECENT_LIMIT && this._recentChunks.length > 1) {
            this._recentChunksSize -= this._recentChunks.shift().length;
        }
    }

    /** Programmatic scroll to bottom that does not flip autoFollow back on via onScroll. */
    _scrollToBottomProgrammatic() {
        this._suppressScrollEvent = true;
        try {
            this.term.scrollToBottom();
        } finally {
            // Restore on next tick — onScroll fires sync inside scrollToBottom in current xterm versions
            setTimeout(() => { this._suppressScrollEvent = false; }, 0);
        }
    }

    /** Analyze recent terminal output to distinguish tool-use request from chat completion. */
    _classifyIdleStatus() {
        if (!this._cachedIdleStale) return this._cachedIdleStatus;

        const recent = this._recentChunks.join('');
        // Strip ANSI escape sequences for analysis
        const clean = recent.replace(/\x1b\[[0-9;]*[a-zA-Z]/g, '').replace(/\x1b\][^\x07]*\x07/g, '');
        const lastChunk = clean.slice(-2000);

        // Tool-use patterns: Claude Code asks for permission before executing tools
        const toolUsePatterns = [
            /Allow\s+(once|always)/i,
            /Do you want to proceed/i,
            /Esc to cancel/i,
            /Tab to amend/i,
            /\(Y\)es\b/i,
            /Yes\s*\/\s*No/i,
            /Allow\s+this/i,
            /Press Enter to allow/i,
            /\? Yes\s/i,
            />\s*1\.\s*Yes/,
        ];

        let result = 'ready';
        for (const pattern of toolUsePatterns) {
            if (pattern.test(lastChunk)) {
                result = 'tooluse';
                break;
            }
        }
        this._cachedIdleStatus = result;
        this._cachedIdleStale = false;
        return result;
    }

    _updateScrollBtn() {
        this._scrollBtn.style.display = this.autoFollow ? 'none' : 'block';
    }

    /**
     * Cache remaining time, in ms. Anthropic prompt-cache extended TTL is 1h.
     * Returns null if no user message has been sent yet (timer hidden).
     */
    get cacheRemainingMs() {
        if (!this.lastUserMessageTime) return null;
        const TTL_MS = 60 * 60 * 1000;
        return TTL_MS - (Date.now() - this.lastUserMessageTime);
    }

    /** 'fresh' (>30m) | 'warn' (10-30m) | 'critical' (0-10m) | 'expired' (<=0) | null */
    get cacheUrgency() {
        const ms = this.cacheRemainingMs;
        if (ms === null) return null;
        if (ms <= 0) return 'expired';
        if (ms <= 10 * 60 * 1000) return 'critical';
        if (ms <= 30 * 60 * 1000) return 'warn';
        return 'fresh';
    }

    fit() {
        try {
            this.fitAddon.fit();
        } catch (e) {
            // ignore fit errors during init
        }
    }

    focus() {
        this.term.focus();
    }

    dispose() {
        if (this._resizeObserver) {
            this._resizeObserver.disconnect();
        }
        if (this.ws) {
            this.ws.close();
        }
        this.term.dispose();
    }
}
