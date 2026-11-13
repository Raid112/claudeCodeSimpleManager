/**
 * Tests for the poll-driven render of web/js/tabs.js — plain node asserts, no framework.
 * Run: node tests/render-skip.test.js
 *
 * The 2 s status poll re-renders the tab bar whether or not anything changed. Rebuilding an
 * identical DOM costs layout and paint and restarts every CSS animation, so the poll passes
 * `onlyIfChanged`. An explicit render() must still rebuild: callers use it to throw away
 * DOM they edited in place (the rename input).
 */

const assert = require('assert');
const path = require('path');

const { TabBar } = require(path.join(__dirname, '..', 'web', 'js', 'tabs.js'));

let writes = 0;
const container = {
    set innerHTML(v) { writes++; this._html = v; },
    get innerHTML() { return this._html; },
    querySelectorAll: () => [],
};
const instance = { status: 'ready', cacheRemainingMs: null, cacheUrgency: null, modState: 'alive' };
const app = {
    terminals: { t1: { instance, customName: null, claudeSessionId: null } },
    activeTerminalId: 't1',
    workItems: { items: [], session_links: {} },
    getDisplayName: () => 'billing',
    claudeSessionIdOf: () => null,
};
const bar = new TabBar(container, app);

bar.render({ onlyIfChanged: true });
assert.strictEqual(writes, 1, 'first render writes');

bar.render({ onlyIfChanged: true });
assert.strictEqual(writes, 1, 'identical poll render is skipped');

instance.status = 'running';
bar.render({ onlyIfChanged: true });
assert.strictEqual(writes, 2, 'a status change is rendered');
assert.ok(container.innerHTML.includes('status-running'));

bar.render();
assert.strictEqual(writes, 3, 'an explicit render always rebuilds');

console.log('render-skip: ok');
