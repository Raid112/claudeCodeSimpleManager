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
        this._recentOutput = '';          // rolling buffer of recent output (last ~5000 chars)
        this._previousStatus = 'running'; // track transitions for sound

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
            this.term.scrollToBottom();
            this.term.focus();
        });
        container.appendChild(this._scrollBtn);

        // Show/hide scroll button based on viewport position
        this.term.onScroll(() => this._updateScrollBtn());
        this.term.onLineFeed(() => this._updateScrollBtn());

        // Connect WebSocket
        this._connectWs(wsPort);

        // Intercept Ctrl+V (paste) and Ctrl+C (copy when selection exists)
        this.term.attachCustomKeyEventHandler((ev) => {
            if (ev.type !== 'keydown') return true;
            if (ev.ctrlKey && ev.key === 'v') {
                ev.preventDefault();
                navigator.clipboard.readText().then((text) => {
                    if (text && this.ws && this.ws.readyState === WebSocket.OPEN) {
                        this.ws.send(text);
                    }
                });
                return false; // prevent xterm from sending \x16
            }
            if (ev.ctrlKey && ev.key === 'c' && this.term.hasSelection()) {
                navigator.clipboard.writeText(this.term.getSelection());
                return false; // prevent sending SIGINT when copying
            }
            return true;
        });

        // Handle input -> send to PTY (and snap viewport to bottom)
        this.term.onData((data) => {
            if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                this.ws.send(data);
            }
            this.term.scrollToBottom();
        });

        // Handle resize
        this.term.onResize(({ cols, rows }) => {
            if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                this.ws.send(JSON.stringify({ type: 'resize', cols, rows }));
            }
        });
    }

    _connectWs(wsPort) {
        this.ws = new WebSocket(`ws://127.0.0.1:${wsPort}/${this.sessionId}`);

        this.ws.onmessage = (event) => {
            this.lastOutputTime = Date.now();
            this.term.write(event.data);
            // Append to rolling buffer, keep last 5000 chars
            this._recentOutput += event.data;
            if (this._recentOutput.length > 5000) {
                this._recentOutput = this._recentOutput.slice(-5000);
            }
            // Auto-scroll to bottom if user is near the bottom (within 5 rows)
            const buf = this.term.buffer.active;
            const viewportAtBottom = buf.viewportY >= buf.baseY - 5;
            if (viewportAtBottom) {
                this.term.scrollToBottom();
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
     * Returns status: 'running' | 'ready' | 'tooluse' | 'stopped'.
     * 'tooluse' = Claude is waiting for tool-use approval.
     * 'ready' = Claude finished responding (idle at prompt).
     */
    get status() {
        if (!this.alive) return 'stopped';
        const idleMs = Date.now() - this.lastOutputTime;
        if (idleMs < 3000) return 'running';

        // Classify idle state by analyzing recent output
        const newStatus = this._classifyIdleStatus();

        // Play sound on transition from running -> ready/tooluse
        if (this._previousStatus === 'running' && newStatus !== 'running') {
            if (newStatus === 'ready') {
                TerminalInstance.playReadySound();
            } else if (newStatus === 'tooluse') {
                TerminalInstance.playToolUseSound();
            }
        }
        this._previousStatus = newStatus;
        return newStatus;
    }

    /** Analyze recent terminal output to distinguish tool-use request from chat completion. */
    _classifyIdleStatus() {
        // Strip ANSI escape sequences for analysis
        const clean = this._recentOutput.replace(/\x1b\[[0-9;]*[a-zA-Z]/g, '').replace(/\x1b\][^\x07]*\x07/g, '');
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

        for (const pattern of toolUsePatterns) {
            if (pattern.test(lastChunk)) {
                return 'tooluse';
            }
        }

        return 'ready';
    }

    _updateScrollBtn() {
        const buf = this.term.buffer.active;
        const isAtBottom = buf.viewportY >= buf.baseY - 2;
        this._scrollBtn.style.display = isAtBottom ? 'none' : 'block';
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
