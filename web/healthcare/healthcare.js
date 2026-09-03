/*
 * Claims Resolution Copilot — client.
 *
 * The browser does three things: show the synthetic claim package, ask the
 * backend to run the review, and capture the specialist's decision. It performs
 * no validation of its own — the gate shown on screen is the server's result,
 * and the approve button is disabled by the server, not by this file.
 *
 * It also computes no money. Every rupee figure rendered here arrives already
 * formatted from the settlement estimate the Lambda calculated.
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

// Indian lakh grouping, for the package view only. Amounts inside a review come
// pre-formatted from the server.
const money = (n) => {
  const s = Math.round(Math.abs(Number(n) || 0)).toString();
  if (s.length <= 3) return '₹' + s;
  const tail = s.slice(-3);
  let head = s.slice(0, -3), groups = [];
  while (head.length > 2) { groups.unshift(head.slice(-2)); head = head.slice(0, -2); }
  if (head) groups.unshift(head);
  return '₹' + groups.concat(tail).join(',');
};

const S = { token: null, user: null, pkg: null, review: null,
            cited: new Set(), editing: false };

/* Whose session this is. Everything on screen is scoped to this identity — the
   review is written against it and only it can decide the review. */
function showUser(id) {
  S.user = id;
  $('who-av').textContent = (id.match(/\d+/) || ['?'])[0].slice(-2) || id.slice(0, 2).toUpperCase();
  $('who-name').textContent = id;
  $('who').hidden = false;
}

function signOut() {
  S.token = null;
  location.reload();
}

function conn(text, kind) {
  $('conn').className = `conn ${kind || ''}`;
  $('conn-text').textContent = text;
}

function toast(text, kind) {
  const el = document.createElement('div');
  el.className = `toast ${kind || ''}`;
  setHTML(el, icon(kind === 'bad' ? 'alert' : 'check') + `<span>${esc(text)}</span>`);
  $('toasts').appendChild(el);
  setTimeout(() => el.remove(), 5000);
}

function step(n) {
  document.querySelectorAll('.flow li').forEach((li) => {
    const s = Number(li.dataset.step);
    li.classList.toggle('on', s === n);
    li.classList.toggle('done', s < n);
  });
}

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

async function api(path, body) {
  const r = await fetch(CONFIG.apiEndpoint + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${S.token}` },
    body: JSON.stringify(body || {}),
  });
  const text = await r.text();
  let d; try { d = JSON.parse(text); } catch { d = { error: text }; }
  if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
  return d;
}

// ── claim package ─────────────────────────────────────────────────────
function renderPackage() {
  const p = S.pkg;
  const c = p.claim;
  const n = p.policy_excerpts.length + p.provider_documents.length + p.claim_history.length;
  $('pkg-count').textContent = `${n} sources`;

  const src = (id, title, sec, text, extra = '') => `
    <div class="src${S.cited.has(id) ? ' cited' : ''}" data-src="${esc(id)}">
      <div class="src-h"><strong>${esc(title)}</strong><code>${esc(id)}</code></div>
      ${sec ? `<div class="sec">${esc(sec)}</div>` : ''}
      ${text ? `<p>${esc(text)}</p>` : ''}${extra}
    </div>`;

  const t = c.treatment, b = c.billing, pol = c.policy, h = c.hospital;

  setHTML($('package'), `
    <div class="pkg-group">
      <p class="pkg-label">Cashless claim</p>
      <dl class="kv">
        <dt>Claim</dt><dd><b>${esc(c.claim_id)}</b> · ${esc(c.claim_type)}</dd>
        <dt>Procedure</dt><dd>${esc(t.procedure)} · ${esc(t.icd_10)}</dd>
        <dt>Hospital</dt><dd>${esc(h.name)}<br><span class="sub">${esc(h.city)} · ${esc(h.network_status)}${h.nabh_accredited ? ' · NABH' : ''}</span></dd>
        <dt>Admission</dt><dd>${esc(t.admission)} → ${esc(t.discharge)} · ${t.length_of_stay_days} days</dd>
        <dt>Room</dt><dd>${esc(t.room_category_occupied)} · ${money(t.room_tariff_per_day)}/day</dd>
        <dt>Billed</dt><dd><b>${money(b.total_billed)}</b></dd>
        <dt>Policy</dt><dd>${esc(pol.product)}<br><span class="sub">SI ${money(pol.sum_insured)} · room limit ${pol.room_rent_sublimit_percent}% · ${pol.continuous_cover_months} months cover</span></dd>
        <dt>Pre-auth</dt><dd>${esc(c.pre_authorisation.reference)} · ${money(c.pre_authorisation.approved_amount)}</dd>
      </dl>
      <div class="missing-flag">${icon('alert')}
        ${c.documents_absent.map(esc).join(' · ')} absent${c.pre_authorisation.reference_quoted_on_final_bill ? '' : ' · pre-auth reference not on final bill'}</div>
      <div class="bill">
        ${Object.entries(b.breakup).map(([k, v]) => `
          <div class="bill-row"><span>${esc(k.replace(/_/g, ' '))}</span><b>${money(v)}</b></div>`).join('')}
      </div>
    </div>

    <div class="pkg-group">
      <p class="pkg-label">Policy and procedure clauses</p>
      ${p.policy_excerpts.map((x) =>
        src(x.source_id, x.document, x.section, x.text)).join('')}
    </div>

    <div class="pkg-group">
      <p class="pkg-label">Hospital documents</p>
      ${p.provider_documents.map((x) =>
        src(x.source_id, x.type, x.date, x.text)).join('')}
    </div>

    <div class="pkg-group">
      <p class="pkg-label">Prior claims on this policy</p>
      ${p.claim_history.map((x) => src(x.source_id,
        `${x.claim_id} · ${x.treatment}`,
        `${x.date_of_service} · ${x.claim_type} · ${x.status}`,
        `${x.note} Settled ${money(x.settled_amount)} of ${money(x.billed_amount)} billed.`)).join('')}
    </div>`);
}

// ── settlement estimate ───────────────────────────────────────────────
function renderSettlement(s) {
  const d = s.display;
  const risk = s.implant_at_risk > 0;

  setHTML($('settlement'), `
    <div class="calcnote">${icon('shield')}
      <span>Arithmetic from the billed heads and the policy. The model is not permitted to
        state any of these figures, and the gate rejects its output if it does.</span></div>

    <div class="sumrow">
      <div class="sumcell">
        <label>Billed</label><b>${esc(d.total_billed)}</b>
        <span class="sub">sum insured ${esc(d.sum_insured)}</span>
      </div>
      <div class="sumcell warn">
        <label>Proportionate deduction</label><b>−${esc(d.proportionate_deduction)}</b>
        <span class="sub">POL-RR-4.1 · room ${esc(d.actual_room_per_day)}/day vs eligible ${esc(d.eligible_room_per_day)}/day</span>
      </div>
      <div class="sumcell ok">
        <label>Payable if complete</label><b>${esc(d.payable_if_complete)}</b>
        <span class="sub">${s.within_pre_authorisation ? 'within' : 'exceeds'} pre-auth ${esc(d.pre_authorisation_approved)}</span>
      </div>
    </div>

    ${risk ? `<div class="atrisk">
      ${icon('alert')}
      <div>
        <b>${esc(d.implant_at_risk)} turns on the query.</b>
        Without the implant invoice, clause 7.3 removes the implant head and the claim settles at
        <b>${esc(d.payable_if_implant_unevidenced)}</b> instead of ${esc(d.payable_if_complete)}.
        That gap is what the letter is worth.
      </div></div>` : ''}

    <table class="heads">
      <thead><tr><th>Head of charge</th><th>Billed</th><th>Payable</th><th></th></tr></thead>
      <tbody>
        ${s.heads.map((h) => `
          <tr class="${h.varies_with_room ? 'varies' : ''}">
            <td>${esc(h.label)}</td>
            <td class="num">${esc(h.billed_display)}</td>
            <td class="num">${esc(h.payable_display)}</td>
            <td class="tag">${h.varies_with_room
              ? `<span class="vtag">varies with room</span>` : ''}</td>
          </tr>`).join('')}
      </tbody>
    </table>
    <p class="basisline">Basis ${s.basis.map((b) => `<code>${esc(b)}</code>`).join(' ')}
      · proportion ${esc(s.room.proportion_display)}
      · co-pay ${s.co_pay_percent}%</p>`);

  $('card-settlement').hidden = false;
}

// ── review ────────────────────────────────────────────────────────────
const STATUS_CLASS = {
  READY_TO_SETTLE: 'ready',
  QUERY_REQUIRED: 'needs',
  REFER_TO_MEDICAL_TEAM: 'refer',
};

function renderResult(d) {
  const r = d.recommendation;
  const v = d.validation;
  S.cited = new Set((r.evidence || []).map((e) => e.source_id));

  const cls = STATUS_CLASS[r.status] || 'needs';
  const label = r.status.replace(/_/g, ' ').toLowerCase();
  const engaged = (r.deductions_applicable || []).filter((x) => x.applies);

  setHTML($('result'), `
    <div class="verdictrow">
      <span class="bigstatus ${cls}">${esc(label)}</span>
      <span class="actionpill">${esc(r.recommended_action)}</span>
      <span class="hint-inline">confidence ${esc(r.confidence)}</span>
    </div>
    <p class="summary">${esc(r.summary)}</p>

    ${(r.missing_information || []).length ? `<div class="blk">
      <h4>${icon('doc')} Completeness gaps · goes in the query letter</h4>
      ${r.missing_information.map((m) => `
        <div class="gap">
          <div class="item">${esc(m.item)}</div>
          <div class="why">${esc(m.why_required)}</div>
          <span class="ref">${esc(m.source_id)}</span>
        </div>`).join('')}
    </div>` : ''}

    ${engaged.length ? `<div class="blk">
      <h4>${icon('minus')} Deductions · goes in the settlement advice, not the query</h4>
      ${engaged.map((x) => `
        <div class="gap ded">
          <div class="item">${esc(x.type.replace(/_/g, ' ').toLowerCase())}</div>
          <div class="why">${esc(x.reason)}</div>
          <span class="ref">${esc(x.basis_source_id)}</span>
        </div>`).join('')}
      <p class="nofigure">${icon('shield')} The copilot flags the provision. It does not
        quantify it — there is no amount field in its schema.</p>
    </div>` : ''}

    <div class="blk">
      <h4>Evidence · ${(r.evidence || []).length} citation(s)</h4>
      ${(r.evidence || []).map((e) => `
        <div class="ev">
          <div class="ev-h"><span class="rf">${esc(e.reference)}</span><code>${esc(e.source_id)}</code></div>
          <q>${esc(e.quote)}</q>
        </div>`).join('')}
    </div>

    <div class="blk">
      <h4>Rationale</h4>
      <p class="summary" style="font-size:13px">${esc(r.rationale)}</p>
    </div>

    <div class="gate">
      <div class="gate-h">
        ${icon(v.blocking ? 'alert' : 'shield')}
        <span class="tally ${v.blocking ? 'bad' : 'ok'}">${v.passed}/${v.total}</span>
        <span>deterministic checks on the model output${v.blocking ? ' · blocking failure' : ''}</span>
      </div>
      ${v.checks.map((c) => `
        <div class="chk ${c.ok ? 'ok' : 'no'}${c.blocking ? ' blocking' : ''}">
          ${icon(c.ok ? 'check' : 'x')}
          <span class="nm">${esc(c.check)}</span>
          <span class="dt">${esc(c.detail)}</span>
        </div>`).join('')}
    </div>`);

  renderPackage();   // re-render so cited sources highlight
  if (d.settlement) renderSettlement(d.settlement);
  renderDraft(r.draft_message);
  $('card-draft').hidden = false;
  $('card-decision').hidden = false;
  $('btn-approve').disabled = v.blocking;
  $('btn-edit').disabled = v.blocking;
  $('decide-note').textContent = v.blocking
    ? 'A blocking validation check failed. The server will refuse approval on this recommendation — it has to be re-run, not patched.'
    : 'The copilot recommends. You decide. Nothing has been sent to the hospital and no settlement has been recorded.';

  const m = d.meta;
  $('meta').textContent = `${m.model} · ${m.latency_ms} ms · ${m.input_tokens} in / ${m.output_tokens} out`;
  $('stat-model').hidden = false;
  $('stat-model').querySelector('b').textContent = m.model;
}

function renderDraft(dm) {
  setHTML($('draft'), `
    <div class="mail">
      <div class="mail-h">
        <div class="row"><span class="lbl">To</span><span class="val">${esc(dm.to)}</span></div>
        <div class="row"><span class="lbl">Subject</span><span class="val">${esc(dm.subject)}</span></div>
      </div>
      ${S.editing
        ? `<textarea class="mail-b" id="draft-body">${esc(dm.body)}</textarea>`
        : `<div class="mail-b">${esc(dm.body)}</div>`}
    </div>
    <div class="notsent">${icon('alert')} Drafted, not sent. Releasing it to the hospital
      requires your approval.</div>`);
}

// ── actions ───────────────────────────────────────────────────────────
$('btn-analyze').onclick = async () => {
  const btn = $('btn-analyze');
  btn.disabled = true;
  step(2);
  conn('reviewing', 'busy');
  setHTML($('result'), `<div class="thinking"><span class="spin"></span>
    Reading the claim, six policy clauses, three hospital documents and three prior claims…</div>`);
  try {
    const d = await api('/claim-analyze', {});
    S.review = d;
    renderResult(d);
    step(d.recommendation.draft_message ? 4 : 3);
    conn('reviewed', 'ok');
    toast(`${d.validation.passed}/${d.validation.total} checks passed`,
          d.validation.blocking ? 'bad' : 'good');
  } catch (e) {
    setHTML($('result'), `<p class="empty-note">${esc(e.message)}</p>`);
    conn('failed', 'bad');
    toast(e.message, 'bad');
  } finally {
    btn.disabled = false;
  }
};

$('btn-edit').onclick = () => {
  S.editing = !S.editing;
  renderDraft(S.review.recommendation.draft_message);
  setHTML($('btn-edit'), icon('edit') + (S.editing ? ' Done editing' : ' Edit first'));
  if (S.editing) toast('Editing the letter. Approve when you are satisfied.', 'warn');
};

async function decide(decision) {
  if (!S.review) return;
  step(5);
  const edited = S.editing ? ($('draft-body')?.value || null) : null;
  try {
    const d = await api('/claim-decision', {
      review_id: S.review.review_id, decision,
      edited_message: edited,
    });
    const good = decision !== 'reject';
    setHTML($('decision-result'), `
      <div class="outcome ${good ? 'ok' : 'no'}">
        ${icon(good ? 'check' : 'x')}
        <span><b>${esc(d.outcome.replace(/_/g, ' ').toLowerCase())}</b>
          ${esc(d.note)}
          <span class="sub">${esc(d.review_id)} · decided by ${esc(d.decided_by)} · audit ${esc(d.audit_chain)}</span>
        </span>
      </div>`);
    ['btn-approve', 'btn-edit', 'btn-reject'].forEach((b) => { $(b).disabled = true; });
    conn(`decision: ${decision}`, good ? 'ok' : '');
    toast(`Recorded — ${d.outcome.replace(/_/g, ' ').toLowerCase()}`, good ? 'good' : 'warn');
  } catch (e) {
    setHTML($('decision-result'), `<div class="outcome no">${icon('alert')}<span>${esc(e.message)}</span></div>`);
    toast(e.message, 'bad');
  }
}

$('btn-approve').onclick = () => decide('approve');
$('btn-reject').onclick = () => decide('reject');
$('btn-signout').onclick = signOut;

$('btn-signin').onclick = async () => {
  $('signin-error').textContent = '';
  const btn = $('btn-signin');
  btn.disabled = true; btn.textContent = 'Signing in…';
  try {
    const id = $('username').value.trim();
    if (!id || !$('password').value) throw new Error('Enter a specialist ID and password.');
    S.token = await signIn(id, $('password').value);
    S.pkg = await api('/claim-package', {});
    showUser(id);
    renderPackage();
    $('view-signin').hidden = true;
    $('view-app').hidden = false;
    conn('ready', 'ok');
    step(1);
  } catch (e) {
    $('signin-error').textContent = e.message;
  } finally {
    btn.disabled = false; btn.textContent = 'Sign in';
  }
};
