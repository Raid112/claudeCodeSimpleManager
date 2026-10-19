/**
 * Task 6: pendências de aprovação Hermes + eventos recentes no painel de work
 * items (openDaily, workitems.js). Cobre os helpers puros que combinam o
 * retorno de `work_item_agent_state` (bridge) numa linha compacta por item —
 * sem duplicar o card de aprovação (isso é papel de agent-approvals.js).
 */

const assert = require('node:assert/strict');
const {
    wiAgentEventsLine, wiPendingLabel, wiAgentLine, wiExtraPendingItems,
} = require('../web/js/workitems.js');

const NOW = new Date(2026, 6, 29, 15, 0, 0).getTime();
const secAgo = (mins) => (NOW - mins * 60e3) / 1000;

// ---------- wiAgentEventsLine ----------
assert.equal(wiAgentEventsLine(null, NOW), '');
assert.equal(wiAgentEventsLine(undefined, NOW), '');
assert.equal(wiAgentEventsLine({}, NOW), '');
assert.equal(
    wiAgentEventsLine({ task_completed: { ts: secAgo(3), status: 'ready' } }, NOW),
    'concluída 3 min',
);
// mais recente primeiro, independente da ordem das chaves no objeto
assert.equal(
    wiAgentEventsLine({
        task_completed: { ts: secAgo(30) },
        permission_requested: { ts: secAgo(2) },
        process_exited: { ts: secAgo(120) },
    }, NOW),
    'permissão 2 min · concluída 30 min · encerrado 2 h',
);

// ---------- wiPendingLabel ----------
assert.equal(wiPendingLabel(null), '');
assert.equal(wiPendingLabel([]), '');
assert.equal(wiPendingLabel(['awaiting_approval']), 'aprovação pendente');
assert.equal(wiPendingLabel(['awaiting_dispatch']), 'aguardando envio');
assert.equal(wiPendingLabel(['awaiting_approval', 'awaiting_approval']), 'aprovação pendente'); // dedupe

// ---------- wiAgentLine (o que o painel efetivamente imprime por item) ----------
assert.equal(wiAgentLine(null, NOW), '');
assert.equal(wiAgentLine({ pending_reasons: [], events: {} }, NOW), '');
assert.equal(
    wiAgentLine({ pending_reasons: ['awaiting_approval'], events: {} }, NOW),
    '⏳ aprovação pendente',
);
assert.equal(
    wiAgentLine({ pending_reasons: [], events: { task_completed: { ts: secAgo(1) } } }, NOW),
    'concluída 1 min',
);
assert.equal(
    wiAgentLine({
        pending_reasons: ['awaiting_dispatch'],
        events: { process_exited: { ts: secAgo(5) } },
    }, NOW),
    '⏳ aguardando envio · encerrado 5 min',
);

// ---------- wiExtraPendingItems ----------
// O caso que motivou isto: um item com aprovação pendente que NÃO está na
// lista "aguardando" nem foi tocado nos últimos dias do resumo — sem esta
// checagem, a pendência simplesmente não aparece em lugar nenhum do painel.
const items = [
    { id: 'wi-shown', title: 'Já aparece no resumo', source: 'manual' },
    { id: 'wi-hidden', title: 'Ativo há dias, aprovação pendente agora', source: 'jira', external_key: 'DS-9' },
    { id: 'wi-no-pending', title: 'Tem evento mas nada pendente', source: 'manual' },
    { id: 'wi-removed', title: 'estado tem dados mas o item sumiu do store' },
];
const agentState = {
    'wi-shown': { pending_reasons: ['awaiting_approval'], events: {} },
    'wi-hidden': { pending_reasons: ['awaiting_dispatch'], events: {} },
    'wi-no-pending': { pending_reasons: [], events: { task_completed: { ts: secAgo(1) } } },
    'wi-does-not-exist': { pending_reasons: ['awaiting_approval'], events: {} },
};
const shown = new Set(['wi-shown']);
const extra = wiExtraPendingItems(agentState, shown, items);
assert.deepEqual(extra.map((x) => x.wiId), ['wi-hidden']);
assert.equal(extra[0].item.title, 'Ativo há dias, aprovação pendente agora');
// já mostrado no resumo -> não duplica aqui
assert.equal(wiExtraPendingItems(agentState, new Set(['wi-shown', 'wi-hidden']), items).length, 0);
// sem pendência -> nunca entra, mesmo com evento recente
assert.equal(wiExtraPendingItems(agentState, new Set(), []).length, 0);
// aceita array além de Set em shownWiIds
assert.deepEqual(
    wiExtraPendingItems(agentState, ['wi-shown'], items).map((x) => x.wiId),
    ['wi-hidden'],
);
// entrada sem item correspondente no store (wi-does-not-exist) nunca aparece
assert.ok(!extra.some((x) => x.wiId === 'wi-does-not-exist'));

console.log('workitem-agent-state tests passed');
