(function (global) {
    const DEFAULT_DELAYS_MS = [500, 1000, 2000, 5000, 10000];

    class ReconnectController {
        constructor(onAttempt, options = {}) {
            if (typeof onAttempt !== 'function') {
                throw new TypeError('onAttempt must be a function');
            }

            this._onAttempt = onAttempt;
            this._delays = [...(options.delays || DEFAULT_DELAYS_MS)];
            this._setTimeout = options.setTimeoutFn || global.setTimeout;
            this._clearTimeout = options.clearTimeoutFn || global.clearTimeout;
            this._timer = null;
            this._attempt = 0;
            this._cancelled = false;
        }

        get pending() {
            return this._timer !== null;
        }

        get attempt() {
            return this._attempt;
        }

        schedule() {
            if (this._cancelled || this.pending) return false;

            const attempt = this._attempt + 1;
            const delayIndex = Math.min(attempt - 1, this._delays.length - 1);
            const delay = this._delays[Math.max(0, delayIndex)] || 0;
            this._attempt = attempt;
            this._timer = this._setTimeout(async () => {
                this._timer = null;
                if (this._cancelled) return;

                let connected = false;
                try {
                    connected = await this._onAttempt(attempt);
                } catch (error) {
                    if (global.console && typeof global.console.warn === 'function') {
                        global.console.warn('Terminal reconnect attempt failed', error);
                    }
                }

                if (this._cancelled) return;
                if (connected) {
                    this.reset();
                } else {
                    this.schedule();
                }
            }, delay);
            return true;
        }

        reset() {
            if (this._timer !== null) {
                this._clearTimeout(this._timer);
            }
            this._timer = null;
            this._attempt = 0;
        }

        cancel() {
            this._cancelled = true;
            this.reset();
        }
    }

    global.ReconnectController = ReconnectController;

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { ReconnectController };
    }
})(typeof window !== 'undefined' ? window : globalThis);
