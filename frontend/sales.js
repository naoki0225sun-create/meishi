// ============================================================
// 営業管理 — 案件一覧・詳細・活動ログ・接点
// Phase 2a: メール作成（用途タブ・署名・メーラー起動）と返信待ちアラート
// 名刺管理と同じログインセッションを使う
// ============================================================

const S = {
  bulk: new Set(),     // まとめて動かす案件（sales-bulk.js）
  meta: null,
  settings: { reply_wait_days: 3, stale_days: 14 },
  deals: [],           // 表示中の種類（営業／ブループリント）の案件
  allDeals: [],
  pipeline: 'sales',
  view: 'deals',       // deals | boss | manual
  layout: 'list',      // list | board
  members: [], memberSuggestions: [], me: '', ownerFilter: '', editMember: null, extra: null,
  bpCandidates: [],
  linking: false,      // 名刺の付け替え中
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
const PIPE_KEY = 'sales.pipeline';
const P = () => S.meta.pipelines.find(p => p.key === S.pipeline) || S.meta.pipelines[0];
const pipeOf = d => S.meta.pipelines.find(p => p.key === (d.pipeline || 'sales')) || S.meta.pipelines[0];
// 一直線に進むステージの数（これより後ろは 受注・失注・保留 など）
const linearCount = p => p.stages.indexOf(p.lost) - (p.key === 'sales' ? 1 : 0);

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

function showToast(message, { type = 'info', title = '', duration = 3000, action = null } = {}) {
  const c = $('toastContainer');
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.innerHTML = `<div class="toast-body">${title ? `<div class="toast-title">${esc(title)}</div>` : ''}
    <div class="toast-message">${esc(message)}</div></div>${action ? `<button class="toast-action">${esc(action.label)}</button>` : ''}`;
  if (action) el.querySelector('.toast-action').onclick = () => { el.remove(); action.onClick(); };
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
  document.body.dataset.view = 'deals';
  renderAppNav();
  try { S.pipeline = localStorage.getItem(PIPE_KEY) || 'sales'; } catch { S.pipeline = 'sales'; }
  try { S.layout = localStorage.getItem(LAYOUT_KEY) === 'board' ? 'board' : 'list'; } catch { S.layout = 'list'; }
  S.dtab = storeGet('sales.dtab') || 'overview';
  S.sort = storeGet('sales.sort') || 'smart';
  S.vendorFilter = storeGet(VENDOR_FILTER_KEY) || '';
  document.body.classList.toggle('board-mode', S.layout === 'board');
  sheetLabel();
  await loadMembers();
  await Promise.all([loadDeals(), loadEvents(), loadPurposes(), loadSignatures()]);
  loadBpCandidates();

  const params = new URLSearchParams(location.search);
  const newCard = params.get('new');
  const openId = params.get('deal');
  const intakeId = params.get('intake');
  const view = params.get('view');
  if (view === 'boss' || view === 'manual' || view === 'calendar' || view === 'lists' || view === 'today' || view === 'deals') {
    history.replaceState(null, '', '/sales.html');
    setView(view);
  } else if (intakeId) {
    history.replaceState(null, '', '/sales.html');
    fetchCardsFlat().catch(() => {});
    openIntake(Number(intakeId));
  } else if (newCard) {
    history.replaceState(null, '', '/sales.html');
    openNewDeal(newCard);
  } else if (openId) {
    showDeal(Number(openId));
  } else {
    setView('today');   // 最初は「今日やること」
  }
}

async function loadDeals() {
  try {
    const data = await api('/api/sales/deals');
    S.allDeals = data.deals;
    if (data.settings) S.settings = data.settings;
    applyPipeline();
  } catch (e) {
    $('listPane').innerHTML = `<div class="empty-text">読み込みに失敗しました: ${esc(e.message)}</div>`;
  }
}
function applyPipeline() {
  if (!S.meta.pipelines.some(p => p.key === S.pipeline)) S.pipeline = 'sales';
  const p = P();
  S.meta.stages = p.stages;
  S.meta.closed_stages = [p.won, p.lost];
  S.deals = S.allDeals.filter(d => (d.pipeline || 'sales') === p.key && ownerMatch(d) && vendorMatch(d));
  if (!['action', 'open', 'all'].includes(S.filter) && !p.stages.includes(S.filter)) S.filter = 'open';
  renderPipelineTabs();
  renderFilters();
  renderList();
  renderAppNav();   // 「今日」の件数
}

function setPipeline(key) {
  if (key === S.pipeline) return;
  S.pipeline = key;
  try { localStorage.setItem(PIPE_KEY, key); } catch { /* 保存できなくても動く */ }
  S.filter = 'open';
  if (S.detail && (S.detail.pipeline || 'sales') !== key) { S.selectedId = null; S.detail = null; detailContainer().innerHTML = '<div class="empty-text">案件を選んでください</div>'; }
  applyPipeline();
}

function setView(v) {
  S.view = v;
  if (typeof drawBulkBar === 'function') drawBulkBar();
  const deals = v === 'deals';
  document.querySelector('.search-wrap').style.display = deals ? '' : 'none';
  $('stageFilter').style.display = deals ? '' : 'none';
  document.querySelector('.main-grid').style.display = deals ? '' : 'none';
  $('viewPane').style.display = deals ? 'none' : '';
  document.body.dataset.view = v;
  renderAppNav();
  renderPipelineTabs();
  if (v === 'today') renderToday();
  if (v === 'boss') renderBoss();
  if (v === 'manual') renderManual();
  if (v === 'calendar') renderCalendar();
  if (v === 'lists') renderLists();
  window.scrollTo(0, 0);
}

// 上の大きな切り替え（案件・部長の目・予定・マニュアル）
const NAV = [
  ['today', '今日', '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'],
  ['deals', '案件', '<rect x="3" y="7" width="18" height="13" rx="2"/><path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>'],
  ['lists', 'リスト', '<path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r="1.2"/><circle cx="4.5" cy="12" r="1.2"/><circle cx="4.5" cy="18" r="1.2"/>'],
  ['boss', '部長の目', '<path d="M3 12s3.5-6 9-6 9 6 9 6-3.5 6-9 6-9-6-9-6z"/><circle cx="12" cy="12" r="2.5"/>'],
  ['calendar', '予定', '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>'],
  ['manual', 'マニュアル', '<path d="M4 4.5A2.5 2.5 0 0 1 6.5 2H20v17H6.5A2.5 2.5 0 0 0 4 21.5z"/><path d="M4 21.5A2.5 2.5 0 0 1 6.5 19H20"/>'],
];
function renderAppNav() {
  const el = $('appNav');
  if (!el) return;
  const n = todayBadgeCount();
  el.innerHTML = NAV.map(([k, label, icon]) => `<button class="app-nav-btn ${S.view === k ? 'is-active' : ''}" data-nav="${k}" ${S.view === k ? 'aria-current="page"' : ''}>
    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">${icon}</svg><span>${label}</span>${k === 'today' && n ? `<em class="nav-n">${n > 99 ? '99+' : n}</em>` : ''}</button>`).join('');
  el.querySelectorAll('[data-nav]').forEach(b => b.onclick = () => { if (S.view !== b.dataset.nav) setView(b.dataset.nav); });
}

function renderPipelineTabs() {
  const el = $('pipelineTabs');
  // 種類のタブは「案件」と「部長の目」のときだけ。種類が1つなら出さない
  if (!['deals', 'boss'].includes(S.view) || S.meta.pipelines.length < 2) { el.style.display = 'none'; el.innerHTML = ''; return; }
  el.style.display = '';
  el.innerHTML = S.meta.pipelines.map(p => {
    const n = S.allDeals.filter(d => (d.pipeline || 'sales') === p.key && d.is_open).length;
    const a = S.allDeals.filter(d => (d.pipeline || 'sales') === p.key && d.needs_action).length;
    return `<button class="pipe-tab ${p.key === S.pipeline ? 'is-active' : ''}" data-pipe="${p.key}">${esc(p.label)}
      <span class="pipe-count">${n}</span>${a ? `<span class="pipe-alert" title="要対応">${a}</span>` : ''}</button>`;
  }).join('') + (S.pipeline === 'sales' && S.view === 'deals' && S.bpCandidates.length
    ? `<button class="pipe-hint" id="bpMoveBtn">ブループリントらしい案件が営業に ${S.bpCandidates.length}件 → 確認して移す</button>` : '');
  el.querySelectorAll('[data-pipe]').forEach(b => b.onclick = () => {
    if (b.dataset.pipe === S.pipeline) return;
    setPipeline(b.dataset.pipe);
    if (S.view !== 'deals') setView(S.view);
  });
  if ($('bpMoveBtn')) $('bpMoveBtn').onclick = openMoveToBp;
}

async function loadBpCandidates() {
  if (!S.meta.pipelines.some(p => p.key === 'bp')) return;
  try { S.bpCandidates = (await api('/api/sales/bp-candidates')).deals; } catch { S.bpCandidates = []; }
  renderPipelineTabs();
}

function openMoveToBp() {
  $('moveOverlay').style.display = 'flex';
  $('moveBody').innerHTML = `<div class="ik-help">案件名やメールの件名に「ブループリント」「Blue Print」が入っている案件です。移さないものはチェックを外してください。</div>`
    + S.bpCandidates.map(d => `
      <label class="move-row"><input type="checkbox" value="${d.id}" checked>
        <span class="move-main"><b>#${d.id} ${esc(d.title)}</b><small>${esc(d.company_snapshot || '')} ${esc(vblank(d.contact_snapshot))}</small></span>
        <span class="move-stage">${esc(d.stage)} → <b>${esc(d.new_stage)}</b></span></label>`).join('');
  $('moveApply').onclick = async () => {
    const ids = [...$('moveBody').querySelectorAll('input:checked')].map(i => Number(i.value));
    if (!ids.length) { showToast('案件を選んでください', { type: 'warning' }); return; }
    $('moveApply').disabled = true;
    try {
      await api('/api/sales/deals/move', { method: 'POST', body: JSON.stringify({ ids, pipeline: 'bp' }) });
      $('moveOverlay').style.display = 'none';
      showToast(`${ids.length}件をブループリントに移しました`, { type: 'success' });
      await loadDeals(); await loadBpCandidates();
      setPipeline('bp');
    } catch (e) { showToast(e.message, { type: 'error' }); }
    finally { $('moveApply').disabled = false; }
  };
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

  let html = '<div class="chip-scroll">';
  if (action.length || S.filter === 'action') html += chip('action', '要対応', action.length, 'chip-alert');
  html += chip('open', '進行中', open.length) + chip('all', 'すべて', S.deals.length);
  if (S.layout !== 'board') html += '<div class="filter-sep"></div>' + S.meta.stages.map(s => chip(s, s, counts[s] || 0, counts[s] ? '' : 'is-zero')).join('');
  html += '</div>';
  // 担当・経路・並び替えはひとつのボタンにまとめる
  const active = [S.ownerFilter ? 1 : 0, S.vendorFilter ? 1 : 0].reduce((a, b) => a + b, 0);
  const sortLabel = (SORTS.find(x => x[0] === S.sort) || SORTS[0])[1];
  const pop = ownerFilterHtml().replace('<div class="filter-sep"></div>', '') + vendorFilterHtml() + sortSelectHtml();
  html += `<div class="view-ctl">
    <div class="filter-pop-wrap">
      <button class="filter-btn ${active ? 'is-on' : ''}" id="filterBtn" aria-expanded="${S.filterOpen ? 'true' : 'false'}">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M3 5h18M6 12h12M10 19h4"/></svg>
        <span>${esc(sortLabel)}</span>${active ? `<b>${active}</b>` : ''}</button>
      <div class="filter-pop" id="filterPop" ${S.filterOpen ? '' : 'hidden'}>
        <div class="filter-pop-title">並び替え・絞り込み</div>
        ${pop}
        ${active ? '<button class="btn-link" id="filterClear">絞り込みを外す</button>' : ''}
      </div>
    </div>
    <div class="layout-toggle" title="b キーでも切り替えられます">
      <button class="${S.layout === 'list' ? 'is-active' : ''}" data-layout="list" aria-label="リスト">${ico('list', 13)}<span> リスト</span></button>
      <button class="${S.layout === 'board' ? 'is-active' : ''}" data-layout="board" aria-label="ボード">▦<span> ボード</span></button></div>
  </div>`;
  $('stageFilter').innerHTML = html;
  $('stageFilter').querySelectorAll('[data-layout]').forEach(b => b.onclick = () => setLayout(b.dataset.layout));
  $('sortSelect').onchange = e => { S.sort = e.target.value; storeSet('sales.sort', S.sort); renderFilters(); renderList(); };
  $('filterBtn').onclick = e => { e.stopPropagation(); S.filterOpen = !S.filterOpen; renderFilters(); };
  $('filterPop').onclick = e => e.stopPropagation();
  if ($('filterClear')) $('filterClear').onclick = () => {
    S.ownerFilter = ''; S.vendorFilter = '';
    storeSet(OWNER_FILTER_KEY, ''); storeSet(VENDOR_FILTER_KEY, '');
    applyPipelineKeepSelection();
  };
  bindOwnerFilter();
  bindVendorFilter();
  $('stageFilter').querySelectorAll('[data-filter]').forEach(b => b.onclick = () => {
    S.filter = b.dataset.filter; renderFilters(); renderList();
  });
  bindChipDrop();
  $('dealCount').textContent = `進行中 ${open.length}件 / 全 ${S.deals.length}件` + (action.length ? ` / 要対応 ${action.length}件` : '');
}
document.addEventListener('click', () => { if (S.filterOpen) { S.filterOpen = false; if ($('filterPop')) $('filterPop').hidden = true; if ($('filterBtn')) $('filterBtn').setAttribute('aria-expanded', 'false'); } });

function filteredDeals() {
  let list = S.deals;
  if (S.filter === 'action') list = list.filter(d => d.needs_action);
  else if (S.filter === 'open') list = list.filter(d => d.is_open);
  else if (S.filter !== 'all') list = list.filter(d => d.stage === S.filter);

  if (S.q) {
    const q = S.q.toLowerCase();
    list = list.filter(d => [d.title, d.company_snapshot, d.contact_snapshot, d.event_name, d.next_action,
      d.card && d.card.name, d.card && d.card.company, d.site_name, d.end_user, d.segment, d.vendor_name]
      .some(v => (v || '').toLowerCase().includes(q)));
  }
  return sortDeals(list);
}

// ── 並び替え ──
const SORTS = [
  ['smart', 'おすすめ順'],
  ['updated', '更新が新しい順'],
  ['created', '登録が新しい順'],
  ['created_old', '登録が古い順'],
  ['due', '期限が近い順'],
  ['amount', '金額が大きい順'],
  ['weighted', '見込み額順'],
  ['close', '受注予定日順'],
  ['delivery', '納期が近い順'],
  ['stalled', '止まっている順'],
  ['stage', '段階順'],
  ['company', '会社名順'],
  ['owner', '担当者順'],
];
function sortOptions() {
  const pl = P();
  return SORTS.filter(([k]) => (k !== 'close' || pl.key === 'sales') && (k !== 'delivery' || hasField('delivery_on'))
    && (k !== 'weighted' || pl.key === 'sales') && (k !== 'owner' || S.members.length));
}
const companyOf = d => (d.card && d.card.company) || d.company_snapshot || '';
function sortDeals(list) {
  const key = S.sort || 'smart';
  const by = (f, desc = false) => (a, b) => {
    const x = f(a), y = f(b);
    const xe = x == null || x === '', ye = y == null || y === '';
    if (xe || ye) return xe && ye ? b.id - a.id : xe ? 1 : -1;   // 空はいつも最後
    const c = typeof x === 'number' ? x - y : String(x).localeCompare(String(y), 'ja');
    return (desc ? -c : c) || b.id - a.id;
  };
  const open = d => d.is_open ? 0 : 1;
  const withOpen = cmp => (a, b) => open(a) - open(b) || cmp(a, b);
  switch (key) {
    case 'updated': return [...list].sort(by(d => d.updated_at, true));
    case 'created': return [...list].sort(by(d => d.created_at, true));
    case 'created_old': return [...list].sort(by(d => d.created_at));
    case 'due': return [...list].sort(withOpen(by(d => d.is_open ? d.next_action_due : null)));
    case 'amount': return [...list].sort(by(d => d.amount, true));
    case 'weighted': return [...list].sort(withOpen(by(d => d.is_open ? d.weighted || null : null, true)));
    case 'close': return [...list].sort(withOpen(by(d => d.is_open ? d.expected_close : null)));
    case 'delivery': return [...list].sort(withOpen(by(d => d.is_open ? d.delivery_on : null)));
    case 'stalled': return [...list].sort(withOpen(by(d => d.is_open ? d.days_in_stage : null, true)));
    case 'stage': {
      const order = P().stages;
      return [...list].sort(by(d => { const i = order.indexOf(d.stage); return i < 0 ? 99 : -i; }));
    }
    case 'company': return [...list].sort(by(companyOf));
    case 'owner': return [...list].sort(by(d => d.owner || null));
    default: {
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
  }
}
function sortSelectHtml() {
  const opts = sortOptions();
  if (!opts.some(([k]) => k === S.sort)) S.sort = 'smart';
  return `<label class="sort-select" title="並び替え（おすすめ順は 要対応→今日の期限→期限の近い順）">
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h13M3 12h9M3 18h5"/><path d="M18 8v12M15 17l3 3 3-3"/></svg>
    <select id="sortSelect">${opts.map(([k, l]) => `<option value="${k}" ${k === S.sort ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select></label>`;
}

// 別の種類の案件でもタブを切り替えて開く
function showDeal(id) {
  if (S.view !== 'deals') setView('deals');
  const d = S.allDeals.find(x => x.id === id);
  if (d && (d.pipeline || 'sales') !== S.pipeline) setPipeline(d.pipeline || 'sales');
  S.filter = 'all'; renderFilters(); renderList();
  selectDeal(id);
}

// ── 一覧 ──
function alertLine(d) {
  const a = d.alerts.find(x => x.level !== 'info') || d.alerts.find(x => x.level === 'info');
  if (!a) return '';
  return `<div class="row-alert level-${esc(a.level)}">${ico(a.level === 'info' ? 'mail' : 'alert', 12)} ${esc(a.message)}</div>`;
}

function bossLine(d) {
  const a = (d.advice || []).find(x => x.level === 'high' || x.level === 'mid');
  return a ? `<div class="row-boss level-${esc(a.level)}">部長: ${esc(a.title)}</div>` : '';
}

function renderList() {
  if (S.layout === 'board') { renderBoard(); return; }
  const list = vqPin(filteredDeals());
  const actionN = S.deals.filter(d => d.needs_action).length;
  const banner = vqBanner() + (actionN && S.filter !== 'action'
    ? `<button class="alert-banner" id="bannerBtn">要対応の案件が ${actionN} 件あります（電話・期限切れなど）→ 表示</button>` : '');

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
        due = `<span class="due-badge ${d.due_today ? 'is-today' : ''}">${ico('clock', 12)} ${esc(label)} ${esc(d.next_action || '')}</span>`;
      } else if (d.is_open && d.next_action && !d.next_action_due) {
        due = `<span>→ ${esc(d.next_action)}</span>`;
      }
      return `
        <div class="deal-row ${d.id === S.selectedId ? 'row-active' : ''} ${S.bulk.has(d.id) ? 'is-picked' : ''} ${vqIsNew(d) ? 'is-vq-new' : ''} ${idleClass(d)}" data-id="${d.id}" ${idleClass(d) ? `title="${idleDays(d)}日 動きがありません"` : ''}>
          <div class="deal-row-top">
            ${pickBoxHtml(d)}
            <div class="deal-company">${esc(company)}${person ? ` <span style="font-weight:400;color:var(--text-secondary)">${esc(person)}</span>` : ''}</div>
            <span class="stage-badge" data-stage="${esc(d.stage)}">${esc(d.stage)}</span>
          </div>
          <div class="deal-title"><span class="deal-no">#${d.id}</span><span class="deal-tt">${esc(d.title)}</span>${d.amount != null ? `<span class="deal-amt">${yen(d.amount)}</span>` : ''}</div>
          ${routeHtml(d)}
          ${alertLine(d) || bossLine(d)}
          <div class="deal-meta">
            ${due}
            ${d.event_name ? `<span>${ico('pin', 11)} ${esc(d.event_name)}</span>` : ''}
            ${d.stage === pipeOf(d).lost && d.lost_reason ? `<span>理由: ${esc(d.lost_reason)}</span>` : ''}
            ${d.card_missing ? '<span class="missing-badge">名刺の紐づけ切れ</span>' : ''}
            ${d.no_card ? '<span class="nocard-badge">名刺なし</span>' : ''}
            ${ownerBadge(d)}
            ${noNextBadge(d)}
            ${vendorBadge(d)}
            ${vqBadge(d)}
            ${panelBadges(d)}
          </div>
        </div>`;
    }).join('');
    $('listPane').querySelectorAll('.deal-row').forEach(el => el.onclick = () => selectDeal(Number(el.dataset.id)));
    bindPicks($('listPane')); bindRowDrag($('listPane'));
  }
  drawBulkBar();
  if ($('bannerBtn')) $('bannerBtn').onclick = () => { S.filter = 'action'; renderFilters(); renderList(); };
}

// ── 詳細 ──
async function selectDeal(id) {
  if (S.selectedId !== id) { S.compose = null; S.linking = false; }
  S.selectedId = id;
  S.showLostBox = false;
  $('listPane').querySelectorAll('.deal-row').forEach(el =>
    el.classList.toggle('row-active', Number(el.dataset.id) === id));
  try {
    [S.detail] = await Promise.all([api(`/api/sales/deals/${id}`), vqLoad(id)]);
  } catch (e) {
    showToast(e.message, { type: 'error', title: '案件を開けません' });
    return;
  }
  renderDetail();
  if (useSheet()) $('mobileSheet').classList.add('is-open');
  if (S.layout === 'board') $('listPane').querySelectorAll('.board-card').forEach(el =>
    el.classList.toggle('is-active', Number(el.dataset.id) === id));
}

function useSheet() { return isMobile() || S.layout === 'board'; }
function detailContainer() { return useSheet() ? $('mobileSheetBody') : $('detailPane'); }

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
    return `<div class="alert-card level-${esc(a.level)}"><span>${ico(a.level === 'info' ? 'mail' : 'alert')} ${esc(a.message)}</span><span class="alert-btns">${btns}</span></div>`;
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
  // ブループリントの案件では取材系の用途を先頭に
  const isBpP = p => /blue\s*print|ブループリント|取材/i.test(p.label);
  const purposes = (d.pipeline || 'sales') === 'bp'
    ? [...S.purposes.filter(isBpP), ...S.purposes.filter(p => !isBpP(p))] : S.purposes;
  const tabs = purposes.map(p =>
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
  const suggestAdvance = (d.pipeline || 'sales') === 'sales' && d.stage === '名刺交換';
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
  const person = card.name || vblank(d.contact_snapshot);
  const pl = pipeOf(d);
  const stages = pl.stages;
  const curIdx = stages.indexOf(d.stage);
  const lin = linearCount(pl);

  const stepper = stages.map((s, i) => {
    const cls = s === d.stage ? 'is-current' : (i < curIdx && i < lin && curIdx < lin ? 'is-past' : '');
    const side = pl.linear && !pl.linear.includes(s) ? 'is-side' : '';
    return `<button class="stage-step ${cls} ${side}" data-stage="${esc(s)}">${esc(s)}</button>`;
  }).join('');

  const lostBox = pl.reason_required && d.stage === pl.lost ? `
    <details class="lost-box" ${S.showLostBox || !d.lost_reason ? 'open' : ''}>
      <summary class="lost-box-title">失注の理由${d.lost_reason ? `：${esc(d.lost_reason)}` : '（任意・あとからでも書けます）'}</summary>
      <select class="email-input compact" id="lostReason">
        <option value="">— 選択 —</option>
        ${S.meta.lost_reasons.map(r => `<option ${d.lost_reason === r ? 'selected' : ''}>${esc(r)}</option>`).join('')}
      </select>
      <textarea class="email-input compact" id="lostDetail" rows="2" placeholder="詳細（任意）例: 他社見積が2割安かった">${esc(d.lost_detail || '')}</textarea>
      <div class="lost-type">
        ${Object.entries(S.meta.lost_types).map(([k, v]) => `<label><input type="radio" name="lostType" value="${k}" ${d.lost_type === k ? 'checked' : ''}> ${esc(v)}</label>`).join('')}
      </div>
      <div style="display:flex;gap:6px;justify-content:flex-end">
        <button class="btn-sm btn-sm-primary" id="lostSave">理由を残す</button>
      </div>
    </details>` : '';

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
          ${actVendorHtml(a, d)}
          ${a.kind !== 'stage' ? `<button class="tl-del" data-del-act="${a.id}" title="削除">削除</button>` : ''}
        </div>
        ${a.title ? `<div class="tl-title">${esc(a.title)}</div>` : ''}
        ${a.body ? `<details class="tl-details" ${a.kind === 'email' ? '' : 'open'}><summary>${a.kind === 'email' ? '本文を表示' : '内容'}</summary><div class="tl-body">${esc(a.body)}</div></details>` : ''}
        ${a.digest_id ? `<a class="tl-src" href="/api/sales/digests/${a.digest_id}/source" target="_blank" rel="noopener">${ico('note', 12)} 元の文字起こし・メモを見る</a>` : ''}
      </div>
    </div>`;
  }).join('') || '<div class="empty-text" style="padding:16px">記録はまだありません</div>';

  const contact = [
    card.department_position ? esc(card.department_position) : '',
    card.email ? `<a href="mailto:${esc(card.email)}">${esc(card.email)}</a>` : '',
    card.mobile ? `<a href="tel:${esc(card.mobile.replace(/[^\d+]/g, ''))}">${esc(card.mobile)}</a>` : '',
    card.phone ? `<a href="tel:${esc(card.phone.replace(/[^\d+]/g, ''))}">${esc(card.phone)}</a>` : '',
  ].filter(Boolean).join(' · ');

  const doneChecks = (() => {
    if (!pl.checks || !pl.checks.length) return null;
    const cur = pl.stages.indexOf(d.stage);
    const due = pl.checks.filter(ck => pl.stages.indexOf(ck.from) <= cur || !d.is_open);
    return { done: due.filter(ck => ((d.checks || {})[ck.key] || {}).done).length, due: due.length };
  })();
  const userActs = (d.activities || []).filter(a => a.kind !== 'stage').length;
  const itemsBlock = itemsHtml(d);
  const mailBlock = composeHtml(d);
  const dg = DG.byDeal[d.id];
  const tabs = [
    ['overview', '概要', '', VQ.byDeal[d.id] && VQ.byDeal[d.id].new ? 'has-dot' : ''],
    doneChecks ? ['checks', '確認項目', `${doneChecks.done}/${doneChecks.due}`] : null,
    ['record', '記録', userActs ? String(userActs) : ''],
    mailBlock ? ['mail', 'メール', ''] : null,
    itemsBlock ? ['items', isPanel() ? '見積明細' : '見積', (d.items || []).length ? String(d.items.length) : ''] : null,
    ['docs', '書類', ''],
  ].filter(Boolean);
  if (!tabs.some(t => t[0] === S.dtab)) S.dtab = 'overview';
  const tabHtml = tabs.map(([k, label, n, cls]) => `<button class="dtab ${k === S.dtab ? 'is-active' : ''} ${cls || ''}" data-dtab="${k}">${esc(label)}${n ? `<span class="dtab-n">${esc(n)}</span>` : ''}</button>`).join('');
  const pane = (k, html) => `<div class="dpane ${k === S.dtab ? 'is-active' : ''}" data-pane="${k}">${html}</div>`;

  detailContainer().innerHTML = `
    <div class="detail-body">
      <div class="dh">
        <div class="deal-title-row"><span class="deal-no deal-no-lg" title="Slackで「#${d.id}」と返信するとこの案件に記録できます">#${d.id}</span>
        <input class="deal-title-input" id="dealTitle" value="${esc(d.title)}" maxlength="120"></div>
        ${cardPanelHtml(d, company, person, contact)}
        ${vqHeadHtml(d)}
        ${alertsHtml(d)}
        <div class="dh-stage">
          <div class="stage-stepper">${stepper}</div>
          ${guideHtml(d)}
          ${lostBox}
          ${closedHtml(d)}
        </div>
        ${digestHtml(d)}
      </div>

      <nav class="dtabs" role="tablist">${tabHtml}</nav>

      ${pane('overview', `
        ${vendorEntryHtml(d)}
        ${vqSectionHtml(d)}
        <div class="ov-grid">
          <div class="detail-section ov-info">
            <div class="section-label"><span>案件情報</span></div>
            <div class="kv-grid">
              <div class="k">次のアクション</div>
              <div class="v"><input class="email-input compact" id="fNext" value="${esc(d.next_action || '')}" placeholder="例: 見積送付" maxlength="200"></div>
              <div class="k">期限</div>
              <div class="v"><input type="date" class="email-input compact" id="fDue" value="${esc(d.next_action_due || '')}"></div>
              <div class="k">金額（税抜）</div>
              <div class="v"><input type="number" min="0" step="1000" class="email-input compact" id="fAmount" value="${d.amount ?? ''}" placeholder="未定">
                ${pl.key === 'sales' && d.is_open ? `<div class="kv-note">確度 ${d.prob}%${d.weighted ? ` → 見込み額 ${yen(d.weighted)}` : ''}</div>` : ''}</div>
              <div class="k">自社の担当</div>
              <div class="v">${ownerSelectHtml('fOwner', d.owner || '')}</div>
            </div>
            <details class="ov-more" ${S.ovMore ? 'open' : ''}><summary>そのほかの項目（受注予定日・納期・テレアポ・接点 など）</summary>
            <div class="kv-grid">
              ${pl.key === 'sales' ? `<div class="k">受注予定日</div>
              <div class="v"><input type="date" class="email-input compact" id="fClose" value="${esc(d.expected_close || '')}">
                <div class="kv-note">お客様が発注を決める時期${d.close_changes ? `（これまで${d.close_changes}回変更）` : ''}</div></div>` : ''}
              ${vendorFieldsHtml(d)}
              ${panelFieldsHtml(d)}
              ${gradeFieldsHtml(d)}
              <div class="k">接点</div>
              <div class="v"><select class="email-input compact" id="fEvent">${eventOpts}</select></div>
              ${S.meta.pipelines.length > 1 ? `<div class="k">案件の種類</div>
              <div class="v"><select class="email-input compact" id="fPipeline">${S.meta.pipelines.map(p =>
                `<option value="${p.key}" ${p.key === pl.key ? 'selected' : ''}>${esc(p.label)}</option>`).join('')}</select></div>` : ''}
              <div class="k">登録日</div>
              <div class="v" style="font-size:13px;color:var(--text-secondary)">${esc(fmtDateTime(d.created_at))}${d.closed_at ? ` ／ 完了 ${esc(fmtDateTime(d.closed_at))}` : ''}</div>
            </div></details>
          </div>
          <div class="ov-side">
            ${bossDealHtml(d)}
            ${latestHtml(d)}
          </div>
        </div>
        <div style="display:flex;justify-content:flex-end;margin-top:8px">
          <button class="danger-link" id="deleteDeal">この案件を削除</button>
        </div>`)}

      ${doneChecks ? pane('checks', checksHtml(d)) : ''}

      ${pane('record', `
        <div class="detail-section" id="recordSection">
          <details class="rec-manual" ${S.recManual ? 'open' : ''}><summary>種類・日時・件名を指定して記録する</summary>
          <div class="act-form">
            <div class="act-kinds">${kindBtns}</div>
            <div class="sales-grid-2">
              <input type="datetime-local" class="email-input compact" id="aWhen" value="${nowLocal()}">
              <input class="email-input compact" id="aTitle" placeholder="件名（例: 初回打合せ）" maxlength="200">
            </div>
            <textarea class="email-input compact" id="aBody" placeholder="内容・議事メモ（長い文字起こしは上の「AIで読み取る」へ）"></textarea>
            <label class="compose-check" id="aAwaitWrap" style="${S.actKind === 'email' ? '' : 'display:none'}"><input type="checkbox" id="aAwait" checked> 返信待ちにする（返信がなければ電話アラート）</label>
            ${recordVisHtml(d)}
            <div class="sales-grid-2">
              <input class="email-input compact" id="aNext" placeholder="次のアクション（任意）" maxlength="200">
              <input type="date" class="email-input compact" id="aDue">
            </div>
            <div style="display:flex;justify-content:flex-end"><button class="btn-sm btn-sm-primary" id="aSave">記録する</button></div>
          </div></details>
          <div class="timeline">${timeline}</div>
        </div>`)}

      ${mailBlock ? pane('mail', mailBlock) : ''}
      ${itemsBlock ? pane('items', itemsBlock) : ''}
      ${pane('docs', '<div class="detail-section" id="dealDocs"><div class="empty-text">読み込み中…</div></div>')}
    </div>`;

  bindDetail();
  if (typeof loadDealDocs === 'function') loadDealDocs(d.id);
}

// 詳細のタブ（再描画せずに切り替える）
function showDTab(name) {
  S.dtab = name;
  storeSet('sales.dtab', name);
  const root = detailContainer();
  if (!root.querySelector(`[data-pane="${name}"]`)) return;
  root.querySelectorAll('[data-dtab]').forEach(b => b.classList.toggle('is-active', b.dataset.dtab === name));
  root.querySelectorAll('[data-pane]').forEach(p => p.classList.toggle('is-active', p.dataset.pane === name));
  const tabs = root.querySelector('.dtabs');
  const pane = root.closest('.pane, .mobile-sheet-body') || root;
  if (tabs && tabs.getBoundingClientRect().top < pane.getBoundingClientRect().top) tabs.scrollIntoView({ block: 'start' });
}

// 概要に出す「最近の記録」（3件）
function latestHtml(d) {
  const acts = (d.activities || []).filter(a => a.kind !== 'stage').slice(0, 3);
  const kinds = S.meta.activity_kinds;
  return `<div class="detail-section latest">
    <div class="section-label"><span>最近の記録</span><button class="section-action" data-goto-tab="record">すべて・追加 →</button></div>
    ${acts.length ? acts.map(a => `<div class="latest-row" data-goto-tab="record">
      <span class="latest-kind" data-kind="${esc(a.kind)}">${esc(kinds[a.kind] || a.kind)}</span>
      <span class="latest-date">${esc(fmtDate(a.occurred_at))}</span>
      <span class="latest-title">${esc(a.title || (a.body || '').slice(0, 40))}</span></div>`).join('')
      : '<div class="boss-empty">まだ記録がありません。上の「メモ」に書くと、AIが記録を整理して次のアクションを提案します</div>'}
  </div>`;
}

function bindDetail() {
  const root = detailContainer();
  const d = S.detail;
  const q = sel => root.querySelector(sel);

  // 案件名
  const titleEl = q('#dealTitle');
  titleEl.onblur = () => { if (titleEl.value.trim() && titleEl.value.trim() !== d.title) patchDeal({ title: titleEl.value.trim() }); };
  titleEl.onkeydown = e => { if (e.key === 'Enter') titleEl.blur(); };

  // タブ
  root.querySelectorAll('[data-dtab]').forEach(b => b.onclick = () => showDTab(b.dataset.dtab));
  root.querySelectorAll('[data-goto-tab]').forEach(b => b.onclick = () => showDTab(b.dataset.gotoTab));
  bindDigest(root, d);
  bindGrade(root, d);
  bindVq(root, d);

  // アラート
  root.querySelectorAll('[data-replied]').forEach(b => b.onclick = () => markReplied(b.dataset.replied));
  root.querySelectorAll('[data-log-call]').forEach(b => b.onclick = () => {
    S.actKind = 'call'; S.dtab = 'record'; S.recManual = true; renderDetail();
    const t = detailContainer().querySelector('#aTitle');
    t.value = '返信がないため電話';
    detailContainer().querySelector('#recordSection').scrollIntoView({ behavior: 'smooth', block: 'start' });
    detailContainer().querySelector('#aBody').focus();
  });
  root.querySelectorAll('[data-focus-next]').forEach(b => b.onclick = () => { showDTab('overview'); q('#fNext').focus(); q('#fNext').select(); });

  // ステージ
  root.querySelectorAll('.stage-step').forEach(b => b.onclick = () => {
    const s = b.dataset.stage;
    if (s === d.stage) return;
    moveStage(d.id, s);
  });
  if (q('#lostCancel')) q('#lostCancel').onclick = () => { S.showLostBox = false; renderDetail(); };
  if (q('#lostSave')) q('#lostSave').onclick = () => {
    const reason = q('#lostReason').value;
    const lt = (root.querySelector('input[name="lostType"]:checked') || {}).value || null;
    const data = { lost_reason: reason || null, lost_detail: q('#lostDetail').value };
    if (d.stage !== '失注') data.stage = '失注';
    if (lt) data.lost_type = lt;
    patchDeal(data, true);
  };
  bindExtras(root, d);

  // 案件情報（変更したら即保存）
  q('#fNext').onchange = e => patchDeal({ next_action: e.target.value });
  if (q('#fOwner')) q('#fOwner').onchange = e => patchDeal({ owner: e.target.value }, true);
  if (q('#fPipeline')) q('#fPipeline').onchange = async e => {
    const key = e.target.value;
    const label = S.meta.pipelines.find(p => p.key === key).label;
    if (!confirm(`この案件を「${label}」に移しますか？ステージは近いものに読み替えます`)) { e.target.value = d.pipeline || 'sales'; return; }
    try {
      await api(`/api/sales/deals/${d.id}`, { method: 'PATCH', body: JSON.stringify({ pipeline: key }) });
      await loadDeals(); loadBpCandidates();
      showDeal(d.id);
      showToast(`「${label}」に移しました`, { type: 'success', duration: 1500 });
    } catch (err) { showToast(err.message, { type: 'error' }); e.target.value = d.pipeline || 'sales'; }
  };
  q('#fDue').onchange = e => patchDeal({ next_action_due: e.target.value || null });
  if (q('#fClose')) q('#fClose').onchange = e => patchDeal({ expected_close: e.target.value || null }, true);
  q('#fAmount').onchange = e => patchDeal({ amount: e.target.value === '' ? null : Math.max(0, parseInt(e.target.value, 10) || 0) });
  q('#fEvent').onchange = e => patchDeal({ event_id: e.target.value ? Number(e.target.value) : null });

  bindPanelFields(root, d);
  bindVendorFields(root, d);
  bindCompose(root);

  // 記録
  const rm = q('.rec-manual'); if (rm) rm.ontoggle = () => { S.recManual = rm.open; };
  const om = q('.ov-more'); if (om) om.ontoggle = () => { S.ovMore = om.open; };
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
    if (q('#aVendorVis')) payload.vendor_visible = q('#aVendorVis').checked;
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
      // 次のアクションを自分で入れていなければ、AIに段階と次のアクションを確かめてもらう
      if (!payload.next_action) dgSuggest(d.id, [payload.title, payload.body].filter(Boolean).join('\n'));
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

// ── 名刺の画像（表・裏の切り替えと入れ替え） ──
const CARD_VIEW = { side: {}, ver: {} };
function cardImgSrc(card, side) {
  const v = CARD_VIEW.ver[card.id] || '';
  return `/api/cards/${encodeURIComponent(card.id)}/image${card.has_back_image ? `?side=${side}` : '?'}${v ? `&v=${v}` : ''}`;
}
function cardImageHtml(card) {
  if (!card.has_image) return '<div class="card-thumb is-empty">画像なし</div>';
  const side = card.has_back_image ? (CARD_VIEW.side[card.id] || 'front') : 'front';
  return `<div class="card-img-wrap">
    <img class="card-thumb" src="${cardImgSrc(card, side)}" alt="名刺（${side === 'front' ? '表' : '裏'}）" loading="lazy"
      data-zoom-card="${esc(card.id)}" data-side="${side}" onerror="this.closest('.card-img-wrap').classList.add('is-broken')">
    ${card.has_back_image ? `<div class="card-img-tools">
      <button class="btn-link" id="cardFlip" title="反対の面を見る">${side === 'front' ? '裏面を見る' : '表面を見る'}</button>
      <button class="btn-link" id="cardSwap" title="表と裏が逆に登録されているときに。名刺管理でも入れ替わります">表裏を入れ替え</button></div>` : ''}
  </div>`;
}

// 公式サイト（なければメールのドメインから候補・入力もできる）
function websiteHtml(card) {
  if (card.website) {
    const url = /^https?:\/\//.test(card.website) ? card.website : 'https://' + card.website;
    return `<div class="deal-sub site-line">${ico('globe')} <a href="${esc(url)}" target="_blank" rel="noopener">${esc(card.website.replace(/^https?:\/\//, '').replace(/\/$/, ''))}</a>
      <button class="btn-link" id="siteEdit">変更</button></div>`;
  }
  const cand = siteFromEmail(card.email);
  return `<div class="deal-sub site-line is-empty">${ico('globe')} 公式サイト未登録
    ${cand ? `<span class="site-cand">候補 <a href="${esc(cand)}" target="_blank" rel="noopener">${esc(cand.replace(/^https:\/\//, ''))}</a>
      <button class="btn-link" id="siteUse" data-url="${esc(cand)}">これにする</button></span>` : ''}
    <button class="btn-link" id="siteEdit">入力する</button></div>`;
}

// ── 名刺パネル（案件にひもづく名刺・名刺なし・付け替え） ──
function cardPanelHtml(d, company, person, contact) {
  const card = d.card;
  const others = (d.company_cards || []);
  const deals = (d.company_deals || []);
  const linker = `
    <div class="card-link">
      <input type="search" class="email-input compact" id="linkSearch" placeholder="つなぐ名刺を検索（会社名・氏名）" autocomplete="off">
      <div class="card-results" id="linkResults"></div>
      ${!card && others.length ? `<div class="card-link-sugg"><span>この会社の名刺:</span>${others.slice(0, 6).map(c =>
        `<button class="chip-btn" data-link-card="${esc(c.id)}">${esc(c.name || '')}${c.department_position ? ` <small>${esc(c.department_position)}</small>` : ''}</button>`).join('')}</div>` : ''}
    </div>`;
  let main;
  if (card) {
    main = `
      <div class="card-panel">
        ${cardImageHtml(card)}
        <div class="card-info">
          <div class="deal-sub"><strong>${esc(company)}</strong> ${esc(person)}</div>
          ${contact ? `<div class="deal-sub field-value">${contact}</div>` : ''}
          ${websiteHtml(card)}
          <div class="card-actions">
            <a class="btn-link" href="/app.html?card=${encodeURIComponent(card.id)}" target="_blank" rel="noopener">名刺管理で見る</a>
            <button class="btn-link" id="relinkBtn">${S.linking ? '付け替えをやめる' : '名刺を付け替える'}</button>
          </div>
        </div>
      </div>
      ${S.linking ? linker : ''}`;
  } else {
    main = `
      <div class="card-panel is-nocard">
        <div class="card-thumb is-empty">名刺なし</div>
        <div class="card-info">
          <div class="nc-grid">
            <input class="email-input compact" data-snap="company_snapshot" value="${esc(d.company_snapshot || '')}" placeholder="会社名">
            <input class="email-input compact" data-snap="contact_snapshot" value="${esc(d.contact_snapshot || '')}" placeholder="氏名">
            <input class="email-input compact" data-snap="email_snapshot" value="${esc(d.email_snapshot || '')}" placeholder="メール">
            <input class="email-input compact" data-snap="phone_snapshot" value="${esc(d.phone_snapshot || '')}" placeholder="電話">
          </div>
          <div class="card-result-sub">${d.card_missing ? '名刺管理側でこの名刺が見つかりません（削除・名前の変更など）。表示は登録時の控えです' : '名刺なしの案件です。同じ会社・氏名かメールの名刺が登録されると自動でつながります'}</div>
        </div>
      </div>
      ${linker}`;
  }
  const more = (others.length || deals.length) ? `
    <details class="company-more">
      <summary>この会社の人 ${others.length}人・ほかの案件 ${deals.length}件</summary>
      ${others.length ? `<div class="cm-people">${others.map(c => `<div class="cm-person"><b>${esc(c.name || '')}</b> <span>${esc(c.department_position || '')}</span>
        ${c.email ? `<a href="mailto:${esc(c.email)}">${esc(c.email)}</a>` : ''}</div>`).join('')}</div>` : ''}
      ${deals.length ? `<div class="cm-deals">${deals.map(x => `<button class="boss-deal" data-open-deal="${x.id}"><span class="deal-no">#${x.id}</span> ${esc(x.title)}
        <span class="stage-badge" data-stage="${esc(x.stage)}">${esc(x.stage)}</span>${x.lost_reason ? ` <small>${esc(x.lost_reason)}</small>` : ''}</button>`).join('')}</div>` : ''}
    </details>` : '';
  return main + more;
}

// ── 確認項目 ──
function checksHtml(d) {
  const pl = pipeOf(d);
  if (!pl.checks || !pl.checks.length) return '';
  const order = pl.stages;
  const cur = order.indexOf(d.stage);
  const due = [], later = [];
  pl.checks.forEach(ck => ((order.indexOf(ck.from) <= cur || !d.is_open) ? due : later).push(ck));
  const row = ck => {
    const v = (d.checks || {})[ck.key] || {};
    return `<div class="chk ${v.done ? 'is-done' : ''}">
      <label class="chk-top"><input type="checkbox" data-check="${ck.key}" ${v.done ? 'checked' : ''}>
        <span class="chk-label">${esc(ck.label)}</span></label>
      <div class="chk-ask">聞くこと: ${esc(ck.ask)}</div>
      <input class="email-input compact chk-note" data-check-note="${ck.key}" value="${esc(v.note || '')}" placeholder="わかったこと（任意）" maxlength="300">
    </div>`;
  };
  const doneN = due.filter(ck => ((d.checks || {})[ck.key] || {}).done).length;
  return `<div class="detail-section" id="checksSection">
    <div class="section-label"><span>確認項目（お客様の状態）</span><span class="chk-count ${doneN === due.length ? 'is-full' : ''}">${doneN} / ${due.length}</span></div>
    <div class="chk-help">空いている項目が次に聞くことです。チェックとメモはすぐ保存されます</div>
    ${due.map(row).join('')}
    ${later.length ? `<details class="chk-later"><summary>この先の段階で確認すること（${later.length}）</summary>${later.map(row).join('')}</details>` : ''}
  </div>`;
}

// ── 見積明細（自社用：品名・材質・板厚・数量・単価） ──
function itemsHtml(d) {
  if (!['sales', 'prod'].includes(d.pipeline || 'sales')) return '';
  const L = itemLabels();
  const items = d.items || [];
  const total = items.reduce((n, x) => n + (x.subtotal || 0), 0);
  const cell = (it, f, ph, type = 'text', cls = '') => `<input class="email-input compact ${cls}" type="${type}" ${type === 'number' ? 'min="0"' : ''}
    data-item="${it ? it.id : 'new'}" data-f="${f}" value="${it ? esc(it[f] ?? '') : ''}" placeholder="${ph}">`;
  const row = it => `<tr>
    <td>${cell(it, 'name', L.ph_name)}</td><td>${cell(it, 'material', L.ph_material)}</td><td>${cell(it, 'thickness', L.ph_thickness, 'text', isPanel() ? '' : 'narrow')}</td>
    <td>${cell(it, 'qty', L.qty, 'number', 'narrow')}</td><td>${cell(it, 'unit_price', '単価', 'number')}</td>
    <td class="num">${it && it.subtotal ? yen(it.subtotal) : ''}</td>
    <td>${it ? `<button class="tl-del" data-del-item="${it.id}" title="削除">✕</button>` : '<button class="btn-sm btn-sm-primary" id="itemAdd">追加</button>'}</td></tr>`;
  return `<div class="detail-section" id="itemsSection">
    <div class="section-label"><span>見積明細</span>${total ? `<span class="items-total">合計 ${yen(total)}</span>` : ''}</div>
    <div class="items-scroll"><table class="items-table">
      <thead><tr><th>${esc(L.name)}</th><th>${esc(L.material)}</th><th>${esc(L.thickness)}</th><th>${esc(L.qty)}</th><th>単価（円）</th><th>小計</th><th></th></tr></thead>
      <tbody>${items.map(row).join('')}${row(null)}</tbody></table></div>
    <div class="items-foot">
      ${total && total !== d.amount ? `<button class="btn-sm" id="itemsApply">合計 ${yen(total)} を案件の金額にする</button>` : ''}
      ${items.length || d.amount ? `<a class="btn-sm" href="/sales-quote.html?deal=${d.id}" target="_blank" rel="noopener">${ico('doc')} 見積書を作る（印刷・PDF）</a>` : ''}
      <span class="card-result-sub">メールの雛形に {{見積明細}} と書くと明細が入ります。リピート案件にも引き継がれます</span>
    </div>
  </div>`;
}

function itemPayload(root, key) {
  const v = f => (root.querySelector(`[data-item="${key}"][data-f="${f}"]`) || {}).value || '';
  const num = f => v(f) === '' ? null : Math.max(0, parseInt(v(f), 10) || 0);
  return { name: v('name').trim(), material: v('material').trim() || null, thickness: v('thickness').trim() || null,
    qty: num('qty'), unit_price: num('unit_price') };
}

function bindItems(root, d) {
  const add = root.querySelector('#itemAdd');
  if (!add) return;
  const doAdd = async () => {
    const body = itemPayload(root, 'new');
    if (!body.name) { showToast(`${itemLabels().name}を入力してください`, { type: 'warning' }); return; }
    try {
      await api(`/api/sales/deals/${d.id}/items`, { method: 'POST', body: JSON.stringify(body) });
      await refreshCurrent();
      detailContainer().querySelector('[data-item="new"][data-f="name"]')?.focus();
    } catch (e) { showToast(e.message, { type: 'error' }); }
  };
  add.onclick = doAdd;
  root.querySelectorAll('[data-item="new"]').forEach(inp => inp.onkeydown = e => { if (e.key === 'Enter') doAdd(); });
  root.querySelectorAll('[data-item]:not([data-item="new"])').forEach(inp => inp.onchange = async () => {
    const body = itemPayload(root, inp.dataset.item);
    if (!body.name) { showToast('品名は空にできません', { type: 'warning' }); return; }
    try { await api(`/api/sales/items/${inp.dataset.item}`, { method: 'PUT', body: JSON.stringify(body) }); await refreshCurrent(); }
    catch (e) { showToast(e.message, { type: 'error' }); }
  });
  root.querySelectorAll('[data-del-item]').forEach(b => b.onclick = async () => {
    try { await api(`/api/sales/items/${b.dataset.delItem}`, { method: 'DELETE' }); await refreshCurrent(); }
    catch (e) { showToast(e.message, { type: 'error' }); }
  });
  const ap = root.querySelector('#itemsApply');
  if (ap) ap.onclick = async () => {
    try { await api(`/api/sales/deals/${d.id}/items/apply`, { method: 'POST' }); await refreshCurrent(); showToast('金額に反映しました', { type: 'success', duration: 1500 }); }
    catch (e) { showToast(e.message, { type: 'error' }); }
  };
}

// ── 終わった案件：失注の種類・再アプローチ・感想戦 ──
function closedHtml(d) {
  if (d.is_open) return '';
  const pl = pipeOf(d);
  const lost = d.stage === pl.lost;
  return `<div class="closed-box">
    ${lost ? `<div class="closed-row"><span class="k">再アプローチ日</span>
      <input type="date" class="email-input compact" id="fReapproach" value="${esc(d.reapproach_on || '')}">
      <span class="card-result-sub">${d.lost_type === 'no_decision' ? '見送りのお客様は定期的に連絡しましょう' : '時期が来たら部長がお知らせします'}</span></div>` : ''}
    ${prodRowHtml(d)}
    ${d.stage === pl.won && pl.key === 'sales' ? `<div class="closed-row"><span class="k">次の発注見込み</span>
      <input type="date" class="email-input compact" id="fRepeat" value="${esc(d.repeat_on || '')}">
      <button class="btn-sm" id="makeRepeat">リピート案件を作る</button>
      <span class="card-result-sub">見込み時期の2週間前に部長が声をかけます</span></div>` : ''}
    <div class="closed-row col"><span class="k">感想戦（振り返り）</span>
      <textarea class="email-input compact" id="fReview" rows="3" placeholder="勝因・敗因はどの時点で決まったか／もっと早く聞くべきだったこと／別の進め方はあったか">${esc(d.review || '')}</textarea></div>
  </div>`;
}

function bindExtras(root, d) {
  bindBossDeal(root, d);
  const q = sel => root.querySelector(sel);
  // 名刺
  root.querySelectorAll('[data-zoom-card]').forEach(img => img.onclick = () => zoomCard(img.dataset.zoomCard));
  if (q('#cardFlip')) q('#cardFlip').onclick = () => {
    const id = d.card.id;
    CARD_VIEW.side[id] = (CARD_VIEW.side[id] || 'front') === 'front' ? 'back' : 'front';
    renderDetail();
  };
  if (q('#cardSwap')) q('#cardSwap').onclick = async () => {
    try {
      await api(`/api/cards/${encodeURIComponent(d.card.id)}/swap-images`, { method: 'POST' });
      CARD_VIEW.ver[d.card.id] = Date.now();
      CARD_VIEW.side[d.card.id] = 'front';
      renderDetail();
      showToast('表と裏を入れ替えました（名刺管理でも入れ替わります）', { type: 'success', duration: 2500 });
    } catch (e) { showToast(e.message, { type: 'error' }); }
  };
  const saveSite = async v => {
    try {
      await api(`/api/cards/${encodeURIComponent(d.card.id)}/field/website`, { method: 'PUT', body: JSON.stringify({ value: v }) });
      cardCache = null;
      await refreshCurrent();
      showToast('公式サイトを保存しました（名刺管理にも反映）', { type: 'success', duration: 2000 });
    } catch (e) { showToast(e.message, { type: 'error' }); }
  };
  if (q('#siteUse')) q('#siteUse').onclick = () => saveSite(q('#siteUse').dataset.url);
  if (q('#siteEdit')) q('#siteEdit').onclick = () => {
    const line = q('.site-line');
    line.innerHTML = `${ico('globe')} <input class="email-input compact site-input" id="siteInput" value="${esc(d.card.website || siteFromEmail(d.card.email) || '')}" placeholder="https://example.co.jp">
      <button class="btn-sm btn-sm-primary" id="siteSave">保存</button><button class="btn-sm" id="siteCancel">やめる</button>`;
    const inp = line.querySelector('#siteInput');
    inp.focus(); inp.select();
    line.querySelector('#siteCancel').onclick = () => renderDetail();
    line.querySelector('#siteSave').onclick = () => saveSite(inp.value.trim());
    inp.onkeydown = e => { if (e.key === 'Enter') saveSite(inp.value.trim()); if (e.key === 'Escape') renderDetail(); };
  };
  if (q('#relinkBtn')) q('#relinkBtn').onclick = () => { S.linking = !S.linking; renderDetail(); if (S.linking) setTimeout(() => detailContainer().querySelector('#linkSearch')?.focus(), 30); };
  root.querySelectorAll('[data-link-card]').forEach(b => b.onclick = () => linkCard(b.dataset.linkCard));
  root.querySelectorAll('[data-open-deal]').forEach(b => b.onclick = () => showDeal(Number(b.dataset.openDeal)));
  const ls = q('#linkSearch');
  if (ls) ls.addEventListener('input', debounce(async () => {
    const t = ls.value.trim().toLowerCase();
    const box = q('#linkResults');
    if (!t) { box.innerHTML = ''; return; }
    const cards = (await fetchCardsFlat()).filter(c =>
      [c.company, c.name, c.email, c.name_kana].some(v => (v || '').toLowerCase().includes(t))).slice(0, 10);
    box.innerHTML = cards.map(c => `<div class="card-result" data-id="${esc(c.id)}">
      <span class="card-result-name">${esc(c.company)} ${esc(c.name)}</span>
      <span class="card-result-sub">${esc(c.department_position || '')} ${esc(c.email || '')}</span></div>`).join('')
      || '<div class="card-result-sub" style="padding:8px">見つかりません。先に名刺管理で登録してください</div>';
    box.querySelectorAll('.card-result').forEach(el => el.onclick = () => linkCard(el.dataset.id));
  }, 200));
  root.querySelectorAll('[data-snap]').forEach(inp => inp.onchange = () => patchDeal({ [inp.dataset.snap]: inp.value.trim() || null }));
  // 確認項目
  root.querySelectorAll('[data-check]').forEach(cb => cb.onchange = () => {
    const k = cb.dataset.check;
    const note = (root.querySelector(`[data-check-note="${k}"]`) || {}).value || '';
    patchDeal({ checks: { [k]: { done: cb.checked, note } } }, true);
  });
  root.querySelectorAll('[data-check-note]').forEach(inp => inp.onchange = () => {
    const k = inp.dataset.checkNote;
    const done = (root.querySelector(`[data-check="${k}"]`) || {}).checked || false;
    patchDeal({ checks: { [k]: { done: done || !!inp.value.trim(), note: inp.value } } }, true);
  });
  // 終わった案件
  if (q('#fReapproach')) q('#fReapproach').onchange = e => patchDeal({ reapproach_on: e.target.value || null }, true);
  if (q('#fReview')) q('#fReview').onchange = e => patchDeal({ review: e.target.value }, true);
  if (q('#fRepeat')) q('#fRepeat').onchange = e => patchDeal({ repeat_on: e.target.value || null }, true);
  if (q('#makeRepeat')) q('#makeRepeat').onclick = async () => {
    try {
      const res = await api(`/api/sales/deals/${d.id}/repeat`, { method: 'POST' });
      await loadDeals();
      showDeal(res.id);
      showToast(`リピート案件 #${res.id} を作りました`, { type: 'success' });
    } catch (e) { showToast(e.message, { type: 'error' }); }
  };
  if (q('#goChecks')) q('#goChecks').onclick = () => showDTab('checks');
  bindItems(root, d);
}

async function linkCard(cardId) {
  try {
    await api(`/api/sales/deals/${S.detail.id}`, { method: 'PATCH', body: JSON.stringify({ card_id: cardId }) });
    S.linking = false;
    await refreshCurrent();
    showToast('名刺をつなぎました', { type: 'success', duration: 1500 });
  } catch (e) { showToast(e.message, { type: 'error' }); }
}

function zoomCard(cardId) {
  const v = CARD_VIEW.ver[cardId] ? `&v=${CARD_VIEW.ver[cardId]}` : '';
  const ov = document.createElement('div');
  ov.className = 'card-zoom';
  ov.innerHTML = `<img src="/api/cards/${encodeURIComponent(cardId)}/image?side=front${v}" alt="名刺（表）" onerror="this.remove()">
    <img src="/api/cards/${encodeURIComponent(cardId)}/image?side=back${v}" alt="名刺（裏）" onerror="this.remove()">`;
  ov.onclick = () => ov.remove();
  document.body.appendChild(ov);
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
  // 開いている1件だけ読み直す（全件を読み直さないので速い）
  const id = S.detail.id;
  const fresh = await api(`/api/sales/deals/${id}`);
  const i = S.allDeals.findIndex(x => x.id === id);
  if (i >= 0) S.allDeals[i] = fresh; else S.allDeals.unshift(fresh);
  S.detail = fresh;
  applyPipelineKeepSelection();
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
  S.noCard = false;
  if ($('newMore')) $('newMore').open = false;   // 詳しい項目は畳んでおく（必要な人だけ開く）
  $('noCardFields').style.display = 'none';
  ['ncCompany', 'ncContact', 'ncEmail', 'ncPhone', 'newClose'].forEach(id => { $(id).value = ''; });
  $('newCloseWrap').style.display = S.pipeline === 'sales' ? '' : 'none';
  $('newOwnerWrap').style.display = S.members.length ? '' : 'none';
  $('newOwnerBox').innerHTML = ownerSelectHtml('newOwner', S.me || '', { emptyLabel: '名刺の所有者にする（いなければ担当なし）' });
  $('cardSearch').value = '';
  $('cardResults').innerHTML = '';
  $('newTitle').value = '';
  $('newNext').value = S.pipeline === 'bp' ? '取材の日程調整' : S.pipeline === 'prod' ? '承認図の提出日を決める' : '御礼メール';
  $('newTitle').placeholder = '例: 制御盤筐体 10台（空欄なら接点名から自動）';
  newDealPanelInit();
  $('newDealTitle').textContent = S.meta.pipelines.length > 1 ? `新規案件（${P().label}）` : '新規案件';
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
  $('cardSearch').style.display = p || S.noCard ? 'none' : '';
  $('cardResults').style.display = p || S.noCard ? 'none' : '';
  $('noCardFields').style.display = S.noCard && !p ? '' : 'none';
  $('noCardToggle').style.display = p ? 'none' : '';
  $('noCardToggle').textContent = S.noCard ? '← 名刺から選ぶ' : '名刺がない → 会社名・氏名を入力して作る';
  if (p) {
    $('pickedCard').innerHTML = `<div><div class="card-result-name">${esc(p.company)} ${esc(p.name)}</div>
      <div class="card-result-sub">${esc(p.department_position || '')} ${esc(p.email || '')}</div></div>
      <button class="btn-sm" id="unpick">変更</button>`;
    $('unpick').onclick = () => { S.picked = null; renderPicked(); $('cardSearch').focus(); };
  }
  showLinkCands(p);
}

// 選んだ名刺の会社に、名刺のない案件（テレアポ業者が登録したものなど）があれば、新しく作る前につなぎ先として出す
async function showLinkCands(card) {
  const box = $('linkCands');
  box.style.display = 'none';
  box.innerHTML = '';
  if (!card) return;
  let res;
  try {
    res = await api(`/api/sales/cards/${encodeURIComponent(card.id)}/deal-candidates`);
  } catch { return; }
  if (!S.picked || S.picked.id !== card.id || !res.candidates.length) return;
  box.innerHTML = `<div class="link-cands-head">この会社の案件がすでにあります。新しく作らずに、名刺をつなげます</div>
    ${res.candidates.map(x => `<div class="link-cand${x.id === res.auto ? ' is-best' : ''}">
      <div><div class="card-result-name">#${x.id} ${esc(x.company || x.title || '')}${x.contact ? ` <span class="card-result-sub">${esc(x.contact)}</span>` : ''}</div>
        <div class="card-result-sub">${esc(x.stage)}${x.vendor ? '・テレアポ業者が登録' : ''}・${esc(x.reason_text)}${x.strong ? 'が一致' : ''}</div></div>
      <button type="button" class="btn-sm${x.id === res.auto ? ' btn-sm-primary' : ''}" data-link-deal="${x.id}">この案件につなぐ</button>
    </div>`).join('')}
    <div class="card-result-sub">どれにも当てはまらなければ、下の「作成」で新しい案件になります</div>`;
  box.style.display = '';
  box.querySelectorAll('[data-link-deal]').forEach(b => b.onclick = async () => {
    const id = Number(b.dataset.linkDeal);
    b.disabled = true;
    try {
      await api(`/api/sales/deals/${id}`, { method: 'PATCH', body: JSON.stringify({ card_id: card.id }) });
      $('newDealOverlay').style.display = 'none';
      S.filter = 'open';
      await loadDeals();
      selectDeal(id);
      showToast(`#${id} に名刺をつなぎました`, { type: 'success', duration: 1800 });
    } catch (e) {
      b.disabled = false;
      showToast(e.message, { type: 'error', title: 'つなげませんでした' });
    }
  });
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
  if (!S.picked && !S.noCard) { showToast('名刺を選ぶか「名刺がない」から会社名を入力してください', { type: 'warning' }); return; }
  if (!S.picked && !$('ncCompany').value.trim() && !$('ncContact').value.trim()) {
    showToast('会社名か氏名を入力してください', { type: 'warning' }); return;
  }
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
      card_id: S.picked ? S.picked.id : null,
      company: S.picked ? null : $('ncCompany').value.trim() || null,
      contact: S.picked ? null : $('ncContact').value.trim() || null,
      email: S.picked ? null : $('ncEmail').value.trim() || null,
      phone: S.picked ? null : $('ncPhone').value.trim() || null,
      expected_close: S.pipeline === 'sales' ? ($('newClose').value || null) : null,
      owner: $('newOwner') && $('newOwner').value ? $('newOwner').value : null,
      pipeline: S.pipeline,
      title: $('newTitle').value.trim() || null,
      stage: $('newStage').value,
      event_id: eventId ? Number(eventId) : null,
      next_action: $('newNext').value.trim() || null,
      next_action_due: $('newDue').value || null,
      ...newDealPanelPayload(),
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
    const p = P();
    const isSales = p.key === 'sales';
    const s = await api(`/api/sales/stats?pipeline=${p.key}`);
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
        ${isSales ? `<div class="stat-tile"><div class="stat-tile-label">受注率（受注÷完了）</div><div class="stat-tile-value">${s.win_rate == null ? '—' : Math.round(s.win_rate * 100) + '%'}</div></div>
        <div class="stat-tile"><div class="stat-tile-label">受注金額</div><div class="stat-tile-value" style="font-size:18px">${yen(s.won_amount)}</div></div>`
        : `<div class="stat-tile"><div class="stat-tile-label">${esc(p.won)}</div><div class="stat-tile-value">${s.won}</div></div>
        <div class="stat-tile"><div class="stat-tile-label">${esc(p.lost)}</div><div class="stat-tile-value">${s.lost}</div></div>`}
      </div>
      <div class="section-label"><span>${esc(p.label)}：ステージ別</span></div>${bars(s.by_stage, maxStage)}
      ${isSales ? `<div class="section-label" style="margin-top:18px"><span>失注理由（${s.lost}件）</span></div>
      ${s.lost ? bars(s.lost_reasons, maxLost, 'is-lost') : '<div class="card-result-sub">失注はまだありません</div>'}
      ${eventRows ? `<div class="section-label" style="margin-top:18px"><span>接点別</span></div>${eventRows}` : ''}` : ''}`;
  } catch (e) {
    $('statsBody').innerHTML = `<div class="empty-text">${esc(e.message)}</div>`;
  }
}

// ── 設定 ──
async function openSettings() {
  S.editSig = null; S.editPurpose = null;
  $('settingsOverlay').style.display = 'flex';
  try {
    VEN.shown = null;
    const [st, ex] = await Promise.all([api('/api/sales/settings'), api('/api/sales/settings/extra'), loadSignatures(), loadPurposes(), loadMembers(), loadVendors()]);
    S.settings = st;
    S.extra = ex;
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
      <label>営業部長の指摘の強さ <select class="email-input compact" id="sStrict" style="width:auto">${Object.entries(S.meta.strictness).map(([k, v]) =>
        `<option value="${k}" ${(st.strictness || 'gentle') === k ? 'selected' : ''}>${esc(v)}</option>`).join('')}</select></label>
      <div class="card-result-sub">ゆるめ：指摘は1案件3つまで・「至急」は出さない・止まっている指摘は60日から</div>
      <label>同じ段階に <input type="number" min="7" max="180" class="email-input compact num" id="sStall" value="${st.stall_days ?? 30}"> 日以上いたら部長が「止まっています」と指摘（ゆるめでは60日未満は出しません）</label>
      <label>月の受注目標 <input type="number" min="0" step="10000" class="email-input compact" id="sTarget" style="width:140px" value="${st.monthly_target ?? ''}" placeholder="未設定"> 円（部長の目で「パイプラインは目標の何倍か」を判定）</label>
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
    ${purposeList}
    ${teamSettingsHtml()}
    ${vendorSettingsHtml()}`;

  const B = $('settingsBody');
  bindTeamSettings(renderSettings);
  bindVendorSettings(renderSettings);
  $('saveAlertSettings').onclick = async () => {
    try {
      await api('/api/sales/settings', { method: 'PUT', body: JSON.stringify({
        reply_wait_days: parseInt($('sReply').value, 10), stale_days: parseInt($('sStale').value, 10),
        stall_days: parseInt($('sStall').value, 10) || 30,
        strictness: $('sStrict').value,
        monthly_target: $('sTarget').value === '' ? null : Math.max(0, parseInt($('sTarget').value, 10) || 0) }) });
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
$('noCardToggle').onclick = () => { S.noCard = !S.noCard; renderPicked(); if (S.noCard) $('ncCompany').focus(); else $('cardSearch').focus(); };
$('newEvent').onchange = e => { $('newEventFields').style.display = e.target.value === '__new' ? 'grid' : 'none'; };
$('mobileBackBtn').onclick = () => $('mobileSheet').classList.remove('is-open');
document.querySelectorAll('[data-close]').forEach(b => b.onclick = () => { $(b.dataset.close).style.display = 'none'; });
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  ['newDealOverlay', 'statsOverlay', 'settingsOverlay', 'listsOverlay'].forEach(id => { if ($(id)) $(id).style.display = 'none'; });
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
    $('ikFileLabel').innerHTML = f ? `${ico('doc')} ${esc(f.name)}（${Math.ceil(f.size / 1024)}KB）<br><small>クリックで変更</small>`
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
    showIntakeJob(await waitIntake(data.id, sec => {
      if ($('ikStatus')) $('ikStatus').textContent = `AIが読み取っています（${sec}秒）。表や長い資料は1〜2分かかります`;
    }));
  } catch (e) {
    showToast(e.message, { type: 'error', title: '読み取りに失敗' });
    btn.disabled = false; btn.textContent = '読み取る';
    $('ikStatus').textContent = '読み取りにはAIを使います（数十秒かかることがあります）';
  }
}

// 裏で動いている読み取りが終わるまで待つ
async function waitIntake(id, onTick) {
  const start = Date.now();
  for (;;) {
    const job = await api(`/api/sales/intake/${id}`);
    if (job.status === 'error') throw new Error(`AIでの読み取りに失敗しました: ${job.error || '不明なエラー'}`);
    if (job.status !== 'analyzing') return job;
    const sec = Math.round((Date.now() - start) / 1000);
    if (sec > 360) throw new Error('読み取りに時間がかかりすぎています。資料を分けてお試しください');
    onTick && onTick(sec);
    await new Promise(r => setTimeout(r, 2000));
  }
}

async function loadIntakeJob(id) {
  $('intakeBody').innerHTML = '<div class="empty-text" id="ikWait">読み込み中…</div>';
  $('intakeFooter').innerHTML = '';
  try { showIntakeJob(await waitIntake(id, sec => { if ($('ikWait')) $('ikWait').textContent = `AIが読み取っています（${sec}秒）…`; })); }
  catch (e) { $('intakeBody').innerHTML = `<div class="empty-text">${esc(e.message)}</div>`; }
}

function ikOpenDeals(cardId, item) {
  const fromList = S.allDeals.filter(d => d.card_id === cardId && d.is_open).map(d => ({ id: d.id, title: d.title, stage: d.stage, pipeline: d.pipeline }));
  return fromList.length ? fromList : ((item.open_deals || {})[cardId] || []);
}

function showIntakeJob(job) {
  I.job = job; I.dec = {};
  $('intakeTitle').textContent = job.filename ? `取り込み：${job.filename}` : '取り込み：貼り付けた文章';
  for (const it of job.items) {
    I.dec[it.idx] = {
      idx: it.idx, action: it.action, card_id: it.card_id, deal_id: it.deal_id, pipeline: it.pipeline || 'sales',
      canNoCard: !!(it.company || it.person),
      title: it.title || ikDefaultTitle(it), stage: it.stage || '', lost_reason: it.lost_reason || '', kind: it.kind || 'memo',
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
    ${job.items.length ? '' : '<div class="empty-text">案件になりそうな内容は見つかりませんでした</div>'}
    ${job.items.some(i => i.status === 'not_found' && (i.company || i.person)) ? `<div class="ik-bulk">
      名刺が見つからないもの ${job.items.filter(i => i.status === 'not_found' && (i.company || i.person)).length}件
      <button class="btn-sm" id="ikAllNoCard">まとめて「名刺なしで新規案件」にする</button></div>` : ''}`;
  $('intakeBody').innerHTML = head + job.items.map(it => `<div class="ik-item" id="ik-${it.idx}"></div>`).join('');
  job.items.forEach(it => renderIkItem(it.idx));
  if ($('ikAllNoCard')) $('ikAllNoCard').onclick = () => {
    job.items.filter(i => i.status === 'not_found' && (i.company || i.person)).forEach(i => {
      if (!I.dec[i.idx].card_id) { I.dec[i.idx].action = 'nocard'; renderIkItem(i.idx); }
    });
    updateIkCount();
  };
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

function ikDefaultTitle(it) {
  // 案件名が読み取れないとき（訪問リストなど）は記録内容の頭から作る
  const t = (it.summary || '').split('\n')[0].replace(/^ランク\s*[A-Z]\s*/, '').trim();
  return t.length > 30 ? t.slice(0, 30) + '…' : t;
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
  const isNew = d.action === 'new' || d.action === 'nocard';
  const exDeal = d.action === 'existing' ? deals.find(x => x.id === d.deal_id) : null;
  const pl = S.meta.pipelines.find(p => p.key === (exDeal ? (exDeal.pipeline || 'sales') : d.pipeline)) || S.meta.pipelines[0];
  if (d.stage && !pl.stages.includes(d.stage)) d.stage = '';
  const stageOpts = `<option value="">${isNew ? `${pl.first}（指定なし）` : '変更しない'}</option>`
    + pl.stages.map(s => `<option ${s === d.stage ? 'selected' : ''}>${esc(s)}</option>`).join('');
  const pipeSel = isNew && S.meta.pipelines.length > 1 ? `<div class="sales-field"><div class="email-field-label">案件の種類</div>
    <select class="email-input" data-f="pipeline">${S.meta.pipelines.map(p =>
      `<option value="${p.key}" ${p.key === d.pipeline ? 'selected' : ''}>${esc(p.label)}</option>`).join('')}</select></div>` : '';
  el.className = `ik-item ik-${cls} ${d.action === 'skip' ? (d.card_id ? 'ik-skip' : 'ik-skip ik-pick') : ''} ${d.action === 'nocard' ? 'ik-nocard' : ''}`;
  el.innerHTML = `
    <div class="ik-head">
      <span class="ik-badge ${cls}">${label}</span>
      ${S.meta.pipelines.length > 1 && d.pipeline !== 'sales' ? `<span class="ik-pipe">${esc((S.meta.pipelines.find(p => p.key === d.pipeline) || {}).label || '')}</span>` : ''}
      <span class="ik-who"><b>${esc(it.company || '（会社不明）')}</b> ${esc(it.person || '')}
        ${it.department ? `<small>${esc(it.department)}</small>` : ''}</span>
      <select class="email-input ik-action" data-f="action">
        <option value="new" ${d.action === 'new' ? 'selected' : ''}>新規案件にする</option>
        <option value="existing" ${d.action === 'existing' ? 'selected' : ''} ${deals.length ? '' : 'disabled'}>既存の案件に記録${deals.length ? '' : '（なし）'}</option>
        ${d.canNoCard ? `<option value="nocard" ${d.action === 'nocard' ? 'selected' : ''}>名刺なしで新規案件</option>` : ''}
        <option value="skip" ${d.action === 'skip' ? 'selected' : ''}>追加しない</option>
      </select>
    </div>
    ${it.evidence ? `<div class="ik-evidence">「${esc(it.evidence)}」</div>` : ''}
    <div class="ik-body">
      <div class="ik-cards">
        <div class="email-field-label">名刺</div>
        <div class="ik-nocard-note">名刺なしで「${esc([it.company, it.person].filter(Boolean).join(' '))}」の案件を作ります。あとで同じ会社・氏名の名刺を登録すると自動でつながります</div>
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
        ${pipeSel}
        ${d.action === 'existing' ? `<div class="sales-field"><div class="email-field-label">記録する案件</div>
          <select class="email-input" data-f="deal_id">${deals.map(x => `<option value="${x.id}" ${x.id === d.deal_id ? 'selected' : ''}>#${x.id} ${esc(x.title)}（${esc(x.stage)}）</option>`).join('')}</select></div>`
        : `<div class="sales-field"><div class="email-field-label">案件名</div>
          <input type="text" class="email-input" data-f="title" value="${esc(d.title)}" maxlength="120" placeholder="例: 制御盤筐体 10台"></div>`}
        <div class="sales-grid-3">
          <div class="sales-field"><div class="email-field-label">ステージ</div><select class="email-input" data-f="stage">${stageOpts}</select></div>
          <div class="sales-field"><div class="email-field-label">記録の種類</div><select class="email-input" data-f="kind">
            ${Object.entries(IK_KIND).map(([k, v]) => `<option value="${k}" ${k === d.kind ? 'selected' : ''}>${v}</option>`).join('')}</select></div>
          <div class="sales-field"><div class="email-field-label">日付</div><input type="date" class="email-input" data-f="occurred_at" value="${esc(d.occurred_at)}"></div>
        </div>
        ${d.stage === pl.lost && pl.reason_required ? `<div class="sales-field"><div class="email-field-label">失注理由</div><select class="email-input" data-f="lost_reason">
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
      if (['action', 'stage', 'pipeline', 'deal_id'].includes(f)) renderIkItem(idx);
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
    if (d.action === 'skip' || d.action === 'nocard') continue;
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
  const decisions = Object.values(I.dec).map(({ canNoCard, ...d }) => ({
    owner: S.me || null,
    ...d, card_id: d.action === 'nocard' ? null : d.card_id, deal_id: d.deal_id || null, stage: d.stage || null, lost_reason: d.lost_reason || null,
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
    showDeal(Number(a.dataset.deal));
  });
}

$('intakeBtn').onclick = () => openIntake();
