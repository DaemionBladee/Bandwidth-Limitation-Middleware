const $ = (s, root = document) => root.querySelector(s);
let csrf = '', policies = [], users = [], activeTab = 'overview';
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const gb = n => `${Number((n || 0) / 1e9).toLocaleString(undefined, {maximumFractionDigits: 2})} GB`;
const date = (s, zone) => s ? new Intl.DateTimeFormat(undefined, {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',timeZone:zone || undefined}).format(new Date(s)) : '—';
const badge = (text, cls = '') => `<span class="badge ${cls}">${esc(text)}</span>`;
async function api(path, options = {}) {
  const response = await fetch('/api' + path, {...options, headers:{'Content-Type':'application/json','X-CSRF-Token':csrf,...options.headers}});
  if (!response.ok) {
    if (response.status === 401 && path !== '/login') { $('#app-view').hidden = true; $('#login-view').hidden = false; }
    let error; try { error = (await response.json()).detail; } catch { error = 'Request failed'; }
    if (Array.isArray(error)) error = error.map(e => `${e.loc.slice(1).join('.')}: ${e.msg}`).join('\n');
    throw new Error(error || `HTTP ${response.status}`);
  }
  return response.json();
}
function toast(text) { $('#toast').textContent = text; $('#toast').hidden = false; setTimeout(() => $('#toast').hidden = true, 3500); }
function openDialog(id) { const el = $(id); const error = $('.form-error', el); if (error) error.textContent = ''; el.showModal(); }
document.querySelectorAll('.close-dialog').forEach(button => button.addEventListener('click', () => button.closest('dialog').close()));
function formHandler(id, handler) {
  $(id).addEventListener('submit', async event => {
    event.preventDefault(); const button = $('button[type="submit"]', event.target); button.disabled = true;
    const error = $('.form-error', event.target) || $('#login-error'); error.textContent = '';
    try { await handler(event.target); } catch (e) { error.textContent = e.message; } finally { button.disabled = false; }
  });
}
async function enterApp() {
  const session = await api('/session'); csrf = session.csrf; $('#admin-name').textContent = session.username;
  $('#login-view').hidden = true; $('#app-view').hidden = false; await refresh();
}
formHandler('#login-form', async form => { const result = await api('/login',{method:'POST',body:JSON.stringify(Object.fromEntries(new FormData(form)))}); csrf = result.csrf; form.password.value = ''; await enterApp(); });
$('#logout').addEventListener('click', async () => { await api('/logout',{method:'POST'}); csrf=''; $('#app-view').hidden=true; $('#login-view').hidden=false; });
function setTab(tab) {
  activeTab=tab; for (const name of ['overview','policies','audit']) $(`#${name}-tab`).hidden=name!==tab;
  document.querySelectorAll('[data-tab]').forEach(b => b.classList.toggle('active',b.dataset.tab===tab));
  const labels={overview:['Overview','Network overview','Usage, policy decisions, and enforcement in one place.'],policies:['Policy library','Policy library','Define allowances, speed profiles, and reset schedules.'],audit:['Change history','Change history','A durable record of administrative changes.']};
  $('#breadcrumb').textContent=labels[tab][0]; $('#page-title').textContent=labels[tab][1]; $('#page-description').textContent=labels[tab][2];
}
document.querySelectorAll('[data-tab]').forEach(b => b.addEventListener('click',()=>setTab(b.dataset.tab)));
async function refresh() {
  $('#app-error').textContent='';
  try {
    const [p,u,status,audit]=await Promise.all([api('/policies'),api('/users'),api('/status'),api('/audit')]); policies=p; users=u;
    $('#clock').textContent=`Updated ${new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}`;
    const mock=status.ise_mode==='mock';
    $('#integration-title').textContent=mock?'Mock ISE · local simulation':'Live ISE adapter selected · verify per-subscriber confirmation';
    $('#integration-description').textContent=mock?'Confirmed mock states are simulated. No policy has been enforced on a live network.':'Requests require a verified deployment contract. Only active-session readback confirms enforcement.';
    $('#source-badge').textContent=`${status.accounting_source.toUpperCase()} ACCOUNTING`;
    $('#stat-users').textContent=u.filter(x=>x.policy).length; $('#stat-usage').textContent=gb(u.reduce((n,x)=>n+x.used_bytes,0));
    $('#stat-limited').textContent=u.filter(x=>x.ise?.desired.mode==='LIMITED').length;
    $('#stat-pending').textContent=u.filter(x=>x.ise && x.ise.status!=='confirmed').length;
    $('#users-empty').hidden=u.length!==0;
    $('#users-table').innerHTML=u.map(user => {
      const percent=user.quota_bytes?Math.min(100,100*user.used_bytes/user.quota_bytes):0;
      const state=user.ise, mode=state?.desired.mode, status=state?.status;
      return `<tr><td><strong>${esc(user.username)}</strong><small>${esc(user.policy || 'No policy assigned')}${user.revision?` · v${user.revision}`:''}${!user.enabled && user.policy?' · disabled':''}</small>${user.pending.length?'<br><small>Scheduled policy change</small>':''}</td><td><div class="usage-amount"><span>${gb(user.used_bytes)}</span><small>/ ${user.quota_bytes?gb(user.quota_bytes):'—'}</small></div><div class="meter ${percent>=100?'over':''}"><span ></span></div><small>${user.remaining_bytes!==null?gb(user.remaining_bytes)+' remaining':'No active quota interval'}</small></td><td>${mode?badge(mode,mode==='LIMITED'?'warning':''):'—'}${state?`<br><small>↓ ${state.desired.download_kbps/1000} / ↑ ${state.desired.upload_kbps/1000} Mbps</small>`:''}</td><td title="${esc(state?.last_error || '')}">${state?badge(`${state.adapter==='mock'?'MOCK · ':''}${status}`,status==='failed'?'danger':status==='pending'?'warning':'neutral'):'—'}${state?.confirmed && state.confirmed_generation!==state.generation?`<br><small>Last ${esc(state.confirmed_adapter || 'unknown adapter')} confirmation: ${esc(state.confirmed.mode)}</small>`:''}</td><td>${date(user.next_reset,user.timezone)}<br><small>${esc(user.timezone || '')}</small></td><td><button class="link-button" data-assign="${user.id}">Assign</button> · <button class="link-button" data-history="${user.id}">History</button>${state?`<br><button class="link-button" data-retry="${user.id}">Reconcile ↻</button>`:''}</td></tr>`;
    }).join('');
    // Style attributes are set through the DOM to retain a strict CSP.
    $('#users-table').querySelectorAll('.meter span').forEach((el,i)=>{el.style.width=`${u[i].quota_bytes?Math.min(100,100*u[i].used_bytes/u[i].quota_bytes):0}%`;});
    $('#jobs').innerHTML=status.jobs.length?status.jobs.map(j=>`<div class="job-row"><span>${esc(j.kind)}</span><span>${badge(j.status,j.status==='failed'?'danger':'neutral')} <small>× ${j.count}</small></span></div>`).join(''):'<p class="muted small">No jobs yet. Start the background worker.</p>';
    $('#accounting-mode').textContent=status.accounting_source.toUpperCase(); $('#mock-usage').hidden=status.accounting_source!=='mock';
    $('#checkpoint').textContent=status.checkpoints.length?`Last ingestion: ${date(status.checkpoints[0].updated_at)}`:'No ingestion checkpoint. Start the background worker.';
    $('#worker-errors').textContent=status.worker_errors.map(j=>`${j.kind}: ${j.error} (${j.attempts} attempts)`).join('\n');
    $('#policy-grid').innerHTML=p.length?p.map(policy=>{const s=policy.spec;return `<article class="policy-card"><div class="card-top">${badge(s.enabled?'ENABLED':'DISABLED',s.enabled?'':'neutral')}<small>v${policy.revision} · #${policy.id}</small></div><h3>${esc(s.name)}</h3><div class="quota">${gb(s.quota_bytes)} <small>/ ${esc(s.period_type)}</small></div><div class="profile-row"><span>Normal ↓ / ↑</span><strong>${s.normal_download_kbps/1000} / ${s.normal_upload_kbps/1000} Mbps</strong></div><div class="profile-row"><span>Limited ↓ / ↑</span><strong>${s.limited_download_kbps/1000} / ${s.limited_upload_kbps/1000} Mbps</strong></div><p class="schedule">${esc(s.timezone)} · ${esc(s.reset_time)}<br>${s.period_type==='monthly'?`Day ${s.monthly_day} · clamps to month end`:s.period_type==='weekly'?['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'][s.weekly_day]:s.period_type==='custom'?esc(s.custom_start)+' → '+esc(s.custom_end):'Every day'}</p><button class="secondary" data-edit="${policy.id}">Edit policy →</button></article>`;}).join(''):'<div class="empty">Create a policy to get started.</div>';
    $('#audit-table').innerHTML=audit.map(a=>`<tr><td>${date(a.created_at)}</td><td>${esc(a.actor)}</td><td>${esc(a.action)}</td><td>${esc(a.entity)}</td><td class="audit-detail"><details><summary>View change</summary><pre>${esc(JSON.stringify({before:a.before,after:a.after},null,2))}</pre></details></td></tr>`).join('');
    bindActions();
  } catch(e) { $('#app-error').textContent=e.message; }
}
$('#refresh').addEventListener('click',refresh);
function bindActions() {
  document.querySelectorAll('[data-edit]').forEach(b=>b.addEventListener('click',()=>editPolicy(Number(b.dataset.edit))));
  document.querySelectorAll('[data-assign]').forEach(b=>b.addEventListener('click',()=>assignUser(Number(b.dataset.assign))));
  document.querySelectorAll('[data-history]').forEach(b=>b.addEventListener('click',()=>showHistory(Number(b.dataset.history))));
  document.querySelectorAll('[data-retry]').forEach(b=>b.addEventListener('click',async()=>{try{await api(`/users/${b.dataset.retry}/reconcile`,{method:'POST'});toast('Reconciliation queued');await refresh();}catch(e){$('#app-error').textContent=e.message;}}));
}
function scheduleFields() { const type=$('#policy-form').period_type.value; $('#monthly-field').hidden=type!=='monthly'; $('#weekly-field').hidden=type!=='weekly'; document.querySelectorAll('[data-custom]').forEach(e=>e.hidden=type!=='custom'); document.querySelectorAll('[data-calendar]').forEach(e=>e.hidden=type==='custom'); }
$('#policy-form').period_type.addEventListener('change',scheduleFields);
function editPolicy(id) {
  const form=$('#policy-form');form.reset();form.elements.id.value='';form.revision.value='';
  const p=policies.find(p=>p.id===id);
  if(p){const s=p.spec;for(const k of ['name','timezone','period_type','reset_time','monthly_day','weekly_day','custom_start','custom_end','repeat_interval_seconds','expiry_action'])form.elements[k].value=s[k]??'';form.quota_gb.value=s.quota_bytes/1e9;for(const k of ['normal_download','normal_upload','limited_download','limited_upload'])form.elements[k].value=s[k+'_kbps']/1000;form.enabled.checked=s.enabled;form.elements.id.value=id;form.revision.value=p.revision;}
  $('#policy-dialog-title').textContent=p?'Edit policy':'Create policy';scheduleFields();openDialog('#policy-dialog');
}
$('#new-policy').addEventListener('click',()=>editPolicy(null));
formHandler('#policy-form',async form=>{
  const f=Object.fromEntries(new FormData(form)),custom=f.period_type==='custom';
  const spec={name:f.name,enabled:form.enabled.checked,quota_bytes:Math.round(Number(f.quota_gb)*1e9),timezone:f.timezone,period_type:f.period_type,reset_time:f.reset_time||'00:00',monthly_day:Number(f.monthly_day)||1,weekly_day:Number(f.weekly_day)||0,custom_start:custom?f.custom_start||null:null,custom_end:custom?f.custom_end||null:null,repeat_interval_seconds:custom&&f.repeat_interval_seconds?Number(f.repeat_interval_seconds):null,expiry_action:custom?f.expiry_action||null:null};
  for(const k of ['normal_download','normal_upload','limited_download','limited_upload'])spec[k+'_kbps']=Math.round(Number(f[k])*1000);
  await api(f.id?`/policies/${f.id}`:'/policies',{method:f.id?'PUT':'POST',body:JSON.stringify(f.id?{spec,application:f.application,expected_revision:Number(f.revision)}:spec)});$('#policy-dialog').close();toast('Policy saved and audited');await refresh();
});
$('#new-user').addEventListener('click',()=>{$('#user-form').reset();openDialog('#user-dialog');});
formHandler('#user-form',async form=>{const u=await api('/users',{method:'POST',body:JSON.stringify({username:form.username.value})});$('#user-dialog').close();await refresh();toast('Subscriber created');assignUser(u.id);});
function assignUser(id) { const user=users.find(u=>u.id===id),form=$('#assignment-form');form.user_id.value=id;$('#assignment-user').textContent=user.username;form.policy_id.innerHTML=policies.map(p=>`<option value="${p.id}">${esc(p.spec.name)}</option>`).join('');if(user.policy_id)form.policy_id.value=user.policy_id;form.enabled.checked=user.policy?user.enabled:true;form.application.value='immediate';openDialog('#assignment-dialog'); }
formHandler('#assignment-form',async form=>{await api(`/users/${form.user_id.value}/assignment`,{method:'PUT',body:JSON.stringify({policy_id:Number(form.policy_id.value),enabled:form.enabled.checked,application:form.application.value})});$('#assignment-dialog').close();toast('Assignment saved');await refresh();});
$('#mock-usage').addEventListener('click',()=>{const form=$('#usage-form');form.reset();form.username.innerHTML=users.map(u=>`<option>${esc(u.username)}</option>`).join('');form.record_id.value=crypto.randomUUID();openDialog('#usage-dialog');});
formHandler('#usage-form',async form=>{await api('/mock/accounting',{method:'POST',body:JSON.stringify({record_id:form.record_id.value,username:form.username.value,session_id:form.session_id.value,observed_at:new Date().toISOString(),kind:'delta',upload:Math.round(Number(form.upload.value)*1e9),download:Math.round(Number(form.download.value)*1e9),unit:'bytes'})});$('#usage-dialog').close();toast('Mock accounting queued for ingestion');await refresh();});
async function showHistory(id) { try { const data=await api(`/users/${id}/history`);$('#history-title').textContent=users.find(u=>u.id===id).username;$('#history-content').innerHTML=`<h3>Retained quota periods</h3><p class="muted small">Immediate policy changes can retain overlapping policy views; these rows are not additive.</p><div class="table-scroll"><table><thead><tr><th>Start</th><th>End</th><th>Policy version</th><th>Used / Quota</th></tr></thead><tbody>${data.periods.map(p=>`<tr><td>${date(p.starts_at)}</td><td>${date(p.ends_at)}</td><td>#${p.version_id}</td><td>${gb(p.used_bytes)} / ${gb(p.quota_bytes)}</td></tr>`).join('')}</tbody></table></div><h3>Enforcement attempts</h3><div class="table-scroll"><table><thead><tr><th>Time</th><th>Generation</th><th>Adapter / Mode</th><th>Outcome</th><th>Error</th></tr></thead><tbody>${data.attempts.map(a=>`<tr><td>${date(a.created_at)}</td><td>${a.generation}</td><td>${esc(a.adapter)} / ${esc(a.mode)}</td><td>${esc(a.outcome)}</td><td>${esc(a.error || '—')}</td></tr>`).join('')}</tbody></table></div>`;openDialog('#history-dialog');}catch(e){$('#app-error').textContent=e.message;} }
enterApp().catch(()=>{});
setInterval(()=>{if(!$('#app-view').hidden && !document.querySelector('dialog[open]'))refresh();},10000);
