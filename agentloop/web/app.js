'use strict';
/* agent-loop web UI: vanilla JS, hash routing, live updates over SSE. */

// Match SAMPLE_S and GPU_STALE_S in infra.py
const INFRA_SAMPLE_S = 2, GPU_STALE_S = 25;
const S = { workspaces: [], route: {}, goal: null, feeds: {}, infra: null, history: [], agents: null, models: {},
  chat: { sid: null, draft: null, messages: [], busy: false, liveEvs: [], creating: false }, edit: {}, cfgDirty: false,
  partial: {}, modal: null, open: {}, more: {}, chatOut: false };
const main = document.getElementById('main');
const AGENTS = ['claude', 'codex', 'agy'];
const NO_AUTOCOMPACT = ['agy'];  // CLIs without an auto-compact setting
const FEED_TYPES = ['turn', 'text', 'tool', 'tool_result', 'thinking', 'turn_end', 'error'];

// ---------- helpers ----------
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const mdHtml = s => (window.marked && window.DOMPurify) ? DOMPurify.sanitize(marked.parse(s || '')) : `<pre>${esc(s)}</pre>`;
// Markdown also typesets math ($…$, $$…$$, \(…\), \[…\]). It is set aside before markdown parsing, which would eat
// its underscores and backslashes, and KaTeX output goes in after sanitizing; code spans and blocks stay as written
const MATH_RE = /(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`)|\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|\$(?=[^\s$])([^$\n]*?[^\s$\\])\$(?!\d)/g;
function md(s) {
  if (!window.katex) return mdHtml(s);
  const math = [];
  const text = String(s || '').replace(MATH_RE, (m, code, dd, db, ip, id) => {
    if (code) return m;
    math.push({ tex: dd ?? db ?? ip ?? id, display: dd !== undefined || db !== undefined });
    return `KATEX${math.length - 1}X`;
  });
  return mdHtml(text).replace(/KATEX(\d+)X/g, (m, i) => math[i]
    ? katex.renderToString(math[i].tex, { displayMode: math[i].display, throwOnError: false, strict: false, output: 'html' }) : m);
}
const fmtTok = n => !n ? '0' : n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? Math.round(n / 1e3) + 'k' : String(n);
const fmtTime = ts => ts ? new Date(ts * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }) : '–';
const hhmm = ts => new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
const fmtDur = hours => { const m = Math.round(hours * 60); return `${Math.floor(m / 60)} h ${m % 60} mins`; };
const label = s => String(s || '').replace(/_/g, ' ');
const firstLine = s => (String(s || '').split('\n').find(l => l.trim()) || '').slice(0, 220);
// Workers and meta sessions are shown with three states only
const simpleStatus = s => (s.live || s.status === 'running') ? 'running' : s.status === 'failed' ? 'failed'
  : s.kind === 'worker' && s.status === 'retired' ? 'retired' : 'idle';
let refreshTimer = null;

async function api(path, opts = {}) {
  const init = { method: opts.method || (opts.body !== undefined ? 'POST' : 'GET'), headers: { 'Content-Type': 'application/json' } };
  if (opts.body !== undefined) init.body = JSON.stringify(opts.body);
  const r = await fetch(path, init);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || data.detail || r.statusText);
  return data;
}
function toast(msg, kind = 'info', ms = 4500) {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), ms);
}
// A pop-up that shows an action running; the returned function swaps in its outcome and closes it 3 s later
function busyToast(msg) {
  const el = document.createElement('div');
  el.className = 'toast';
  el.innerHTML = `<span class="spin">|</span> ${esc(msg)}`;
  $('#toasts').appendChild(el);
  return (text, kind) => { el.className = `toast ${kind}`; el.textContent = text; setTimeout(() => el.remove(), 3000); };
}
async function act(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg, 'ok'); return r; } catch (e) { toast(e.message, 'error', 8000); }
}
// size: false (form), true / 'wide' (wide form), 'full' (a session or chat window, nearly the whole screen)
function modal(html, size = false) {
  const root = $('#modal-root');
  root.innerHTML = `<div class="modal-back"><div class="modal ${size === true ? 'wide' : size || ''}">${html}</div></div>`;
  const back = $('.modal-back', root);
  back.addEventListener('mousedown', e => { if (e.target === back) closeModal(); });
  $$('[data-close]', root).forEach(b => b.onclick = closeModal);
  return $('.modal', root);
}
function closeModal() {
  // An expanded meta-chat goes back to its overview pane before the window is cleared
  if (S.chatOut) {
    const box = $('#ov-chat'), pop = $('#chat-pop');
    if (box && pop) [...pop.children].forEach(c => box.appendChild(c));
    S.chatOut = false;
    renderChatBar(); if ($('#chat-feed')) stick($('#chat-feed'));
  }
  $('#modal-root').innerHTML = ''; S.modal = null;
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });
function pill(status, text) { return `<span class="pill ${esc(status)}">${esc(label(text || status))}</span>`; }
// A goal's or agent's status; running gets a spinner instead of a separate "thinking" line.
// The delay ties the spinner's phase to the clock so repainting the header does not restart it.
const agentPill = st => st === 'running'
  ? `<span class="pill running"><span class="spin" style="animation-delay:-${Date.now() % 1000}ms">|</span> running</span>` : pill(st);
function meter(used, max) {
  if (!max) return '<div class="meter"><i style="width:0"></i></div>';
  const pct = Math.min(100, (used / max) * 100);
  // A limit turns amber/red as it fills
  const cls = pct >= 100 ? 'bad' : pct >= 80 ? 'warn' : '';
  return `<div class="meter ${cls}"><i style="width:${pct}%"></i></div>`;
}
const box = (title, body, attrs = '') => `<div class="box" ${attrs}><div class="lt">${title}</div>${body}</div>`;
// A document box never grows past the window: its text scrolls inside, and expand opens it whole in a window
const docBox = (key, title, body, cls = '', after = '') => `<div class="box doc-box ${cls}"><div class="lt">${title}
  <button class="btn small" data-doc-expand="${esc(key)}">expand</button></div><div class="doc-scroll" data-scroll="${esc(key)}">${body}</div>${after}</div>`;
// A long list item shows its first lines until opened; `open` is its default (the newest item), the human's choice wins
const clamp = (key, body, open) => `<div class="clamp ${(S.more[key] ?? open) ? 'open' : ''}" data-clamp="${esc(key)}">
  <div class="clamp-body">${body}</div><button class="more" data-more="${esc(key)}"></button></div>`;
// Only items taller than the clamp get the toggle; measured folded, so an opened item keeps its button
function fitClamps(root) {
  $$('[data-clamp]', root).forEach(c => {
    const open = c.classList.contains('open'), body = $('.clamp-body', c);
    c.classList.remove('open');
    c.classList.toggle('long', body.scrollHeight > body.clientHeight + 2);
    c.classList.toggle('open', open);
    $('.more', c).textContent = open ? 'show less' : 'show more';
  });
}
// A refresh repaints a tab; readers stay where they were in every scrolled document or list
function keepScroll(paint) {
  const at = {};
  $$('[data-scroll]').forEach(e => { at[e.dataset.scroll] = e.scrollTop; });
  paint();
  $$('[data-scroll]').forEach(e => { if (at[e.dataset.scroll]) e.scrollTop = at[e.dataset.scroll]; });
}
document.addEventListener('click', e => {
  const more = e.target.closest('[data-more]'), exp = e.target.closest('[data-doc-expand]');
  const head = !e.target.closest('button') && e.target.closest('[data-fold-item] > .ai-head');
  if (head) {
    const item = head.parentElement, open = !item.classList.contains('open');
    S.more[item.dataset.foldItem] = open;
    item.classList.toggle('open', open);
    $('.fold', head).textContent = open ? '▾' : '▸';
  } else if (more) {
    const c = more.closest('[data-clamp]'), open = !c.classList.contains('open');
    S.more[more.dataset.more] = open;
    c.classList.toggle('open', open);
    more.textContent = open ? 'show less' : 'show more';
    fitTab();  // the panes below a folded or unfolded report take the height it leaves
  } else if (exp) {
    const key = exp.dataset.docExpand, body = $(`[data-scroll="${CSS.escape(key)}"]`);
    if (body) docModal(key, body.innerHTML);
  }
});
// The expanded window of spec.md and goal.md also edits them, as their boxes in the config tab do
const EDITABLE_DOCS = { 'spec.md': 'spec', 'goal.md': 'goal' };
function docModal(key, html) {
  const name = S.route.tab === 'config' && EDITABLE_DOCS[key];
  const m = modal(`<div class="modal-head"><h2>${esc(key)}</h2><span class="spacer"></span>
      ${name ? '<button class="btn small" id="dm-edit">edit</button>' : ''}<button class="icon-btn" data-close>×</button></div>
    <div class="doc-full">${html}</div>`, 'full');
  if (!name) return;
  $('#dm-edit', m).onclick = () => {
    $('#dm-edit', m).hidden = true;
    $('.doc-full', m).outerHTML = `<div class="doc-edit"><textarea class="editor wrap" id="dm-text" spellcheck="false">${esc(S.goal.files[key])}</textarea>
      <div class="row" style="margin-top:6px"><span class="spacer"></span><button class="btn small" id="dm-cancel">cancel</button>
        <button class="btn small primary" id="dm-save">save</button></div></div>`;
    $('#dm-text', m).focus();
    saveOnChange($('#dm-text', m), $('#dm-save', m), S.goal.files[key]);
    $('#dm-cancel', m).onclick = () => docModal(key, md(S.goal.files[key]));
    $('#dm-save', m).onclick = () => act(async () => {
      await saveDoc(name, $('#dm-text', m).value);
      docModal(key, md(S.goal.files[key]));
    }, `${key} saved`);
  };
}
// Save stays disabled until the text differs from the saved file
function saveOnChange(textarea, btn, saved) {
  btn.disabled = true;
  textarea.oninput = () => { btn.disabled = textarea.value === saved; };
}
async function saveDoc(name, text) {
  const { ws, gid } = S.route;
  await api(`/api/goals/${ws}/${gid}/files/${name}.md`, { method: 'PUT', body: { text } });
  S.edit[name] = false; await loadGoal(); renderConfigTab(true);
}

// ---------- YAML (small dumper for goal.yaml) ----------
function yamlScalar(v) {
  if (v === null || v === undefined || v === '') return 'null';
  if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  const s = String(v);
  return /^[\w./-]+$/.test(s) && !/^(true|false|null|yes|no|on|off|~|[\d.]+)$/i.test(s) ? s : JSON.stringify(s);
}
function toYaml(obj, ind = '') {
  const lines = Object.entries(obj).map(([k, v]) => {
    if (v && typeof v === 'object' && !Array.isArray(v))
      return Object.keys(v).length ? `${ind}${k}:\n${toYaml(v, ind + '  ')}` : `${ind}${k}: {}`;
    return `${ind}${k}: ${Array.isArray(v) ? JSON.stringify(v) : yamlScalar(v)}`;
  });
  return lines.join('\n') + (ind ? '' : '\n');
}

// ---------- model catalog (follows the installed CLI versions; Agent CLIs page refreshes it) ----------
const modelList = agent => S.models?.[agent]?.models || [];
function modelOptions(agent, current) {
  const list = modelList(agent);
  const opts = list.map(m => `<option value="${esc(m.id)}" ${m.id === current ? 'selected' : ''}>${esc(m.alias ? m.name : m.id)}</option>`);
  if (current && !list.some(m => m.id === current)) opts.unshift(`<option value="${esc(current)}" selected>${esc(current)} (not available)</option>`);
  return `<option value="" ${current ? '' : 'selected'}>CLI default</option>${opts.join('')}`;
}
function effortOptions(agent, model, current) {
  const m = modelList(agent).find(x => x.id === model);
  const list = m ? m.efforts : [...new Set(modelList(agent).flatMap(x => x.efforts))];
  const opts = list.map(e => `<option ${e === current ? 'selected' : ''}>${esc(e)}</option>`);
  if (current && !list.includes(current)) opts.unshift(`<option value="${esc(current)}" selected>${esc(current)} (unsupported)</option>`);
  return `<option value="" ${current ? '' : 'selected'}>default</option>${opts.join('')}`;
}
async function loadModels() { try { S.models = await api('/api/models'); } catch { S.models = {}; } }

// ---------- agent settings form (HUMAN layer, new goal, new meta-chat) ----------
// Empty autocompact = the CLI's own setting (for Claude Code: autoCompactWindow in ~/.claude/settings.json)
const autocompactHint = agent => NO_AUTOCOMPACT.includes(agent) ? 'n/a'
  : S.models?.[agent]?.autocompact ? `CLI setting: ${fmtTok(S.models[agent].autocompact)}` : 'CLI default';
function agentRow(key, conf = {}, opts = {}) {
  const agent = conf.agent || opts.agent || 'claude';
  const off = opts.toggle && conf.enabled === false;
  return `<div class="agent-row ${off ? 'off' : ''}" data-key="${esc(key)}" data-agent="${esc(agent)}">
    <div class="name">${opts.toggle ? `<label class="inline"><input type="checkbox" data-f="enabled" ${off ? '' : 'checked'}> ${esc(opts.title)}</label>` : esc(opts.title)}</div>
    ${opts.fixedAgent ? `<div>${esc(agent)}</div>` : `<select data-f="agent">${AGENTS.map(a => `<option ${a === agent ? 'selected' : ''}>${a}</option>`).join('')}</select>`}
    <select data-f="model">${modelOptions(agent, conf.model)}</select>
    <select data-f="effort">${effortOptions(agent, conf.model, conf.effort)}</select>
    ${opts.noAutocompact ? '<span></span>' : `<input type="text" data-f="autocompact" value="${esc(conf.autocompact ?? '')}" placeholder="${esc(autocompactHint(agent))}" ${NO_AUTOCOMPACT.includes(agent) ? 'disabled' : ''}>`}
  </div>`;
}
function agentHeader(label = '') {
  return `<div class="agent-row faint small"><span class="k">${label}</span><span>agent</span><span>model</span><span>effort</span><span>autocompact</span></div>`;
}
function wireAgentRows(root, onChange) {
  root.addEventListener('change', e => {
    const row = e.target.closest('.agent-row');
    if (row) {
      const f = e.target.dataset.f;
      if (f === 'agent') {
        row.dataset.agent = e.target.value;
        $('[data-f=model]', row).innerHTML = modelOptions(e.target.value, null);
        const ac = $('[data-f=autocompact]', row);
        if (ac) {
          ac.placeholder = autocompactHint(e.target.value);
          ac.disabled = NO_AUTOCOMPACT.includes(e.target.value);
          if (ac.disabled) ac.value = '';
        }
      }
      if (f === 'agent' || f === 'model') {
        const agent = row.dataset.agent, eff = $('[data-f=effort]', row);
        eff.innerHTML = effortOptions(agent, $('[data-f=model]', row).value, eff.value);
      }
      if (f === 'enabled') row.classList.toggle('off', !e.target.checked);
    }
    onChange?.();
  });
  root.addEventListener('input', () => onChange?.());
}
function readAgentRow(row) {
  const val = f => { const el = $(`[data-f=${f}]`, row); const v = el ? el.value.trim() : ''; return v === '' ? null : v; };
  const out = { agent: row.dataset.agent, model: val('model'), effort: val('effort') };
  if ($('[data-f=autocompact]', row)) out.autocompact = val('autocompact');
  if ($('[data-f=enabled]', row)) out.enabled = $('[data-f=enabled]', row).checked;
  return out;
}
function configForm(conf, footer = '') {
  const b = conf.budget || {}, mw = conf.meta_work || {}, wk = conf.workers || {};
  const num = (k, v, title, tip) => `<label class="f" title="${tip}">${title}<input type="number" min="0" step="1" data-b="${k}" value="${v ?? ''}" placeholder="∞"></label>`;
  const lang = conf.language || 'English';
  // Agents on the left, the short settings beside them; long explanations sit in tooltips so the form stays short
  return `<div class="cfg">
    <div class="cfg-agents">${agentHeader('agents')}
      ${agentRow('meta_work', mw, { title: 'meta-work' })}
      ${agentRow('meta_advisor', conf.meta_advisor || {}, { title: 'meta-advisor' })}
      ${AGENTS.map(a => agentRow(`worker:${a}`, { agent: a, ...(wk[a] || {}) }, { title: 'worker', toggle: true, fixedAgent: true })).join('')}
      <div class="hint">The meta-work picks <code>--agent ${AGENTS.join('|')}</code> per worker; model and effort are always taken from here.</div></div>
    <div class="cfg-side">
      <div class="k" title="start a fresh meta-work session when its context exceeds this many tokens">rotate at</div>
      <input type="text" data-rotate value="${esc(mw.rotate_at_tokens ?? '')}" placeholder="off (e.g. 600k)" title="start a fresh meta-work session when its context exceeds this many tokens">
      <div class="k">budget</div><div class="budget-row">${num('max_concurrent_workers', b.max_concurrent_workers, 'workers', 'concurrent workers')}
        ${num('max_worker_turns', b.max_worker_turns, 'worker turns', 'worker turns in total')}${num('max_meta_turns', b.max_meta_turns, 'meta turns', 'meta-work + advisor turns in total')}</div>
      <div class="k">language</div><select data-lang title="for plan, report, advice and chat replies to you; meta-work and workers talk to each other in English">${['English', 'Vietnamese'].concat(
        ['English', 'Vietnamese'].includes(lang) ? [] : [lang]).map(l => `<option ${l === lang ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>
      <div class="k">base branch</div>
      <input type="text" data-base value="${esc(conf.base_branch ?? '')}" placeholder="repo/'s current branch" title="the integration branch starts from it and the PR opened by push &amp; create PR targets it; empty = the branch repo/ has checked out when the goal starts">
      <div></div><div class="row cfg-foot"><span class="hint">hover a field for details</span><span class="spacer"></span>${footer}</div>
    </div>
  </div>`;
}
function readConfigForm(root) {
  const rows = $$('.agent-row[data-key]', root);
  const metaWork = readAgentRow(rows.find(r => r.dataset.key === 'meta_work'));
  metaWork.rotate_at_tokens = $('[data-rotate]', root).value.trim() || null;
  const workers = {};
  rows.filter(r => r.dataset.key.startsWith('worker:')).forEach(r => {
    const { agent, ...conf } = readAgentRow(r);
    workers[agent] = conf;
  });
  if (!Object.values(workers).some(w => w.enabled)) throw new Error('enable at least one worker agent');
  const budget = {};
  $$('[data-b]', root).forEach(i => { budget[i.dataset.b] = i.value.trim() === '' ? null : Number(i.value); });
  return { meta_work: metaWork, meta_advisor: readAgentRow(rows.find(r => r.dataset.key === 'meta_advisor')),
    workers, budget, language: $('[data-lang]', root).value,
    base_branch: $('[data-base]', root).value.trim() || null };
}

// ---------- sidebar & routing ----------
async function loadState() {
  S.workspaces = (await api('/api/state')).workspaces;
  renderSidebar();
}
const folded = (() => { try { return JSON.parse(localStorage.getItem('agentloop.folded') || '{}'); } catch { return {}; } })();
function toggleWs(name) {
  folded[name] = !folded[name];
  try { localStorage.setItem('agentloop.folded', JSON.stringify(folded)); } catch { /* storage blocked: fold for this page only */ }
  renderSidebar();
}
function renderSidebar() {
  const r = S.route;
  $('#ws-list').innerHTML = S.workspaces.map(w => `<div>
      <div class="ws-name ${r.view === 'workspace' && r.ws === w.name ? 'active' : ''}">
        <button class="icon-btn fold" data-fold="${esc(w.name)}" title="${folded[w.name] ? 'Show' : 'Hide'} goals">${folded[w.name] ? '▸' : '▾'}</button>
        <a class="ws-link" href="#/ws/${w.name}">${esc(w.name)}</a>${folded[w.name] ? `<span class="faint small">${w.goals.length}</span>` : ''}
        <button class="icon-btn" data-new-goal="${esc(w.name)}" title="New goal">+</button></div>
      ${folded[w.name] ? '' : w.goals.map(m => `<a class="m-link ${r.gid === m.id && r.ws === w.name ? 'active' : ''}" href="#/ws/${w.name}/g/${m.id}" title="${esc(m.title)}">
        <span class="dot ${m.status}"></span><span class="title">${esc(m.title)}</span></a>`).join('') || '<div class="m-link faint">no goals yet</div>'}
    </div>`).join('') || '<div class="empty small">No workspace yet</div>';
  $$('[data-new-goal]').forEach(b => b.onclick = e => { e.preventDefault(); e.stopPropagation(); newGoalModal(b.dataset.newGoal); });
  $$('[data-fold]').forEach(b => b.onclick = () => toggleWs(b.dataset.fold));
  $('#nav-infra').classList.toggle('active', r.view === 'infra');
  $('#nav-agents').classList.toggle('active', r.view === 'agents');
}
$('#new-ws').onclick = () => newWorkspaceModal();
// The sidebar folds to a thin rail; the choice is remembered per browser
function setSidebar(off, save = true) {
  $('.app').classList.toggle('side-off', off);
  $('#side-toggle').textContent = off ? '»' : '«';
  $('#side-toggle').title = off ? 'Show sidebar' : 'Hide sidebar';
  if (!save) return;
  try { localStorage.setItem('agentloop.sideOff', off ? '1' : ''); } catch { /* storage blocked: this page only */ }
  window.dispatchEvent(new Event('resize'));  // charts and the overview height follow the new width
}
$('#side-toggle').onclick = () => setSidebar(!$('.app').classList.contains('side-off'));
setSidebar((() => { try { return localStorage.getItem('agentloop.sideOff') === '1'; } catch { return false; } })(), false);

const TABS = [['overview', 'OVERVIEW'], ['config', 'CONFIG'], ['plan', 'PLAN & ADVICE'], ['notes', 'HUMAN REPORT & NOTES'], ['git', 'GIT']];
function parseHash() {
  const p = location.hash.replace(/^#\/?/, '').split('/').map(decodeURIComponent);
  if (p[0] === 'ws' && p[2] === 'g') {
    const tab = TABS.some(([k]) => k === p[4]) ? p[4] : 'overview';
    return { view: 'goal', ws: p[1], gid: p[3], tab };
  }
  if (p[0] === 'ws' && p[1]) return { view: 'workspace', ws: p[1] };
  if (p[0] === 'infra') return { view: 'infra' };
  if (p[0] === 'agents') return { view: 'agents' };
  return { view: 'home' };
}
async function render() {
  const prev = S.route;
  S.route = parseHash();
  if (prev.gid !== S.route.gid) {
    S.feeds = {}; S.edit = {}; S.cfgDirty = false;
    S.chat = { sid: null, draft: null, messages: [], busy: false, liveEvs: [], creating: false };
  }
  renderSidebar();
  try {
    if (S.route.view === 'goal') await renderGoal();
    else if (S.route.view === 'workspace') await renderWorkspace();
    else if (S.route.view === 'infra') renderInfra();
    else if (S.route.view === 'agents') await renderAgents();
    else renderHome();
  } catch (e) {
    main.innerHTML = `<div class="page"><div class="empty">${esc(e.message)}</div></div>`;
  }
}
window.addEventListener('hashchange', () => { closeModal(); render(); });

// ---------- home & workspace ----------
function renderHome() {
  main.innerHTML = `<div class="hero">
    <h1>$ human on the loop</h1>
    <p class="muted" style="max-width:760px">Write a spec and a goal. A meta-work agent plans, delegates to Claude Code / Codex / agy
      workers, evaluates evidence and iterates until the goal is met. You steer with notes and meta-chat, review the report, and merge.</p>
    <div class="row" style="margin-top:14px"><button class="btn primary" id="h-new-ws">new workspace</button></div>
    <div class="ws-cards">${S.workspaces.map(w => {
      const counts = w.goals.reduce((a, m) => (a[m.status] = (a[m.status] || 0) + 1, a), {});
      return `<a class="card" href="#/ws/${w.name}"><h2>${esc(w.name)}</h2>
        <div class="row" style="margin-top:6px">${Object.entries(counts).map(([k, v]) => pill(k, `${v} ${label(k)}`)).join('') || '<span class="faint small">no goals</span>'}</div></a>`;
    }).join('')}</div></div>`;
  $('#h-new-ws').onclick = () => newWorkspaceModal();
}
async function renderWorkspace() {
  const ws = S.route.ws;
  const [info, state] = [await api(`/api/workspaces/${ws}`), S.workspaces.find(w => w.name === ws)];
  main.innerHTML = `<div class="page">
    <div class="page-head"><div><div class="crumbs">workspace</div><h1>${esc(ws)}</h1><div class="faint small">${esc(info.path)}</div></div>
      <div class="actions"><button class="btn primary" id="w-new">new goal</button>
        <button class="btn small del" id="w-del" title="Delete this workspace">delete</button></div></div>
    <div class="grid cols-2" style="margin-top:18px">
      ${box('goals', state && state.goals.length ? `<table class="t"><tr><th>goal</th><th>status</th></tr>
        ${state.goals.map(m => `<tr><td><a href="#/ws/${ws}/g/${m.id}">${esc(m.title)}</a><div class="faint small">${esc(m.id)}</div></td>
        <td>${pill(m.status)} ${m.outcome ? pill(m.outcome) : ''}</td></tr>`).join('')}</table>` : '<div class="empty">No goals yet.</div>')}
      ${box('loop.yaml <span class="dirty" id="w-dirty"></span>', `<div class="row"><span class="hint">Workspace defaults; new goals copy <code>defaults</code>.</span><span class="spacer"></span>
        <button class="btn small primary" id="w-save">save</button></div>
        <textarea class="editor" id="w-yaml" spellcheck="false">${esc(info.loop_yaml)}</textarea>`)}
    </div>
    <div style="margin-top:22px">${box('harness <span class="dirty" id="h-dirty"></span>', `<div class="row">
        <span class="hint">Instructions, skills, hooks and memory for <code>meta/</code> and <code>worker/</code>. Changes apply to the next turn, committed as <span class="chip human">human</span>.</span>
        <span class="spacer"></span><input type="text" id="h-newpath" placeholder="new file, e.g. worker/skills/kaggle-submit/SKILL.md" style="max-width:420px">
        <button class="btn small" id="h-new">create</button></div>
      <div class="grid" style="grid-template-columns:minmax(220px,320px) 1fr;margin-top:8px"><div id="h-files" class="file-list"></div>
        <div><div class="row"><b id="h-path">select a file</b><span class="spacer"></span>
          <button class="btn small primary" id="h-save" disabled>save</button></div>
          <textarea class="editor" id="h-text" spellcheck="false" style="margin-top:6px" disabled></textarea></div></div>`)}</div>
    <div style="margin-top:22px">${box('audit log', auditTable(info.log))}</div></div>`;
  wireHarness(ws);
  $('#w-new').onclick = () => newGoalModal(ws);
  $('#w-del').onclick = () => deleteWorkspaceModal(ws);
  $('#w-yaml').oninput = () => $('#w-dirty').textContent = 'unsaved';
  $('#w-save').onclick = () => act(async () => {
    await api(`/api/workspaces/${ws}/config`, { method: 'PUT', body: { text: $('#w-yaml').value } });
    $('#w-dirty').textContent = '';
  }, 'loop.yaml saved');
}
async function wireHarness(ws, select) {
  const { files } = await api(`/api/workspaces/${ws}/harness`);
  const list = $('#h-files');
  list.innerHTML = files.map(f => `<div class="list-item small" data-hf="${esc(f)}">${esc(f)}</div>`).join('');
  const open = async path => {
    const r = await api(`/api/workspaces/${ws}/harness?path=${encodeURIComponent(path)}`);
    $$('[data-hf]', list).forEach(x => x.classList.toggle('active', x.dataset.hf === path));
    $('#h-path').textContent = path; $('#h-text').value = r.text; $('#h-text').disabled = false;
    $('#h-save').disabled = false; $('#h-dirty').textContent = '';
  };
  $$('[data-hf]', list).forEach(x => x.onclick = () => open(x.dataset.hf));
  $('#h-text').oninput = () => $('#h-dirty').textContent = 'unsaved';
  $('#h-save').onclick = () => act(async () => {
    await api(`/api/workspaces/${ws}/harness`, { method: 'PUT', body: { path: $('#h-path').textContent, text: $('#h-text').value } });
    $('#h-dirty').textContent = '';
  }, 'Harness file saved');
  $('#h-new').onclick = () => act(async () => {
    const path = $('#h-newpath').value.trim();
    if (!path) throw new Error('enter a path under meta/ or worker/');
    const text = path.endsWith('SKILL.md') ? `---\nname: ${path.split('/').slice(-2)[0]}\ndescription: When to use this skill\n---\n\nInstructions…\n` : '';
    await api(`/api/workspaces/${ws}/harness`, { method: 'PUT', body: { path, text } });
    $('#h-newpath').value = ''; await wireHarness(ws, path);
  }, 'File created');
  if (select) open(select);
}
function auditTable(rows) {
  const chip = a => { const k = a.startsWith('meta-work') ? 'meta-work' : a.startsWith('meta-chat') ? 'meta-chat' : a; return `<span class="chip ${esc(k)}">${esc(a)}</span>`; };
  return rows.length ? `<table class="t"><tr><th>commit</th><th>actor</th><th>message</th><th>when</th></tr>${rows.map(c =>
    `<tr><td>${esc(c.hash)}</td><td>${chip(c.author)}</td><td>${esc(c.subject)}</td><td class="faint small">${esc(c.when)}</td></tr>`).join('')}</table>`
    : '<div class="empty">No commits.</div>';
}
function newWorkspaceModal() {
  const m = modal(`<div class="modal-head"><h2>new workspace</h2><button class="icon-btn" data-close>×</button></div>
    <div class="grid" style="gap:10px">
      <label class="f">name (lowercase, e.g. kaggle-titanic)<input type="text" id="nw-name" placeholder="my-project"></label>
      <label class="f">project repository to clone (git URL or local path); empty = fresh repo<input type="text" id="nw-repo" placeholder="git@github.com:org/repo.git or /path/to/repo"></label>
      <label class="f">base branch (optional)<input type="text" id="nw-base" placeholder="repo default"></label>
      <div class="row"><span class="spacer"></span><button class="btn" data-close>cancel</button><button class="btn primary" id="nw-ok">create</button></div></div>`);
  $('#nw-name', m).focus();
  $('#nw-ok', m).onclick = () => act(async () => {
    $('#nw-ok', m).disabled = true;
    try {
      const r = await api('/api/workspaces', { body: { name: $('#nw-name', m).value.trim(), repo: $('#nw-repo', m).value.trim(), base_branch: $('#nw-base', m).value.trim() } });
      closeModal(); await loadState(); location.hash = `#/ws/${r.name}`;
    } finally { if ($('#nw-ok', m)) $('#nw-ok', m).disabled = false; }
  }, 'Workspace created');
}
function deleteWorkspaceModal(ws) {
  const m = modal(`<div class="modal-head"><h2>delete workspace</h2><button class="icon-btn" data-close>×</button></div>
    <p class="muted">Stops every session and deletes <code>workspaces/${esc(ws)}/</code> for good: all goals, the project repo
      in <code>repo/</code> with any branch not pushed elsewhere, <code>data/</code> and the workspace history.</p>
    <label class="f">type ${esc(ws)} to confirm<input type="text" id="dw-name" autocomplete="off"></label>
    <div class="row" style="margin-top:10px"><span class="spacer"></span><button class="btn" data-close>cancel</button>
      <button class="btn danger" id="dw-ok" disabled>delete workspace</button></div>`);
  $('#dw-name', m).focus();
  $('#dw-name', m).oninput = e => { $('#dw-ok', m).disabled = e.target.value.trim() !== ws; };
  $('#dw-ok', m).onclick = () => act(async () => {
    $('#dw-ok', m).disabled = true;
    await api(`/api/workspaces/${ws}`, { method: 'DELETE' });
    closeModal(); await loadState(); location.hash = '#/';
  }, `Workspace ${ws} deleted`);
}
async function newGoalModal(ws) {
  const info = await api(`/api/workspaces/${ws}`);
  const m = modal(`<div class="modal-head"><h2>new goal · ${esc(ws)}</h2><button class="icon-btn" data-close>×</button></div>
    <div class="grid" style="gap:12px">
      <label class="f">title<input type="text" id="nm-title" placeholder="e.g. Add OAuth login"></label>
      <div class="grid cols-2">
        <label class="f">spec: context, requirements, inputs, constraints<textarea id="nm-spec" style="min-height:190px" placeholder="empty = reuse the latest goal's spec; with none, the meta-work explores the repo"></textarea></label>
        <label class="f">goal<textarea id="nm-goal" style="min-height:190px" placeholder="- criterion (qualitative ones are judged by the meta-work)&#10;- verify: \`pytest -q\`   (optional command run on the integration branch)"></textarea></label>
      </div>
      <div id="nm-conf">${configForm(info.config.defaults)}</div>
      <div class="row"><label class="inline"><input type="checkbox" id="nm-start" checked> start right away</label><span class="spacer"></span>
        <button class="btn" data-close>cancel</button><button class="btn primary" id="nm-ok">create goal</button></div></div>`, true);
  wireAgentRows($('#nm-conf', m));
  $('#nm-title', m).focus();
  $('#nm-ok', m).onclick = () => act(async () => {
    const config = readConfigForm($('#nm-conf', m));
    const r = await api(`/api/workspaces/${ws}/goals`, { body: { title: $('#nm-title', m).value, spec: $('#nm-spec', m).value, goal: $('#nm-goal', m).value, config } });
    if ($('#nm-start', m).checked) await api(`/api/goals/${ws}/${r.id}/start`, { body: {} });
    closeModal(); await loadState(); location.hash = `#/ws/${ws}/g/${r.id}`;
  }, 'Goal created');
}

// ---------- goal page ----------
async function loadGoal() {
  const { ws, gid } = S.route;
  S.goal = await api(`/api/goals/${ws}/${gid}`);
}
async function renderGoal() {
  await loadGoal();
  main.innerHTML = `<div class="page"><div id="m-head"></div><div class="tabs" id="m-tabs"></div><div id="m-tab"></div></div>`;
  renderGoalHead(); renderTabs(); await renderTab();
}
function renderGoalHead() {
  const M = S.goal, st = M.row.status, { ws, gid } = S.route;
  const buttons = [];
  if (['draft', 'stopped', 'paused'].includes(st)) buttons.push(`<button class="btn primary" data-act="start">${st === 'draft' ? 'start goal' : 'resume'}</button>`);
  if (st === 'running') buttons.push('<button class="btn small del" data-act="stop">stop</button>');
  buttons.push('<button class="btn small del" data-act="delete" title="Delete this goal with its sessions">delete</button>');
  $('#m-head').innerHTML = `<div class="page-head"><div class="row goal-title" title="${esc(gid)}">
      <a href="#/ws/${ws}">${esc(ws)}</a><span class="faint">/</span><span class="gt">${esc(M.config.title)}</span>
      ${agentPill(st)} ${M.row.outcome ? pill(M.row.outcome, 'outcome: ' + M.row.outcome) : ''}</div>
    <div class="actions">${buttons.join('')}</div></div>`;
  $$('[data-act]', $('#m-head')).forEach(b => b.onclick = () => goalAction(b.dataset.act));
}
// Review actions sit at the right end of the tabs row; once the goal is accepted, push lights up when repo/ has a
// remote and merge into base when it has none
function reviewButtons() {
  const M = S.goal, st = M.row.status, canPush = st === 'done' && M.remote;
  const canMerge = st === 'done' && !M.remote && !M.merged, branch = `loop/${S.route.gid}/integration`;
  const why = st !== 'done' ? 'accept the goal first' : !M.remote ? "repo/ has no remote 'origin'"
    : `push ${branch} to origin and open a PR into ${M.base}` + (M.row.pushed ? ` (last pushed ${fmtTime(M.row.pushed)})` : '');
  const whyMerge = st !== 'done' ? 'accept the goal first' : M.remote ? "repo/ has a remote 'origin'; use push & create PR"
    : M.merged ? `${M.base} already holds ${branch}` : `merge ${branch} into ${M.base} in repo/`;
  return (st === 'awaiting_review' ? '<button class="btn small" data-act="reopen">reopen with note</button><button class="btn small primary" data-act="accept">accept</button>' : '')
    + (M.row.pr ? `<a class="btn small" href="${esc(M.row.pr)}" target="_blank" rel="noopener">PR ↗</a>` : '')
    + `<button class="btn small ${canMerge ? 'primary' : ''}" data-act="merge" ${canMerge ? '' : 'disabled'} title="${esc(whyMerge)}">merge into base</button>`
    + `<button class="btn small ${canPush && !M.row.pushed ? 'primary' : ''}" data-act="push" ${canPush ? '' : 'disabled'} title="${esc(why)}">push &amp; create PR</button>`;
}
async function goalAction(action) {
  const { ws, gid } = S.route;
  if (action === 'reopen') return reopenModal();
  if (action === 'delete') return deleteGoal();
  if (action === 'push') return pushGoal();
  if (action === 'merge') return mergeGoal();
  if (action === 'stop' && !window.confirm('Stop the goal? Running sessions are killed; their work is snapshotted.')) return;
  await act(async () => {
    const r = await api(`/api/goals/${ws}/${gid}/${action}`, { body: {} });
    if (action === 'accept' && r.tags?.length) toast(`Unmerged experiments kept as tags: ${r.tags.join(', ')}`, 'info', 9000);
    await loadState(); await refreshGoal(true);
  }, { start: 'Goal started', stop: 'Goal stopped', accept: 'Goal accepted' }[action]);
}
async function deleteGoal() {
  const { ws, gid } = S.route;
  if (!window.confirm(`Delete the goal "${S.goal.config.title}"? Its sessions are stopped, and its files, worktrees, `
    + 'branches and logs are deleted for good.')) return;
  $$('#m-head [data-act], #m-tabs [data-act]').forEach(b => b.disabled = true);
  await act(async () => {
    try { await api(`/api/goals/${ws}/${gid}`, { method: 'DELETE' }); }
    catch (e) { renderGoalHead(); renderTabs(); throw e; }
    await loadState(); location.hash = `#/ws/${ws}`;
  }, 'Goal deleted');
}
async function pushGoal() {
  const { ws, gid } = S.route;
  if (!window.confirm(`Push loop/${gid}/integration to origin and open a pull request into ${S.goal.base}? `
    + `${S.goal.base} itself is not touched.`)) return;
  const b = $('#m-tabs [data-act=push]');
  b.disabled = true; b.textContent = 'pushing…';
  await act(async () => {
    try {
      const r = await api(`/api/goals/${ws}/${gid}/push`, { body: {} });
      if (r.pr) toast(`Pushed ${r.branch}; PR into ${r.base}: ${r.pr}`, 'ok', 9000);
      else toast(`Pushed ${r.branch}; no PR opened: ${r.pr_error}`, 'warn', 12000);
    } finally { await refreshGoal(true); }
  });
}
async function mergeGoal() {
  const { ws, gid } = S.route;
  if (!window.confirm(`Merge loop/${gid}/integration into ${S.goal.base} in repo/? New goals then start from it.`)) return;
  await act(async () => {
    try {
      const r = await api(`/api/goals/${ws}/${gid}/merge`, { body: {} });
      toast(`${r.base} is now at ${r.commit.slice(0, 7)}`, 'ok');
    } finally { await refreshGoal(true); }
  });
}
function reopenModal(prefill = '') {
  const { ws, gid } = S.route;
  const m = modal(`<div class="modal-head"><h2>reopen goal</h2><button class="icon-btn" data-close>×</button></div>
    <p class="muted">Tell the meta-work what is missing or what to change. It receives this note immediately.</p>
    <textarea id="ro-text" style="min-height:140px">${esc(prefill)}</textarea>
    <div class="row" style="margin-top:10px"><span class="spacer"></span><button class="btn" data-close>cancel</button><button class="btn primary" id="ro-ok">reopen</button></div>`);
  $('#ro-ok', m).onclick = () => act(async () => {
    await api(`/api/goals/${ws}/${gid}/reopen`, { body: { text: $('#ro-text', m).value } });
    closeModal(); await loadState(); await refreshGoal(true);
  }, 'Goal reopened');
}
function renderTabs() {
  const M = S.goal, { ws, gid, tab } = S.route;
  const counts = { notes: M.notes.filter(n => n.status === 'pending').length || '' };
  $('#m-tabs').innerHTML = TABS.map(([k, t]) => `<a class="tab ${tab === k ? 'active' : ''}" href="#/ws/${ws}/g/${gid}/${k}">${t}${counts[k] ? `<span class="count">${counts[k]}</span>` : ''}</a>`).join('')
    + `<div class="actions tab-actions">${reviewButtons()}</div>`;
  $$('[data-act]', $('#m-tabs')).forEach(b => b.onclick = () => goalAction(b.dataset.act));
}
async function renderTab() {
  const t = S.route.tab;
  if (t === 'overview') renderOverview();
  else if (t === 'config') { $('#m-tab').innerHTML = '<div id="cfg-tab" style="margin-top:18px"></div>'; renderConfigTab(true); }
  else if (t === 'plan') {
    // Refreshes come with every session event; repaint only when the plan or the advice changed
    const html = planHtml();
    if ($('#m-tab .plan-grid') && S.planHtml === html) return;
    S.planHtml = html;
    keepScroll(() => { $('#m-tab').innerHTML = html; });
    fitTab();
    $$('#m-tab [data-log]').forEach(b => b.onclick = () => sessionModal(b.dataset.log));
  }
  else if (t === 'notes') renderNotes();
  else if (t === 'git') await renderGit();
}
function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => refreshGoal(false), 350);
}
async function refreshGoal(force) {
  if (S.route.view !== 'goal') return;
  try { await loadGoal(); } catch { return; }
  renderGoalHead(); renderTabs();
  const t = S.route.tab;
  if (t === 'overview') return updateOverview(force);
  if (t === 'config') return renderConfigTab(force);
  if (t === 'notes') return renderNotes(true);
  if (t === 'git' && !force) return;
  await renderTab();
}

// ---------- overview: stats + infra, then META-AGENTS (left) and WORKER-AGENTS (right) ----------
function renderOverview() {
  if (S.chatOut) closeModal();
  $('#m-tab').innerHTML = `<div class="strip" id="ov-stats"></div>
    <div class="ov-main" id="ov-main">
      <div class="layer col"><div class="lt">meta-agents</div>
        <div class="box mw" id="ov-mw"></div><div class="box chat" id="ov-chat"></div></div>
      <div class="layer col"><div class="lt">worker-agents <span class="x faint" id="ov-wcount"></span></div>
        <div id="ov-workers" class="wlist"></div></div></div>`;
  renderStats(); renderMetaWork(); renderChat(); renderWorkers();
  fitTab();
}
// The overview, and the panes of the config, plan and notes tabs, fill the window; each pane scrolls on its own
function fitTab() {
  const el = $('#ov-main') || $('#m-tab .fit');
  if (el) el.style.height = window.innerWidth <= 1000 ? '' : Math.max(380, window.innerHeight - el.getBoundingClientRect().top - 17) + 'px';
}
function updateOverview(force) {
  if (!$('#ov-stats')) return renderOverview();
  renderStats(); renderMetaWorkHead(); renderChatBar(); renderWorkers();
  // A rotated meta-work session joins the same feed
  const fresh = S.goal.sessions.filter(s => s.kind === 'meta-work' && !S.feeds[s.id]);
  if (fresh.length) Promise.all(fresh.map(s => loadFeed(s.id))).then(() => paintFeed('meta-work'));
  liveKeys().forEach(paintLive);
}
// One status line instead of five cards: the feeds below get the height
function renderStats() {
  const M = S.goal, b = M.budget, c = M.checklist;
  const workers = M.sessions.filter(s => s.kind === 'worker');
  const running = workers.filter(s => simpleStatus(s) === 'running').length;
  const lim = v => v || '∞';
  const seg = (k, v, extra = '', title = '') => `<div class="seg" title="${esc(title)}"><span class="k">${k}</span><span class="v">${v}</span>${extra}</div>`;
  $('#ov-stats').innerHTML =
    seg('plan', `${c.done}/${c.total}`, c.total ? planBar(c) : '<span class="s">no checklist</span>', 'checklist items in plan.md checked by the meta-work')
    + seg('workers', `${running} running`, '', `${workers.length} total, at most ${lim(b.max_concurrent_workers)} at once`)
    + seg('turns', `worker ${b.worker_turns}/${lim(b.max_worker_turns)} · meta ${b.meta_turns}/${lim(b.max_meta_turns)}`)
    + seg('time', fmtDur(b.hours) + (b.from ? ` · ${fmtTime(b.from)} → ${b.to ? fmtTime(b.to) : 'now'}` : ''), '', 'active time of the goal')
    + seg('total tokens', fmtTok(M.row.tokens), '', 'tokens used by the meta-work, advisor and worker turns (meta-chats excluded)');
}
// One segment per checklist item: done items green, open ones grey
function planBar(c) {
  const cells = [...Array(c.total)].map((_, i) => `<i class="${i < c.done ? 'done' : ''}"></i>`).join('');
  return `<span class="planbar" style="width:${Math.min(c.total * 12, 160)}px">${cells}</span>`;
}
// Sidebar INFRA: the host's last two minutes, redrawn with every 2 s sample; click for the infra page
function renderSideInfra() {
  const s = S.infra;
  if (!s) return;
  const gpu = s.gpus[0], stale = gpuStale(s);
  $('#si-ts').textContent = hhmm(s.ts);
  $('#si-vals').innerHTML = `<span class="c-cpu">cpu ${s.cpu_pct.toFixed(0)}%</span><span class="c-ram">ram ${(s.ram_total_gb - s.ram_free_gb).toFixed(1)}/${s.ram_total_gb}G</span>`
    + (gpu ? `<span class="c-gpu">vram ${gpu.mem_used_gb}/${gpu.mem_total_gb}G · util ${gpu.util.toFixed(0)}%${stale ? ` · ${stale}` : ''}</span>` : '');
  // Jobs running while their session has no turn (detached) are not in any session's memory
  const inTurn = new Set((s.sessions || []).map(x => x.id)), alone = (s.jobs || []).filter(j => !inTurn.has(j.sid));
  $('#nav-infra').title = `${s.sessions?.length || 0} agent sessions using ${(s.sessions || []).reduce((a, x) => a + x.rss_gb, 0).toFixed(1)} GB`
    + (alone.length ? ` · ${alone.length} jobs outside a turn using ${alone.reduce((a, j) => a + j.rss_gb, 0).toFixed(1)} GB` : '') + ' · open the infra page';
  drawChart($('#side-chart'), 120 / INFRA_SAMPLE_S, true);
}
function renderReport() {
  const M = S.goal, report = M.files['report.md'], el = $('#nt-report');
  if (!el) return;
  const review = M.row.status === 'awaiting_review';
  $('#nt-human').hidden = !report;
  if (!report) { el.innerHTML = ''; return; }
  if (el.dataset.report === report + review) return;
  el.dataset.report = report + review;
  // Open while it awaits review; after that it folds to its first lines
  el.innerHTML = docBox('report.md', `report${review ? ' · awaiting your review' : ''}`,
    clamp('report', `<div class="md doc">${md(report.replace(/^# Completion report\s*\n/, ''))}</div>`, review), 'report',
    review ? `<div class="row" style="margin-top:8px"><span class="spacer"></span><button class="btn" id="nt-reopen">reopen with note</button>
      <button class="btn primary" id="nt-accept">accept</button></div>` : '');
  fitClamps(el);
  if (review) { $('#nt-accept').onclick = () => goalAction('accept'); $('#nt-reopen').onclick = () => reopenModal(); }
}
function renderConfigTab(force) {
  const M = S.goal, el = $('#cfg-tab');
  if (!el) return;
  // Never overwrite what the human is typing, and skip repaints when nothing changed
  if (!force && (S.edit.spec || S.edit.goal || S.cfgDirty) && el.innerHTML) return;
  const key = JSON.stringify([M.files['spec.md'], M.files['goal.md'], M.config, S.edit, Object.keys(S.models)]);
  if (!force && el.dataset.key === key) return;
  el.dataset.key = key;
  const doc = (name, title, hint) => {
    const editing = S.edit[name];
    if (!editing) return docBox(`${name}.md`, `${title} <button class="btn small" data-edit="${name}">edit</button>`,
      `<div class="md doc">${md(M.files[name + '.md'])}</div>`);
    return box(title,
      `<textarea class="editor wrap" data-doc="${name}" spellcheck="false" style="min-height:220px">${esc(M.files[name + '.md'])}</textarea>
        <div class="row" style="margin-top:6px"><span class="hint">${hint}</span><span class="spacer"></span>
          <button class="btn small" data-cancel="${name}">cancel</button><button class="btn small primary" data-save-doc="${name}">save</button></div>`);
  };
  // The config form, edited most often, comes first; the documents scroll in their own boxes below it
  // Save sits in the form's bottom-right corner, so it takes no extra row; spec and goal fill the rest of the window
  const save = `<button class="btn small primary" id="cfg-save" disabled title="Changing the meta-work agent starts a fresh meta-work session (the plan carries over).">save config</button>`;
  keepScroll(() => { el.innerHTML = `${box(`agents · budget · git <span class="x">applies from the next turn</span><span class="dirty" id="cfg-dirty"></span>`,
      `<div id="ov-cfg">${configForm(M.config, save)}</div>`)}
    <div class="grid cols-2 fit" style="margin-top:18px">${doc('spec', 'spec', 'read-only for agents; a running meta-work gets the new text')}
      ${doc('goal', 'goal','lines like - verify: `pytest -q` are executed by the platform')}</div>`; });
  fitTab();
  S.cfgDirty = false;
  // Save is enabled only while the form differs from the saved config, so an edit undone disables it again
  // An invalid form (no worker enabled) counts as changed, and save reports why it is refused
  const formKey = () => { try { return JSON.stringify(readConfigForm($('#ov-cfg'))); } catch { return null; } };
  const saved = formKey();
  wireAgentRows($('#ov-cfg'), () => {
    const now = formKey();
    S.cfgDirty = now === null || now !== saved;
    $('#cfg-dirty').textContent = S.cfgDirty ? 'unsaved' : '';
    $('#cfg-save').disabled = !S.cfgDirty;
  });
  $('#cfg-save').onclick = () => act(async () => {
    const { ws, gid } = S.route;
    const created = (M.files['goal.yaml'].match(/^created:\s*(.+)$/m) || [])[1];
    const obj = { title: M.config.title, ...(created ? { created: created.replace(/^['"]|['"]$/g, '') } : {}), ...readConfigForm($('#ov-cfg')) };
    await api(`/api/goals/${ws}/${gid}/files/goal.yaml`, { method: 'PUT', body: { text: toYaml(obj) } });
    S.cfgDirty = false; await loadGoal(); renderConfigTab(true);
  }, 'Configuration saved');
  $$('[data-edit]', el).forEach(b => b.onclick = () => { S.edit[b.dataset.edit] = true; renderConfigTab(true); });
  $$('[data-cancel]', el).forEach(b => b.onclick = () => { S.edit[b.dataset.cancel] = false; renderConfigTab(true); });
  $$('[data-doc]', el).forEach(t => saveOnChange(t, $(`[data-save-doc="${t.dataset.doc}"]`, el), M.files[t.dataset.doc + '.md']));
  $$('[data-save-doc]', el).forEach(b => b.onclick = () => act(
    () => saveDoc(b.dataset.saveDoc, $(`[data-doc="${b.dataset.saveDoc}"]`, el).value), `${b.dataset.saveDoc}.md saved`));
}

// ---------- live feeds: TUI-like stream of prompts, replies, tool calls; follows the bottom ----------
const FEED_TAIL = 400;  // events kept per overview pane; the log window shows the whole session
const cliName = a => a === 'claude' ? 'claude code' : a;
const firstLines = (s, n) => String(s || '').split('\n').filter(l => l.trim()).slice(0, n).join('\n').slice(0, 400);
// A meta-work turn carrying worker replies ("[Worker <sid> · role <role> ...]") or advice ("[Advice] <sid> ...") is
// labelled by those senders; one of platform events alone keeps who.prompt. A worker turn started by the result of
// its `loop run` job (Engine.wake_for_jobs; older turns open with the job result itself) comes from the platform
function turnSender(e, who) {
  if (who.prompt === 'meta-work → worker' && /^(\[System\] The `loop run` job|\[Job j-)/.test(e.prompt || ''))
    return 'platform → worker (loop run)';
  if (who.prompt === '→ meta-advisor' && /^\[Consultation\]\n\[System\] The `loop run` job/.test(e.prompt || ''))
    return 'platform → meta-advisor (loop run)';
  if (who.prompt !== 'platform → meta-work') return who.prompt;
  const senders = [...new Set([...String(e.prompt || '').matchAll(/^\[(?:Worker \S+ · role ([^\]·]+?)\s*[·\]]|Advice\] )/gm)]
    .map(m => m[1] ? `worker ${m[1]}` : 'meta-advisor'))];
  return senders.length ? `${senders.join(', ')} → meta-work` : who.prompt;
}
function tuiEvent(e, who) {
  switch (e.type) {
    case 'turn': return `<div class="fr prompt"><div class="fl"><span>▶ ${esc(turnSender(e, who))}</span>
      <span class="spacer"></span><span class="faint">turn ${e.n} · ${hhmm(e.ts)}</span></div><div class="md">${md(e.prompt)}</div></div>`;
    case 'text': return `<div class="fr reply"><div class="fl">◀ ${esc(e.sub ? 'subagent' : who.reply)}</div><div class="md">${md(e.text)}</div></div>`;
    case 'tool': return `<div class="tl"><span class="dotc">⏺</span><b>${esc(e.name)}</b> ${esc(e.detail || '')}</div>`;
    case 'tool_result': return e.text ? `<div class="tres ${e.error ? 'err' : ''}">⎿ ${esc(firstLines(e.text, 3))}</div>` : '';
    case 'thinking': return `<div class="think">✻ ${esc(firstLine(e.text))}</div>`;
    case 'error': return /--dangerously-bypass-hook-trust/.test(e.text) ? '' : `<div class="fe err">! ${esc(e.text)}</div>`;
    case 'turn_end': return e.status === 'failed' ? `<div class="fe err">! turn ${e.n} failed${e.error ? ': ' + esc(e.error) : ''}</div>` : '';
    default: return '';
  }
}
function feedShell(key) {
  return `<div class="feed-wrap"><div class="feed" data-feed="${key}"><div class="fs"></div><div class="fp"></div></div>
    <button class="follow" hidden>▼ follow live</button></div>`;
}
async function loadFeed(sid) {
  if (!S.feeds[sid]) S.feeds[sid] = (await api(`/api/sessions/${sid}/events?limit=4000&types=${FEED_TYPES.join(',')}`)).events;
}
// A finished turn folds to its prompt, final reply and failure, as in the meta-chat; the running turn shows every
// step. `done` counts the folded turns: when it changes, already painted steps must go, so the pane repaints
function foldTurns(evs, sid) {
  const out = [];
  let start = 0, done = 0;
  const fold = end => {
    const seg = evs.slice(start, end), reply = seg.findLast(e => e.type === 'text' && !e.sub);
    out.push(...seg.filter(e => e.type === 'turn' || e.type === 'turn_end' || e === reply));
    start = end; done++;
  };
  evs.forEach((e, i) => {
    if (e.type === 'turn' && i > start) fold(i);  // the previous turn never logged its end
    else if (e.type === 'turn_end') fold(i + 1);
  });
  // A turn cut by a server restart stays open in the log; its session is marked interrupted
  if (start < evs.length && S.goal.sessions.find(x => x.id === sid)?.status === 'interrupted') fold(evs.length);
  else out.push(...evs.slice(start));
  return { evs: out, done };
}
// Which sessions a pane shows, and how prompts/replies are labelled; the log window folds finished turns like the panes
function feedSource(key) {
  if (key === 'modal') {
    const s = S.goal?.sessions.find(x => x.id === S.modal.sid) || {};
    return { ...foldTurns(S.modal.evs, S.modal.sid), sids: [S.modal.sid], sig: S.modal.sid, who: s.kind === 'worker'
      ? { prompt: 'meta-work → worker', reply: cliName(s.agent) }
      : { prompt: s.kind === 'meta-work' ? 'platform → meta-work' : `→ ${s.kind || 'session'}`, reply: s.kind || 'session' } };
  }
  if (key === 'meta-work') {
    const ids = S.goal.sessions.filter(s => s.kind === 'meta-work' && S.feeds[s.id]).map(s => s.id);
    const folded = ids.map(id => foldTurns(S.feeds[id], id));
    return { evs: folded.flatMap(f => f.evs), done: folded.reduce((n, f) => n + f.done, 0), sids: ids, sig: ids.join(),
      who: { prompt: 'platform → meta-work', reply: 'meta-work' } };
  }
  const s = S.goal.sessions.find(x => x.id === key);
  return { ...foldTurns(S.feeds[key] || [], key), sids: [key], sig: key, who: { prompt: 'meta-work → worker', reply: cliName(s?.agent) } };
}
const liveKeys = () => ['meta-work', ...S.goal.sessions.filter(s => s.kind === 'worker').map(s => s.id), ...(S.modal ? ['modal'] : [])];
const stick = el => { if (!el.dataset.free) el.scrollTop = el.scrollHeight; };
// Coalesce bursts of streamed events into one paint per frame
const queued = new Map();
function schedule(kind, key, fn) {
  const id = kind + key;
  if (queued.has(id)) return;
  queued.set(id, requestAnimationFrame(() => { queued.delete(id); fn(key); }));
}
const paintFeed = key => schedule('f', key, renderFeed);
const paintLive = key => schedule('l', key, renderLive);
function renderFeed(key) {
  const el = $(`[data-feed="${key}"]`);
  if (!el || (key === 'modal' && !S.modal)) return;
  const { evs, who, sig, done } = feedSource(key), fs = $('.fs', el);
  let from = Number(el.dataset.n || 0);
  // Append only what is new; repaint when the set of sessions changed or a turn folded
  if (el.dataset.sig !== sig || el.dataset.done !== String(done) || from > evs.length) {
    fs.innerHTML = ''; el.dataset.sig = sig; el.dataset.done = done; from = Math.max(0, evs.length - FEED_TAIL);
  }
  const html = evs.slice(from).map(e => tuiEvent(e, who)).join('');
  if (html) { $('.empty', fs)?.remove(); fs.insertAdjacentHTML('beforeend', html); }
  while (fs.childElementCount > FEED_TAIL) fs.firstElementChild.remove();
  if (!fs.childElementCount) fs.innerHTML = '<div class="empty">no activity yet</div>';
  el.dataset.n = evs.length;
  // The feed's last tool call blinks its dot while the session runs (see renderLive)
  $('.tl.last', fs)?.classList.remove('last');
  [...fs.querySelectorAll('.tl')].pop()?.classList.add('last');
  renderLive(key);
}
// The reply being typed right now (Claude streams tokens; Codex shows each step as it completes)
function renderLive(key) {
  const el = $(`[data-feed="${key}"]`);
  if (!el || (key === 'modal' && !S.modal)) return;
  const { sids, who } = feedSource(key);
  const text = sids.map(id => S.partial[id]).find(Boolean);
  const html = text ? `<div class="fr reply live"><div class="fl">◀ ${esc(who.reply)}</div><pre class="stream">${esc(text)}<span class="cursor"></span></pre></div>` : '';
  const fp = $('.fp', el);
  if (fp.innerHTML !== html) fp.innerHTML = html;
  el.classList.toggle('running', S.goal?.sessions.some(s => sids.includes(s.id) && (s.live || s.status === 'running')) ?? false);
  stick(el);
}
// Scrolling up pauses following; the button (or scrolling back down) resumes it
document.addEventListener('scroll', e => {
  const el = e.target;
  if (!el.classList?.contains('feed')) return;
  const free = el.scrollHeight - el.scrollTop - el.clientHeight > 40;
  el.dataset.free = free ? '1' : '';
  const btn = $('.follow', el.parentElement);
  if (btn) btn.hidden = !free;
}, true);
document.addEventListener('click', e => {
  const b = e.target.closest('.follow');
  if (!b) return;
  const el = $('.feed', b.parentElement);
  el.dataset.free = ''; b.hidden = true; el.scrollTop = el.scrollHeight;
});
function agentLine(s, tokens) {
  return `${esc(cliName(s.agent))} · ${esc(s.model || 'default')}/${esc(s.effort || 'default')}${tokens === undefined ? '' : ` · ${fmtTok(tokens)} tokens`}`;
}

// META-WORK: every meta-work session of the goal, oldest first, as one continuous feed
function renderMetaWork() {
  $('#ov-mw').innerHTML = `<div class="lt row">meta-work <span class="spacer"></span><button id="mw-advisor" class="btn small advisor-status"></button></div>
    <div id="mw-head" class="row small pane-head"></div>${feedShell('meta-work')}`;
  renderMetaWorkHead();
  const sessions = S.goal.sessions.filter(s => s.kind === 'meta-work');
  Promise.all(sessions.map(s => loadFeed(s.id))).then(() => paintFeed('meta-work'));
}
function renderMetaWorkHead() {
  const el = $('#mw-head');
  if (!el) return;
  renderAdvisorStatus();
  const sessions = S.goal.sessions.filter(s => s.kind === 'meta-work');
  const cur = sessions.find(s => s.id === S.goal.row.meta_work) || sessions[sessions.length - 1];
  if (!cur) { el.innerHTML = `<span class="muted">${agentLine({ agent: S.goal.config.meta_work.agent, ...S.goal.config.meta_work })}</span>`; return; }
  el.innerHTML = `<span class="muted">${agentLine(cur, cur.context)}</span>
    <span class="spacer"></span>${agentPill(simpleStatus(cur))}<button class="btn small" data-log="${cur.id}">expand</button>`;
  $('[data-log]', el).onclick = () => sessionModal(cur.id);
}

// Advice uses the existing session log window; the overview only adds one status button.
function renderAdvisorStatus() {
  const btn = $('#mw-advisor');
  if (!btn) return;
  const sessions = S.goal.sessions.filter(s => s.kind === 'meta-advisor');
  const running = sessions.some(s => simpleStatus(s) === 'running');
  const queued = sessions.some(s => s.status === 'queued');
  btn.className = `btn small advisor-status ${running ? 'active' : 'dormant'}`;
  btn.disabled = !sessions.length;
  btn.innerHTML = `ADVISOR ${running ? agentPill('running') : queued ? pill('pending', 'queued') : ''}`;
  btn.title = sessions.length ? `${sessions.length} consultation(s) · open advice` : 'No consultations yet';
  btn.onclick = () => {
    const current = sessions.find(s => simpleStatus(s) === 'running') || sessions[sessions.length - 1];
    if (current) sessionModal(current.id);
  };
}

// META-CHAT: chats bound to this goal; agent/model/effort chosen per chat. Folding it to its bar, like an idle
// worker, gives the meta-work the room; the choice is remembered in this browser
let chatFold = (() => { try { return localStorage.getItem('agentloop.chatFold') === '1'; } catch { return false; } })();
function toggleChat(fold = !chatFold) {
  chatFold = fold;
  try { localStorage.setItem('agentloop.chatFold', fold ? '1' : '0'); } catch { /* storage blocked: fold for this page only */ }
  $('#ov-chat')?.classList.toggle('folded', fold);
  renderChatBar();
  if (!fold && $('#chat-feed')) stick($('#chat-feed'));
}
function renderChat() {
  $('#ov-chat').classList.toggle('folded', chatFold);
  $('#ov-chat').innerHTML = `<div class="lt">meta-chat</div>
    <div class="chat-bar" id="chat-bar"></div><div id="chat-new"></div>
    <div class="feed-wrap"><div class="feed" id="chat-feed"></div><button class="follow" hidden>▼ follow live</button></div>
    <div class="composer"><textarea id="chat-input" placeholder="ask about progress or risks, or propose a change… (ctrl+enter)"></textarea>
      <button class="btn primary" id="chat-send">send</button></div>`;
  renderChatBar();
  $('#chat-send').onclick = sendChat;
  $('#chat-input').onkeydown = e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) sendChat(); };
  $('#chat-feed').onclick = e => {
    const b = e.target.closest('[data-save-note]');
    if (b) saveNoteModal(S.chat.messages[Number(b.dataset.saveNote)].text);
  };
  loadChatMessages();
}
function renderChatBar() {
  const bar = $('#chat-bar');
  if (!bar) return;
  const chats = S.goal.sessions.filter(s => s.kind === 'meta-chat');
  const draft = S.chat.draft;
  if (!S.chat.sid && !draft && chats.length) S.chat.sid = chats[chats.length - 1].id;
  const cur = chats.find(c => c.id === S.chat.sid);
  // The expanded window shows the whole chat, so only the overview pane folds
  const fold = S.chatOut ? '' : `<button class="icon-btn fold" id="chat-fold" title="${chatFold ? 'Show' : 'Hide'} the chat">${chatFold ? '▸' : '▾'}</button>`;
  const options = (draft ? `<option value="" ${S.chat.sid ? '' : 'selected'}>new chat · ${agentLine(draft)} · not sent yet</option>` : '')
    + chats.map((c, i) => `<option value="${c.id}" ${c.id === S.chat.sid ? 'selected' : ''}>
      chat ${i + 1} · ${agentLine(c, c.context)}</option>`).reverse().join('');
  bar.innerHTML = `${fold}<select id="chat-sel" ${options ? '' : 'disabled'}>${options || '<option>no chats yet</option>'}</select>
    ${cur ? agentPill(simpleStatus(cur)) : ''}<button class="btn small" id="chat-new-btn">+ new chat</button>
    ${cur || draft ? `<button class="btn small del" id="chat-del" title="${cur ? 'Delete this chat with its messages' : 'Discard this new chat'}">delete</button>` : ''}
    <button class="btn small" id="chat-expand">${S.chatOut ? 'collapse' : 'expand'}</button>`;
  $('#chat-expand').onclick = () => S.chatOut ? closeModal() : expandChat();
  bar.onclick = e => { if (e.target === bar || e.target.id === 'chat-fold') toggleChat(); };
  $('#chat-sel').onchange = e => { S.chat.sid = e.target.value || null; S.chat.liveEvs = []; loadChatMessages(); };
  if ($('#chat-del')) $('#chat-del').onclick = deleteChat;
  $('#chat-new-btn').onclick = () => { S.chat.creating = !S.chat.creating; if (chatFold) toggleChat(false); renderNewChat(); };
  renderNewChat();
}
function renderNewChat() {
  const el = $('#chat-new');
  if (!S.chat.creating) { el.innerHTML = ''; return; }
  if (el.innerHTML) return;
  el.innerHTML = `<div class="new-chat">${agentHeader()}${agentRow('meta_chat', S.goal.config.meta_work, { title: 'meta-chat', noAutocompact: true })}
    <div class="row" style="margin-top:6px"><span class="hint">Runs commands and tools; steers the meta-work through notes you approve.</span><span class="spacer"></span>
      <button class="btn small" id="nc-cancel">cancel</button><button class="btn small primary" id="nc-ok">create</button></div></div>`;
  wireAgentRows(el);
  $('#nc-cancel').onclick = () => { S.chat.creating = false; renderNewChat(); };
  // Nothing is stored until the first message: the new chat stays a draft in this page until then
  $('#nc-ok').onclick = () => {
    S.chat = { ...S.chat, draft: readAgentRow($('.agent-row[data-key]', el)), sid: null, creating: false, liveEvs: [] };
    renderChatBar(); loadChatMessages(); $('#chat-input')?.focus();
  };
}
async function deleteChat() {
  if (!S.chat.sid) { S.chat.draft = null; renderChatBar(); return loadChatMessages(); }  // a draft has nothing stored
  if (!window.confirm('Delete this meta-chat? Its messages and logs are deleted for good; notes it proposed stay.')) return;
  await act(async () => { await api(`/api/meta-chats/${S.chat.sid}`, { method: 'DELETE' }); await chatGone(); }, 'Meta-chat deleted');
}
// The shown chat was deleted (here or in another tab): fall back to the newest one left
async function chatGone() {
  S.chat.sid = null; S.chat.liveEvs = [];
  await loadGoal(); renderChatBar(); await loadChatMessages();
}
// META-CHAT expand: the chat pane itself (feed, live turn, composer) moves into a large window
function expandChat() {
  const box = $('#ov-chat');
  if (!box || S.chatOut) return;
  const m = modal('<div class="modal-head"><h2>meta-chat</h2><button class="icon-btn" data-close>×</button></div><div class="chat-pop" id="chat-pop"></div>', 'full');
  [...box.children].filter(c => !c.classList.contains('lt')).forEach(c => $('#chat-pop', m).appendChild(c));
  S.chatOut = true;
  renderChatBar(); stick($('#chat-feed')); $('#chat-input')?.focus();
}
async function loadChatMessages() {
  const feed = $('#chat-feed');
  if (!feed) return;
  if (!S.chat.sid) {
    S.chat.messages = []; S.chat.busy = false;
    feed.innerHTML = S.chat.draft ? '<div class="empty">Ask anything, e.g. what is blocking the goal right now?</div>'
      : '<div class="empty">Create a chat to talk with a meta-agent about this goal.</div>';
    return setComposer(true);
  }
  const r = await api(`/api/meta-chats/${S.chat.sid}/messages`);
  S.chat.messages = r.messages; S.chat.busy = r.busy;
  if (!r.busy) S.chat.liveEvs = [];
  feed.innerHTML = (r.messages.map((m, i) => m.role === 'user'
    ? `<div class="fr prompt"><div class="fl"><span>▶ human</span><span class="faint">${hhmm(m.ts)}</span></div><div class="md">${md(m.text)}</div></div>`
    : `<div class="fr reply"><div class="fl"><span>◀ meta-chat</span><span class="spacer"></span><button class="btn small" data-save-note="${i}">save as note</button></div>
        <div class="md">${md(m.text)}</div></div>`).join('')
    || '<div class="empty">Ask anything, e.g. what is blocking the goal right now?</div>') + '<div id="chat-live"></div>';
  feed.dataset.free = '';
  paintChatLive();
  setComposer(!r.busy);
}
// The meta-chat's current turn, streamed like the other panes
function paintChatLive() {
  const el = $('#chat-live');
  if (!el) return;
  const text = S.partial[S.chat.sid];
  el.innerHTML = S.chat.busy ? S.chat.liveEvs.filter(e => e.type !== 'text').map(e => tuiEvent(e, { prompt: '', reply: 'meta-chat' })).join('')
    + (text ? `<div class="fr reply live"><div class="fl">◀ meta-chat</div><pre class="stream">${esc(text)}<span class="cursor"></span></pre></div>` : '')
    : '';
  // Like the other feeds, the last tool call blinks its dot while the turn runs
  [...el.querySelectorAll('.tl')].pop()?.classList.add('last');
  $('#chat-feed').classList.toggle('running', S.chat.busy);
  stick($('#chat-feed'));
}
function setComposer(enabled) {
  if (!$('#chat-send')) return;
  const chat = S.chat.sid || S.chat.draft;
  $('#chat-send').disabled = !enabled || !chat;
  $('#chat-input').disabled = !chat;
}
async function sendChat() {
  const text = $('#chat-input').value.trim();
  if (!text || !(S.chat.sid || S.chat.draft)) return;
  const { ws, gid } = S.route;
  await act(async () => {
    if (S.chat.sid) await api(`/api/meta-chats/${S.chat.sid}/messages`, { body: { text } });
    else {  // a draft is stored together with its first message
      const r = await api(`/api/goals/${ws}/${gid}/meta-chats`, { body: { ...S.chat.draft, text } });
      S.chat.sid = r.id; S.chat.draft = null;
      await loadGoal(); renderChatBar();
    }
    $('#chat-input').value = ''; S.chat.liveEvs = [];
    await loadChatMessages();
  });
}
function saveNoteModal(text) {
  const { ws, gid } = S.route;
  const m = modal(`<div class="modal-head"><h2>save as note for the meta-work</h2><button class="icon-btn" data-close>×</button></div>
    <textarea id="sn-text" style="min-height:180px">${esc(text)}</textarea>
    <div class="row" style="margin-top:10px"><label class="inline"><input type="radio" name="snmode" value="queue" checked> queue</label>
      <label class="inline"><input type="radio" name="snmode" value="now"> deliver now</label><span class="spacer"></span>
      <button class="btn" data-close>cancel</button><button class="btn primary" id="sn-ok">save note</button></div>`);
  $('#sn-ok', m).onclick = () => act(async () => {
    await api(`/api/goals/${ws}/${gid}/notes`, { body: { text: $('#sn-text', m).value, mode: $('input[name=snmode]:checked', m).value } });
    closeModal(); await loadGoal(); renderTabs();
  }, 'Note saved');
}

// WORKER-AGENTS: running first, retired (no worktree) last, newest first within each; header = role · status · CLI · model/effort
const workerOpen = w => simpleStatus(w) === 'running' || !!S.open[w.id];
function workerHead(w) {
  const st = simpleStatus(w);
  return `<span class="fold">${workerOpen(w) ? '▾' : '▸'}</span><span class="role">${esc(w.role)}</span>${agentPill(st)}
    <span class="spacer"></span><span class="faint wmeta">${agentLine(w, w.context)}</span>
    <button class="btn small" data-log="${w.id}">expand</button>${st === 'running' ? `<button class="btn small stop" data-stop="${w.id}">stop</button>` : ''}`;
}
function paintHeads() { renderMetaWorkHead(); renderWorkers(); if (!$('#chat-sel:focus')) renderChatBar(); }
function renderWorkers() {
  const el = $('#ov-workers');
  if (!el) return;
  const rank = w => ({ running: 2, retired: 0 })[simpleStatus(w)] ?? 1;
  const workers = S.goal.sessions.filter(s => s.kind === 'worker').sort((a, b) => rank(b) - rank(a) || b.created - a.created);
  $('#ov-wcount').textContent = workers.length ? `(${workers.length})` : '';
  const ids = workers.map(w => w.id).join();
  if (el.dataset.ids !== ids) {
    el.dataset.ids = ids;
    el.innerHTML = workers.map(w => `<div class="${workerClass(w)}" id="w-${w.id}"><div class="wh row small">${workerHead(w)}</div>
      ${feedShell(w.id)}</div>`).join('')
      || '<div class="empty">No workers yet. The meta-work spawns them as it needs them.</div>';
    el.onclick = e => {
      const log = e.target.closest('[data-log]'), stop = e.target.closest('[data-stop]'), head = e.target.closest('.wh');
      if (log) sessionModal(log.dataset.log);
      else if (stop) act(() => api(`/api/sessions/${stop.dataset.stop}/stop`, { body: {} }), 'Stop requested');
      else if (head) toggleWorker(head.parentElement.id.slice(2));
    };
    workers.forEach(w => loadFeed(w.id).then(() => paintFeed(w.id)));
    return;
  }
  workers.forEach(w => {
    const row = $(`#w-${w.id}`), was = row.classList.contains('open');
    row.className = workerClass(w);
    $('.wh', row).innerHTML = workerHead(w);
    if (!was && workerOpen(w)) renderLive(w.id);  // just opened: jump to the live end
  });
}
function workerClass(w) {
  // A worker that starts running opens itself and folds again when it goes idle
  if (simpleStatus(w) === 'running') delete S.open[w.id];
  return `worker ${simpleStatus(w)} ${workerOpen(w) ? 'open' : ''}`;
}
function toggleWorker(sid) {
  const w = S.goal.sessions.find(x => x.id === sid);
  if (!w || simpleStatus(w) === 'running') return;
  S.open[sid] = !S.open[sid];
  renderWorkers();
}

// log window: one whole session, finished turns folded, the running turn streaming every step
async function sessionModal(sid) {
  const s = S.goal.sessions.find(x => x.id === sid) || { id: sid };
  const evs = await api(`/api/sessions/${sid}/events?limit=3000`);
  const title = s.kind === 'worker' ? s.role : s.kind;
  const advisors = S.goal.sessions.filter(x => x.kind === 'meta-advisor');
  const picker = s.kind === 'meta-advisor' ? `<label class="row small">Consultation <select id="advice-session">${advisors.map((x, i) =>
    `<option value="${esc(x.id)}" ${x.id === sid ? 'selected' : ''}>${i + 1} · ${esc(x.id)} · ${esc(x.status)} · ${fmtTime(x.created)}</option>`).join('')}</select></label>` : '';
  modal(`<div class="modal-head"><div><div class="crumbs">${esc(s.kind)} · ${agentLine(s, s.context)}</div><h2>${esc(title)}</h2></div>
      <button class="icon-btn" data-close>×</button></div>
    ${picker}${s.cwd ? `<div class="faint small">${esc(s.cwd)}${s.branch ? ' · ' + esc(s.branch) : ''}</div>` : ''}
    <div class="modal-feed">${feedShell('modal')}</div>`, 'full');
  S.modal = { sid, evs: evs.events };
  if ($('#advice-session')) $('#advice-session').onchange = e => sessionModal(e.target.value);
  renderFeed('modal');
}

// ---------- plan, human report & notes, git ----------
// Plan on the left; on the right, the final reply of each advisor consultation as the meta-work received it
function planHtml() {
  const f = S.goal.files, advisors = S.goal.sessions.filter(s => s.kind === 'meta-advisor');
  const items = advisors.map((s, i) => {
    const replies = S.goal.advice[s.id] || [], last = replies[replies.length - 1];
    // Each consultation folds to its header; the newest starts open, the human's choice wins
    const key = `advice-${s.id}`, open = S.more[key] ?? i === advisors.length - 1;
    // Follow-ups resume the same session, so their replies stack under one consultation
    const body = replies.map(a => `${replies.length > 1 ? `<div class="faint small">${a.turn > 1 ? `follow-up ${a.turn - 1}` : 'first reply'}</div>` : ''}${md(a.text)}`).join('');
    const waiting = s.status === 'queued' || s.status === 'running';
    return `<div class="advice-item ${open ? 'open' : ''}" data-fold-item="${key}"><div class="row small ai-head"><span class="fold">${open ? '▾' : '▸'}</span>
        <b>consultation ${i + 1}</b>${pill(last && !waiting ? last.status : s.status)}
        <span class="spacer"></span><span class="faint">${fmtTime(s.created)}</span><button class="btn small" data-log="${s.id}">expand</button></div>
      ${body ? `<div class="ai-body md doc">${body}</div>` : '<div class="ai-body faint small">no advice yet</div>'}</div>`;
  }).reverse().join('');
  return `<div class="grid plan-grid fit" style="margin-top:18px">
    ${docBox('plan.md', 'plan.md <span class="x">current state, owned by the meta-work</span>', `<div class="md doc">${md(f['plan.md'])}</div>`)}
    ${box('advice <span class="x">meta-advisor replies, newest first</span>',
      `<div class="list-scroll" data-scroll="advice">${items || '<div class="empty">No consultations yet.</div>'}</div>`)}</div>`;
}
function renderNotes(update = false) {
  const el = $('#m-tab'), notes = S.goal.notes, review = S.goal.row.status === 'awaiting_review';
  const list = notes.length ? notes.map(n => `<div class="advice-item"><div class="row small note-head"><b>note #${n.seq}</b><span class="sep">|</span>
        <span class="chip ${n.author === 'human' ? 'human' : 'meta-chat'}">${esc(n.author)}</span><span class="sep">|</span>${pill(n.status)}<span class="spacer"></span>
        ${n.status === 'queued' ? `<button class="btn small danger" data-note="${n.id}" data-v="reject">reject</button>
          <button class="btn small primary" data-note="${n.id}" data-v="deliver">deliver now</button>` : ''}
        <span class="faint">${fmtTime(n.ts)}</span></div>
      ${n.status === 'pending' ? `<textarea data-note-text="${n.id}" style="margin-top:6px;min-height:70px">${esc(n.text)}</textarea>
        <div class="row" style="margin-top:6px"><span class="hint">proposed by a meta-chat; approve to send it to the meta-work</span><span class="spacer"></span>
        <button class="btn small danger" data-note="${n.id}" data-v="reject">reject</button>
        ${review ? `<button class="btn small primary" data-note="${n.id}" data-v="approve" data-reopen="1">approve & reopen goal</button>`
          : `<button class="btn small" data-note="${n.id}" data-v="approve" data-mode="queue">approve (queue)</button>
        <button class="btn small primary" data-note="${n.id}" data-v="approve" data-mode="now">approve & deliver now</button>`}</div>`
      : clamp(`note-${n.id}`, `<div class="md doc note-text">${md(n.text)}</div>`, n === notes[0])}</div>`).join('')
    : '<div class="empty">No notes yet.</div>';
  if (update && $('#notes-list')) {
    keepScroll(() => { $('#notes-list').innerHTML = list; });
    fitClamps($('#notes-list')); renderReport(); fitTab(); return;
  }
  // Steering and the notes split the window below the report; the note list scrolls inside its pane
  el.innerHTML = `<div class="layer human" id="nt-human" hidden><div class="lt">human report</div><div id="nt-report"></div></div>
    <div class="grid cols-2 fit" style="margin-top:18px">
    ${box('steer the meta-work', `<p class="hint">Notes override the meta-work's plan. They are appended to <code>notes.md</code> and delivered to the meta-work.</p>
      <textarea id="note-text" style="min-height:130px;flex:1 1 auto" placeholder="e.g. prefer a simpler approach; skip the caching layer for now."></textarea>
      <div class="row" style="margin-top:8px">
        <label class="inline"><input type="radio" name="nmode" value="queue" checked> queue: deliver with the next event</label>
        <label class="inline"><input type="radio" name="nmode" value="now"> now: wake the meta-work</label>
        <span class="spacer"></span><button class="btn primary" id="note-add">${review ? 'add note & reopen' : 'add note'}</button></div>`)}
    ${box('notes <span class="x">newest first</span>', `<div id="notes-list" class="list-scroll" data-scroll="notes">${list}</div>`)}</div>`;
  fitClamps($('#notes-list'));
  renderReport(); fitTab();
  $('#note-add').onclick = () => act(async () => {
    const { ws, gid } = S.route;
    if (S.goal.row.status === 'awaiting_review') {
      await api(`/api/goals/${ws}/${gid}/reopen`, { body: { text: $('#note-text').value } });
      await loadState();
    } else {
      await api(`/api/goals/${ws}/${gid}/notes`, { body: { text: $('#note-text').value, mode: $('input[name=nmode]:checked').value } });
    }
    $('#note-text').value = ''; await refreshGoal(true);
  }, 'Note added');
  el.onclick = e => {
    const b = e.target.closest('[data-note]');
    if (!b) return;
    const text = $(`[data-note-text="${b.dataset.note}"]`)?.value;
    act(async () => {
      await api(`/api/notes/${b.dataset.note}/${b.dataset.v}`, { body: { mode: b.dataset.mode || 'queue', text, reopen: !!b.dataset.reopen } });
      if (b.dataset.reopen) await loadState();
      await refreshGoal(true);
    }, b.dataset.reopen ? 'Note approved; goal reopened'
      : { approve: 'Note approved', deliver: 'Note delivered', reject: 'Note rejected' }[b.dataset.v]);
  };
}
async function renderGit() {
  const { ws, gid } = S.route;
  const g = await api(`/api/goals/${ws}/${gid}/git`);
  $('#m-tab').innerHTML = `<div class="grid cols-2" style="align-items:start;margin-top:18px">
    ${box('branches', `${g.branches.length ? `<table class="t"><tr><th>branch</th><th>ahead</th><th>state</th><th>last commit</th></tr>
      ${g.branches.map(b => `<tr><td class="small">${esc(b.branch)}</td><td>${b.ahead}</td><td>${b.branch.endsWith('/integration') ? pill('running', 'integration') : b.merged ? pill('ok', 'merged') : pill('pending', 'not merged')}</td>
      <td class="small">${esc(b.subject)}<div class="faint">${esc(b.when)}</div></td></tr>`).join('')}</table>` : '<div class="empty">No branches yet (created when the goal starts).</div>'}
      <p class="hint">After accept, push &amp; create PR sends the integration branch to origin and opens a PR into ${esc(g.base)}; merging the PR is yours.</p>`)}
    ${box(`integration branch <span class="x">commits not in ${esc(g.base)}</span>`, g.integration_log.length ? `<table class="t">${g.integration_log.map(c => `<tr><td class="small">${esc(c.hash)}</td><td class="small">${esc(c.subject)}</td><td class="faint small">${esc(c.when)}</td></tr>`).join('')}</table>` : '<div class="empty">Empty.</div>')}</div>
    <div style="margin-top:22px">${box('workspace audit log (this goal)', auditTable(g.workspace_log))}</div>`;
}

// ---------- infra ----------
// The GPU is read on its own thread; while nvidia-smi hangs the sample carries its last reading, labelled here
function gpuStale(s) {
  const age = s.gpus_ts ? s.ts - s.gpus_ts : 0;
  return age > GPU_STALE_S ? `stale ${Math.round(age)}s` : '';
}
// VRAM granted to a session's `loop run` jobs, per GPU (sessions themselves see no GPU)
function gpuCell(x) {
  const granted = {};
  for (const g of (x.grants || []).filter(g => g.vram)) granted[g.gpu] = (granted[g.gpu] || 0) + g.vram;
  return Object.entries(granted).map(([i, gb]) => `GPU${i} ${gb} GB`).join('<br>');
}
// From the Python interpreter on, as `python -I …`; wrappers before it (taskset, env, paths) are left out
function pyCmd(cmd) {
  const t = cmd.split(' '), i = t.findIndex(x => /^['"]?python[\d.]*$/.test(x.split('/').pop()));
  return i < 0 ? cmd : ['python', ...t.slice(i + 1)].join(' ');
}
// At most 40 characters on one line, ending at the script: `python …/b/c.py`, the path cut from the left to fill
// the width. Arguments after the script are left to the tooltip (the stored command is itself cut at 500 characters)
function shortCmd(cmd, max = 40) {
  const c = pyCmd(cmd);
  if (c.length <= max) return c;
  const m = c.match(/^.*?[^\s'"]+\.(?:py|sh|R|jl)(?=['"\s]|$)/);
  if (!m) return `${c.slice(0, max - 1)}…`;
  const upto = m[0];
  if (upto.length < max - 1) return `${upto} …`;
  const head = upto.includes(' ') ? upto.slice(0, upto.indexOf(' ') + 1) : '';
  return `${head}…${upto.slice(-(max - head.length - 1))}`;
}
// Sessions whose jobs the human folded away; every other session shows its jobs under its row
const jobsFolded = new Set();
document.addEventListener('click', e => {
  const b = e.target.closest('[data-jobs-fold]');
  if (!b) return;
  const id = b.dataset.jobsFold;
  if (!jobsFolded.delete(id)) jobsFolded.add(id);
  renderInfra();
});
// A session's `loop run` jobs under its row, with headings of their own in the session table's columns: job under
// session, the command across kind to goal, then memory, cpu and time where the session has memory, VRAM and jobs
// (its VRAM covers its jobs). Jobs still waiting for room in the pool or on a GPU come last
function jobRows(jobs, waiting) {
  const since = t => fmtDur((S.infra.ts - t) / 3600);
  const cmd = c => `<td colspan="4"><code class="small" title="${esc(c)}">${esc(shortCmd(c))}</code></td>`;
  const tr = cells => `<tr class="job">${cells}</tr>`;
  return `<tr class="job job-head"><th>job</th><th colspan="4">command</th><th class="num">memory</th>
      <th class="num">cpu</th><th class="num">time</th></tr>`
    + jobs.map(j => tr(`<td>${esc(j.job || '—')}</td>${cmd(j.cmd)}
      <td class="num">${j.rss_gb.toFixed(1)} GB</td><td class="num">${j.cpu_cores == null ? '' : `${j.cpu_cores.toFixed(1)} cores`}</td>
      <td class="num">${since(j.started)}</td>`)).join('')
    + waiting.map(w => tr(`<td style="color:var(--warn)">waiting</td>${cmd(w.cmd || '')}
      <td></td><td></td><td class="num">${since(w.since)}</td>`)).join('');
}
// Sessions in a turn, plus idle ones whose jobs still run, each followed by its jobs
function sessionsTable(s) {
  const rows = (s.sessions || []).map(x => ({ ...x, jobs: [] }));
  for (const j of s.jobs || []) {
    let x = rows.find(r => r.id === j.sid);
    if (!x) rows.push(x = { id: j.sid, kind: j.kind, role: j.role, ws: j.ws, goal: j.goal, idle: true, rss_gb: 0,
                            grants: [], waiting: [], jobs: [] });
    x.jobs.push(j);
    if (x.idle) { x.rss_gb = +(x.rss_gb + j.rss_gb).toFixed(2); x.grants.push(j); }
  }
  if (!rows.length) return '<div class="empty">No agent sessions running.</div>';
  // Each session with its jobs is one tbody, so the rule between entries falls between sessions, not under jobs
  return `<div class="scroll-x"><table class="t sessions"><thead><tr><th>session</th><th>kind</th><th>role</th><th>CLI</th><th>goal</th>
      <th class="num">memory</th><th class="num">vram (grant)</th><th class="num">jobs</th></tr></thead>
    ${rows.map(x => {
      const n = x.jobs.length + x.waiting.length, open = n > 0 && !jobsFolded.has(x.id);
      const fold = !n ? '' : `${n} job${n > 1 ? 's' : ''}${x.waiting.length ? ` <span style="color:var(--warn)">(${x.waiting.length} waiting)</span>` : ''}`
        + `<button class="icon-btn fold" data-jobs-fold="${esc(x.id)}" title="${open ? 'Hide' : 'Show'} jobs">${open ? '▾' : '▸'}</button>`;
      return `<tbody class="group"><tr class="${open ? 'has-jobs' : ''}"><td>${esc(x.id)}</td><td>${esc(x.kind)}</td><td>${esc(x.role || '')}</td>
        <td>${x.idle ? '<span class="faint">idle</span>' : esc(cliName(x.agent || ''))}</td>
        <td>${x.ws ? `<a href="#/ws/${x.ws}/g/${x.goal}">${esc(x.ws)}/${esc(x.goal)}</a>` : ''}</td>
        <td class="num">${x.rss_gb.toFixed(1)} GB</td><td class="num">${gpuCell(x)}</td><td class="num">${fold}</td></tr>
        ${open ? `${jobRows(x.jobs, x.waiting)}<tr class="gap"><td colspan="8"></td></tr>` : ''}</tbody>`;
    }).join('')}</table></div>`;
}
function renderInfra() {
  const s = S.infra;
  if (!s) { main.innerHTML = '<div class="page"><div class="empty">Waiting for the first sample…</div></div>'; return; }
  const usedRam = (s.ram_total_gb - s.ram_free_gb).toFixed(1);
  main.innerHTML = `<div class="page"><div class="page-head"><div><div class="crumbs">host</div><h1>infrastructure</h1></div>
      <span class="faint small">updated ${hhmm(s.ts)}</span></div>
    <div class="grid cols-4">
      <div class="card stat"><div class="label">cpu</div><div class="value">${s.cpu_pct.toFixed(0)}%</div>${meter(s.cpu_pct, 100)}<div class="sub">${s.cpus} cores</div></div>
      <div class="card stat"><div class="label">ram</div><div class="value">${usedRam} / ${s.ram_total_gb} GB</div>${meter(s.ram_pct, 100)}<div class="sub">${s.ram_free_gb} GB available · swap ${s.swap_pct}%</div></div>
      <div class="card stat"><div class="label">disk</div><div class="value">${s.disk_free_gb} GB free</div>${meter(s.disk_total_gb - s.disk_free_gb, s.disk_total_gb)}<div class="sub">of ${s.disk_total_gb} GB</div></div>
      <div class="card stat"><div class="label">gpu</div>${s.gpus.length ? s.gpus.map(g => `<div class="value">${esc(g.name)}</div>${meter(g.mem_used_gb, g.mem_total_gb)}<div class="sub">VRAM ${g.mem_used_gb}/${g.mem_total_gb} GB · util ${g.util}%${gpuStale(s) ? ` · ${gpuStale(s)} (nvidia-smi not answering)` : ''}</div>`).join('')
        : '<div class="value">none</div><div class="sub">jobs run CPU-only; loop run --vram is refused</div>'}</div>
    </div>
    <div style="margin-top:22px">${box('last 10 minutes', `<div class="legend"><span><i style="background:var(--accent)"></i>cpu %</span><span><i style="background:var(--reply)"></i>ram %</span>${s.gpus.length ? '<span><i style="background:var(--warn)"></i>vram %</span>' : ''}</div>
      <canvas class="chart" id="infra-chart"></canvas>`)}</div>
    <div style="margin-top:22px">${box('running sessions', sessionsTable(s))}</div></div>`;
  drawChart($('#infra-chart'), 600 / INFRA_SAMPLE_S);
}
// Line chart of cpu/ram(/vram) %; `pct` labels the grid lines as 0%…100% (sidebar)
function drawChart(c, points, pct = false) {
  if (!c) return;
  const dpr = window.devicePixelRatio || 1, w = c.clientWidth, h = c.clientHeight;
  c.width = w * dpr; c.height = h * dpr;
  const g = c.getContext('2d'); g.scale(dpr, dpr);
  const css = getComputedStyle(document.documentElement);
  const spark = h < 40, top = spark ? 2 : 6, bottom = spark ? 2 : 6, left = spark ? 0 : pct ? 30 : 28;
  const y = v => h - bottom - (h - top - bottom) * Math.min(100, v) / 100;
  g.strokeStyle = css.getPropertyValue('--border'); g.fillStyle = css.getPropertyValue('--faint');
  g.font = `10px ${css.getPropertyValue('--font')}`; g.lineWidth = 1;
  (spark ? [] : h < 120 && !pct ? [0, 50, 100] : [0, 25, 50, 75, 100]).forEach(v => {
    g.beginPath(); g.moveTo(left, y(v)); g.lineTo(w, y(v)); g.stroke();
    g.fillText(pct ? `${v}%` : v, 0, y(v) + 3);
  });
  const hist = S.history.slice(-points);
  const series = [[css.getPropertyValue('--accent'), x => x.cpu_pct], [css.getPropertyValue('--reply'), x => x.ram_pct]];
  if (hist.some(x => x.gpus?.length)) series.push([css.getPropertyValue('--warn'), x => x.gpus?.[0] ? 100 * x.gpus[0].mem_used_gb / x.gpus[0].mem_total_gb : 0]);
  // Fixed time axis: the newest sample sits at the right edge, older ones scroll left
  series.forEach(([color, f]) => {
    g.strokeStyle = color; g.lineWidth = 1.5; g.beginPath();
    hist.forEach((x, i) => { const px = left + (w - left) * (points - hist.length + i) / Math.max(1, points - 1); i ? g.lineTo(px, y(f(x))) : g.moveTo(px, y(f(x))); });
    g.stroke();
  });
}

// ---------- agent CLIs: versions, smoke test, update, available models ----------
// `fresh` false re-renders from what the page holds, e.g. after a live usage-limit report
async function renderAgents(fresh = true) {
  if (fresh || !S.agents) S.agents = await api('/api/agents');
  S.models = S.agents.models || {};
  const card = name => {
    const op = S.agents.ops[name];
    const models = modelList(name), quota = S.agents.quotas?.[name];
    // A window whose reset has passed is unused again, whatever the last report said
    const used = w => w.resets_at && w.resets_at * 1000 <= Date.now() ? 0 : w.used;
    return box(cliName(name), `<div class="row"><span>${esc(S.agents.versions[name])}</span></div>
      ${quota ? `<div class="row" style="margin-top:14px"><b>usage limits</b><span class="faint small">as of ${fmtTime(quota.ts)}</span></div>
        <table class="t" style="margin-top:4px">${quota.windows.map(w => `<tr><td>${esc(w.name)}</td><td style="width:35%">${meter(used(w), 1)}</td>
          <td class="small">${Math.round(used(w) * 100)}%</td><td class="small faint">${w.resets_at && used(w) ? `resets ${fmtTime(w.resets_at)}` : ''}</td></tr>`).join('')}</table>` : ''}
      <div class="row" style="margin-top:8px"><button class="btn" data-op="${name}/smoke" ${op?.state === 'running' ? 'disabled' : ''}>smoke test</button>
        <button class="btn primary" data-op="${name}/update" ${op?.state === 'running' ? 'disabled' : ''}>update to latest</button></div>
      <div class="row" style="margin-top:14px"><b>available models</b><span class="faint small">${models.length} · as reported by ${esc(S.models[name]?.version || '?')}</span></div>
      ${models.length ? `<table class="t" style="margin-top:4px"><tr><th>model</th><th>efforts</th></tr>${models.map(m => `<tr><td>${esc(m.alias ? m.name : m.id)}</td>
        <td class="small">${esc(m.efforts.join(' ') || '(none)')}</td></tr>`).join('')}</table>` : '<div class="empty">No catalog yet; run a smoke test, then refresh.</div>'}`);
  };
  main.innerHTML = `<div class="page"><div class="page-head"><div><div class="crumbs">platform</div><h1>agent CLIs</h1>
      <p class="muted small">Sessions use your existing logins. Update runs the CLI's own updater, then a smoke turn, then re-reads the model list;
        every model/effort selector in the UI uses this list.</p></div>
      <div class="actions"><button class="btn" id="quotas-refresh">refresh usage</button>
        <button class="btn" id="models-refresh">refresh model list</button></div></div>
    <div class="grid cols-2" style="margin-top:18px">${AGENTS.map(card).join('')}</div></div>`;
  // Smoke and update run in the background: their pop-up waits for the `agents` event that ends the op
  $$('[data-op]').forEach(b => b.onclick = async () => {
    const [name, op] = b.dataset.op.split('/');
    const done = busyToast(`${cliName(name)} ${op === 'smoke' ? 'smoke test' : 'update'} running`);
    (S.opDone ||= {})[name] = done;
    try { await api(`/api/agents/${b.dataset.op}`, { body: {} }); await renderAgents(); }
    catch (e) { delete S.opDone[name]; done(e.message, 'error'); }
  });
  const refresh = (id, what, path) => $(id).onclick = async () => {
    const done = busyToast(`refreshing ${what}`);
    try { await api(path, { body: {} }); done(`${what}: done`, 'ok'); await renderAgents(); }
    catch (e) { done(`${what}: failed (${e.message})`, 'error'); }
  };
  refresh('#models-refresh', 'model list', '/api/models/refresh');
  refresh('#quotas-refresh', 'usage', '/api/quotas/refresh');
}

// ---------- live events ----------
function handle(evt) {
  const r = S.route;
  const inGoal = r.view === 'goal' && evt.ws === r.ws && evt.goal === r.gid;
  switch (evt.type) {
    case 'infra':
      S.infra = evt.data; S.history.push(evt.data); if (S.history.length > 600 / INFRA_SAMPLE_S) S.history.shift();
      if (r.view === 'infra') renderInfra();
      renderSideInfra();
      break;
    case 'session_event': {
      if (!inGoal) break;
      const e = evt.event, sid = evt.session;
      const s = S.goal?.sessions.find(x => x.id === sid);
      const keys = [s?.kind === 'meta-work' ? 'meta-work' : sid, ...(S.modal?.sid === sid ? ['modal'] : [])];
      const chat = sid === S.chat.sid;
      if (e.type === 'usage' && s && e.context) { s.context = e.context; schedule('h', 'heads', paintHeads); break; }
      // Token deltas only feed the live reply; the complete message replaces it
      if (e.type === 'delta') {
        S.partial[sid] = (S.partial[sid] || '') + e.text;
        keys.forEach(paintLive);
        if (chat) paintChatLive();
        break;
      }
      if (['turn', 'tool', 'turn_end', 'error'].includes(e.type) || (e.type === 'text' && !e.sub)) S.partial[sid] = '';
      if (S.feeds[sid]) { if (FEED_TYPES.includes(e.type)) S.feeds[sid].push(e); }
      else if (e.type === 'turn') S.feeds[sid] = [e];  // a session that started after the page loaded
      if (S.modal?.sid === sid) S.modal.evs.push(e);
      keys.forEach(paintFeed);
      if (chat) {
        if (e.type === 'turn') { S.chat.liveEvs = []; S.chat.busy = true; }
        else if (FEED_TYPES.includes(e.type)) S.chat.liveEvs.push(e);
        paintChatLive();
      }
      if (e.type === 'turn' || e.type === 'turn_end') scheduleRefresh();
      break;
    }
    case 'session_deleted':
      if (inGoal && evt.session === S.chat.sid) chatGone();
      else if (inGoal) scheduleRefresh();
      break;
    case 'meta_chat_message':
      if (inGoal && evt.session === S.chat.sid) loadChatMessages();
      if (inGoal) scheduleRefresh();
      break;
    case 'deleted':
      // Leave a page whose goal or workspace is gone (deleted here or from another tab)
      loadState().then(() => {
        if (r.ws === evt.ws && (!evt.goal || r.gid === evt.goal)) location.hash = evt.goal ? `#/ws/${evt.ws}` : '#/';
        else if (r.view === 'home') renderHome();
      });
      break;
    case 'goal_status':
      loadState();
      if (inGoal) scheduleRefresh();
      break;
    case 'alert':
      toast(`${evt.goal}: ${evt.text}`, 'warn', 9000);
      loadState();
      if (inGoal) scheduleRefresh();
      break;
    case 'agents': {
      // The finished op's outcome replaces its pop-up, wherever the human is
      const done = S.opDone?.[evt.name], op = evt.op, res = op?.result || {};
      if (done && op) {
        delete S.opDone[evt.name];
        const what = `${cliName(evt.name)} ${op.op === 'smoke' ? 'smoke test' : 'update'}`;
        if (op.state === 'failed' || !res.ok) done(`${what}: failed${res.error ? ` (${res.error})` : ''}`, 'error');
        else if (op.op === 'smoke') done(`${what}: passed`, 'ok');
        else done(`${what}: updated${res.smoke?.ok ? '' : ', smoke test failed'}`, res.smoke?.ok ? 'ok' : 'warn');
      }
      if (r.view === 'agents') renderAgents();
      break;
    }
    case 'quota':
      if (r.view === 'agents' && S.agents) { (S.agents.quotas ||= {})[evt.agent] = evt; renderAgents(false); }
      break;
    case 'models':
      loadModels().then(() => { if (r.view === 'agents') renderAgents(); });
      break;
    case 'error':
      toast(evt.text, 'error', 9000);
      break;
    default:
      if (inGoal) scheduleRefresh();
  }
}
function connect() {
  const es = new EventSource('/api/events');
  es.onopen = () => $('#conn').classList.add('on');
  es.onerror = () => $('#conn').classList.remove('on');
  es.onmessage = e => { try { handle(JSON.parse(e.data)); } catch (err) { console.error(err); } };
}
window.addEventListener('resize', () => {
  if (S.route.view === 'infra') drawChart($('#infra-chart'), 600 / INFRA_SAMPLE_S);
  fitTab(); renderSideInfra();
});

(async function boot() {
  try { const r = await api('/api/infra'); S.history = r.history; S.infra = r.latest; } catch { /* first sample arrives over SSE */ }
  await Promise.all([loadState(), loadModels()]);
  connect();
  render(); renderSideInfra();
})();
