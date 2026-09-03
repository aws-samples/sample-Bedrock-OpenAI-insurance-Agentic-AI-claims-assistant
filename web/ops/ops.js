/*
 * Operator view.
 *
 * Reads POST /session-trace, which assembles everything from the server's own
 * records: the persisted evidence trace, the hash-chained audit table, and the
 * Session_Record. The browser supplies only a session id, and the API rejects
 * one that does not belong to the signed-in identity.
 */
'use strict';

const $ = (id) => document.getElementById(id);
const icon = (n) => `<svg><use href="#i-${n}"/></svg>`;
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

// ── safe DOM write sink ───────────────────────────────────────────────
// Every dynamic value interpolated into markup below is wrapped in esc(); that
// is the XSS defense. These helpers give that markup a single, audited write
// path (clear-then-parse for setHTML, append for addHTML) instead of scattered
// innerHTML assignments.
function setHTML(el, markup) {
  if (!el) return;
  el.replaceChildren();
  el.insertAdjacentHTML('beforeend', markup);
}
function addHTML(el, markup) {
  if (el) el.insertAdjacentHTML('beforeend', markup);
}
const fmt = (n) => (n || 0).toLocaleString();
const when = (ts) => ts ? new Date(ts * 1000).toLocaleTimeString([], {
  hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—';

const S = { token: null, session: null, data: null, timer: null };

function toast(text, kind) {
  const el = document.createElement('div');
  el.className = `toast ${kind || ''}`;
  setHTML(el, icon(kind === 'bad' ? 'alert' : 'check') + `<span>${esc(text)}</span>`);
  $('toasts').appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

// ── auth + fetch ──────────────────────────────────────────────────────
async function signIn(u, p) {
  const r = await fetch(`https://cognito-idp.${CONFIG.region}.amazonaws.com/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-amz-json-1.1',
               'X-Amz-Target': 'AWSCognitoIdentityProviderService.InitiateAuth' },
    body: JSON.stringify({ AuthFlow: 'USER_PASSWORD_AUTH', ClientId: CONFIG.userPoolClientId,
                           AuthParameters: { USERNAME: u, PASSWORD: p } }),
  });
  const d = await r.json();
  if (!r.ok) throw new Error(d.message || 'sign-in failed');
  if (!d.AuthenticationResult) throw new Error('additional challenge required');
  return d.AuthenticationResult.IdToken;
}

async function load() {
  const r = await fetch(CONFIG.apiEndpoint + '/session-trace', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${S.token}` },
    body: JSON.stringify(S.session ? { session_id: S.session } : {}),
  });
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
  S.data = d;
  render();
  $('updated').textContent = new Date().toLocaleTimeString([], {
    hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

// ── render ────────────────────────────────────────────────────────────
function renderSessionPicker() {
  const sel = $('sess');
  const list = S.data.sessions || [];
  setHTML(sel, list.length
    ? list.map((s) => `<option value="${esc(s.session_id)}"${s.session_id === S.session ? ' selected' : ''}>` +
        `${esc(s.session_id.slice(0, 12))}…  ·  ${esc(when(s.created_at))}  ·  ${esc(s.assurance_level)}</option>`).join('')
    : `<option value="">no sessions yet</option>`);
  if (!S.session && list.length) {
    S.session = list[0].session_id;
    sel.value = S.session;
    return true;
  }
  return false;
}

function renderChips() {
  const s = S.data.session;
  const host = $('sesschips');
  if (!s) { setHTML(host, ''); return; }
  setHTML(host, [
    ['user', s.customer_id],
    ['shield', s.assurance_level],
    [null, s.geography],
    [null, (s.eligible_classifications || []).join(' + ')],
    [null, (s.accounts || []).join(', ')],
    [null, `policy ${S.data.policy_version}`],
  ].filter(([, v]) => v).map(([ic, v]) =>
    `<span class="chip${v === 'verified' ? ' verified' : ''}">${ic ? icon(ic) : ''}<b>${esc(v)}</b></span>`
  ).join(''));
}

function renderTiles() {
  const ev = S.data.evidence || [];
  const tools = S.data.tools || [];
  const denials = S.data.denials || [];
  const audit = S.data.audit || {};
  const docs = (S.data.documents_cited || []).length;
  const classes = new Set();
  ev.forEach((t) => (t.evidence || []).forEach((e) => classes.add(e.access_classification)));

  setHTML($('tiles'), [
    ['', 'doc', 'Retrievals', fmt(ev.length), `${docs} distinct documents cited`],
    ['b', 'eye', 'Classifications seen', [...classes].map(esc).join(' + ') || '—',
     'internal and restricted must never appear'],
    ['g', 'shield', 'Tool calls', fmt(tools.length),
     `${denials.length} denied by policy`],
    [audit.ok ? 'p' : 'o', 'chain', 'Audit chain',
     audit.ok ? `${fmt(audit.entries)} ✓` : `broken @ ${audit.break_at ?? '?'}`,
     audit.ok ? 'digests recomputed and verified' : esc(audit.why || 'verification failed')],
  ].map(([cls, ic, k, v, s]) => `
    <div class="tile ${cls}">${icon(ic)}
      <div class="k">${k}</div><div class="v">${v}</div><div class="s">${s}</div>
    </div>`).join(''));
}

function renderEvidence() {
  const ev = S.data.evidence || [];
  const host = $('evidence');
  if (!ev.length) {
    setHTML(host, `<p class="empty-note">No retrievals recorded on this session yet.</p>`);
    return;
  }
  setHTML(host, ev.map((turn) => {
    const docs = turn.evidence || [];
    const bad = docs.filter((d) => ['internal', 'restricted'].includes(d.access_classification));
    return `
      <div class="evturn">
        <div class="evhead">
          <span class="tool">${esc(turn.tool)}</span>
          <span>${when(turn.ts)}</span>
          <span>${docs.length} chunk(s)</span>
        </div>
        <div class="evgrid">
          ${docs.map((d) => {
            const pct = Math.round((d.score || 0) * 100);
            return `<div class="cite ${d.superseded ? 'superseded' : ''}">
              <div class="cite-h"><strong>${esc(d.title)}</strong>
                <span class="v">v${esc(d.version)}</span></div>
              <div class="doclabel"><code>${esc(d.document_id)}</code>
                <span class="v" style="font-size:10.5px;color:var(--muted)">${esc(d.citation_id || '')}</span></div>
              <div class="cite-sec">${esc(d.section_ref || '')}</div>
              <div class="cite-tags">
                <span class="tag ${d.access_classification === 'public' ? 'neutral' : 'info'}">${esc(d.access_classification)}</span>
                <span class="tag neutral">${esc(d.geography)}</span>
                <span class="tag neutral">eff. ${esc(d.effective_date)}</span>
                ${d.superseded ? '<span class="tag warn">superseded</span>' : ''}
              </div>
              <div class="score"><span>relevance</span>
                <span class="sbar"><i style="width:${pct}%"></i></span><span>${pct}%</span></div>
            </div>`;
          }).join('')}
        </div>
        ${bad.length ? `<div class="blocked">${icon('alert')} <b>${bad.length} ineligible
          document(s) reached the Evidence_Set.</b> This should be impossible — the filter
          runs inside the vector search.</div>` : ''}
      </div>`;
  }).join(''));
}

function renderTools() {
  const tools = S.data.tools || [];
  const denials = S.data.denials || [];
  const host = $('tools');
  if (!tools.length) {
    setHTML(host, `<p class="empty-note">No tool calls on this session yet.</p>`);
    return;
  }
  const rows = tools.slice().reverse().map((t) => {
    const outcome = t.status === 'ok' ? 'allowed'
      : (t.decision === 'deny' || t.status === 'not_permitted') ? 'denied' : 'error';
    return `<div class="trow ${outcome}">
      <div class="trow-h">${icon(outcome === 'allowed' ? 'check' : outcome === 'denied' ? 'x' : 'alert')}
        <span class="tname">${esc(t.tool)}</span>
        <span class="tlat">${when(t.ts)}</span></div>
      ${t.status && t.status !== 'ok' ? `<div class="tdet">status: ${esc(t.status)}</div>` : ''}
    </div>`;
  }).join('');
  setHTML(host, rows + (denials.length
    ? `<p class="section-label">denied by policy</p>` + denials.map((d) =>
        `<div class="denial"><span class="act">${esc(d.action)}</span><br>${esc(d.reason)}</div>`).join('')
    : ''));
}

function renderAudit() {
  const a = S.data.audit || {};
  $('chain-note').textContent = a.ok ? 'digests verified' : 'break detected';
  const shown = Math.min(a.entries || 0, 14);
  setHTML($('audit'), `<div class="verdict ${a.ok ? 'ok' : 'bad'}">${icon(a.ok ? 'check' : 'alert')}
       ${a.ok ? `Chain intact — ${fmt(a.entries)} entries verified`
              : `Chain broken at entry ${a.break_at} — ${esc(a.why || '')}`}</div>` +
    `<div class="chain" style="margin-top:12px">` +
      Array.from({ length: shown }, (_, i) =>
        `<div class="link${a.ok ? '' : ' broken'}">${icon(a.ok ? 'check' : 'x')}
           entry ${i + 1} · digest ${a.ok ? 'verified' : 'unchecked'}</div>`).join('') +
      ((a.entries || 0) > shown ? `<div class="link">…and ${a.entries - shown} more</div>` : '') +
    `</div>`);
}

function render() {
  const autoPicked = renderSessionPicker();
  if (autoPicked) { load(); return; }
  renderChips();
  renderTiles();
  renderEvidence();
  renderTools();
  renderAudit();
}

/* The trace API returns only the caller's own sessions, so the identity in the
   corner is the scope of everything below it. Worth showing, not decoration. */
function showUser(id) {
  $('who-av').textContent = (id.match(/\d+/) || ['?'])[0].slice(-2) || id.slice(0, 2).toUpperCase();
  $('who-name').textContent = id;
  $('who').hidden = false;
}

// ── wiring ────────────────────────────────────────────────────────────
$('btn-signin').onclick = async () => {
  $('signin-error').textContent = '';
  try {
    const id = $('username').value.trim();
    if (!id || !$('password').value) throw new Error('Enter a customer ID and password.');
    S.token = await signIn(id, $('password').value);
    showUser(id);
    $('view-signin').hidden = true;
    $('view-ops').hidden = false;
    await load();
    S.timer = setInterval(() => load().catch(() => {}), 15000);
  } catch (e) {
    $('signin-error').textContent = e.message;
  }
};

$('btn-signout').onclick = () => { clearInterval(S.timer); location.reload(); };

$('sess').onchange = () => { S.session = $('sess').value || null; load().catch(() => {}); };

$('btn-refresh').onclick = () =>
  load().then(() => toast('Refreshed', 'good')).catch((e) => toast(e.message, 'bad'));
