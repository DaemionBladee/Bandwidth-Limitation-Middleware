/* Bandwidth Engine. Native event streaming; no periodic page or table reloads. */
'use strict';
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const icon = name => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const storage = {
  get(key) { try { return localStorage.getItem(`bandwidth.${key}`); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(`bandwidth.${key}`, value); } catch { /* Private browser storage. */ } }
};
let csrf = '', snapshot = null, policies = [], users = [], timezones = ['UTC'];
let activeTab = 'overview', activeFilter = 'all', pageNumber = 1, chartMode = 'both', selectedDay = null;
let stream = null, streamVersion = 0, loggedIn = false, lastUpdated = null;
let detailUserId = null, detailTab = 'usage', detailHistory = null, historyRequest = 0;
let resetRequest = null, menuUserId = null, menuAnchor = null;
let displayZone = storage.get('timezone') || Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
const pageSize = 25;
const zoneSupport = new Map(), formatters = new Map(), offsets = new Map();
let offsetMinute = 0;
const formatter = (key, options) => { if (!formatters.has(key)) formatters.set(key, new Intl.DateTimeFormat(undefined, options)); return formatters.get(key); };
const supportedZone = zone => {
  if (!zoneSupport.has(zone)) { try { new Intl.DateTimeFormat('en', {timeZone: zone}); zoneSupport.set(zone,true); } catch { zoneSupport.set(zone,false); } }
  return zoneSupport.get(zone);
};
if (!supportedZone(displayZone)) displayZone = 'UTC';
const bytes = n => {
  const value = Number(n || 0);
  const scale = value >= 1e12 ? [1e12,'TB'] : value >= 1e9 ? [1e9,'GB'] : value >= 1e6 ? [1e6,'MB'] : value >= 1e3 ? [1e3,'KB'] : [1,'B'];
  return `${(value / scale[0]).toLocaleString(undefined, {maximumFractionDigits: 2})} ${scale[1]}`;
};
const gb = n => `${Number((n || 0) / 1e9).toLocaleString(undefined, {maximumFractionDigits: 2})} GB`;
const num = n => Number(n || 0).toLocaleString(undefined, {maximumFractionDigits: 2});
const date = (value, zone = displayZone, full = false) => value ? formatter(`date:${zone}:${full}`, {
  month: 'short', day: 'numeric', ...(full ? {year: 'numeric'} : {}), hour: '2-digit', minute: '2-digit',
  timeZone: supportedZone(zone) ? zone : 'UTC', hourCycle: 'h23'
}).format(new Date(value)) : '—';
const zoneOffset = zone => {
  const minute = Math.floor(Date.now()/60000); if (minute !== offsetMinute) { offsets.clear(); offsetMinute = minute; }
  if (!offsets.has(zone)) {
    try { offsets.set(zone,new Intl.DateTimeFormat('en', {timeZone: zone, timeZoneName: 'longOffset'}).formatToParts(new Date()).find(p => p.type === 'timeZoneName').value.replace('GMT', 'UTC').replace(/^UTC$/, 'UTC+00:00')); }
    catch { offsets.set(zone,''); }
  }
  return offsets.get(zone);
};
const zoneLabel = zone => `${zone.replaceAll('_', ' ')} · ${zoneOffset(zone)}`;
const badge = (text, kind = '') => `<span class="badge ${kind}">${esc(text)}</span>`;
const percent = user => user.quota_bytes ? Math.min(100, Math.max(0, user.used_bytes * 100 / user.quota_bytes)) : 0;
const userById = id => users.find(user => user.id === Number(id));
const hasAttention = user => Boolean(user.ise && user.ise.status !== 'confirmed');
const isLimited = user => user.ise?.desired.mode === 'LIMITED';
const isNormal = user => user.ise?.desired.mode === 'NORMAL';
const initials = username => username.split(/[@.\s_-]/).filter(Boolean).slice(0,2).map(s => s[0]).join('').toUpperCase() || 'S';
function setText(selector, value) { const element = $(selector); if (element.textContent !== String(value)) element.textContent = value; }
function showError(message) { $('#app-error').textContent = message; $('#app-error').hidden = !message; }
function toast(message, error = false) {
  const node = document.createElement('div'); node.className = `toast ${error ? 'error-toast' : ''}`;
  node.innerHTML = `${icon(error ? 'alert' : 'check')}<span>${esc(message)}</span><button aria-label="Dismiss notification">${icon('close')}</button>`;
  $('button', node).addEventListener('click', () => node.remove()); $('#toast-stack').append(node);
  setTimeout(() => node.remove(), 5500);
}
function openDialog(selector) {
  const dialog = $(selector), error = $('.form-error', dialog);
  if (error) error.textContent = '';
  if (!dialog.open) dialog.showModal();
}
function closeMenu() { const menu = $('#subscriber-actions'); if (menu.matches(':popover-open')) menu.hidePopover(); }
function closeSession() {
  loggedIn = false; disconnectStream(); csrf = ''; snapshot = null; users = []; policies = [];
  $$('dialog[open]').forEach(d => d.close()); closeMenu();
  $('#app-view').hidden = true; $('#login-view').hidden = false; $('#login-form').password.value = '';
}
async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {...options, headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf, ...options.headers}});
  if (!response.ok) {
    let message;
    try { message = (await response.json()).detail; } catch { message = `Request failed (${response.status})`; }
    if (Array.isArray(message)) message = message.map(e => `${e.loc.slice(1).join('.')}: ${e.msg}`).join('\n');
    if (response.status === 401 && path !== '/login') closeSession();
    const error = new Error(message || 'Request failed'); error.status = response.status; throw error;
  }
  return response.json();
}
function formHandler(selector, handler) {
  $(selector).addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.target, button = $('button[type="submit"]', form), label = button.innerHTML;
    const error = $('.form-error', form) || $('#login-error'); error.textContent = '';
    button.disabled = true; button.textContent = 'Please wait…';
    try { await handler(form); } catch (failure) { error.textContent = failure.message; error.scrollIntoView({block:'nearest'}); }
    finally { button.disabled = false; button.innerHTML = label; }
  });
}
// Reconcile only changed keyed nodes. Unchanged rows, focus, open audit details,
// scroll position, searches, and form controls survive incoming snapshots.
function patchList(parent, items, keyOf, html, signatureOf = item => JSON.stringify(item)) {
  const existing = new Map([...parent.children].map(node => [node.dataset.key, node]));
  const wanted = new Set();
  items.forEach((item, index) => {
    const key = String(keyOf(item)), signature = signatureOf(item); wanted.add(key);
    let node = existing.get(key);
    if (!node || node.dataset.signature !== signature) {
      const focusIndex = node?.contains(document.activeElement) ? $$('button', node).indexOf(document.activeElement) : -1;
      const template = document.createElement('template'); template.innerHTML = html(item).trim();
      const next = template.content.firstElementChild; next.dataset.key = key; next.dataset.signature = signature;
      if (node) { node.replaceWith(next); next.classList.add('updated-row'); }
      node = next;
      if (focusIndex >= 0) $$('button', node)[focusIndex]?.focus({preventScroll: true});
    }
    if (parent.children[index] !== node) parent.insertBefore(node, parent.children[index] || null);
  });
  [...parent.children].forEach(node => { if (!wanted.has(node.dataset.key)) node.remove(); });
}
function setConnection(state) {
  const pill = $('#live-status'); pill.className = `live-pill ${state === 'live' ? '' : state === 'paused' || state === 'connecting' ? 'connecting' : 'offline'}`;
  $('b', pill).textContent = {live: 'Live updates', connecting: 'Connecting', paused: 'Paused', offline: 'Reconnecting'}[state];
  $('#connection-warning').hidden = state !== 'offline';
  setText('#footer-live', {live: 'Updates connected', connecting: 'Connecting to updates', paused: 'Updates paused in background', offline: 'Reconnecting to updates'}[state]);
}
function disconnectStream() { if (stream) stream.close(); stream = null; }
function connectStream() {
  disconnectStream();
  if (!loggedIn || document.hidden) return;
  setConnection('connecting');
  const source = new EventSource(`/api/events?timezone=${encodeURIComponent(displayZone)}`); stream = source;
  source.addEventListener('dashboard', event => {
    if (stream !== source) return;
    try { streamVersion++; applySnapshot(JSON.parse(event.data)); setConnection('live'); }
    catch (error) { showError(`Could not display a live update: ${error.message}`); }
  });
  source.addEventListener('session-expired', () => { closeSession(); $('#login-error').textContent = 'Your session expired. Please sign in again.'; });
  source.onerror = async () => {
    if (stream !== source || !loggedIn) return;
    setConnection('offline');
    // EventSource does not expose HTTP status. A revoked session must stop retries.
    try { await api('/session'); } catch (error) { if (error.status !== 401) return; }
  };
}
async function syncSnapshot() {
  const revision = streamVersion, zone = displayZone;
  const data = await api(`/dashboard?timezone=${encodeURIComponent(zone)}`);
  if (loggedIn && zone === displayZone && revision === streamVersion) applySnapshot(data);
}
async function enterApp() {
  const session = await api('/session'); csrf = session.csrf || ''; loggedIn = true;
  setText('#admin-name', session.username); setText('#admin-avatar', initials(session.username));
  $('#login-view').hidden = true; $('#app-view').hidden = false; $('#loading-state').hidden = Boolean(snapshot);
  const [zones] = await Promise.all([api('/timezones'), syncSnapshot()]);
  timezones = zones.filter(supportedZone); if (!timezones.includes(displayZone)) displayZone = 'UTC';
  populateZoneSelect($('#display-timezone'), '', displayZone);
  setTab(location.hash.slice(1) || 'overview', false); updateClock(); connectStream();
}
function setTab(tab, push = true) {
  if (!['overview', 'subscribers', 'policies', 'audit'].includes(tab)) tab = 'overview';
  activeTab = tab;
  $('#overview-tab').hidden = !['overview','subscribers'].includes(tab);
  $('#policies-tab').hidden = tab !== 'policies'; $('#audit-tab').hidden = tab !== 'audit';
  for (const id of ['overview-metrics','overview-charts','overview-operations']) $(`#${id}`).hidden = tab !== 'overview';
  $$('[data-tab]').forEach(button => button.classList.toggle('active', button.dataset.tab === tab));
  const labels = {
    overview: ['Overview','Network overview','Your subscribers, usage, and policies. All in sync.'],
    subscribers: ['Subscribers','Subscribers','Find a subscriber, manage their policy, and keep usage in view.'],
    policies: ['Policy library','Policy library','The right allowance and speeds for every subscriber.'],
    audit: ['Change history','Change history','A clear record of what changed, when, and who changed it.']
  };
  setText('#breadcrumb', labels[tab][0]); setText('#page-title', labels[tab][1]); setText('#page-description', labels[tab][2]);
  if (push && location.hash !== `#${tab}`) history.pushState(null, '', `#${tab}`);
}
function applySnapshot(data) {
  const previousUser = detailUserId && userById(detailUserId), previousAudit = snapshot?.audit[0]?.id;
  snapshot = data; users = data.users; policies = data.policies; lastUpdated = new Date();
  $('#loading-state').hidden = true; $('#main-content').removeAttribute('aria-busy');
  const mock = data.status.ise_mode === 'mock';
  setText('#integration-title', mock ? 'Mock ISE · local simulation' : 'Live ISE adapter selected');
  setText('#integration-description', mock ? 'Mock confirmations are simulated. No live network enforcement.' : 'Only verified active-session readback confirms enforcement.');
  setText('#sidebar-mode', mock ? 'Local development' : 'ISE integration');
  setText('#sidebar-description', mock ? 'Mock integrations are clearly labeled throughout your workspace.' : 'Check each subscriber’s confirmed state for the applied result.');
  setText('#source-badge', `${data.status.accounting_source.toUpperCase()} ACCOUNTING`);
  setText('#accounting-mode', data.status.accounting_source.toUpperCase());
  $('#mock-usage').hidden = data.status.accounting_source !== 'mock';
  renderMetrics(); renderUsers(); renderPolicies(); renderAudit(); renderTraffic(); renderWorkers();
  if (detailUserId && $('#subscriber-dialog').open) {
    renderDetail();
    if (JSON.stringify(previousUser) !== JSON.stringify(userById(detailUserId)) || previousAudit !== data.audit[0]?.id) loadHistory(detailUserId);
  }
  updateClock();
}
function renderMetrics() {
  const upload = users.reduce((n,u) => n + u.upload_bytes, 0), download = users.reduce((n,u) => n + u.download_bytes, 0);
  setText('#stat-users', users.length); setText('#nav-user-count', users.length); setText('#subscriber-count', users.length);
  setText('#stat-assigned', `${users.filter(u => u.policy).length} assigned`);
  setText('#stat-usage', bytes(upload + download)); setText('#stat-upload', `↑ ${bytes(upload)}`); setText('#stat-download', `↓ ${bytes(download)}`);
  setText('#stat-limited', users.filter(isLimited).length); setText('#stat-pending', users.filter(hasAttention).length);
  setText('#stat-failed', `${users.filter(u => u.ise?.status === 'failed').length} failed`);
  const groups = [
    ['Normal', users.filter(u => isNormal(u) && !hasAttention(u)).length, '#28a592', 'normal'],
    ['Limited', users.filter(u => isLimited(u) && !hasAttention(u)).length, '#dda44f', 'limited'],
    ['Pending / failed', users.filter(hasAttention).length, '#8c86c7', 'pending'],
    ['Unassigned', users.filter(u => !u.ise).length, '#ccd6df', 'unassigned']
  ];
  let cursor = 0;
  const gradient = groups.map(([,count,color]) => { const start = cursor; cursor += count / Math.max(users.length,1) * 100; return `${color} ${start}% ${cursor}%`; }).join(',');
  $('#mode-donut').style.background = users.length ? `conic-gradient(${gradient})` : 'var(--line)';
  setText('#donut-total', users.length);
  patchList($('#mode-legend'), groups, row => row[3], ([label,count,,kind]) => `<div><i class="legend-${kind}"></i><span>${label}</span><b>${count}</b></div>`);
}
function filteredUsers() {
  const query = $('#subscriber-search').value.trim().toLowerCase();
  const match = user => ({all: true, normal: isNormal(user), limited: isLimited(user), attention: hasAttention(user), unassigned: !user.policy}[activeFilter]);
  const list = users.filter(user => match(user) && `${user.username} ${user.policy || ''}`.toLowerCase().includes(query));
  const sort = $('#subscriber-sort').value;
  return list.sort((a,b) => sort === 'usage' ? b.used_bytes - a.used_bytes || a.username.localeCompare(b.username) : sort === 'reset' ? (a.next_reset ? new Date(a.next_reset).getTime() : Infinity) - (b.next_reset ? new Date(b.next_reset).getTime() : Infinity) || a.username.localeCompare(b.username) : a.username.localeCompare(b.username));
}
function renderUserRow(user) {
  const state = user.ise, mode = state?.desired.mode;
  const enforcement = state ? badge(`${state.adapter === 'mock' ? 'MOCK · ' : ''}${state.status}`, state.status === 'failed' ? 'danger' : state.status === 'pending' ? 'warning' : 'neutral') : badge('Not assigned', 'neutral');
  const oldConfirmation = state?.confirmed && state.confirmed_generation !== state.generation ? `<span class="state-note">Last ${esc(state.confirmed_adapter || 'unknown')} · ${esc(state.confirmed.mode)}</span>` : '';
  return `<tr><td><div class="subscriber-identity"><span class="avatar">${esc(initials(user.username))}</span><div><button class="subscriber-name" data-detail="${user.id}">${esc(user.username)}</button><small>${esc(user.policy || 'No policy assigned')}${user.revision ? ` · v${user.revision}` : ''}${user.policy && !user.enabled ? ' · disabled' : ''}</small>${user.pending.length ? '<span class="state-note">Policy change scheduled</span>' : ''}</div></div></td><td><div class="usage-amount"><span>${bytes(user.used_bytes)}</span><small>/ ${user.quota_bytes ? bytes(user.quota_bytes) : '—'}</small></div><div class="meter ${percent(user) >= 100 ? 'over' : ''}"><span></span></div><small>${user.remaining_bytes !== null ? `${bytes(user.remaining_bytes)} remaining` : 'No active allowance'}</small></td><td>${mode ? badge(mode, mode === 'LIMITED' ? 'warning' : '') : '—'}${state ? `<span class="speed-values">↓ ${num(state.desired.download_kbps / 1000)} / ↑ ${num(state.desired.upload_kbps / 1000)} Mbps</span>` : ''}</td><td title="${esc(state?.last_error || '')}">${enforcement}${oldConfirmation}${state?.last_error ? `<span class="state-note">${esc(state.last_error)}</span>` : ''}</td><td class="next-reset" title="Policy timezone: ${esc(user.timezone || '—')}">${date(user.next_reset)}<small data-countdown="${esc(user.next_reset || '')}">—</small></td><td><div class="row-actions"><button class="text-button" data-detail="${user.id}">History</button><button class="icon-button" data-menu="${user.id}" aria-label="Actions for ${esc(user.username)}">${icon('dots')}</button></div></td></tr>`;
}
function renderUsers() {
  const counts = {all: users.length, normal: users.filter(isNormal).length, limited: users.filter(isLimited).length, attention: users.filter(hasAttention).length, unassigned: users.filter(u => !u.policy).length};
  $$('[data-filter]').forEach(button => { $('span',button).textContent = counts[button.dataset.filter]; button.classList.toggle('active',button.dataset.filter === activeFilter); button.setAttribute('aria-pressed',button.dataset.filter === activeFilter); });
  const list = filteredUsers(), pages = Math.max(1,Math.ceil(list.length / pageSize)); pageNumber = Math.min(pageNumber,pages);
  const shown = list.slice((pageNumber-1)*pageSize,pageNumber*pageSize);
  patchList($('#users-table'), shown, u => u.id, renderUserRow, u => JSON.stringify([u,displayZone]));
  shown.forEach(user => { const row = $(`#users-table tr[data-key="${user.id}"]`); $('.meter span',row).style.width = `${percent(user)}%`; });
  $('#users-empty').hidden = users.length !== 0; $('#search-empty').hidden = !users.length || list.length !== 0;
  setText('#pagination-label', list.length ? `${(pageNumber-1)*pageSize+1}–${Math.min(pageNumber*pageSize,list.length)} of ${list.length} subscribers` : '0 subscribers');
  setText('#page-number', `${pageNumber} / ${pages}`); $('#previous-page').disabled = pageNumber <= 1; $('#next-page').disabled = pageNumber >= pages;
  updateCountdowns();
}
function renderTraffic() {
  const rows = snapshot.traffic, amount = row => (chartMode !== 'download' ? row.upload_bytes : 0) + (chartMode !== 'upload' ? row.download_bytes : 0);
  const max = Math.max(...rows.map(amount),1), total = rows.reduce((n,row) => n + amount(row),0);
  setText('#traffic-total', bytes(total)); setText('#chart-zone', displayZone); $('#traffic-empty').hidden = total > 0;
  $$('[data-chart]').forEach(button => { button.classList.toggle('active', button.dataset.chart === chartMode); button.setAttribute('aria-pressed', button.dataset.chart === chartMode); });
  patchList($('#traffic-bars'), rows, row => row.day, row => {
    const day = new Date(`${row.day}T12:00:00Z`), label = new Intl.DateTimeFormat(undefined,{month:'short',day:'numeric',timeZone:'UTC'}).format(day);
    return `<button class="bar-column ${selectedDay === row.day ? 'selected' : ''}" data-day="${row.day}" title="${esc(label)} · ${bytes(amount(row))}" aria-label="${esc(label)}: ${bytes(amount(row))}"><span class="bar-space"><span class="bar-fill"><span class="bar-upload"></span><span class="bar-download"></span></span></span><small>${label}</small></button>`;
  }, row => JSON.stringify([row,chartMode,selectedDay]));
  rows.forEach(row => {
    const node = $(`#traffic-bars [data-key="${row.day}"]`), sum = amount(row);
    $('.bar-fill',node).style.height = `${sum / max * 100}%`;
    $('.bar-upload',node).style.height = `${sum && chartMode !== 'download' ? row.upload_bytes / sum * 100 : 0}%`;
    $('.bar-download',node).style.height = `${sum && chartMode !== 'upload' ? row.download_bytes / sum * 100 : 0}%`;
  });
  $('#traffic-bars').setAttribute('aria-label', `Daily ${chartMode === 'both' ? 'combined' : chartMode} accounting over seven days: ${bytes(total)}, timezone ${displayZone}`);
  const selected = rows.find(row => row.day === selectedDay);
  setText('#chart-selection', selected ? `${selected.day} · Upload ${bytes(selected.upload_bytes)} · Download ${bytes(selected.download_bytes)} · ${displayZone}` : 'Recorded transfer includes earlier allowances. A quota reset preserves this history.');
}
function renderPolicies() {
  const query = $('#policy-search').value.trim().toLowerCase(), list = policies.filter(p => `${p.spec.name} ${p.spec.timezone}`.toLowerCase().includes(query));
  setText('#policy-count', `${policies.length} ${policies.length === 1 ? 'policy' : 'policies'}`);
  patchList($('#policy-grid'), list, p => p.id, policy => {
    const spec = policy.spec, schedule = spec.period_type === 'monthly' ? `Day ${spec.monthly_day} · clamps to month end` : spec.period_type === 'weekly' ? ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'][spec.weekly_day] : spec.period_type === 'custom' ? `${date(spec.custom_start,spec.timezone)} → ${date(spec.custom_end,spec.timezone)}` : 'Every day';
    return `<article class="policy-card"><div class="card-top">${badge(spec.enabled ? 'ENABLED' : 'DISABLED',spec.enabled ? '' : 'neutral')}<small>Version ${policy.revision} · #${policy.id}</small></div><h3>${esc(spec.name)}</h3><div class="quota">${gb(spec.quota_bytes)}<small>/ ${esc(spec.period_type)}</small></div><div class="profile-row"><span><b></b>Normal ↓ / ↑</span><strong>${num(spec.normal_download_kbps/1000)} / ${num(spec.normal_upload_kbps/1000)} Mbps</strong></div><div class="profile-row limited"><span><b></b>Limited ↓ / ↑</span><strong>${num(spec.limited_download_kbps/1000)} / ${num(spec.limited_upload_kbps/1000)} Mbps</strong></div><p class="schedule">${icon('globe')}<span>${esc(spec.timezone)} · ${esc(spec.reset_time.slice(0,5))}<br>${esc(schedule)}</span></p><button class="secondary" data-edit="${policy.id}">Edit policy ${icon('arrow-right')}</button></article>`;
  });
  if (!list.length) $('#policy-grid').innerHTML = `<div class="empty-state"><span class="empty-icon">${icon('layers')}</span><h3>${policies.length ? 'No matching policies' : 'Create your first policy'}</h3><p>${policies.length ? 'Try a different policy name or timezone.' : 'Define a quota, speed profiles, and a reset schedule.'}</p><button class="secondary" data-new-policy>New policy</button></div>`;
}
const actionNames = {'policy.create':'Policy created','policy.edit':'Policy updated','user.create':'Subscriber added','assignment.change':'Assignment changed','usage.reset':'Subscriber usage reset','enforcement.retry':'Enforcement reconciliation requested','mock.accounting':'Mock accounting submitted','admin.create':'Administrator created','assignment.create':'Assignment created'};
function renderAudit() {
  const query = $('#audit-search').value.trim().toLowerCase(), rows = snapshot.audit.filter(a => `${a.actor} ${a.action} ${a.entity} ${a.after?.reason || ''}`.toLowerCase().includes(query));
  patchList($('#audit-list'), rows, a => a.id, a => `<article class="audit-item"><span class="audit-icon">${icon(a.action === 'usage.reset' ? 'reset' : a.action.startsWith('policy') ? 'layers' : 'history')}</span><div><strong>${esc(actionNames[a.action] || a.action)}</strong><p>${esc(a.actor)} · ${esc(a.entity)}${a.after?.reason ? ` · ${esc(a.after.reason)}` : ''}</p><details><summary>View change</summary><pre>${esc(JSON.stringify({before:a.before,after:a.after},null,2))}</pre></details></div><time datetime="${esc(a.created_at)}">${date(a.created_at)}</time></article>`, a => JSON.stringify([a,displayZone]));
  if (!rows.length) $('#audit-list').innerHTML = '<div class="empty-state"><h3>No matching changes</h3><p>Administrative actions will appear here as they happen.</p></div>';
}
function renderWorkers() {
  const labels = {ingest:['Accounting ingestion','database'],evaluate:['Quota decisions','gauge'],rollover:['Period rollover','reset'],reconcile:['Enforcement checks','shield']};
  const rows = Object.entries(labels).map(([kind,[label,symbol]]) => {
    const jobs = snapshot.status.jobs.filter(j => j.kind === kind), error = snapshot.status.worker_errors.some(e => e.kind === kind);
    return {kind,label,symbol,state: error ? 'Needs attention' : jobs.some(j => j.status === 'running') ? 'Processing' : jobs.length ? 'Scheduled' : 'Not started',error};
  });
  patchList($('#jobs'),rows,row => row.kind,row => `<div class="job-row"><span class="job-icon">${icon(row.symbol)}</span><span>${row.label}</span>${badge(row.state,row.error ? 'danger' : 'neutral')}</div>`);
  setText('#worker-errors',snapshot.status.worker_errors.map(j => `${j.kind}: ${j.error}`).join('\n'));
}
function updateCountdowns() {
  $$('[data-countdown]').forEach(node => {
    const value = node.dataset.countdown;
    if (!value) { node.textContent = 'No upcoming boundary'; return; }
    const seconds = Math.max(0,Math.floor((new Date(value)-new Date())/1000));
    const text = seconds === 0 ? 'Boundary reached · processing' : seconds >= 86400 ? `in ${Math.floor(seconds/86400)}d ${Math.floor(seconds%86400/3600)}h` : seconds >= 3600 ? `in ${Math.floor(seconds/3600)}h ${Math.floor(seconds%3600/60)}m` : seconds >= 60 ? `in ${Math.floor(seconds/60)}m ${seconds%60}s` : `in ${seconds}s`;
    if (node.textContent !== text) node.textContent = text;
  });
}
function updateClock() {
  setText('#display-zone-label', displayZone); setText('#footer-zone', displayZone); setText('#table-zone', displayZone);
  if (lastUpdated) setText('#last-updated', `Updated ${new Intl.DateTimeFormat(undefined,{hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23',timeZone:displayZone}).format(lastUpdated)}`);
  if (snapshot) {
    const checkpoint = snapshot.status.checkpoints.find(c => c.source === snapshot.status.accounting_source);
    setText('#checkpoint',checkpoint ? `Last ingestion · ${date(checkpoint.updated_at)}` : 'No checkpoint yet · start the background worker');
  }
  if ($('#policy-dialog').open) setText('#policy-zone-preview', `Policy time: ${date(new Date().toISOString(), $('#policy-timezone').value)} · ${zoneOffset($('#policy-timezone').value)}`);
  if ($('#preferences-dialog').open) setText('#display-zone-preview', date(new Date().toISOString(),$('#display-timezone').value,true));
  updateCountdowns();
}
function populateZoneSelect(select, query = '', selected = select.value || displayZone) {
  const search = query.trim().toLowerCase(), favorites = ['UTC','Asia/Yangon','Asia/Bangkok','Asia/Singapore','Asia/Kolkata','Europe/London','America/New_York'];
  const matches = zone => !search || zone.toLowerCase().replaceAll('_',' ').includes(search.replaceAll('_',' ')) || zoneOffset(zone).toLowerCase().includes(search);
  const list = [...new Set([selected,...favorites,...timezones])].filter(zone => timezones.includes(zone) && (zone === selected || matches(zone)));
  select.replaceChildren(...list.map(zone => { const option = document.createElement('option'); option.value = zone; option.textContent = zoneLabel(zone); return option; }));
  select.value = selected; updateClock();
}
function editPolicy(id) {
  const form = $('#policy-form'); form.reset(); form.elements.id.value = ''; form.revision.value = ''; $('#policy-zone-search').value = '';
  const policy = policies.find(p => p.id === id);
  if (policy) {
    const spec = policy.spec;
    for (const name of ['name','period_type','reset_time','monthly_day','weekly_day','custom_start','custom_end','repeat_interval_seconds','expiry_action']) form.elements[name].value = spec[name] ?? '';
    form.quota_gb.value = spec.quota_bytes / 1e9;
    for (const name of ['normal_download','normal_upload','limited_download','limited_upload']) form.elements[name].value = spec[`${name}_kbps`] / 1000;
    form.enabled.checked = spec.enabled; form.elements.id.value = id; form.revision.value = policy.revision;
  }
  populateZoneSelect(form.timezone,'',policy?.spec.timezone || displayZone);
  setText('#policy-dialog-title',policy ? 'Edit policy' : 'Create policy'); scheduleFields(); openDialog('#policy-dialog'); updateClock();
}
function scheduleFields() {
  const custom = $('#policy-form').period_type.value === 'custom';
  $('#monthly-field').hidden = $('#policy-form').period_type.value !== 'monthly'; $('#weekly-field').hidden = $('#policy-form').period_type.value !== 'weekly';
  $$('[data-custom]').forEach(node => node.hidden = !custom); $$('[data-calendar]').forEach(node => node.hidden = custom);
}
function newUser() { $('#user-form').reset(); openDialog('#user-dialog'); }
function assignUser(id) {
  const user = userById(id); if (!user) return;
  if (!policies.length) { toast('Create a policy before assigning a subscriber.',true); editPolicy(null); return; }
  const form = $('#assignment-form'); form.user_id.value = id; setText('#assignment-user',user.username);
  form.policy_id.replaceChildren(...policies.map(policy => { const option = document.createElement('option'); option.value = policy.id; option.textContent = policy.spec.name; return option; }));
  if (user.policy_id) form.policy_id.value = user.policy_id;
  form.enabled.checked = user.policy ? user.enabled : true; form.application.value = 'immediate';
  $('option[value="next_period"]',form.application).disabled = !user.policy;
  assignmentPreview(); openDialog('#assignment-dialog');
}
function assignmentPreview() {
  const policy = policies.find(p => p.id === Number($('#assignment-form').policy_id.value));
  setText('#assignment-preview',policy ? `${gb(policy.spec.quota_bytes)} / ${policy.spec.period_type} · ${policy.spec.timezone} · ${policy.spec.normal_download_kbps/1000}/${policy.spec.normal_upload_kbps/1000} Mbps normal` : '');
}
function showMenu(id, anchor) {
  closeMenu(); menuUserId = Number(id); menuAnchor = anchor; const user = userById(id), menu = $('#subscriber-actions');
  $('[data-action="reset"]',menu).disabled = !user?.period_id;
  $('[data-action="reconcile"]',menu).disabled = !user?.ise;
  menu.showPopover(); const rect = anchor.getBoundingClientRect(), height = menu.getBoundingClientRect().height;
  menu.style.left = `${Math.max(10,Math.min(innerWidth-222,rect.right-212))}px`;
  menu.style.top = `${rect.bottom+height+8 < innerHeight ? rect.bottom+5 : Math.max(10,rect.top-height-5)}px`;
  $('[role=menuitem]:not(:disabled)',menu)?.focus({preventScroll:true});
}
async function reconcile(id) {
  try { await api(`/users/${id}/reconcile`,{method:'POST'}); toast('Enforcement reconciliation queued'); await syncSnapshot(); }
  catch (error) { toast(error.message,true); }
}
function openReset(id) {
  const user = userById(id); if (!user?.period_id) { toast('An active quota allowance is required to reset usage.',true); return; }
  resetRequest = {userId:user.id,expected_period_id:user.period_id,request_id:crypto.randomUUID()};
  $('#reset-form').reset(); setText('#reset-user',user.username);
  $('#reset-summary').innerHTML = `<div><strong>${bytes(user.used_bytes)}</strong><small> of ${bytes(user.quota_bytes)} used</small></div>${icon('arrow-right')}<div><strong>0 B</strong><small> fresh allowance</small></div>`;
  openDialog('#reset-dialog');
}
async function showHistory(id) {
  detailUserId = Number(id); detailTab = 'usage'; detailHistory = null; renderDetail();
  $('#history-content').innerHTML = '<p class="history-note">Loading subscriber history…</p>';
  openDialog('#subscriber-dialog'); await loadHistory(detailUserId);
}
function renderDetail() {
  const user = userById(detailUserId); if (!user) return;
  setText('#history-title',user.username); setText('#detail-policy',user.policy ? `${user.policy} · v${user.revision}${user.pending.length ? ' · change scheduled' : ''}` : 'No policy assigned');
  const state = user.ise;
  $('#detail-summary').innerHTML = `<div class="detail-usage"><div class="detail-usage-head"><div><strong>${bytes(user.used_bytes)}</strong><small>/ ${user.quota_bytes ? bytes(user.quota_bytes) : '—'}</small></div>${state ? badge(state.desired.mode,isLimited(user) ? 'warning' : '') : badge('Unassigned','neutral')}</div><div class="meter ${percent(user)>=100?'over':''}"><span></span></div><p><span>↑ ${bytes(user.upload_bytes)} uploaded</span><span>↓ ${bytes(user.download_bytes)} downloaded</span></p></div><div class="detail-keyvals"><div><small>Remaining allowance</small><strong>${user.remaining_bytes !== null ? bytes(user.remaining_bytes) : 'No active allowance'}</strong></div><div><small>Next reset · ${esc(user.timezone || displayZone)}</small><strong>${date(user.next_reset,user.timezone || displayZone)}</strong></div><div><small>Requested profile</small><strong>${state ? `${num(state.desired.download_kbps/1000)} / ${num(state.desired.upload_kbps/1000)} Mbps` : '—'}</strong></div><div><small>Confirmed enforcement</small>${state?.confirmed ? badge(`${state.confirmed_adapter === 'mock' ? 'MOCK · ' : ''}${state.confirmed.mode}`,state.confirmed_generation !== state.generation ? 'warning' : 'neutral') : '<strong>Not confirmed</strong>'}<span class="state-note">${state ? esc(state.status) : 'Assign a policy to begin'}</span></div></div>${state?.last_error ? `<p class="detail-error">${esc(state.last_error)}</p>` : ''}`;
  $('.meter span',$('#detail-summary')).style.width = `${percent(user)}%`;
  $('#detail-reset').disabled = !user.period_id; $('#detail-reconcile').disabled = !state;
}
async function loadHistory(id) {
  const revision = ++historyRequest;
  try {
    const data = await api(`/users/${id}/history`);
    if (revision !== historyRequest || id !== detailUserId) return;
    detailHistory = data; renderHistory();
  } catch (error) { if (revision === historyRequest) $('#history-content').innerHTML = `<p class="error">${esc(error.message)}</p>`; }
}
function renderHistory() {
  if (!detailHistory) return;
  const data = detailHistory;
  $$('[data-detail-tab]').forEach(button => { button.classList.toggle('active',button.dataset.detailTab === detailTab); button.setAttribute('aria-pressed',button.dataset.detailTab === detailTab); });
  if (detailTab === 'enforcement') {
    $('#history-content').innerHTML = `<h3>Enforcement attempts</h3><p class="history-note">Requested and confirmed modes remain separate. Mock responses are simulations.</p>${data.attempts.length ? data.attempts.map(attempt => `<article class="attempt-card"><div>${badge(attempt.outcome,attempt.outcome==='failed'?'danger':'neutral')}<time>${date(attempt.created_at)}</time></div><p>${esc(attempt.adapter.toUpperCase())} · ${esc(attempt.mode)} · generation ${attempt.generation}</p>${attempt.error ? `<p class="error">${esc(attempt.error)}</p>` : ''}</article>`).join('') : '<p class="history-note">No enforcement attempts yet.</p>'}`;
  } else {
    $('#history-content').innerHTML = `<h3>Usage resets</h3>${data.resets.length ? data.resets.map(reset => `<article class="reset-card"><strong>Fresh allowance started</strong><time>${date(reset.effective_at)}</time><p>${esc(reset.actor)} · ${esc(reset.reason)}</p></article>`).join('') : '<p class="history-note">No manual resets. Scheduled allowances renew automatically.</p>'}<h3>Retained quota periods</h3><p class="history-note">Earlier allowances are preserved. Policy edits can retain overlapping views; these rows are not additive.</p>${data.periods.length ? data.periods.map(period => `<article class="period-card"><div><strong>${bytes(period.used_bytes)} <small>/ ${bytes(period.quota_bytes)}</small></strong>${badge(`Policy version ${period.version_id}`,'neutral')}</div><p><time>${date(period.starts_at)}</time> → <time>${date(period.ends_at)}</time><br>↑ ${bytes(period.upload_bytes)} · ↓ ${bytes(period.download_bytes)}</p></article>`).join('') : '<p class="history-note">No quota periods yet.</p>'}`;
  }
}
function setTheme(theme) {
  document.documentElement.dataset.theme = theme; storage.set('theme',theme);
  $('#theme-toggle').innerHTML = icon(theme === 'dark' ? 'sun' : 'moon');
  $('#theme-toggle').setAttribute('aria-label',`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`);
}
setTheme(storage.get('theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));
formHandler('#login-form',async form => {
  const result = await api('/login',{method:'POST',body:JSON.stringify({username:form.username.value,password:form.password.value})});
  csrf = result.csrf; form.password.value = ''; await enterApp();
});
formHandler('#policy-form',async form => {
  const values = Object.fromEntries(new FormData(form)), custom = values.period_type === 'custom';
  const spec = {name:values.name,enabled:form.enabled.checked,quota_bytes:Math.round(Number(values.quota_gb)*1e9),timezone:values.timezone,period_type:values.period_type,reset_time:values.reset_time || '00:00',monthly_day:Number(values.monthly_day)||1,weekly_day:Number(values.weekly_day)||0,custom_start:custom ? values.custom_start || null : null,custom_end:custom ? values.custom_end || null : null,repeat_interval_seconds:custom && values.repeat_interval_seconds ? Number(values.repeat_interval_seconds) : null,expiry_action:custom ? values.expiry_action || null : null};
  for (const name of ['normal_download','normal_upload','limited_download','limited_upload']) spec[`${name}_kbps`] = Math.round(Number(values[name])*1000);
  await api(values.id ? `/policies/${values.id}` : '/policies',{method:values.id ? 'PUT' : 'POST',body:JSON.stringify(values.id ? {spec,expected_revision:Number(values.revision),application:values.application} : spec)});
  $('#policy-dialog').close(); toast('Policy saved. Your change is recorded in history.'); await syncSnapshot();
});
formHandler('#user-form',async form => {
  const user = await api('/users',{method:'POST',body:JSON.stringify({username:form.username.value})});
  $('#user-dialog').close(); await syncSnapshot(); toast('Subscriber added'); if (policies.length) assignUser(user.id);
});
formHandler('#assignment-form',async form => {
  await api(`/users/${form.user_id.value}/assignment`,{method:'PUT',body:JSON.stringify({policy_id:Number(form.policy_id.value),enabled:form.enabled.checked,application:form.application.value})});
  $('#assignment-dialog').close(); toast('Policy assignment saved'); await syncSnapshot();
});
formHandler('#usage-form',async form => {
  await api('/mock/accounting',{method:'POST',body:JSON.stringify({record_id:form.record_id.value,username:form.username.value,session_id:form.session_id.value,observed_at:new Date().toISOString(),kind:'delta',upload:Math.round(Number(form.upload.value)*1e9),download:Math.round(Number(form.download.value)*1e9),unit:'bytes'})});
  $('#usage-dialog').close(); toast('Mock usage queued. The dashboard will update automatically.');
});
formHandler('#reset-form',async form => {
  const request = {...resetRequest,reason:form.reason.value.trim()};
  if (!request.userId) return;
  const result = await api(`/users/${request.userId}/reset-usage`,{method:'POST',body:JSON.stringify({expected_period_id:request.expected_period_id,request_id:request.request_id,reason:request.reason})});
  const index = users.findIndex(u => u.id === result.user.id); if (index >= 0) users[index] = result.user;
  $('#reset-dialog').close(); resetRequest = null; renderMetrics(); renderUsers(); if ($('#subscriber-dialog').open) renderDetail();
  toast('Fresh allowance started. Earlier usage is preserved.'); await syncSnapshot();
});
formHandler('#preferences-form',async () => {
  const zoneChanged = displayZone !== $('#display-timezone').value;
  setTheme($('#appearance-select').value);
  displayZone = $('#display-timezone').value; storage.set('timezone',displayZone); selectedDay = null;
  $('#preferences-dialog').close();
  if (zoneChanged) { disconnectStream(); try { await syncSnapshot(); } finally { connectStream(); } }
  toast(`Display timezone set to ${displayZone}`);
});
$('#policy-form').period_type.addEventListener('change',scheduleFields);
$('#policy-zone-search').addEventListener('input',event => populateZoneSelect($('#policy-timezone'),event.target.value));
$('#display-zone-search').addEventListener('input',event => populateZoneSelect($('#display-timezone'),event.target.value));
$('#policy-timezone').addEventListener('change',updateClock); $('#display-timezone').addEventListener('change',updateClock);
$('#assignment-form').policy_id.addEventListener('change',assignmentPreview);
$('#subscriber-search').addEventListener('input',() => { pageNumber = 1; renderUsers(); });
$('#subscriber-sort').addEventListener('change',() => { pageNumber = 1; renderUsers(); });
$('#policy-search').addEventListener('input',renderPolicies); $('#audit-search').addEventListener('input',renderAudit);
$('#previous-page').addEventListener('click',() => { pageNumber--; renderUsers(); }); $('#next-page').addEventListener('click',() => { pageNumber++; renderUsers(); });
$('#clear-filters').addEventListener('click',() => { $('#subscriber-search').value = ''; activeFilter = 'all'; pageNumber = 1; renderUsers(); });
$('#preferences').addEventListener('click',() => { $('#display-zone-search').value = ''; $('#appearance-select').value = document.documentElement.dataset.theme; populateZoneSelect($('#display-timezone'),'',displayZone); openDialog('#preferences-dialog'); updateClock(); });
$('#theme-toggle').addEventListener('click',() => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'));
$('#new-policy').addEventListener('click',() => editPolicy(null)); $('#new-user').addEventListener('click',newUser); $('#empty-add-user').addEventListener('click',newUser);
$('#reconnect').addEventListener('click',connectStream);
$('#mock-usage').addEventListener('click',() => {
  if (!users.length) { toast('Add a subscriber before submitting accounting.',true); newUser(); return; }
  const form = $('#usage-form'); form.reset();
  form.username.replaceChildren(...users.map(user => { const option = document.createElement('option'); option.textContent = user.username; return option; }));
  form.record_id.value = crypto.randomUUID(); openDialog('#usage-dialog');
});
$('#detail-assign').addEventListener('click',() => assignUser(detailUserId)); $('#detail-reset').addEventListener('click',() => openReset(detailUserId)); $('#detail-reconcile').addEventListener('click',() => reconcile(detailUserId));
$('#logout').addEventListener('click',async () => { try { await api('/logout',{method:'POST'}); closeSession(); } catch (error) { toast(error.message,true); } });
$('#toggle-password').addEventListener('click',() => { const input = $('#login-form').password; input.type = input.type === 'password' ? 'text' : 'password'; $('#toggle-password').setAttribute('aria-label',input.type === 'password' ? 'Show password' : 'Hide password'); });
document.addEventListener('click',event => {
  const button = event.target.closest('button'); if (!button) return;
  if (button.classList.contains('close-dialog')) button.closest('dialog').close();
  if (button.dataset.tab) setTab(button.dataset.tab);
  if (button.dataset.filter) { activeFilter = button.dataset.filter; pageNumber = 1; renderUsers(); }
  if (button.dataset.chart) { chartMode = button.dataset.chart; renderTraffic(); }
  if (button.dataset.day) { selectedDay = selectedDay === button.dataset.day ? null : button.dataset.day; renderTraffic(); }
  if (button.dataset.edit) editPolicy(Number(button.dataset.edit));
  if (button.hasAttribute('data-new-policy')) editPolicy(null);
  if (button.dataset.detail) showHistory(Number(button.dataset.detail));
  if (button.dataset.menu) showMenu(Number(button.dataset.menu),button);
  if (button.dataset.detailTab) { detailTab = button.dataset.detailTab; renderHistory(); }
  if (button.dataset.action) { closeMenu(); (menuAnchor?.isConnected ? menuAnchor : $(`[data-menu="${menuUserId}"]`))?.focus({preventScroll:true}); ({assign:assignUser,history:showHistory,reconcile,reset:openReset})[button.dataset.action]?.(menuUserId); }
});
$('#subscriber-actions').addEventListener('keydown',event => {
  const items = $$('[role=menuitem]:not(:disabled)',$('#subscriber-actions'));
  const index = items.indexOf(document.activeElement);
  if (['ArrowDown','ArrowUp','Home','End'].includes(event.key)) {
    event.preventDefault();
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length-1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
    items[next]?.focus();
  } else if (event.key === 'Escape' || event.key === 'Tab') {
    if (event.key === 'Escape') event.preventDefault();
    closeMenu(); (menuAnchor?.isConnected ? menuAnchor : $(`[data-menu="${menuUserId}"]`))?.focus({preventScroll:true});
  }
});
$('#subscriber-dialog').addEventListener('close',() => { detailUserId = null; historyRequest++; });
window.addEventListener('popstate',() => setTab(location.hash.slice(1),false)); window.addEventListener('hashchange',() => { if (loggedIn) setTab(location.hash.slice(1),false); });
window.addEventListener('online',connectStream); window.addEventListener('offline',() => { disconnectStream(); setConnection('offline'); });
document.addEventListener('visibilitychange',() => { if (!loggedIn) return; if (document.hidden) { disconnectStream(); setConnection('paused'); } else connectStream(); });
// Only local clocks/countdowns tick. All server data arrives through EventSource.
setInterval(() => { if (loggedIn && !document.hidden) updateClock(); },1000);
enterApp().catch(error => { if (error.status !== 401) $('#login-error').textContent = error.message; });
