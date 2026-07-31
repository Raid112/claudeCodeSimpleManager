/**
 * Terminal wrapper — manages xterm.js instance + WebSocket connection to PTY.
 */

const TERMINAL_THEME = {
    background: '#000000',
    foreground: '#e8e8ef',
    cursor: '#ffffff',
    cursorAccent: '#000000',
    selectionBackground: 'rgba(255, 255, 255, 0.22)',
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

    constructor(sessionId, wsPort, container, wsCapability) {
        this.sessionId = sessionId;
        this.wsPort = wsPort;
        this.wsCapability = wsCapability;
        this.container = container;
        this.ws = null;
        this.alive = true;
        this._disposed = false;
        this._wsGeneration = 0;
        this._wsConnected = false;
        this._sawDisconnect = false;
        this.lastOutputTime = Date.now();

        this._reconnectController = new ReconnectController(
            () => this._requestReconnect(),
            { delays: [500, 1000, 2000, 5000, 10000] },
        );

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
        // Wall-clock ms of the hook event that set backendState (state_ts*1000).
        // Used to detect that claude resumed streaming after a 'waiting' state
        // without a new hook firing. null until the first hook event.
        this.backendStateTs = null;

        // Explicit follow-mode state. true = output rolls to bottom; false = user is reading scrollback.
        // autoFollow ONLY changes on a real user scroll (wheel). write/resize/scrollback-trim all
        // emit onScroll too, but must NOT flip follow — that was the old "jumps while reading" bug.
        this.autoFollow = true;
        this._userScrolling = false;   // true briefly after a real wheel event; gates follow changes
        this._userScrollTimer = null;

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

        // Mark a short window after a real wheel gesture. Only during this window may
        // onScroll change autoFollow — so output/resize/trim-driven onScroll events
        // (which also fire) can never flip follow on their own.
        container.addEventListener('wheel', () => {
            this._userScrolling = true;
            clearTimeout(this._userScrollTimer);
            this._userScrollTimer = setTimeout(() => { this._userScrolling = false; }, 200);
        }, { passive: true });

        // Follow toggles only on user-driven scroll: up → stop following, back to bottom → resume.
        this.term.onScroll(() => {
            if (this._userScrolling) {
                const buf = this.term.buffer.active;
                this.autoFollow = buf.viewportY >= buf.baseY;
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
            if (ev.ctrlKey && ev.key.toLowerCase() === 'c' && this.term.hasSelection()) {
                this._copySelection();
                return false; // prevent sending SIGINT when copying
            }
            return true;
        });

        // copyOnSelect: copy as soon as the mouse selection is released. The new
        // Claude CLI re-renders constantly (spinner/status line), and each incoming
        // write can clear the selection before Ctrl+C fires — so Ctrl+C would miss
        // the selection and send SIGINT instead. Copying on mouseup sidesteps that.
        container.addEventListener('mouseup', () => {
            if (this.term.hasSelection()) this._copySelection();
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

    async _requestReconnect() {
        if (this._disposed) return true;

        try {
            const api = window.pywebview && window.pywebview.api;
            if (!api || typeof api.issue_ws_capability !== 'function') return false;

            const capability = await api.issue_ws_capability(this.sessionId);
            if (!capability || this._disposed) return false;

            this.wsCapability = capability;
            this._connectWs(this.wsPort);
            return true;
        } catch (error) {
            if (window.console && typeof window.console.warn === 'function') {
                window.console.warn(`Failed to reconnect terminal ${this.sessionId}`, error);
            }
            return false;
        }
    }

    _scheduleReconnect() {
        if (!this._disposed) this._reconnectController.schedule();
    }

    _connectWs(wsPort) {
        const generation = ++this._wsGeneration;
        const socket = new WebSocket(`ws://127.0.0.1:${wsPort}/${this.sessionId}`);
        this.ws = socket;

        socket.onopen = () => {
            if (generation !== this._wsGeneration || this._disposed) {
                socket.close();
                return;
            }

            this._wsConnected = true;
            this.alive = true;
            this._reconnectController.reset();
            if (this._sawDisconnect) {
                this.term.write('\r\n\x1b[92m--- Terminal reconectado ---\x1b[0m\r\n');
                this._sawDisconnect = false;
            }

            // The capability is sent only after the socket opens, never in the URL.
            // The server will not start PTY reader/writer tasks until this handshake passes.
            socket.send(JSON.stringify({
                type: 'handshake',
                session_id: this.sessionId,
                capability: this.wsCapability,
            }));
            if (window.app) window.app.refreshStatus();
        };

        socket.onmessage = (event) => {
            if (generation !== this._wsGeneration || this._disposed) return;
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

        socket.onclose = (event) => {
            if (generation !== this._wsGeneration) return;
            this._wsConnected = false;
            this.alive = false;
            if (window.console && typeof window.console.warn === 'function') {
                window.console.warn('Terminal WebSocket closed', {
                    sessionId: this.sessionId,
                    code: event.code,
                    reason: event.reason,
                    wasClean: event.wasClean,
                });
            }
            if (!this._disposed) {
                this._sawDisconnect = true;
                this.term.write('\r\n\x1b[90m--- Terminal desconectado; reconectando... ---\x1b[0m\r\n');
                this._scheduleReconnect();
            }
            if (window.app) window.app.refreshStatus();
        };

        socket.onerror = () => {
            if (generation !== this._wsGeneration || this._disposed) return;
            this._wsConnected = false;
            this.alive = false;
            this._sawDisconnect = true;
            if (window.console && typeof window.console.warn === 'function') {
                window.console.warn(`Terminal WebSocket error for ${this.sessionId}`);
            }
            this._scheduleReconnect();
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
        if (!this.alive || (this._sawDisconnect && !this._wsConnected)) return 'stopped';

        const idleMs = Date.now() - this.lastOutputTime;
        if (this.backendState) {
            // 'waiting' = claude idle waiting for input. If it resumed via an
            // in-terminal reply (no UserPromptSubmit hook), it streams output
            // before the next hook fires and would stay stuck purple. Fresh
            // output after the hook ts means it's actually working again.
            if (this.backendState === 'waiting' && this.backendStateTs &&
                this.lastOutputTime > this.backendStateTs + 500 && idleMs < 3000) {
                return 'running';
            }
            return this.backendState;
        }
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

    /** Programmatic scroll to bottom. Safe to call freely: onScroll only flips follow
     *  during a user wheel window, so this never accidentally re-enables autoFollow. */
    _scrollToBottomProgrammatic() {
        this.term.scrollToBottom();
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

    /** Copy current xterm selection to the OS clipboard, with a WebView2 fallback. */
    _copySelection() {
        const text = this.term.getSelection();
        if (!text) return;
        try {
            navigator.clipboard.writeText(text).catch(() => this._copyFallback(text));
        } catch (e) {
            this._copyFallback(text);
        }
    }

    /** Legacy clipboard path for contexts where navigator.clipboard is blocked. */
    _copyFallback(text) {
        try {
            const ta = document.createElement('textarea');
            ta.value = text;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            document.body.removeChild(ta);
        } catch (e) { /* clipboard unavailable */ }
    }

    dispose() {
        this._disposed = true;
        this._reconnectController.cancel();
        this._wsGeneration += 1;
        clearTimeout(this._userScrollTimer);
        if (this._resizeObserver) {
            this._resizeObserver.disconnect();
        }
        if (this.ws) {
            this.ws.close();
        }
        this.term.dispose();
    }
}
