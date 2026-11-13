/**
 * Guard for web/css/theme.css — plain node asserts, no framework.
 * Run: node tests/theme-motion.test.js
 *
 * An `infinite` animation on a state that lasts (an idle tab, a running session, a parked item, the
 * last ten minutes of a cache timer) keeps the WebView producing frames for as long as the window is
 * on screen. A finite one does not help either: the tab bar and the sidebar are rebuilt with innerHTML
 * every 2 s, so it would restart forever. Endless motion is therefore reserved for what ends on its
 * own or needs the user right now: a permission ask (`tooluse`) and the refresh spinner.
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const css = fs.readFileSync(path.join(__dirname, '..', 'web', 'css', 'theme.css'), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '');

const ALLOWED = [/tooluse/, /^\.wi-refresh\.spinning$/];

const endless = [];
for (const [, selectors, body] of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (!/animation\s*:[^;]*\binfinite\b/.test(body)) continue;
    for (const selector of selectors.split(',')) endless.push(selector.trim().replace(/\s+/g, ' '));
}

assert.ok(endless.length > 0, 'the parser found no endless animation at all: it is probably broken');

const offenders = endless.filter((s) => !ALLOWED.some((rx) => rx.test(s)));
assert.deepStrictEqual(offenders, [], `endless animation on a lasting state: ${offenders.join(' | ')}`);

const declared = [...css.matchAll(/@keyframes\s+([\w-]+)/g)].map((m) => m[1]);
const unused = declared.filter((name) => !new RegExp(`animation\\s*:[^;{}]*\\b${name}\\b`).test(css));
assert.deepStrictEqual(unused, [], `keyframes nobody uses: ${unused.join(', ')}`);

console.log(`theme-motion: ok (${endless.length} endless animations, all allowed)`);
