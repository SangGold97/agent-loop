'use strict';
/* agent-loop web UI: vanilla JS, hash routing, live updates over SSE. */

const S = { workspaces: [], route: {}, mission: null, logs: {}, infra: null, history: [], agents: null,
  adv: { sid: null, messages: [], busy: false, live: '' }, dirty: {}, git: null };
const main = document.getElementById('main');
const EFFORTS = { claude: ['low', 'medium', 'high', 'xhigh', 'max'], codex: ['minimal', 'low', 'medium', 'high', 'xhigh'] };
const MODELS = { claude: ['opus', 'sonnet', 'haiku', 'fable', 'claude-opus-5-5', 'claude-sonnet-5', 'claude-fable-5-1'],
  codex: ['gpt-6-astra'] };

// ---------- helpers ----------
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const md = s => (window.marked && window.DOMPurify) ? DOMPurify.sanitize(marked.parse(s || '')) : `<pre>${esc(s)}</pre>`;
const fmtTok = n => !n ? '0' : n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? Math.round(n / 1e3) + 'k' : String(n);
const fmtTime = ts => ts ? new Date(ts * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '–';
const hhmm = ts => new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
const label = s => String(s || '').replace(/_/g, ' ');
const firstLine = s => (String(s || '').split('\n').find(l => l.trim()) || '').slice(0, 220);
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
function meter(used, max, invert = false) {
  if (!max) return `<div class="meter"><i style="width:0"></i></div>`;
  const pct = Math.min(100, (used / max) * 100);
  const cls = pct >= 100 ? 'bad' : pct >= 80 ? 'warn' : invert ? 'ok' : '';
  return `<div class="meter ${cls}"><i style="width:${pct}%"></i></div>`;
}

// ---------- YAML (small dumper for mission.yaml / config objects) ----------
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

// ---------- agent/budget form (new mission + config tab) ----------
function agentFields(key, conf = {}, opts = {}) {
  const agent = conf.agent || 'claude';
  const efforts = EFFORTS[agent] || [];
  return `<div class="agent-row" data-key="${esc(key)}">
    ${opts.editableName ? `<label class="f">Role<input type="text" data-f="name" value="${esc(key)}"></label>` : `<div class="name">${esc(opts.title || key)}</div>`}
    <label class="f">Agent<select data-f="agent">${['claude', 'codex'].map(a => `<option ${a === agent ? 'selected' : ''}>${a}</option>`).join('')}</select></label>
    <label class="f">Model<input type="text" data-f="model" list="models-${agent}" value="${esc(conf.model ?? '')}" placeholder="CLI default"></label>
    <label class="f">Effort<select data-f="effort"><option value="">default</option>${efforts.map(e => `<option ${e === conf.effort ? 'selected' : ''}>${e}</option>`).join('')}</select></label>
    <label class="f">Autocompact<input type="text" data-f="autocompact" value="${esc(conf.autocompact ?? '')}" placeholder="e.g. 400k"></label>
    ${opts.editableName ? `<button type="button" class="icon-btn" data-remove title="Remove role" style="margin-bottom:6px">×</button>` : '<span></span>'}
  </div>`;
}
function configForm(conf) {
  const b = conf.budget || {};
  const num = (k, v) => `<label class="f">${label(k)}<input type="number" min="0" step="any" data-b="${k}" value="${v ?? ''}" placeholder="∞"></label>`;
  return `<datalist id="models-claude">${MODELS.claude.map(m => `<option value="${m}">`).join('')}</datalist>
    <datalist id="models-codex">${MODELS.codex.map(m => `<option value="${m}">`).join('')}</datalist>
    <fieldset><legend>Meta-agent (orchestrator)</legend>${agentFields('meta', conf.meta, { title: 'Orchestrator' })}
      <label class="f" style="max-width:260px">Rotate session when context exceeds<input type="text" data-rotate value="${esc(conf.meta?.rotate_at_tokens ?? '')}" placeholder="off (e.g. 600k)"></label>
      <div style="height:8px"></div></fieldset>
    <fieldset><legend>Advisor (Q&A chat)</legend>${agentFields('advisor', conf.advisor, { title: 'Advisor' })}</fieldset>
    <fieldset><legend>Worker roles</legend><div data-roles>${Object.entries(conf.roles || {}).map(([k, r]) => agentFields(k, r, { editableName: true })).join('')}</div>
      <button type="button" class="btn small" data-add-role>+ Add role</button><div style="height:10px"></div></fieldset>
    <fieldset><legend>Budget</legend><div class="form-grid">
      ${num('max_concurrent_workers', b.max_concurrent_workers)}${num('max_worker_turns', b.max_worker_turns)}
      ${num('max_meta_turns', b.max_meta_turns)}${num('max_hours', b.max_hours)}
      <label class="f">max tokens<input type="text" data-b="max_tokens" value="${esc(b.max_tokens ?? '')}" placeholder="∞ (e.g. 20M)"></label>
    </div><div style="height:10px"></div></fieldset>
    <fieldset><legend>Git</legend><div class="form-grid">
      <label class="f">Base branch<input type="text" data-base value="${esc(conf.base_branch ?? '')}" placeholder="repo's current branch"></label>
      <label class="f">Push policy<select data-push>${['manual', 'branch'].map(p => `<option ${p === conf.push ? 'selected' : ''}>${p}</option>`).join('')}</select></label>
    </div><div style="height:10px"></div></fieldset>`;
}
function wireConfigForm(root) {
  root.addEventListener('change', e => {
    if (e.target.dataset.f !== 'agent') return;
    const row = e.target.closest('.agent-row');
    const agent = e.target.value;
    $('[data-f=model]', row).setAttribute('list', `models-${agent}`);
    $('[data-f=effort]', row).innerHTML = '<option value="">default</option>' + EFFORTS[agent].map(x => `<option>${x}</option>`).join('');
  });
  root.addEventListener('click', e => {
    if (e.target.matches('[data-remove]')) e.target.closest('.agent-row').remove();
    if (e.target.matches('[data-add-role]')) {
      $('[data-roles]', root).insertAdjacentHTML('beforeend', agentFields('new-role', { agent: 'claude', model: 'opus', effort: 'high' }, { editableName: true }));
    }
  });
}
function readAgentRow(row) {
  const val = f => { const v = $(`[data-f=${f}]`, row).value.trim(); return v === '' ? null : v; };
  return { agent: val('agent'), model: val('model'), effort: val('effort'), autocompact: val('autocompact') };
}
function readConfigForm(root) {
  const rows = $$('.agent-row', root);
  const meta = readAgentRow(rows.find(r => r.dataset.key === 'meta'));
  meta.rotate_at_tokens = $('[data-rotate]', root).value.trim() || null;
  const roles = {};
  $$('[data-roles] .agent-row', root).forEach(r => {
    const name = $('[data-f=name]', r).value.trim().toLowerCase().replace(/[^a-z0-9_-]+/g, '-');
    if (name) roles[name] = readAgentRow(r);
  });
  if (!Object.keys(roles).length) throw new Error('at least one worker role is required');
  const budget = {};
  $$('[data-b]', root).forEach(i => {
    const v = i.value.trim();
    budget[i.dataset.b] = v === '' ? null : (i.type === 'number' ? Number(v) : v);
  });
  return { meta, advisor: readAgentRow(rows.find(r => r.dataset.key === 'advisor')), roles, budget,
    base_branch: $('[data-base]', root).value.trim() || null, push: $('[data-push]', root).value };
}

// ---------- sidebar ----------
async function loadState() {
  S.workspaces = (await api('/api/state')).workspaces;
  renderSidebar();
}
function renderSidebar() {
  const r = S.route;
  $('#ws-list').innerHTML = S.workspaces.map(w => `<div class="ws-item">
      <a class="ws-name ${r.view === 'workspace' && r.ws === w.name ? 'active' : ''}" href="#/ws/${w.name}">
        <span>${esc(w.name)}</span><button class="icon-btn" data-new-mission="${esc(w.name)}" title="New mission">+</button></a>
      ${w.missions.map(m => `<a class="m-link ${r.mid === m.id && r.ws === w.name ? 'active' : ''}" href="#/ws/${w.name}/m/${m.id}" title="${esc(m.title)}">
        <span class="dot ${m.status}"></span><span class="title">${esc(m.title)}</span></a>`).join('') || '<div class="m-link faint">no missions yet</div>'}
    </div>`).join('') || '<div class="empty small">No workspace yet</div>';
  $$('[data-new-mission]').forEach(b => b.onclick = e => { e.preventDefault(); e.stopPropagation(); newMissionModal(b.dataset.newMission); });
  $('#nav-infra').classList.toggle('active', r.view === 'infra');
  $('#nav-agents').classList.toggle('active', r.view === 'agents');
}
$('#new-ws').onclick = () => newWorkspaceModal();

// ---------- routing ----------
function parseHash() {
  const p = location.hash.replace(/^#\/?/, '').split('/').map(decodeURIComponent);
  if (p[0] === 'ws' && p[2] === 'm') return { view: 'mission', ws: p[1], mid: p[3], tab: p[4] || 'overview' };
  if (p[0] === 'ws' && p[1]) return { view: 'workspace', ws: p[1] };
  if (p[0] === 'infra') return { view: 'infra' };
  if (p[0] === 'agents') return { view: 'agents' };
  return { view: 'home' };
}
async function render() {
  const prev = S.route;
  S.route = parseHash();
  if (prev.mid !== S.route.mid) { S.logs = {}; S.adv = { sid: null, messages: [], busy: false, live: '' }; S.dirty = {}; }
  renderSidebar();
  try {
    if (S.route.view === 'mission') await renderMission();
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
    <h1>Human on the loop.</h1>
    <p class="muted" style="max-width:720px">Write a spec and a Definition of Done; a meta-agent plans, delegates to Claude Code / Codex workers,
      evaluates evidence and iterates until the DoD is met. You steer with notes, review the report, and merge.</p>
    <div class="row" style="margin-top:14px"><button class="btn primary" id="h-new-ws">New workspace</button></div>
    <div class="ws-cards">${S.workspaces.map(w => {
      const counts = w.missions.reduce((a, m) => (a[m.status] = (a[m.status] || 0) + 1, a), {});
      return `<a class="card" href="#/ws/${w.name}" style="color:inherit"><h2>${esc(w.name)}</h2>
        <div class="row">${Object.entries(counts).map(([k, v]) => pill(k, `${v} ${label(k)}`)).join('') || '<span class="muted small">no missions</span>'}</div></a>`;
    }).join('')}</div></div>`;
  $('#h-new-ws').onclick = () => newWorkspaceModal();
}
async function renderWorkspace() {
  const ws = S.route.ws;
  const [info, state] = [await api(`/api/workspaces/${ws}`), S.workspaces.find(w => w.name === ws)];
  main.innerHTML = `<div class="page">
    <div class="page-head"><div><div class="crumbs">workspace</div><h1>${esc(ws)}</h1><div class="muted small mono">${esc(info.path)}</div></div>
      <div class="actions"><button class="btn primary" id="w-new">New mission</button></div></div>
    <div class="grid cols-2">
      <div class="card"><h2>Missions</h2>${state && state.missions.length ? `<table class="t"><tr><th>Mission</th><th>Status</th></tr>
        ${state.missions.map(m => `<tr><td><a href="#/ws/${ws}/m/${m.id}">${esc(m.title)}</a><div class="faint small mono">${esc(m.id)}</div></td>
        <td>${pill(m.status)} ${m.outcome ? pill(m.outcome) : ''}</td></tr>`).join('')}</table>` : '<div class="empty">No missions yet.</div>'}</div>
      <div class="card"><div class="row"><h2 style="margin:0">loop.yaml</h2><span class="dirty" id="w-dirty"></span><span class="spacer"></span>
        <button class="btn small primary" id="w-save">Save</button></div>
        <p class="hint">Workspace defaults (agents, models, budgets, infra limits, guard patterns). New missions copy <code>defaults</code>.</p>
        <textarea class="editor" id="w-yaml" spellcheck="false">${esc(info.loop_yaml)}</textarea></div>
    </div>
    <div class="card" style="margin-top:14px"><div class="row"><h2 style="margin:0">Harness</h2><span class="spacer"></span>
        <input type="text" id="h-newpath" placeholder="new file, e.g. worker/skills/kaggle-submit/SKILL.md" style="max-width:420px">
        <button class="btn small" id="h-new">Create</button></div>
      <p class="hint">Instructions, skills, hooks and memory for the meta layer (<code>meta/</code>) and the workers (<code>worker/</code>).
        Changes apply to the next agent turn and are committed as <span class="chip human">human</span>.</p>
      <div class="grid" style="grid-template-columns:minmax(220px,300px) 1fr"><div id="h-files" class="adv-list"></div>
        <div><div class="row"><b class="mono" id="h-path">select a file</b><span class="dirty" id="h-dirty"></span><span class="spacer"></span>
          <button class="btn small primary" id="h-save" disabled>Save</button></div>
          <textarea class="editor" id="h-text" spellcheck="false" style="margin-top:8px" disabled></textarea></div></div></div>
    <div class="card" style="margin-top:14px"><h2>Audit log</h2>${auditTable(info.log)}</div></div>`;
  wireHarness(ws);
  $('#w-new').onclick = () => newMissionModal(ws);
  $('#w-yaml').oninput = () => $('#w-dirty').textContent = 'unsaved';
  $('#w-save').onclick = () => act(async () => {
    await api(`/api/workspaces/${ws}/config`, { method: 'PUT', body: { text: $('#w-yaml').value } });
    $('#w-dirty').textContent = '';
  }, 'loop.yaml saved');
}
async function wireHarness(ws, select) {
  const { files } = await api(`/api/workspaces/${ws}/harness`);
  const box = $('#h-files');
  box.innerHTML = files.map(f => `<div class="adv-item mono small" data-hf="${esc(f)}">${esc(f)}</div>`).join('');
  const open = async path => {
    const r = await api(`/api/workspaces/${ws}/harness?path=${encodeURIComponent(path)}`);
    $$('[data-hf]', box).forEach(x => x.classList.toggle('active', x.dataset.hf === path));
    $('#h-path').textContent = path; $('#h-text').value = r.text; $('#h-text').disabled = false;
    $('#h-save').disabled = false; $('#h-dirty').textContent = '';
  };
  $$('[data-hf]', box).forEach(x => x.onclick = () => open(x.dataset.hf));
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
  const chip = a => { const k = a.startsWith('meta') ? 'meta' : a.startsWith('advisor') ? 'advisor' : a; return `<span class="chip ${esc(k)}">${esc(a)}</span>`; };
  return rows.length ? `<table class="t"><tr><th>Commit</th><th>Actor</th><th>Message</th><th>When</th></tr>${rows.map(c =>
    `<tr><td class="mono">${esc(c.hash)}</td><td>${chip(c.author)}</td><td>${esc(c.subject)}</td><td class="muted small">${esc(c.when)}</td></tr>`).join('')}</table>`
    : '<div class="empty">No commits.</div>';
}
function newWorkspaceModal() {
  const m = modal(`<div class="modal-head"><h2>New workspace</h2><button class="icon-btn" data-close>×</button></div>
    <div class="grid" style="gap:12px">
      <label class="f">Name (lowercase, e.g. <code>kaggle-titanic</code>)<input type="text" id="nw-name" placeholder="my-project"></label>
      <label class="f">Project repository to clone (git URL or local path) — leave empty for a fresh repo<input type="text" id="nw-repo" placeholder="git@github.com:org/repo.git or /path/to/repo"></label>
      <label class="f">Base branch (optional)<input type="text" id="nw-base" placeholder="repo default"></label>
      <p class="hint">The workspace gets its own git history, a meta-agent harness (<code>meta/</code>) and a worker harness (<code>worker/</code>) you can customise.</p>
      <div class="row"><span class="spacer"></span><button class="btn" data-close>Cancel</button><button class="btn primary" id="nw-ok">Create</button></div></div>`);
  $('#nw-name', m).focus();
  $('#nw-ok', m).onclick = () => act(async () => {
    $('#nw-ok', m).disabled = true; $('#nw-ok', m).textContent = 'Creating…';
    try {
      const r = await api('/api/workspaces', { body: { name: $('#nw-name', m).value.trim(), repo: $('#nw-repo', m).value.trim(), base_branch: $('#nw-base', m).value.trim() } });
      closeModal(); await loadState(); location.hash = `#/ws/${r.name}`;
    } finally { $('#nw-ok', m) && ($('#nw-ok', m).disabled = false, $('#nw-ok', m).textContent = 'Create'); }
  }, 'Workspace created');
}
async function newMissionModal(ws) {
  const info = await api(`/api/workspaces/${ws}`);
  const d = info.config.defaults;
  const m = modal(`<div class="modal-head"><h2>New mission · ${esc(ws)}</h2><button class="icon-btn" data-close>×</button></div>
    <div class="grid" style="gap:12px">
      <label class="f">Title<input type="text" id="nm-title" placeholder="e.g. Add OAuth login"></label>
      <div class="grid cols-2">
        <label class="f">Spec — goal, context, inputs, constraints<textarea id="nm-spec" style="min-height:200px" placeholder="What should be achieved and why…"></textarea></label>
        <label class="f">Definition of Done<textarea id="nm-dod" style="min-height:200px" placeholder="- criterion (qualitative ones are judged by the orchestrator)&#10;- verify: \`pytest -q\`   ← optional command run on the integration branch"></textarea></label>
      </div>
      <details><summary class="btn small" style="display:inline-block">Agents, budget & git</summary><div style="margin-top:12px" id="nm-conf">${configForm(d)}</div></details>
      <div class="row"><label class="row small"><input type="checkbox" id="nm-start" checked> Start right away</label><span class="spacer"></span>
        <button class="btn" data-close>Cancel</button><button class="btn primary" id="nm-ok">Create mission</button></div></div>`, true);
  wireConfigForm($('#nm-conf', m));
  $('#nm-title', m).focus();
  $('#nm-ok', m).onclick = () => act(async () => {
    const config = readConfigForm($('#nm-conf', m));
    const r = await api(`/api/workspaces/${ws}/missions`, { body: { title: $('#nm-title', m).value, spec: $('#nm-spec', m).value, dod: $('#nm-dod', m).value, config } });
    if ($('#nm-start', m).checked) await api(`/api/missions/${ws}/${r.id}/start`, { body: {} });
    closeModal(); await loadState(); location.hash = `#/ws/${ws}/m/${r.id}`;
  }, 'Mission created');
}

// ---------- mission ----------
async function loadMission() {
  const { ws, mid } = S.route;
  S.mission = await api(`/api/missions/${ws}/${mid}`);
}
async function renderMission() {
  await loadMission();
  main.innerHTML = `<div class="page"><div id="m-head"></div><div class="tabs" id="m-tabs"></div><div id="m-tab"></div></div>`;
  renderMissionHead(); renderTabs(); await renderTab();
}
function renderMissionHead() {
  const M = S.mission, st = M.row.status, { ws, mid } = S.route;
  const busy = M.meta_active ? '<span class="thinking">orchestrator is thinking</span>' : '';
  const buttons = [];
  if (['draft', 'stopped', 'paused'].includes(st)) buttons.push(`<button class="btn primary" data-act="start">${st === 'draft' ? 'Start mission' : 'Resume'}</button>`);
  if (st === 'running') buttons.push('<button class="btn danger" data-act="stop">Stop</button>');
  if (st === 'awaiting_review') buttons.push('<button class="btn" data-act="reopen">Reopen with note</button><button class="btn primary" data-act="accept">Accept</button>');
  $('#m-head').innerHTML = `<div class="page-head"><div>
      <div class="crumbs"><a href="#/ws/${ws}">${esc(ws)}</a> / <span class="mono">${esc(mid)}</span></div>
      <h1>${esc(M.config.title)}</h1>
      <div class="row" style="margin-top:6px">${pill(st)} ${M.row.outcome ? pill(M.row.outcome, 'outcome: ' + M.row.outcome) : ''} ${busy}</div></div>
    <div class="actions">${buttons.join('')}</div></div>`;
  $$('[data-act]', $('#m-head')).forEach(b => b.onclick = () => missionAction(b.dataset.act));
}
async function missionAction(action) {
  const { ws, mid } = S.route;
  if (action === 'reopen') return reopenModal();
  if (action === 'stop' && !confirmInline('Stop the mission? Running sessions are killed; their work is snapshotted.')) return;
  await act(async () => {
    const r = await api(`/api/missions/${ws}/${mid}/${action}`, { body: {} });
    if (action === 'accept' && r.tags?.length) toast(`Unmerged experiments kept as tags: ${r.tags.join(', ')}`, 'info', 9000);
    await loadState(); await refreshMission(true);
  }, { start: 'Mission started', stop: 'Mission stopped', accept: 'Mission accepted' }[action]);
}
function confirmInline(msg) { return window.confirm(msg); }
function reopenModal() {
  const { ws, mid } = S.route;
  const m = modal(`<div class="modal-head"><h2>Reopen mission</h2><button class="icon-btn" data-close>×</button></div>
    <p class="muted">Tell the orchestrator what is missing or what to change. It receives this note immediately.</p>
    <textarea id="ro-text" style="min-height:140px" placeholder="e.g. The API works but there are no tests for the error paths…"></textarea>
    <div class="row" style="margin-top:12px"><span class="spacer"></span><button class="btn" data-close>Cancel</button><button class="btn primary" id="ro-ok">Reopen</button></div>`);
  $('#ro-ok', m).onclick = () => act(async () => {
    await api(`/api/missions/${ws}/${mid}/reopen`, { body: { text: $('#ro-text', m).value } });
    closeModal(); await loadState(); await refreshMission(true);
  }, 'Mission reopened');
}
const TABS = [['overview', 'Overview'], ['sessions', 'Sessions'], ['plan', 'Plan & journal'], ['notes', 'Notes'],
  ['advisor', 'Advisor'], ['spec', 'Spec & DoD'], ['config', 'Config'], ['git', 'Git']];
function renderTabs() {
  const M = S.mission, { ws, mid, tab } = S.route;
  const counts = { sessions: M.sessions.filter(s => s.kind !== 'advisor' && s.status !== 'retired').length,
    notes: M.notes.filter(n => n.status === 'pending').length || '', advisor: M.sessions.filter(s => s.kind === 'advisor').length || '' };
  $('#m-tabs').innerHTML = TABS.map(([k, t]) => `<a class="tab ${tab === k ? 'active' : ''}" href="#/ws/${ws}/m/${mid}/${k}">${t}${counts[k] ? `<span class="count">${counts[k]}</span>` : ''}</a>`).join('');
}
async function renderTab() {
  const el = $('#m-tab');
  const t = S.route.tab;
  if (t === 'overview') el.innerHTML = overviewHtml(), wireOverview();
  else if (t === 'sessions') renderSessions();
  else if (t === 'plan') el.innerHTML = planHtml();
  else if (t === 'notes') renderNotes();
  else if (t === 'advisor') await renderAdvisor();
  else if (t === 'spec') renderSpec();
  else if (t === 'config') renderConfig();
  else if (t === 'git') await renderGit();
}
function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => refreshMission(false), 350);
}
async function refreshMission(force) {
  if (S.route.view !== 'mission') return;
  try { await loadMission(); } catch { return; }
  renderMissionHead(); renderTabs();
  const t = S.route.tab;
  if (t === 'sessions') return renderSessions(true);
  if (t === 'notes') return renderNotes(true);
  if (t === 'advisor') return renderAdvisorList();
  if ((t === 'spec' || t === 'config') && !force) return;
  if (t === 'overview') {
    const keep = $('#ov-reopen')?.value;
    await renderTab();
    if (keep && $('#ov-reopen')) $('#ov-reopen').value = keep;
    return;
  }
  await renderTab();
}

// overview
function budgetRows(b) {
  const row = (name, used, max, txt) => `<div class="budget-row"><span class="muted">${name}</span>${meter(used, max)}<span class="small mono">${txt}</span></div>`;
  const lim = v => v ? v : '∞';
  return row('Running workers', b.running_workers, b.max_concurrent_workers, `${b.running_workers}/${lim(b.max_concurrent_workers)}`)
    + row('Worker turns', b.worker_turns, b.max_worker_turns, `${b.worker_turns}/${lim(b.max_worker_turns)}`)
    + row('Meta turns', b.meta_turns, b.max_meta_turns, `${b.meta_turns}/${lim(b.max_meta_turns)}`)
    + row('Hours', b.hours, b.max_hours, `${b.hours.toFixed(1)}/${lim(b.max_hours)}`)
    + row('Tokens', b.tokens, null, `${fmtTok(b.tokens)}/${b.max_tokens || '∞'}`);
}
function activityHtml(items) {
  if (!items.length) return '<div class="empty">No events yet. Start the mission to kick off the orchestrator.</div>';
  return `<div class="timeline">${items.map(a => {
    const text = a.kind === 'note' ? `Human note #${a.payload.id}: ${a.payload.text}` : a.kind === 'file_changed' ? `${a.payload.name} changed by the human\n\n${a.payload.diff}` : a.payload.text;
    const state = a.consumed ? '' : (a.deferred ? '<span class="pill pending">queued</span>' : '<span class="pill running">pending</span>');
    return `<details class="tl-item"><summary><span class="tl-kind ${a.kind}">${label(a.kind)}</span><span class="tl-first">${esc(firstLine(text))}</span>${state}
      <span class="faint small">${hhmm(a.ts)}</span></summary><pre>${esc(text)}</pre></details>`;
  }).join('')}</div>`;
}
function overviewHtml() {
  const M = S.mission, b = M.budget, c = M.checklist;
  const workers = M.sessions.filter(s => s.kind === 'worker');
  const running = workers.filter(s => s.status === 'running').length;
  const report = M.files['report.md'];
  return `<div class="grid cols-4">
      <div class="card stat"><div class="label">Plan checklist</div><div class="value">${c.done}/${c.total}</div>${meter(c.done, c.total || 1, true)}
        <div class="sub">${c.total ? Math.round(100 * c.done / c.total) + '% of items checked by the orchestrator' : 'no checklist yet'}</div></div>
      <div class="card stat"><div class="label">Workers</div><div class="value">${running} <span class="muted small">running</span></div>
        <div class="sub">${workers.length} total · ${workers.filter(w => w.merged).length} merged</div></div>
      <div class="card stat"><div class="label">Tokens</div><div class="value">${fmtTok(b.tokens)}</div><div class="sub">${b.cost ? '≈ $' + b.cost.toFixed(2) + ' (estimate)' : 'cost n/a'}</div></div>
      <div class="card stat"><div class="label">Elapsed</div><div class="value">${b.hours.toFixed(1)} h</div><div class="sub">${M.row.started ? 'started ' + fmtTime(M.row.started) : M.row.ended ? 'closed ' + fmtTime(M.row.ended) : 'not started'}</div></div>
    </div>
    ${report ? `<div class="card" style="margin-top:14px;border-color:var(--warn)"><div class="row"><h2 style="margin:0">Completion report</h2><span class="spacer"></span>
      ${M.row.status === 'awaiting_review' ? '<button class="btn" id="ov-reopen-btn">Reopen with note</button><button class="btn primary" id="ov-accept">Accept</button>' : ''}</div>
      <div class="md">${md(report.replace(/^# Completion report\s*\n/, ''))}</div>
      ${M.row.status === 'awaiting_review' ? '<textarea id="ov-reopen" placeholder="Optional: what should change if you reopen…" style="min-height:70px;margin-top:8px"></textarea>' : ''}</div>` : ''}
    <div class="grid cols-2" style="margin-top:14px">
      <div class="card"><h2>Budget</h2>${budgetRows(b)}</div>
      <div class="card"><h2>Definition of Done</h2><div class="md">${md(M.files['dod.md'])}</div>
        ${M.verify.length ? `<div class="hint" style="margin-top:8px">${M.verify.length} <code>verify:</code> command${M.verify.length > 1 ? 's' : ''} run by the platform on the integration branch before a <b>success</b> close.</div>` : '<div class="hint">No <code>verify:</code> commands — the orchestrator judges completion.</div>'}</div>
    </div>
    <div class="card" style="margin-top:14px"><h2>What the orchestrator received</h2>${activityHtml(M.activity)}</div>`;
}
function wireOverview() {
  const { ws, mid } = S.route;
  if ($('#ov-accept')) $('#ov-accept').onclick = () => missionAction('accept');
  if ($('#ov-reopen-btn')) $('#ov-reopen-btn').onclick = () => act(async () => {
    const text = $('#ov-reopen').value.trim();
    if (!text) return reopenModal();
    await api(`/api/missions/${ws}/${mid}/reopen`, { body: { text } });
    await loadState(); await refreshMission(true);
  }, 'Mission reopened');
}

// sessions
function sessMeta(s) {
  const infra = (S.infra?.sessions || []).find(x => x.id === s.id);
  return [`${s.agent} · ${s.model || 'default'} · ${s.effort || 'default'}`, `${s.turns} turns`, `${fmtTok(s.tokens)} tokens`,
    s.context ? `ctx ~${fmtTok(s.context)}` : '', s.cost ? `$${s.cost.toFixed(2)}` : '', infra ? `${infra.rss_gb} GB RSS` : '',
    s.branch ? `<span class="mono">${esc(s.branch)}</span>` : ''].filter(Boolean).map(x => `<span>${x}</span>`).join('');
}
function sessCard(s) {
  const status = s.live ? 'running' : s.status;
  return `<div class="card sess ${s.kind}" id="card-${s.id}">
    <div class="sess-head"><div class="sess-title"><span class="kind">${s.kind}</span><span class="sess-id">${esc(s.id)}</span>
      ${s.kind === 'worker' ? `<span>${esc(s.role)} · ${esc(s.label)}</span>` : ''}<span class="spacer"></span>
      ${s.merged ? '<span class="pill ok">merged</span>' : ''}<span data-st>${pill(status)}</span></div>
      <div class="sess-meta" data-meta>${sessMeta(s)}</div></div>
    <div class="log" id="log-${s.id}"><div class="faint small">loading…</div></div>
    <div class="sess-foot"><button class="btn small" data-detail="${s.id}">Details & turns</button>
      ${s.kind === 'worker' && status === 'running' ? `<button class="btn small danger" data-stop="${s.id}">Stop</button>` : ''}</div></div>`;
}
function renderSessions(update = false) {
  const el = $('#m-tab');
  const list = S.mission.sessions.filter(s => s.kind !== 'advisor');
  const active = list.filter(s => s.status !== 'retired');
  const retired = list.filter(s => s.status === 'retired');
  const ids = active.map(s => s.id).join();
  if (update && el.dataset.ids === ids) {
    active.forEach(s => {
      const card = $(`#card-${s.id}`);
      if (!card) return;
      $('[data-st]', card).innerHTML = pill(s.live ? 'running' : s.status);
      $('[data-meta]', card).innerHTML = sessMeta(s);
      const foot = $('.sess-foot', card);
      const running = s.live || s.status === 'running';
      if (s.kind === 'worker' && running && !$('[data-stop]', foot)) foot.insertAdjacentHTML('beforeend', `<button class="btn small danger" data-stop="${s.id}">Stop</button>`);
      if (!running) $('[data-stop]', foot)?.remove();
    });
    return;
  }
  el.dataset.ids = ids;
  if (!active.length) { el.innerHTML = '<div class="empty">No sessions yet. Start the mission to create the orchestrator; it spawns workers as it needs them.</div>'; return; }
  el.innerHTML = `<div class="sessions">${active.map(sessCard).join('')}</div>
    ${retired.length ? `<p class="muted small" style="margin-top:14px">Retired orchestrator sessions: ${retired.map(r => `<a href="#" data-detail="${r.id}" class="mono">${r.id}</a>`).join(', ')}</p>` : ''}`;
  el.onclick = e => {
    const d = e.target.closest('[data-detail]'), st = e.target.closest('[data-stop]');
    if (d) { e.preventDefault(); sessionModal(d.dataset.detail); }
    if (st) act(() => api(`/api/sessions/${st.dataset.stop}/stop`, { body: {} }), 'Stop requested');
  };
  active.forEach(async s => {
    if (!S.logs[s.id]) S.logs[s.id] = (await api(`/api/sessions/${s.id}/events?limit=250`)).events;
    paintLog(s.id);
  });
}
function evHtml(e) {
  switch (e.type) {
    case 'turn': return `<div class="ev turn"><details><summary>Turn ${e.n} · ${hhmm(e.ts)} — prompt</summary><pre>${esc(e.prompt)}</pre></details></div>`;
    case 'text': return `<div class="ev text md">${md(e.text)}</div>`;
    case 'tool': return `<div class="ev tool">▸ <b>${esc(e.name)}</b> ${esc(e.detail)}</div>`;
    case 'tool_result': return `<div class="ev tool_result ${e.error ? 'err' : ''}">${esc(e.text)}</div>`;
    case 'thinking': return `<div class="ev thinking">${esc(e.text)}</div>`;
    case 'error': return `<div class="ev error">⚠ ${esc(e.text)}</div>`;
    case 'turn_end': return `<div class="ev turn_end">turn ${e.n} ${esc(e.status)}${e.error ? ' — ' + esc(e.error) : ''}${e.tokens ? ' · ' + fmtTok(e.tokens) + ' tokens' : ''}</div>`;
    default: return '';
  }
}
function paintLog(sid) {
  const el = $(`#log-${sid}`);
  if (!el) return;
  const evs = S.logs[sid] || [];
  el.innerHTML = evs.map(evHtml).join('') || '<div class="faint small">no activity yet</div>';
  el.scrollTop = el.scrollHeight;
}
function appendLog(sid, evt) {
  if (!S.logs[sid]) return;
  S.logs[sid].push(evt);
  if (S.logs[sid].length > 800) S.logs[sid].splice(0, 200);
  const el = $(`#log-${sid}`);
  if (!el) return;
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
  if (el.firstElementChild?.classList.contains('faint')) el.innerHTML = '';
  el.insertAdjacentHTML('beforeend', evHtml(evt));
  if (atBottom) el.scrollTop = el.scrollHeight;
}
async function sessionModal(sid) {
  const { ws, mid } = S.route;
  const s = S.mission.sessions.find(x => x.id === sid) || { id: sid };
  const [turns, evs] = await Promise.all([api(`/api/sessions/${sid}/turns`), api(`/api/sessions/${sid}/events?limit=3000`)]);
  const file = (n, ext) => `<a href="/api/runs/${ws}/${mid}/${sid}/t${n}.${ext}" target="_blank">${ext}</a>`;
  const m = modal(`<div class="modal-head"><div><div class="crumbs">${esc(s.kind || '')} ${esc(s.role || '')}</div><h2 class="mono" style="margin:0">${esc(sid)}</h2></div>
      <button class="icon-btn" data-close>×</button></div>
    <div class="sess-meta" style="margin-bottom:10px">${s.agent ? sessMeta(s) : ''}${s.cwd ? `<span class="mono">${esc(s.cwd)}</span>` : ''}</div>
    <table class="t"><tr><th>#</th><th>Status</th><th>Started</th><th>Tokens</th><th>Final message</th>${s.kind === 'worker' ? '<th>Evidence</th>' : ''}</tr>
      ${turns.turns.map(t => `<tr><td>${t.n}</td><td>${pill(t.status)}</td><td class="small">${fmtTime(t.started)}</td><td class="small">${fmtTok(t.tokens)}</td>
        <td class="small">${esc(firstLine(t.final))}</td>${s.kind === 'worker' ? `<td class="small">${file(t.n, 'prompt.md')} · ${file(t.n, 'out.md')} · ${file(t.n, 'diff')}</td>` : ''}</tr>`).join('')}</table>
    <h3 style="margin-top:16px">Full log</h3><div class="log" style="height:52vh;border:1px solid var(--border);border-radius:8px">${evs.events.map(evHtml).join('')}</div>`, true);
  const log = $('.log', m); log.scrollTop = log.scrollHeight;
}

// plan
function planHtml() {
  const f = S.mission.files;
  return `<div class="grid cols-2"><div class="card"><h2>plan.md <span class="faint small">· owned by the orchestrator</span></h2><div class="md">${md(f['plan.md'])}</div></div>
    <div class="card"><h2>journal.md</h2><div class="md">${md(f['journal.md'])}</div></div></div>`;
}

// notes
function renderNotes(update = false) {
  const el = $('#m-tab'), notes = S.mission.notes, review = S.mission.row.status === 'awaiting_review';
  const list = notes.length ? notes.map(n => `<div class="card" style="margin-bottom:10px">
      <div class="row"><b>#${n.id}</b><span class="chip ${n.author === 'human' ? 'human' : 'advisor'}">${esc(n.author)}</span>${pill(n.status)}
        ${n.status === 'approved' ? `<span class="muted small">${n.mode === 'now' ? 'delivered immediately' : 'queued with next event'}</span>` : ''}
        <span class="spacer"></span><span class="faint small">${fmtTime(n.ts)}</span></div>
      ${n.status === 'pending' ? `<textarea data-note-text="${n.id}" style="margin-top:8px;min-height:70px">${esc(n.text)}</textarea>
        <div class="row" style="margin-top:8px"><span class="muted small">Proposed by the advisor — approve to send it to the orchestrator.</span><span class="spacer"></span>
        <button class="btn small danger" data-note="${n.id}" data-v="reject">Reject</button>
        ${review ? `<button class="btn small primary" data-note="${n.id}" data-v="approve" data-reopen="1">Approve & reopen mission</button>`
          : `<button class="btn small" data-note="${n.id}" data-v="approve" data-mode="queue">Approve (queue)</button>
        <button class="btn small primary" data-note="${n.id}" data-v="approve" data-mode="now">Approve & deliver now</button>`}</div>`
      : `<div class="md" style="margin-top:6px">${md(n.text)}</div>`}</div>`).join('') : '<div class="empty">No notes yet.</div>';
  if (update && $('#notes-list')) { $('#notes-list').innerHTML = list; return; }
  el.innerHTML = `<div class="grid cols-2" style="align-items:start">
    <div class="card"><h2>Steer the orchestrator</h2>
      <p class="hint">Notes override the orchestrator's plan. They are appended to <code>notes.md</code> and delivered to the mission's orchestrator.</p>
      <textarea id="note-text" style="min-height:130px" placeholder="e.g. Prefer a simpler approach; skip the caching layer for now."></textarea>
      <div class="row" style="margin-top:10px">
        <label class="row small"><input type="radio" name="nmode" value="queue" checked> Queue — deliver with the next event</label>
        <label class="row small"><input type="radio" name="nmode" value="now"> Now — wake the orchestrator</label>
        <span class="spacer"></span><button class="btn primary" id="note-add">Add note</button></div></div>
    <div id="notes-list">${list}</div></div>`;
  if (review) $('#note-add').textContent = 'Add note & reopen';
  $('#note-add').onclick = () => act(async () => {
    const { ws, mid } = S.route;
    if (S.mission.row.status === 'awaiting_review') {
      await api(`/api/missions/${ws}/${mid}/reopen`, { body: { text: $('#note-text').value } });
      await loadState();
    } else {
      await api(`/api/missions/${ws}/${mid}/notes`, { body: { text: $('#note-text').value, mode: $('input[name=nmode]:checked').value } });
    }
    $('#note-text').value = ''; await refreshMission(true);
  }, 'Note added');
  el.onclick = e => {
    const b = e.target.closest('[data-note]');
    if (!b) return;
    const text = $(`[data-note-text="${b.dataset.note}"]`)?.value;
    act(async () => {
      await api(`/api/notes/${b.dataset.note}/${b.dataset.v}`, { body: { mode: b.dataset.mode || 'queue', text, reopen: !!b.dataset.reopen } });
      if (b.dataset.reopen) await loadState();
      await refreshMission(true);
    }, b.dataset.reopen ? 'Note approved; mission reopened' : b.dataset.v === 'approve' ? 'Note approved' : 'Note rejected');
  };
}

// advisor chat
async function renderAdvisor() {
  const el = $('#m-tab');
  const advisors = S.mission.sessions.filter(s => s.kind === 'advisor');
  if (!S.adv.sid && advisors.length) S.adv.sid = advisors[advisors.length - 1].id;
  el.innerHTML = `<div class="chat"><div><button class="btn primary" id="adv-new" style="width:100%;margin-bottom:10px">New advisor chat</button>
      <div class="adv-list" id="adv-list"></div><p class="hint" style="margin-top:12px">Advisors read the mission state and answer questions. They cannot dispatch workers; they can propose notes for your approval.</p></div>
    <div class="card chat-pane"><div class="messages" id="adv-msgs"></div><div class="live-strip" id="adv-live"></div>
      <div class="composer"><textarea id="adv-input" placeholder="Ask about progress, risks, or propose a strategy change… (Ctrl+Enter to send)"></textarea>
        <button class="btn primary" id="adv-send">Send</button></div></div></div>`;
  renderAdvisorList();
  $('#adv-new').onclick = () => newAdvisorModal();
  $('#adv-send').onclick = sendAdvisor;
  $('#adv-input').onkeydown = e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) sendAdvisor(); };
  $('#adv-msgs').onclick = e => {
    const b = e.target.closest('[data-save-note]');
    if (b) saveNoteModal(S.adv.messages[Number(b.dataset.saveNote)].text);
  };
  await loadAdvisorMessages();
}
function renderAdvisorList() {
  const list = $('#adv-list');
  if (!list) return;
  const advisors = S.mission.sessions.filter(s => s.kind === 'advisor');
  list.innerHTML = advisors.slice().reverse().map(a => `<div class="adv-item ${a.id === S.adv.sid ? 'active' : ''}" data-adv="${a.id}">
      <div class="row"><b class="mono small">${a.id}</b><span class="spacer"></span>${a.live ? '<span class="thinking"></span>' : ''}</div>
      <div class="faint small">${esc(a.agent)} · ${esc(a.model || 'default')} · ${a.turns} turns</div></div>`).join('') || '<div class="muted small">No advisor chats yet.</div>';
  $$('[data-adv]', list).forEach(x => x.onclick = async () => { S.adv.sid = x.dataset.adv; S.adv.live = ''; renderAdvisorList(); await loadAdvisorMessages(); });
}
async function loadAdvisorMessages() {
  const box = $('#adv-msgs');
  if (!box) return;
  if (!S.adv.sid) { box.innerHTML = '<div class="empty">Start a new advisor chat to talk with a meta-agent about this mission.</div>'; setComposer(false); return; }
  const r = await api(`/api/advisors/${S.adv.sid}/messages`);
  S.adv.messages = r.messages; S.adv.busy = r.busy;
  box.innerHTML = r.messages.map((m, i) => m.role === 'user' ? `<div class="msg user">${esc(m.text)}</div>`
    : `<div class="msg assistant"><div class="md">${md(m.text)}</div><div class="msg-actions"><button class="btn small" data-save-note="${i}">Save as note</button></div></div>`).join('')
    + (r.busy ? '<div class="msg assistant"><span class="thinking">thinking</span></div>' : '')
    || '<div class="empty">Ask anything about this mission — e.g. “What is blocking the DoD right now?”</div>';
  box.scrollTop = box.scrollHeight;
  $('#adv-live').textContent = r.busy ? S.adv.live : '';
  setComposer(!r.busy);
}
function setComposer(enabled) {
  if (!$('#adv-send')) return;
  $('#adv-send').disabled = !enabled || !S.adv.sid;
  $('#adv-input').disabled = !S.adv.sid;
}
async function sendAdvisor() {
  const text = $('#adv-input').value.trim();
  if (!text || !S.adv.sid) return;
  await act(async () => {
    await api(`/api/advisors/${S.adv.sid}/messages`, { body: { text } });
    $('#adv-input').value = ''; S.adv.live = '';
    await loadAdvisorMessages();
  });
}
function newAdvisorModal() {
  const { ws, mid } = S.route;
  const conf = S.mission.config.advisor;
  const m = modal(`<div class="modal-head"><h2>New advisor chat</h2><button class="icon-btn" data-close>×</button></div>
    <p class="muted">A meta-agent session bound to this mission, for prompt-by-prompt questions.</p>
    <datalist id="models-claude">${MODELS.claude.map(x => `<option value="${x}">`).join('')}</datalist><datalist id="models-codex">${MODELS.codex.map(x => `<option value="${x}">`).join('')}</datalist>
    <div id="na-form">${agentFields('advisor', conf, { title: 'Advisor' })}</div>
    <div class="row" style="margin-top:12px"><span class="spacer"></span><button class="btn" data-close>Cancel</button><button class="btn primary" id="na-ok">Create</button></div>`);
  wireConfigForm($('#na-form', m));
  $('#na-ok', m).onclick = () => act(async () => {
    const r = await api(`/api/missions/${ws}/${mid}/advisors`, { body: readAgentRow($('.agent-row', m)) });
    closeModal(); S.adv.sid = r.id; await loadMission(); renderTabs(); await renderAdvisor();
  });
}
function saveNoteModal(text) {
  const { ws, mid } = S.route;
  const m = modal(`<div class="modal-head"><h2>Save as note for the orchestrator</h2><button class="icon-btn" data-close>×</button></div>
    <textarea id="sn-text" style="min-height:180px">${esc(text)}</textarea>
    <div class="row" style="margin-top:10px"><label class="row small"><input type="radio" name="snmode" value="queue" checked> Queue</label>
      <label class="row small"><input type="radio" name="snmode" value="now"> Deliver now</label><span class="spacer"></span>
      <button class="btn" data-close>Cancel</button><button class="btn primary" id="sn-ok">Save note</button></div>`);
  $('#sn-ok', m).onclick = () => act(async () => {
    await api(`/api/missions/${ws}/${mid}/notes`, { body: { text: $('#sn-text', m).value, mode: $('input[name=snmode]:checked', m).value } });
    closeModal(); await loadMission(); renderTabs();
  }, 'Note saved');
}

// spec & dod
function renderSpec() {
  const f = S.mission.files;
  const box = (name, hint) => `<div class="card"><div class="row"><h2 style="margin:0">${name}</h2><span class="dirty" data-dirty="${name}"></span><span class="spacer"></span>
      <button class="btn small primary" data-save="${name}">Save</button></div><p class="hint">${hint}</p>
      <textarea class="editor" data-file="${name}" spellcheck="false">${esc(f[name])}</textarea></div>`;
  $('#m-tab').innerHTML = `<div class="grid cols-2">${box('spec.md', 'Goal, context, inputs and constraints. Read-only for agents.')}
    ${box('dod.md', 'Definition of Done. Lines like <code>- verify: `pytest -q`</code> are executed on the integration branch before a <b>success</b> close; other criteria are judged by the orchestrator.')}</div>
    <p class="hint">Saving commits the file as <span class="chip human">human</span>; a running orchestrator is notified with the diff.</p>`;
  $$('[data-file]').forEach(t => t.oninput = () => { S.dirty[t.dataset.file] = true; $(`[data-dirty="${t.dataset.file}"]`).textContent = 'unsaved'; });
  $$('[data-save]').forEach(b => b.onclick = () => act(async () => {
    const name = b.dataset.save, { ws, mid } = S.route;
    await api(`/api/missions/${ws}/${mid}/files/${name}`, { method: 'PUT', body: { text: $(`[data-file="${name}"]`).value } });
    S.dirty[name] = false; $(`[data-dirty="${name}"]`).textContent = '';
    await loadMission();
  }, `${b.dataset.save} saved`));
}

// config
function renderConfig() {
  const M = S.mission;
  $('#m-tab').innerHTML = `<div class="card"><div class="row"><h2 style="margin:0">Mission configuration</h2><span class="spacer"></span>
      <button class="btn small" id="cf-raw">Edit raw YAML</button><button class="btn small primary" id="cf-save">Save</button></div>
    <p class="hint">Changes apply to the next session turn. Existing sessions keep their agent; new workers use the updated roles.</p>
    <div id="cf-form">${configForm(M.config)}</div>
    <textarea class="editor" id="cf-yaml" spellcheck="false" style="display:none">${esc(M.files['mission.yaml'])}</textarea></div>`;
  wireConfigForm($('#cf-form'));
  let raw = false;
  $('#cf-raw').onclick = () => {
    raw = !raw;
    $('#cf-form').style.display = raw ? 'none' : ''; $('#cf-yaml').style.display = raw ? '' : 'none';
    $('#cf-raw').textContent = raw ? 'Use form' : 'Edit raw YAML';
  };
  $('#cf-save').onclick = () => act(async () => {
    const { ws, mid } = S.route;
    let text = $('#cf-yaml').value;
    if (!raw) {
      const created = (M.files['mission.yaml'].match(/^created:\s*(.+)$/m) || [])[1];
      const obj = { title: M.config.title, ...(created ? { created: created.replace(/^['"]|['"]$/g, '') } : {}), ...readConfigForm($('#cf-form')) };
      text = toYaml(obj);
    }
    await api(`/api/missions/${ws}/${mid}/files/mission.yaml`, { method: 'PUT', body: { text } });
    await loadMission(); renderConfig();
  }, 'Configuration saved');
}

// git
async function renderGit() {
  const { ws, mid } = S.route;
  const g = await api(`/api/missions/${ws}/${mid}/git`);
  $('#m-tab').innerHTML = `<div class="grid cols-2" style="align-items:start">
    <div class="card"><h2>Branches</h2>${g.branches.length ? `<table class="t"><tr><th>Branch</th><th>Ahead</th><th>State</th><th>Last commit</th></tr>
      ${g.branches.map(b => `<tr><td class="mono small">${esc(b.branch)}</td><td>${b.ahead}</td><td>${b.branch.endsWith('/integration') ? pill('running', 'integration') : b.merged ? pill('ok', 'merged') : pill('pending', 'not merged')}</td>
      <td class="small">${esc(b.subject)}<div class="faint">${esc(b.when)}</div></td></tr>`).join('')}</table>` : '<div class="empty">No branches yet (created when the mission starts).</div>'}
      <p class="hint">Only you merge the integration branch into your main branch (open a PR from it).</p></div>
    <div class="card"><h2>Integration branch <span class="faint small">· commits not in ${esc(g.base)}</span></h2>${g.integration_log.length ? `<table class="t">${g.integration_log.map(c => `<tr><td class="mono small">${esc(c.hash)}</td><td class="small">${esc(c.subject)}</td><td class="faint small">${esc(c.when)}</td></tr>`).join('')}</table>` : '<div class="empty">Empty.</div>'}</div></div>
    <div class="card" style="margin-top:14px"><h2>Workspace audit log (this mission)</h2>${auditTable(g.workspace_log)}</div>`;
}

// ---------- infra ----------
function renderInfra() {
  const s = S.infra;
  if (!s) { main.innerHTML = '<div class="page"><div class="empty">Waiting for the first sample…</div></div>'; return; }
  const usedRam = (s.ram_total_gb - s.ram_free_gb).toFixed(1);
  main.innerHTML = `<div class="page"><div class="page-head"><div><div class="crumbs">host</div><h1>Infrastructure</h1></div>
      <span class="muted small">updated ${hhmm(s.ts)}</span></div>
    <div class="grid cols-4">
      <div class="card stat"><div class="label">CPU</div><div class="value">${s.cpu_pct.toFixed(0)}%</div>${meter(s.cpu_pct, 100)}<div class="sub">${s.cpus} cores · load ${s.load.join(' / ')}</div></div>
      <div class="card stat"><div class="label">RAM</div><div class="value">${usedRam} <span class="muted small">/ ${s.ram_total_gb} GB</span></div>${meter(s.ram_pct, 100)}<div class="sub">${s.ram_free_gb} GB available · swap ${s.swap_pct}%</div></div>
      <div class="card stat"><div class="label">Disk</div><div class="value">${s.disk_free_gb} <span class="muted small">GB free</span></div>${meter(s.disk_total_gb - s.disk_free_gb, s.disk_total_gb)}<div class="sub">of ${s.disk_total_gb} GB</div></div>
      <div class="card stat"><div class="label">GPU</div>${s.gpus.length ? s.gpus.map(g => `<div class="value" style="font-size:16px">${esc(g.name)}</div>${meter(g.mem_used_gb, g.mem_total_gb)}<div class="sub">VRAM ${g.mem_used_gb}/${g.mem_total_gb} GB · util ${g.util}%</div>`).join('')
        : '<div class="value" style="font-size:16px">None detected</div><div class="sub">workers run CPU-only; <code>--gpu</code> spawns are refused</div>'}</div>
    </div>
    <div class="card" style="margin-top:14px"><div class="row"><h2 style="margin:0">Last 10 minutes</h2><span class="spacer"></span>
      <span class="legend"><span><i style="background:var(--accent)"></i>CPU %</span><span><i style="background:var(--ok)"></i>RAM %</span>${s.gpus.length ? '<span><i style="background:var(--warn)"></i>VRAM %</span>' : ''}</span></div>
      <canvas class="chart" id="infra-chart"></canvas></div>
    <div class="card" style="margin-top:14px"><h2>Running sessions</h2>${s.sessions?.length ? `<table class="t"><tr><th>Session</th><th>Kind</th><th>Mission</th><th>Memory</th></tr>
      ${s.sessions.map(x => `<tr><td class="mono">${esc(x.id)}</td><td>${esc(x.kind)} ${esc(x.role || '')}</td><td>${x.ws ? `<a href="#/ws/${x.ws}/m/${x.mission}/sessions">${esc(x.ws)}/${esc(x.mission)}</a>` : ''}</td><td>${x.rss_gb} GB</td></tr>`).join('')}</table>`
        : '<div class="empty">No agent sessions running.</div>'}
      <p class="hint">Workers run in a systemd scope capped by <code>infra.worker_memory_max_gb</code> (loop.yaml); new workers are refused below <code>min_free_ram_gb</code>.</p></div></div>`;
  drawChart();
}
function drawChart() {
  const c = $('#infra-chart');
  if (!c) return;
  const dpr = window.devicePixelRatio || 1, w = c.clientWidth, h = c.clientHeight;
  c.width = w * dpr; c.height = h * dpr;
  const g = c.getContext('2d'); g.scale(dpr, dpr);
  const css = getComputedStyle(document.documentElement);
  const border = css.getPropertyValue('--border'), faint = css.getPropertyValue('--faint');
  g.strokeStyle = border; g.fillStyle = faint; g.font = '11px sans-serif'; g.lineWidth = 1;
  [0, 25, 50, 75, 100].forEach(v => { const y = h - 18 - (h - 28) * v / 100; g.beginPath(); g.moveTo(30, y); g.lineTo(w, y); g.stroke(); g.fillText(v, 4, y + 4); });
  const hist = S.history.slice(-200);
  const series = [[css.getPropertyValue('--accent'), x => x.cpu_pct], [css.getPropertyValue('--ok'), x => x.ram_pct]];
  if (hist.some(x => x.gpus?.length)) series.push([css.getPropertyValue('--warn'), x => x.gpus?.[0] ? 100 * x.gpus[0].mem_used_gb / x.gpus[0].mem_total_gb : 0]);
  series.forEach(([color, f]) => {
    g.strokeStyle = color; g.lineWidth = 2; g.beginPath();
    hist.forEach((x, i) => { const px = 30 + (w - 30) * i / Math.max(1, hist.length - 1), py = h - 18 - (h - 28) * Math.min(100, f(x)) / 100; i ? g.lineTo(px, py) : g.moveTo(px, py); });
    g.stroke();
  });
}

// ---------- agent CLIs ----------
async function renderAgents() {
  S.agents = await api('/api/agents');
  const card = name => {
    const op = S.agents.ops[name];
    const res = op?.result;
    const smoke = res?.smoke || (op?.op === 'smoke' ? res : null);
    return `<div class="card"><div class="row"><h2 style="margin:0">${name === 'claude' ? 'Claude Code' : 'Codex'}</h2><span class="spacer"></span>
        ${op?.state === 'running' ? `<span class="thinking">${op.op} running</span>` : ''}</div>
      <div class="mono" style="margin:8px 0 12px">${esc(S.agents.versions[name])}</div>
      <div class="row"><button class="btn" data-op="${name}/smoke" ${op?.state === 'running' ? 'disabled' : ''}>Smoke test</button>
        <button class="btn primary" data-op="${name}/update" ${op?.state === 'running' ? 'disabled' : ''}>Update to latest</button></div>
      ${smoke ? `<div style="margin-top:14px">${pill(smoke.ok ? 'ok' : 'failed', smoke.ok ? 'smoke test passed' : 'smoke test failed')}
        <span class="muted small">${smoke.seconds ?? ''}s</span>${smoke.error ? `<pre>${esc(smoke.error)}</pre>` : ''}</div>` : ''}
      ${res?.output ? `<h3 style="margin-top:14px">Update output</h3><pre style="max-height:260px">${esc(res.output)}</pre>` : ''}
      ${op?.state === 'failed' ? `<pre>${esc(res?.error)}</pre>` : ''}</div>`;
  };
  main.innerHTML = `<div class="page"><div class="page-head"><div><div class="crumbs">platform</div><h1>Agent CLIs</h1>
      <p class="muted small">Sessions use your existing logins. After an update a smoke test runs one tiny turn through the adapter to confirm flags and output parsing still work.</p></div></div>
    <div class="grid cols-2">${card('claude')}${card('codex')}</div></div>`;
  $$('[data-op]').forEach(b => b.onclick = () => act(async () => {
    await api(`/api/agents/${b.dataset.op}`, { body: {} }); await renderAgents();
  }, 'Started'));
}

// ---------- live events ----------
function handle(evt) {
  const r = S.route;
  const inMission = r.view === 'mission' && evt.ws === r.ws && evt.mission === r.mid;
  switch (evt.type) {
    case 'infra':
      S.infra = evt.data; S.history.push(evt.data); if (S.history.length > 600) S.history.shift();
      $('#mini-cpu').style.setProperty('--v', evt.data.cpu_pct + '%');
      $('#mini-ram').style.setProperty('--v', evt.data.ram_pct + '%');
      $('#nav-infra').title = `CPU ${evt.data.cpu_pct.toFixed(0)}% · RAM ${evt.data.ram_pct}%`;
      if (r.view === 'infra') renderInfra();
      break;
    case 'session_event':
      if (!inMission) break;
      appendLog(evt.session, evt.event);
      if (evt.session === S.adv.sid && ['tool', 'text', 'thinking'].includes(evt.event.type)) {
        S.adv.live = evt.event.type === 'tool' ? `▸ ${evt.event.name} ${evt.event.detail}` : firstLine(evt.event.text);
        if ($('#adv-live')) $('#adv-live').textContent = S.adv.live;
      }
      if (evt.event.type === 'turn' || evt.event.type === 'turn_end') scheduleRefresh();
      break;
    case 'advisor_message':
      if (inMission && evt.session === S.adv.sid) loadAdvisorMessages();
      if (inMission) scheduleRefresh();
      break;
    case 'mission_status':
      loadState();
      if (inMission) scheduleRefresh();
      break;
    case 'alert':
      toast(`${evt.mission}: ${evt.text}`, 'warn', 9000);
      loadState();
      if (inMission) scheduleRefresh();
      break;
    case 'agents':
      if (r.view === 'agents') renderAgents();
      break;
    case 'error':
      toast(evt.text, 'error', 9000);
      break;
    default:
      if (inMission) scheduleRefresh();
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
  await loadState();
  connect();
  render();
})();
