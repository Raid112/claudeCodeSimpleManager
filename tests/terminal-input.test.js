const assert = require('node:assert/strict');

const TerminalInput = require('../web/js/terminal-input.js');

assert.equal(
    TerminalInput.prepareTerminalPaste('alpha\nbeta', { bracketedPasteMode: false }),
    'alpha\rbeta',
);

assert.equal(
    TerminalInput.prepareTerminalPaste('alpha\r\nbeta\r\ngamma', { bracketedPasteMode: false }),
    'alpha\rbeta\rgamma',
);

assert.equal(
    TerminalInput.prepareTerminalPaste('alpha\n\nbeta', { bracketedPasteMode: true }),
    '\x1b[200~alpha\r\rbeta\x1b[201~',
);

assert.equal(
    TerminalInput.prepareComposerMessage('single line', { bracketedPasteMode: true }),
    'single line\r',
);

assert.equal(
    TerminalInput.prepareComposerMessage('alpha\nbeta', { bracketedPasteMode: false }),
    'alpha\nbeta\r',
);

assert.equal(
    TerminalInput.prepareComposerMessage('alpha\n\nbeta', { bracketedPasteMode: true }),
    '\x1b[200~alpha\r\rbeta\x1b[201~\r',
);

console.log('terminal-input tests passed');
