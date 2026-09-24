// ============================================================
// 名刺管理 v4 - 編集機能追加
// ============================================================

const state = {
  allGroups: [],
  allCards: [],
  allTags: [],
  selectedId: null,
  searchQuery: '',
  activeFilter: 'all',
  activeTag: null,
  activeAssignedTo: null,  // v6.8 担当者フィルター
  tenant: null,
  editingNote: false,
  addingTag: false,
  editingField: null,
  editingAll: false,
  editAllDraft: {},
  // v5 追加
  selectionMode: false,
  selectedCardIds: new Set(),
  duplicatesCount: 0,
  currentMergePair: null,
  currentMergeChoices: {},
  // v5.1 追加: メール送信元
  currentSender: null,
  presets: [],
  emailModalBodyTemplate: null,
  // v6.5 追加: 現在表示中のカードID
  visibleCardIds: [],
};

const $ = id => document.getElementById(id);

const els = {
  tenantName: $('tenantName'),
  cardCount: $('cardCount'),
  searchInput: $('searchInput'),
  filterRow: $('filterRow'),
  tagFilterGroup: $('tagFilterGroup'),
  listPane: $('listPane'),
  detailPane: $('detailPane'),
  refreshBtn: $('refreshBtn'),
  switchBtn: $('switchBtn'),
  mobileSheet: $('mobileSheet'),
  mobileBody: $('mobileSheetBody'),
  mobileBack: $('mobileBackBtn'),
  // v5
  dupesBtn: $('dupesBtn'),
  dupesCount: $('dupesCount'),
  selectionBar: $('selectionBar'),
  selectionCount: $('selectionCount'),
  sendEmailBtn: $('sendEmailBtn'),
  bulkDeleteBtn: $('bulkDeleteBtn'),
  clearSelectionBtn: $('clearSelectionBtn'),
  // v6.5
  bulkSelectBtn: $('bulkSelectBtn'),
  bulkSelectLabel: $('bulkSelectLabel'),
  dupeModalOverlay: $('dupeModalOverlay'),
  dupeModalBody: $('dupeModalBody'),
  closeDupeModal: $('closeDupeModal'),
  mergeModalOverlay: $('mergeModalOverlay'),
  mergeModalBody: $('mergeModalBody'),
  closeMergeModal: $('closeMergeModal'),
  cancelMergeBtn: $('cancelMergeBtn'),
  confirmMergeBtn: $('confirmMergeBtn'),
  emailModalOverlay: $('emailModalOverlay'),
  emailRecipientList: $('emailRecipientList'),
  emailSubject: $('emailSubject'),
  emailBody: $('emailBody'),
  emailFooterInfo: $('emailFooterInfo'),
  cancelEmailBtn: $('cancelEmailBtn'),
  sendEmailConfirmBtn: $('sendEmailConfirmBtn'),
  closeEmailModal: $('closeEmailModal'),
  // v5.1
  senderBarName: $('senderBarName'),
  senderBarEmail: $('senderBarEmail'),
  openSenderPickerBtn: $('openSenderPickerBtn'),
  senderModalOverlay: $('senderModalOverlay'),
  senderModalBody: $('senderModalBody'),
  closeSenderModal: $('closeSenderModal'),
};

// フィールド定義（ラベル、タイプ、など）
const FIELD_DEFS = [
  { key: 'email',               label: 'メール',    type: 'email',    placeholder: 'name@example.com' },
  { key: 'mobile',              label: '携帯',      type: 'tel',      placeholder: '090-1234-5678' },
  { key: 'phone',               label: '会社電話',  type: 'tel',      placeholder: '03-1234-5678' },
  { key: 'address',             label: '住所',      type: 'textarea', placeholder: '〒...' },
  { key: 'website',             label: 'サイト',    type: 'url',      placeholder: 'example.com' },
  { key: 'assigned_to',         label: '所有者',    type: 'text',     placeholder: '所有者名を入力' },
];

// ============================================================
// API helpers
// ============================================================
async function api(path, options = {}) {
  const res = await fetch(path, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (res.status === 401) {
    location.href = '/';
    throw new Error('Unauthorized');
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function highlight(text, query) {
  if (!query || !text) return escapeHtml(text || '');
  const q = query.toLowerCase();
  const t = String(text);
  const tl = t.toLowerCase();
  let out = '';
  let i = 0;
  while (i < t.length) {
    const idx = tl.indexOf(q, i);
    if (idx === -1) { out += escapeHtml(t.slice(i)); break; }
    out += escapeHtml(t.slice(i, idx));
    out += '<span class="hl">' + escapeHtml(t.slice(idx, idx + q.length)) + '</span>';
    i = idx + q.length;
  }
  return out;
}

function getInitial(name) {
  if (!name) return '?';
  const trimmed = name.trim();
  const firstChar = trimmed.charAt(0);
  if (/[\u4e00-\u9fa5\u3040-\u309f\u30a0-\u30ff]/.test(firstChar)) {
    const parts = trimmed.split(/\s+/);
    return parts[0].slice(0, 1);
  }
  return firstChar.toUpperCase();
}

const COLOR_PALETTE = [
  { bg: '#EEF4FF', fg: '#1C3685' },
  { bg: '#F4F3FF', fg: '#5925DC' },
  { bg: '#FDF2FA', fg: '#C11574' },
  { bg: '#FEF3F2', fg: '#B42318' },
  { bg: '#FFFAEB', fg: '#B54708' },
  { bg: '#FEFBE8', fg: '#A15C07' },
  { bg: '#ECFDF3', fg: '#027A48' },
  { bg: '#ECFDFF', fg: '#0E7090' },
  { bg: '#F8F9FC', fg: '#363F72' },
];
function colorForCompany(normalized) {
  if (!normalized) return COLOR_PALETTE[0];
  let hash = 0;
  for (let i = 0; i < normalized.length; i++) {
    hash = (hash * 31 + normalized.charCodeAt(i)) >>> 0;
  }
  return COLOR_PALETTE[hash % COLOR_PALETTE.length];
}

function debounce(fn, wait) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), wait); };
}

function daysSince(dateStr) {
  if (!dateStr) return Infinity;
  const d = new Date(dateStr);
  if (isNaN(d)) return Infinity;
  return Math.floor((Date.now() - d.getTime()) / (1000 * 60 * 60 * 24));
}

// ============================================================
// Init
// ============================================================
async function init() {
  try {
    const meData = await api('/api/me');
    state.tenant = meData.tenant;
    els.tenantName.textContent = state.tenant.display_name;
  } catch {
    location.href = '/';
    return;
  }
  await Promise.all([loadCards(), loadTags(), loadDupesCount()]);
}

async function loadDupesCount() {
  try {
    const data = await api('/api/duplicates');
    state.duplicatesCount = data.count || 0;
    if (state.duplicatesCount > 0) {
      els.dupesCount.textContent = state.duplicatesCount;
      els.dupesCount.style.display = '';
    } else {
      els.dupesCount.style.display = 'none';
    }
  } catch (err) {
    console.error('重複取得失敗', err);
  }
}

// ============================================================
// Data loading
// ============================================================
async function loadCards(keepSelection = true) {
  try {
    const params = new URLSearchParams();
    if (state.searchQuery) params.set('q', state.searchQuery);
    if (state.activeTag) params.set('tag', state.activeTag);
    const data = await api(`/api/cards?${params}`);
    state.allGroups = data.groups || [];
    state.allCards = state.allGroups.flatMap(g => g.cards);
    els.cardCount.textContent = `${data.total}件`;
    renderList();
    renderAssignedToFilter();  // 担当者フィルター更新 (v6.8)
    if (keepSelection && state.selectedId) {
      const card = state.allCards.find(c => c.id === state.selectedId);
      if (card) renderDetailInto(els.detailPane, card);
    }
  } catch (err) {
    els.listPane.innerHTML = `<div class="empty-text">エラー: ${escapeHtml(err.message)}</div>`;
  }
}

async function loadTags() {
  try {
    const data = await api('/api/tags');
    state.allTags = data.tags || [];
    renderTagFilter();
  } catch (err) {
    console.error('タグ取得失敗', err);
  }
}

// ============================================================
// Tag filter bar
// ============================================================
function renderTagFilter() {
  if (!state.allTags.length) {
    els.tagFilterGroup.innerHTML = '';
    return;
  }
  els.tagFilterGroup.innerHTML = state.allTags.map(t => {
    const active = state.activeTag === t.tag ? 'chip-active' : '';
    return `
      <button class="chip ${active}" data-tag="${escapeHtml(t.tag)}">
        <span class="tag-prefix">#</span>${escapeHtml(t.tag)}
        <span class="chip-count">${t.count}</span>
      </button>
    `;
  }).join('');

  els.tagFilterGroup.querySelectorAll('[data-tag]').forEach(el => {
    el.addEventListener('click', () => {
      const tag = el.dataset.tag;
      state.activeTag = state.activeTag === tag ? null : tag;
      renderTagFilter();
      loadCards();
    });
  });
}

// 担当者フィルター描画 (v6.8)
function renderAssignedToFilter() {
  const container = document.getElementById('assignedToFilterGroup');
  const sep = document.getElementById('assignedToFilterSep');
  if (!container) return;

  // 所有者一覧を収集（カンマ区切り複数対応・重複なし・空除外・件数付き）
  const counts = {};
  state.allCards.forEach(c => {
    const list = (c.assigned_to_list && c.assigned_to_list.length)
      ? c.assigned_to_list
      : (c.assigned_to ? c.assigned_to.split(',').map(x => x.trim()).filter(Boolean) : []);
    list.forEach(name => {
      if (name) counts[name] = (counts[name] || 0) + 1;
    });
  });
  const persons = Object.entries(counts).sort((a, b) => b[1] - a[1]);

  if (!persons.length) {
    container.innerHTML = '';
    if (sep) sep.style.display = 'none';
    return;
  }

  if (sep) sep.style.display = '';
  container.innerHTML = persons.map(([name, count]) => {
    const active = state.activeAssignedTo === name ? 'chip-active' : '';
    return `
      <button class="chip chip-assigned ${active}" data-assigned="${escapeHtml(name)}">
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="8" r="4"/><path d="M4 21v-1a6 6 0 0 1 6-6h4a6 6 0 0 1 6 6v1"/>
        </svg>
        ${escapeHtml(name)}
        <span class="chip-count">${count}</span>
      </button>
    `;
  }).join('');

  container.querySelectorAll('[data-assigned]').forEach(el => {
    el.addEventListener('click', () => {
      const name = el.dataset.assigned;
      state.activeAssignedTo = state.activeAssignedTo === name ? null : name;
      renderAssignedToFilter();
      renderList();
    });
  });
}

// ============================================================
// List
// ============================================================
function renderList() {
  let groups = state.allGroups;

  if (state.activeFilter === 'recent') {
    groups = groups.map(g => ({ ...g, cards: g.cards.filter(c => daysSince(c.registered_date) <= 30) }))
                   .filter(g => g.cards.length > 0);
  } else if (state.activeFilter === 'with-note') {
    groups = groups.map(g => ({ ...g, cards: g.cards.filter(c => (c.note || '').trim().length > 0) }))
                   .filter(g => g.cards.length > 0);
  }

  // 所有者フィルター (v6.9: カンマ区切り複数対応)
  if (state.activeAssignedTo) {
    groups = groups.map(g => ({
      ...g,
      cards: g.cards.filter(c => {
        const list = (c.assigned_to_list && c.assigned_to_list.length)
          ? c.assigned_to_list
          : (c.assigned_to ? [c.assigned_to] : []);
        return list.some(a => a === state.activeAssignedTo);
      })
    })).filter(g => g.cards.length > 0);
  }

  // 表示中のカードIDを集計（v6.5 一括選択用）
  state.visibleCardIds = [];
  groups.forEach(g => g.cards.forEach(c => state.visibleCardIds.push(c.id)));
  updateBulkSelectBtn();

  if (groups.length === 0) {
    els.listPane.innerHTML = state.searchQuery
      ? `<div class="empty-text">「${escapeHtml(state.searchQuery)}」に一致する名刺はありません</div>`
      : `<div class="empty-text">表示する名刺がありません</div>`;
    return;
  }

  const q = state.searchQuery;
  els.listPane.innerHTML = groups.map(group => {
    const color = colorForCompany(group.company_normalized);
    const head = `
      <div class="company-group-head">
        <div class="company-dot" style="background:${color.fg}"></div>
        <div class="company-name">${highlight(group.company, q)}</div>
        <div class="company-count">${group.cards.length}名</div>
      </div>
    `;
    return `<div class="company-group">${head}${group.cards.map(c => rowHtml(c, q, color)).join('')}</div>`;
  }).join('');

  // 行クリック
  els.listPane.querySelectorAll('.row').forEach(el => {
    el.addEventListener('click', (e) => {
      // チェックボックスのクリックは伝播しないが念のため
      if (e.target.closest('[data-checkbox]')) return;
      selectCard(el.dataset.id);
    });
  });

  // チェックボックスクリック
  els.listPane.querySelectorAll('[data-checkbox]').forEach(el => {
    el.addEventListener('click', (e) => {
      e.stopPropagation();
      if (el.classList.contains('is-disabled')) return;
      const id = el.dataset.checkbox;
      if (state.selectedCardIds.has(id)) {
        state.selectedCardIds.delete(id);
      } else {
        state.selectedCardIds.add(id);
      }
      updateSelectionBar();
      renderList();
    });
  });
}

function rowHtml(card, q, color) {
  const active = card.id === state.selectedId ? 'row-active' : '';
  const initial = getInitial(card.name);
  const hasNote = (card.note || '').trim().length > 0;
  const avatarStyle = `background:${color.bg};color:${color.fg}`;
  const noteBadge = hasNote ? `
    <svg class="row-note-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
      <polyline points="14 2 14 8 20 8"/>
    </svg>
  ` : '';

  const hasEmail = !!(card.email || '').trim();
  const isChecked = state.selectedCardIds.has(card.id);
  const cbClass = `row-checkbox ${isChecked ? 'is-checked' : ''} ${!hasEmail ? 'is-disabled' : ''}`;
  const cbTitle = hasEmail ? 'メール送信対象として選択' : 'メールアドレスが未設定';
  const checkbox = `
    <span class="${cbClass}" data-checkbox="${escapeHtml(card.id)}" title="${cbTitle}">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">
        <polyline points="20 6 9 17 4 12"/>
      </svg>
    </span>
  `;

  return `
    <div class="row ${active}" data-id="${escapeHtml(card.id)}">
      ${checkbox}
      <div class="avatar" style="${avatarStyle}">${escapeHtml(initial)}</div>
      <div class="row-body">
        <div class="row-name">${highlight(card.name || '(名前なし)', q)}</div>
        <div class="row-role">${highlight(card.department_position || '—', q)}</div>
      </div>
      ${noteBadge}
    </div>
  `;
}

// ============================================================
// Detail
// ============================================================
function selectCard(id) {
  state.selectedId = id;
  // 編集系ステートをクリア
  state.editingNote = false;
  state.addingTag = false;
  state.editingField = null;
  state.editingAll = false;
  state.editAllDraft = {};

  const card = state.allCards.find(c => c.id === id);
  if (!card) return;

  renderDetailInto(els.detailPane, card);

  if (window.innerWidth <= 860) {
    renderDetailInto(els.mobileBody, card);
    els.mobileSheet.classList.add('is-open');
  }

  els.listPane.querySelectorAll('.row').forEach(el => {
    el.classList.toggle('row-active', el.dataset.id === id);
  });
}

function renderDetailInto(container, card) {
  container.innerHTML = detailHtml(card);
  attachDetailHandlers(container, card);
}

function detailHtml(card) {
  const color = colorForCompany(card.company_normalized);
  const initial = getInitial(card.name);
  const overrides = card.overrides || {};

  // Header (編集モード時は input に差し替え)
  const headerInner = state.editingAll ? headerEditHtml(card) : headerViewHtml(card);

  // Quick actions (view mode only)
  const actions = [];
  if (!state.editingAll) {
    if (card.email) actions.push(quickBtn('mail', 'メール', `mailto:${card.email}`));
    if (card.mobile) actions.push(quickBtn('phone', '電話', `tel:${card.mobile.replace(/[^\d+]/g, '')}`));
  }

  // Fields
  const fieldsHtml = FIELD_DEFS.map(def => fieldRowHtml(card, def, overrides)).join('');
  const registeredRow = card.registered_date && !state.editingAll
    ? `<div class="field-row">
         <div class="field-label">登録日</div>
         <div class="field-value">${escapeHtml(card.registered_date)}</div>
       </div>`
    : '';

  // Notes
  const hasNote = (card.note || '').trim().length > 0;
  const noteSection = state.editingNote
    ? noteEditorHtml(card.note || '')
    : (hasNote ? noteDisplayHtml(card.note) : `<button class="add-btn" data-act="edit-note">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
        メモを追加
      </button>`);

  // Tags
  const hasTags = (card.tags || []).length > 0;
  const tagSection = hasTags
    ? tagsDisplayHtml(card.tags, state.addingTag)
    : (state.addingTag ? tagsDisplayHtml([], true) : `<button class="add-btn" data-act="add-tag">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
        タグを付ける
      </button>`);

  // Contact section label with edit-all button
  const contactLabel = state.editingAll
    ? `<div class="section-label">
         <span>連絡先 (編集中)</span>
       </div>`
    : `<div class="section-label">
         <span>連絡先</span>
         <button class="section-action edit-all" data-act="edit-all">
           <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
           一括編集
         </button>
       </div>`;

  const editActionBar = state.editingAll
    ? `<div class="edit-action-bar">
         <button class="btn-sm" data-act="edit-all-cancel">キャンセル</button>
         <button class="btn-sm btn-sm-primary" data-act="edit-all-save">すべて保存</button>
       </div>`
    : '';

  const hasImage = !!card.has_image;
  const layoutClass = hasImage ? 'detail-layout has-image' : 'detail-layout no-image';
  const imageColHtml = hasImage
    ? `<div class="detail-image-col">${imagePreviewHtml(card)}</div>`
    : '';

  return `
    <div class="detail-body">
      <div class="detail-head-actions">
        <button class="detail-delete-btn" data-act="delete-card" title="この名刺を削除">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="3 6 5 6 21 6"/>
            <path d="M19 6l-2 14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L5 6"/>
            <path d="M10 11v6M14 11v6"/>
            <path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>
          </svg>
        </button>
      </div>
      <div class="${layoutClass}">
        ${imageColHtml}
        <div class="detail-info-col">
          <div class="detail-head">
            <div class="detail-avatar" style="background:${color.bg};color:${color.fg}">${escapeHtml(initial)}</div>
            <div class="detail-id">
              ${headerInner}
              ${actions.length ? `<div class="quick-actions">${actions.join('')}</div>` : ''}
            </div>
          </div>

          <div class="detail-section">
            ${contactLabel}
            <div class="field-list ${state.editingAll ? 'is-editing-all' : ''}">
              ${fieldsHtml}
              ${registeredRow}
            </div>
            ${editActionBar}
          </div>

          <div class="detail-section">
            <div class="section-label">
              <span>メモ</span>
              ${hasNote && !state.editingNote ? `<button class="section-action" data-act="edit-note">編集</button>` : ''}
            </div>
            ${noteSection}
          </div>

          <div class="detail-section">
            <div class="section-label">
              <span>タグ</span>
              ${hasTags && !state.addingTag ? `<button class="section-action" data-act="add-tag">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
                追加
              </button>` : ''}
            </div>
            ${tagSection}
          </div>
        </div>
      </div>
    </div>
  `;
}

function imagePreviewHtml(card) {
  const cacheKey = encodeURIComponent(card.registered_date || '');
  const cid = encodeURIComponent(card.id);
  const hasBack = !!card.has_back_image;

  // 旧形式（{card_id}.jpg のみ）の名刺もあるため、
  // - hasBack=false のとき: side指定なし → サーバ側フォールバック(front → 旧)で旧形式も拾う
  // - hasBack=true のとき:  side=front を明示 → 確実に _front.jpg を取りに行く
  const frontSrc = hasBack
    ? `/api/cards/${cid}/image?side=front&v=${cacheKey}`
    : `/api/cards/${cid}/image?v=${cacheKey}`;
  const backSrc = `/api/cards/${cid}/image?side=back&v=${cacheKey}`;

  if (!hasBack) {
    // 表面のみ
    return `
      <div class="card-image-pair single">
        <div class="card-image-slot">
          <div class="card-image-frame">
            <img src="${frontSrc}" alt="名刺画像" data-act="open-lightbox" data-src="${frontSrc}">
          </div>
          <div class="card-image-caption">クリックで拡大</div>
        </div>
      </div>
    `;
  }

  // 表裏2枚
  return `
    <div class="card-image-pair dual">
      <div class="card-image-slot">
        <div class="card-image-side-label">表面</div>
        <div class="card-image-frame">
          <img src="${frontSrc}" alt="表面画像" data-act="open-lightbox" data-src="${frontSrc}">
        </div>
      </div>
      <div class="card-image-slot">
        <div class="card-image-side-label">
          <span>裏面</span>
          <button class="card-image-swap-btn" data-act="swap-images" title="表と裏を入れ替える">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="17 1 21 5 17 9"/>
              <path d="M3 11V9a4 4 0 0 1 4-4h14"/>
              <polyline points="7 23 3 19 7 15"/>
              <path d="M21 13v2a4 4 0 0 1-4 4H3"/>
            </svg>
            入れ替え
          </button>
        </div>
        <div class="card-image-frame">
          <img src="${backSrc}" alt="裏面画像" data-act="open-lightbox" data-src="${backSrc}">
        </div>
      </div>
    </div>
  `;
}

// ── Header (view/edit) ──
function headerViewHtml(card) {
  const overrides = card.overrides || {};
  const editableText = (field, value, className) => {
    const edited = overrides[field] !== undefined ? '<span class="field-edited-badge">編集済</span>' : '';
    const empty = !value ? 'empty-value' : '';
    const display = value || '(未入力)';
    return `<div class="${className} editable-inline ${empty}" data-field="${field}" title="クリックで編集">${escapeHtml(display)}${edited}</div>`;
  };

  // 役職の前に全角スラッシュ"/"は OCR の癖でよく出るので、表示時にも控えめに正規化
  const role = card.department_position || '';

  return `
    ${editableText('name', card.name || '(名前なし)', 'detail-name')}
    ${role || overrides.department_position !== undefined
      ? editableText('department_position', role, 'detail-role')
      : `<div class="detail-role editable-inline empty-value" data-field="department_position" title="クリックで編集">(部署・役職を追加)</div>`}
    ${editableText('company', card.company, 'detail-company')}
  `;
}

function headerEditHtml(card) {
  const v = (k) => (state.editAllDraft[k] !== undefined ? state.editAllDraft[k] : (card[k] || ''));
  return `
    <input type="text" class="detail-name-input" data-edit-all="name" value="${escapeHtml(v('name'))}" placeholder="氏名">
    <input type="text" class="detail-role-input" data-edit-all="department_position" value="${escapeHtml(v('department_position'))}" placeholder="部署・役職">
    <input type="text" class="detail-company-input" data-edit-all="company" value="${escapeHtml(v('company'))}" placeholder="会社名">
  `;
}

function fieldRowHtml(card, def, overrides) {
  const value = card[def.key] || '';
  const edited = overrides[def.key] !== undefined;

  if (state.editingAll) {
    const v = state.editAllDraft[def.key] !== undefined ? state.editAllDraft[def.key] : value;
    const inputHtml = def.type === 'textarea'
      ? `<textarea data-edit-all="${def.key}" placeholder="${escapeHtml(def.placeholder || '')}">${escapeHtml(v)}</textarea>`
      : `<input type="${def.type}" data-edit-all="${def.key}" value="${escapeHtml(v)}" placeholder="${escapeHtml(def.placeholder || '')}">`;
    return `
      <div class="field-row">
        <div class="field-label">${escapeHtml(def.label)}</div>
        <div class="field-value">${inputHtml}</div>
      </div>
    `;
  }

  if (state.editingField === def.key) {
    const inputHtml = def.type === 'textarea'
      ? `<textarea class="field-edit-input" data-field-input="${def.key}" placeholder="${escapeHtml(def.placeholder || '')}">${escapeHtml(value)}</textarea>`
      : `<input type="${def.type}" class="field-edit-input" data-field-input="${def.key}" value="${escapeHtml(value)}" placeholder="${escapeHtml(def.placeholder || '')}">`;
    return `
      <div class="field-row">
        <div class="field-label">${escapeHtml(def.label)}</div>
        <div class="field-value">
          <div class="field-inline-editor">
            ${inputHtml}
          </div>
        </div>
      </div>
    `;
  }

  // View mode
  let display;
  if (!value) {
    display = `<span class="empty-value"></span>`;
  } else if (def.type === 'email') {
    display = `<a href="mailto:${escapeHtml(value)}">${escapeHtml(value)}</a>`;
  } else if (def.type === 'tel') {
    const tel = value.replace(/[^\d+]/g, '');
    display = `<a href="tel:${tel}">${escapeHtml(value)}</a>`;
  } else if (def.type === 'url') {
    const url = value.startsWith('http') ? value : 'https://' + value;
    display = `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">${escapeHtml(value)}</a>`;
  } else {
    display = escapeHtml(value);
  }

  const editedBadge = edited ? '<span class="field-edited-badge">編集済</span>' : '';
  const resetBtn = edited ? `<button class="field-reset-btn" data-act="reset-field" data-field="${def.key}" title="元の値に戻す">×</button>` : '';

  return `
    <div class="field-row is-editable" data-field-click="${def.key}">
      <div class="field-label">${escapeHtml(def.label)}</div>
      <div class="field-value">
        <div class="field-value-wrap">
          <span class="field-value-text">${display}${editedBadge}</span>
          ${resetBtn}
          <svg class="field-edit-icon" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
        </div>
      </div>
    </div>
  `;
}

function quickBtn(type, label, href) {
  const icons = {
    mail: '<path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/>',
    phone: '<path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z"/>',
  };
  return `
    <a class="quick-btn" href="${escapeHtml(href)}">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${icons[type]}</svg>
      ${escapeHtml(label)}
    </a>
  `;
}

function noteDisplayHtml(note) {
  return `
    <div class="note-area" data-act="edit-note">
      <div class="note-display">${escapeHtml(note)}</div>
    </div>
  `;
}

function noteEditorHtml(note) {
  return `
    <div class="note-area">
      <textarea class="note-editor" id="noteEditor" placeholder="メモを入力...">${escapeHtml(note)}</textarea>
      <div class="note-toolbar">
        <button class="btn-sm" data-act="cancel-note">キャンセル</button>
        <button class="btn-sm btn-sm-primary" data-act="save-note">保存</button>
      </div>
    </div>
  `;
}

function tagsDisplayHtml(tags, showInput) {
  const items = tags.map(t => `
    <span class="tag">
      <span class="tag-prefix">#</span>${escapeHtml(t)}
      <button class="tag-delete" data-act="remove-tag" data-tag="${escapeHtml(t)}" aria-label="削除">
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </span>
  `).join('');

  const input = showInput ? `
    <span class="tag-input-wrap">
      <span class="tag-prefix">#</span>
      <input type="text" class="tag-input" id="tagInput" placeholder="タグ名" maxlength="40">
    </span>
  ` : '';

  return `<div class="tag-list">${items}${input}</div>`;
}

// ============================================================
// Handlers
// ============================================================
function attachDetailHandlers(container, card) {
  // data-act 要素
  container.querySelectorAll('[data-act]').forEach(el => {
    const act = el.dataset.act;
    el.addEventListener('click', async (e) => {
      e.stopPropagation();
      if (act === 'edit-note') startEditingNote(card);
      else if (act === 'cancel-note') cancelNote(card);
      else if (act === 'save-note') saveNote(card);
      else if (act === 'add-tag') startAddingTag(card);
      else if (act === 'remove-tag') removeTag(card, el.dataset.tag);
      else if (act === 'reset-field') resetField(card, el.dataset.field);
      else if (act === 'edit-all') startEditAll(card);
      else if (act === 'edit-all-cancel') cancelEditAll(card);
      else if (act === 'edit-all-save') saveEditAll(card);
      else if (act === 'open-lightbox') openImageLightbox(el.dataset.src);
      else if (act === 'swap-images') swapCardImages(card);
      else if (act === 'delete-card') deleteCard(card);
    });
  });

  // フィールドのクリック（インライン編集）
  container.querySelectorAll('[data-field-click]').forEach(el => {
    el.addEventListener('click', () => {
      if (state.editingAll) return;
      startEditField(card, el.dataset.fieldClick);
    });
  });

  // ヘッダーのインラインテキスト編集（名前・役職・会社名）
  container.querySelectorAll('.editable-inline').forEach(el => {
    el.addEventListener('click', (e) => {
      e.stopPropagation();
      if (state.editingAll) return;
      startEditHeaderField(card, el.dataset.field, el);
    });
  });

  // 単一フィールド編集中のinputフォーカス処理
  const fieldInput = container.querySelector('.field-edit-input');
  if (fieldInput) {
    fieldInput.focus();
    const len = fieldInput.value.length;
    fieldInput.setSelectionRange(len, len);

    fieldInput.addEventListener('keydown', async (e) => {
      if (e.key === 'Enter' && !e.shiftKey && fieldInput.tagName !== 'TEXTAREA') {
        e.preventDefault();
        await saveField(card, fieldInput.dataset.fieldInput, fieldInput.value);
      } else if (e.key === 'Escape') {
        state.editingField = null;
        renderDetailInto(els.detailPane, card);
        if (window.innerWidth <= 860) renderDetailInto(els.mobileBody, card);
      }
    });
    fieldInput.addEventListener('blur', async () => {
      setTimeout(async () => {
        if (state.editingField === fieldInput.dataset.fieldInput) {
          await saveField(card, fieldInput.dataset.fieldInput, fieldInput.value);
        }
      }, 150);
    });
  }

  // note editor
  const noteEditor = container.querySelector('#noteEditor');
  if (noteEditor) {
    noteEditor.focus();
    const len = noteEditor.value.length;
    noteEditor.setSelectionRange(len, len);
  }

  // tag input
  const tagInput = container.querySelector('#tagInput');
  if (tagInput) {
    tagInput.focus();
    tagInput.addEventListener('keydown', async (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        const v = tagInput.value.trim();
        if (v) await submitTag(card, v);
      } else if (e.key === 'Escape') {
        cancelAddingTag(card);
      }
    });
    tagInput.addEventListener('blur', async (e) => {
      setTimeout(async () => {
        const v = tagInput.value.trim();
        if (v) await submitTag(card, v);
        else cancelAddingTag(card);
      }, 120);
    });
  }

  // edit-all の各入力 - draftに保持
  container.querySelectorAll('[data-edit-all]').forEach(el => {
    el.addEventListener('input', () => {
      state.editAllDraft[el.dataset.editAll] = el.value;
    });
  });
}

// ============================================================
// Note
// ============================================================
function startEditingNote(card) {
  state.editingNote = true;
  rerender(card);
}
function cancelNote(card) {
  state.editingNote = false;
  rerender(card);
}
async function saveNote(card) {
  const editor = document.getElementById('noteEditor');
  if (!editor) return;
  const newNote = editor.value;
  try {
    await api(`/api/cards/${encodeURIComponent(card.id)}/note`, {
      method: 'PUT',
      body: JSON.stringify({ note: newNote }),
    });
    card.note = newNote;
    state.allCards.forEach(c => { if (c.id === card.id) c.note = newNote; });
    state.editingNote = false;
    renderList();
    rerender(card);
  } catch (err) {
    showToast(err.message, { type: 'error', title: 'メモの保存に失敗' });
  }
}

// ============================================================
// Tags
// ============================================================
function startAddingTag(card) { state.addingTag = true; rerender(card); }
function cancelAddingTag(card) { state.addingTag = false; rerender(card); }

async function submitTag(card, tagName) {
  const tag = tagName.replace(/^#/, '').trim();
  if (!tag) { cancelAddingTag(card); return; }
  try {
    await api(`/api/cards/${encodeURIComponent(card.id)}/tags`, {
      method: 'POST', body: JSON.stringify({ tag }),
    });
    if (!card.tags) card.tags = [];
    if (!card.tags.includes(tag)) card.tags.push(tag);
    state.addingTag = false;
    await loadTags();
    rerender(card);
  } catch (err) {
    showToast(err.message, { type: 'error', title: 'タグの追加に失敗' });
  }
}

async function removeTag(card, tag) {
  try {
    await api(`/api/cards/${encodeURIComponent(card.id)}/tags/${encodeURIComponent(tag)}`, { method: 'DELETE' });
    card.tags = (card.tags || []).filter(t => t !== tag);
    await loadTags();
    rerender(card);
  } catch (err) {
    showToast(err.message, { type: 'error', title: 'タグの削除に失敗' });
  }
}

// ============================================================
// Field edit (single)
// ============================================================
function startEditField(card, field) {
  state.editingField = field;
  rerender(card);
}

function startEditHeaderField(card, field, el) {
  // ヘッダーの3項目はインライン textarea 的な置き換え
  state.editingField = null;
  // 簡易: ヘッダーだけその場で editable にする
  const currentValue = card[field] || '';
  const newHtml = `<input type="text" class="detail-${field === 'name' ? 'name' : (field === 'company' ? 'company' : 'role')}-input" value="${escapeHtml(currentValue)}" data-header-edit="${field}">`;
  el.outerHTML = newHtml;
  const input = document.querySelector(`[data-header-edit="${field}"]`);
  if (input) {
    input.focus();
    input.select();

    const save = async () => {
      const newVal = input.value;
      await saveField(card, field, newVal);
    };
    const cancel = () => {
      rerender(card);
    };

    input.addEventListener('keydown', async (e) => {
      if (e.key === 'Enter') { e.preventDefault(); await save(); }
      else if (e.key === 'Escape') cancel();
    });
    input.addEventListener('blur', async () => {
      if (input.value !== currentValue) await save();
      else cancel();
    });
  }
}

async function saveField(card, field, rawValue) {
  const value = rawValue.trim();
  try {
    if (value === (card[field] || '') || (value === '' && card[field] === undefined)) {
      state.editingField = null;
      rerender(card);
      return;
    }
    await api(`/api/cards/${encodeURIComponent(card.id)}/field/${encodeURIComponent(field)}`, {
      method: 'PUT', body: JSON.stringify({ value }),
    });
    // 最新データを取得
    await loadCards();
    state.editingField = null;
  } catch (err) {
    showToast(err.message, { type: 'error', title: '保存に失敗' });
    state.editingField = null;
    rerender(card);
  }
}

async function resetField(card, field) {
  const ok = await showConfirm({
    title: '元の値に戻しますか？',
    message: 'この項目を、名刺から読み取った元の値に戻します。編集した内容は失われます。',
    okLabel: '戻す',
  });
  if (!ok) return;
  try {
    await api(`/api/cards/${encodeURIComponent(card.id)}/field/${encodeURIComponent(field)}`, {
      method: 'DELETE',
    });
    await loadCards();
  } catch (err) {
    showToast(err.message, { type: 'error', title: 'リセットに失敗' });
  }
}

// ============================================================
// Edit all
// ============================================================
function startEditAll(card) {
  state.editingAll = true;
  state.editingField = null;
  state.editAllDraft = {
    name: card.name || '',
    company: card.company || '',
    department_position: card.department_position || '',
    email: card.email || '',
    mobile: card.mobile || '',
    phone: card.phone || '',
    address: card.address || '',
    website: card.website || '',
  };
  rerender(card);
}

function cancelEditAll(card) {
  state.editingAll = false;
  state.editAllDraft = {};
  rerender(card);
}

async function saveEditAll(card) {
  const draft = state.editAllDraft;
  const changes = [];
  for (const [field, newValue] of Object.entries(draft)) {
    const currentValue = card[field] || '';
    if (newValue.trim() !== currentValue.trim()) {
      changes.push([field, newValue]);
    }
  }

  if (changes.length === 0) {
    cancelEditAll(card);
    return;
  }

  try {
    // 並列で保存
    await Promise.all(changes.map(([field, value]) =>
      api(`/api/cards/${encodeURIComponent(card.id)}/field/${encodeURIComponent(field)}`, {
        method: 'PUT', body: JSON.stringify({ value }),
      })
    ));
    state.editingAll = false;
    state.editAllDraft = {};
    await loadCards();
  } catch (err) {
    showToast(err.message, { type: 'error', title: '保存に失敗' });
  }
}

// ============================================================
// Helpers
// ============================================================
function rerender(card) {
  renderDetailInto(els.detailPane, card);
  if (window.innerWidth <= 860) renderDetailInto(els.mobileBody, card);
}

// ============================================================
// Events
// ============================================================
els.searchInput.addEventListener('input', debounce(e => {
  state.searchQuery = e.target.value.trim();
  loadCards();
}, 200));

els.filterRow.querySelectorAll('.chip[data-filter]').forEach(chip => {
  chip.addEventListener('click', () => {
    els.filterRow.querySelectorAll('.chip[data-filter]').forEach(c => c.classList.remove('chip-active'));
    chip.classList.add('chip-active');
    state.activeFilter = chip.dataset.filter;
    renderList();
  });
});

els.refreshBtn.addEventListener('click', async () => {
  els.refreshBtn.disabled = true;
  try {
    await api('/api/refresh', { method: 'POST' });
    await Promise.all([loadCards(), loadTags()]);
  } catch (err) {
    showToast(err.message, { type: 'error', title: '更新に失敗' });
  } finally {
    els.refreshBtn.disabled = false;
  }
});

els.switchBtn.addEventListener('click', async () => {
  const ok = await showConfirm({
    title: '会社を切り替えますか？',
    message: '現在の会社からログアウトし、別の会社を選択する画面に移動します。',
    okLabel: '切り替える',
  });
  if (!ok) return;
  try { await fetch('/api/logout', { method: 'POST', credentials: 'include' }); } catch {}
  location.href = '/';
});

els.mobileBack.addEventListener('click', () => {
  els.mobileSheet.classList.remove('is-open');
});

document.addEventListener('keydown', e => {
  const inInput = ['INPUT', 'TEXTAREA'].includes(document.activeElement?.tagName);
  if (!inInput && e.key === '/') {
    e.preventDefault();
    els.searchInput.focus();
  } else if (inInput && e.key === 'Escape') {
    if (document.activeElement === els.searchInput) {
      if (els.searchInput.value) {
        els.searchInput.value = '';
        state.searchQuery = '';
        loadCards();
      } else {
        els.searchInput.blur();
      }
    }
  } else if (e.key === 'Escape' && els.mobileSheet.classList.contains('is-open')) {
    els.mobileSheet.classList.remove('is-open');
  }
});

// ============================================================
// Selection bar / Bulk email
// ============================================================
function updateSelectionBar() {
  const count = state.selectedCardIds.size;
  els.selectionCount.textContent = count;
  if (count > 0) {
    els.selectionBar.classList.add('is-visible');
  } else {
    els.selectionBar.classList.remove('is-visible');
  }
  // v6.5: 一括選択ボタンの状態も更新
  updateBulkSelectBtn();
}

function clearSelection() {
  state.selectedCardIds.clear();
  updateSelectionBar();
  renderList();
}

els.clearSelectionBtn.addEventListener('click', clearSelection);

els.sendEmailBtn.addEventListener('click', () => {
  if (!state.tenant.can_send_email) {
    showToast('RESEND_API_KEY と tenants.yaml の from_email を設定してください。', { type: 'info', title: 'メール送信機能が未設定', duration: 5000 });
    return;
  }
  openEmailModal();
});

function openEmailModal() {
  const selectedCards = state.allCards.filter(c =>
    state.selectedCardIds.has(c.id) && (c.email || '').trim()
  );
  if (selectedCards.length === 0) {
    showToast('選択した名刺にメールアドレスが登録されていません。', { type: 'warning', title: '送信できません' });
    return;
  }
  // 送信元は毎回デフォルトに戻す
  state.currentSender = null;
  updateSenderBar();
  els.emailSubject.value = '';
  els.emailBody.value = '';
  renderEmailPreview(selectedCards);
  els.emailModalOverlay.style.display = 'flex';
  setTimeout(() => els.emailSubject.focus(), 100);
}

function getEffectiveSender() {
  if (state.currentSender) {
    return {
      local_part: state.currentSender.local_part,
      from_name: state.currentSender.from_name,
      email: state.currentSender.local_part + '@' + (state.tenant.from_domain || ''),
    };
  }
  return {
    local_part: state.tenant.from_local_part || '',
    from_name: state.tenant.from_name || '',
    email: state.tenant.from_email || '',
  };
}

function updateSenderBar() {
  const s = getEffectiveSender();
  els.senderBarName.textContent = s.from_name || '(表示名未設定)';
  els.senderBarEmail.textContent = s.email || '(未設定)';
}

function closeEmailModal() {
  els.emailModalOverlay.style.display = 'none';
}

function renderEmailPreview(cards) {
  const subject = els.emailSubject.value || '(件名なし)';
  const bodyTpl = els.emailBody.value || '';
  els.emailFooterInfo.textContent = `${cards.length}名に送信します`;

  els.emailRecipientList.innerHTML = cards.map(card => {
    const renderedSubject = renderTemplate(subject, card);
    const renderedBody = appendSignature(renderTemplate(bodyTpl, card), card);
    return `
      <div class="email-recipient-card">
        <div class="email-recipient-head">
          <div class="email-recipient-name">${escapeHtml(card.name || '(名前なし)')} <span style="color:var(--text-tertiary);font-weight:400;font-size:11px;">${escapeHtml(card.company)}</span></div>
          <div class="email-recipient-to">${escapeHtml(card.email)}</div>
        </div>
        <div class="email-recipient-subject">${escapeHtml(renderedSubject)}</div>
        <div class="email-recipient-body">${escapeHtml(renderedBody) || '<span style="color:var(--text-tertiary)">（本文なし）</span>'}</div>
      </div>
    `;
  }).join('');
}

function renderTemplate(template, card) {
  if (!template) return '';
  return template
    .replace(/\{\{会社\}\}/g, card.company || '')
    .replace(/\{\{名前\}\}/g, card.name || '')
    .replace(/\{\{役職\}\}/g, card.department_position || '')
    .replace(/\{\{メール\}\}/g, card.email || '')
    .replace(/\{\{携帯\}\}/g, card.mobile || '');
}

// 本文に署名を連結する (v6.7)。プリセットが選択されていて、かつ署名がある場合のみ。
// 署名内でも {{会社}} {{名前}} 等の変数が使えるようにする。
function appendSignature(body, card) {
  const sig = (state.currentSender && state.currentSender.signature) || '';
  if (!sig.trim()) return body;
  const renderedSig = renderTemplate(sig, card);
  const trimmedBody = (body || '').replace(/\s+$/, '');
  return trimmedBody + '\n\n' + renderedSig;
}

function onEmailEditorInput() {
  const cards = state.allCards.filter(c =>
    state.selectedCardIds.has(c.id) && (c.email || '').trim()
  );
  renderEmailPreview(cards);
}
els.emailSubject.addEventListener('input', onEmailEditorInput);
els.emailBody.addEventListener('input', onEmailEditorInput);

// 変数クリックで挿入
document.addEventListener('click', (e) => {
  const t = e.target.closest('code[data-var]');
  if (t) {
    const v = t.dataset.var;
    const ta = els.emailBody;
    const start = ta.selectionStart;
    const end = ta.selectionEnd;
    ta.value = ta.value.slice(0, start) + v + ta.value.slice(end);
    ta.focus();
    ta.setSelectionRange(start + v.length, start + v.length);
    ta.dispatchEvent(new Event('input'));
  }
});

els.cancelEmailBtn.addEventListener('click', closeEmailModal);
els.closeEmailModal.addEventListener('click', closeEmailModal);

els.sendEmailConfirmBtn.addEventListener('click', async () => {
  const cards = state.allCards.filter(c =>
    state.selectedCardIds.has(c.id) && (c.email || '').trim()
  );
  if (cards.length === 0) return;

  const subject = els.emailSubject.value.trim();
  const body = els.emailBody.value.trim();
  if (!subject) { showToast('件名を入力してください', { type: 'warning' }); return; }
  if (!body) { showToast('本文を入力してください', { type: 'warning' }); return; }

  const confirmOk = await showConfirm({
    title: `${cards.length} 名に送信しますか？`,
    message: `件名: ${subject}\n\n差し込みメールを送信します。送信後は取り消しできません。`,
    okLabel: '送信する',
  });
  if (!confirmOk) return;

  els.sendEmailConfirmBtn.disabled = true;
  els.cancelEmailBtn.disabled = true;
  els.sendEmailConfirmBtn.textContent = '送信中...';

  const items = cards.map(card => ({
    card_id: card.id,
    to_email: card.email,
    subject: renderTemplate(subject, card),
    body: appendSignature(renderTemplate(body, card), card),
  }));

  const sender = getEffectiveSender();
  const payload = {
    items,
    from_local_part: state.currentSender ? sender.local_part : null,
    from_name: state.currentSender ? sender.from_name : null,
  };

  try {
    const result = await api('/api/email/send-bulk', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    renderEmailResult(result);
  } catch (err) {
    showToast(err.message, { type: 'error', title: '送信失敗' });
    els.sendEmailConfirmBtn.disabled = false;
    els.cancelEmailBtn.disabled = false;
    els.sendEmailConfirmBtn.textContent = '送信';
  }
});

function renderEmailResult(result) {
  const summary = result.failed === 0
    ? { cls: 'is-success', title: `✓ ${result.sent}件すべて送信完了`, }
    : result.sent === 0
    ? { cls: 'is-failed', title: `✗ ${result.failed}件すべて送信失敗` }
    : { cls: 'is-mixed', title: `送信: ${result.sent}件 / 失敗: ${result.failed}件` };

  const body = document.getElementById('emailModalBody');
  body.style.display = 'block';
  body.style.gridTemplateColumns = 'none';
  body.style.padding = '20px 24px';
  body.style.overflowY = 'auto';
  body.innerHTML = `
    <div class="email-result-summary ${summary.cls}">
      <div class="email-result-summary-title">${escapeHtml(summary.title)}</div>
    </div>
    <div style="padding: 0 8px;">
      ${result.results.map(r => `
        <div class="email-result-item">
          ${r.ok
            ? '<svg class="email-result-icon-ok" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>'
            : '<svg class="email-result-icon-ng" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>'
          }
          <div style="flex:1;min-width:0">
            <div class="email-result-to">${escapeHtml(r.to)}</div>
            ${r.error ? `<div class="email-result-error">${escapeHtml(r.error)}</div>` : ''}
          </div>
        </div>
      `).join('')}
    </div>
  `;
  els.sendEmailConfirmBtn.style.display = 'none';
  els.cancelEmailBtn.textContent = '閉じる';
  els.cancelEmailBtn.disabled = false;

  els.cancelEmailBtn.onclick = () => {
    // 復元
    closeEmailModal();
    setTimeout(() => {
      body.style.display = '';
      body.style.gridTemplateColumns = '';
      body.style.padding = '';
      body.innerHTML = `
        <div class="email-editor-pane">
          <div class="email-sender-bar" id="emailSenderBar">
            <svg class="email-sender-bar-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="8" r="4"/>
              <path d="M4 21v-1a6 6 0 0 1 6-6h4a6 6 0 0 1 6 6v1"/>
            </svg>
            <div class="email-sender-bar-info">
              <div class="email-sender-bar-name" id="senderBarName">—</div>
              <div class="email-sender-bar-email" id="senderBarEmail">—</div>
            </div>
            <button class="email-sender-bar-change" id="openSenderPickerBtn">送信元を変更</button>
          </div>
          <div>
            <div class="email-field-label">件名</div>
            <input type="text" class="email-input" id="emailSubject" placeholder="例: 新製品のご案内">
          </div>
          <div>
            <div class="email-field-label">本文</div>
            <textarea class="email-textarea" id="emailBody" placeholder="{{会社}} {{名前}} 様"></textarea>
          </div>
          <div class="email-variables-hint">
            使える変数（クリックで挿入）:
            <code data-var="{{会社}}">{{会社}}</code>
            <code data-var="{{名前}}">{{名前}}</code>
            <code data-var="{{役職}}">{{役職}}</code>
            <code data-var="{{メール}}">{{メール}}</code>
          </div>
        </div>
        <div class="email-preview-pane">
          <div class="email-field-label" style="margin-bottom: 10px;">プレビュー（宛先ごと）</div>
          <div class="email-recipient-list" id="emailRecipientList"></div>
        </div>
      `;
      els.sendEmailConfirmBtn.style.display = '';
      els.sendEmailConfirmBtn.textContent = '送信';
      els.sendEmailConfirmBtn.disabled = false;
      els.cancelEmailBtn.textContent = 'キャンセル';
      els.cancelEmailBtn.onclick = closeEmailModal;
      // 再度els取得
      els.emailSubject = $('emailSubject');
      els.emailBody = $('emailBody');
      els.emailRecipientList = $('emailRecipientList');
      els.senderBarName = $('senderBarName');
      els.senderBarEmail = $('senderBarEmail');
      els.openSenderPickerBtn = $('openSenderPickerBtn');
      // 送信元をデフォルトに戻す + バー再バインド
      state.currentSender = null;
      updateSenderBar();
      els.openSenderPickerBtn.addEventListener('click', openSenderPicker);
      // 入力イベント再バインド
      els.emailSubject.addEventListener('input', onEmailEditorInput);
      els.emailBody.addEventListener('input', onEmailEditorInput);
    }, 400);
    clearSelection();
    loadCards();
  };
}

// ============================================================
// Duplicates
// ============================================================
els.dupesBtn.addEventListener('click', () => openDupesModal());
els.closeDupeModal.addEventListener('click', () => els.dupeModalOverlay.style.display = 'none');

async function openDupesModal() {
  els.dupeModalBody.innerHTML = '<div class="empty-text">読み込み中…</div>';
  els.dupeModalOverlay.style.display = 'flex';
  try {
    const data = await api('/api/duplicates');
    renderDupes(data.pairs);
  } catch (err) {
    els.dupeModalBody.innerHTML = `<div class="empty-text">エラー: ${escapeHtml(err.message)}</div>`;
  }
}

function renderDupes(pairs) {
  if (!pairs || pairs.length === 0) {
    els.dupeModalBody.innerHTML = `
      <div class="dupe-empty">
        <div class="dupe-empty-icon">
          <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
        </div>
        <div class="dupe-empty-text">重複候補はありません</div>
      </div>
    `;
    return;
  }

  const strengthLabel = { strong: '強', medium: '中', weak: '弱' };

  els.dupeModalBody.innerHTML = pairs.map((p, idx) => {
    const a = p.a, b = p.b;
    return `
      <div class="dupe-item" data-idx="${idx}">
        <div class="dupe-item-head">
          <div style="display:flex;align-items:center;gap:10px;">
            <div class="dupe-strength-badge strength-${escapeHtml(p.strength)}">
              ${strengthLabel[p.strength] || '?'}
            </div>
            <div class="dupe-reason">${escapeHtml(p.reason)}</div>
          </div>
        </div>
        <div class="dupe-pair">
          ${dupeCardSide(a)}
          <div class="dupe-vs">vs</div>
          ${dupeCardSide(b)}
        </div>
        <div class="dupe-item-actions">
          <button class="btn-sm" data-act="dismiss" data-a="${escapeHtml(a.id)}" data-b="${escapeHtml(b.id)}">別人として扱う</button>
          <button class="btn-sm btn-sm-primary" data-act="merge" data-idx="${idx}">マージ...</button>
        </div>
      </div>
    `;
  }).join('');

  // ハンドラ
  els.dupeModalBody.querySelectorAll('[data-act="dismiss"]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const ok = await showConfirm({
        title: '別人として扱いますか？',
        message: 'この2件を別人と判定し、次回から重複候補に表示しないようにします。',
        okLabel: '別人にする',
      });
      if (!ok) return;
      try {
        await api('/api/duplicates/dismiss', {
          method: 'POST',
          body: JSON.stringify({ card_id_a: btn.dataset.a, card_id_b: btn.dataset.b }),
        });
        openDupesModal(); // refresh
        loadDupesCount();
      } catch (err) {
        showToast(err.message, { type: 'error', title: 'エラー' });
      }
    });
  });
  els.dupeModalBody.querySelectorAll('[data-act="merge"]').forEach(btn => {
    btn.addEventListener('click', () => openMergeModal(pairs[parseInt(btn.dataset.idx)]));
  });
}

function dupeCardSide(card) {
  const color = colorForCompany(card.company_normalized);
  return `
    <div class="dupe-card-side">
      <div style="display:flex;gap:10px;margin-bottom:8px;">
        <div class="avatar" style="background:${color.bg};color:${color.fg};width:32px;height:32px;font-size:12px;">${escapeHtml(getInitial(card.name))}</div>
        <div>
          <div class="dupe-card-name">${escapeHtml(card.name || '(名前なし)')}</div>
          <div class="dupe-card-sub">${escapeHtml(card.company || '')}</div>
        </div>
      </div>
      ${card.department_position ? `<div class="dupe-card-field"><span class="dupe-card-field-label">役職</span><span class="dupe-card-field-value">${escapeHtml(card.department_position)}</span></div>` : ''}
      ${card.email ? `<div class="dupe-card-field"><span class="dupe-card-field-label">メール</span><span class="dupe-card-field-value">${escapeHtml(card.email)}</span></div>` : ''}
      ${card.mobile ? `<div class="dupe-card-field"><span class="dupe-card-field-label">携帯</span><span class="dupe-card-field-value">${escapeHtml(card.mobile)}</span></div>` : ''}
      ${card.phone ? `<div class="dupe-card-field"><span class="dupe-card-field-label">電話</span><span class="dupe-card-field-value">${escapeHtml(card.phone)}</span></div>` : ''}
      ${card.registered_date ? `<div class="dupe-card-field"><span class="dupe-card-field-label">登録日</span><span class="dupe-card-field-value">${escapeHtml(card.registered_date)}</span></div>` : ''}
    </div>
  `;
}

// ============================================================
// Merge modal
// ============================================================
els.closeMergeModal.addEventListener('click', () => els.mergeModalOverlay.style.display = 'none');
els.cancelMergeBtn.addEventListener('click', () => els.mergeModalOverlay.style.display = 'none');

function openMergeModal(pair) {
  state.currentMergePair = pair;
  // 新しい方のcard（registered_dateを比較）を kept とする初期値
  const aDate = new Date(pair.a.registered_date || 0);
  const bDate = new Date(pair.b.registered_date || 0);
  const keptSide = aDate >= bDate ? 'a' : 'b';
  const kept = pair[keptSide];
  const removed = pair[keptSide === 'a' ? 'b' : 'a'];

  state.currentMergePair.keptSide = keptSide;
  state.currentMergePair.keptCard = kept;
  state.currentMergePair.removedCard = removed;

  // デフォルト: keptの値を全部採用
  state.currentMergeChoices = {};
  const FIELDS = ['name', 'company', 'department_position', 'email', 'mobile', 'phone', 'address', 'website'];
  for (const f of FIELDS) {
    state.currentMergeChoices[f] = 'kept';
  }

  renderMergeModal();
  els.mergeModalOverlay.style.display = 'flex';
}

function renderMergeModal() {
  const kept = state.currentMergePair.keptCard;
  const removed = state.currentMergePair.removedCard;
  const FIELDS = [
    ['name', '氏名'],
    ['company', '会社名'],
    ['department_position', '部署・役職'],
    ['email', 'メール'],
    ['mobile', '携帯'],
    ['phone', '会社電話'],
    ['address', '住所'],
    ['website', 'サイト'],
  ];

  const rows = FIELDS.map(([f, label]) => {
    const valA = kept[f] || '';
    const valB = removed[f] || '';
    const choice = state.currentMergeChoices[f] || 'kept';
    return `
      <div class="merge-row">
        <div class="merge-label">${escapeHtml(label)}</div>
        <div class="merge-choice ${choice === 'kept' ? 'is-selected' : ''} ${!valA ? 'is-empty' : ''}" data-field="${escapeHtml(f)}" data-choice="kept">
          ${valA ? escapeHtml(valA) : '(空)'}
        </div>
        <div class="merge-choice ${choice === 'removed' ? 'is-selected' : ''} ${!valB ? 'is-empty' : ''}" data-field="${escapeHtml(f)}" data-choice="removed">
          ${valB ? escapeHtml(valB) : '(空)'}
        </div>
      </div>
    `;
  }).join('');

  els.mergeModalBody.innerHTML = `
    <p style="font-size:13px;color:var(--text-secondary);margin-bottom:14px;">
      採用する項目をクリックして選んでください。マージ後は2つの名刺が1つになり、選ばなかった側は非表示になります（後から復元可能）。
    </p>
    <div class="merge-compare">
      <div class="merge-header">
        <div></div>
        <div>採用する側 ${state.currentMergePair.keptSide === 'a' ? '(左)' : '(右)'}</div>
        <div>消える側</div>
      </div>
      ${rows}
    </div>
    <p style="font-size:12px;color:var(--text-tertiary);margin-top:10px;">
      メモとタグは両方マージされます。
    </p>
  `;

  // choice クリック
  els.mergeModalBody.querySelectorAll('.merge-choice').forEach(el => {
    el.addEventListener('click', () => {
      const f = el.dataset.field;
      const c = el.dataset.choice;
      state.currentMergeChoices[f] = c;
      renderMergeModal();
    });
  });
}

els.confirmMergeBtn.addEventListener('click', async () => {
  const kept = state.currentMergePair.keptCard;
  const removed = state.currentMergePair.removedCard;
  const ok = await showConfirm({
    title: 'マージを実行しますか？',
    message: `「${removed.name || '(名前なし)'}」を削除し、「${kept.name || '(名前なし)'}」に情報を統合します。`,
    okLabel: 'マージする',
  });
  if (!ok) return;

  els.confirmMergeBtn.disabled = true;
  try {
    await api('/api/duplicates/merge', {
      method: 'POST',
      body: JSON.stringify({
        kept_id: kept.id,
        removed_id: removed.id,
        field_choices: state.currentMergeChoices,
      }),
    });
    els.mergeModalOverlay.style.display = 'none';
    // リストと重複再取得
    await Promise.all([loadCards(), loadTags(), loadDupesCount()]);
    openDupesModal(); // refresh
  } catch (err) {
    showToast(err.message, { type: 'error', title: 'マージ失敗' });
  } finally {
    els.confirmMergeBtn.disabled = false;
  }
});

// ============================================================
// Sender Picker (v5.1)
// ============================================================
els.openSenderPickerBtn.addEventListener('click', openSenderPicker);
els.closeSenderModal.addEventListener('click', () => els.senderModalOverlay.style.display = 'none');

async function loadPresets() {
  try {
    const data = await api('/api/email/presets');
    state.presets = data.presets || [];
  } catch (err) {
    console.error('プリセット取得失敗', err);
    state.presets = [];
  }
}

async function openSenderPicker() {
  await loadPresets();
  renderSenderPicker({ showNewForm: false, editingId: null });
  els.senderModalOverlay.style.display = 'flex';
}

function renderSenderPicker(opts) {
  const { showNewForm, editingId } = opts;
  const domain = state.tenant.from_domain || '';

  // 現在選択中の判定
  const curLocal = state.currentSender ? state.currentSender.local_part : state.tenant.from_local_part;
  const curName = state.currentSender ? state.currentSender.from_name : state.tenant.from_name;
  const isDefaultSelected = !state.currentSender;

  // デフォルト項目
  const defaultItem = `
    <div class="sender-option ${isDefaultSelected ? 'is-selected' : ''}" data-default="1">
      <div class="sender-option-radio"></div>
      <div class="sender-option-body">
        <div class="sender-option-label">既定</div>
        <div class="sender-option-name">${escapeHtml(state.tenant.from_name || '(未設定)')}</div>
        <div class="sender-option-email">${escapeHtml(state.tenant.from_email || '(未設定)')}</div>
      </div>
    </div>
  `;

  // プリセット項目
  const presetItems = state.presets.map(p => {
    const selected = !isDefaultSelected && curLocal === p.local_part && curName === p.from_name;
    if (editingId === p.id) {
      return renderPresetEditForm(p, domain);
    }
    return `
      <div class="sender-option ${selected ? 'is-selected' : ''}" data-preset-id="${p.id}">
        <div class="sender-option-radio"></div>
        <div class="sender-option-body">
          <div class="sender-option-label">${escapeHtml(p.label)}</div>
          <div class="sender-option-name">${escapeHtml(p.from_name)}</div>
          <div class="sender-option-email">${escapeHtml(p.local_part)}@${escapeHtml(domain)}</div>
        </div>
        <div class="sender-option-actions">
          <button class="sender-option-action" data-edit-preset="${p.id}">編集</button>
          <button class="sender-option-action is-danger" data-delete-preset="${p.id}">削除</button>
        </div>
      </div>
    `;
  }).join('');

  // 新規フォーム or 追加ボタン
  const newPart = showNewForm
    ? renderPresetNewForm(domain)
    : `<button class="sender-new-btn" id="showNewPresetBtn">
         <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
         新しいプリセットを追加
       </button>`;

  els.senderModalBody.innerHTML = `
    <p style="font-size:13px;color:var(--text-secondary);margin-bottom:14px;line-height:1.6;">
      使用したい送信元を選んでください。プリセットは <strong>DBに保存</strong>され、どの端末からでも同じものが使えます。
    </p>
    <div class="sender-picker-list">
      ${defaultItem}
      ${presetItems}
    </div>
    ${newPart}
  `;

  // 既定選択
  els.senderModalBody.querySelector('[data-default]')?.addEventListener('click', () => {
    state.currentSender = null;
    updateSenderBar();
    onEmailEditorInput();
    els.senderModalOverlay.style.display = 'none';
  });

  // プリセット選択
  els.senderModalBody.querySelectorAll('[data-preset-id]').forEach(el => {
    el.addEventListener('click', (e) => {
      // 編集・削除ボタンのクリックは通過
      if (e.target.closest('[data-edit-preset]') || e.target.closest('[data-delete-preset]')) return;
      const pid = parseInt(el.dataset.presetId);
      const p = state.presets.find(x => x.id === pid);
      if (!p) return;
      state.currentSender = {
        local_part: p.local_part,
        from_name: p.from_name,
        signature: p.signature || '',
      };
      updateSenderBar();
      onEmailEditorInput();
      els.senderModalOverlay.style.display = 'none';
    });
  });

  // 編集ボタン
  els.senderModalBody.querySelectorAll('[data-edit-preset]').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      renderSenderPicker({ showNewForm: false, editingId: parseInt(btn.dataset.editPreset) });
    });
  });

  // 削除ボタン
  els.senderModalBody.querySelectorAll('[data-delete-preset]').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const pid = parseInt(btn.dataset.deletePreset);
      const p = state.presets.find(x => x.id === pid);
      const ok = await showConfirm({
        title: 'プリセットを削除しますか？',
        message: `「${p.label}」を削除します。`,
        okLabel: '削除する',
        danger: true,
      });
      if (!ok) return;
      try {
        await api(`/api/email/presets/${pid}`, { method: 'DELETE' });
        // 現在選択中のプリセットだったら既定に戻す
        if (state.currentSender && state.currentSender.local_part === p.local_part && state.currentSender.from_name === p.from_name) {
          state.currentSender = null;
          updateSenderBar();
        }
        await loadPresets();
        renderSenderPicker({ showNewForm: false, editingId: null });
      } catch (err) {
        showToast(err.message, { type: 'error', title: '削除失敗' });
      }
    });
  });

  // 追加ボタン
  document.getElementById('showNewPresetBtn')?.addEventListener('click', () => {
    renderSenderPicker({ showNewForm: true, editingId: null });
    setTimeout(() => document.getElementById('presetNewLabel')?.focus(), 50);
  });

  // 新規フォームのボタン
  document.getElementById('presetNewCancel')?.addEventListener('click', () => {
    renderSenderPicker({ showNewForm: false, editingId: null });
  });
  document.getElementById('presetNewSave')?.addEventListener('click', async () => {
    const label = document.getElementById('presetNewLabel').value.trim();
    const localPart = document.getElementById('presetNewLocal').value.trim();
    const fromName = document.getElementById('presetNewName').value.trim();
    const signature = (document.getElementById('presetNewSignature')?.value || '').replace(/\s+$/, '');
    if (!label) { showToast('プリセット名を入力してください', { type: 'warning' }); return; }
    if (!localPart) { showToast('@の前の部分を入力してください', { type: 'warning' }); return; }
    if (!fromName) { showToast('送信者の表示名を入力してください', { type: 'warning' }); return; }
    if (signature.length > 2000) { showToast('署名が長すぎます (2000文字以下)', { type: 'warning' }); return; }
    try {
      await api('/api/email/presets', {
        method: 'POST',
        body: JSON.stringify({ label, local_part: localPart, from_name: fromName, signature }),
      });
      await loadPresets();
      renderSenderPicker({ showNewForm: false, editingId: null });
    } catch (err) {
      showToast(err.message, { type: 'error', title: '保存失敗' });
    }
  });

  // 編集フォームのボタン
  document.getElementById('presetEditCancel')?.addEventListener('click', () => {
    renderSenderPicker({ showNewForm: false, editingId: null });
  });
  document.getElementById('presetEditSave')?.addEventListener('click', async () => {
    const pid = parseInt(document.getElementById('presetEditSave').dataset.presetId);
    const label = document.getElementById('presetEditLabel').value.trim();
    const localPart = document.getElementById('presetEditLocal').value.trim();
    const fromName = document.getElementById('presetEditName').value.trim();
    const signature = (document.getElementById('presetEditSignature')?.value || '').replace(/\s+$/, '');
    if (!label || !localPart || !fromName) { showToast('すべての項目を入力してください', { type: 'warning' }); return; }
    if (signature.length > 2000) { showToast('署名が長すぎます (2000文字以下)', { type: 'warning' }); return; }
    try {
      await api(`/api/email/presets/${pid}`, {
        method: 'PUT',
        body: JSON.stringify({ label, local_part: localPart, from_name: fromName, signature }),
      });
      await loadPresets();
      renderSenderPicker({ showNewForm: false, editingId: null });
    } catch (err) {
      showToast(err.message, { type: 'error', title: '保存失敗' });
    }
  });
}

function renderPresetNewForm(domain) {
  return `
    <div class="sender-form">
      <div class="sender-form-row">
        <div class="sender-form-label">プリセット名</div>
        <input type="text" class="email-input" id="presetNewLabel" placeholder="例: 営業用、イベント用">
      </div>
      <div class="sender-form-row">
        <div class="sender-form-label">送信者の表示名</div>
        <input type="text" class="email-input" id="presetNewName" placeholder="例: 和宏電機 営業部">
      </div>
      <div class="sender-form-row">
        <div class="sender-form-label">メールアドレス</div>
        <div class="sender-form-localgroup">
          <input type="text" id="presetNewLocal" placeholder="info">
          <span class="sender-form-domain">@${escapeHtml(domain)}</span>
        </div>
      </div>
      <div class="sender-form-row">
        <div class="sender-form-label">署名（本文末尾に自動で追加されます）</div>
        <textarea class="email-input sender-form-signature" id="presetNewSignature" rows="6" placeholder="例:&#10;─────────────&#10;株式会社 和宏電機&#10;営業部 日野 直樹&#10;TEL: 06-XXXX-XXXX&#10;hino@wakou-dk.com&#10;─────────────"></textarea>
      </div>
      <div class="sender-form-actions">
        <button class="btn-sm" id="presetNewCancel">キャンセル</button>
        <button class="btn-sm btn-sm-primary" id="presetNewSave">保存</button>
      </div>
    </div>
  `;
}

function renderPresetEditForm(p, domain) {
  return `
    <div class="sender-form">
      <div style="font-size:11px;color:var(--text-tertiary);font-weight:600;letter-spacing:0.04em;text-transform:uppercase;">プリセットを編集</div>
      <div class="sender-form-row">
        <div class="sender-form-label">プリセット名</div>
        <input type="text" class="email-input" id="presetEditLabel" value="${escapeHtml(p.label)}">
      </div>
      <div class="sender-form-row">
        <div class="sender-form-label">送信者の表示名</div>
        <input type="text" class="email-input" id="presetEditName" value="${escapeHtml(p.from_name)}">
      </div>
      <div class="sender-form-row">
        <div class="sender-form-label">メールアドレス</div>
        <div class="sender-form-localgroup">
          <input type="text" id="presetEditLocal" value="${escapeHtml(p.local_part)}">
          <span class="sender-form-domain">@${escapeHtml(domain)}</span>
        </div>
      </div>
      <div class="sender-form-row">
        <div class="sender-form-label">署名（本文末尾に自動で追加されます）</div>
        <textarea class="email-input sender-form-signature" id="presetEditSignature" rows="6" placeholder="署名を入力...">${escapeHtml(p.signature || '')}</textarea>
      </div>
      <div class="sender-form-actions">
        <button class="btn-sm" id="presetEditCancel">キャンセル</button>
        <button class="btn-sm btn-sm-primary" id="presetEditSave" data-preset-id="${p.id}">保存</button>
      </div>
    </div>
  `;
}

// ============================================================
// ============================================================
// Swap front/back images (v6.7)
// ============================================================
async function swapCardImages(card) {
  if (!card || !card.id) return;
  try {
    await api(`/api/cards/${encodeURIComponent(card.id)}/swap-images`, {
      method: 'POST',
    });
    // キャッシュを無効化するため registered_date を一時的にずらす（再描画時に画像が差し替わる）
    card.registered_date = (card.registered_date || '') + '#' + Date.now();
    renderDetailInto(els.detailPane, card);
    if (window.innerWidth <= 860) renderDetailInto(els.mobileBody, card);
    showToast('表面と裏面を入れ替えました', { type: 'success', duration: 2000 });
  } catch (e) {
    showToast(`入れ替えに失敗しました: ${e.message}`, { type: 'error', duration: 4000 });
  }
}

// ============================================================
// Image Lightbox (v6)
// ============================================================
function openImageLightbox(src) {
  if (!src) return;
  closeImageLightbox(); // 既存があれば閉じる
  const overlay = document.createElement('div');
  overlay.className = 'image-lightbox';
  overlay.id = 'imageLightbox';
  overlay.innerHTML = `
    <button class="image-lightbox-close" aria-label="閉じる">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <line x1="18" y1="6" x2="6" y2="18"/>
        <line x1="6" y1="6" x2="18" y2="18"/>
      </svg>
    </button>
    <img src="${src}" alt="名刺画像">
  `;
  overlay.addEventListener('click', (e) => {
    if (e.target.tagName !== 'IMG') closeImageLightbox();
  });
  document.body.appendChild(overlay);
  document.addEventListener('keydown', lightboxEscHandler);
}

function closeImageLightbox() {
  const ov = document.getElementById('imageLightbox');
  if (ov) ov.remove();
  document.removeEventListener('keydown', lightboxEscHandler);
}

function lightboxEscHandler(e) {
  if (e.key === 'Escape') closeImageLightbox();
}

// ============================================================
// Delete cards (v6.4)
// ============================================================
function hideDetail() {
  state.selectedId = null;
  if (els.detailPane) {
    els.detailPane.innerHTML = '<div class="detail-empty">名刺を選択してください</div>';
  }
  if (els.mobileSheet) {
    els.mobileSheet.classList.remove('is-open');
  }
  els.listPane.querySelectorAll('.row').forEach(el => el.classList.remove('row-active'));
}

async function deleteCard(card) {
  const displayName = card.name || '(名前なし)';
  const displayCompany = card.company || '(会社名なし)';
  const ok = await showConfirm({
    title: '名刺を削除しますか？',
    message: `${displayCompany}\n${displayName}\n\nGoogleスプレッドシート、メモ、タグ、画像もすべて削除されます。\nこの操作は元に戻せません。`,
    okLabel: '削除する',
    cancelLabel: 'キャンセル',
    danger: true,
  });
  if (!ok) return;

  try {
    const r = await api('/api/cards/delete', {
      method: 'POST',
      body: JSON.stringify({ card_ids: [card.id] }),
    });
    await Promise.all([loadCards(), loadTags(), loadDupesCount()]);
    hideDetail();
    showToast(`スプシ ${r.sheet_removed} 件 / 画像 ${r.images_removed} 件 を削除しました`, {
      type: 'success',
      title: '削除完了',
    });
  } catch (err) {
    showToast('削除失敗: ' + err.message, { type: 'error', title: 'エラー' });
  }
}

async function deleteSelectedCards() {
  const ids = Array.from(state.selectedCardIds);
  if (ids.length === 0) return;

  const ok = await showConfirm({
    title: `${ids.length} 件の名刺を削除しますか？`,
    message: `選択した ${ids.length} 件が完全に削除されます。\nGoogleスプレッドシート、メモ、タグ、画像もすべて削除されます。\nこの操作は元に戻せません。`,
    okLabel: '削除する',
    cancelLabel: 'キャンセル',
    danger: true,
  });
  if (!ok) return;

  const btn = els.bulkDeleteBtn;
  if (btn) {
    btn.disabled = true;
    btn.textContent = '削除中...';
  }

  try {
    const r = await api('/api/cards/delete', {
      method: 'POST',
      body: JSON.stringify({ card_ids: ids }),
    });
    clearSelection();
    await Promise.all([loadCards(), loadTags(), loadDupesCount()]);
    hideDetail();
    showToast(`スプシ ${r.sheet_removed} 件 / 画像 ${r.images_removed} 件 を削除しました`, {
      type: 'success',
      title: '削除完了',
    });
  } catch (err) {
    showToast('削除失敗: ' + err.message, { type: 'error', title: 'エラー' });
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = `
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <polyline points="3 6 5 6 21 6"/>
          <path d="M19 6l-2 14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L5 6"/>
          <path d="M10 11v6M14 11v6"/>
          <path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>
        </svg>
        削除
      `;
    }
  }
}

// 一括削除ボタンのハンドラ
if (els.bulkDeleteBtn) {
  els.bulkDeleteBtn.addEventListener('click', deleteSelectedCards);
}

// ============================================================
// Bulk select (v6.5): 表示中の全件を選択/解除
// ============================================================
function updateBulkSelectBtn() {
  if (!els.bulkSelectBtn || !els.bulkSelectLabel) return;
  const visible = state.visibleCardIds || [];
  const count = visible.length;
  // 表示中のカードが全て選択済みかどうか
  const allSelected = count > 0 && visible.every(id => state.selectedCardIds.has(id));

  if (allSelected) {
    els.bulkSelectLabel.textContent = `選択解除 (${count}件)`;
    els.bulkSelectBtn.classList.add('is-all-selected');
  } else {
    els.bulkSelectLabel.textContent = `表示中の${count}件を一括選択`;
    els.bulkSelectBtn.classList.remove('is-all-selected');
  }

  // 表示中0件ならボタン自体を隠す
  els.bulkSelectBtn.style.display = count === 0 ? 'none' : '';
}

function toggleBulkSelect() {
  const visible = state.visibleCardIds || [];
  if (visible.length === 0) return;
  const allSelected = visible.every(id => state.selectedCardIds.has(id));

  if (allSelected) {
    // 全部解除（ただし表示外のは残す: 一般的な期待値）
    visible.forEach(id => state.selectedCardIds.delete(id));
  } else {
    // 全部選択
    visible.forEach(id => state.selectedCardIds.add(id));
  }

  renderList();
  updateSelectionBar();
}

if (els.bulkSelectBtn) {
  els.bulkSelectBtn.addEventListener('click', toggleBulkSelect);
}

// ============================================================
// Custom Toast & Confirm (v6.6)
// ============================================================
const TOAST_ICONS = {
  success: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>',
  error: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>',
  warning: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
  info: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
};

/**
 * トースト通知を表示する
 * @param {string} message 本文 (改行対応)
 * @param {object} options { type: 'success'|'error'|'warning'|'info', title: string, duration: ms }
 */
function showToast(message, options = {}) {
  const type = options.type || 'info';
  const title = options.title || '';
  const duration = options.duration !== undefined ? options.duration : 3500;

  const container = document.getElementById('toastContainer');
  if (!container) return;

  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.innerHTML = `
    <div class="toast-icon">${TOAST_ICONS[type] || TOAST_ICONS.info}</div>
    <div class="toast-body">
      ${title ? `<div class="toast-title">${escapeHtml(title)}</div>` : ''}
      <div class="toast-message">${escapeHtml(message)}</div>
    </div>
    <button class="toast-close" aria-label="閉じる">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
        <line x1="18" y1="6" x2="6" y2="18"/>
        <line x1="6" y1="6" x2="18" y2="18"/>
      </svg>
    </button>
  `;

  const remove = () => {
    toast.classList.add('is-leaving');
    setTimeout(() => toast.remove(), 250);
  };

  toast.querySelector('.toast-close').addEventListener('click', remove);
  container.appendChild(toast);

  if (duration > 0) {
    setTimeout(remove, duration);
  }

  return toast;
}

/**
 * カスタム確認ダイアログ
 * @param {string|object} arg1 メッセージ文字列、または詳細オプション
 * @returns {Promise<boolean>} OKなら true、キャンセルなら false
 */
function showConfirm(arg1) {
  let title = '確認';
  let message = '';
  let okLabel = 'OK';
  let cancelLabel = 'キャンセル';
  let danger = false;
  let warning = false;

  if (typeof arg1 === 'string') {
    message = arg1;
  } else if (arg1 && typeof arg1 === 'object') {
    title = arg1.title !== undefined ? arg1.title : '確認';
    message = arg1.message || '';
    okLabel = arg1.okLabel || 'OK';
    cancelLabel = arg1.cancelLabel || 'キャンセル';
    danger = !!arg1.danger;
    warning = !!arg1.warning;
  }

  return new Promise((resolve) => {
    const overlay = document.getElementById('confirmModalOverlay');
    const iconEl = document.getElementById('confirmModalIcon');
    const titleEl = document.getElementById('confirmModalTitle');
    const msgEl = document.getElementById('confirmModalMessage');
    const okBtn = document.getElementById('confirmModalOk');
    const cancelBtn = document.getElementById('confirmModalCancel');

    // アイコン
    iconEl.className = 'confirm-modal-icon';
    if (danger) {
      iconEl.classList.add('is-danger');
      iconEl.innerHTML = '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-2 14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/></svg>';
    } else if (warning) {
      iconEl.classList.add('is-warning');
      iconEl.innerHTML = '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>';
    } else {
      iconEl.innerHTML = '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>';
    }

    titleEl.textContent = title;
    msgEl.textContent = message;
    okBtn.textContent = okLabel;
    cancelBtn.textContent = cancelLabel;

    okBtn.className = 'confirm-modal-btn confirm-modal-btn-ok';
    if (danger) okBtn.classList.add('is-danger');

    overlay.style.display = 'flex';

    const cleanup = () => {
      overlay.style.display = 'none';
      okBtn.removeEventListener('click', onOk);
      cancelBtn.removeEventListener('click', onCancel);
      overlay.removeEventListener('click', onOverlayClick);
      document.removeEventListener('keydown', onKey);
    };
    const onOk = () => { cleanup(); resolve(true); };
    const onCancel = () => { cleanup(); resolve(false); };
    const onOverlayClick = (e) => { if (e.target === overlay) onCancel(); };
    const onKey = (e) => {
      if (e.key === 'Escape') onCancel();
      if (e.key === 'Enter') onOk();
    };

    okBtn.addEventListener('click', onOk);
    cancelBtn.addEventListener('click', onCancel);
    overlay.addEventListener('click', onOverlayClick);
    document.addEventListener('keydown', onKey);
    // OKボタンにフォーカス
    setTimeout(() => okBtn.focus(), 50);
  });
}

init();
