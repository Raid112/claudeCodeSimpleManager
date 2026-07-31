/**
 * WorkItemsUI — overlays that mutate work items: the link popover (Jira/Teams/manual),
 * the close-tab ceremony, and the daily digest panel. Injects its own DOM into <body>
 * so index.html stays lean. Talks to the backend via window.pywebview.api and drives
 * state changes through window.app (which owns the work-items cache + re-render).
 */

const WI_ICON = { jira: 'i-issue', teams: 'i-msg', manual: 'i-todo' };
const WI_COLOR = { jira: 'var(--t-issue)', teams: 'var(--t-msg)', manual: 'var(--t-todo)' };

const WI_DIACRITICS = /[\u0300-\u036f]/g;
const WI_BLANK = /[\s\u00a0\u200b-\u200d\ufeff]+/g;

/** Texto sem acento e SEM espaço nenhum — a chave da busca de mensagens.
 * Colar uma mensagem de várias linhas na caixa de busca (um <input> single-line) faz o
 * navegador comer as quebras, às vezes sem deixar espaço no lugar; e o Teams ainda entrega
 * \u00a0 (nbsp) no meio do texto. Comparando sem espaço algum, colar a mensagem inteira casa. */
function wiSquash(s) {
    return (s || '').normalize('NFD').replace(WI_DIACRITICS, '').toLowerCase().replace(WI_BLANK, '');
}

/** Tokens ≥2 chars, sem acento. Rede de segurança quando o squash falha porque o texto
 * guardado perdeu um pedaço que o colado tem (emoji virou nada, anexo virou [imagem]). */
function wiTokens(s) {
    return (s || '').normalize('NFD').replace(WI_DIACRITICS, '').toLowerCase()
        .split(/[^a-z0-9]+/).filter(t => t.length >= 2);
}

/** Sem acento, minúsculo, MAS preservando os espaços e o comprimento — para localizar
 * a posição do match no texto original (wiSquash destrói offsets). */
function wiFold(s) {
    return (s || '').normalize('NFD').replace(WI_DIACRITICS, '').toLowerCase();
}

function wiTeamsExternalKey(msg) {
    if (!msg || !msg.chat_id || !msg.msg_id) return null;
    return `teams:${msg.chat_id}:${msg.msg_id}`;
}

function wiFindExistingItem(items, candidate) {
    const all = (items || []).filter(it => !it.merged_into);
    if (candidate.external_key) {
        const exact = all.find(it =>
            it.source === candidate.source && it.external_key === candidate.external_key);
        if (exact) return exact;
    }
    // Items created before Teams message ids were persisted can still be recognized
    // conservatively by the same person + normalized full message.
    if (candidate.source === 'teams') {
        const title = wiSquash(candidate.title);
        const person = wiSquash(candidate.person);
        return all.find(it =>
            it.source === 'teams'
            && !it.external_key
            && wiSquash(it.title) === title
            && wiSquash(it.person) === person) || null;
    }
    return null;
}

function wiExistingAction(item) {
    if (item && item.done) return 'reopen';
    if (item && item.archived) return 'unarchive';
    if (item && item.workflow_state === 'waiting') return 'resume';
    return 'link';
}

const WI_DOW = ['dom', 'seg', 'ter', 'qua', 'qui', 'sex', 'sáb'];

function wiMidnight(x) {
    const d = new Date(x);
    d.setHours(0, 0, 0, 0);
    return d.getTime();
}

/** Dias de calendário entre `ts` e agora (0 = hoje, 1 = ontem). NaN se ts inválido. */
function wiDaysAgo(ts, now = Date.now()) {
    const d = new Date(ts);
    if (isNaN(d)) return NaN;
    return Math.round((wiMidnight(now) - wiMidnight(d)) / 86400000);
}

/** Rótulo temporal curto da linha: 'agora' · '14:32' (hoje) · 'ontem 09:12' ·
 * 'sex 16:40' (mesma semana) · '22/07 11:05'. Sem isto o usuário não tem como
 * conferir a ordem: HH:MM puro faz mensagem de 5 dias atrás parecer de hoje. */
function wiWhen(ts, now = Date.now()) {
    if (!ts) return '';
    const d = new Date(ts);
    if (isNaN(d)) return '';
    const hhmm = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    const mins = (now - d.getTime()) / 60000;
    if (mins >= 0 && mins < 2) return 'agora';
    const dias = wiDaysAgo(ts, now);
    if (dias <= 0) return hhmm;
    if (dias === 1) return `ontem ${hhmm}`;
    if (dias < 7) return `${WI_DOW[d.getDay()]} ${hhmm}`;
    const dd = String(d.getDate()).padStart(2, '0'), mm = String(d.getMonth() + 1).padStart(2, '0');
    return `${dd}/${mm} ${hhmm}`;
}

function wiSince(tsSeconds, now = Date.now()) {
    if (!tsSeconds) return 'agora';
    const mins = Math.max(0, Math.floor((now - tsSeconds * 1000) / 60000));
    if (mins < 60) return `${mins} min`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours} h`;
    const days = Math.floor(hours / 24);
    return `${days} ${days === 1 ? 'dia' : 'dias'}`;
}

/** Chave de agrupamento por dia local ('2026-07-29'); '' se ts inválido. */
function wiDayKey(ts) {
    const d = new Date(ts);
    if (!ts || isNaN(d)) return '';
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

/** Cabeçalho do grupo de dia: 'hoje' / 'ontem' / 'sex' / '22/07' + a data crua. */
function wiDayHeader(ts, now = Date.now()) {
    const d = new Date(ts);
    if (!ts || isNaN(d)) return { label: 'sem data', date: '' };
    const dias = wiDaysAgo(ts, now);
    const dd = String(d.getDate()).padStart(2, '0'), mm = String(d.getMonth() + 1).padStart(2, '0');
    const label = dias <= 0 ? 'hoje' : dias === 1 ? 'ontem' : dias < 7 ? WI_DOW[d.getDay()] : `${dd}/${mm}`;
    return { label, date: `${dd}/${mm}` };
}

/** Quem me mandou mensagem, mais recente primeiro. Todas as `messages` que o backend
 * devolve já são do OUTRO (nunca minhas), então basta agregar por remetente:
 * `sender_name` (necessário em grupo) com fallback no nome do chat 1:1. */
function wiRecentPeople(chats, limit = 10) {
    const map = new Map();
    for (const c of (chats || [])) {
        for (const m of (c.messages || [])) {
            const name = m.sender_name || c.person;
            if (!name) continue;
            const ts = String(m.ts || '');
            const cur = map.get(name);
            if (!cur) map.set(name, { name, ts, count: 1 });
            else { cur.count++; if (ts > cur.ts) cur.ts = ts; }
        }
    }
    return [...map.values()]
        .sort((a, b) => b.ts.localeCompare(a.ts))
        .slice(0, limit);
}

/** Rótulo curto do chip: primeiro nome, desambiguado com a inicial do sobrenome
 * quando duas pessoas compartilham o primeiro nome. */
function wiShortNames(people) {
    const first = (n) => (n || '').trim().split(/\s+/)[0] || n || '?';
    const dupes = new Set();
    const seen = new Set();
    for (const p of people) {
        const f = first(p.name).toLowerCase();
        if (seen.has(f)) dupes.add(f); else seen.add(f);
    }
    return people.map(p => {
        const parts = (p.name || '').trim().split(/\s+/);
        const f = parts[0] || p.name || '?';
        const short = dupes.has(f.toLowerCase()) && parts[1] ? `${f} ${parts[1][0]}.` : f;
        return { ...p, short };
    });
}

/** Todas as mensagens de uma pessoa, achatadas e por recência. É o que o chip faz:
 * filtrar a lista de-uma-linha-por-chat daria 1 linha só num DM (inútil). */
function wiPersonRows(chats, person, limit = 60) {
    const out = [];
    for (const c of (chats || [])) {
        for (const msg of (c.messages || [])) {
            if ((msg.sender_name || c.person) === person) out.push({ c, msg });
        }
    }
    out.sort((a, b) => String(b.msg.ts || '').localeCompare(String(a.msg.ts || '')));
    return out.slice(0, limit);
}

/** Um chat que casa só pelo NOME entrega até 50 mensagens e, no corte global por
 * recência, come a lista inteira — enterrando o hit de conteúdo de outro chat.
 * Só limita quando existe hit de conteúdo para proteger: busca puramente por pessoa
 * continua devolvendo tudo. */
function wiCapNameOnly(rows, cap = 6) {
    if (!rows.some(r => !r.nameOnly)) return rows;
    const per = new Map();
    return rows.filter(r => {
        if (!r.nameOnly) return true;
        const id = (r.c && r.c.chat_id) || '';
        const n = (per.get(id) || 0) + 1;
        per.set(id, n);
        return n <= cap;
    });
}

/** Janela de ~`width` chars centrada no primeiro pedaço da busca que aparece no texto.
 * A linha é truncada por CSS: sem isto, colar um trecho do MEIO de uma mensagem longa
 * mostra só o começo dela e o usuário não vê o que casou. */
function wiSnippet(text, q, width = 130) {
    const t = String(text || '');
    if (t.length <= width) return t;
    const ft = wiFold(t);
    const needles = [wiFold(q).trim(), ...wiTokens(q).sort((a, b) => b.length - a.length)];
    let at = -1;
    for (const n of needles) {
        if (n.length < 2) continue;
        at = ft.indexOf(n);
        if (at >= 0) break;
    }
    if (at < 0) return t.slice(0, width).trimEnd() + '…';
    let start = Math.max(0, at - Math.floor(width / 3));
    if (start > 0) {                                  // não corta palavra no início
        const sp = t.indexOf(' ', start);
        if (sp >= 0 && sp - start < 15) start = sp + 1;
    }
    const end = Math.min(t.length, start + width);
    return (start > 0 ? '…' : '') + t.slice(start, end).trim() + (end < t.length ? '…' : '');
}

class WorkItemsUI {
    constructor(app) {
        this.app = app;
        this._targetSession = null;   // pty session id the popover acts on
        this._seg = 'jira';
        this._jiraCache = null;       // last jira list (avoid refetch on keystroke)
        this._teamsCache = null;      // recent chats + messages (content search); per popover open
        this._statusFilter = null;    // active status chip (jira), null = todos
        this._personFilter = null;    // active person chip (teams), null = todos
        this._teamsPeople = [];       // quem me mandou msg, por recência (chips)
        this._searchTimer = null;
        this._sel = -1;               // linha selecionada (navegação por teclado)
        this._injectDom();
    }

    _esc(s) {
        return (s || '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
    }

    _injectDom() {
        const el = document.createElement('div');
        el.innerHTML = `
        <div class="scrim" id="wi-scrim"></div>
        <div class="pop" id="wi-pop">
          <div class="pop-h"><span id="wi-pop-title"></span><span class="wi-refresh" id="wi-refresh" title="Atualizar mensagens do Teams" style="display:none">↻</span></div>
          <div class="seg" id="wi-seg">
            <div data-k="jira"><svg viewBox="0 0 20 20"><use href="#i-issue"/></svg>Jira</div>
            <div data-k="teams"><svg viewBox="0 0 20 20"><use href="#i-msg"/></svg>Mensagens</div>
            <div data-k="manual"><svg viewBox="0 0 20 20"><use href="#i-todo"/></svg>TODO</div>
          </div>
          <input class="search" id="wi-q" placeholder="buscar…">
          <div class="chips filter-chips" id="wi-chips"></div>
          <div class="list" id="wi-list"></div>
          <div class="hint" id="wi-hint"></div>
        </div>
        <div class="close" id="wi-close">
          <h4 id="wi-close-h"></h4>
          <div class="acts" id="wi-close-acts"></div>
          <div class="foot"><span id="wi-close-plain">fechar sem nada</span><span id="wi-close-cancel">cancelar</span></div>
        </div>
        <div class="pop wi-daily" id="wi-daily">
          <div class="pop-h">Resumo — <b>daily</b></div>
          <div class="list" id="wi-daily-list"></div>
        </div>`;
        document.body.appendChild(el);

        this.scrim = document.getElementById('wi-scrim');
        this.pop = document.getElementById('wi-pop');
        this.closeBox = document.getElementById('wi-close');
        this.daily = document.getElementById('wi-daily');

        this.scrim.addEventListener('click', () => this.closeAll());
        document.addEventListener('keydown', (e) => { if (e.key === 'Escape') this.closeAll(); });

        document.querySelectorAll('#wi-seg div').forEach(d =>
            d.addEventListener('click', () => this._setSeg(d.dataset.k)));
        const q = document.getElementById('wi-q');
        q.addEventListener('input', () => {
            clearTimeout(this._searchTimer);
            this._searchTimer = setTimeout(() => {
                this._searchTimer = null;
                this._renderList();
            }, 180);
        });
        // Navegar sem sair da caixa de busca: ↓/↑ move, Enter vincula a linha selecionada.
        q.addEventListener('keydown', (e) => {
            if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                e.preventDefault();
                this._move(e.key === 'ArrowDown' ? 1 : -1);
            } else if (e.key === 'Enter') {
                e.preventDefault();
                this._activateSel();
            }
        });
        document.getElementById('wi-close-plain').addEventListener('click', () => {
            const id = this._targetSession; this.closeAll(); if (id) this.app.closeTerminal(id);
        });
        document.getElementById('wi-close-cancel').addEventListener('click', () => this.closeAll());
        document.getElementById('wi-refresh').addEventListener('click', () => this._refreshTeams());
    }

    closeAll() {
        [this.scrim, this.pop, this.closeBox, this.daily].forEach(x => x.classList.remove('on'));
    }

    // ---------- link popover ----------
    /** Sem ptySessionId o popover vira "criar work item solto": o item nasce na lista de
     * Trabalho sem aba nenhuma vinculada (`_linkTarget` só recarrega quando não há alvo). */
    async openLinkPopover(ptySessionId = null) {
        this._targetSession = ptySessionId;
        document.getElementById('wi-pop-title').innerHTML = ptySessionId
            ? 'Vincular <b>aba</b> a um work item'
            : 'Criar <b>work item</b>';
        this.scrim.classList.add('on');
        this.pop.classList.add('on');
        // Default to a segment that works: Jira only if enabled.
        let jiraOn = false;
        try { jiraOn = await window.pywebview.api.jira_available(); } catch (e) {}
        this._jiraCache = null;
        this._teamsCache = null;
        this._setSeg(jiraOn ? 'jira' : 'manual');
        document.getElementById('wi-q').focus();
    }

    async _setSeg(k) {
        this._seg = k;
        this._statusFilter = null;
        this._personFilter = null;
        document.querySelectorAll('#wi-seg div').forEach(d => d.classList.toggle('on', d.dataset.k === k));
        document.getElementById('wi-refresh').style.display = (k === 'teams') ? '' : 'none';
        const q = document.getElementById('wi-q');
        q.value = '';
        q.placeholder = { jira: 'buscar por key (DS-201) ou nome…', teams: 'buscar por pessoa ou conteúdo das mensagens…', manual: 'buscar ou digitar um TODO novo…' }[k];
        const hints = {
            jira: 'Guarda key · status · prazo. A aba vira <b>DS-xxx</b>.',
            teams: 'Chip filtra por <b>pessoa</b> · <b>↑↓</b> navega · <b>Enter</b> vincula.',
            manual: 'Texto livre. Vira work item reutilizável.',
        };
        document.getElementById('wi-hint').innerHTML = hints[k];
        await this._renderList();
    }

    async _rows() {
        const q = (document.getElementById('wi-q').value || '').trim();
        if (this._seg === 'jira') {
            let list;
            if (q) { list = await window.pywebview.api.jira_search(q, 25); }
            else {
                if (!this._jiraCache) this._jiraCache = await window.pywebview.api.jira_list_issues(50);
                list = this._jiraCache;
            }
            this._jiraStatuses = [...new Set((this._jiraCache || list || [])
                .map(i => i.status).filter(Boolean))];
            return (list || [])
                .filter(i => !this._statusFilter || i.status === this._statusFilter)
                .map(i => ({
                    key: i.external_key, title: i.title,
                    meta: [i.status, i.duedate].filter(Boolean).join(' · ') || 'sem prazo',
                    issue: i,
                }));
        }
        if (this._seg === 'teams') {
            // Graph Search API (/search/query) dá 403 (falta ChannelMessage.Read.All + admin
            // consent). Em vez disso o backend carrega os chats recentes + as últimas msgs de
            // cada um (Chat.Read cobre isso), só as do OUTRO (nunca as minhas). Cache no
            // backend por 120s; cada tecla filtra sobre o cache local — instantâneo.
            if (!this._teamsCache) {
                this._setTeamsCache(await window.pywebview.api.teams_recent(30));
            }
            const chats = (this._teamsCache || []);
            // Chip de pessoa: sai da visão por chat e vira o feed daquela pessoa (a busca,
            // se houver, filtra dentro dele).
            if (this._personFilter) {
                let rows = wiPersonRows(chats, this._personFilter);
                if (q) {
                    const hit = new Set(this._teamsMatch(chats, q).map(r => r.msg));
                    rows = rows.filter(r => hit.has(r.msg));
                }
                return rows.map(r => this._teamsRow(r, q));
            }
            if (!q) {
                // Sem busca: uma linha por chat = última msg do outro (já é o preview, com
                // placeholder [áudio]/[imagem] quando for anexo). Ordena pelo ts DESSA msg
                // (_lastTs), não pelo do chat: a ordem do backend inclui as MINHAS mensagens,
                // então um chat onde eu respondi por último subia ao topo exibindo uma
                // mensagem de dias atrás — a queixa de "não está por recência".
                return chats.filter(m => (m.preview || '').trim())
                    .sort((a, b) => String(b._lastTs || '').localeCompare(String(a._lastTs || '')))
                    .map(m => ({
                        key: m.person || '?', title: m.preview, ts: m._lastTs,
                        meta: [m.chat_name, wiWhen(m._lastTs)].filter(Boolean).join(' · '),
                        msg: { ...m, preview: m.preview },
                    }));
            }
            // Com busca: achata as mensagens individuais do outro e vincula em UMA delas.
            // Casa por nome (person/chat) OU pelo texto da mensagem — assim "carol" lista as
            // msgs dela (incl. as antigas) e "cnpj" acha a mensagem direto.
            const rows = wiCapNameOnly(this._teamsMatch(chats, q));
            // Só recência: mais recente primeiro (ts ISO ordena lexical). Assim conversa ativa
            // de hoje fica no topo e menções antigas (ForgeAI de dias atrás) descem pro fim.
            rows.sort((a, b) => String(b.msg.ts || '').localeCompare(String(a.msg.ts || '')));
            return rows.slice(0, 40).map(r => this._teamsRow(r, q));
        }
        // manual: existing manual items + a create row
        const items = (this.app.workItems.items || []).filter(it => it.source === 'manual' && !it.done)
            .filter(it => !q || (it.title || '').toLowerCase().includes(q.toLowerCase()))
            .map(it => ({ key: 'TODO', title: it.title, meta: '', existing: it }));
        return items;
    }

    /** Pré-computa as formas de busca uma vez por carga (≈30 chats × 50 msgs), em vez de
     * re-normalizar 1500 strings a cada tecla. `_lastTs` = ts da última mensagem do OUTRO,
     * que é exatamente o texto exibido na linha sem busca (a chave de ordenação tem que ser
     * a do conteúdo mostrado). Puro: não toca em `this` (o teste chama sem instância). */
    _prepTeams(chats) {
        for (const c of (chats || [])) {
            const who = `${c.person || ''} ${c.chat_name || ''}`;
            c._s = wiSquash(who);
            c._t = wiTokens(who);
            let last = '';
            for (const m of (c.messages || [])) {
                m._s = wiSquash(m.text);
                m._t = wiTokens(m.text);
                const ts = String(m.ts || '');
                if (ts > last) last = ts;
            }
            c._lastTs = last || c.ts || '';
        }
        return chats || [];
    }

    /** Guarda o cache do Teams já normalizado + a lista de pessoas dos chips (agregada uma
     * vez por carga, não a cada tecla). */
    _setTeamsCache(chats) {
        this._teamsCache = this._prepTeams(chats);
        this._teamsPeople = wiShortNames(wiRecentPeople(this._teamsCache));
        // Pessoa filtrada desapareceu do cache novo: não deixa filtro fantasma preso.
        if (this._personFilter && !this._teamsPeople.some(p => p.name === this._personFilter)) {
            this._personFilter = null;
        }
        return this._teamsCache;
    }

    /** {c, msg} -> linha da lista. `q` só afeta a exibição (snippet centrado no match). */
    _teamsRow({ c, msg }, q) {
        return {
            key: msg.sender_name || c.person || '?',   // grupo: quem falou, não o topic
            title: q ? wiSnippet(msg.text, q) : msg.text,
            ts: msg.ts,
            meta: [c.chat_name, wiWhen(msg.ts)].filter(Boolean).join(' · '),
            // vincula NESTA mensagem: preview = o texto escolhido (não o último do chat).
            msg: {
                ...c,
                msg_id: msg.msg_id,
                preview: msg.text,
                person: msg.sender_name || c.person,
            },
        };
    }

    /** Mensagens que casam com a busca, em dois níveis:
     *  1) substring ignorando acento e QUALQUER espaço — cobre a mensagem colada inteira;
     *  2) só se o nível 1 não achou nada: todos os tokens da busca presentes no texto —
     *     salva o caso em que o texto guardado perdeu um trecho que o colado tem. */
    _teamsMatch(chats, q) {
        const collect = (hit) => {
            const out = [];
            for (const c of chats) {
                const nameHit = hit(c._s, c._t);
                for (const msg of (c.messages || [])) {
                    const textHit = hit(msg._s, msg._t);
                    // nameOnly: entrou por casar o NOME do chat, não o próprio texto.
                    if (nameHit || textHit) out.push({ c, msg, nameOnly: !textHit });
                }
            }
            return out;
        };
        const qs = wiSquash(q);
        const rows = collect((s) => s.includes(qs));
        if (rows.length) return rows;
        const qt = wiTokens(q);
        if (qt.length < 2) return rows;
        return collect((s, t) => qt.every(x => t.some(y => y.includes(x))));
    }

    /** Force-refresh the Teams cache (bypass the 120s TTL) and re-render. */
    async _refreshTeams() {
        if (this._seg !== 'teams') return;
        const btn = document.getElementById('wi-refresh');
        if (btn.classList.contains('spinning')) return;  // already refreshing
        btn.classList.add('spinning');
        try {
            this._setTeamsCache(await window.pywebview.api.teams_recent(30, true));
        } catch (e) {
            this._teamsCache = this._teamsCache || [];
        }
        btn.classList.remove('spinning');
        await this._renderList();
    }

    async _renderList() {
        const listEl = document.getElementById('wi-list');
        listEl.innerHTML = '<div class="hint">carregando…</div>';
        let rows;
        try { rows = await this._rows(); } catch (e) { rows = []; }
        this._renderChips();
        const color = WI_COLOR[this._seg], ic = WI_ICON[this._seg];
        const q = (document.getElementById('wi-q').value || '').trim();
        // Mensagens: cabeçalho de dia entre os grupos (hoje / ontem / sex / 22/07). Torna a
        // ordem por recência visível em vez de deduzível.
        const byDay = this._seg === 'teams';
        let html = '', lastDay = null;
        rows.forEach((r, i) => {
            if (byDay) {
                const k = wiDayKey(r.ts);
                if (k !== lastDay) {
                    lastDay = k;
                    const h = wiDayHeader(r.ts);
                    html += `<div class="daily-day">${this._esc(h.label)}<span class="daily-date">${this._esc(h.date)}</span></div>`;
                }
            }
            html += `
          <div class="row${byDay ? ' msg' : ''}" data-i="${i}" style="--rc:${color}">
            <svg class="ic" viewBox="0 0 20 20"><use href="#${ic}"/></svg>
            <div class="tx"><div class="t1"><span class="k" style="color:${color}">${this._esc(r.key)}</span>${this._mark(r.title, q)}</div>
            <div class="t2">${this._esc(r.meta)}</div></div></div>`;
        });
        if (this._seg === 'manual') {
            const sub = this._targetSession ? 'vincula esta aba na hora' : 'entra direto em Trabalho';
            html += `<div class="row new" data-create="1"><svg class="ic" viewBox="0 0 20 20"><use href="#i-add"/></svg>
              <div class="tx"><div class="t1">Criar TODO${q ? ` “${this._esc(q)}”` : ' vazio'}</div>
              <div class="t2">${sub}</div></div></div>`;
        }
        if (rows.length === 0 && this._seg === 'jira') {
            html = `<div class="hint">Nenhuma issue em ${'DS'}. Confira o token do Jira em secrets.json.</div>` + html;
        }
        // Teams: o cache do backend tem TTL de 120s, então uma mensagem que acabou de chegar
        // ainda não está aqui — aponta o ↻ em vez de deixar o usuário achando que sumiu.
        const empty = this._seg === 'teams'
            ? '<div class="hint">Nada encontrado. Mensagem recém-chegada? Clique em ↻ para recarregar o Teams.</div>'
            : '<div class="hint">nada aqui</div>';
        listEl.innerHTML = html || empty;
        this._storedRows = rows;
        listEl.querySelectorAll('.row[data-i]').forEach(el =>
            el.addEventListener('click', () => this._pick(rows[+el.dataset.i])));
        const createEl = listEl.querySelector('.row[data-create]');
        if (createEl) createEl.addEventListener('click', () => this._createManual(q));
        // Navegação por teclado: com busca ativa a primeira linha já vem selecionada
        // (Enter vincula direto); sem busca nada vem selecionado, pra não vincular sem querer.
        this._rowEls = [...listEl.querySelectorAll('.row')];
        this._sel = (q && this._rowEls.length) ? 0 : -1;
        if (this._sel === 0) this._rowEls[0].classList.add('sel');
    }

    /** Destaca no texto o que casou com a busca. Fatia o texto CRU e escapa cada pedaço:
     * aplicar a regex sobre HTML já escapado casaria dentro de entidade (&amp; contém "amp").
     * Match literal (sem fold), então "migracao" não pinta "migração" — a linha ainda aparece,
     * só não fica realçada. */
    _mark(text, q) {
        // ≥3 chars só aqui (a busca continua em ≥2): pintar "do"/"de"/"no" acenderia
        // meia mensagem em português.
        const toks = [...new Set(wiTokens(q))].filter(t => t.length >= 3)
            .sort((a, b) => b.length - a.length).slice(0, 8);
        if (!toks.length) return this._esc(text);
        const re = new RegExp(`(${toks.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})`, 'gi');
        return String(text || '').split(re)
            .map((part, i) => (i % 2 ? `<mark>${this._esc(part)}</mark>` : this._esc(part)))
            .join('');
    }

    _move(delta) {
        const els = this._rowEls || [];
        if (!els.length) return;
        this._sel = Math.max(0, Math.min(els.length - 1, this._sel + delta));
        els.forEach((el, i) => el.classList.toggle('sel', i === this._sel));
        els[this._sel].scrollIntoView({ block: 'nearest' });
    }

    /** Enter na caixa de busca: age SÓ na linha selecionada (no TODO, cria com o texto
     * digitado). Nunca age sem seleção: com a busca vazia nada está selecionado, e um Enter
     * reflexo criaria+vincularia a mensagem do topo sem o usuário pedir. */
    async _activateSel() {
        if (this._searchTimer) {
            // Enter antes do debounce de 180ms: renderiza a busca atual primeiro, senão a
            // seleção ainda aponta pra lista anterior (não filtrada).
            clearTimeout(this._searchTimer);
            this._searchTimer = null;
            await this._renderList();
        }
        const els = this._rowEls || [];
        if (this._sel >= 0 && els[this._sel]) { els[this._sel].click(); return; }
        if (this._seg === 'manual') {
            this._createManual((document.getElementById('wi-q').value || '').trim());
        }
    }

    /** Filter chips: status no Jira, pessoas recentes nas Mensagens. */
    _renderChips() {
        const box = document.getElementById('wi-chips');
        if (!box) return;
        box.classList.toggle('people', this._seg === 'teams');
        if (this._seg === 'teams') { this._renderPeopleChips(box); return; }
        const statuses = (this._seg === 'jira' && this._jiraStatuses) || [];
        if (!statuses.length) { box.innerHTML = ''; return; }
        const chip = (label, val) =>
            `<span class="chip${this._statusFilter === val ? ' on' : ''}" data-st="${this._esc(val ?? '')}">${this._esc(label)}</span>`;
        box.innerHTML = chip('todas', null) + statuses.map(s => chip(s, s)).join('');
        box.querySelectorAll('.chip').forEach(el => el.addEventListener('click', () => {
            const v = el.dataset.st || null;
            this._statusFilter = (this._statusFilter === v) ? null : v;
            this._renderList();
        }));
    }

    /** Pessoas que me mandaram mensagem, mais recente primeiro. Clicar abre o feed
     * completo daquela pessoa (todas as msgs no cache, não só a última do chat). */
    _renderPeopleChips(box) {
        const people = this._teamsPeople || [];
        if (!people.length) { box.innerHTML = ''; return; }
        const chip = (label, val, title, n = '') =>
            `<span class="chip${this._personFilter === val ? ' on' : ''}" data-p="${this._esc(val || '')}"` +
            ` title="${this._esc(title)}">${this._esc(label)}${n}</span>`;
        box.innerHTML = chip('todas', null, 'Todos os chats recentes')
            + people.map(p => chip(p.short, p.name,
                `${p.name} · última ${wiWhen(p.ts)} · ${p.count} msg`,
                `<i class="chip-n">${p.count}</i>`)).join('');
        box.querySelectorAll('.chip').forEach(el => el.addEventListener('click', () => {
            const v = el.dataset.p || null;
            this._personFilter = (this._personFilter === v) ? null : v;
            this._renderList();
        }));
    }

    async _linkTarget(wiId) {
        const id = this._targetSession;
        if (id) await this.app.linkSessionToItem(id, wiId);
        else await this.app.refreshWorkItems();
    }

    async _reuseExisting(existing) {
        const action = wiExistingAction(existing);
        if (action === 'reopen') {
            if (!window.confirm(`“${existing.title}” já foi concluído. Reabrir e reutilizar?`)) {
                return false;
            }
            await this.app.reopenWorkItem(existing.id);
        } else if (action === 'unarchive') {
            await this.app.archiveWorkItem(existing.id, false);
        } else if (action === 'resume') {
            await this.app.setWorkItemWaiting(existing.id, false);
        }
        await this._linkTarget(existing.id);
        return true;
    }

    async _pick(row) {
        this.closeAll();
        if (this._seg === 'jira') {
            const iss = row.issue;
            const existing = wiFindExistingItem(this.app.workItems.items, {
                source: 'jira', external_key: iss.external_key, title: iss.title,
            });
            if (existing) {
                await this._reuseExisting(existing);
                return;
            }
            const it = await this.app.createWorkItem('jira', iss.title, {
                external_key: iss.external_key, external_url: iss.external_url,
                status: iss.status, duedate: iss.duedate, duedate_has_time: false,
            });
            await this._linkTarget(it.id);
        } else if (this._seg === 'teams') {
            const m = row.msg;
            const externalKey = wiTeamsExternalKey(m);
            const existing = wiFindExistingItem(this.app.workItems.items, {
                source: 'teams',
                external_key: externalKey,
                title: m.preview || m.chat_name || 'mensagem',
                person: m.person,
            });
            if (existing) {
                await this._reuseExisting(existing);
                return;
            }
            const it = await this.app.createWorkItem('teams', m.preview || m.chat_name || 'mensagem', {
                external_key: externalKey, person: m.person, external_url: null,
            });
            await this._linkTarget(it.id);
        } else if (row.existing) {
            await this._linkTarget(row.existing.id);
        }
    }

    async _createManual(text) {
        this.closeAll();
        const named = (text || '').trim();
        const it = await this.app.createWorkItem('manual', named || 'Novo TODO', {});
        await this._linkTarget(it.id);
        // Created without a name -> drop straight into inline rename so the user
        // can title it right away (instead of being stuck with "Novo TODO").
        if (!named && this.app.sidebar) this.app.sidebar.renameItemById(it.id);
    }

    // ---------- complete confirmation (from the sidebar dot) ----------
    async confirmComplete(wiId) {
        await this.app.completeWorkItem(wiId);
    }

    // ---------- close ceremony ----------
    async openCloseDialog(ptySessionId, wi) {
        this._targetSession = ptySessionId;
        const sessionKey = this.app.sessionLinkKeyOf(ptySessionId);
        const color = WI_COLOR[wi.source] || 'var(--t-todo)';
        const keyLabel = wi.source === 'jira' ? wi.external_key
            : wi.source === 'teams' ? (wi.person || '?').toUpperCase() : 'TODO';
        document.getElementById('wi-close-h').innerHTML =
            `Fechar <span style="color:${color}">${this._esc(keyLabel)}</span> · ${this._esc(this.app.getDisplayName(ptySessionId))}`;

        const acts = document.getElementById('wi-close-acts');
        let html = `<div class="act-row main" data-act="complete"><svg viewBox="0 0 20 20"><use href="#i-check"/></svg>Concluir work item<span class="tag">marca done</span></div>`;
        if (wi.source === 'jira' && wi.external_key) {
            html += `<div class="act-row" data-act="transition"><svg viewBox="0 0 20 20"><use href="#i-issue"/></svg>Transicionar issue<span class="tag" id="wi-trans-tag">${this._esc(wi.status || '')} →</span></div>`;
            html += `<div class="act-row" data-act="browser"><svg viewBox="0 0 20 20"><use href="#i-msg"/></svg>Abrir no Jira<span class="tag">comentar</span></div>`;
        }
        if (wi.source === 'teams') {
            html += `<div class="act-row" data-act="notify"><svg viewBox="0 0 20 20"><use href="#i-msg"/></svg>Copiar aviso de conclusão<span class="tag">avisar a pessoa</span></div>`;
        }
        html += `<div class="act-row wait" data-act="wait"><svg viewBox="0 0 20 20"><use href="#i-wait"/></svg>Aguardar resposta<span class="tag">esconde abas · mantém cache</span></div>`;
        html += `<div class="act-row" data-act="archive"><svg viewBox="0 0 20 20"><use href="#i-arch"/></svg>Só arquivar a aba<span class="tag">item segue aberto</span></div>`;
        acts.innerHTML = html;

        acts.querySelectorAll('.act-row').forEach(el =>
            el.addEventListener('click', () => this._closeAct(el.dataset.act, ptySessionId, sessionKey, wi)));

        this.scrim.classList.add('on');
        this.closeBox.classList.add('on');
    }

    async _closeAct(act, ptySessionId, sessionKey, wi) {
        if (act === 'transition') { await this._showTransitions(wi); return; }  // stays open
        this.closeAll();
        if (act === 'complete') {
            await this.app.completeWorkItem(wi.id);
            await this.app.closeTerminal(ptySessionId);
        } else if (act === 'wait') {
            await this.app.setWorkItemWaiting(wi.id, true);
        } else if (act === 'archive') {
            if (sessionKey) await this.app.archiveSession(sessionKey, true);
            await this.app.closeTerminal(ptySessionId);
        } else if (act === 'browser') {
            if (wi.external_url) window.pywebview.api.open_url(wi.external_url);
        } else if (act === 'notify') {
            const txt = `Concluí: ${wi.title}`;
            try { await navigator.clipboard.writeText(txt); } catch (e) {}
            await this.app.completeWorkItem(wi.id);
            await this.app.closeTerminal(ptySessionId);
        }
    }

    async _showTransitions(wi) {
        const acts = document.getElementById('wi-close-acts');
        acts.innerHTML = '<div class="hint">carregando transições…</div>';
        let trans = [];
        try { trans = await window.pywebview.api.jira_transitions(wi.external_key); } catch (e) {}
        if (!trans.length) { acts.innerHTML = '<div class="hint">sem transições disponíveis</div>'; return; }
        acts.innerHTML = trans.map(t =>
            `<div class="act-row" data-tid="${t.id}"><svg viewBox="0 0 20 20"><use href="#i-issue"/></svg>${this._esc(t.name)}<span class="tag">→ ${this._esc(t.to_status || '')}</span></div>`).join('');
        acts.querySelectorAll('.act-row[data-tid]').forEach(el =>
            el.addEventListener('click', async () => {
                this.closeAll();
                await window.pywebview.api.jira_transition(wi.external_key, el.dataset.tid);
                await window.pywebview.api.refresh_jira_item(wi.id, wi.external_key);
                await this.app.refreshWorkItems();
            }));
    }

    // ---------- daily digest ----------
    async openDaily() {
        this.scrim.classList.add('on');
        this.daily.classList.add('on');
        const listEl = document.getElementById('wi-daily-list');
        listEl.innerHTML = '<div class="hint">carregando…</div>';
        let overview = { days: [], waiting: [], counts: {} };
        try { overview = await window.pywebview.api.work_daily_digest(2); } catch (e) {}
        // Backward-compatible with a backend from before the overview envelope.
        if (Array.isArray(overview)) overview = { days: overview, waiting: [], counts: {} };
        const counts = overview.counts || {};
        const digest = overview.days || [];
        const labelDay = ['hoje', 'ontem'];
        let html = `<div class="daily-stats">`
            + `<div><b>${counts.active || 0}</b><span>em foco</span></div>`
            + `<div class="waiting"><b>${counts.waiting || 0}</b><span>aguardando</span></div>`
            + `<div><b>${counts.completed_today || 0}</b><span>concluídos hoje</span></div>`
            + `</div>`;
        if ((overview.waiting || []).length) {
            html += `<div class="daily-day waiting-title">Aguardando <span class="daily-date">respostas e dependências</span></div>`;
            for (const it of overview.waiting) {
                const color = WI_COLOR[it.source] || 'var(--t-todo)';
                const key = it.source === 'jira' ? (it.external_key || 'JIRA')
                    : it.source === 'teams'
                        ? ((it.person || 'MSG').split(/\s+/)[0]).toUpperCase()
                        : 'TODO';
                const last = it.last_activity ? wiWhen(it.last_activity * 1000) : '';
                html += `<div class="row daily-wait"><div class="tx">`
                    + `<div class="t1"><span class="k" style="color:${color}">${this._esc(key)}</span>${this._esc(it.title)}</div>`
                    + `<div class="t2">aguardando há ${wiSince(it.waiting_since)}${last ? ` · última atividade ${this._esc(last)}` : ''}</div>`
                    + `</div></div>`;
            }
        }
        digest.forEach((d, i) => {
            html += `<div class="daily-day">${labelDay[i] || d.day} <span class="daily-date">${d.day}</span></div>`;
            if (!d.items.length) { html += `<div class="hint">nada registrado</div>`; return; }
            for (const it of d.items) {
                const color = WI_COLOR[it.source] || 'var(--t-todo)';
                const key = it.source === 'jira' ? (it.external_key || 'JIRA')
                    : (it.source === 'teams' ? '💬' : 'TODO');
                const kinds = it.kinds.map(k => ({
                    create: 'criou', link: 'vinculou', complete: 'concluiu',
                    status: 'status', duedate: 'prazo', wait: 'aguardou',
                    resume: 'retomou', reopen: 'reabriu',
                }[k] || k)).join(', ');
                html += `<div class="row"><div class="tx"><div class="t1"><span class="k" style="color:${color}">${this._esc(key)}</span>${this._esc(it.title)} ${it.done ? '✓' : ''}</div><div class="t2">${this._esc(kinds)}</div></div></div>`;
            }
        });
        listEl.innerHTML = html || '<div class="hint">nada nos últimos dias</div>';
    }
}

// Exporta só o que é testável fora do navegador (helpers puros + a classe, cujo
// _teamsMatch não toca no DOM). No navegador este bloco não roda.
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        wiSquash, wiTokens, wiFold, wiWhen, wiSince, wiDayKey, wiDayHeader, wiDaysAgo,
        wiRecentPeople, wiShortNames, wiPersonRows, wiCapNameOnly, wiSnippet,
        wiTeamsExternalKey, wiFindExistingItem, wiExistingAction, WorkItemsUI,
    };
}
