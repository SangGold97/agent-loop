'use strict';
/* agent-loop web UI: vanilla JS, hash routing, live updates over SSE. */

const S = { workspaces: [], route: {}, goal: null, feeds: {}, infra: null, history: [], agents: null, models: {},
  chat: { sid: null, messages: [], busy: false, live: '', creating: false }, edit: {}, cfgDirty: false };
const main = document.getElementById('main');
const AGENTS = ['claude', 'codex'];
const FEED_TYPES = ['turn', 'text', 'tool', 'turn_end', 'error'];

// ---------- helpers ----------
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const md = s => (window.marked && window.DOMPurify) ? DOMPurify.sanitize(marked.parse(s || '')) : `<pre>${esc(s)}</pre>`;
const fmtTok = n => !n ? '0' : n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? Math.round(n / 1e3) + 'k' : String(n);
const fmtTime = ts => ts ? new Date(ts * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }) : '–';
const hhmm = ts => new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
const fmtDur = hours => { const m = Math.round(hours * 60); return `${Math.floor(m / 60)} h ${m % 60} mins`; };
const label = s => String(s || '').replace(/_/g, ' ');
const firstLine = s => (String(s || '').split('\n').find(l => l.trim()) || '').slice(0, 220);
// Workers and meta sessions are shown with three states only
const simpleStatus = s => (s.live || s.status === 'running') ? 'running' : s.status === 'failed' ? 'failed' : 'idle';
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
async function act(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg, 'ok'); return r; } catch (e) { toast(e.message, 'error', 8000); }
}
function modal(html, wide = false) {
  const root = $('#modal-root');
  root.innerHTML = `<div class="modal-back"><div class="modal ${wide ? 'wide' : ''}">${html}</div></div>`;
  const back = $('.modal-back', root);
  back.addEventListener('mousedown', e => { if (e.target === back) closeModal(); });
  $$('[data-close]', root).forEach(b => b.onclick = closeModal);
  return $('.modal', root);
}
function closeModal() { $('#modal-root').innerHTML = ''; }
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });
function pill(status, text) { return `<span class="pill ${esc(status)}">${esc(label(text || status))}</span>`; }
function meter(used, max, progress = false) {
  if (!max) return '<div class="meter"><i style="width:0"></i></div>';
  const pct = Math.min(100, (used / max) * 100);
  // A limit turns amber/red as it fills; progress (e.g. the plan checklist) stays green
  const cls = progress ? '' : pct >= 100 ? 'bad' : pct >= 80 ? 'warn' : '';
  return `<div class="meter ${cls}"><i style="width:${pct}%"></i></div>`;
}
const box = (title, body, attrs = '') => `<div class="box" ${attrs}><div class="lt">${title}</div>${body}</div>`;

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
function agentRow(key, conf = {}, opts = {}) {
  const agent = conf.agent || opts.agent || 'claude';
  const off = opts.toggle && conf.enabled === false;
  return `<div class="agent-row ${off ? 'off' : ''}" data-key="${esc(key)}" data-agent="${esc(agent)}">
    <div class="name">${opts.toggle ? `<label class="inline"><input type="checkbox" data-f="enabled" ${off ? '' : 'checked'}> ${esc(opts.title)}</label>` : esc(opts.title)}</div>
    ${opts.fixedAgent ? `<div>${esc(agent)}</div>` : `<select data-f="agent">${AGENTS.map(a => `<option ${a === agent ? 'selected' : ''}>${a}</option>`).join('')}</select>`}
    <select data-f="model">${modelOptions(agent, conf.model)}</select>
    <select data-f="effort">${effortOptions(agent, conf.model, conf.effort)}</select>
    ${opts.noAutocompact ? '<span></span>' : `<input type="text" data-f="autocompact" value="${esc(conf.autocompact ?? '')}" placeholder="autocompact">`}
  </div>`;
}
function agentHeader() {
  return `<div class="agent-row faint small"><span></span><span>agent</span><span>model</span><span>effort</span><span>autocompact</span></div>`;
}
function wireAgentRows(root, onChange) {
  root.addEventListener('change', e => {
    const row = e.target.closest('.agent-row');
    if (row) {
      const f = e.target.dataset.f;
      if (f === 'agent') {
        row.dataset.agent = e.target.value;
        $('[data-f=model]', row).innerHTML = modelOptions(e.target.value, null);
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
function configForm(conf) {
  const b = conf.budget || {}, mw = conf.meta_work || {}, wk = conf.workers || {};
  const num = (k, v, title) => `<label class="f">${title}<input type="number" min="0" step="1" data-b="${k}" value="${v ?? ''}" placeholder="∞"></label>`;
  return `<div class="cfg">
    <div class="k">agents</div><div>${agentHeader()}
      ${agentRow('meta_work', mw, { title: 'meta-work' })}
      ${AGENTS.map(a => agentRow(`worker:${a}`, { agent: a, ...(wk[a] || {}) }, { title: 'worker', toggle: true, fixedAgent: true })).join('')}
      <div class="hint">The meta-work picks <code>--agent claude|codex</code> per worker; model and effort are always taken from here.</div></div>
    <div class="k">rotate</div><div class="row"><span class="muted small">start a fresh meta-work session when its context exceeds</span>
      <input type="text" data-rotate value="${esc(mw.rotate_at_tokens ?? '')}" placeholder="off (e.g. 600k)" style="width:160px"></div>
    <div class="k">budget</div><div class="budget-row">${num('max_concurrent_workers', b.max_concurrent_workers, 'concurrent workers')}
      ${num('max_worker_turns', b.max_worker_turns, 'worker turns')}${num('max_meta_turns', b.max_meta_turns, 'meta-work turns')}</div>
    <div class="k">language</div><div class="row"><select data-lang style="width:160px">${['English', 'Vietnamese'].concat(
        ['English', 'Vietnamese'].includes(conf.language || 'English') ? [] : [conf.language]).map(l => `<option ${l === (conf.language || 'English') ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>
      <span class="hint">for plan, journal, report, chat replies, worker prompts and reports; code and commits stay as the project uses</span></div>
    <div class="k">git</div><div class="budget-row">
      <label class="f">base branch<input type="text" data-base value="${esc(conf.base_branch ?? '')}" placeholder="repo's current"></label>
      <label class="f">push policy<select data-push>${['manual', 'branch'].map(p => `<option ${p === conf.push ? 'selected' : ''}>${p}</option>`).join('')}</select></label></div>
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
  return { meta_work: metaWork, workers, budget, language: $('[data-lang]', root).value,
    base_branch: $('[data-base]', root).value.trim() || null,
    push: $('[data-push]', root).value };
}

// ---------- sidebar & routing ----------
async function loadState() {
  S.workspaces = (await api('/api/state')).workspaces;
  renderSidebar();
}
function renderSidebar() {
  const r = S.route;
  $('#ws-list').innerHTML = S.workspaces.map(w => `<div>
      <a class="ws-name ${r.view === 'workspace' && r.ws === w.name ? 'active' : ''}" href="#/ws/${w.name}">
        <span>${esc(w.name)}</span><button class="icon-btn" data-new-goal="${esc(w.name)}" title="New goal">+</button></a>
      ${w.goals.map(m => `<a class="m-link ${r.gid === m.id && r.ws === w.name ? 'active' : ''}" href="#/ws/${w.name}/g/${m.id}" title="${esc(m.title)}">
        <span class="dot ${m.status}"></span><span class="title">${esc(m.title)}</span></a>`).join('') || '<div class="m-link faint">no goals yet</div>'}
    </div>`).join('') || '<div class="empty small">No workspace yet</div>';
  $$('[data-new-goal]').forEach(b => b.onclick = e => { e.preventDefault(); e.stopPropagation(); newGoalModal(b.dataset.newGoal); });
  $('#nav-infra').classList.toggle('active', r.view === 'infra');
  $('#nav-agents').classList.toggle('active', r.view === 'agents');
}
$('#new-ws').onclick = () => newWorkspaceModal();

const TABS = [['overview', 'OVERVIEW'], ['config', 'CONFIG'], ['plan', 'PLAN & JOURNAL'], ['notes', 'HUMAN REPORT & NOTES'], ['git', 'GIT']];
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
    S.chat = { sid: null, messages: [], busy: false, live: '', creating: false };
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
    <p class="muted" style="max-width:760px">Write a spec and a goal. A meta-work agent plans, delegates to Claude Code / Codex
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
      <div class="actions"><button class="btn primary" id="w-new">new goal</button></div></div>
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
async function newGoalModal(ws) {
  const info = await api(`/api/workspaces/${ws}`);
  const m = modal(`<div class="modal-head"><h2>new goal · ${esc(ws)}</h2><button class="icon-btn" data-close>×</button></div>
    <div class="grid" style="gap:12px">
      <label class="f">title<input type="text" id="nm-title" placeholder="e.g. Add OAuth login"></label>
      <div class="grid cols-2">
        <label class="f">spec: context, requirements, inputs, constraints<textarea id="nm-spec" style="min-height:190px"></textarea></label>
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
  if (st === 'running') buttons.push('<button class="btn danger" data-act="stop">stop</button>');
  if (st === 'awaiting_review') buttons.push('<button class="btn" data-act="reopen">reopen with note</button><button class="btn primary" data-act="accept">accept</button>');
  $('#m-head').innerHTML = `<div class="page-head"><div>
      <div class="crumbs"><a href="#/ws/${ws}">${esc(ws)}</a> / ${esc(gid)}</div>
      <h1>${esc(M.config.title)}</h1>
      <div class="row" style="margin-top:4px">${pill(st)} ${M.row.outcome ? pill(M.row.outcome, 'outcome: ' + M.row.outcome) : ''}
        ${M.meta_active ? '<span class="thinking">meta-work is thinking</span>' : ''}</div></div>
    <div class="actions">${buttons.join('')}</div></div>`;
  $$('[data-act]', $('#m-head')).forEach(b => b.onclick = () => goalAction(b.dataset.act));
}
async function goalAction(action) {
  const { ws, gid } = S.route;
  if (action === 'reopen') return reopenModal();
  if (action === 'stop' && !window.confirm('Stop the goal? Running sessions are killed; their work is snapshotted.')) return;
  await act(async () => {
    const r = await api(`/api/goals/${ws}/${gid}/${action}`, { body: {} });
    if (action === 'accept' && r.tags?.length) toast(`Unmerged experiments kept as tags: ${r.tags.join(', ')}`, 'info', 9000);
    await loadState(); await refreshGoal(true);
  }, { start: 'Goal started', stop: 'Goal stopped', accept: 'Goal accepted' }[action]);
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
  $('#m-tabs').innerHTML = TABS.map(([k, t]) => `<a class="tab ${tab === k ? 'active' : ''}" href="#/ws/${ws}/g/${gid}/${k}">${t}${counts[k] ? `<span class="count">${counts[k]}</span>` : ''}</a>`).join('');
}
async function renderTab() {
  const t = S.route.tab;
  if (t === 'overview') renderOverview();
  else if (t === 'config') { $('#m-tab').innerHTML = '<div id="cfg-tab" style="margin-top:18px"></div>'; renderConfigTab(true); }
  else if (t === 'plan') $('#m-tab').innerHTML = planHtml();
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

// ---------- overview: stats, then META-AGENTS / WORKER-AGENTS layers ----------
function renderOverview() {
  $('#m-tab').innerHTML = `<div id="ov-stats" class="stats"></div>
    <div class="layer"><div class="lt">meta-agents</div><div class="meta-grid">
      <div class="box" id="ov-mw"></div><div class="box" id="ov-chat"></div></div></div>
    <div class="layer"><div class="lt">worker-agents <span class="x faint" id="ov-wcount"></span></div><div id="ov-workers"></div></div>`;
  renderStats(); renderMetaWork(); renderChat(); renderWorkers();
}
function updateOverview(force) {
  if (!$('#ov-stats')) return renderOverview();
  renderStats(); renderMetaWorkHead(); renderChatBar(); renderWorkers();
  // A rotated meta-work session joins the same feed
  const fresh = S.goal.sessions.filter(s => s.kind === 'meta-work' && !S.feeds[s.id]);
  if (fresh.length) Promise.all(fresh.map(s => loadFeed(s.id))).then(() => paintFeed('meta-work'));
}
function renderStats() {
  const M = S.goal, b = M.budget, c = M.checklist;
  const workers = M.sessions.filter(s => s.kind === 'worker');
  const running = workers.filter(s => simpleStatus(s) === 'running').length;
  const lim = v => v || '∞';
  $('#ov-stats').innerHTML = `
    <div class="card stat"><div class="label">plan</div><div class="value">${c.done}/${c.total}</div>${meter(c.done, c.total || 1, true)}
      <div class="sub">${c.total ? 'items checked by the meta-work' : 'no checklist yet'}</div></div>
    <div class="card stat"><div class="label">workers</div><div class="value">${running} running</div>
      <div class="sub">${workers.length} sessions · max ${lim(b.max_concurrent_workers)} at once</div></div>
    <div class="card stat"><div class="label">turns</div><div class="value">worker ${b.worker_turns}/${lim(b.max_worker_turns)}</div>
      <div class="sub">meta-work ${b.meta_turns}/${lim(b.max_meta_turns)}</div></div>
    <div class="card stat"><div class="label">time</div>
      <div class="sub">From ${fmtTime(b.from)} To ${b.from ? (b.to ? fmtTime(b.to) : 'now') : '–'}</div>
      <div class="value">Total: ${fmtDur(b.hours)}</div></div>`;
}
function renderReport() {
  const M = S.goal, report = M.files['report.md'], el = $('#nt-report');
  if (!el) return;
  const review = M.row.status === 'awaiting_review';
  $('#nt-human').hidden = !report;
  if (!report) { el.innerHTML = ''; return; }
  if (el.dataset.report === report + review) return;
  el.dataset.report = report + review;
  el.innerHTML = `<div class="box report"><div class="lt">report${review ? ' · awaiting your review' : ''}</div>
    <div class="md">${md(report.replace(/^# Completion report\s*\n/, ''))}</div>
    ${review ? `<div class="row" style="margin-top:8px"><span class="spacer"></span><button class="btn" id="nt-reopen">reopen with note</button>
      <button class="btn primary" id="nt-accept">accept</button></div>` : ''}</div>`;
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
    return box(`${title} ${editing ? '' : `<button class="btn small" data-edit="${name}">edit</button>`}`,
      editing ? `<textarea class="editor" data-doc="${name}" spellcheck="false" style="min-height:220px">${esc(M.files[name + '.md'])}</textarea>
        <div class="row" style="margin-top:6px"><span class="hint">${hint}</span><span class="spacer"></span>
          <button class="btn small" data-cancel="${name}">cancel</button><button class="btn small primary" data-save-doc="${name}">save</button></div>`
      : `<div class="md">${md(M.files[name + '.md'])}</div>`);
  };
  const verify = M.verify.length ? `${M.verify.length} verify command${M.verify.length > 1 ? 's' : ''} run on the integration branch before a success close`
    : 'no verify: commands; the meta-work judges completion';
  el.innerHTML = `<div class="grid cols-2">${doc('spec', 'spec', 'read-only for agents; a running meta-work is notified with the diff')}
      ${doc('goal', `goal <span class="x">${esc(verify)}</span>`, 'lines like - verify: `pytest -q` are executed by the platform')}</div>
    <div style="margin-top:18px">${box('agents · budget · git <span class="dirty" id="cfg-dirty"></span>', `<div id="ov-cfg">${configForm(M.config)}</div>
      <div class="row" style="margin-top:8px"><span class="hint">Applies from the next turn. Changing the meta-work agent starts a fresh meta-work session (plan and journal carry over).</span>
        <span class="spacer"></span><button class="btn small primary" id="cfg-save">save config</button></div>`)}</div>`;
  S.cfgDirty = false;
  wireAgentRows($('#ov-cfg'), () => { S.cfgDirty = true; $('#cfg-dirty').textContent = 'unsaved'; });
  $('#cfg-save').onclick = () => act(async () => {
    const { ws, gid } = S.route;
    const created = (M.files['goal.yaml'].match(/^created:\s*(.+)$/m) || [])[1];
    const obj = { title: M.config.title, ...(created ? { created: created.replace(/^['"]|['"]$/g, '') } : {}), ...readConfigForm($('#ov-cfg')) };
    await api(`/api/goals/${ws}/${gid}/files/goal.yaml`, { method: 'PUT', body: { text: toYaml(obj) } });
    S.cfgDirty = false; await loadGoal(); renderConfigTab(true);
  }, 'Configuration saved');
  $$('[data-edit]', el).forEach(b => b.onclick = () => { S.edit[b.dataset.edit] = true; renderConfigTab(true); });
  $$('[data-cancel]', el).forEach(b => b.onclick = () => { S.edit[b.dataset.cancel] = false; renderConfigTab(true); });
  $$('[data-save-doc]', el).forEach(b => b.onclick = () => act(async () => {
    const name = b.dataset.saveDoc, { ws, gid } = S.route;
    await api(`/api/goals/${ws}/${gid}/files/${name}.md`, { method: 'PUT', body: { text: $(`[data-doc="${name}"]`, el).value } });
    S.edit[name] = false; await loadGoal(); renderConfigTab(true);
  }, `${b.dataset.saveDoc}.md saved`));
}

// feeds: one frame colour for prompts sent to a session, another for what the session answers
function feedHtml(evs, who) {
  const out = [];
  let tools = 0, lastTool = '', reply = [];
  const flushTools = () => {
    if (tools) out.push(`<div class="fe tools">${tools} tool call${tools > 1 ? 's' : ''} · last: ${esc(lastTool)}</div>`);
    tools = 0;
  };
  const flushReply = () => {
    if (reply.length) out.push(`<div class="fr reply"><div class="fl">◀ ${esc(who.reply)}</div><div class="md">${md(reply.join('\n\n'))}</div></div>`);
    reply = [];
  };
  for (const e of evs) {
    if (e.type === 'turn') {
      flushTools(); flushReply();
      out.push(`<div class="fr prompt"><div class="fl"><span>▶ ${esc(who.prompt)}</span><span class="faint">turn ${e.n} · ${hhmm(e.ts)}</span></div><pre>${esc(e.prompt)}</pre></div>`);
    } else if (e.type === 'text') { flushTools(); reply.push(e.text); }
    else if (e.type === 'tool') { flushReply(); tools++; lastTool = `${e.name} ${e.detail || ''}`.slice(0, 100); }
    else if (e.type === 'error') { flushTools(); flushReply(); out.push(`<div class="fe err">! ${esc(e.text)}</div>`); }
    else if (e.type === 'turn_end') {
      flushTools(); flushReply();
      out.push(`<div class="fe ${e.status === 'failed' ? 'err' : ''}">└ turn ${e.n} ${esc(e.status)}${e.tokens ? ' · ' + fmtTok(e.tokens) + ' tokens' : ''}${e.status === 'failed' && e.error ? ' · ' + esc(e.error) : ''}</div>`);
    }
  }
  flushTools(); flushReply();
  return out.join('') || '<div class="empty">no activity yet</div>';
}
async function loadFeed(sid) {
  if (!S.feeds[sid]) S.feeds[sid] = (await api(`/api/sessions/${sid}/events?limit=4000&types=${FEED_TYPES.join(',')}`)).events;
}
const painting = new Set();
function paintFeed(key) {
  // Coalesce bursts of streamed events into one repaint per frame
  if (painting.has(key)) return;
  painting.add(key);
  requestAnimationFrame(() => {
    painting.delete(key);
    const el = $(`[data-feed="${key}"]`);
    if (!el) return;
    const atBottom = !el.dataset.painted || el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    if (key === 'meta-work') {
      const sessions = S.goal.sessions.filter(s => s.kind === 'meta-work');
      el.innerHTML = feedHtml(sessions.flatMap(s => S.feeds[s.id] || []), { prompt: 'platform → meta-work', reply: 'meta-work' });
    } else {
      const s = S.goal.sessions.find(x => x.id === key);
      if (s) el.innerHTML = feedHtml(S.feeds[key] || [], { prompt: 'meta-work → worker', reply: s.agent });
    }
    el.dataset.painted = '1';
    if (atBottom) el.scrollTop = el.scrollHeight;
  });
}
function agentLine(s) { return `${esc(s.agent)} · ${esc(s.model || 'default')} · ${esc(s.effort || 'default')}`; }

// META-WORK: every meta-work session of the goal, oldest first, as one continuous feed
function renderMetaWork() {
  $('#ov-mw').innerHTML = `<div class="lt">meta-work</div><div id="mw-head" class="row small" style="margin-bottom:4px"></div>
    <div class="feed" data-feed="meta-work"></div>`;
  renderMetaWorkHead();
  const sessions = S.goal.sessions.filter(s => s.kind === 'meta-work');
  Promise.all(sessions.map(s => loadFeed(s.id))).then(() => paintFeed('meta-work'));
}
function renderMetaWorkHead() {
  const el = $('#mw-head');
  if (!el) return;
  const sessions = S.goal.sessions.filter(s => s.kind === 'meta-work');
  const cur = sessions.find(s => s.id === S.goal.row.meta_work) || sessions[sessions.length - 1];
  if (!cur) { el.innerHTML = `<span class="muted">${agentLine({ agent: S.goal.config.meta_work.agent, ...S.goal.config.meta_work })}</span>`; return; }
  const tokens = sessions.reduce((a, s) => a + (s.tokens || 0), 0);
  el.innerHTML = `<span class="muted">${agentLine(cur)}</span><span class="faint">·</span><span>${fmtTok(tokens)} tokens</span>
    <span class="spacer"></span>${pill(simpleStatus(cur))}<a href="#" data-log="${cur.id}" class="small">log</a>`;
  $('[data-log]', el).onclick = e => { e.preventDefault(); sessionModal(cur.id); };
}

// META-CHAT: chats bound to this goal; agent/model/effort chosen per chat
function renderChat() {
  $('#ov-chat').innerHTML = `<div class="lt">meta-chat</div>
    <div class="chat-bar" id="chat-bar"></div><div id="chat-new"></div>
    <div class="feed" id="chat-feed" style="min-height:120px"></div><div class="live-strip" id="chat-live"></div>
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
  if (!S.chat.sid && chats.length) S.chat.sid = chats[chats.length - 1].id;
  bar.innerHTML = `<select id="chat-sel" ${chats.length ? '' : 'disabled'}>${chats.map((c, i) => `<option value="${c.id}" ${c.id === S.chat.sid ? 'selected' : ''}>
      chat ${i + 1} · ${esc(c.agent)}/${esc(c.model || 'default')}/${esc(c.effort || 'default')} · ${c.turns} turns</option>`).reverse().join('') || '<option>no chats yet</option>'}</select>
    <button class="btn small" id="chat-new-btn">+ new chat</button>`;
  $('#chat-sel').onchange = e => { S.chat.sid = e.target.value; S.chat.live = ''; loadChatMessages(); };
  $('#chat-new-btn').onclick = () => { S.chat.creating = !S.chat.creating; renderNewChat(); };
  renderNewChat();
}
function renderNewChat() {
  const el = $('#chat-new');
  if (!S.chat.creating) { el.innerHTML = ''; return; }
  if (el.innerHTML) return;
  el.innerHTML = `<div class="new-chat">${agentHeader()}${agentRow('meta_chat', S.goal.config.meta_work, { title: 'meta-chat', noAutocompact: true })}
    <div class="row" style="margin-top:6px"><span class="hint">Read-only on the goal; proposes notes you approve.</span><span class="spacer"></span>
      <button class="btn small" id="nc-cancel">cancel</button><button class="btn small primary" id="nc-ok">create</button></div></div>`;
  wireAgentRows(el);
  $('#nc-cancel').onclick = () => { S.chat.creating = false; renderNewChat(); };
  $('#nc-ok').onclick = () => act(async () => {
    const { ws, gid } = S.route;
    const r = await api(`/api/goals/${ws}/${gid}/meta-chats`, { body: readAgentRow($('.agent-row[data-key]', el)) });
    S.chat = { ...S.chat, sid: r.id, creating: false };
    await loadGoal(); renderChatBar(); await loadChatMessages();
  }, 'Meta-chat created');
}
async function loadChatMessages() {
  const feed = $('#chat-feed');
  if (!feed) return;
  if (!S.chat.sid) { feed.innerHTML = '<div class="empty">Create a chat to talk with a meta-agent about this goal.</div>'; setComposer(false); return; }
  const r = await api(`/api/meta-chats/${S.chat.sid}/messages`);
  S.chat.messages = r.messages; S.chat.busy = r.busy;
  feed.innerHTML = r.messages.map((m, i) => m.role === 'user'
    ? `<div class="fr prompt"><div class="fl"><span>▶ human</span><span class="faint">${hhmm(m.ts)}</span></div><pre>${esc(m.text)}</pre></div>`
    : `<div class="fr reply"><div class="fl"><span>◀ meta-chat</span><span class="spacer"></span><button class="btn small" data-save-note="${i}">save as note</button></div>
        <div class="md">${md(m.text)}</div></div>`).join('')
    + (r.busy ? '<div class="fe"><span class="thinking">thinking</span></div>' : '')
    || '<div class="empty">Ask anything, e.g. what is blocking the goal right now?</div>';
  feed.scrollTop = feed.scrollHeight;
  $('#chat-live').textContent = r.busy ? S.chat.live : '';
  setComposer(!r.busy);
}
function setComposer(enabled) {
  if (!$('#chat-send')) return;
  $('#chat-send').disabled = !enabled || !S.chat.sid;
  $('#chat-input').disabled = !S.chat.sid;
}
async function sendChat() {
  const text = $('#chat-input').value.trim();
  if (!text || !S.chat.sid) return;
  await act(async () => {
    await api(`/api/meta-chats/${S.chat.sid}/messages`, { body: { text } });
    $('#chat-input').value = ''; S.chat.live = '';
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

// WORKER-AGENTS: newest first; left = role/label/tokens/status, right = prompts and replies
function workerLeft(w) {
  const st = simpleStatus(w);
  return `<div class="role">${esc(w.role)}</div><div class="label">${esc(w.label)}</div>
    <div class="small">${fmtTok(w.tokens)} tokens</div><div>${pill(st)}</div>
    <div class="faint small">${esc(w.agent)}</div>
    <div class="row small" style="margin-top:4px"><a href="#" data-log="${w.id}">log</a>
      ${st === 'running' ? `<button class="btn small danger" data-stop="${w.id}">stop</button>` : ''}</div>`;
}
function renderWorkers() {
  const el = $('#ov-workers');
  if (!el) return;
  const workers = S.goal.sessions.filter(s => s.kind === 'worker').sort((a, b) => b.created - a.created);
  $('#ov-wcount').textContent = workers.length ? `(${workers.length})` : '';
  const ids = workers.map(w => w.id).join();
  if (el.dataset.ids !== ids) {
    el.dataset.ids = ids;
    el.innerHTML = workers.map(w => `<div class="worker ${simpleStatus(w)}" id="w-${w.id}"><div class="wl">${workerLeft(w)}</div>
      <div class="wr"><div class="feed" data-feed="${w.id}"></div></div></div>`).join('')
      || '<div class="empty">No workers yet. The meta-work spawns them as it needs them.</div>';
    el.onclick = e => {
      const log = e.target.closest('[data-log]'), stop = e.target.closest('[data-stop]');
      if (log) { e.preventDefault(); sessionModal(log.dataset.log); }
      if (stop) act(() => api(`/api/sessions/${stop.dataset.stop}/stop`, { body: {} }), 'Stop requested');
    };
    workers.forEach(w => loadFeed(w.id).then(() => paintFeed(w.id)));
    return;
  }
  workers.forEach(w => {
    const row = $(`#w-${w.id}`);
    row.className = `worker ${simpleStatus(w)}`;
    $('.wl', row).innerHTML = workerLeft(w);
  });
}

// full log with tool calls and evidence files
function evHtml(e) {
  switch (e.type) {
    case 'turn': return `<div class="fr prompt"><div class="fl">▶ turn ${e.n} · ${hhmm(e.ts)}</div><pre>${esc(e.prompt)}</pre></div>`;
    case 'text': return `<div class="fr reply"><div class="md">${md(e.text)}</div></div>`;
    case 'tool': return `<div class="fe">▸ ${esc(e.name)} ${esc(e.detail)}</div>`;
    case 'tool_result': return `<div class="fe ${e.error ? 'err' : ''}" style="padding-left:14px">${esc(String(e.text || '').slice(0, 400))}</div>`;
    case 'thinking': return `<div class="fe" style="font-style:italic">${esc(e.text)}</div>`;
    case 'error': return `<div class="fe err">! ${esc(e.text)}</div>`;
    case 'turn_end': return `<div class="fe">└ turn ${e.n} ${esc(e.status)}${e.error ? ' · ' + esc(e.error) : ''}${e.tokens ? ' · ' + fmtTok(e.tokens) + ' tokens' : ''}</div>`;
    default: return '';
  }
}
async function sessionModal(sid) {
  const { ws, gid } = S.route;
  const s = S.goal.sessions.find(x => x.id === sid) || { id: sid };
  const [turns, evs] = await Promise.all([api(`/api/sessions/${sid}/turns`), api(`/api/sessions/${sid}/events?limit=3000`)]);
  const file = (n, ext) => `<a href="/api/runs/${ws}/${gid}/${sid}/t${n}.${ext}" target="_blank">${ext}</a>`;
  const title = s.kind === 'worker' ? `${s.role} · ${s.label}` : s.kind;
  const m = modal(`<div class="modal-head"><div><div class="crumbs">${esc(s.kind)} · ${agentLine(s)}</div><h2>${esc(title)}</h2></div>
      <button class="icon-btn" data-close>×</button></div>
    ${s.cwd ? `<div class="faint small">${esc(s.cwd)}${s.branch ? ' · ' + esc(s.branch) : ''}</div>` : ''}
    <table class="t" style="margin-top:8px"><tr><th>#</th><th>status</th><th>started</th><th>tokens</th><th>final message</th>${s.kind === 'worker' ? '<th>evidence</th>' : ''}</tr>
      ${turns.turns.map(t => `<tr><td>${t.n}</td><td>${pill(t.status)}</td><td class="small">${fmtTime(t.started)}</td><td class="small">${fmtTok(t.tokens)}</td>
        <td class="small">${esc(firstLine(t.final))}</td>${s.kind === 'worker' ? `<td class="small">${file(t.n, 'prompt.md')} ${file(t.n, 'out.md')} ${file(t.n, 'diff')}</td>` : ''}</tr>`).join('')}</table>
    <div class="feed" style="max-height:56vh;margin-top:10px">${evs.events.map(evHtml).join('')}</div>`, true);
  const log = $('.feed', m); log.scrollTop = log.scrollHeight;
}

// ---------- plan, human report & notes, git ----------
function planHtml() {
  const f = S.goal.files;
  return `<div class="grid cols-2" style="margin-top:18px">${box('plan.md <span class="x">owned by the meta-work</span>', `<div class="md">${md(f['plan.md'])}</div>`)}
    ${box('journal.md <span class="x">decision log, written by the meta-work</span>', `<div class="md">${md(f['journal.md'])}</div>`)}</div>`;
}
function renderNotes(update = false) {
  const el = $('#m-tab'), notes = S.goal.notes, review = S.goal.row.status === 'awaiting_review';
  const list = notes.length ? notes.map(n => `<div class="box" style="margin-top:18px"><div class="lt">note #${n.id}
        <span class="chip ${n.author === 'human' ? 'human' : 'meta-chat'}">${esc(n.author)}</span>${pill(n.status)}</div>
      <div class="row small faint">${n.status === 'approved' ? (n.mode === 'now' ? 'delivered immediately' : 'queued with the next event') : ''}<span class="spacer"></span>${fmtTime(n.ts)}</div>
      ${n.status === 'pending' ? `<textarea data-note-text="${n.id}" style="margin-top:6px;min-height:70px">${esc(n.text)}</textarea>
        <div class="row" style="margin-top:6px"><span class="hint">proposed by a meta-chat; approve to send it to the meta-work</span><span class="spacer"></span>
        <button class="btn small danger" data-note="${n.id}" data-v="reject">reject</button>
        ${review ? `<button class="btn small primary" data-note="${n.id}" data-v="approve" data-reopen="1">approve & reopen goal</button>`
          : `<button class="btn small" data-note="${n.id}" data-v="approve" data-mode="queue">approve (queue)</button>
        <button class="btn small primary" data-note="${n.id}" data-v="approve" data-mode="now">approve & deliver now</button>`}</div>`
      : `<div class="md" style="margin-top:4px">${md(n.text)}</div>`}</div>`).join('') : '<div class="empty">No notes yet.</div>';
  if (update && $('#notes-list')) { $('#notes-list').innerHTML = list; renderReport(); return; }
  el.innerHTML = `<div class="layer human" id="nt-human" hidden><div class="lt">human report</div><div id="nt-report"></div></div>
    <div class="grid cols-2" style="align-items:start;margin-top:18px">
    ${box('steer the meta-work', `<p class="hint">Notes override the meta-work's plan. They are appended to <code>notes.md</code> and delivered to the meta-work.</p>
      <textarea id="note-text" style="min-height:130px" placeholder="e.g. prefer a simpler approach; skip the caching layer for now."></textarea>
      <div class="row" style="margin-top:8px">
        <label class="inline"><input type="radio" name="nmode" value="queue" checked> queue: deliver with the next event</label>
        <label class="inline"><input type="radio" name="nmode" value="now"> now: wake the meta-work</label>
        <span class="spacer"></span><button class="btn primary" id="note-add">${review ? 'add note & reopen' : 'add note'}</button></div>`)}
    <div id="notes-list">${list}</div></div>`;
  renderReport();
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
    }, b.dataset.reopen ? 'Note approved; goal reopened' : b.dataset.v === 'approve' ? 'Note approved' : 'Note rejected');
  };
}
async function renderGit() {
  const { ws, gid } = S.route;
  const g = await api(`/api/goals/${ws}/${gid}/git`);
  $('#m-tab').innerHTML = `<div class="grid cols-2" style="align-items:start;margin-top:18px">
    ${box('branches', `${g.branches.length ? `<table class="t"><tr><th>branch</th><th>ahead</th><th>state</th><th>last commit</th></tr>
      ${g.branches.map(b => `<tr><td class="small">${esc(b.branch)}</td><td>${b.ahead}</td><td>${b.branch.endsWith('/integration') ? pill('running', 'integration') : b.merged ? pill('ok', 'merged') : pill('pending', 'not merged')}</td>
      <td class="small">${esc(b.subject)}<div class="faint">${esc(b.when)}</div></td></tr>`).join('')}</table>` : '<div class="empty">No branches yet (created when the goal starts).</div>'}
      <p class="hint">Only you merge the integration branch into your main branch.</p>`)}
    ${box(`integration branch <span class="x">commits not in ${esc(g.base)}</span>`, g.integration_log.length ? `<table class="t">${g.integration_log.map(c => `<tr><td class="small">${esc(c.hash)}</td><td class="small">${esc(c.subject)}</td><td class="faint small">${esc(c.when)}</td></tr>`).join('')}</table>` : '<div class="empty">Empty.</div>')}</div>
    <div style="margin-top:22px">${box('workspace audit log (this goal)', auditTable(g.workspace_log))}</div>`;
}

// ---------- infra ----------
function renderInfra() {
  const s = S.infra;
  if (!s) { main.innerHTML = '<div class="page"><div class="empty">Waiting for the first sample…</div></div>'; return; }
  const usedRam = (s.ram_total_gb - s.ram_free_gb).toFixed(1);
  main.innerHTML = `<div class="page"><div class="page-head"><div><div class="crumbs">host</div><h1>infrastructure</h1></div>
      <span class="faint small">updated ${hhmm(s.ts)}</span></div>
    <div class="grid cols-4">
      <div class="card stat"><div class="label">cpu</div><div class="value">${s.cpu_pct.toFixed(0)}%</div>${meter(s.cpu_pct, 100)}<div class="sub">${s.cpus} cores · load ${s.load.join(' / ')}</div></div>
      <div class="card stat"><div class="label">ram</div><div class="value">${usedRam} / ${s.ram_total_gb} GB</div>${meter(s.ram_pct, 100)}<div class="sub">${s.ram_free_gb} GB available · swap ${s.swap_pct}%</div></div>
      <div class="card stat"><div class="label">disk</div><div class="value">${s.disk_free_gb} GB free</div>${meter(s.disk_total_gb - s.disk_free_gb, s.disk_total_gb)}<div class="sub">of ${s.disk_total_gb} GB</div></div>
      <div class="card stat"><div class="label">gpu</div>${s.gpus.length ? s.gpus.map(g => `<div class="value">${esc(g.name)}</div>${meter(g.mem_used_gb, g.mem_total_gb)}<div class="sub">VRAM ${g.mem_used_gb}/${g.mem_total_gb} GB · util ${g.util}%</div>`).join('')
        : '<div class="value">none</div><div class="sub">workers run CPU-only; --gpu spawns are refused</div>'}</div>
    </div>
    <div style="margin-top:22px">${box('last 10 minutes', `<div class="legend"><span><i style="background:var(--accent)"></i>cpu %</span><span><i style="background:var(--reply)"></i>ram %</span>${s.gpus.length ? '<span><i style="background:var(--warn)"></i>vram %</span>' : ''}</div>
      <canvas class="chart" id="infra-chart"></canvas>`)}</div>
    <div style="margin-top:22px">${box('running sessions', `${s.sessions?.length ? `<table class="t"><tr><th>session</th><th>kind</th><th>goal</th><th>memory</th></tr>
      ${s.sessions.map(x => `<tr><td>${esc(x.id)}</td><td>${esc(x.kind)} ${esc(x.role || '')}</td><td>${x.ws ? `<a href="#/ws/${x.ws}/g/${x.goal}">${esc(x.ws)}/${esc(x.goal)}</a>` : ''}</td><td>${x.rss_gb} GB</td></tr>`).join('')}</table>`
        : '<div class="empty">No agent sessions running.</div>'}
      <p class="hint">Workers run in a systemd scope capped by infra.worker_memory_max_gb (loop.yaml); new workers are refused below min_free_ram_gb.</p>`)}</div></div>`;
  drawChart();
}
function drawChart() {
  const c = $('#infra-chart');
  if (!c) return;
  const dpr = window.devicePixelRatio || 1, w = c.clientWidth, h = c.clientHeight;
  c.width = w * dpr; c.height = h * dpr;
  const g = c.getContext('2d'); g.scale(dpr, dpr);
  const css = getComputedStyle(document.documentElement);
  g.strokeStyle = css.getPropertyValue('--border'); g.fillStyle = css.getPropertyValue('--faint');
  g.font = `11px ${css.getPropertyValue('--font')}`; g.lineWidth = 1;
  [0, 25, 50, 75, 100].forEach(v => { const y = h - 18 - (h - 28) * v / 100; g.beginPath(); g.moveTo(30, y); g.lineTo(w, y); g.stroke(); g.fillText(v, 4, y + 4); });
  const hist = S.history.slice(-200);
  const series = [[css.getPropertyValue('--accent'), x => x.cpu_pct], [css.getPropertyValue('--reply'), x => x.ram_pct]];
  if (hist.some(x => x.gpus?.length)) series.push([css.getPropertyValue('--warn'), x => x.gpus?.[0] ? 100 * x.gpus[0].mem_used_gb / x.gpus[0].mem_total_gb : 0]);
  series.forEach(([color, f]) => {
    g.strokeStyle = color; g.lineWidth = 1.5; g.beginPath();
    hist.forEach((x, i) => { const px = 30 + (w - 30) * i / Math.max(1, hist.length - 1), py = h - 18 - (h - 28) * Math.min(100, f(x)) / 100; i ? g.lineTo(px, py) : g.moveTo(px, py); });
    g.stroke();
  });
}

// ---------- agent CLIs: versions, smoke test, update, available models ----------
async function renderAgents() {
  S.agents = await api('/api/agents');
  S.models = S.agents.models || {};
  const card = name => {
    const op = S.agents.ops[name], res = op?.result;
    const smoke = res?.smoke || (op?.op === 'smoke' ? res : null);
    const models = modelList(name);
    return box(name === 'claude' ? 'claude code' : 'codex', `<div class="row"><span>${esc(S.agents.versions[name])}</span><span class="spacer"></span>
        ${op?.state === 'running' ? `<span class="thinking">${op.op} running</span>` : ''}</div>
      <div class="row" style="margin-top:8px"><button class="btn" data-op="${name}/smoke" ${op?.state === 'running' ? 'disabled' : ''}>smoke test</button>
        <button class="btn primary" data-op="${name}/update" ${op?.state === 'running' ? 'disabled' : ''}>update to latest</button></div>
      ${smoke ? `<div style="margin-top:10px">${pill(smoke.ok ? 'ok' : 'failed', smoke.ok ? 'smoke test passed' : 'smoke test failed')}
        <span class="faint small">${smoke.seconds ?? ''}s</span>${smoke.error ? `<pre class="fe err">${esc(smoke.error)}</pre>` : ''}</div>` : ''}
      ${res?.output ? `<div class="hint" style="margin-top:10px">update output</div><pre class="small" style="max-height:200px;overflow:auto">${esc(res.output)}</pre>` : ''}
      ${op?.state === 'failed' ? `<pre class="fe err">${esc(res?.error)}</pre>` : ''}
      <div class="row" style="margin-top:14px"><b>available models</b><span class="faint small">${models.length} · as reported by ${esc(S.models[name]?.version || '?')}</span></div>
      ${models.length ? `<table class="t" style="margin-top:4px"><tr><th>model</th><th>efforts</th></tr>${models.map(m => `<tr><td>${esc(m.alias ? m.name : m.id)}</td>
        <td class="small">${esc(m.efforts.join(' ') || '(none)')}</td></tr>`).join('')}</table>` : '<div class="empty">No catalog yet; run a smoke test, then refresh.</div>'}`);
  };
  main.innerHTML = `<div class="page"><div class="page-head"><div><div class="crumbs">platform</div><h1>agent CLIs</h1>
      <p class="muted small">Sessions use your existing logins. Update runs the CLI's own updater, then a smoke turn, then re-reads the model list;
        every model/effort selector in the UI uses this list.</p></div>
      <div class="actions"><button class="btn" id="models-refresh">refresh model list</button></div></div>
    <div class="grid cols-2" style="margin-top:18px">${card('claude')}${card('codex')}</div></div>`;
  $$('[data-op]').forEach(b => b.onclick = () => act(async () => {
    await api(`/api/agents/${b.dataset.op}`, { body: {} }); await renderAgents();
  }, 'Started'));
  $('#models-refresh').onclick = () => act(async () => { await api('/api/models/refresh', { body: {} }); await renderAgents(); }, 'Model list refreshed');
}

// ---------- live events ----------
function handle(evt) {
  const r = S.route;
  const inGoal = r.view === 'goal' && evt.ws === r.ws && evt.goal === r.gid;
  switch (evt.type) {
    case 'infra':
      S.infra = evt.data; S.history.push(evt.data); if (S.history.length > 600) S.history.shift();
      $('#nav-infra').title = `CPU ${evt.data.cpu_pct.toFixed(0)}% · RAM ${evt.data.ram_pct}%`;
      if (r.view === 'infra') renderInfra();
      break;
    case 'session_event': {
      if (!inGoal) break;
      const e = evt.event;
      if (S.feeds[evt.session] && FEED_TYPES.includes(e.type)) {
        S.feeds[evt.session].push(e);
        const s = S.goal?.sessions.find(x => x.id === evt.session);
        paintFeed(s?.kind === 'meta-work' ? 'meta-work' : evt.session);
      } else if (!S.feeds[evt.session] && e.type === 'turn') {
        S.feeds[evt.session] = [e];  // a session that started after the page loaded
      }
      if (evt.session === S.chat.sid && ['tool', 'text', 'thinking'].includes(e.type)) {
        S.chat.live = e.type === 'tool' ? `▸ ${e.name} ${e.detail}` : firstLine(e.text);
        if ($('#chat-live')) $('#chat-live').textContent = S.chat.live;
      }
      if (e.type === 'turn' || e.type === 'turn_end') scheduleRefresh();
      break;
    }
    case 'meta_chat_message':
      if (inGoal && evt.session === S.chat.sid) loadChatMessages();
      if (inGoal) scheduleRefresh();
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
    case 'agents':
      if (r.view === 'agents') renderAgents();
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
window.addEventListener('resize', () => { if (S.route.view === 'infra') drawChart(); });

(async function boot() {
  try { const r = await api('/api/infra'); S.history = r.history; S.infra = r.latest; } catch { /* first sample arrives over SSE */ }
  await Promise.all([loadState(), loadModels()]);
  connect();
  render();
})();
