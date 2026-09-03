/*
 * Provider Voice Assistant — client.
 *
 * Holds the WebRTC media session to OpenAI Realtime using an ephemeral secret
 * minted by AWS. The long-lived OpenAI key is never sent to the browser and
 * never appears in this file.
 *
 * Tool calls are NOT executed here. The model asks; this file forwards the ask
 * to POST /claim-voice-tool, which resolves identity from the server-side
 * Session_Record, validates the arguments, and dispatches from a registry that
 * contains no approve or settle entry. The browser is a relay, not an authority.
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
const clock = () => new Date().toLocaleTimeString([], {
  hour: '2-digit', minute: '2-digit', second: '2-digit' });

// Indian lakh grouping, display only.
const money = (n) => {
  const s = Math.round(Math.abs(Number(n) || 0)).toString();
  if (s.length <= 3) return '₹' + s;
  const tail = s.slice(-3);
  let head = s.slice(0, -3);
  const groups = [];
  while (head.length > 2) { groups.unshift(head.slice(-2)); head = head.slice(0, -2); }
  if (head) groups.unshift(head);
  return '₹' + groups.concat(tail).join(',');
};

const S = {
  token: null, user: null, sessionId: null, claim: null,
  pc: null, dc: null, micStream: null, audioRaf: null,
  muted: false, startedAt: null, timer: null,
  handled: new Set(), turns: 0, greeted: false,
  firstAudioAt: null, assistantSpeaking: false,
  // Buffered assistant transcript for the turn in flight. See sayCommit.
  say: { pending: '', bubble: null, body: null, audioAt: null,
         doneAt: null, committed: false },
  // Speaking rate measured from this session's completed turns, chars/sec.
  // Only used to trim an interrupted turn to what was heard.
  cps: null,
  // Playback state derived from the remote stream's level. See watchOutput.
  out: { playing: false, silentAt: null },
  // Diagnostics, reported once at the end of the call.
  seen: new Set(), commits: {},
};

// ── chrome ────────────────────────────────────────────────────────────
function conn(text, kind) {
  $('conn').className = `conn ${kind || ''}`;
  $('conn-text').textContent = text;
}

function stage(title, sub) {
  $('stage-title').textContent = title;
  if (sub !== undefined) $('stage-sub').textContent = sub;
}

function toast(text, kind) {
  const el = document.createElement('div');
  el.className = `toast ${kind || ''}`;
  setHTML(el, icon(kind === 'bad' ? 'alert' : 'check') + `<span>${esc(text)}</span>`);
  $('toasts').appendChild(el);
  setTimeout(() => el.remove(), 5000);
}

function showError(message, retry) {
  const box = $('rt-error');
  box.hidden = false;
  setHTML(box, icon('alert') +
    `<div><b>Voice connection problem.</b> ${esc(message)}</div>` +
    `<button class="btn ghost sm" id="btn-retry">Try again</button>`);
  $('btn-retry').onclick = () => { box.hidden = true; if (retry) retry(); };
}

function clearError() { $('rt-error').hidden = true; }

// ── api ───────────────────────────────────────────────────────────────
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

async function api(path, body, extra) {
  const r = await fetch(CONFIG.apiEndpoint + path, {
    method: 'POST',
    headers: Object.assign({ 'Content-Type': 'application/json',
                             Authorization: `Bearer ${S.token}` }, extra || {}),
    body: JSON.stringify(body || {}),
  });
  const text = await r.text();
  let d; try { d = JSON.parse(text); } catch { d = { error: text }; }
  if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
  return d;
}

/* Telemetry. Timings and lifecycle only — never transcript content. Failure to
   report must never disturb the call, so it is swallowed. */
function telemetry(name, fields) {
  if (!S.sessionId) return;
  api('/claim-voice-event', { event: name, session_id: S.sessionId, ...(fields || {}) })
    .catch(() => {});
}

// ── claim panel ───────────────────────────────────────────────────────
function renderClaim(c) {
  S.claim = c;
  const pending = c.status === 'PENDING_DOCUMENTS';
  setHTML($('claim-summary'), `
    <div class="statusrow ${pending ? 'pending' : 'ok'}">
      ${icon(pending ? 'alert' : 'check')}
      <div><b>${esc(c.status_label)}</b>
        <span class="sub">${esc(c.claim_id)} · ${esc(c.claim_category)}</span></div>
    </div>

    <dl class="kv">
      <dt>Member</dt><dd>${esc(c.member_name)}<br>
        <span class="sub">${esc(c.member_id)} · synthetic</span></dd>
      <dt>Provider</dt><dd>${esc(c.provider)}<br>
        <span class="sub">${esc(c.provider_city)} · ${esc(c.network_status)}</span></dd>
      <dt>Procedure</dt><dd>${esc(c.procedure)}</dd>
      <dt>Admission</dt><dd>${esc(c.admission)} → ${esc(c.discharge)}</dd>
      <dt>Claimed</dt><dd><b>${money(c.claimed_amount)}</b></dd>
    </dl>

    <div class="outstanding ${pending ? '' : 'clear'}">
      <p class="ol-head">${icon('doc')} Outstanding items
        <b>${c.outstanding_items_count}</b></p>
      ${c.outstanding_items.length
        ? `<ul>${c.outstanding_items.map((i) =>
            `<li>${esc(i.document)}<span class="cl">${esc(i.source_clause)}</span></li>`).join('')}</ul>`
        : `<p class="empty-note">Nothing outstanding.</p>`}
      <p class="ol-foot">Documentary gaps only. The room-rent sub-limit is a deduction and is
         deliberately not listed here.</p>
    </div>

    <p class="synthnote">${icon('shield')} ${esc(c.note)}</p>`);
}

function renderAuthority(a) {
  setHTML($('auth-may'), a.may.map((x) => `<li>${esc(x)}</li>`).join(''));
  setHTML($('auth-maynot'), a.may_not.map((x) => `<li>${esc(x)}</li>`).join(''));
}

// ── transcript ────────────────────────────────────────────────────────
function line(role, text) {
  const host = $('transcript');
  host.querySelector('.empty')?.remove();
  const wrap = document.createElement('div');
  wrap.className = `rtmsg ${role}`;
  setHTML(wrap, `<div class="rtwho">${icon(role === 'user' ? 'user' : 'bot')}` +
    `<span>${role === 'user' ? 'USER' : 'ASSISTANT'}</span>` +
    `<time>${clock()}</time></div><div class="rtbody"></div>`);
  host.appendChild(wrap);
  wrap.querySelector('.rtbody').textContent = text;
  host.scrollTop = host.scrollHeight;
  S.turns += 1;
  $('turn-count').textContent = `${S.turns} turn${S.turns === 1 ? '' : 's'}`;
  return wrap;
}

/*
 * Assistant transcript, committed only after the words have been spoken.
 *
 * Two earlier attempts got this wrong and both showed text ahead of the voice:
 *
 *   1. Writing transcript deltas straight to the DOM. Deltas arrive as the model
 *      generates tokens, several times faster than speech, so whole sentences
 *      appeared before the caller heard a word.
 *   2. Buffering the deltas and revealing them at an assumed 16 characters per
 *      second from the moment audio "started". Still ahead, for two compounding
 *      reasons: response.output_audio.delta fires when audio chunks arrive over
 *      the network, not when the speaker plays them, so the clock started early;
 *      and a fixed rate cannot match a voice that pauses, so it then drifted
 *      further ahead over the turn.
 *
 * The lesson is that any approach which predicts speech progress will sometimes
 * run ahead of it. So this one does not predict. Nothing is written until the
 * audio for that turn has finished playing — at which point every word in the
 * buffer is known to have been spoken. During speech the bubble shows a
 * speaking indicator and no text. Rendering a word before it is spoken is now
 * structurally impossible rather than merely unlikely.
 *
 * The one estimate that remains is confined to interruptions, where we do need
 * to know how much of the answer the caller actually heard. That uses a rate
 * MEASURED from this session's own completed turns — characters divided by their
 * true playback duration — so it calibrates to this voice instead of guessing.
 */
const CPS_FALLBACK = 15;      // until the first turn has been measured

function sayReset() {
  S.say = { pending: '', bubble: null, body: null, audioAt: null,
            doneAt: null, committed: false };
}

/** Open the bubble with a speaking indicator. No words yet, by design. */
function sayIndicator() {
  if (S.say.bubble) return;
  S.say.bubble = line('assistant', '');
  S.say.body = S.say.bubble.querySelector('.rtbody');
  setHTML(S.say.body, '<span class="speaking" aria-label="speaking">'
                       + '<i></i><i></i><i></i></span>');
  const host = $('transcript');
  host.scrollTop = host.scrollHeight;
}

/**
 * Audio for this turn has begun playing out — called when the output level
 * actually rises, not when the transport reports a buffer.
 *
 * Time to first audio is reported from here for the same reason: it should mean
 * "when the caller first heard something", which is later than "when the first
 * packet arrived" and is the number worth quoting.
 */
function sayAudioStart() {
  if (S.say.audioAt === null) S.say.audioAt = performance.now();
  sayIndicator();
  if (!S.firstAudioAt && S.startedAt) {
    S.firstAudioAt = Date.now();
    const ms = S.firstAudioAt - S.startedAt;
    $('ttfa').hidden = false;
    $('ttfa').textContent = `first audio ${ms} ms`;
    telemetry('first_assistant_audio', { ms_to_first_audio: ms });
  }
}

/**
 * Write the turn. `cut` means the caller interrupted, so trim to what was heard.
 * Idempotent: whichever end-of-turn event arrives first wins.
 */
function sayCommit(cut) {
  if (S.say.committed) return;
  const full = S.say.pending.trimEnd();
  if (!full && !S.say.bubble) { sayReset(); return; }
  S.say.committed = true;
  sayIndicator();

  const played = S.say.audioAt === null ? 0 : (performance.now() - S.say.audioAt) / 1000;
  let out = full;

  if (cut) {
    const heard = Math.max(0, Math.round(played * (S.cps || CPS_FALLBACK)));
    if (heard < full.length) out = full.slice(0, heard).trimEnd() + ' …';
  } else if (played > 0.6 && full.length > 24) {
    // A turn that played to completion gives a true rate for this voice.
    // Smoothed, so one odd turn cannot skew the interruption estimate.
    const measured = full.length / played;
    S.cps = S.cps ? S.cps * 0.7 + measured * 0.3 : measured;
  }

  S.say.body.textContent = out;
  const host = $('transcript');
  host.scrollTop = host.scrollHeight;
  sayReset();
}

function sysline(text, kind) {
  const host = $('transcript');
  host.querySelector('.empty')?.remove();
  const el = document.createElement('div');
  el.className = `rtsys ${kind || ''}`;
  setHTML(el, icon(kind === 'bad' ? 'alert' : kind === 'warn' ? 'alert' : 'check') +
    `<span>${esc(text)}</span>`);
  host.appendChild(el);
  host.scrollTop = host.scrollHeight;
}

// ── caller-facing outcomes ────────────────────────────────────────────
/*
 * This replaced a tool-activity console. The person on the call is a member or a
 * hospital clerk, so tool names, arguments and raw JSON are noise to them and a
 * distraction on screen. Only outcomes land here — what was agreed, and the
 * reference they can quote. Retrieval calls produce no entry at all, because
 * "we looked something up" is not news to the person who asked.
 *
 * The tool detail has not been lost: every call is still logged server-side with
 * its latency and status, and /ops reads those records.
 */
function update(kind, head, body, reference) {
  const host = $('updates');
  host.querySelector('.empty-note')?.remove();
  const el = document.createElement('div');
  el.className = `upd ${kind}`;
  setHTML(el, `<div class="upd-h">${icon(kind === 'bad' ? 'alert' : 'check')}` +
    `<b>${esc(head)}</b><time>${clock()}</time></div>` +
    `<p>${esc(body)}</p>` +
    (reference ? `<code class="upd-ref">${esc(reference)}</code>` : ''));
  host.prepend(el);
}

// ── audio meters ──────────────────────────────────────────────────────
function meter(stream, el, cssVar, onLevel) {
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
    if (onLevel) onLevel(level);
  };
}

/*
 * Playback boundaries, measured from the audio the caller is actually hearing.
 *
 * This is the third attempt at the transcript timing and the first one that does
 * not trust a server event to mean "the speech has finished". The logs showed
 * why the others failed: the only end-of-turn signal that reliably arrives is
 * `response.done`, and that fires when the model finishes GENERATING. For a
 * short answer generation completes in a fraction of the time it takes to speak
 * it, so committing there put the whole turn on screen while the voice was still
 * mid-sentence — the reported symptom, surviving both earlier fixes because both
 * ultimately keyed off a server event.
 *
 * The remote MediaStream is ground truth. We already run an analyser on it for
 * the output level meter, so the level is available every frame at no extra
 * cost. Rising above the floor means the caller has begun hearing this turn;
 * staying below it for OUT_HANG_MS means the turn has finished playing. Both are
 * observations of real audio, not predictions about it.
 *
 * The hang time has to clear the pauses inside a sentence without waiting so
 * long that the transcript feels detached. 700 ms comfortably exceeds normal
 * inter-word and inter-clause gaps. A pause longer than that would split one
 * turn into two bubbles, which is untidy but never early — and the failure mode
 * we are eliminating is text arriving before speech.
 */
const OUT_FLOOR = 0.02;
const OUT_HANG_MS = 700;

function watchOutput(level) {
  const now = performance.now();

  if (level > OUT_FLOOR) {
    S.out.silentAt = null;
    if (!S.out.playing) {
      S.out.playing = true;
      sayAudioStart();                 // true playback start
    }
    return;
  }

  if (S.out.playing) {
    if (S.out.silentAt === null) S.out.silentAt = now;
    else if (now - S.out.silentAt >= OUT_HANG_MS) {
      S.out.playing = false;
      S.out.silentAt = null;
      S.commits.audio = (S.commits.audio || 0) + 1;
      sayCommit(false);                // true end of playback
      S.assistantSpeaking = false;
      stage('Listening', 'Ask about the claim. Interrupt whenever you like.');
    }
    return;
  }

  // A turn that produced text but no audible audio at all. Only reachable once
  // response.done has arrived, so the buffer is known to be complete.
  if (S.say.doneAt !== null && now - S.say.doneAt >= 900 && S.say.pending) {
    S.say.doneAt = null;
    S.commits.textOnly = (S.commits.textOnly || 0) + 1;
    sayCommit(false);
  }
}

function runMeters(fns) {
  const tick = () => { fns.forEach((f) => f()); S.audioRaf = requestAnimationFrame(tick); };
  tick();
}

// ── the call ──────────────────────────────────────────────────────────
function startTimer() {
  S.startedAt = Date.now();
  $('call-timer').hidden = false;
  S.timer = setInterval(() => {
    const s = Math.floor((Date.now() - S.startedAt) / 1000);
    $('call-timer').textContent =
      `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
  }, 1000);
}

async function startCall() {
  clearError();
  $('btn-start').disabled = true;
  conn('connecting', 'busy');
  stage('Connecting', 'Setting up the voice session.');

  let session;
  try {
    session = await api('/claim-voice-session', {});
  } catch (e) {
    conn('failed', 'bad');
    stage('Ready when you are', 'Start the conversation and ask why the claim is still pending.');
    $('btn-start').disabled = false;
    showError(`Could not start a session — ${e.message}`, startCall);
    return;
  }

  S.sessionId = session.session_id;
  renderClaim(session.claim);
  renderAuthority(session.authority);
  $('claim-sync').textContent = `${session.model} · shared claim data`;
  // Show what the caller is about to hear during the second or so it takes to
  // connect, using the server's copy rather than a duplicate in this file.
  if (session.greeting) stage('Connecting', `“${session.greeting}”`);

  try {
    S.micStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true },
    });
  } catch (e) {
    conn('no microphone', 'bad');
    $('btn-start').disabled = false;
    showError('Microphone access was refused. Allow it in the browser, then try again.',
              startCall);
    return;
  }
  $('mic-state').textContent = 'microphone live';

  const pc = new RTCPeerConnection();
  S.pc = pc;
  const meters = [meter(S.micStream, $('lvl-in'), '--lvl-in')];

  pc.ontrack = (e) => {
    $('remote-audio').srcObject = e.streams[0];
    // The output level drives the transcript timing, not just the meter bar.
    meters.push(meter(e.streams[0], $('lvl-out'), '--lvl-out', watchOutput));
  };
  pc.addTrack(S.micStream.getAudioTracks()[0]);

  const dc = pc.createDataChannel('oai-events');
  S.dc = dc;
  dc.onmessage = (e) => onServerEvent(JSON.parse(e.data));
  dc.onopen = () => {
    conn('connected', 'live');
    clearError();
    $('levels').hidden = false;
    $('btn-mute').hidden = false;
    $('btn-stop').hidden = false;
    $('orb').classList.remove('idle');
    stage('Greeting you', 'The assistant speaks first.');
    startTimer();
    runMeters(meters);
    sysline('Connected. You can interrupt the assistant at any time.', 'good');
    telemetry('session_connected');
    // Fallback: if session.created never arrives, greet anyway rather than
    // leaving the caller listening to silence.
    setTimeout(greet, 700);
  };
  pc.oniceconnectionstatechange = () => {
    const st = pc.iceConnectionState;
    if (st === 'failed' || st === 'disconnected') {
      conn('unstable', 'bad');
      telemetry('connection_failed', { detail: st });
      showError('The connection dropped. The claim details above are unchanged.',
                () => { stopCall(true); startCall(); });
    }
  };

  try {
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    const r = await fetch(
      `https://api.openai.com/v1/realtime/calls?model=${encodeURIComponent(session.model)}`,
      { method: 'POST',
        headers: { Authorization: `Bearer ${session.client_secret}`,
                   'Content-Type': 'application/sdp' },
        body: offer.sdp });
    if (!r.ok) throw new Error(`handshake failed (${r.status})`);
    await pc.setRemoteDescription({ type: 'answer', sdp: await r.text() });
  } catch (e) {
    conn('failed', 'bad');
    telemetry('connection_failed', { detail: e.message });
    stopCall(true);
    $('btn-start').disabled = false;
    showError(`${e.message}. The Claims Specialist workflow is unaffected.`, startCall);
  }
}

function stopCall(quiet) {
  const duration = S.startedAt ? Date.now() - S.startedAt : 0;
  if (S.sessionId && S.seen.size) {
    // Ship the diagnostic before the session record is closed out. Event type
    // names and commit-path counters only — no transcript content.
    telemetry('realtime_events', {
      event_types: [...S.seen].sort().join(','),
      commit_paths: JSON.stringify(S.commits),
    });
  }
  if (!quiet && S.sessionId) {
    telemetry('session_ended', { session_duration_ms: duration, turn: S.turns });
  }
  S.dc?.close();
  S.pc?.close();
  S.micStream?.getTracks().forEach((t) => t.stop());
  cancelAnimationFrame(S.audioRaf);
  clearInterval(S.timer);
  S.pc = S.dc = S.micStream = null;
  S.startedAt = null;
  S.firstAudioAt = null;
  S.assistantSpeaking = false;
  S.greeted = false;          // so a second call is greeted too
  S.out = { playing: false, silentAt: null };
  S.seen = new Set();
  S.commits = {};
  sayReset();

  $('btn-start').disabled = false;
  $('btn-stop').hidden = true;
  $('btn-mute').hidden = true;
  $('levels').hidden = true;
  $('call-timer').hidden = true;
  $('lvl-in').style.width = $('lvl-out').style.width = '0%';
  const orb = $('orb');
  orb.classList.add('idle');
  orb.style.setProperty('--lvl-in', 0);
  orb.style.setProperty('--lvl-out', 0);
  $('mic-state').textContent = 'microphone idle';
  if (!quiet) {
    conn('conversation ended', '');
    stage('Conversation ended', 'Start another whenever you are ready.');
    sysline('Conversation ended.', '');
  }
}

function send(ev) {
  if (S.dc?.readyState === 'open') S.dc.send(JSON.stringify(ev));
}

/*
 * Make the assistant speak first.
 *
 * Realtime waits for input before it produces anything, so without an explicit
 * nudge the caller connects to silence and has to open the conversation. One
 * bare response.create is enough: the greeting itself lives in the session
 * instructions server-side, so no wording is duplicated here and the safety
 * rules stay attached to the turn. Passing per-response instructions would
 * override the session ones for that turn, which is not a trade worth making
 * for a hello.
 *
 * Guarded because it can be reached from both session.created and a timeout.
 */
function greet() {
  if (S.greeted || S.dc?.readyState !== 'open') return;
  S.greeted = true;
  send({ type: 'response.create' });
}

// ── realtime events ───────────────────────────────────────────────────
async function onServerEvent(ev) {
  // Diagnostic. Event type names only, reported once at the end of the call, so
  // the server can see which Realtime events this API version actually sends.
  // Without it the browser-to-OpenAI stream is invisible to CloudWatch.
  if (ev.type) S.seen.add(ev.type);

  switch (ev.type) {
    // The session is live and configured — safe to ask for the opening line.
    case 'session.created':
    case 'session.updated':
      greet();
      break;

    // Buffered only. These never reach the screen directly — they arrive as the
    // model generates, which is far ahead of the speech.
    case 'response.audio_transcript.delta':
    case 'response.output_audio_transcript.delta':
      S.say.pending += ev.delta || '';
      break;

    // Audio has been handed to the transport. NOT proof the caller can hear it
    // yet, so this only sets the speaking flag — the transcript waits for the
    // output level to actually rise. See watchOutput.
    case 'output_audio_buffer.started':
    case 'response.output_audio.delta':
      S.assistantSpeaking = true;
      stage('Speaking', 'Interrupt any time — just start talking.');
      break;

    // Precise end-of-playback, when this API version provides it. The audio
    // watcher reaches the same conclusion independently; whichever arrives
    // first wins, because sayCommit is idempotent.
    case 'output_audio_buffer.stopped':
      S.assistantSpeaking = false;
      S.out.playing = false;
      S.out.silentAt = null;
      S.commits.bufferStopped = (S.commits.bufferStopped || 0) + 1;
      sayCommit(false);
      break;

    // The server cleared its output buffer, which is what an interruption looks
    // like from here. Trim to what the caller actually heard.
    case 'output_audio_buffer.cleared':
      S.assistantSpeaking = false;
      S.out.playing = false;
      S.out.silentAt = null;
      S.commits.bufferCleared = (S.commits.bufferCleared || 0) + 1;
      sayCommit(true);
      break;

    // Generation finished. This is NOT the end of the speech — the audio is
    // still playing out — so it deliberately does not commit. It only marks the
    // buffer as complete, which lets watchOutput handle the rare turn that
    // produced text but no audible audio.
    case 'response.done':
      // Generation finished, not the speech. Recorded on the TURN so it cannot
      // outlive it and make the next turn look ready to commit.
      S.say.doneAt = performance.now();
      break;

    case 'conversation.item.input_audio_transcription.completed':
      if (ev.transcript) line('user', ev.transcript);
      break;

    // Barge-in. The caller started speaking; if the assistant had the floor,
    // that is an interruption and the server cancels the in-flight response.
    case 'input_audio_buffer.speech_started':
      $('mic-state').textContent = 'you are speaking';
      if (S.assistantSpeaking) {
        S.assistantSpeaking = false;
        // Commit before the notice, so the transcript reads in the order it
        // happened: what was heard, then the interruption.
        sayCommit(true);
        sysline('You interrupted — the assistant stopped to listen.', 'warn');
        telemetry('interruption', { turn: S.turns });
      }
      break;

    case 'input_audio_buffer.speech_stopped':
      $('mic-state').textContent = 'microphone live';
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
      sysline(`Session error: ${ev.error?.message || 'unknown'}`, 'bad');
      break;
  }
}

/* Forward one tool call to the backend and hand the result back to the model.
   Nothing is decided here. A failure returns a bounded error so the assistant
   says it cannot retrieve the information rather than inventing it. */
async function broker(name, callId, rawArgs) {
  if (!name || !callId || S.handled.has(callId)) return;
  S.handled.add(callId);

  let args = {};
  try { args = rawArgs ? JSON.parse(rawArgs) : {}; } catch { /* keep {} */ }

  let result;
  try {
    result = await api('/claim-voice-tool', { name, arguments: args },
                       { 'x-session-id': S.sessionId });
  } catch (e) {
    result = { status: 'error', result: null,
               error: 'That information is not available right now.' };
  }

  const payload = result.result || {};

  // Only outcomes reach the screen. A lookup that simply answered the caller's
  // question produces nothing here.
  if (payload.audit_reference) {
    update('good', 'Document request sent',
           `We have asked ${payload.sent_to || 'the hospital'} for `
           + payload.requested_documents.map((d) => d.document).join(' and ') + '.',
           payload.audit_reference);
  } else if (payload.status === 'handoff_created') {
    update('good', 'Passed to a claims specialist',
           'A specialist will pick this up and can discuss the amount payable.',
           payload.reference);
  } else if (result.status === 'not_permitted') {
    update('warn', 'That needs a claims specialist',
           'This assistant explains and retrieves. Approving or settling a claim, and the '
           + 'amount payable, are decided by a specialist.');
  } else if (result.status === 'not_found') {
    update('warn', 'Claim not found',
           'That claim number is not visible on this profile. Please check the number.');
  } else if (result.status === 'error') {
    update('bad', 'Could not retrieve that just now',
           'The assistant will say so rather than guess. Please try again in a moment.');
  }

  send({ type: 'conversation.item.create',
         item: { type: 'function_call_output', call_id: callId,
                 output: JSON.stringify(result) } });
  send({ type: 'response.create' });
}

function sendText(text) {
  line('user', text);
  send({ type: 'conversation.item.create',
         item: { type: 'message', role: 'user', content: [{ type: 'input_text', text }] } });
  send({ type: 'response.create' });
}

// ── wiring ────────────────────────────────────────────────────────────
function showUser(id) {
  S.user = id;
  $('who-av').textContent =
    (id.match(/\d+/) || ['?'])[0].slice(-2) || id.slice(0, 2).toUpperCase();
  $('who-name').textContent = id;
  $('who').hidden = false;
}

$('btn-signin').onclick = async () => {
  $('signin-error').textContent = '';
  const btn = $('btn-signin');
  btn.disabled = true; btn.textContent = 'Signing in…';
  try {
    const id = $('username').value.trim();
    if (!id || !$('password').value) throw new Error('Enter an agent ID and password.');
    S.token = await signIn(id, $('password').value);
    showUser(id);
    $('view-signin').hidden = true;
    $('view-app').hidden = false;
    conn('ready', 'ok');
  } catch (e) {
    $('signin-error').textContent = e.message;
  } finally {
    btn.disabled = false; btn.textContent = 'Sign in';
  }
};

$('btn-start').onclick = () => startCall();
$('btn-stop').onclick = () => stopCall();

$('btn-mute').onclick = () => {
  S.muted = !S.muted;
  S.micStream?.getAudioTracks().forEach((t) => { t.enabled = !S.muted; });
  $('btn-mute').classList.toggle('on', S.muted);
  setHTML($('btn-mute'), icon(S.muted ? 'micoff' : 'mic') +
    ` <span id="mute-label">${S.muted ? 'Unmute' : 'Mute'}</span>`);
  $('mic-state').textContent = S.muted ? 'microphone muted' : 'microphone live';
  if (S.muted) $('lvl-in').style.width = '0%';
};

$('btn-signout').onclick = () => { if (S.pc) stopCall(true); location.reload(); };

document.querySelectorAll('.pchip').forEach((chip) => {
  chip.onclick = () => {
    if (S.dc?.readyState !== 'open') { toast('Start the conversation first', 'warn'); return; }
    sendText(chip.dataset.say);
  };
});

window.addEventListener('beforeunload', () => { if (S.pc) stopCall(true); });
