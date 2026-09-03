/*
 * Token usage dashboard.
 *
 * Reads aggregates from POST /usage-summary, which sits behind the same Cognito
 * authorizer as the rest of the API. Cost rates are user-editable and stored in
 * localStorage only — nothing about pricing is hard-coded server-side.
 */
'use strict';

const $ = (id) => document.getElementById(id);
const fmt = (n) => (n || 0).toLocaleString();
const usd = (n) => '$' + (n || 0).toFixed(n >= 1 ? 2 : 4);

// Escape any value that came from the server or the user before it goes into
// markup. This is the actual XSS defense: every interpolated dynamic value is
// wrapped in esc(). A customer id, session id or tool name containing HTML is
// rendered as text, never parsed.
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// Single DOM write sink. Callers assemble app-authored markup with every dynamic
// value already run through esc(); this clears the node and parses that trusted
// markup in one place, so there is one audited write path rather than scattered
// innerHTML assignments.
function setHTML(el, markup) {
  if (!el) return;
  el.replaceChildren();
  el.insertAdjacentHTML('beforeend', markup);
}

// Published gpt-realtime-2.1 rates, per 1M tokens. Editable in the UI.
const DEFAULT_RATES = {
  in_audio: 32.00, in_cached_audio: 0.40, out_audio: 64.00,
  in_text: 4.00, in_cached_text: 0.40, out_text: 24.00,
};

const BANDS = [
  ['in_audio', 'Audio input', '#1f4fd8'],
  ['out_audio', 'Audio output', '#ff9900'],
  ['in_cached_audio', 'Cached audio input', '#6e40c9'],
  ['in_text', 'Text input', '#0e7690'],
  ['out_text', 'Text output', '#10803c'],
  ['in_cached_text', 'Cached text input', '#98a1ad'],
];

const S = { token: null, data: null, rates: loadRates(), session: null, timer: null };

function loadRates() {
  // Overrides come from localStorage, so they are treated as untrusted input:
  // copy only the keys DEFAULT_RATES already defines, and only finite numbers.
  // Merging the parsed object wholesale would let a "__proto__" key reach the
  // prototype chain, and would accept non-numeric rates that break the maths.
  const rates = { ...DEFAULT_RATES };
  let stored;
  try {
    stored = JSON.parse(localStorage.ea_rates || '{}');
  } catch { return rates; }
  if (!stored || typeof stored !== 'object') return rates;
  for (const key of Object.keys(DEFAULT_RATES)) {
    if (!Object.prototype.hasOwnProperty.call(stored, key)) continue;
    const value = Number(stored[key]);
    if (Number.isFinite(value) && value >= 0) rates[key] = value;
  }
  return rates;
}
function saveRates() { localStorage.ea_rates = JSON.stringify(S.rates); }

function toast(text, kind) {
  const el = document.createElement('div');
  el.className = `toast ${kind || ''}`;
  el.textContent = text;
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
  const r = await fetch(CONFIG.apiEndpoint + '/usage-summary', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${S.token}` },
    body: JSON.stringify(S.session ? { session_id: S.session } : {}),
  });
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
  S.data = d;
  render();
  $('updated').textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

// ── cost ──────────────────────────────────────────────────────────────
function costOf(row) {
  let total = 0;
  for (const [k] of BANDS) total += ((row?.[k] || 0) / 1e6) * (S.rates[k] || 0);
  return total;
}

// ── render ────────────────────────────────────────────────────────────
function render() {
  const g = S.data.global || {};
  const tools = S.data.tools || {};
  const sessions = S.data.sessions || [];

  const totalTokens = BANDS.reduce((a, [k]) => a + (g[k] || 0), 0);
  const turns = g.turns || 0;
  const cost = costOf(g);
  const cachedIn = (g.in_cached_audio || 0) + (g.in_cached_text || 0);
  const freshIn = (g.in_audio || 0) + (g.in_text || 0);
  const cachePct = (cachedIn + freshIn) ? Math.round(cachedIn / (cachedIn + freshIn) * 100) : 0;

  // what the same cached tokens would have cost at full input rate
  const saved = ((g.in_cached_audio || 0) / 1e6) * (S.rates.in_audio - S.rates.in_cached_audio)
              + ((g.in_cached_text || 0) / 1e6) * (S.rates.in_text - S.rates.in_cached_text);

  setHTML($('tiles'), [
    ['', 'i-chart', 'Total tokens', fmt(totalTokens), `${fmt(turns)} model turns`],
    ['o', 'i-coin', 'Estimated cost', usd(cost), turns ? `${usd(cost / turns)} per turn` : 'no turns yet'],
    ['b', 'i-mic', 'Audio share', `${totalTokens ? Math.round(((g.in_audio || 0) + (g.out_audio || 0) + (g.in_cached_audio || 0)) / totalTokens * 100) : 0}<small>%</small>`, 'of all tokens'],
    ['p', 'i-cache', 'Cached input', `${cachePct}<small>%</small>`, saved > 0 ? `${usd(saved)} avoided` : 'no cache hits yet'],
    ['g', 'i-tool', 'Tool calls', fmt(tools.tool_calls || 0), `${fmt(sessions.length)} sessions tracked`],
  ].map(([cls, ic, k, v, s]) => `
    <div class="tile ${cls}">
      <svg><use href="#${ic}"/></svg>
      <div class="k">${k}</div><div class="v">${v}</div><div class="s">${s}</div>
    </div>`).join(''));

  // split bar
  const bars = BANDS.filter(([k]) => g[k]);
  setHTML($('split'), totalTokens ? `
    <div class="splitbar">${bars.map(([k, , c]) =>
      `<i style="width:${(g[k] / totalTokens * 100).toFixed(2)}%;background:${c}" title="${k}"></i>`).join('')}</div>
    <div class="legend">${BANDS.map(([k, nm, c]) => `
      <div class="lrow"><span class="sw" style="background:${c}"></span>
        <span class="nm">${nm}</span>
        <span class="tk">${fmt(g[k] || 0)}</span>
        <span class="cs">${usd(((g[k] || 0) / 1e6) * (S.rates[k] || 0))}</span></div>`).join('')}
    </div>` : `<p class="empty-note">No turns recorded yet. Start a call on the advisor.</p>`);

  // rates. k and nm come from the static BANDS table, not from any input.
  setHTML($('rates'), BANDS.map(([k, nm]) => `
    <div class="rrow"><label for="r-${k}">${nm}</label>
      <input id="r-${k}" data-k="${k}" type="number" step="0.01" min="0" value="${S.rates[k]}"></div>`).join(''));
  $('rates').querySelectorAll('input').forEach((el) => {
    el.onchange = () => {
      S.rates[el.dataset.k] = parseFloat(el.value) || 0;
      saveRates(); render();
    };
  });

  // sessions
  const tb = $('sessions').querySelector('tbody');
  if (!sessions.length) {
    setHTML(tb, `<tr><td class="empty-note" colspan="7">No sessions yet.</td></tr>`);
  } else {
    setHTML(tb, `<tr>
        <th>Session</th><th>Customer</th><th class="n">Turns</th>
        <th class="n">Audio</th><th class="n">Text</th><th class="n">Cached</th>
        <th class="n">Cost</th></tr>` +
      sessions.map((r) => {
        const sid = esc((r.session_id || '').slice(0, 10));
        const audio = (r.in_audio || 0) + (r.out_audio || 0);
        const text = (r.in_text || 0) + (r.out_text || 0);
        const cached = (r.in_cached_audio || 0) + (r.in_cached_text || 0);
        return `<tr data-sid="${esc(r.session_id)}" class="${S.session === r.session_id ? 'sel' : ''}">
          <td><span class="who"><span class="dot9"></span><code>${sid}…</code></span></td>
          <td>${esc(r.customer_id || '—')}</td>
          <td class="n">${fmt(r.turns)}</td>
          <td class="n">${fmt(audio)}</td>
          <td class="n">${fmt(text)}</td>
          <td class="n">${fmt(cached)}</td>
          <td class="n">${usd(costOf(r))}</td></tr>`;
      }).join(''));
    tb.querySelectorAll('tr[data-sid]').forEach((tr) => {
      tr.onclick = () => { S.session = tr.dataset.sid; load(); };
    });
  }

  // tool bars
  const toolRows = Object.entries(tools)
    .filter(([k]) => !k.startsWith('outcome_') && !['tool_calls', 'last_ts'].includes(k))
    .sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...toolRows.map(([, v]) => v));
  setHTML($('tools'), toolRows.length ? toolRows.map(([k, v]) => `
    <div class="tb"><span class="nm">${esc(k)}</span>
      <span class="bar"><i style="width:${v / max * 100}%"></i></span>
      <span class="ct">${v}</span></div>`).join('') +
    `<div class="tb" style="margin-top:6px"><span class="nm" style="color:#10803c">ok</span>
       <span class="bar"><i style="width:${(tools.outcome_ok || 0) / (tools.tool_calls || 1) * 100}%;background:#10803c"></i></span>
       <span class="ct">${tools.outcome_ok || 0}</span></div>
     <div class="tb"><span class="nm" style="color:#c9350f">not_permitted</span>
       <span class="bar"><i style="width:${(tools.outcome_not_permitted || 0) / (tools.tool_calls || 1) * 100}%;background:#c9350f"></i></span>
       <span class="ct">${tools.outcome_not_permitted || 0}</span></div>`
    : `<p class="empty-note">No tool calls recorded yet.</p>`);

  // per-turn trace
  const turnsArr = S.data.turns || [];
  $('turn-note').textContent = S.session
    ? `${S.session.slice(0, 10)}… · ${turnsArr.length} turns`
    : 'pick a session above';
  const tmax = Math.max(1, ...turnsArr.map((t) =>
    BANDS.reduce((a, [k]) => a + (t[k] || 0), 0)));
  setHTML($('turns'), turnsArr.length ? turnsArr.map((t) => {
    const tot = BANDS.reduce((a, [k]) => a + (t[k] || 0), 0);
    return `<div class="turn">
      <span class="ix">#${fmt(t.turn)}</span>
      <span class="bar" style="width:${Math.max(6, tot / tmax * 100)}%">
        ${BANDS.filter(([k]) => t[k]).map(([k, , c]) =>
          `<i style="width:${t[k] / tot * 100}%;background:${c}"></i>`).join('')}</span>
      <span class="tk">${fmt(tot)}</span></div>`;
  }).join('') : `<p class="empty-note">Select a session to see its turn-by-turn breakdown.</p>`);
}

/* Usage is aggregated per identity, so name the identity the figures belong to. */
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
    $('view-dash').hidden = false;
    await load();
    S.timer = setInterval(() => load().catch(() => {}), 15000);
  } catch (e) {
    $('signin-error').textContent = e.message;
  }
};

$('btn-signout').onclick = () => { clearInterval(S.timer); location.reload(); };

$('btn-refresh').onclick = () =>
  load().then(() => toast('Refreshed', 'good')).catch((e) => toast(e.message, 'bad'));

document.addEventListener('keydown', (e) => {
  if (e.key === 'r' && !e.metaKey && S.token && document.activeElement.tagName !== 'INPUT') {
    load().catch(() => {});
  }
});
