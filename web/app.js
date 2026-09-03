/*
 * BFSI Assistant client.
 *
 * Holds the WebRTC session to OpenAI using an ephemeral credential minted by AWS.
 * Tool calls are NOT executed here — they are forwarded to the Tool_Broker, which
 * resolves identity from the server-side Session_Record, authorizes the action,
 * and appends the audit entry. This client never sees the OpenAI API key and
 * cannot widen its own permissions.
 *
 * ── RESPONSIBLE AI ────────────────────────────────────────────────────────────
 * This is a customer-facing assistant over financial and insurance workflows, so
 * the constraints below are load-bearing, not decoration. Full detail lives in
 * RESPONSIBLE-AI.md at the repository root.
 *
 * Assist, don't decide. The assistant is advisory only. It cannot settle a claim,
 *   approve a payout, move money, or change an account. That is enforced by the
 *   server-side tool registry, which contains no such tool, and by a
 *   deny-by-default authorization policy — not by asking the model nicely.
 *
 * Human-in-the-loop is mandatory. Any consequential outcome routes to a person:
 *   a settlement or amount request is escalated to a specialist, high-risk
 *   actions require step-up verification, and every claims recommendation is
 *   reviewed and recorded by a named specialist before it takes effect.
 *
 * Disclosure to the customer. The server's DISCLOSURE text is injected into the
 *   session instructions and is also rendered in this UI, and the page carries a
 *   persistent `ai-disclaimer` banner. It states that responses are AI-generated,
 *   are not financial/legal/insurance advice, and should be reviewed by a
 *   colleague before being acted on.
 *
 * Guardrails, and an honest limit. The claims-review path runs on Amazon Bedrock
 *   and is screened by a Bedrock guardrail (content filters, PII anonymisation,
 *   and a denied topic for advice presented as a final decision); that call fails
 *   closed if the guardrail is absent. This voice path is different: the browser
 *   holds a WebRTC media session directly with OpenAI, so the audio and the
 *   model's response never traverse AWS and a Bedrock guardrail cannot be applied
 *   to them. What compensates is architectural — the model has no authority, no
 *   tool returns a monetary figure, every tool call is authorized and audited
 *   server-side, and money is computed by deterministic code rather than
 *   generated. Adopters who need output filtering on this path must add it at the
 *   model provider.
 *
 * Fairness. The sample runs on synthetic fixtures and makes no fairness claim.
 *   RESPONSIBLE-AI.md lists the bias and robustness testing a production adopter
 *   owns before launch, including outcome parity across protected and vulnerable
 *   groups — a senior-citizen persona is in the fixtures so it is not overlooked.
 * ─────────────────────────────────────────────────────────────────────────────
 */
'use strict';

const PERSONAS = [
  { id: 'cust-1001', name: 'Priya Raman', geo: 'Chennai · resident', close: true,
    eligible: ['public', 'customer'], code: '471028',
    tags: ['UPI dispute', 'home loan', 'health policy'],
    note: 'Failed UPI payment past its reversal deadline, floating-rate home loan, and a health policy still inside the pre-existing disease waiting period.' },
  { id: 'cust-1002', name: 'Rahul Mehta', geo: 'Dubai · NRI', close: false,
    eligible: ['public', 'customer'], code: '882301',
    tags: ['KYC overdue', 'partial freeze'],
    note: 'NRI joint NRE holder. KYC overdue on an 8-year cycle, so partial freeze applies and repatriation is suspended. No Video KYC.' },
  { id: 'cust-1003', name: 'Ananya Iyer', geo: 'Bengaluru · senior', close: true,
    eligible: ['public', 'customer'], code: '330715',
    tags: ['claim estimate', 'co-pay'],
    note: 'Senior citizen, all waiting periods served. A deluxe room at a non-preferred hospital triggers proportionate deduction plus a 10% co-payment.' },
  { id: 'cust-1004', name: 'Vikram Singh', geo: 'Jaipur · resident', close: false,
    eligible: ['public'], code: '119654',
    tags: ['public only'],
    note: 'Minimal entitlements, public documents only. Proves the retrieval filter narrows per customer.' },
];

const S = {
  idToken: null, customerId: null, persona: null,
  sessionId: null, pc: null, dc: null, micStream: null,
  assurance: 'authenticated', muted: false,
  citations: new Map(), toolCount: 0, startedAt: null,
  timer: null, audioRaf: null, handled: new Set(),
  turnCites: [],
};

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
const clock = () => new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

// ── chrome ────────────────────────────────────────────────────────────
function conn(text, kind) {
  $('conn').className = `conn ${kind || ''}`;
  $('conn-text').textContent = text;
}

function toast(text, kind) {
  const el = document.createElement('div');
  el.className = `toast ${kind || ''}`;
  setHTML(el, icon(kind === 'bad' ? 'alert' : kind === 'good' ? 'check' : 'shield') +
    `<span>${esc(text)}</span>`);
  $('toasts').appendChild(el);
  setTimeout(() => el.remove(), 5200);
}

function clearEmpty(host) { host.querySelector('.empty')?.remove(); }

function stage(title, sub) {
  $('stage-title').textContent = title;
  if (sub !== undefined) $('stage-sub').textContent = sub;
}

// ── transcript ────────────────────────────────────────────────────────
function msg(role, text) {
  const host = $('transcript');
  clearEmpty(host);
  const wrap = document.createElement('div');
  wrap.className = `msg ${role === 'user' ? 'user' : 'bot'}`;
  setHTML(wrap, `<div class="who">${icon(role === 'user' ? 'user' : 'bot')}</div>` +
    `<div class="body"><div class="bubble"></div>` +
    `<div class="time">${clock()}</div></div>`);
  host.appendChild(wrap);
  wrap.querySelector('.bubble').textContent = text;
  host.scrollTop = host.scrollHeight;
  return wrap;
}

function sysline(text, kind, ic) {
  const host = $('transcript');
  clearEmpty(host);
  const el = document.createElement('div');
  el.className = `sysline ${kind || ''}`;
  setHTML(el, icon(ic || 'shield') + `<span>${esc(text)}</span>`);
  host.appendChild(el);
  host.scrollTop = host.scrollHeight;
}

/* Attach the citations used during a turn under the advisor's message. */
function attachCites(wrap, cites) {
  if (!wrap || !cites.length) return;
  const host = wrap.querySelector('.body');
  if (host.querySelector('.cite-chips')) return;
  const row = document.createElement('div');
  row.className = 'cite-chips';
  const seen = new Set();
  for (const c of cites) {
    const key = `${c.document_id}@${c.version}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const chip = document.createElement('span');
    chip.className = 'cite-chip';
    chip.textContent = `${c.title} v${c.version}`;
    chip.title = `${c.section_ref || ''} · effective ${c.effective_date || ''}`;
    row.appendChild(chip);
  }
  host.appendChild(row);
}

/* Citations are still shown inline under the assistant's message — that is
   customer-appropriate. The Evidence / Tools / Audit inspector moved to /ops,
   which reads the server's own records instead of anything assembled here. */
function switchTab() {}

// ── auth + api ────────────────────────────────────────────────────────
async function signIn(username, password) {
  const res = await fetch(`https://cognito-idp.${CONFIG.region}.amazonaws.com/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-amz-json-1.1',
               'X-Amz-Target': 'AWSCognitoIdentityProviderService.InitiateAuth' },
    body: JSON.stringify({ AuthFlow: 'USER_PASSWORD_AUTH', ClientId: CONFIG.userPoolClientId,
                           AuthParameters: { USERNAME: username, PASSWORD: password } }),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.message || 'sign-in failed');
  if (!data.AuthenticationResult) throw new Error('additional challenge required');
  return data.AuthenticationResult.IdToken;
}

async function api(path, body, extra) {
  const res = await fetch(CONFIG.apiEndpoint + path, {
    method: 'POST',
    headers: Object.assign({ 'Content-Type': 'application/json',
                             Authorization: `Bearer ${S.idToken}` }, extra || {}),
    body: JSON.stringify(body || {}),
  });
  const text = await res.text();
  let data; try { data = JSON.parse(text); } catch { data = { error: text }; }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

async function callTool(name, args) {
  const rpc = await api('/mcp', {
    jsonrpc: '2.0', id: Date.now(), method: 'tools/call',
    params: { name, arguments: args || {} },
  }, {
    'x-session-id': S.sessionId,
    'x-idempotency-key': `${S.sessionId}:${name}:${JSON.stringify(args || {})}`,
  });
  if (rpc.error) return { status: 'rpc_error', result: null, error: rpc.error.message };
  try { return JSON.parse(rpc.result?.content?.[0]?.text); }
  catch { return { status: 'error', result: null, error: 'bad tool payload' }; }
}

// ── audio meters ──────────────────────────────────────────────────────
function meter(stream, el, cssVar) {
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const src = ctx.createMediaStreamSource(stream);
  const an = ctx.createAnalyser();
  an.fftSize = 512;
  an.smoothingTimeConstant = 0.75;
  src.connect(an);
  const buf = new Uint8Array(an.frequencyBinCount);
  const orb = $('orb');
  return () => {
    an.getByteFrequencyData(buf);
    let sum = 0;
    for (const v of buf) sum += v;
    const level = Math.min(1, (sum / buf.length) / 42);
    el.style.width = `${Math.round(level * 100)}%`;
    orb.style.setProperty(cssVar, level.toFixed(3));
  };
}

function runMeters(fns) {
  const tick = () => { fns.forEach((f) => f()); S.audioRaf = requestAnimationFrame(tick); };
  tick();
}

// ── session ───────────────────────────────────────────────────────────
function startTimer() {
  S.startedAt = Date.now();
  $('stat-timer').hidden = false;
  S.timer = setInterval(() => {
    const s = Math.floor((Date.now() - S.startedAt) / 1000);
    $('stat-timer').querySelector('b').textContent =
      `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
  }, 1000);
}

async function startCall() {
  conn('connecting', 'busy');
  $('btn-start').disabled = true;

  const session = await api('/session', {});
  S.sessionId = session.session_id;
  S.assurance = session.assurance_level;
  setHTML($('chip-session'), `<b>${session.session_id.slice(0, 12)}…</b>`);
  setAssurance(session.assurance_level);
  $('disclosure').textContent = session.disclosure;
  $('disclosure').hidden = false;

  S.micStream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true },
  });

  const pc = new RTCPeerConnection();
  S.pc = pc;
  const meters = [meter(S.micStream, $('lvl-in'), '--lvl-in')];

  pc.ontrack = (e) => {
    $('remote-audio').srcObject = e.streams[0];
    meters.push(meter(e.streams[0], $('lvl-out'), '--lvl-out'));
  };
  pc.addTrack(S.micStream.getAudioTracks()[0]);

  const dc = pc.createDataChannel('oai-events');
  S.dc = dc;
  dc.onmessage = (e) => onServerEvent(JSON.parse(e.data));
  dc.onopen = () => {
    conn('live', 'live');
    $('levels').hidden = false;
    $('btn-mute').hidden = false;
    $('orb').classList.remove('idle');
    stage('Listening', 'Speak naturally, or tap a suggested prompt.');
    startTimer();
    runMeters(meters);
    sysline('Call connected. Speak, or use a suggested prompt below.', 'good', 'check');
  };
  pc.oniceconnectionstatechange = () => {
    if (['failed', 'disconnected'].includes(pc.iceConnectionState)) conn('unstable', 'bad');
  };

  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);

  const r = await fetch(
    `https://api.openai.com/v1/realtime/calls?model=${encodeURIComponent(session.model)}`,
    { method: 'POST',
      headers: { Authorization: `Bearer ${session.client_secret}`, 'Content-Type': 'application/sdp' },
      body: offer.sdp });
  if (!r.ok) throw new Error(`realtime handshake failed (${r.status}): ${(await r.text()).slice(0, 160)}`);
  await pc.setRemoteDescription({ type: 'answer', sdp: await r.text() });

  $('btn-stop').hidden = false;
}

function stopCall() {
  S.dc?.close(); S.pc?.close();
  S.micStream?.getTracks().forEach((t) => t.stop());
  cancelAnimationFrame(S.audioRaf);
  clearInterval(S.timer);
  S.pc = S.dc = S.micStream = null;
  conn('call ended', '');
  $('btn-start').disabled = false;
  $('btn-stop').hidden = true;
  $('btn-mute').hidden = true;
  $('levels').hidden = true;
  $('lvl-in').style.width = $('lvl-out').style.width = '0%';
  const orb = $('orb');
  orb.classList.add('idle');
  orb.style.setProperty('--lvl-in', 0);
  orb.style.setProperty('--lvl-out', 0);
  stage('Call ended', 'Start another call whenever you are ready.');
  sysline('Call ended.', '', 'x');
}

function send(ev) { if (S.dc?.readyState === 'open') S.dc.send(JSON.stringify(ev)); }

function setAssurance(level) {
  S.assurance = level;
  const chip = $('chip-assurance');
  chip.className = `chip ${level === 'verified' ? 'verified' : ''}`;
  setHTML(chip, icon('shield') + `<b>${level}</b>`);
}

// ── realtime events ───────────────────────────────────────────────────
let botMsg = null;

async function onServerEvent(ev) {
  switch (ev.type) {
    case 'response.audio_transcript.delta':
    case 'response.output_audio_transcript.delta':
      if (!botMsg) { botMsg = msg('bot', ''); S.turnCites = []; }
      botMsg.querySelector('.bubble').textContent += ev.delta || '';
      $('transcript').scrollTop = $('transcript').scrollHeight;
      break;

    case 'response.done':
      attachCites(botMsg, S.turnCites);
      botMsg = null;
      reportUsage(ev.response?.usage);
      break;

    case 'conversation.item.input_audio_transcription.completed':
      if (ev.transcript) msg('user', ev.transcript);
      break;

    case 'response.function_call_arguments.done':
      await broker(ev.name, ev.call_id, ev.arguments);
      break;

    case 'response.output_item.done':
      if (ev.item?.type === 'function_call') {
        await broker(ev.item.name, ev.item.call_id, ev.item.arguments);
      }
      break;

    case 'error':
      sysline(`Session error: ${ev.error?.message || 'unknown'}`, 'bad', 'alert');
      break;
  }
}

async function broker(name, callId, rawArgs) {
  if (!name || !callId || S.handled.has(callId)) return;
  S.handled.add(callId);

  let args = {};
  try { args = rawArgs ? JSON.parse(rawArgs) : {}; } catch { /* keep {} */ }

  conn(`tool · ${name}`, 'busy');
  stage('Checking policy', name.replace(/_/g, ' '));
  const t0 = performance.now();
  const result = await callTool(name, args);
  const ms = Math.round(performance.now() - t0);

  const outcome = result.status === 'ok' ? 'allowed'
    : (result.status === 'not_permitted' || result.status === 'rpc_error') ? 'denied' : 'error';
  console.debug('[tool]', name, outcome, `${ms}ms`, result.error || '');

  const p = result.result || {};

  if (p.evidence?.length) {
    S.turnCites.push(...p.evidence);
  } else if (p.evidence) {
    sysline('No eligible policy matched. The advisor will not answer from general knowledge.', 'warn', 'alert');
  }

  if (p.conflict) {
    sysline(`Conflicting guidance in ${p.conflict.document_id} (versions ` +
            `${p.conflict.versions.join(' and ')}). Escalating rather than choosing.`, 'warn', 'alert');
    toast('Conflicting policy versions detected', 'warn');
  }
  if (result.assurance_level === 'verified') {
    setAssurance('verified');
    sysline('Identity verified. High-risk actions are now permitted for 10 minutes.', 'good', 'check');
    toast('Session verified', 'good');
  }
  if (p.challenge_sent) {
    sysline(`One-time code sent to ${p.channel}. Read it back to the advisor.`, 'warn', 'shield');
    toast(`Demo code for ${S.persona.name}: ${S.persona.code}`, 'warn');
  }
  if (result.status === 'not_permitted') {
    sysline(`Blocked by policy: ${result.error}`, 'bad', 'x');
    toast('Action blocked by policy', 'bad');
  }
  if (result.status === 'rpc_error') {
    sysline(`Rejected: ${result.error}`, 'bad', 'x');
  }
  if (p.escalation_reference) {
    sysline(`Escalated to a colleague. Reference ${p.escalation_reference}.`, 'warn', 'alert');
  }
  if (p.request_id) {
    sysline(`Service request ${p.request_id} created on ${p.account_id}` +
            `${p.replayed ? ' (idempotent replay)' : ''}.`, 'good', 'check');
    toast(`Request ${p.request_id} created`, 'good');
  }

  send({ type: 'conversation.item.create',
         item: { type: 'function_call_output', call_id: callId, output: JSON.stringify(result) } });
  send({ type: 'response.create' });
  conn('live', 'live');
  stage('Listening', 'Speak naturally, or tap a suggested prompt.');
}

/* Token accounting. The Realtime API reports usage on response.done; we post it
   to AWS so the /token dashboard can aggregate it. Never blocks the turn. */
async function reportUsage(usage) {
  if (!usage || !S.sessionId) return;
  S.turn = (S.turn || 0) + 1;
  S.tokens = (S.tokens || 0) + (usage.total_tokens || 0);
  const chip = $('chip-tokens');
  if (chip) {
    chip.hidden = false;
    setHTML(chip, icon('speed') + `<b>${S.tokens.toLocaleString()}</b> tokens`);
  }
  try {
    await api('/usage', { session_id: S.sessionId, turn: S.turn, usage });
  } catch (e) {
    console.warn('usage report failed', e.message);
  }
}

function sendText(text) {
  msg('user', text);
  send({ type: 'conversation.item.create',
         item: { type: 'message', role: 'user', content: [{ type: 'input_text', text }] } });
  send({ type: 'response.create' });
}

// ── personas ──────────────────────────────────────────────────────────
function renderPersonas() {
  const host = $('personas');
  setHTML(host, '');
  for (const p of PERSONAS) {
    const el = document.createElement('button');
    el.className = 'persona' + (p.id === $('username').value ? ' sel' : '');
    setHTML(el, `<div class="persona-top">` +
        `<span class="avatar">${esc(p.name.split(' ').map((n) => n[0]).join(''))}</span>` +
        `<span><h4>${esc(p.name)}</h4><span class="pid">${esc(p.id)}</span></span>` +
      `</div>` +
      `<div class="persona-tags">` +
        `<span class="tag neutral">${esc(p.geo)}</span>` +
        `<span class="tag ${p.eligible.length > 1 ? 'info' : 'warn'}">${esc(p.eligible.join(' + '))}</span>` +
        (p.tags || []).map((t) => `<span class="tag ok">${esc(t)}</span>`).join('') +
      `</div>` +
      `<p class="note">${esc(p.note)}</p>`);
    el.onclick = () => {
      $('username').value = p.id;
      document.querySelectorAll('.persona').forEach((n) => n.classList.remove('sel'));
      el.classList.add('sel');
    };
    host.appendChild(el);
  }
}

// ── wiring ────────────────────────────────────────────────────────────
renderPersonas();

$('btn-signin').onclick = async () => {
  $('signin-error').textContent = '';
  const u = $('username').value.trim();
  const btn = $('btn-signin');
  btn.disabled = true; btn.textContent = 'Signing in…';
  try {
    if (!u || !$('password').value) throw new Error('Enter a customer ID and password.');
    S.idToken = await signIn(u, $('password').value);
    S.customerId = u;
    S.persona = PERSONAS.find((p) => p.id === u) || { name: u, code: '—' };
    // Name and id together: the name is who the customer is, the id is the
    // scope every tool call resolves against.
    setHTML($('chip-who'), icon('user') +
      `<b>${esc(S.persona.name)}</b><span class="chip-id">${esc(u)}</span>`);
    $('chip-geo').textContent = S.persona.geo || '—';
    $('chip-eligible').textContent = (S.persona.eligible || []).join(' + ');
    setAssurance('authenticated');
    $('view-signin').hidden = true;
    $('view-app').hidden = false;
    conn('ready', 'ok');
    stage('Ready when you are',
          'Start the call and ask about a failed UPI payment, a claim estimate, '
          + 'a loan payoff, or your KYC.');
  } catch (e) {
    $('signin-error').textContent = e.message;
  } finally {
    btn.disabled = false; btn.textContent = 'Sign in';
  }
};

$('btn-start').onclick = async () => {
  try { await startCall(); }
  catch (e) { sysline(e.message, 'bad', 'alert'); toast(e.message, 'bad'); conn('failed', 'bad'); $('btn-start').disabled = false; }
};

$('btn-stop').onclick = stopCall;

$('btn-mute').onclick = () => {
  S.muted = !S.muted;
  S.micStream?.getAudioTracks().forEach((t) => { t.enabled = !S.muted; });
  $('btn-mute').classList.toggle('on', S.muted);
  setHTML($('btn-mute'), icon(S.muted ? 'micoff' : 'mic'));
  if (S.muted) $('lvl-in').style.width = '0%';
};

$('btn-signout').onclick = () => {
  if (S.pc) stopCall();
  location.reload();
};

$('text-form').onsubmit = (e) => {
  e.preventDefault();
  const v = $('text-message').value.trim();
  if (!v) return;
  if (S.dc?.readyState !== 'open') { toast('Start the call first', 'warn'); return; }
  $('text-message').value = '';
  sendText(v);
};

document.querySelectorAll('.pchip').forEach((chip) => {
  chip.onclick = () => {
    if (S.dc?.readyState !== 'open') { toast('Start the call first', 'warn'); return; }
    sendText(chip.dataset.say);
  };
});

$('username').addEventListener('input', () => {
  document.querySelectorAll('.persona').forEach((n) =>
    n.classList.toggle('sel', n.querySelector('.pid').textContent === $('username').value.trim()));
});
