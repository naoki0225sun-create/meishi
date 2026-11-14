// ============================================================
// 図面管理 — 共通・取り込み・確認待ち（AIの読み取り結果を1クリックで確定）
// 名刺管理・営業と同じログインと画面の作りを使う
// ============================================================

const Z = {
  meta: null, view: 'inbox', me: '',
  inbox: [], processing: 0, failed: [], settings: {}, selId: null, doc: null, edit: null,
  saveTimer: null, poll: null,
};
const $ = id => document.getElementById(id);
const ME_KEY = 'sales.me';   // 「自分」は営業と同じ設定を使う
const KIND_ICON = { 図面: 'drawing', 注文書: 'order', 注文変更書: 'order', 見積書: 'doc', 請求書: 'yen', 納品書: 'box', 発注書: 'order', 検査成績書: 'check', 写真: 'camera', その他: 'doc' };
const DR_STATUS = {
  new: ['新しい図番', 'zst-new'], first_rev: ['初めて登録する版', 'zst-new'], newer: ['登録済みの最新版より新しい版', 'zst-good'],
  same_rev: ['同じ版が登録済み', 'zst-warn'], older: ['登録済みの最新版より古い版', 'zst-info'], unknown: ['新旧を判断できません', 'zst-warn'], same_file: ['同じファイルが登録済み', 'zst-info'],
};

// ── 共通 ──
async function api(path, options = {}) {
  const isForm = options.body instanceof FormData;
  const res = await fetch(path, { credentials: 'include', headers: isForm ? {} : { 'Content-Type': 'application/json' }, ...options });
  if (res.status === 401) { location.href = '/'; throw new Error('Unauthorized'); }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    const msg = Array.isArray(err.detail) ? '入力内容に誤りがあります。数字や日付を確認してください'
      : res.status === 413 ? 'ファイルが大きすぎるため、アップロードできませんでした（上限は200MBです）'
      : res.status >= 500 ? 'サーバーでエラーが発生しました。しばらくしてからもう一度お試しください' : err.detail;
    throw new Error(msg || `HTTP ${res.status}`);
  }
  return res.json();
}
const post = (p, body) => api(p, { method: 'POST', body: JSON.stringify(body || {}) });
const put = (p, body) => api(p, { method: 'PUT', body: JSON.stringify(body || {}) });
function esc(s) {
  if (s == null) return '';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
function pad(n) { return String(n).padStart(2, '0'); }
function todayStr() { const d = new Date(); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; }
function fmtDate(s) { if (!s) return ''; const [y, m, d] = s.slice(0, 10).split('-'); return `${y}/${Number(m)}/${Number(d)}`; }
function fmtMD(s) { if (!s) return ''; const [, m, d] = s.slice(0, 10).split('-'); return `${Number(m)}/${Number(d)}`; }
function yen(n) { return n == null || n === '' ? '' : '¥' + Number(n).toLocaleString('ja-JP'); }
function man(n) { if (n == null) return '—'; const a = Math.abs(n); return (n < 0 ? '−' : '') + (a >= 1e8 ? (a / 1e8).toFixed(2) + '億円' : a >= 1e4 ? Math.round(a / 1e4).toLocaleString('ja-JP') + '万円' : a.toLocaleString('ja-JP') + '円'); }
function pct(v) { return v == null ? '—' : Math.round(v * 100) + '%'; }
function storeGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function storeSet(k, v) { try { localStorage.setItem(k, v); } catch { /* 使えない環境では無視 */ } }
function bytes(n) { if (!n) return '0'; const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0; while (n >= 1024 && i < 4) { n /= 1024; i++; } return n.toFixed(i ? 1 : 0) + u[i]; }

function showToast(message, { type = 'info', title = '', duration = 3200, action = null, action2 = null } = {}) {
  const c = $('toastContainer');
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.innerHTML = `<div class="toast-body">${title ? `<div class="toast-title">${esc(title)}</div>` : ''}<div class="toast-message">${esc(message)}</div></div>
    ${action ? `<button class="toast-action" data-a="1">${esc(action.label)}</button>` : ''}${action2 ? `<button class="toast-action" data-a="2">${esc(action2.label)}</button>` : ''}`;
  if (action) el.querySelector('[data-a="1"]').onclick = () => { el.remove(); action.onClick(); };
  if (action2) el.querySelector('[data-a="2"]').onclick = () => { el.remove(); action2.onClick(); };
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

function kindBadge(kind) {
  return `<span class="zk" data-k="${esc(kind || 'その他')}">${ico(KIND_ICON[kind] || 'doc', 12)}${esc(kind || '未判定')}</span>`;
}
function thumb(fileId, page = 1, cls = '') {
  return `<img class="zm-thumb ${cls}" loading="lazy" src="${ZV.pageUrl(fileId, page, 'thumb')}" alt="" onerror="this.classList.add('is-none')">`;
}

// ── 初期化 ──
const NAV = [
  ['inbox', '確認待ち', 'inbox'], ['companies', '会社', 'folder'], ['drawings', '図面', 'drawing'], ['schedule', '工程表', 'cal'],
  ['site', '現場', 'factory'], ['numbers', '業績', 'chart'], ['settings', '設定', 'gear'],
];

async function init() {
  try {
    const me = await api('/api/me');
    $('tenantName').textContent = me.tenant.display_name;
  } catch { return; }
  Z.meta = await api('/api/zumen/meta');
  Z.me = storeGet(ME_KEY) || '';
  if (Z.me && !Z.meta.members.includes(Z.me)) Z.me = '';
  renderMe();
  bindUpload();
  const p = new URLSearchParams(location.search);
  const v = p.get('view');
  if (p.get('doc')) { setView('doc', { id: Number(p.get('doc')) }); }
  else if (p.get('drawing')) { setView('drawing', { id: Number(p.get('drawing')), rev: p.get('rev') ? Number(p.get('rev')) : null }); }
  else if (p.get('company')) { setView('companies', { id: Number(p.get('company')) }); }
  else if (v === 'site' && p.get('deal')) setView('site', { deal: Number(p.get('deal')) });
  else if (v === 'schedule' && p.get('job')) setView('schedule', { job: Number(p.get('job')) });
  else setView(NAV.some(n => n[0] === v) ? v : (storeGet('zumen.view') || 'inbox'));
  refreshCount();
  setInterval(refreshCount, 60000);
}

function renderMe() {
  const el = $('meSelect');
  if (!Z.meta.members.length) { el.style.display = 'none'; return; }
  el.style.display = '';
  el.innerHTML = `<option value="">担当者を選択</option>` + Z.meta.members.map(n => `<option value="${esc(n)}" ${n === Z.me ? 'selected' : ''}>${esc(n)}</option>`).join('');
  el.onchange = () => { Z.me = el.value; storeSet(ME_KEY, Z.me); showToast(Z.me ? `これからの操作は「${Z.me}」さんとして記録します` : '担当者の選択を解除しました', { duration: 1800 }); };
}

function renderNav() {
  const n = Z.meta.review_count;
  $('appNav').innerHTML = NAV.map(([k, label, icon]) => `<button class="app-nav-btn ${Z.view === k ? 'is-active' : ''}" data-nav="${k}">
    ${ico(icon, 17)}<span>${label}</span>${k === 'inbox' && n ? `<em class="nav-n">${n > 99 ? '99+' : n}</em>` : ''}</button>`).join('');
  $('appNav').querySelectorAll('[data-nav]').forEach(b => b.onclick = () => setView(b.dataset.nav));
}

async function refreshCount() {
  try {
    const m = await api('/api/zumen/meta');
    Z.meta.review_count = m.review_count;
    renderNav();
    if (Z.view !== 'inbox') $('zmCount').textContent = m.review_count ? `確認待ち ${m.review_count}件` : '確認待ちはありません';
  } catch { /* 無視 */ }
}

function setView(v, arg = {}) {
  Z.view = v;
  if (NAV.some(n => n[0] === v)) storeSet('zumen.view', v);
  document.body.dataset.view = v;
  document.body.classList.toggle('site-mode', v === 'site');
  renderNav();
  if (v !== 'inbox') $('zmCount').textContent = Z.meta.review_count ? `確認待ち ${Z.meta.review_count}件` : '確認待ちはありません';
  const split = v === 'inbox';
  $('inboxGrid').hidden = !split;
  if (!split) document.body.classList.remove('zm-detail-open');
  $('inboxGrid').style.display = split ? '' : 'none';
  $('viewPane').style.display = split ? 'none' : '';
  if (Z.poll) { clearInterval(Z.poll); Z.poll = null; }
  if (v !== 'doc' && v !== 'drawing' && v !== 'companies') history.replaceState(null, '', '/zumen.html' + (v === 'inbox' ? '' : `?view=${v}`));
  if (v === 'inbox') renderInbox();
  else ZVIEWS[v](arg);
  window.scrollTo(0, 0);
}

// ── 取り込み（ボタン・画面全体へのドロップ） ──
function bindUpload() {
  $('uploadBtn').onclick = () => $('fileInput').click();
  $('fileInput').onchange = e => { uploadFiles([...e.target.files]); e.target.value = ''; };
  let depth = 0;
  const ov = $('dropOverlay');
  const hasFiles = e => [...(e.dataTransfer?.types || [])].includes('Files');
  window.addEventListener('dragenter', e => { if (!hasFiles(e)) return; depth++; ov.classList.add('is-on'); });
  window.addEventListener('dragleave', e => { if (!hasFiles(e)) return; depth = Math.max(0, depth - 1); if (!depth) ov.classList.remove('is-on'); });
  window.addEventListener('dragover', e => { if (hasFiles(e)) e.preventDefault(); });
  window.addEventListener('drop', e => {
    if (!hasFiles(e)) return;
    e.preventDefault(); depth = 0; ov.classList.remove('is-on');
    if (e.target.closest && e.target.closest('[data-drop-deal]')) return;
    uploadFiles([...e.dataTransfer.files]);
  });
}

async function uploadFiles(files, extra = {}) {
  if (!files.length) return;
  const fd = new FormData();
  files.forEach(f => fd.append('files', f, f.name));
  if (Z.me) fd.append('actor', Z.me);
  if (Z.noAi) fd.append('no_ai', '1');
  Object.entries(extra).forEach(([k, v]) => fd.append(k, v));
  showToast(`${files.length}件をアップロードしています…`, { duration: 1500 });
  try {
    const r = await api('/api/zumen/upload', { method: 'POST', body: fd });
    const msg = [`${r.queued.length}件を受け付けました。AIの読み取りが終わったものから「確認待ち」に表示されます`];
    if (r.duplicates.length) msg.push(`${r.duplicates.length}件は登録済みのファイルと同じため、取り込みませんでした`);
    if (r.errors.length) msg.push(r.errors.map(x => `${x.filename}：${x.error}`).join(' / '));
    showToast(msg.join('。'), { type: r.errors.length ? 'warning' : 'success', duration: 5000,
      action: r.duplicates.length && r.duplicates[0].docs[0] ? { label: '登録済みの書類を開く', onClick: () => openDoc(r.duplicates[0].docs[0].id) } : null });
    if (Z.view === 'inbox') loadInbox(); else if (!extra.deal_id) setView('inbox');
  } catch (e) { showToast(e.message, { type: 'error' }); }
}

function openDoc(id) {
  const it = Z.inbox.find(x => x.id === id);
  if (it) { if (Z.view !== 'inbox') setView('inbox'); selectDoc(id); } else setView('doc', { id });
}

// ════════════════════════════════════════
// 確認待ち
// ════════════════════════════════════════
async function renderInbox() {
  $('listPane').innerHTML = '<div class="skel-list"><div class="skel-row"></div><div class="skel-row"></div></div>';
  await loadInbox();
  // 読み取り中だけ見に行く。確認待ちが多いとき（過去分の取り込み中）は間をあける
  let last = 0;
  Z.poll = setInterval(() => {
    const gap = Z.inbox.length > 500 ? 30000 : 5000;
    if (Z.processing && Date.now() - last >= gap) { last = Date.now(); loadInbox(true); }
  }, 5000);
}

async function loadInbox(quiet = false) {
  let r;
  try { r = await api('/api/zumen/inbox'); } catch (e) { if (!quiet) $('listPane').innerHTML = `<div class="empty-text">${esc(e.message)}</div>`; return; }
  const before = Z.inbox.map(x => x.id).join(',');
  Z.inbox = r.items; Z.processing = r.processing; Z.failed = r.failed; Z.settings = r.settings;
  Z.meta.review_count = r.items.length; renderNav();
  $('zmCount').textContent = r.items.length || r.processing ? `確認待ち ${r.items.length}件${r.processing ? `・読み取り中 ${r.processing}件` : ''}` : '確認待ちはありません';
  if (quiet && before === Z.inbox.map(x => x.id).join(',')) { drawInboxHead(r); return; }
  drawInboxList(r);
  if (Z.selId && !Z.inbox.some(x => x.id === Z.selId)) Z.selId = null;
  if (!Z.selId && Z.inbox.length && window.innerWidth > 860) {
    const f = INBOX_FILTERS.find(x => x[0] === Z.inboxFilter) || INBOX_FILTERS[0];
    const first = Z.inbox.find(f[2]) || Z.inbox[0];
    selectDoc(first.id);
  }
  if (!Z.inbox.length) { Z.selId = null; Z.doc = null; $('detailPane').innerHTML = emptyInboxHtml(r); }
}

function emptyInboxHtml(r) {
  return `<div class="detail-empty"><div class="detail-empty-icon">${ico('check', 40)}</div>
    <p class="detail-empty-text">${r.processing ? `AIが ${r.processing}件を読み取っています。終わったものから左の一覧に表示されます` : '確認待ちの書類はありません。右上の「書類を追加」を押すか、画面にファイルをドラッグしてください'}</p>
    ${r.ai_available ? '' : '<p class="zm-warn-line">AIの設定（APIキー）が済んでいないため、内容は手入力してください</p>'}</div>`;
}

function drawInboxHead(r) {
  const h = $('inboxHead');
  if (!h) return;
  h.innerHTML = `${r.processing ? `<div class="zm-reading"><span class="zm-spin"></span>AIが読み取り中 ${r.processing}件</div>` : ''}
    ${r.failed.length ? `<div class="zm-failed">${ico('alert', 13)} 読み取れなかったファイル ${r.failed.length}件 <button class="btn-link" id="retryBtn">もう一度読み取る</button></div>` : ''}
    ${Z.settings.bulk_confirm && r.items.length ? '<button class="btn-sm" id="bulkBtn">AIの読み取りが確かな書類をまとめて確定</button>' : ''}
    ${r.items.some(x => x.source === 'import') ? `<button class="btn-sm" id="importBtn">${ico('layers', 13)} 過去の書類をまとめて確定</button>` : ''}`;
  if ($('retryBtn')) $('retryBtn').onclick = async () => { await post('/api/zumen/admin/retry'); showToast('もう一度読み取りを始めました'); loadInbox(); };
  if ($('bulkBtn')) $('bulkBtn').onclick = bulkConfirm;
  if ($('importBtn')) $('importBtn').onclick = importConfirm;
}

async function importConfirm() {
  const pv = await api('/api/zumen/inbox/import-preview');
  const box = $('zmModalBox');
  $('zmModal').style.display = 'flex';
  box.className = 'zm-modal-box zm-share';
  box.innerHTML = `<div class="zm-modal-head"><b>${ico('layers', 15)} 過去の書類をまとめて確定</b><button class="dupe-modal-close" id="zmModalClose">×</button></div>
    <div class="zsh-body">
      <div class="zsh-help">会社・図番・版だけを確定し、図面と単価の履歴に登録します。案件への紐づけや納期の予定は作りません。会社を判別できない書類（フォルダ名などの手がかりがないもの）は残しますので、1件ずつ確認してください。</div>
      <div class="zim-sum"><div><b>${pv.ready.length.toLocaleString()}</b><span>まとめて確定できる</span></div><div><b>${pv.hold.toLocaleString()}</b><span>1件ずつ確認が必要</span></div>
        <div><b>${pv.reading.toLocaleString()}</b><span>まだ読み取り中</span></div></div>
      ${pv.folders.length ? `<div class="zr-label">フォルダ別の件数</div><div class="zim-folders">${pv.folders.map(f => `<span>${esc(f.folder)}<b>${f.n}</b></span>`).join('')}</div>` : ''}
      <div class="zr-actions" style="position:static;background:none">
        <button class="btn-sm btn-sm-primary" id="imOk" ${pv.ready.length ? '' : 'disabled'}>${pv.ready.length.toLocaleString()}件を確定する</button>
        <button class="btn-sm" id="imCancel">キャンセル</button></div>
      <div id="imRun"></div>
    </div>`;
  $('zmModalClose').onclick = $('imCancel').onclick = zmCloseModal;
  $('imOk').onclick = async () => {
    $('imOk').disabled = true;
    let done = 0;
    for (let i = 0; i < pv.ready.length; i += 100) {
      const r = await post('/api/zumen/inbox/import-confirm', { ids: pv.ready.slice(i, i + 100), actor: Z.me || null });
      done += r.confirmed.length;
      $('imRun').innerHTML = `<div class="zim-bar"><i style="width:${Math.round(Math.min(i + 100, pv.ready.length) / pv.ready.length * 100)}%"></i></div>`;
    }
    zmCloseModal();
    showToast(`${done.toLocaleString()}件を確定しました。「図面」と「会社」の画面から確認できます`, { type: 'success', duration: 5000 });
    loadInbox();
  };
}

function confClass(c) { return c == null ? '' : c >= 0.85 ? 'cf-hi' : c >= 0.6 ? 'cf-mid' : 'cf-lo'; }

const INBOX_FILTERS = [['all', 'すべて', () => true], ['order', '注文', x => /注文/.test(x.kind || '')], ['drawing', '図面', x => x.kind === '図面'],
  ['money', '見積・請求', x => /見積|請求|発注|納品/.test(x.kind || '')], ['import', '過去の書類', x => x.source === 'import'], ['check', '要確認', x => x.questions || (x.ai_conf != null && x.ai_conf < 0.6) || x.error || !(x.company && x.company.kind === 'company')]];

function visibleInbox() {
  const f = INBOX_FILTERS.find(x => x[0] === Z.inboxFilter) || INBOX_FILTERS[0];
  return Z.inbox.filter(f[2]);
}

function drawInboxList(r) {
  const f = INBOX_FILTERS.find(x => x[0] === Z.inboxFilter) || INBOX_FILTERS[0];
  const all = Z.inbox.filter(f[2]);
  const limit = Z.inboxShow || 150;   // 過去分で数千件になっても重くならないよう、少しずつ出す
  const items = all.slice(0, limit);
  $('listPane').innerHTML = `
    ${Z.inbox.length > 2 ? `<div class="zm-filters">${INBOX_FILTERS.map(([k, l, fn]) => { const n = Z.inbox.filter(fn).length; return n || k === 'all' ? `<button class="zm-chip ${f[0] === k ? 'is-on' : ''} ${k === 'check' ? 'is-warn' : ''}" data-flt="${k}">${l}<b>${n}</b></button>` : ''; }).join('')}</div>` : ''}
    <div class="zm-list-head" id="inboxHead"></div>
    <div class="zm-drop-inline" id="inlineDrop">${ico('upload', 16)}<span>ここにファイルをドラッグするか、<button class="btn-link" id="inlinePick">ファイルを選択</button></span>
      <label class="zm-noai" title="秘密保持契約などで、外部のAIに送れない書類のときにチェックします"><input type="checkbox" id="noAi" ${Z.noAi ? 'checked' : ''}> AIに送らない</label></div>
    <div class="zm-inbox-list">${items.map(it => `
      <button class="zm-card ${it.id === Z.selId ? 'is-sel' : ''}" data-doc="${it.id}">
        ${thumb(it.file_id, it.page_from)}
        <div class="zm-card-main">
          <div class="zm-card-top">${kindBadge(it.kind)}<span class="zm-conf ${confClass(it.ai_conf)}" title="AIの読み取りの確かさ">${it.ai_conf == null ? '' : `AI確度 ${pct(it.ai_conf)}`}</span>
            ${it.questions ? `<span class="zm-q" title="確認が必要な点の数">${ico('question', 11)}確認 ${it.questions}</span>` : ''}${it.edited ? '<span class="zm-ed">修正あり</span>' : ''}${it.source === 'mail' ? `<span class="zm-src" title="メールで届いた書類">${ico('mail2', 11)}</span>` : ''}</div>
          <div class="zm-card-co ${it.company && it.company.kind === 'company' ? '' : 'is-new'}">${esc(it.company ? it.company.name : (it.counterparty || '会社名を読み取れませんでした'))}
            ${it.company && it.company.kind !== 'company' ? `<em>${it.folder && it.company.name === it.folder ? 'フォルダ名から推定' : '新しい会社'}</em>` : ''}</div>
          <div class="zm-card-sub">${esc([it.doc_no, it.drawings.join('・'), it.title].filter(Boolean).join('　') || it.filename)}</div>
          <div class="zm-card-foot">${it.amount != null ? `<span>${yen(it.amount)}</span>` : ''}${it.due_on ? `<span class="zm-due">納期 ${fmtMD(it.due_on)}</span>` : ''}
            ${it.pages > 1 ? `<span>${it.page_from === it.page_to ? `${it.page_from}ページ目` : `${it.page_from}〜${it.page_to}ページ`}／${it.pages}</span>` : ''}${it.error ? '<span class="zm-err">読み取れませんでした</span>' : ''}</div>
        </div></button>`).join('') || `<div class="boss-empty" style="margin:16px">${Z.inbox.length ? 'この条件に当てはまる書類はありません' : '確認待ちはありません'}</div>`}
      ${all.length > items.length ? `<button class="btn-sm zm-more" id="inboxMore">さらに表示する（残り ${(all.length - items.length).toLocaleString()}件）</button>` : ''}</div>
    <div class="zm-keys">キーボード操作：Enter で確定、J で次の書類、K で前の書類、X で取り込まない</div>`;
  drawInboxHead(r);
  $('listPane').querySelectorAll('[data-doc]').forEach(b => b.onclick = () => selectDoc(Number(b.dataset.doc)));
  $('inlinePick').onclick = () => $('fileInput').click();
  $('noAi').onchange = e => { Z.noAi = e.target.checked; };
  if ($('inboxMore')) $('inboxMore').onclick = () => { Z.inboxShow = (Z.inboxShow || 150) + 150; drawInboxList(r); };
  $('listPane').querySelectorAll('[data-flt]').forEach(b => b.onclick = () => {
    Z.inboxFilter = b.dataset.flt; drawInboxList(r);
    const f = INBOX_FILTERS.find(x => x[0] === Z.inboxFilter) || INBOX_FILTERS[0];
    const cur = Z.inbox.find(x => x.id === Z.selId);
    if (window.innerWidth > 860 && (!cur || !f[2](cur))) { const first = Z.inbox.find(f[2]); if (first) selectDoc(first.id); }
  });
}

async function selectDoc(id) {
  Z.selId = id;
  $('listPane').querySelectorAll('[data-doc]').forEach(b => b.classList.toggle('is-sel', Number(b.dataset.doc) === id));
  $('detailPane').innerHTML = '<div class="empty-text">読み込み中…</div>';
  try { Z.doc = await api(`/api/zumen/docs/${id}`); } catch (e) { $('detailPane').innerHTML = `<div class="empty-text">${esc(e.message)}</div>`; return; }
  if (Z.selId !== id) return;
  startEdit(Z.doc);
  drawReview();
  if (window.innerWidth <= 860) { document.body.classList.add('zm-detail-open'); $('detailPane').scrollTop = 0; }
  if ($('zmBack')) $('zmBack').onclick = () => { document.body.classList.remove('zm-detail-open'); };
}

// 編集中の値（確定で送る形）
function startEdit(doc) {
  const v = doc.values, c = doc.candidates || {};
  const co = c.company;
  Z.edit = {
    kind: v.kind || 'その他', direction: v.direction || '受け取り',
    company: co ? { kind: co.kind, company_id: co.company_id, name: co.name } : (c.companies && c.companies[0]) || { kind: 'new', company_id: null, name: v.counterparty || '' },
    doc_no: v.doc_no || '', ref_no: v.ref_no || '', doc_date: v.doc_date || '', amount: v.amount ?? '', due_on: v.due_on || '', title: v.title || '',
    lines: (v.lines || []).map(x => ({ ...x })), drawings: (v.drawings || []).map(x => ({ ...x })),
    deal: c.deal_pick ? { id: c.deal_pick } : (c.deals && c.deals.length ? null : null), create_deal: false, register_parts: true,
    answers: {},
  };
  if (!Z.edit.deal && !c.deal_pick) Z.edit.dealMode = 'none';
  if (c.deal_pick) Z.edit.dealMode = 'pick';
}

const conf = k => (Z.doc.confidence || {})[k];
function aiMark(k, val) {
  if (!Z.doc.ai_read || val === '' || val == null) return '';
  const c = conf(k);
  return c != null && c < 0.8 ? 'is-low' : 'is-ai';
}

function drawReview() {
  const d = Z.doc, e = Z.edit, c = d.candidates || {};
  const kinds = Z.meta.kinds;
  const showMoney = ['注文書', '注文変更書', '見積書', '請求書', '発注書', '納品書'].includes(e.kind);
  const showDue = ['注文書', '注文変更書', '見積書'].includes(e.kind);
  const showDrawings = e.kind === '図面' || e.drawings.length;
  const multi = d.page_to > d.page_from;
  const coCands = c.companies || [];
  const warns = c.warnings || [];
  $('detailPane').innerHTML = `
  <button class="zm-back" id="zmBack">← 確認待ちの一覧に戻る</button>
  <div class="zr">
    <div class="zr-view" id="zrView"></div>
    <div class="zr-form">
      <div class="zr-head">
        <div class="zr-file">${ico('doc', 13)} ${esc(d.file.filename)}${d.file.pages > 1 ? `（${d.page_from}〜${d.page_to}ページ／全${d.file.pages}）` : ''}</div>
        ${d.error ? `<div class="zr-warn bad">${ico('alert', 13)} ${esc(d.error)}</div>` : ''}
      </div>
      ${warns.map(w => `<div class="zr-warn ${w.level}">${ico(w.level === 'info' ? 'info' : 'alert', 13)}<span>${esc(w.text)}</span>
        ${w.diff ? `<button class="btn-link" data-diff-rev="${w.diff.rev_id}" data-diff-drawing="${w.diff.drawing_id}">前の版と比べる</button>` : ''}
        ${w.drawing_id ? `<a class="btn-link" href="/zumen.html?drawing=${w.drawing_id}" target="_blank">メモを見る</a>` : ''}
        ${w.doc_id ? `<a class="btn-link" href="/zumen.html?doc=${w.doc_id}" target="_blank">開く</a>` : ''}</div>`).join('')}

      <div class="zr-sec">
        <div class="zr-label">書類の種類</div>
        <div class="seg seg-wrap" id="fKind">${kinds.map(k => `<button data-k="${esc(k)}" class="${k === e.kind ? 'is-on' : ''} ${k === e.kind ? aiMark('kind', e.kind) : ''}">${esc(k)}</button>`).join('')}</div>
        <div class="zr-dir"><label><input type="radio" name="dir" value="受け取り" ${e.direction === '受け取り' ? 'checked' : ''}> 受け取った書類</label>
          <label><input type="radio" name="dir" value="発行" ${e.direction === '発行' ? 'checked' : ''}> 自社が発行した書類</label></div>
      </div>

      <div class="zr-sec">
        <div class="zr-label">会社 <span class="zr-raw">${d.values.counterparty ? `書類上の表記：「${esc(d.values.counterparty)}」` : ''}</span></div>
        <div class="zr-cands" id="fCompany">${coCands.map((x, i) => `<label class="zr-cand ${sameCo(x, e.company) ? 'is-on' : ''}">
            <input type="radio" name="co" value="${i}" ${sameCo(x, e.company) ? 'checked' : ''}>
            <span class="zr-cand-main"><b>${esc(x.name)}</b>${x.kind === 'new' ? '<em class="tag-new">新しい会社として登録</em>' : x.kind === 'card' ? '<em class="tag-card">名刺の会社として登録</em>' : ''}
            <small>${esc(x.reason)}</small></span>${x.score ? `<span class="zr-score ${x.score >= 85 ? 'hi' : 'mid'}">${x.score}</span>` : ''}</label>`).join('')}
          <label class="zr-cand ${e.company && e.company.kind === 'search' ? 'is-on' : ''}"><input type="radio" name="co" value="other" ${e.company && e.company.kind === 'search' ? 'checked' : ''}>
            <span class="zr-cand-main"><b>ほかの会社</b><input class="email-input compact" id="coSearch" list="coList" placeholder="会社名で検索、または新しい会社名を入力" value="${e.company && e.company.kind === 'search' ? esc(e.company.name) : ''}"></span></label>
        </div>
        <datalist id="coList"></datalist>
      </div>

      <div class="zr-sec zr-grid">
        <label>${e.kind === '図面' ? '図面の番号' : '書類の番号'}<input class="email-input compact ${aiMark('doc_no', e.doc_no)}" id="fNo" value="${esc(e.doc_no)}"></label>
        <label>関連する番号<input class="email-input compact" id="fRef" value="${esc(e.ref_no)}" placeholder="見積番号・元の注文番号など"></label>
        <label>日付<input type="date" class="email-input compact" id="fDate" value="${esc(e.doc_date)}"></label>
        ${showMoney ? `<label>金額（税抜）<input type="number" class="email-input compact ${aiMark('amount', e.amount)}" id="fAmount" value="${esc(e.amount)}"></label>` : ''}
        ${showDue ? `<label>納期<input type="date" class="email-input compact ${aiMark('due_on', e.due_on)}" id="fDueOn" value="${esc(e.due_on)}"></label>` : ''}
        <label class="span2">件名・品名<input class="email-input compact" id="fTitle" value="${esc(e.title)}" maxlength="60"></label>
      </div>

      ${showMoney || e.lines.length ? `<div class="zr-sec">
        <div class="zr-label">明細 <button class="btn-link" id="addLine">＋ 行を追加</button></div>
        <div class="zr-table-wrap"><table class="zr-table"><thead><tr><th>図番</th><th>版</th><th>品名</th><th>材質</th><th>板厚</th><th class="num">数量</th><th class="num">単価</th><th class="num">金額</th><th>納期</th><th></th></tr></thead>
        <tbody>${e.lines.map((ln, i) => `<tr data-line="${i}">
          ${['drawing_no', 'rev', 'name', 'material', 'thickness', 'qty', 'unit_price', 'amount', 'due_on'].map(k => `<td><input class="zr-cell ${['qty', 'unit_price', 'amount'].includes(k) ? 'num' : ''}" data-f="${k}" ${k === 'due_on' ? 'type="date"' : ['qty', 'unit_price', 'amount'].includes(k) ? 'type="number"' : ''} value="${esc(ln[k] ?? '')}"></td>`).join('')}
          <td><button class="zr-x" data-del-line="${i}" title="行を削除">×</button></td></tr>`).join('') || '<tr><td colspan="10" class="zr-none">明細はありません</td></tr>'}</tbody></table></div>
      </div>` : ''}

      ${showDrawings ? `<div class="zr-sec">
        <div class="zr-label">図番と版 <button class="btn-link" id="addDrawing">＋ 図番を追加</button></div>
        ${e.drawings.map((dw, i) => {
          const st = (c.drawings || [])[i];
          const s = st ? DR_STATUS[st.status] : null;
          return `<div class="zr-dw" data-dw="${i}">
            <div class="zr-dw-row"><label>図番<input class="email-input compact ${aiMark('drawing_no', dw.drawing_no)}" data-df="drawing_no" value="${esc(dw.drawing_no)}"></label>
              <label class="w-s">版<input class="email-input compact ${aiMark('rev', dw.rev)}" data-df="rev" value="${esc(dw.rev)}" placeholder="初版"></label>
              <label>品名<input class="email-input compact" data-df="name" value="${esc(dw.name)}"></label>
              ${multi ? `<label class="w-s">ページ<input type="number" class="email-input compact" data-df="page" value="${esc(dw.page)}" min="${d.page_from}" max="${d.page_to}"></label>` : ''}
              <button class="zr-x" data-del-dw="${i}" title="削除">×</button></div>
            <div class="zr-dw-row"><label>材質<input class="email-input compact" data-df="material" value="${esc(dw.material)}"></label>
              <label class="w-s">板厚<input class="email-input compact" data-df="thickness" value="${esc(dw.thickness)}"></label>
              <label>表面処理<input class="email-input compact" data-df="surface" value="${esc(dw.surface)}"></label></div>
            <div class="zr-dw-row"><label class="span3">改訂内容<input class="email-input compact" data-df="change_note" value="${esc(dw.change_note)}"></label></div>
            <div class="zr-dw-foot">${s ? `<span class="zr-st ${s[1]}">${s[0]}${st.current_rev != null ? `（登録済みの最新版：${esc(st.current_rev || '初版')}）` : ''}</span>` : ''}
              ${st && st.drawing_id ? `<a class="btn-link" href="/zumen.html?drawing=${st.drawing_id}" target="_blank">登録済みの図面を見る</a>` : ''}
              ${st && st.drawing_id && st.current_rev != null && st.status !== 'same_file' ? `<button class="btn-link" data-diff-drawing="${st.drawing_id}" data-diff-page="${dw.page || d.page_from}">登録済みの版と比べる</button>` : ''}
              ${(dw.parts || []).length ? `<span class="zr-parts">部品表 ${dw.parts.length}点</span>` : ''}</div>
          </div>`;
        }).join('') || '<div class="zr-none">図番を読み取れませんでした。「＋ 図番を追加」から入力してください</div>'}
        ${e.drawings.some(x => (x.parts || []).length) ? `<label class="zr-check"><input type="checkbox" id="fParts" ${e.register_parts ? 'checked' : ''}> 部品表の内容を、組立図と部品の関係として登録する</label>` : ''}
      </div>` : ''}

      <div class="zr-sec">
        <div class="zr-label">紐づける案件</div>
        <div class="zr-cands" id="fDeal">${(c.deals || []).map(x => `<label class="zr-cand ${e.dealMode === 'pick' && e.deal && e.deal.id === x.deal_id ? 'is-on' : ''}">
            <input type="radio" name="deal" value="${x.deal_id}" ${e.dealMode === 'pick' && e.deal && e.deal.id === x.deal_id ? 'checked' : ''}>
            <span class="zr-cand-main"><b>#${x.deal_id} ${esc(x.title)}</b> <span class="stage-badge" data-stage="${esc(x.stage)}">${esc(x.stage)}</span>
            <small>${esc(x.reasons.join('・'))}</small></span><span class="zr-score ${x.score >= 70 ? 'hi' : 'mid'}">${x.score}</span></label>`).join('')}
          <label class="zr-cand ${e.dealMode === 'new' ? 'is-on' : ''}"><input type="radio" name="deal" value="new" ${e.dealMode === 'new' ? 'checked' : ''}>
            <span class="zr-cand-main"><b>新しい案件を作る</b><small>${e.kind === '注文書' ? '営業の画面に、受注の案件として作成します' : '営業の画面に案件を作成します'}</small></span></label>
          <label class="zr-cand ${e.dealMode === 'none' ? 'is-on' : ''}"><input type="radio" name="deal" value="none" ${e.dealMode === 'none' ? 'checked' : ''}>
            <span class="zr-cand-main"><b>紐づけない</b><small>会社のフォルダにだけ保存します</small></span></label>
        </div>
      </div>

      ${(c.related || []).length ? `<div class="zr-sec"><div class="zr-label">関連する書類</div>${c.related.map(x => `<a class="zr-rel" href="/zumen.html?doc=${x.doc_id}" target="_blank">
        ${kindBadge(x.kind)} ${esc(x.doc_no || '')} ${x.doc_date ? fmtDate(x.doc_date) : ''} ${x.amount != null ? yen(x.amount) : ''}<small>${esc(x.reason)}</small></a>`).join('')}</div>` : ''}

      ${d.questions.length ? `<div class="zr-sec zr-qs"><div class="zr-label">${ico('ai', 13)} AIからの確認事項</div>
        ${d.questions.map((q, i) => `<div class="zr-q"><div>${esc(q)}</div><input class="email-input compact" data-q="${i}" placeholder="回答を入力すると、AIが読み取り直します" value="${esc(e.answers[i] || '')}"></div>`).join('')}
        <button class="btn-sm zr-ai-btn" id="answerBtn">${ico('ai', 13)} 回答をもとにAIに読み直してもらう</button></div>` : ''}

      <div class="zr-actions">
        <button class="btn-sm btn-sm-primary zr-ok" id="confirmBtn">${ico('check', 14)} この内容で確定</button>
        <button class="btn-sm" id="rejectBtn">取り込まない</button>
        ${multi ? `<button class="btn-sm" id="splitBtn">ページで分ける…</button>` : ''}
        <span class="zr-saved" id="savedNote">${d.edited ? '修正内容は保存されています' : ''}</span>
      </div>
    </div>
  </div>`;
  zvCreate($('zrView'), { fileId: d.file.id, pages: d.file.pages, pageFrom: d.page_from, pageTo: d.page_to, title: d.file.filename });
  bindReview();
}

function sameCo(a, b) {
  if (!a || !b) return false;
  if (a.company_id && b.company_id) return a.company_id === b.company_id;
  return !a.company_id && !b.company_id && a.kind === b.kind && a.name === b.name;
}

let COMPANIES = null;
async function companyList() {
  if (!COMPANIES) COMPANIES = (await api('/api/zumen/companies')).companies;
  return COMPANIES;
}

function bindReview() {
  const e = Z.edit, d = Z.doc, c = d.candidates || {};
  const root = $('detailPane');
  const save = debounce(saveEdit, 700);
  root.querySelectorAll('#fKind [data-k]').forEach(b => b.onclick = () => { e.kind = b.dataset.k; if (e.kind === '図面' && !e.drawings.length) e.drawings.push(blankDw()); drawReview(); save(); });
  root.querySelectorAll('input[name="dir"]').forEach(r => r.onchange = () => { e.direction = r.value; save(); });
  root.querySelectorAll('input[name="co"]').forEach(r => r.onchange = async () => {
    if (r.value === 'other') { $('coSearch').focus(); return; }
    const x = c.companies[Number(r.value)];
    e.company = { kind: x.kind, company_id: x.company_id, name: x.name };
    await saveEdit(true);
  });
  companyList().then(list => { $('coList').innerHTML = list.map(x => `<option value="${esc(x.name)}">`).join(''); });
  $('coSearch').onfocus = () => { root.querySelector('input[name="co"][value="other"]').checked = true; };
  $('coSearch').onchange = async () => {
    const name = $('coSearch').value.trim();
    if (!name) return;
    const list = await companyList();
    const hit = list.find(x => x.name === name);
    e.company = hit ? { kind: 'search', company_id: hit.id, name: hit.name } : { kind: 'search', company_id: null, name };
    await saveEdit(true);
  };
  const bindIn = (id, k, num) => { const el = $(id); if (el) el.oninput = () => { e[k] = num ? (el.value === '' ? '' : Number(el.value)) : el.value; save(); }; };
  bindIn('fNo', 'doc_no'); bindIn('fRef', 'ref_no'); bindIn('fDate', 'doc_date'); bindIn('fAmount', 'amount', true); bindIn('fDueOn', 'due_on'); bindIn('fTitle', 'title');
  root.querySelectorAll('[data-line]').forEach(tr => tr.querySelectorAll('[data-f]').forEach(inp => inp.oninput = () => {
    const ln = e.lines[Number(tr.dataset.line)], f = inp.dataset.f;
    ln[f] = ['qty', 'unit_price', 'amount'].includes(f) ? (inp.value === '' ? null : Number(inp.value)) : inp.value;
    if ((f === 'qty' || f === 'unit_price') && ln.qty != null && ln.unit_price != null) {
      ln.amount = Math.round(ln.qty * ln.unit_price);
      const a = tr.querySelector('[data-f="amount"]'); if (a) a.value = ln.amount;
    }
    save();
  }));
  root.querySelectorAll('[data-del-line]').forEach(b => b.onclick = () => { e.lines.splice(Number(b.dataset.delLine), 1); drawReview(); save(); });
  if ($('addLine')) $('addLine').onclick = () => { e.lines.push({ drawing_no: '', rev: '', name: '', material: '', thickness: '', qty: null, unit_price: null, amount: null, due_on: '' }); drawReview(); };
  root.querySelectorAll('[data-dw]').forEach(box => box.querySelectorAll('[data-df]').forEach(inp => inp.oninput = () => {
    const dw = e.drawings[Number(box.dataset.dw)];
    dw[inp.dataset.df] = inp.dataset.df === 'page' ? Number(inp.value) : inp.value; save();
  }));
  root.querySelectorAll('[data-del-dw]').forEach(b => b.onclick = () => { e.drawings.splice(Number(b.dataset.delDw), 1); drawReview(); save(); });
  if ($('addDrawing')) $('addDrawing').onclick = () => { e.drawings.push(blankDw()); drawReview(); };
  if ($('fParts')) $('fParts').onchange = ev => { e.register_parts = ev.target.checked; };
  root.querySelectorAll('input[name="deal"]').forEach(r => r.onchange = () => {
    if (r.value === 'new') { e.dealMode = 'new'; e.deal = null; } else if (r.value === 'none') { e.dealMode = 'none'; e.deal = null; } else { e.dealMode = 'pick'; e.deal = { id: Number(r.value) }; }
    root.querySelectorAll('#fDeal .zr-cand').forEach(l => l.classList.toggle('is-on', l.querySelector('input').checked));
  });
  root.querySelectorAll('[data-q]').forEach(inp => inp.oninput = () => { e.answers[inp.dataset.q] = inp.value; });
  if ($('answerBtn')) $('answerBtn').onclick = answer;
  root.querySelectorAll('[data-diff-drawing]').forEach(b => b.onclick = () => diffWithCurrent(Number(b.dataset.diffDrawing), Number(b.dataset.diffPage || d.page_from)));
  $('confirmBtn').onclick = confirmDoc;
  $('rejectBtn').onclick = rejectDoc;
  if ($('splitBtn')) $('splitBtn').onclick = splitDoc;
}

function blankDw() { return { page: Z.doc.page_from, drawing_no: '', rev: '', name: '', material: '', thickness: '', surface: '', change_note: '', parts: [] }; }

async function diffWithCurrent(drawingId, page) {
  try {
    const dd = await api(`/api/zumen/drawings/${drawingId}`);
    if (!dd.current) { showToast('比べられる版がまだありません', { type: 'warning' }); return; }
    zvDiff({ fileId: dd.current.file_id, page: dd.current.page_no || 1, label: `登録済みの最新版（${dd.current.rev || '初版'}）` },
      { fileId: Z.doc.file.id, page, label: '今回の図面' });
  } catch (err) { showToast(err.message, { type: 'error' }); }
}

function editPayload() {
  const e = Z.edit;
  return { kind: e.kind, direction: e.direction, doc_no: e.doc_no, ref_no: e.ref_no, doc_date: e.doc_date || null, amount: e.amount === '' ? null : e.amount,
    due_on: e.due_on || null, title: e.title, lines: e.lines, drawings: e.drawings };
}

async function saveEdit(redraw = false) {
  if (!Z.doc || Z.doc.status !== 'review') return;
  const id = Z.doc.id;
  try {
    const r = await put(`/api/zumen/docs/${id}`, { values: editPayload(), company: Z.edit.company });
    if (Z.doc && Z.doc.id === id) {
      Z.doc.candidates = r.candidates; Z.doc.edited = true;
      if (redraw) {
        const keepMode = Z.edit.dealMode, keepDeal = Z.edit.deal;
        if (r.candidates.deal_pick && keepMode !== 'new') { Z.edit.dealMode = 'pick'; Z.edit.deal = { id: r.candidates.deal_pick }; }
        else if (keepMode === 'pick' && !(r.candidates.deals || []).some(x => keepDeal && x.deal_id === keepDeal.id)) { Z.edit.dealMode = 'none'; Z.edit.deal = null; }
        drawReview();
      }
      const n = $('savedNote'); if (n) n.textContent = '修正を保存しました';
    }
  } catch (err) { showToast(err.message, { type: 'error' }); }
}

async function confirmDoc() {
  const e = Z.edit, d = Z.doc;
  if (!e.company || !(e.company.company_id || (e.company.name || '').trim())) { showToast('会社を選んでください', { type: 'warning' }); return; }
  const body = { ...editPayload(), company_id: e.company.company_id || null, company_name: e.company.company_id ? null : e.company.name,
    card_company: e.company.kind === 'card' ? e.company.name : null,
    deal_id: e.dealMode === 'pick' && e.deal ? e.deal.id : null, create_deal: e.dealMode === 'new', register_parts: e.register_parts, actor: Z.me || null };
  const btn = $('confirmBtn'); btn.disabled = true; btn.textContent = '確定しています…';
  let r;
  try { r = await post(`/api/zumen/docs/${d.id}/confirm`, body); } catch (err) { showToast(err.message, { type: 'error' }); btn.disabled = false; btn.innerHTML = `${ico('check', 14)} この内容で確定`; return; }
  COMPANIES = null;
  const vis = visibleInbox(), idx = vis.findIndex(x => x.id === d.id);
  const next = vis[idx + 1] || vis[idx - 1];
  const parts = [`${e.kind}を確定しました`];
  if (r.deal_id) parts.push(`案件 #${r.deal_id} に紐づけました`);
  showToast(parts.join('。'), { type: 'success', duration: 7000, action: { label: '元に戻す', onClick: () => undoEvent(r.event_id, d.id) },
    action2: r.suggest_won ? { label: '案件を受注に進める', onClick: () => advanceWon(r.suggest_won) } : null });
  (r.notes || []).forEach(n => showToast(n.text, { type: 'warning', duration: 9000,
    action: { label: '案件の納期も更新する', onClick: async () => { await api(`/api/sales/deals/${r.deal_id}`, { method: 'PATCH', body: JSON.stringify({ delivery_on: n.due_on }) }); showToast('案件の納期を更新しました', { type: 'success' }); } } }));
  Z.selId = next ? next.id : null;
  await loadInbox();
  if (Z.selId) selectDoc(Z.selId);
}

async function advanceWon(dealId) {
  try { await api(`/api/sales/deals/${dealId}`, { method: 'PATCH', body: JSON.stringify({ stage: '受注' }) }); showToast(`案件 #${dealId} を受注にしました`, { type: 'success' }); }
  catch (err) { showToast(err.message, { type: 'error' }); }
}

async function undoEvent(eventId, docId) {
  try {
    await post(`/api/zumen/events/${eventId}/undo`);
    showToast('元に戻しました', { type: 'success' });
    COMPANIES = null;
    if (Z.view === 'inbox') { Z.selId = docId || Z.selId; await loadInbox(); if (docId) selectDoc(docId); } else setView(Z.view);
  } catch (err) { showToast(err.message, { type: 'error' }); }
}

async function rejectDoc() {
  const d = Z.doc;
  const vis = visibleInbox(), idx = vis.findIndex(x => x.id === d.id);
  const next = vis[idx + 1] || vis[idx - 1];
  try { await post(`/api/zumen/docs/${d.id}/reject`); } catch (err) { showToast(err.message, { type: 'error' }); return; }
  showToast('取り込みを取りやめました。ごみ箱に30日間保管されます', { action: { label: '元に戻す', onClick: async () => { await post(`/api/zumen/docs/${d.id}/restore`); loadInbox(); } } });
  Z.selId = next ? next.id : null;
  await loadInbox();
  if (Z.selId) selectDoc(Z.selId);
}

async function splitDoc() {
  const d = Z.doc;
  const v = prompt(`何ページ目から別の書類にしますか？（${d.page_from + 1}〜${d.page_to}）`, String(d.page_from + 1));
  if (!v) return;
  try { await post(`/api/zumen/docs/${d.id}/split`, { at_page: Number(v) }); showToast('書類を分けました。AIが読み取り直しています'); loadInbox(); }
  catch (err) { showToast(err.message, { type: 'error' }); }
}

async function answer() {
  const d = Z.doc, e = Z.edit;
  const answers = d.questions.map((q, i) => ({ q, a: e.answers[i] || '' })).filter(x => x.a.trim());
  if (!answers.length) { showToast('回答を入力してください', { type: 'warning' }); return; }
  try { await post(`/api/zumen/docs/${d.id}/answer`, { answers }); showToast('AIが読み取り直しています。終わると「確認待ち」に戻ります'); Z.selId = null; loadInbox(); }
  catch (err) { showToast(err.message, { type: 'error' }); }
}

async function bulkConfirm() {
  const pv = await api('/api/zumen/inbox/bulk-preview');
  if (!pv.ids.length) { showToast('まとめて確定できる書類はありません（AIの読み取りの確かさが基準に届いていないか、確認が必要な点があります）'); return; }
  if (!await showConfirm({ title: 'まとめて確定', message: `AIの読み取りの確かさが基準以上で、確認が必要な点のない${pv.ids.length}件を、表示されている内容のまま確定します。`, okLabel: `${pv.ids.length}件を確定` })) return;
  const r = await post('/api/zumen/inbox/bulk-confirm', { ids: pv.ids, actor: Z.me || null });
  showToast(`${r.confirmed.length}件を確定しました${r.skipped.length ? `（${r.skipped.length}件は確認が必要なので残しました）` : ''}`, { type: 'success', duration: 8000,
    action: r.events.length ? { label: 'まとめて元に戻す', onClick: async () => { for (const id of r.events) await post(`/api/zumen/events/${id}/undo`).catch(() => {}); loadInbox(); } } : null });
  Z.selId = null; loadInbox();
}

// キー操作（入力中は使わない）
document.addEventListener('keydown', ev => {
  if (Z.view !== 'inbox' || !Z.doc || $('zmModal').style.display !== 'none') return;
  const tag = (ev.target.tagName || '').toLowerCase();
  if (['input', 'textarea', 'select'].includes(tag) || ev.metaKey || ev.ctrlKey || ev.altKey) return;
  const vis = visibleInbox(), idx = vis.findIndex(x => x.id === Z.selId);
  if (ev.key === 'Enter') { ev.preventDefault(); confirmDoc(); }
  if ((ev.key === 'j' || ev.key === 'J') && vis[idx + 1]) selectDoc(vis[idx + 1].id);
  if ((ev.key === 'k' || ev.key === 'K') && vis[idx - 1]) selectDoc(vis[idx - 1].id);
  if (ev.key === 'x' || ev.key === 'X') rejectDoc();
});

const ZVIEWS = {};
document.addEventListener('DOMContentLoaded', init);
