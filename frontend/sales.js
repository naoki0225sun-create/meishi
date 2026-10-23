// ============================================================
// 営業管理 — 案件一覧・詳細・活動ログ・接点
// Phase 2a: メール作成（用途タブ・署名・メーラー起動）と返信待ちアラート
// 名刺管理と同じログインセッションを使う
// ============================================================

const S = {
  meta: null,
  settings: { reply_wait_days: 3, stale_days: 14 },
  deals: [],
  events: [],
  purposes: [],
  signatures: [],
  filter: 'open',      // action | open | all | <stage名>
  q: '',
  selectedId: null,
  detail: null,
  actKind: 'meeting',
  picked: null,        // 新規案件モーダルで選んだ名刺
  showLostBox: false,
  compose: null,       // { purposeId, subject, body, to, source, open }
  editSig: null,       // 設定画面: 編集中の署名 id / 'new'
  editPurpose: null,   // 設定画面: 編集中の用途 id / 'new'
};

const $ = id => document.getElementById(id);
const isMobile = () => window.innerWidth <= 860;
const SIG_KEY = 'sales.signatureId';

// ── 共通 ──
async function api(path, options = {}) {
  const res = await fetch(path, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (res.status === 401) { location.href = '/'; throw new Error('Unauthorized'); }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

function esc(s) {
  if (s == null) return '';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
function pad(n) { return String(n).padStart(2, '0'); }
function todayStr() { const d = new Date(); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; }
function nowLocal() { const d = new Date(); return `${todayStr()}T${pad(d.getHours())}:${pad(d.getMinutes())}`; }
function fmtDate(s) { if (!s) return ''; const [, m, d] = s.slice(0, 10).split('-'); return `${Number(m)}/${Number(d)}`; }
function fmtDateTime(s) {
  if (!s) return '';
  const d = s.slice(0, 10).split('-'); const t = s.length > 10 ? s.slice(11, 16) : '';
  return `${d[0]}/${Number(d[1])}/${Number(d[2])}${t ? ' ' + t : ''}`;
}
function yen(n) { return n == null ? '' : '¥' + Number(n).toLocaleString('ja-JP'); }
function storeGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function storeSet(k, v) { try { localStorage.setItem(k, v); } catch { /* 使えない環境では無視 */ } }

function showToast(message, { type = 'info', title = '', duration = 3000 } = {}) {
  const c = $('toastContainer');
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.innerHTML = `<div class="toast-body">${title ? `<div class="toast-title">${esc(title)}</div>` : ''}
    <div class="toast-message">${esc(message)}</div></div>`;
  c.appendChild(el);
  setTimeout(() => { el.classList.add('is-leaving'); setTimeout(() => el.remove(), 250); }, duration);
}

function showConfirm({ title = '確認', message = '', okLabel = 'OK', danger = false }) {
  return new Promise(resolve => {
    const ov = $('confirmModalOverlay'), ok = $('confirmModalOk'), cancel = $('confirmModalCancel');
    $('confirmModalIcon').className = 'confirm-modal-icon' + (danger ? ' is-danger' : '');
    $('confirmModalIcon').textContent = danger ? '!' : '?';
    $('confirmModalTitle').textContent = title;
    $('confirmModalMessage').textContent = message;
    ok.textContent = okLabel;
    ok.className = 'confirm-modal-btn confirm-modal-btn-ok' + (danger ? ' is-danger' : '');
    ov.style.display = 'flex';
    const done = v => { ov.style.display = 'none'; ok.onclick = cancel.onclick = null; resolve(v); };
    ok.onclick = () => done(true);
    cancel.onclick = () => done(false);
  });
}

async function copyText(text, label) {
  try {
    await navigator.clipboard.writeText(text);
    showToast(`${label}をコピーしました`, { type: 'success', duration: 1500 });
  } catch {
    showToast('コピーできませんでした。手動で選択してコピーしてください', { type: 'warning' });
  }
}

// ── 初期化 ──
async function init() {
  try {
    const me = await api('/api/me');
    $('tenantName').textContent = me.tenant.display_name;
  } catch { return; }
  S.meta = await api('/api/sales/meta');
  await Promise.all([loadDeals(), loadEvents(), loadPurposes(), loadSignatures()]);

  const params = new URLSearchParams(location.search);
  const newCard = params.get('new');
  const openId = params.get('deal');
  const intakeId = params.get('intake');
  if (intakeId) {
    history.replaceState(null, '', '/sales.html');
    fetchCardsFlat().catch(() => {});
    openIntake(Number(intakeId));
  } else if (newCard) {
    history.replaceState(null, '', '/sales.html');
    openNewDeal(newCard);
  } else if (openId) {
    selectDeal(Number(openId));
  }
}

async function loadDeals() {
  try {
    const data = await api('/api/sales/deals');
    S.deals = data.deals;
    if (data.settings) S.settings = data.settings;
    renderFilters();
    renderList();
  } catch (e) {
    $('listPane').innerHTML = `<div class="empty-text">読み込みに失敗しました: ${esc(e.message)}</div>`;
  }
}
async function loadEvents() { S.events = (await api('/api/sales/events')).events; }
async function loadPurposes() { S.purposes = (await api('/api/sales/purposes')).purposes; }
async function loadSignatures() { S.signatures = (await api('/api/sales/signatures')).signatures; }

// ── フィルタ ──
function renderFilters() {
  const open = S.deals.filter(d => d.is_open);
  const action = S.deals.filter(d => d.needs_action);
  const counts = {};
  S.meta.stages.forEach(s => counts[s] = 0);
  S.deals.forEach(d => counts[d.stage] = (counts[d.stage] || 0) + 1);

  const chip = (key, label, n, extra = '') =>
    `<button class="chip ${S.filter === key ? 'chip-active' : ''} ${extra}" data-filter="${esc(key)}">${esc(label)}<span class="chip-count">${n}</span></button>`;

  let html = '';
  if (action.length || S.filter === 'action') html += chip('action', '要対応', action.length, 'chip-alert');
  html += chip('open', '進行中', open.length) + chip('all', 'すべて', S.deals.length);
  html += '<div class="filter-sep"></div>';
  html += S.meta.stages.map(s => chip(s, s, counts[s] || 0)).join('');
  $('stageFilter').innerHTML = html;
  $('stageFilter').querySelectorAll('[data-filter]').forEach(b => b.onclick = () => {
    S.filter = b.dataset.filter; renderFilters(); renderList();
  });
  $('dealCount').textContent = `進行中 ${open.length}件 / 全 ${S.deals.length}件` + (action.length ? ` / 要対応 ${action.length}件` : '');
}

function filteredDeals() {
  let list = S.deals;
  if (S.filter === 'action') list = list.filter(d => d.needs_action);
  else if (S.filter === 'open') list = list.filter(d => d.is_open);
  else if (S.filter !== 'all') list = list.filter(d => d.stage === S.filter);

  if (S.q) {
    const q = S.q.toLowerCase();
    list = list.filter(d => [d.title, d.company_snapshot, d.contact_snapshot, d.event_name, d.next_action,
      d.card && d.card.name, d.card && d.card.company].some(v => (v || '').toLowerCase().includes(q)));
  }
  // 要対応 → 今日期限 → 期限あり(近い順) → それ以外(更新順)
  const rank = d => d.needs_action ? 0 : d.due_today ? 1 : (d.is_open && d.next_action_due) ? 2 : 3;
  return [...list].sort((a, b) => {
    const r = rank(a) - rank(b);
    if (r) return r;
    if (rank(a) <= 2 && a.next_action_due && b.next_action_due && a.next_action_due !== b.next_action_due)
      return a.next_action_due < b.next_action_due ? -1 : 1;
    return (b.updated_at || '').localeCompare(a.updated_at || '');
  });
}

// ── 一覧 ──
function alertLine(d) {
  const a = d.alerts.find(x => x.level !== 'info') || d.alerts.find(x => x.level === 'info');
  if (!a) return '';
  return `<div class="row-alert level-${esc(a.level)}">${a.level === 'info' ? '✉' : '⚠'} ${esc(a.message)}</div>`;
}

function renderList() {
  const list = filteredDeals();
  const actionN = S.deals.filter(d => d.needs_action).length;
  const banner = actionN && S.filter !== 'action'
    ? `<button class="alert-banner" id="bannerBtn">要対応の案件が ${actionN} 件あります（電話・期限切れなど）→ 表示</button>` : '';

  if (!list.length) {
    $('listPane').innerHTML = banner + `<div class="empty-text">${S.deals.length
      ? '該当する案件はありません'
      : 'まだ案件がありません。右上の「＋ 新規案件」か、名刺詳細の「案件を作成」から登録できます'}</div>`;
  } else {
    $('listPane').innerHTML = banner + list.map(d => {
      const company = (d.card && d.card.company) || d.company_snapshot || '(会社不明)';
      const person = (d.card && d.card.name) || d.contact_snapshot || '';
      let due = '';
      if (d.is_open && d.next_action_due && !d.overdue) {
        const label = d.due_today ? '今日' : fmtDate(d.next_action_due);
        due = `<span class="due-badge ${d.due_today ? 'is-today' : ''}">⏱ ${esc(label)} ${esc(d.next_action || '')}</span>`;
      } else if (d.is_open && d.next_action && !d.next_action_due) {
        due = `<span>→ ${esc(d.next_action)}</span>`;
      }
      return `
        <div class="deal-row ${d.id === S.selectedId ? 'row-active' : ''}" data-id="${d.id}">
          <div class="deal-row-top">
            <div class="deal-company">${esc(company)}${person ? ` <span style="font-weight:400;color:var(--text-secondary)">${esc(person)}</span>` : ''}</div>
            <span class="stage-badge" data-stage="${esc(d.stage)}">${esc(d.stage)}</span>
          </div>
          <div class="deal-title"><span class="deal-no">#${d.id}</span> ${esc(d.title)}${d.amount != null ? ' · ' + yen(d.amount) : ''}</div>
          ${alertLine(d)}
          <div class="deal-meta">
            ${due}
            ${d.event_name ? `<span>📍${esc(d.event_name)}</span>` : ''}
            ${d.stage === '失注' && d.lost_reason ? `<span>理由: ${esc(d.lost_reason)}</span>` : ''}
            ${d.card_missing ? '<span class="missing-badge">名刺の紐づけ切れ</span>' : ''}
          </div>
        </div>`;
    }).join('');
    $('listPane').querySelectorAll('.deal-row').forEach(el => el.onclick = () => selectDeal(Number(el.dataset.id)));
  }
  if ($('bannerBtn')) $('bannerBtn').onclick = () => { S.filter = 'action'; renderFilters(); renderList(); };
}

// ── 詳細 ──
async function selectDeal(id) {
  if (S.selectedId !== id) S.compose = null;
  S.selectedId = id;
  S.showLostBox = false;
  $('listPane').querySelectorAll('.deal-row').forEach(el =>
    el.classList.toggle('row-active', Number(el.dataset.id) === id));
  try {
    S.detail = await api(`/api/sales/deals/${id}`);
  } catch (e) {
    showToast(e.message, { type: 'error', title: '案件を開けません' });
    return;
  }
  renderDetail();
  if (isMobile()) $('mobileSheet').classList.add('is-open');
}

function detailContainer() { return isMobile() ? $('mobileSheetBody') : $('detailPane'); }

function alertsHtml(d) {
  if (!d.alerts.length) return '';
  return `<div class="detail-section alert-stack">${d.alerts.map(a => {
    let btns = '';
    if (a.type === 'no_reply' || a.type === 'waiting') {
      btns = `<button class="btn-sm" data-replied="${a.activity_id}">返信あり</button>`;
      if (a.type === 'no_reply') btns += `<button class="btn-sm btn-sm-primary" data-log-call="1">電話を記録</button>`;
    } else if (a.type === 'overdue' || a.type === 'stale') {
      btns = `<button class="btn-sm" data-focus-next="1">次のアクションを更新</button>`;
    }
    return `<div class="alert-card level-${esc(a.level)}"><span>${a.level === 'info' ? '✉' : '⚠'} ${esc(a.message)}</span><span class="alert-btns">${btns}</span></div>`;
  }).join('')}</div>`;
}

function signatureBody() {
  const id = storeGet(SIG_KEY);
  const sig = S.signatures.find(s => String(s.id) === id);
  return sig ? sig.body : '';
}

function composeFullBody() {
  const c = S.compose;
  const sig = signatureBody();
  const body = (c.body || '').replace(/\s+$/, '');
  return sig ? `${body}\n\n${sig}` : body;
}

function composeHtml(d) {
  if (!d.is_open) return '';
  const c = S.compose;
  const tabs = S.purposes.map(p =>
    `<button class="act-kind ${c && c.purposeId === p.id ? 'is-active' : ''}" data-purpose="${p.id}">${esc(p.label)}</button>`).join('')
    || '<span class="card-result-sub">用途がありません（設定から追加）</span>';

  if (!c || !c.open) {
    return `<div class="detail-section">
      <div class="section-label"><span>メールを作成</span></div>
      <div class="compose-box"><div class="act-kinds">${tabs}</div>
        <div class="card-result-sub" style="margin-top:6px">用途を選ぶと下書きが作られます。送信はお使いのメーラーで行います</div></div>
    </div>`;
  }
  const sigId = storeGet(SIG_KEY) || '';
  const sigOpts = `<option value="">署名なし</option>` + S.signatures.map(s =>
    `<option value="${s.id}" ${String(s.id) === sigId ? 'selected' : ''}>${esc(s.label)}</option>`).join('');
  const purpose = S.purposes.find(p => p.id === c.purposeId);
  const suggestAdvance = d.stage === '名刺交換';
  const advanceTo = '御礼済';
  return `<div class="detail-section">
    <div class="section-label"><span>メールを作成</span>
      <button class="section-action" id="composeClose">閉じる</button></div>
    <div class="compose-box">
      <div class="act-kinds">${tabs}</div>
      <div class="compose-row">
        <span class="compose-source">${c.source === 'ai' ? 'AIの下書き' : '雛形から作成'}</span>
        <span style="flex:1"></span>
        <button class="btn-sm" id="composeTpl">雛形に戻す</button>
        ${S.meta.ai_available
          ? `<button class="btn-sm btn-sm-primary" id="composeAi">AIで作り直す</button>`
          : `<span class="card-result-sub" title="サーバーに ANTHROPIC_API_KEY を設定すると使えます">AI未設定</span>`}
      </div>
      ${S.meta.ai_available ? `<input class="email-input compact" id="composeNote" placeholder="AIへの追加指示（任意）例: 来週の訪問日程も聞く" maxlength="500" value="${esc(c.note || '')}">` : ''}
      <div class="compose-to">宛先: ${c.to ? esc(c.to) : '<span class="missing-badge">メールアドレスが名刺にありません</span>'}
        ${c.to ? `<button class="btn-link" data-copy="to">コピー</button>` : ''}</div>
      <input class="email-input compact" id="composeSubject" value="${esc(c.subject)}" placeholder="件名">
      <textarea class="email-input compact compose-body" id="composeBody">${esc(c.body)}</textarea>
      <div class="compose-row">
        <select class="email-input compact" id="composeSig" style="max-width:220px">${sigOpts}</select>
        <span class="card-result-sub">${S.signatures.length ? '' : '署名は「設定」から登録できます'}</span>
      </div>
      <div class="compose-actions">
        <button class="btn-sm btn-sm-primary" id="openMailer" ${c.to ? '' : 'disabled'}>メーラーで開く</button>
        <button class="btn-sm" id="openGmail" ${c.to ? '' : 'disabled'}>Gmailで開く</button>
        <button class="btn-sm" data-copy="subject">件名をコピー</button>
        <button class="btn-sm" data-copy="body">本文をコピー（署名込み）</button>
      </div>
      <div class="compose-sent">
        <div class="card-result-sub">送信したら記録してください。${S.settings.reply_wait_days}営業日返信がなければ電話アラートが出ます</div>
        ${suggestAdvance ? `<label class="compose-check"><input type="checkbox" id="advanceStage" ${purpose && /御礼|お礼/.test(purpose.label) ? 'checked' : ''}> ステージを「${advanceTo}」に進める</label>` : ''}
        <button class="btn-sm btn-sm-primary" id="markSent">送信済みにする</button>
      </div>
    </div>
  </div>`;
}

function renderDetail() {
  const d = S.detail;
  if (!d) return;
  const card = d.card || {};
  const company = card.company || d.company_snapshot || '';
  const person = card.name || d.contact_snapshot || '';
  const stages = S.meta.stages;
  const curIdx = stages.indexOf(d.stage);

  const stepper = stages.map((s, i) => {
    const cls = s === d.stage ? 'is-current' : (i < curIdx && i < 5 && curIdx < 5 ? 'is-past' : '');
    return `<button class="stage-step ${cls}" data-stage="${esc(s)}">${esc(s)}</button>`;
  }).join('');

  const lostBox = (S.showLostBox || d.stage === '失注') ? `
    <div class="lost-box">
      <div class="lost-box-title">${d.stage === '失注' ? '失注理由' : '失注にする理由を選んでください'}</div>
      <select class="email-input compact" id="lostReason">
        <option value="">— 選択 —</option>
        ${S.meta.lost_reasons.map(r => `<option ${d.lost_reason === r ? 'selected' : ''}>${esc(r)}</option>`).join('')}
      </select>
      <textarea class="email-input compact" id="lostDetail" rows="2" placeholder="詳細（任意）例: 他社見積が2割安かった">${esc(d.lost_detail || '')}</textarea>
      <div style="display:flex;gap:6px;justify-content:flex-end">
        ${d.stage !== '失注' ? '<button class="btn-sm" id="lostCancel">やめる</button>' : ''}
        <button class="btn-sm btn-sm-primary" id="lostSave">${d.stage === '失注' ? '理由を更新' : '失注にする'}</button>
      </div>
    </div>` : '';

  const eventOpts = `<option value="">— なし —</option>` + S.events.map(e =>
    `<option value="${e.id}" ${d.event_id === e.id ? 'selected' : ''}>${esc(e.name)}${e.event_date ? ` (${fmtDate(e.event_date)})` : ''}</option>`).join('');

  const kinds = S.meta.activity_kinds;
  const kindBtns = ['meeting', 'call', 'email', 'memo'].map(k =>
    `<button class="act-kind ${S.actKind === k ? 'is-active' : ''}" data-kind="${k}">${esc(kinds[k])}</button>`).join('');

  const timeline = (d.activities || []).map(a => {
    let badge = '';
    if (a.kind === 'email' && a.awaiting_reply) {
      badge = a.replied_at
        ? '<span class="tl-badge is-done">返信あり</span>'
        : `<span class="tl-badge">返信待ち</span><button class="btn-link" data-replied="${a.id}">返信あり</button>`;
    }
    return `
    <div class="tl-item" data-kind="${esc(a.kind)}">
      <div class="tl-dot"></div>
      <div>
        <div class="tl-head">
          <span class="tl-kind">${esc(kinds[a.kind] || a.kind)}</span>
          <span class="tl-time">${esc(fmtDateTime(a.occurred_at))}</span>
          ${badge}
          ${a.kind !== 'stage' ? `<button class="tl-del" data-del-act="${a.id}" title="削除">削除</button>` : ''}
        </div>
        ${a.title ? `<div class="tl-title">${esc(a.title)}</div>` : ''}
        ${a.body ? `<details class="tl-details" ${a.kind === 'email' ? '' : 'open'}><summary>${a.kind === 'email' ? '本文を表示' : '内容'}</summary><div class="tl-body">${esc(a.body)}</div></details>` : ''}
      </div>
    </div>`;
  }).join('') || '<div class="empty-text" style="padding:16px">記録はまだありません</div>';

  const contact = [
    card.department_position ? esc(card.department_position) : '',
    card.email ? `<a href="mailto:${esc(card.email)}">${esc(card.email)}</a>` : '',
    card.mobile ? `<a href="tel:${esc(card.mobile.replace(/[^\d+]/g, ''))}">${esc(card.mobile)}</a>` : '',
    card.phone ? `<a href="tel:${esc(card.phone.replace(/[^\d+]/g, ''))}">${esc(card.phone)}</a>` : '',
  ].filter(Boolean).join(' · ');

  detailContainer().innerHTML = `
    <div class="detail-body">
      <div class="detail-section">
        <div class="deal-title-row"><span class="deal-no deal-no-lg" title="Slackで「#${d.id}」と返信するとこの案件に記録できます">#${d.id}</span>
        <input class="deal-title-input" id="dealTitle" value="${esc(d.title)}" maxlength="120"></div>
        <div class="deal-sub"><strong>${esc(company)}</strong> ${esc(person)}</div>
        ${contact ? `<div class="deal-sub field-value">${contact}</div>` : ''}
        ${d.card_missing ? '<div class="deal-sub missing-badge">名刺管理側で該当の名刺が見つかりません（削除または会社名・氏名の変更）。表示は登録時の控えです</div>' : ''}
      </div>

      ${alertsHtml(d)}

      <div class="detail-section">
        <div class="section-label"><span>ステージ</span></div>
        <div class="stage-stepper">${stepper}</div>
        ${lostBox}
      </div>

      <div class="detail-section">
        <div class="section-label"><span>案件情報</span></div>
        <div class="kv-grid">
          <div class="k">次のアクション</div>
          <div class="v"><input class="email-input compact" id="fNext" value="${esc(d.next_action || '')}" placeholder="例: 見積送付" maxlength="200"></div>
          <div class="k">期限</div>
          <div class="v"><input type="date" class="email-input compact" id="fDue" value="${esc(d.next_action_due || '')}"></div>
          <div class="k">金額（税抜）</div>
          <div class="v"><input type="number" min="0" step="1000" class="email-input compact" id="fAmount" value="${d.amount ?? ''}" placeholder="未定"></div>
          <div class="k">接点</div>
          <div class="v"><select class="email-input compact" id="fEvent">${eventOpts}</select></div>
          <div class="k">登録日</div>
          <div class="v" style="font-size:13px;color:var(--text-secondary)">${esc(fmtDateTime(d.created_at))}${d.closed_at ? ` ／ 完了 ${esc(fmtDateTime(d.closed_at))}` : ''}</div>
        </div>
      </div>

      ${composeHtml(d)}

      <div class="detail-section" id="recordSection">
        <div class="section-label"><span>記録を追加</span></div>
        <div class="act-form">
          <div class="act-kinds">${kindBtns}</div>
          <div class="sales-grid-2">
            <input type="datetime-local" class="email-input compact" id="aWhen" value="${nowLocal()}">
            <input class="email-input compact" id="aTitle" placeholder="件名（例: 初回打合せ）" maxlength="200">
          </div>
          <textarea class="email-input compact" id="aBody" placeholder="内容・議事メモ（AI議事録は今後追加します）"></textarea>
          <label class="compose-check" id="aAwaitWrap" style="${S.actKind === 'email' ? '' : 'display:none'}"><input type="checkbox" id="aAwait" checked> 返信待ちにする（返信がなければ電話アラート）</label>
          <div class="sales-grid-2">
            <input class="email-input compact" id="aNext" placeholder="次のアクション（任意）" maxlength="200">
            <input type="date" class="email-input compact" id="aDue">
          </div>
          <div style="display:flex;justify-content:flex-end"><button class="btn-sm btn-sm-primary" id="aSave">記録する</button></div>
        </div>
        <div class="timeline">${timeline}</div>
      </div>

      <div style="display:flex;justify-content:flex-end">
        <button class="danger-link" id="deleteDeal">この案件を削除</button>
      </div>
    </div>`;

  bindDetail();
}

function bindDetail() {
  const root = detailContainer();
  const d = S.detail;
  const q = sel => root.querySelector(sel);

  // 案件名
  const titleEl = q('#dealTitle');
  titleEl.onblur = () => { if (titleEl.value.trim() && titleEl.value.trim() !== d.title) patchDeal({ title: titleEl.value.trim() }); };
  titleEl.onkeydown = e => { if (e.key === 'Enter') titleEl.blur(); };

  // アラート
  root.querySelectorAll('[data-replied]').forEach(b => b.onclick = () => markReplied(b.dataset.replied));
  root.querySelectorAll('[data-log-call]').forEach(b => b.onclick = () => {
    S.actKind = 'call'; renderDetail();
    const t = detailContainer().querySelector('#aTitle');
    t.value = '返信がないため電話';
    detailContainer().querySelector('#recordSection').scrollIntoView({ behavior: 'smooth', block: 'start' });
    detailContainer().querySelector('#aBody').focus();
  });
  root.querySelectorAll('[data-focus-next]').forEach(b => b.onclick = () => { q('#fNext').focus(); q('#fNext').select(); });

  // ステージ
  root.querySelectorAll('.stage-step').forEach(b => b.onclick = () => {
    const s = b.dataset.stage;
    if (s === d.stage) return;
    if (s === '失注') { S.showLostBox = true; renderDetail(); return; }
    patchDeal({ stage: s }, true);
  });
  if (q('#lostCancel')) q('#lostCancel').onclick = () => { S.showLostBox = false; renderDetail(); };
  if (q('#lostSave')) q('#lostSave').onclick = () => {
    const reason = q('#lostReason').value;
    if (!reason) { showToast('失注理由を選んでください', { type: 'warning' }); return; }
    patchDeal({ stage: '失注', lost_reason: reason, lost_detail: q('#lostDetail').value }, true);
  };

  // 案件情報（変更したら即保存）
  q('#fNext').onchange = e => patchDeal({ next_action: e.target.value });
  q('#fDue').onchange = e => patchDeal({ next_action_due: e.target.value || null });
  q('#fAmount').onchange = e => patchDeal({ amount: e.target.value === '' ? null : Math.max(0, parseInt(e.target.value, 10) || 0) });
  q('#fEvent').onchange = e => patchDeal({ event_id: e.target.value ? Number(e.target.value) : null });

  bindCompose(root);

  // 記録
  root.querySelectorAll('#recordSection .act-kind').forEach(b => b.onclick = () => {
    S.actKind = b.dataset.kind;
    root.querySelectorAll('#recordSection .act-kind').forEach(x => x.classList.toggle('is-active', x === b));
    q('#aAwaitWrap').style.display = S.actKind === 'email' ? '' : 'none';
  });
  q('#aSave').onclick = async () => {
    const payload = {
      kind: S.actKind,
      occurred_at: q('#aWhen').value ? q('#aWhen').value + ':00' : null,
      title: q('#aTitle').value,
      body: q('#aBody').value,
      awaiting_reply: S.actKind === 'email' && q('#aAwait').checked,
    };
    if (!payload.title.trim() && !payload.body.trim()) { showToast('件名か内容を入力してください', { type: 'warning' }); return; }
    if (q('#aNext').value.trim() || q('#aDue').value) {
      payload.next_action = q('#aNext').value;
      payload.next_action_due = q('#aDue').value || null;
    }
    q('#aSave').disabled = true;
    try {
      await api(`/api/sales/deals/${d.id}/activities`, { method: 'POST', body: JSON.stringify(payload) });
      showToast('記録しました', { type: 'success', duration: 1500 });
      await refreshCurrent();
    } catch (e) {
      showToast(e.message, { type: 'error', title: '記録に失敗' });
      q('#aSave').disabled = false;
    }
  };
  root.querySelectorAll('[data-del-act]').forEach(b => b.onclick = async () => {
    if (!await showConfirm({ title: 'この記録を削除しますか？', okLabel: '削除', danger: true })) return;
    try { await api(`/api/sales/activities/${b.dataset.delAct}`, { method: 'DELETE' }); await refreshCurrent(); }
    catch (e) { showToast(e.message, { type: 'error' }); }
  });

  q('#deleteDeal').onclick = async () => {
    if (!await showConfirm({ title: '案件を削除しますか？', message: `「${d.title}」と記録がすべて削除されます。名刺は削除されません。`, okLabel: '削除する', danger: true })) return;
    try {
      await api(`/api/sales/deals/${d.id}`, { method: 'DELETE' });
      S.selectedId = null; S.detail = null; S.compose = null;
      $('detailPane').innerHTML = '<div class="detail-empty"><p class="detail-empty-text">案件を選択すると詳細が表示されます</p></div>';
      $('mobileSheet').classList.remove('is-open');
      await loadDeals();
      showToast('削除しました', { type: 'success', duration: 1500 });
    } catch (e) { showToast(e.message, { type: 'error' }); }
  };
}

// ── メール作成 ──
async function makeDraft(purposeId, mode) {
  const d = S.detail;
  const note = (detailContainer().querySelector('#composeNote') || {}).value || (S.compose && S.compose.note) || '';
  const btn = detailContainer().querySelector(mode === 'ai' ? '#composeAi' : '#composeTpl');
  if (btn) { btn.disabled = true; btn.textContent = mode === 'ai' ? 'AIが作成中…' : '作成中…'; }
  try {
    const r = await api(`/api/sales/deals/${d.id}/draft`, {
      method: 'POST', body: JSON.stringify({ purpose_id: purposeId, mode, note: mode === 'ai' ? note : null }),
    });
    S.compose = { purposeId, subject: r.subject, body: r.body, to: r.to, source: r.source, open: true, note };
    renderDetail();
  } catch (e) {
    showToast(e.message, { type: 'error', title: '下書きを作れませんでした' });
    if (btn) { btn.disabled = false; btn.textContent = mode === 'ai' ? 'AIで作り直す' : '雛形に戻す'; }
  }
}

function bindCompose(root) {
  const q = sel => root.querySelector(sel);
  root.querySelectorAll('[data-purpose]').forEach(b => b.onclick = () => {
    const pid = Number(b.dataset.purpose);
    if (S.compose && S.compose.open && S.compose.purposeId === pid) return;
    makeDraft(pid, 'template');
  });
  if (!S.compose || !S.compose.open) return;
  const c = S.compose;
  q('#composeClose').onclick = () => { S.compose = null; renderDetail(); };
  q('#composeTpl').onclick = () => makeDraft(c.purposeId, 'template');
  if (q('#composeAi')) q('#composeAi').onclick = () => makeDraft(c.purposeId, 'ai');
  if (q('#composeNote')) q('#composeNote').oninput = e => { c.note = e.target.value; };
  q('#composeSubject').oninput = e => { c.subject = e.target.value; };
  q('#composeBody').oninput = e => { c.body = e.target.value; };
  q('#composeSig').onchange = e => storeSet(SIG_KEY, e.target.value);

  root.querySelectorAll('[data-copy]').forEach(b => b.onclick = () => {
    const k = b.dataset.copy;
    if (k === 'to') copyText(c.to, '宛先');
    else if (k === 'subject') copyText(c.subject, '件名');
    else copyText(composeFullBody(), '本文');
  });
  q('#openMailer').onclick = () => {
    const body = composeFullBody().replace(/\r?\n/g, '\r\n');
    const url = `mailto:${encodeURIComponent(c.to)}?subject=${encodeURIComponent(c.subject)}&body=${encodeURIComponent(body)}`;
    location.href = url;
    showToast('メーラーで送信したら「送信済みにする」を押してください', { duration: 5000 });
  };
  q('#openGmail').onclick = () => {
    const url = 'https://mail.google.com/mail/?view=cm&fs=1'
      + `&to=${encodeURIComponent(c.to)}&su=${encodeURIComponent(c.subject)}&body=${encodeURIComponent(composeFullBody())}`;
    window.open(url, '_blank', 'noopener');
    showToast('Gmailで送信したら「送信済みにする」を押してください', { duration: 5000 });
  };
  q('#markSent').onclick = async () => {
    const adv = q('#advanceStage');
    q('#markSent').disabled = true;
    try {
      await api(`/api/sales/deals/${S.detail.id}/emails`, {
        method: 'POST',
        body: JSON.stringify({
          subject: c.subject, body: composeFullBody(),
          advance_stage: adv && adv.checked ? '御礼済' : null,
          set_followup: true,
        }),
      });
      S.compose = null;
      showToast(`記録しました。${S.settings.reply_wait_days}営業日返信がなければ電話アラートを出します`, { type: 'success', duration: 3500 });
      await refreshCurrent();
    } catch (e) {
      showToast(e.message, { type: 'error', title: '記録に失敗' });
      q('#markSent').disabled = false;
    }
  };
}

function markReplied(activityId) {
  const ov = $('replyOverlay'), note = $('replyNote'), btn = $('replySave');
  note.value = '';
  ov.style.display = 'flex';
  setTimeout(() => note.focus(), 50);
  btn.disabled = false;
  btn.onclick = async () => {
    btn.disabled = true;
    try {
      await api(`/api/sales/activities/${activityId}/replied`, {
        method: 'POST', body: JSON.stringify({ note: note.value.trim() || null }),
      });
      ov.style.display = 'none';
      showToast('返信ありにしました。アラートを止めました', { type: 'success', duration: 2000 });
      await refreshCurrent();
    } catch (e) {
      showToast(e.message, { type: 'error' });
      btn.disabled = false;
    }
  };
}

async function patchDeal(data, rerender = false) {
  try {
    await api(`/api/sales/deals/${S.detail.id}`, { method: 'PATCH', body: JSON.stringify(data) });
    if (rerender) S.showLostBox = false;
    await refreshCurrent(rerender);
    if (!rerender) showToast('保存しました', { type: 'success', duration: 1200 });
  } catch (e) {
    showToast(e.message, { type: 'error', title: '保存に失敗' });
    if (rerender) renderDetail();
  }
}

async function refreshCurrent(rerender = true) {
  const id = S.detail.id;
  const [detail] = await Promise.all([api(`/api/sales/deals/${id}`), loadDeals()]);
  S.detail = detail;
  if (rerender) renderDetail();
}

// ── 新規案件 ──
let cardCache = null;

async function fetchCardsFlat() {
  if (cardCache) return cardCache;
  const data = await api('/api/cards');
  cardCache = data.groups.flatMap(g => g.cards);
  return cardCache;
}

async function openNewDeal(presetCardId = null) {
  S.picked = null;
  $('cardSearch').value = '';
  $('cardResults').innerHTML = '';
  $('newTitle').value = '';
  $('newNext').value = '御礼メール';
  const due = new Date(); due.setDate(due.getDate() + 2);
  $('newDue').value = `${due.getFullYear()}-${pad(due.getMonth() + 1)}-${pad(due.getDate())}`;
  $('newStage').innerHTML = S.meta.stages.filter(s => !S.meta.closed_stages.includes(s))
    .map(s => `<option>${esc(s)}</option>`).join('');
  $('newEvent').innerHTML = '<option value="">— なし —</option>'
    + S.events.map(e => `<option value="${e.id}">${esc(e.name)}${e.event_date ? ` (${fmtDate(e.event_date)})` : ''}</option>`).join('')
    + '<option value="__new">＋ 新しい接点を登録…</option>';
  $('newEventFields').style.display = 'none';
  $('newEventName').value = ''; $('newEventDate').value = todayStr(); $('newEventPlace').value = '';
  renderPicked();
  $('newDealOverlay').style.display = 'flex';

  if (presetCardId) {
    try {
      const card = (await fetchCardsFlat()).find(c => c.id === presetCardId);
      if (card) { S.picked = card; renderPicked(); }
    } catch { /* 検索から選べばよい */ }
  }
  if (!S.picked) setTimeout(() => $('cardSearch').focus(), 50);
}

function renderPicked() {
  const p = S.picked;
  $('pickedCard').style.display = p ? 'flex' : 'none';
  $('cardSearch').style.display = p ? 'none' : '';
  $('cardResults').style.display = p ? 'none' : '';
  if (p) {
    $('pickedCard').innerHTML = `<div><div class="card-result-name">${esc(p.company)} ${esc(p.name)}</div>
      <div class="card-result-sub">${esc(p.department_position || '')} ${esc(p.email || '')}</div></div>
      <button class="btn-sm" id="unpick">変更</button>`;
    $('unpick').onclick = () => { S.picked = null; renderPicked(); $('cardSearch').focus(); };
  }
}

const searchCards = debounce(async () => {
  const q = $('cardSearch').value.trim().toLowerCase();
  if (!q) { $('cardResults').innerHTML = ''; return; }
  try {
    const cards = (await fetchCardsFlat()).filter(c =>
      [c.company, c.name, c.email, c.name_kana].some(v => (v || '').toLowerCase().includes(q))).slice(0, 20);
    $('cardResults').innerHTML = cards.length ? cards.map(c => `
      <div class="card-result" data-id="${esc(c.id)}">
        <span class="card-result-name">${esc(c.company)} ${esc(c.name)}</span>
        <span class="card-result-sub">${esc(c.department_position || '')} ${esc(c.email || '')}</span>
      </div>`).join('') : '<div class="card-result-sub" style="padding:8px">見つかりません</div>';
    $('cardResults').querySelectorAll('.card-result').forEach(el => el.onclick = () => {
      S.picked = cardCache.find(c => c.id === el.dataset.id); renderPicked();
    });
  } catch (e) { showToast(e.message, { type: 'error' }); }
}, 200);

async function createDeal() {
  if (!S.picked) { showToast('名刺を選んでください', { type: 'warning' }); return; }
  const btn = $('createDealBtn');
  btn.disabled = true;
  try {
    let eventId = $('newEvent').value;
    if (eventId === '__new') {
      const name = $('newEventName').value.trim();
      if (!name) { showToast('接点名を入力してください', { type: 'warning' }); btn.disabled = false; return; }
      const ev = await api('/api/sales/events', { method: 'POST', body: JSON.stringify({
        name, event_date: $('newEventDate').value || null, place: $('newEventPlace').value || null }) });
      eventId = ev.id;
      await loadEvents();
    }
    const res = await api('/api/sales/deals', { method: 'POST', body: JSON.stringify({
      card_id: S.picked.id,
      title: $('newTitle').value.trim() || null,
      stage: $('newStage').value,
      event_id: eventId ? Number(eventId) : null,
      next_action: $('newNext').value.trim() || null,
      next_action_due: $('newDue').value || null,
    }) });
    $('newDealOverlay').style.display = 'none';
    S.filter = 'open';
    await loadDeals();
    selectDeal(res.id);
    showToast('案件を作成しました', { type: 'success', duration: 1500 });
  } catch (e) {
    showToast(e.message, { type: 'error', title: '作成に失敗' });
  } finally {
    btn.disabled = false;
  }
}

// ── 集計 ──
async function openStats() {
  $('statsOverlay').style.display = 'flex';
  $('statsBody').innerHTML = '<div class="empty-text">集計中…</div>';
  try {
    const s = await api('/api/sales/stats');
    const openN = S.meta.stages.filter(x => !S.meta.closed_stages.includes(x)).reduce((a, x) => a + s.by_stage[x], 0);
    const maxStage = Math.max(1, ...Object.values(s.by_stage));
    const maxLost = Math.max(1, ...Object.values(s.lost_reasons));
    const bars = (obj, max, cls = '') => Object.entries(obj).map(([k, v]) => `
      <div class="bar-row"><span>${esc(k)}</span>
        <div class="bar-track"><div class="bar-fill ${cls}" style="width:${(v / max) * 100}%"></div></div>
        <span class="bar-num">${v}</span></div>`).join('');
    const eventRows = S.events.filter(e => e.deal_count).map(e => `
      <div class="bar-row" style="grid-template-columns:1fr 80px"><span>${esc(e.name)}</span>
        <span class="bar-num">${e.won_count || 0} / ${e.deal_count} 受注</span></div>`).join('');
    $('statsBody').innerHTML = `
      <div class="stat-tiles">
        <div class="stat-tile"><div class="stat-tile-label">進行中</div><div class="stat-tile-value">${openN}</div></div>
        <div class="stat-tile"><div class="stat-tile-label">受注率（受注÷完了）</div><div class="stat-tile-value">${s.win_rate == null ? '—' : Math.round(s.win_rate * 100) + '%'}</div></div>
        <div class="stat-tile"><div class="stat-tile-label">受注金額</div><div class="stat-tile-value" style="font-size:18px">${yen(s.won_amount)}</div></div>
      </div>
      <div class="section-label"><span>ステージ別</span></div>${bars(s.by_stage, maxStage)}
      <div class="section-label" style="margin-top:18px"><span>失注理由（${s.lost}件）</span></div>
      ${s.lost ? bars(s.lost_reasons, maxLost, 'is-lost') : '<div class="card-result-sub">失注はまだありません</div>'}
      ${eventRows ? `<div class="section-label" style="margin-top:18px"><span>接点別</span></div>${eventRows}` : ''}`;
  } catch (e) {
    $('statsBody').innerHTML = `<div class="empty-text">${esc(e.message)}</div>`;
  }
}

// ── 設定 ──
async function openSettings() {
  S.editSig = null; S.editPurpose = null;
  $('settingsOverlay').style.display = 'flex';
  try {
    const [st] = await Promise.all([api('/api/sales/settings'), loadSignatures(), loadPurposes()]);
    S.settings = st;
  } catch (e) { showToast(e.message, { type: 'error' }); }
  renderSettings();
}

function renderSettings() {
  const st = S.settings;
  const sigList = S.signatures.map(s => S.editSig === s.id ? sigForm(s) : `
    <div class="set-item"><div><div class="card-result-name">${esc(s.label)}</div>
      <div class="set-preview">${esc(s.body)}</div></div>
      <div class="set-btns"><button class="btn-sm" data-edit-sig="${s.id}">編集</button>
      <button class="btn-sm" data-del-sig="${s.id}">削除</button></div></div>`).join('');
  const purposeList = S.purposes.map(p => S.editPurpose === p.id ? purposeForm(p) : `
    <div class="set-item"><div><div class="card-result-name">${esc(p.label)}</div>
      <div class="set-preview">${esc(p.subject_tpl || '（件名なし）')}</div></div>
      <div class="set-btns"><button class="btn-sm" data-edit-purpose="${p.id}">編集</button>
      <button class="btn-sm" data-del-purpose="${p.id}">削除</button></div></div>`).join('');

  $('settingsBody').innerHTML = `
    <div class="section-label"><span>アラート</span></div>
    <div class="set-alert">
      <label>メール送信後 <input type="number" min="1" max="30" class="email-input compact num" id="sReply" value="${st.reply_wait_days}"> 営業日 返信がなければ「電話しましょう」</label>
      <label>記録が <input type="number" min="1" max="180" class="email-input compact num" id="sStale" value="${st.stale_days}"> 日間なければ「放置」アラート</label>
      <div class="card-result-sub">営業日は土日を除いて数えます（祝日は数に入ります）</div>
      <div style="text-align:right"><button class="btn-sm btn-sm-primary" id="saveAlertSettings">保存</button></div>
    </div>

    <div class="section-label" style="margin-top:22px"><span>署名</span>
      ${S.editSig === 'new' ? '' : '<button class="section-action" id="newSig">＋ 追加</button>'}</div>
    ${S.editSig === 'new' ? sigForm({ label: '', body: '' }) : ''}
    ${sigList || (S.editSig === 'new' ? '' : '<div class="card-result-sub">署名はまだありません</div>')}

    <div class="section-label" style="margin-top:22px"><span>メールの用途（タブ）</span>
      ${S.editPurpose === 'new' ? '' : '<button class="section-action" id="newPurpose">＋ 追加</button>'}</div>
    ${S.editPurpose === 'new' ? purposeForm({ label: '', subject_tpl: '', body_tpl: '', instruction: '' }) : ''}
    ${purposeList}`;

  const B = $('settingsBody');
  $('saveAlertSettings').onclick = async () => {
    try {
      await api('/api/sales/settings', { method: 'PUT', body: JSON.stringify({
        reply_wait_days: parseInt($('sReply').value, 10), stale_days: parseInt($('sStale').value, 10) }) });
      S.settings = await api('/api/sales/settings');
      showToast('アラート設定を保存しました', { type: 'success', duration: 1500 });
      await loadDeals();
      if (S.detail) await refreshCurrent();
    } catch (e) { showToast(e.message, { type: 'error' }); }
  };
  if ($('newSig')) $('newSig').onclick = () => { S.editSig = 'new'; renderSettings(); };
  if ($('newPurpose')) $('newPurpose').onclick = () => { S.editPurpose = 'new'; renderSettings(); };
  B.querySelectorAll('[data-edit-sig]').forEach(b => b.onclick = () => { S.editSig = Number(b.dataset.editSig); renderSettings(); });
  B.querySelectorAll('[data-edit-purpose]').forEach(b => b.onclick = () => { S.editPurpose = Number(b.dataset.editPurpose); renderSettings(); });
  B.querySelectorAll('[data-del-sig]').forEach(b => b.onclick = async () => {
    if (!await showConfirm({ title: '署名を削除しますか？', okLabel: '削除', danger: true })) return;
    try { await api(`/api/sales/signatures/${b.dataset.delSig}`, { method: 'DELETE' }); await loadSignatures(); renderSettings(); }
    catch (e) { showToast(e.message, { type: 'error' }); }
  });
  B.querySelectorAll('[data-del-purpose]').forEach(b => b.onclick = async () => {
    if (!await showConfirm({ title: '用途を削除しますか？', okLabel: '削除', danger: true })) return;
    try { await api(`/api/sales/purposes/${b.dataset.delPurpose}`, { method: 'DELETE' }); await loadPurposes(); renderSettings(); }
    catch (e) { showToast(e.message, { type: 'error' }); }
  });

  if ($('sigSave')) {
    $('sigCancel').onclick = () => { S.editSig = null; renderSettings(); };
    $('sigSave').onclick = async () => {
      const payload = { label: $('sigLabel').value, body: $('sigBody').value };
      const isNew = S.editSig === 'new';
      try {
        await api(isNew ? '/api/sales/signatures' : `/api/sales/signatures/${S.editSig}`,
          { method: isNew ? 'POST' : 'PUT', body: JSON.stringify(payload) });
        S.editSig = null; await loadSignatures(); renderSettings();
      } catch (e) { showToast(e.message, { type: 'error' }); }
    };
  }
  if ($('purSave')) {
    $('purCancel').onclick = () => { S.editPurpose = null; renderSettings(); };
    $('purSave').onclick = async () => {
      const payload = { label: $('purLabel').value, subject_tpl: $('purSubject').value,
        body_tpl: $('purBody').value, instruction: $('purInstr').value };
      const isNew = S.editPurpose === 'new';
      try {
        await api(isNew ? '/api/sales/purposes' : `/api/sales/purposes/${S.editPurpose}`,
          { method: isNew ? 'POST' : 'PUT', body: JSON.stringify(payload) });
        S.editPurpose = null; await loadPurposes(); renderSettings();
      } catch (e) { showToast(e.message, { type: 'error' }); }
    };
    B.querySelectorAll('[data-var]').forEach(c => c.onclick = () => {
      const ta = $('purBody'); const v = c.dataset.var;
      const s = ta.selectionStart; ta.value = ta.value.slice(0, s) + v + ta.value.slice(ta.selectionEnd);
      ta.focus(); ta.setSelectionRange(s + v.length, s + v.length);
    });
  }
}

function sigForm(s) {
  return `<div class="set-form">
    <input class="email-input compact" id="sigLabel" placeholder="署名名（例: 日野・ワコーメタル）" maxlength="40" value="${esc(s.label)}">
    <textarea class="email-input compact" id="sigBody" rows="6" placeholder="────────────&#10;会社名&#10;部署 氏名&#10;TEL: 000-000-0000&#10;name@example.com&#10;────────────">${esc(s.body)}</textarea>
    <div class="set-btns" style="justify-content:flex-end"><button class="btn-sm" id="sigCancel">キャンセル</button><button class="btn-sm btn-sm-primary" id="sigSave">保存</button></div>
  </div>`;
}

function purposeForm(p) {
  const vars = S.meta.template_vars.map(v => `<code data-var="{{${v}}}">{{${v}}}</code>`).join(' ');
  return `<div class="set-form">
    <input class="email-input compact" id="purLabel" placeholder="用途名（タブに表示）例: 訪問のお礼" maxlength="30" value="${esc(p.label)}">
    <input class="email-input compact" id="purSubject" placeholder="件名の雛形" maxlength="200" value="${esc(p.subject_tpl)}">
    <textarea class="email-input compact" id="purBody" rows="8" placeholder="本文の雛形（署名は不要）">${esc(p.body_tpl)}</textarea>
    <div class="email-variables-hint">変数（クリックで本文に挿入）: ${vars}</div>
    <textarea class="email-input compact" id="purInstr" rows="2" placeholder="AIへの指示（AIで作るときに使います）例: 見積りの提出を申し出る。図面と数量を依頼する">${esc(p.instruction)}</textarea>
    <div class="set-btns" style="justify-content:flex-end"><button class="btn-sm" id="purCancel">キャンセル</button><button class="btn-sm btn-sm-primary" id="purSave">保存</button></div>
  </div>`;
}

// ── イベント ──
$('searchInput').addEventListener('input', debounce(e => { S.q = e.target.value.trim(); renderList(); }, 150));
$('newDealBtn').onclick = () => openNewDeal();
$('statsBtn').onclick = openStats;
$('settingsBtn').onclick = openSettings;
$('cardSearch').addEventListener('input', searchCards);
$('createDealBtn').onclick = createDeal;
$('newEvent').onchange = e => { $('newEventFields').style.display = e.target.value === '__new' ? 'grid' : 'none'; };
$('mobileBackBtn').onclick = () => $('mobileSheet').classList.remove('is-open');
document.querySelectorAll('[data-close]').forEach(b => b.onclick = () => { $(b.dataset.close).style.display = 'none'; });
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  ['newDealOverlay', 'statsOverlay', 'settingsOverlay'].forEach(id => { $(id).style.display = 'none'; });
});

init();

// ── 資料から取り込み（AI） ──
const I = { job: null, dec: {}, file: null, busy: false };
const IK_KIND = { meeting: 'アポ・打合せ', call: '電話', email: 'メール', memo: 'メモ' };
const IK_STATUS = { matched: ['名刺が確定', 'ok'], ambiguous: ['名刺を確認', 'warn'], not_found: ['名刺が見つからない', 'ng'] };

function openIntake(jobId = null) {
  $('intakeOverlay').style.display = 'flex';
  if (jobId) { loadIntakeJob(jobId); return; }
  I.job = null; I.file = null;
  $('intakeTitle').textContent = '資料から取り込み';
  $('intakeBody').innerHTML = `
    <div class="ik-input">
      <div class="ik-help">営業の報告メモ・議事録・週報・案件一覧などを読み取り、名刺を探して案件の追加や記録の候補を作ります。追加する前に内容を確認できます。</div>
      <label class="ik-drop" id="ikDrop">
        <input type="file" id="ikFile" accept=".xlsx,.xlsm,.docx,.pptx,.pdf,.txt,.csv,.md,.eml,.jpg,.jpeg,.png,.webp" hidden>
        <span id="ikFileLabel">ファイルを選ぶ／ここにドロップ<br><small>Excel・Word・PowerPoint・PDF・画像・テキスト（15MBまで）</small></span>
      </label>
      <div class="email-field-label" style="margin-top:14px">文章を貼り付け（ファイルに添える補足にも使えます）</div>
      <textarea class="email-input" id="ikText" rows="7" placeholder="例：&#10;10/20 テスト工業の山田部長と打合せ。制御盤筐体10台の見積依頼あり。来週中に回答。&#10;北摂精機の架台は価格で失注。"></textarea>
    </div>`;
  $('intakeFooter').innerHTML = `
    <div class="email-footer-info" id="ikStatus">読み取りにはAIを使います（数十秒かかることがあります）</div>
    <div class="email-footer-actions">
      <button class="btn-sm" data-close="intakeOverlay">キャンセル</button>
      <button class="btn-sm btn-sm-primary" id="ikAnalyze">読み取る</button>
    </div>`;
  bindIntakeClose();
  const drop = $('ikDrop');
  const setFile = f => {
    I.file = f || null;
    $('ikFileLabel').innerHTML = f ? `📄 ${esc(f.name)}（${Math.ceil(f.size / 1024)}KB）<br><small>クリックで変更</small>`
      : 'ファイルを選ぶ／ここにドロップ<br><small>Excel・Word・PowerPoint・PDF・画像・テキスト（15MBまで）</small>';
    drop.classList.toggle('has-file', !!f);
  };
  $('ikFile').onchange = e => setFile(e.target.files[0]);
  drop.ondragover = e => { e.preventDefault(); drop.classList.add('over'); };
  drop.ondragleave = () => drop.classList.remove('over');
  drop.ondrop = e => { e.preventDefault(); drop.classList.remove('over'); setFile(e.dataTransfer.files[0]); };
  $('ikAnalyze').onclick = analyzeIntake;
  if (!S.meta.ai_available) {
    $('ikStatus').textContent = 'AIのキーが未設定のため読み取れません（管理者に設定を依頼してください）';
    $('ikAnalyze').disabled = true;
  }
}

function bindIntakeClose() {
  $('intakeOverlay').querySelectorAll('[data-close]').forEach(b => b.onclick = () => { $('intakeOverlay').style.display = 'none'; });
}

async function analyzeIntake() {
  const text = $('ikText').value.trim();
  if (!I.file && !text) { showToast('ファイルを選ぶか文章を貼り付けてください', { type: 'warning' }); return; }
  const fd = new FormData();
  if (I.file) fd.append('file', I.file);
  if (text) fd.append('text', text);
  const btn = $('ikAnalyze');
  btn.disabled = true; btn.textContent = '読み取り中…';
  $('ikStatus').textContent = 'AIが読み取っています。資料が長いと1分ほどかかります';
  try {
    const res = await fetch('/api/sales/intake', { method: 'POST', body: fd, credentials: 'include' });
    if (res.status === 401) { location.href = '/'; return; }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    showIntakeJob(data);
  } catch (e) {
    showToast(e.message, { type: 'error', title: '読み取りに失敗' });
    btn.disabled = false; btn.textContent = '読み取る';
    $('ikStatus').textContent = '読み取りにはAIを使います（数十秒かかることがあります）';
  }
}

async function loadIntakeJob(id) {
  $('intakeBody').innerHTML = '<div class="empty-text">読み込み中…</div>';
  $('intakeFooter').innerHTML = '';
  try { showIntakeJob(await api(`/api/sales/intake/${id}`)); }
  catch (e) { $('intakeBody').innerHTML = `<div class="empty-text">${esc(e.message)}</div>`; }
}

function ikOpenDeals(cardId, item) {
  const fromList = S.deals.filter(d => d.card_id === cardId && d.is_open).map(d => ({ id: d.id, title: d.title, stage: d.stage }));
  return fromList.length ? fromList : ((item.open_deals || {})[cardId] || []);
}

function showIntakeJob(job) {
  I.job = job; I.dec = {};
  $('intakeTitle').textContent = job.filename ? `取り込み：${job.filename}` : '取り込み：貼り付けた文章';
  for (const it of job.items) {
    I.dec[it.idx] = {
      idx: it.idx, action: it.action, card_id: it.card_id, deal_id: it.deal_id,
      title: it.title || '', stage: it.stage || '', lost_reason: it.lost_reason || '', kind: it.kind || 'memo',
      occurred_at: it.occurred_at || '', summary: it.summary || '', next_action: it.next_action || '',
      next_action_due: it.next_action_due || '', amount: it.amount ?? '',
    };
  }
  if (job.status === 'applied') {
    $('intakeBody').innerHTML = `<div class="ik-done">この取り込みは追加済みです。${ikResultHtml(job.applied)}</div>`;
    $('intakeFooter').innerHTML = '<div class="email-footer-info"></div><div class="email-footer-actions"><button class="btn-sm" data-close="intakeOverlay">閉じる</button></div>';
    bindIntakeClose(); bindDealLinks();
    return;
  }
  const head = `
    ${job.overview ? `<div class="ik-overview">${esc(job.overview)}</div>` : ''}
    ${job.unclear?.length ? `<div class="ik-unclear"><b>読み取れなかった点</b><ul>${job.unclear.map(u => `<li>${esc(u)}</li>`).join('')}</ul></div>` : ''}
    ${job.truncated ? '<div class="ik-unclear">件数が多いため先頭100件だけ表示しています</div>' : ''}
    ${job.items.length ? '' : '<div class="empty-text">案件になりそうな内容は見つかりませんでした</div>'}`;
  $('intakeBody').innerHTML = head + job.items.map(it => `<div class="ik-item" id="ik-${it.idx}"></div>`).join('');
  job.items.forEach(it => renderIkItem(it.idx));
  $('intakeFooter').innerHTML = `
    <div class="email-footer-info" id="ikCount"></div>
    <div class="email-footer-actions">
      <button class="btn-sm" data-close="intakeOverlay">あとで</button>
      <button class="btn-sm btn-sm-primary" id="ikApply">追加する</button>
    </div>`;
  bindIntakeClose();
  $('ikApply').onclick = applyIntake;
  updateIkCount();
}

function ikCandidates(it) {
  const list = it.candidates.map(c => ({ ...c }));
  const d = I.dec[it.idx];
  if (d.card_id && !list.some(c => c.card.id === d.card_id)) {
    const card = (cardCache || []).find(c => c.id === d.card_id);
    if (card) list.unshift({ score: 0, why: '手動で選択', card });
  }
  return list;
}

function renderIkItem(idx) {
  const it = I.job.items.find(x => x.idx === idx);
  const d = I.dec[idx];
  const el = $(`ik-${idx}`);
  const [label, cls] = IK_STATUS[it.status];
  const deals = d.card_id ? ikOpenDeals(d.card_id, it) : [];
  if (d.action === 'existing' && !deals.some(x => x.id === d.deal_id)) d.deal_id = deals[0]?.id || null;
  const stageOpts = `<option value="">${d.action === 'new' ? '名刺交換（指定なし）' : '変更しない'}</option>`
    + S.meta.stages.map(s => `<option ${s === d.stage ? 'selected' : ''}>${esc(s)}</option>`).join('');
  el.className = `ik-item ik-${cls} ${d.action === 'skip' ? (d.card_id ? 'ik-skip' : 'ik-skip ik-pick') : ''}`;
  el.innerHTML = `
    <div class="ik-head">
      <span class="ik-badge ${cls}">${label}</span>
      <span class="ik-who"><b>${esc(it.company || '（会社不明）')}</b> ${esc(it.person || '')}
        ${it.department ? `<small>${esc(it.department)}</small>` : ''}</span>
      <select class="email-input ik-action" data-f="action">
        <option value="new" ${d.action === 'new' ? 'selected' : ''}>新規案件にする</option>
        <option value="existing" ${d.action === 'existing' ? 'selected' : ''} ${deals.length ? '' : 'disabled'}>既存の案件に記録${deals.length ? '' : '（なし）'}</option>
        <option value="skip" ${d.action === 'skip' ? 'selected' : ''}>追加しない</option>
      </select>
    </div>
    ${it.evidence ? `<div class="ik-evidence">「${esc(it.evidence)}」</div>` : ''}
    <div class="ik-body">
      <div class="ik-cards">
        <div class="email-field-label">名刺</div>
        ${ikCandidates(it).map(c => `
          <label class="ik-card">
            <input type="radio" name="ikc-${idx}" value="${esc(c.card.id)}" ${c.card.id === d.card_id ? 'checked' : ''}>
            <span><b>${esc(c.card.company || '')} ${esc(c.card.name || '')}</b>
              <small>${esc(c.card.department_position || '')} ${esc(c.card.email || '')}</small></span>
            <span class="ik-why">${esc(c.why)}</span>
          </label>`).join('') || '<div class="ik-none">名刺管理に見つかりませんでした。下で検索するか、先に名刺を登録してください</div>'}
        <input type="search" class="email-input ik-search" placeholder="別の名刺を検索（会社名・氏名）" autocomplete="off">
        <div class="card-results ik-results"></div>
      </div>
      <div class="ik-fields">
        ${d.action === 'existing' ? `<div class="sales-field"><div class="email-field-label">記録する案件</div>
          <select class="email-input" data-f="deal_id">${deals.map(x => `<option value="${x.id}" ${x.id === d.deal_id ? 'selected' : ''}>#${x.id} ${esc(x.title)}（${esc(x.stage)}）</option>`).join('')}</select></div>`
        : `<div class="sales-field"><div class="email-field-label">案件名</div>
          <input type="text" class="email-input" data-f="title" value="${esc(d.title)}" maxlength="120" placeholder="例: 制御盤筐体 10台"></div>`}
        <div class="sales-grid-3">
          <div class="sales-field"><div class="email-field-label">ステージ</div><select class="email-input" data-f="stage">${stageOpts}</select></div>
          <div class="sales-field"><div class="email-field-label">種類</div><select class="email-input" data-f="kind">
            ${Object.entries(IK_KIND).map(([k, v]) => `<option value="${k}" ${k === d.kind ? 'selected' : ''}>${v}</option>`).join('')}</select></div>
          <div class="sales-field"><div class="email-field-label">日付</div><input type="date" class="email-input" data-f="occurred_at" value="${esc(d.occurred_at)}"></div>
        </div>
        ${d.stage === '失注' ? `<div class="sales-field"><div class="email-field-label">失注理由</div><select class="email-input" data-f="lost_reason">
          ${S.meta.lost_reasons.map(r => `<option ${r === d.lost_reason ? 'selected' : ''}>${esc(r)}</option>`).join('')}</select></div>` : ''}
        <div class="sales-field"><div class="email-field-label">記録する内容</div>
          <textarea class="email-input" data-f="summary" rows="3">${esc(d.summary)}</textarea></div>
        <div class="sales-grid-3">
          <div class="sales-field"><div class="email-field-label">次のアクション</div><input type="text" class="email-input" data-f="next_action" value="${esc(d.next_action)}" maxlength="200"></div>
          <div class="sales-field"><div class="email-field-label">期限</div><input type="date" class="email-input" data-f="next_action_due" value="${esc(d.next_action_due)}"></div>
          <div class="sales-field"><div class="email-field-label">金額（税抜）</div><input type="number" min="0" class="email-input" data-f="amount" value="${esc(d.amount)}"></div>
        </div>
      </div>
    </div>`;
  el.querySelectorAll('[data-f]').forEach(inp => {
    const ev = inp.tagName === 'SELECT' ? 'change' : 'input';
    inp.addEventListener(ev, () => {
      const f = inp.dataset.f;
      d[f] = f === 'deal_id' ? Number(inp.value) : inp.value;
      if (f === 'stage' && d.stage === '失注' && !d.lost_reason) d.lost_reason = 'その他';
      if (['action', 'stage'].includes(f)) renderIkItem(idx);
      updateIkCount();
    });
  });
  el.querySelectorAll(`input[name="ikc-${idx}"]`).forEach(rb => rb.addEventListener('change', () => {
    d.card_id = rb.value;
    if (d.action === 'skip') d.action = ikOpenDeals(d.card_id, it).length ? 'existing' : 'new';
    renderIkItem(idx); updateIkCount();
  }));
  const search = el.querySelector('.ik-search'), results = el.querySelector('.ik-results');
  search.addEventListener('input', debounce(async () => {
    const q = search.value.trim().toLowerCase();
    if (!q) { results.innerHTML = ''; return; }
    const cards = (await fetchCardsFlat()).filter(c =>
      [c.company, c.name, c.email, c.name_kana].some(v => (v || '').toLowerCase().includes(q))).slice(0, 8);
    results.innerHTML = cards.map(c => `<div class="card-result" data-id="${esc(c.id)}">
      <span class="card-result-name">${esc(c.company)} ${esc(c.name)}</span>
      <span class="card-result-sub">${esc(c.department_position || '')} ${esc(c.email || '')}</span></div>`).join('')
      || '<div class="card-result-sub" style="padding:8px">見つかりません</div>';
    results.querySelectorAll('.card-result').forEach(r => r.onclick = () => {
      d.card_id = r.dataset.id;
      d.action = ikOpenDeals(d.card_id, it).length ? 'existing' : 'new';
      renderIkItem(idx); updateIkCount();
    });
  }, 200));
}

function ikProblems() {
  const out = [];
  for (const d of Object.values(I.dec)) {
    if (d.action === 'skip') continue;
    if (!d.card_id) out.push(`${d.idx + 1}件目: 名刺を選んでください`);
    else if (d.action === 'existing' && !d.deal_id) out.push(`${d.idx + 1}件目: 記録する案件を選んでください`);
  }
  return out;
}

function updateIkCount() {
  const n = Object.values(I.dec).filter(d => d.action !== 'skip').length;
  const probs = ikProblems();
  const btn = $('ikApply');
  if (!btn) return;
  btn.textContent = `${n}件を追加`;
  btn.disabled = !n || probs.length > 0 || I.busy;
  $('ikCount').textContent = probs[0] || `追加しない ${Object.keys(I.dec).length - n}件`;
}

async function applyIntake() {
  const decisions = Object.values(I.dec).map(d => ({
    ...d, deal_id: d.deal_id || null, stage: d.stage || null, lost_reason: d.lost_reason || null,
    occurred_at: d.occurred_at || null, next_action: d.next_action || null, next_action_due: d.next_action_due || null,
    amount: d.amount === '' || d.amount == null ? null : Number(d.amount), title: d.title || null,
  }));
  I.busy = true; updateIkCount();
  try {
    const res = await api(`/api/sales/intake/${I.job.id}/apply`, { method: 'POST', body: JSON.stringify({ decisions }) });
    await loadDeals();
    $('intakeBody').innerHTML = `<div class="ik-done">${ikResultHtml(res)}</div>`;
    $('intakeFooter').innerHTML = '<div class="email-footer-info"></div><div class="email-footer-actions"><button class="btn-sm btn-sm-primary" data-close="intakeOverlay">閉じる</button></div>';
    bindIntakeClose(); bindDealLinks();
    showToast(`新規 ${res.created.length}件・記録 ${res.recorded.length}件を追加しました`, { type: 'success' });
  } catch (e) {
    showToast(e.message, { type: 'error', title: '追加に失敗' });
  } finally {
    I.busy = false; updateIkCount();
  }
}

function ikResultHtml(r) {
  if (!r) return '';
  const link = id => `<a href="#" class="ik-deal-link" data-deal="${id}">#${id}</a>`;
  return `
    ${r.created?.length ? `<div>新規案件: ${r.created.map(link).join(' ')}</div>` : ''}
    ${r.recorded?.length ? `<div>記録した案件: ${r.recorded.map(link).join(' ')}</div>` : ''}
    ${r.skipped ? `<div>追加しなかった: ${r.skipped}件</div>` : ''}
    ${r.errors?.length ? `<div class="ik-errors">${r.errors.map(esc).join('<br>')}</div>` : ''}`;
}

function bindDealLinks() {
  $('intakeBody').querySelectorAll('.ik-deal-link').forEach(a => a.onclick = e => {
    e.preventDefault();
    $('intakeOverlay').style.display = 'none';
    S.filter = 'all'; renderFilters(); renderList();
    selectDeal(Number(a.dataset.deal));
  });
}

$('intakeBtn').onclick = () => openIntake();
