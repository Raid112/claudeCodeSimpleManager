const assert = require('node:assert/strict');

const { findExternalSessions } = require('../web/js/session-sync.js');

const sessions = [
    { id: 'known', is_alive: true, group_name: 'clones', path: 'C:/clones' },
    { id: 'external', is_alive: true, group_name: 'clones', path: 'C:/clones', session_key: 'k1' },
    { id: 'dead', is_alive: false, group_name: 'clones', path: 'C:/clones' },
    { id: 'incomplete', is_alive: true, group_name: 'clones' },
];

assert.deepEqual(
    findExternalSessions(sessions, new Set(['known'])).map((session) => session.id),
    ['external'],
);

assert.deepEqual(findExternalSessions(null, []), []);

console.log('session-sync tests passed');
