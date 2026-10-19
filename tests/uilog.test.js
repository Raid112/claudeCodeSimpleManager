/**
 * Tests for web/js/uilog.js — plain node asserts, no framework.
 * Run: node tests/uilog.test.js
 *
 * The point of the log is that the evidence survives, so the two things worth pinning are:
 * a flush before the bridge exists keeps the records (boot is when the first fit()s happen),
 * and a failed flush puts them back instead of dropping them on the floor.
 */

const assert = require('assert');
const path = require('path');

const listeners = { window: [], document: [] };
global.window = { addEventListener: (ev) => listeners.window.push(ev) };
global.document = { hidden: false, addEventListener: (ev) => listeners.document.push(ev) };

const { UiLog } = require(path.join(__dirname, '..', 'web', 'js', 'uilog.js'));

const sent = [];
function bridge(impl) {
    global.window.pywebview = { api: { ui_log: impl } };
}

(async () => {
    // Sem bridge (boot): o flush não pode perder nada — os registros ficam na fila.
    delete global.window.pywebview;
    UiLog.push('fit.suspect', { session: 's1', cols: 8 });
    await UiLog.flush();
    bridge(async (batch) => { sent.push(batch); return batch.length; });
    await UiLog.flush();
    assert.equal(sent.length, 1, 'flush com bridge disponível envia o lote acumulado');
    assert.equal(sent[0].length, 1);
    assert.equal(sent[0][0].event, 'fit.suspect');
    assert.equal(sent[0][0].session, 's1');
    assert.equal(sent[0][0].src, 'ui');
    assert.ok(sent[0][0].ts > 1e9, 'carrega o ts do evento (epoch em segundos)');

    // Lote vazio não faz IPC.
    await UiLog.flush();
    assert.equal(sent.length, 1, 'flush sem nada na fila não chama a bridge');

    // Bridge falhando: os registros voltam pra fila e saem no flush seguinte, na ordem.
    bridge(async () => { throw new Error('bridge caiu'); });
    UiLog.push('ws.close', { session: 's1', code: 1006 });
    await UiLog.flush();
    const ok = [];
    bridge(async (batch) => { ok.push(batch); return batch.length; });
    UiLog.push('ws.open', { session: 's1' });
    await UiLog.flush();
    assert.equal(ok.length, 1);
    assert.deepEqual(ok[0].map(r => r.event), ['ws.close', 'ws.open'],
        'o registro que falhou volta antes do que chegou depois');

    // Os anzóis globais são registrados uma vez (erro/rejection/visibilidade/foco).
    assert.deepEqual(listeners.window, ['error', 'unhandledrejection', 'focus', 'blur']);
    assert.deepEqual(listeners.document, ['visibilitychange']);

    console.log('uilog tests passed');
})();
