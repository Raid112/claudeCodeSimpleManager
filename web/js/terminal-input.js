(function (global) {
    const BRACKETED_PASTE_START = '\x1b[200~';
    const BRACKETED_PASTE_END = '\x1b[201~';

    function normalizeLineEndings(text) {
        return String(text).replace(/\r\n/g, '\n').replace(/\r/g, '\n');
    }

    function prepareTextForTerminal(text) {
        return normalizeLineEndings(text).replace(/\n/g, '\r');
    }

    function bracketTextForPaste(text) {
        return `${BRACKETED_PASTE_START}${text}${BRACKETED_PASTE_END}`;
    }

    function isDebugEnabled() {
        return !!global.CLAUDE_MANAGER_DEBUG_INPUT;
    }

    function summarizeText(text) {
        return text
            .replace(/\x1b/g, '\\x1b')
            .replace(/\r/g, '\\r')
            .replace(/\n/g, '\\n')
            .slice(0, 160);
    }

    function debugBoundary(boundary, originalText, payload, extra) {
        if (!isDebugEnabled() || !global.console || typeof global.console.debug !== 'function') return;
        const normalized = normalizeLineEndings(originalText);
        global.console.debug(`[input-debug] ${boundary}`, {
            sourceChars: String(originalText).length,
            normalizedChars: normalized.length,
            payloadChars: payload.length,
            lineCount: normalized.length === 0 ? 0 : normalized.split('\n').length,
            preview: summarizeText(payload),
            ...extra,
        });
    }

    function prepareTerminalPaste(text, options = {}) {
        const prepared = prepareTextForTerminal(text);
        const payload = options.bracketedPasteMode ? bracketTextForPaste(prepared) : prepared;
        debugBoundary('terminal-paste', text, payload, {
            bracketedPasteMode: !!options.bracketedPasteMode,
        });
        return payload;
    }

    function prepareComposerMessage(text, options = {}) {
        const normalized = normalizeLineEndings(text);
        const isMultiLine = normalized.includes('\n');
        let payload;

        if (isMultiLine && options.bracketedPasteMode) {
            payload = `${bracketTextForPaste(prepareTextForTerminal(normalized))}\r`;
        } else {
            payload = `${normalized}\r`;
        }

        debugBoundary('composer-submit', text, payload, {
            bracketedPasteMode: !!options.bracketedPasteMode,
            isMultiLine,
        });
        return payload;
    }

    const api = {
        normalizeLineEndings,
        prepareTextForTerminal,
        bracketTextForPaste,
        prepareTerminalPaste,
        prepareComposerMessage,
    };

    global.TerminalInput = api;

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = api;
    }
})(typeof window !== 'undefined' ? window : globalThis);
