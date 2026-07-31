/**
 * Busca de mensagens do popover de work items (workitems.js).
 *
 * O caso real que motivou isto: o Graph devolve o texto com \n entre blocos e   (nbsp)
 * no meio; a caixa de busca é um <input> single-line, então colar a mensagem inteira faz o
 * navegador comer as quebras. Comparar cru nunca casa — daí o squash (sem acento, sem
 * espaço nenhum) e o fallback por tokens.
 */

const assert = require('node:assert/strict');

const {
    wiSquash, wiTokens, wiWhen, wiSince, wiDayKey, wiDayHeader, wiRecentPeople, wiShortNames,
    wiPersonRows, wiCapNameOnly, wiSnippet, wiTeamsExternalKey, wiFindExistingItem,
    wiExistingAction,
    WorkItemsUI,
} = require('../web/js/workitems.js');
const { wiWaitingElapsed, wiPartitionWorkItems } = require('../web/js/sidebar.js');

const prep = WorkItemsUI.prototype._prepTeams;
const match = WorkItemsUI.prototype._teamsMatch;

// Como o backend guarda (pós _html_to_text) vs como o texto chega ao colar.
const chats = prep([
    {
        person: 'Marco Sousa', chat_name: '',
        messages: [
            { text: 'TPFlex: fluxo  Stone com PagSeguro Usando email: force-created@sandbox.pagseguro.com.br', ts: '2026-07-24T10:00:00Z' },
            { text: 'kkk blz', ts: '2026-07-24T09:00:00Z' },
        ],
    },
    {
        person: 'Gustavo Avila', chat_name: '',
        messages: [
            { text: 'é legal usar a ferramenta de migração, pois ela é determinística', ts: '2026-07-23T18:00:00Z' },
        ],
    },
]);

const hits = (q) => match.call(null, chats, q).map(r => r.msg.text);

// 1) mensagem colada com as quebras comidas pelo <input> (sem espaço no lugar do \n)
assert.deepEqual(
    hits('TPFlex:fluxo Stone com PagSeguroUsando email: force-created@sandbox.pagseguro.com.br').length,
    1,
);

// 2) mensagem colada com \n preservado (colagem programática)
assert.equal(hits('TPFlex:\nfluxo Stone com PagSeguro').length, 1);

// 3) trecho de uma linha só — já funcionava antes, não pode regredir
assert.equal(hits('fluxo').length, 1);

// 4) busca por pessoa devolve todas as mensagens dela
assert.equal(hits('marco').length, 2);

// 5) sem acento acha com acento
assert.equal(hits('migracao deterministica').length, 1);

// 6) fallback por tokens: o colado tem um emoji que o texto guardado perdeu
assert.equal(hits('kkk 😂 blz').length, 1);

// 7) busca que não existe continua vazia (o fallback não pode virar vale-tudo)
assert.equal(hits('nao existe essa mensagem aqui').length, 0);

// helpers puros
assert.equal(wiSquash('Ele é o titular. \nEle disse'), 'eleeotitular.eledisse');
assert.deepEqual(wiTokens('migração, pois é'), ['migracao', 'pois']);

// ---------- ordenação e rótulo de tempo ----------
// A queixa original: a lista não parecia estar por recência, e HH:MM puro fazia mensagem
// de dias atrás parecer de hoje. `now` é injetado pra não depender do relógio.
const NOW = new Date(2026, 6, 29, 15, 0, 0).getTime();       // 29/07/2026 15:00 local
const hAgo = (h) => new Date(NOW - h * 3600e3).toISOString();

assert.equal(wiWhen(hAgo(0.01), NOW), 'agora');
assert.match(wiWhen(hAgo(2), NOW), /^\d{1,2}[:h]\d{2}/);      // hoje: só a hora
assert.match(wiWhen(hAgo(24), NOW), /^ontem /);
assert.match(wiWhen(hAgo(72), NOW), /^dom /);                 // 26/07/2026 é domingo
assert.match(wiWhen(hAgo(24 * 10), NOW), /^19\/07 /);         // >7 dias: data
assert.equal(wiWhen(null, NOW), '');
assert.equal(wiWhen('lixo', NOW), '');
assert.equal(wiSince((NOW - 25 * 60e3) / 1000, NOW), '25 min');
assert.equal(wiSince((NOW - 4 * 3600e3) / 1000, NOW), '4 h');
assert.equal(wiSince((NOW - 3 * 86400e3) / 1000, NOW), '3 dias');

assert.equal(wiDayHeader(hAgo(2), NOW).label, 'hoje');
assert.equal(wiDayHeader(hAgo(24), NOW).label, 'ontem');
assert.equal(wiDayHeader(hAgo(24 * 10), NOW).date, '19/07');
assert.notEqual(wiDayKey(hAgo(2)), wiDayKey(hAgo(24)));       // dias diferentes = grupos diferentes

// _lastTs = ts da mensagem do OUTRO que a linha exibe (não o do chat, que inclui as minhas):
// é a chave de ordenação da lista sem busca.
const semOrdem = WorkItemsUI.prototype._prepTeams([
    { person: 'A', chat_id: 'a', ts: hAgo(0.5), messages: [{ text: 'antiga', ts: hAgo(50) }] },
    { person: 'B', chat_id: 'b', ts: hAgo(30), messages: [{ text: 'nova', ts: hAgo(1) }] },
]);
assert.equal(semOrdem[0]._lastTs, semOrdem[0].messages[0].ts);
const ordenado = [...semOrdem].sort((x, y) => String(y._lastTs).localeCompare(String(x._lastTs)));
assert.equal(ordenado[0].person, 'B');   // pelo ts do chat, 'A' subiria mostrando msg de 2 dias

// ---------- chips de pessoas recentes ----------
const chatsGrupo = WorkItemsUI.prototype._prepTeams([
    {
        person: 'Squad Billing', chat_name: 'Squad Billing', chat_id: 'g1',
        messages: [
            { sender_name: 'Carla Dias', text: 'subiu em staging', ts: hAgo(1) },
            { sender_name: 'Carlos Melo', text: 'vou revisar', ts: hAgo(5) },
            { sender_name: 'Carla Dias', text: 'bom dia', ts: hAgo(28) },
        ],
    },
    { person: 'Marco Sousa', chat_name: '', chat_id: 'd1', messages: [{ text: 'olha isso', ts: hAgo(3) }] },
]);

const people = wiRecentPeople(chatsGrupo);
assert.deepEqual(people.map(p => p.name), ['Carla Dias', 'Marco Sousa', 'Carlos Melo']);
assert.equal(people[0].count, 2);              // agrega as duas msgs da Carla
// primeiro nome só; sobrenome entra quando dois primeiros nomes colidem de verdade
assert.deepEqual(wiShortNames(people).map(p => p.short), ['Carla', 'Marco', 'Carlos']);
assert.deepEqual(wiShortNames([{ name: 'Ana Paula' }, { name: 'Ana Souza' }]).map(p => p.short),
    ['Ana P.', 'Ana S.']);

// chip -> feed da pessoa: todas as msgs dela, por recência, achatadas do grupo
assert.deepEqual(wiPersonRows(chatsGrupo, 'Carla Dias').map(r => r.msg.text),
    ['subiu em staging', 'bom dia']);
assert.deepEqual(wiPersonRows(chatsGrupo, 'Marco Sousa').map(r => r.msg.text), ['olha isso']);
assert.deepEqual(wiPersonRows(chatsGrupo, 'Ninguém'), []);

// ---------- cap do match por nome ----------
const flood = WorkItemsUI.prototype._prepTeams([
    { person: 'Carol Billing', chat_id: 'c1',
      messages: Array.from({ length: 12 }, (_, i) => ({ text: `msg ${i}`, ts: hAgo(i + 1) })) },
    { person: 'Outro', chat_id: 'c2', messages: [{ text: 'o billing quebrou', ts: hAgo(40) }] },
]);
// busca só por nome: sem hit de conteúdo pra proteger -> devolve tudo (comportamento antigo)
assert.equal(wiCapNameOnly(WorkItemsUI.prototype._teamsMatch.call(null, flood, 'carol')).length, 12);
// o termo casa o NOME de um chat e o TEXTO de outro: as 12 msgs do primeiro não podem comer
// o slice(40) e enterrar o hit de conteúdo
const misto = wiCapNameOnly(WorkItemsUI.prototype._teamsMatch.call(null, flood, 'billing'));
assert.ok(misto.length <= 7, `esperava cap, veio ${misto.length}`);
assert.ok(misto.some(r => r.msg.text.includes('billing')));

// ---------- snippet centrado no match ----------
const longa = 'bom dia, seguinte: ' + 'blá '.repeat(40) + 'o CNPJ do cliente está errado no cadastro';
const snip = wiSnippet(longa, 'CNPJ do cliente');
assert.ok(snip.includes('CNPJ do cliente'), 'o trecho buscado tem que aparecer na linha');
assert.ok(snip.length < 170 && snip.startsWith('…'));
assert.equal(wiSnippet('curta', 'curta'), 'curta');            // curta passa inteira
assert.ok(wiSnippet(longa, 'inexistente').endsWith('…'));      // sem match: começo + reticência

// ---------- identidade estável + deduplicação ----------
assert.equal(
    wiTeamsExternalKey({ chat_id: 'chat-1', msg_id: 'msg-9' }),
    'teams:chat-1:msg-9',
);
assert.equal(wiTeamsExternalKey({ chat_id: 'chat-1' }), null);

const existingItems = [
    { id: 'jira-1', source: 'jira', external_key: 'DS-201', title: 'Issue', done: true },
    { id: 'teams-1', source: 'teams', external_key: 'teams:chat-1:msg-9',
      title: 'Mensagem exata', person: 'Carol' },
    // Item legado, criado antes de msg_id ser persistido.
    { id: 'teams-old', source: 'teams', external_key: null,
      title: 'Olha esse erro em produção', person: 'Marco Sousa' },
    { id: 'merged', source: 'teams', external_key: 'teams:chat-2:msg-2',
      title: 'Cópia consolidada', person: 'Carol', merged_into: 'teams-1' },
];
assert.equal(wiFindExistingItem(existingItems, {
    source: 'jira', external_key: 'DS-201', title: 'outro título',
}).id, 'jira-1');
assert.equal(wiFindExistingItem(existingItems, {
    source: 'teams', external_key: 'teams:chat-1:msg-9',
    title: 'Mensagem exata', person: 'Carol',
}).id, 'teams-1');
assert.equal(wiFindExistingItem(existingItems, {
    source: 'teams', external_key: null,
    title: ' olha  esse erro em producao ', person: 'Marco Sousa',
}).id, 'teams-old');
assert.equal(wiFindExistingItem(existingItems, {
    source: 'manual', external_key: null, title: 'Issue',
}), null);
assert.equal(wiFindExistingItem(existingItems, {
    source: 'teams', external_key: 'teams:chat-2:msg-2',
    title: 'Cópia consolidada', person: 'Carol',
}), null);
assert.equal(wiExistingAction({ done: true }), 'reopen');
assert.equal(wiExistingAction({ archived: true }), 'unarchive');
assert.equal(wiExistingAction({ workflow_state: 'waiting' }), 'resume');
assert.equal(wiExistingAction({}), 'link');
assert.equal(wiWaitingElapsed((NOW - 30 * 60e3) / 1000, NOW), '30m');
assert.equal(wiWaitingElapsed((NOW - 3 * 3600e3) / 1000, NOW), '3h');
assert.equal(wiWaitingElapsed((NOW - 2 * 86400e3) / 1000, NOW), '2d');
assert.deepEqual(
    Object.fromEntries(Object.entries(wiPartitionWorkItems([
        { id: 'a' },
        { id: 'w', workflow_state: 'waiting' },
        { id: 'x', archived: true },
        { id: 'd', done: true },
        { id: 'm', merged_into: 'a' },
    ])).map(([k, v]) => [k, v.map(x => x.id)])),
    { active: ['a'], waiting: ['w'], archived: ['x'] },
);

// ---------- integração leve: _rows() no segmento Mensagens ----------
// Stub mínimo de DOM + bridge para exercitar o caminho real (ordem, meta, chip de pessoa)
// sem navegador. Só _rows()/_setTeamsCache, que não tocam em DOM além da caixa de busca.
const Q = { value: '' };
global.document = { getElementById: () => ({ value: Q.value }) };
// _teamsRow formata com o relógio real (não injeta `now`), então aqui os ts são
// relativos a agora — assim o teste não expira quando a data do sistema mudar.
const rAgo = (h) => new Date(Date.now() - h * 3600e3).toISOString();
const fixture = [
    // chat onde EU respondi por último: ts do chat é recente, a msg do outro é de 2 dias atrás
    { person: 'Marco Sousa', chat_name: '', chat_id: 'd1', preview: 'olha o print', ts: rAgo(0.5),
      messages: [{ sender_name: 'Marco Sousa', text: 'olha o print', ts: rAgo(50) }] },
    { person: 'Squad Billing', chat_name: 'Squad Billing', chat_id: 'g1', preview: 'subiu em staging', ts: rAgo(1),
      messages: [
          { sender_name: 'Carla Dias', text: 'subiu em staging', ts: rAgo(1) },
          { sender_name: 'Carla Dias', text: 'o CNPJ do cliente está errado', ts: rAgo(26) },
      ] },
];
global.window = { pywebview: { api: { teams_recent: async () => JSON.parse(JSON.stringify(fixture)) } } };

(async () => {
    const ui = Object.create(WorkItemsUI.prototype);
    Object.assign(ui, { _seg: 'teams', _personFilter: null, _teamsCache: null, _teamsPeople: [] });

    Q.value = '';
    const semBusca = await ui._rows();
    // ordem pelo ts da msg EXIBIDA (não pelo do chat, que inclui as minhas)
    assert.deepEqual(semBusca.map(r => r.key), ['Squad Billing', 'Marco Sousa']);
    assert.ok(semBusca.every(r => r.meta && r.ts), 'toda linha precisa de rótulo temporal');
    // DM sem topic: meta é só o rótulo temporal. 2 dias atrás tem que virar dia da semana
    // ou data — HH:MM puro é o que fazia msg antiga parecer de hoje.
    assert.match(semBusca[1].meta, /^(seg|ter|qua|qui|sex|sáb|dom|\d{2}\/\d{2}) /);
    assert.deepEqual(ui._teamsPeople.map(p => p.name), ['Carla Dias', 'Marco Sousa']);

    Q.value = 'cnpj';
    const busca = await ui._rows();
    assert.equal(busca.length, 1);
    assert.equal(busca[0].key, 'Carla Dias');            // grupo: quem falou, não o topic
    assert.equal(busca[0].msg.preview, 'o CNPJ do cliente está errado');

    Q.value = '';
    ui._personFilter = 'Carla Dias';
    const feed = await ui._rows();
    assert.deepEqual(feed.map(r => r.title), ['subiu em staging', 'o CNPJ do cliente está errado']);

    Q.value = 'staging';                                  // chip + busca combinam
    assert.deepEqual((await ui._rows()).map(r => r.title), ['subiu em staging']);

    const identityRow = ui._teamsRow({
        c: { chat_id: 'chat-1', person: 'Carol', chat_name: '' },
        msg: { msg_id: 'msg-9', sender_name: 'Carol', text: 'ping', ts: rAgo(1) },
    }, '');
    assert.equal(identityRow.msg.msg_id, 'msg-9');
    assert.equal(wiTeamsExternalKey(identityRow.msg), 'teams:chat-1:msg-9');

    console.log('teams-search tests passed');
})();
