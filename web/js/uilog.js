/**
 * UiLog — diagnostics that survive the session.
 *
 * Exists because of one concrete bug: a terminal now and then breaks its layout (wrong
 * columns, input that goes nowhere) and only recovers after switching tabs or minimizing
 * and restoring the window. Both of those are resize triggers, so the suspects are fit()
 * running against a hidden/mid-transition container, a resize that never reached the PTY,
 * and a throw in the render path leaving the DOM half-updated. Each of those is logged
 * here with the fields that tell them apart — a console.log would be gone by the time
 * anyone looked, and DevTools no longer opens on launch.
 *
 * Batched (2s or 50 events) through Bridge.ui_log into
 * %LOCALAPPDATA%\ClaudeManager\logs\ui-YYYY-MM-DD.jsonl. Batching is not cosmetic: one IPC
 * per fit() would itself cause the jank being measured. Every call is fire-and-forget;
 * logging must never break the app it is observing.
 */
const UiLog = (function () {
    const FLUSH_MS = 2000;
    const FLUSH_AT = 50;      // events buffered before an early flush
    const MAX_QUEUE = 500;    // hard cap; oldest go first (a loop can't eat memory)

    let buffer = [];
    let timer = null;
    let flushing = false;

    function push(event, fields) {
        const rec = { event, src: 'ui', ts: Date.now() / 1000 };
        if (fields) for (const k of Object.keys(fields)) rec[k] = fields[k];
        buffer.push(rec);
        if (buffer.length > MAX_QUEUE) buffer = buffer.slice(-MAX_QUEUE);
        if (buffer.length >= FLUSH_AT) flush();
        else if (!timer) timer = setTimeout(flush, FLUSH_MS);
    }

    async function flush() {
        if (timer) { clearTimeout(timer); timer = null; }
        if (flushing || !buffer.length) return;
        const api = window.pywebview && window.pywebview.api;
        // Before the bridge is ready, keep buffering — boot is exactly when the first
        // fit()s happen, and those are the ones worth having.
        if (!api || typeof api.ui_log !== 'function') {
            timer = setTimeout(flush, FLUSH_MS);
            return;
        }
        const batch = buffer;
        buffer = [];
        flushing = true;
        try {
            await api.ui_log(batch);
        } catch (e) {
            buffer = batch.concat(buffer).slice(-MAX_QUEUE);   // retry on the next tick
        } finally {
            flushing = false;
        }
    }

    // Guard: the module is also loadable outside a browser (tests); without a window
    // there is nothing to listen to and push() alone still works.
    if (typeof window === 'undefined') return { push, flush };

    window.addEventListener('error', (e) => {
        push('js.error', {
            message: String(e.message || ''),
            source: String(e.filename || ''),
            line: e.lineno || 0,
            col: e.colno || 0,
            stack: e.error && e.error.stack ? String(e.error.stack) : '',
        });
        flush();   // a throw may be the last thing this page does; don't sit on it
    });

    window.addEventListener('unhandledrejection', (e) => {
        const reason = e.reason;
        push('js.rejection', {
            message: reason && reason.message ? String(reason.message) : String(reason || ''),
            stack: reason && reason.stack ? String(reason.stack) : '',
        });
        flush();
    });

    // Minimizing/restoring and tab switches are the gestures that "fix" the broken
    // terminal, so they are the timeline anchors every other record is read against.
    document.addEventListener('visibilitychange', () => {
        push('window.visibility', { hidden: !!document.hidden });
        flush();
    });
    window.addEventListener('focus', () => push('window.focus', {}));
    window.addEventListener('blur', () => push('window.blur', {}));

    return { push, flush };
})();

if (typeof module !== 'undefined' && module.exports) module.exports = { UiLog };
