// ============================================================
// リスト — テレアポ業者と共有するリスト
//   取り込み（Excel/CSV）→ 判定（かけてよい・かけない・保留）→ AIで調べる（公式サイト・マッチ度）
//   → 電話の記録 → アポ獲得で案件に → 訪問評価がリストに戻る
// sales.js の S / api / esc / showToast などを使う
// ============================================================

const LS = { lists: [], vendors: [], ai: false, cur: null, rows: [], stats: null, sel: null, detail: null, checked: new Set(),
  filter: 'all', more: '', doneF: 'all', q: '', sort: 'row', poll: null, showInd: false };
// 状況（業者のリストの塗り分けと同じ: 赤=かけない 黄=アポ 青=資料送付・再架電 緑=保留）。文字が赤い行＝完了
const LSTATUS = [['new', '未着手'], ['calling', '架電中'], ['follow', '資料送付・再架電'], ['appo', 'アポ'], ['hold', '保留'], ['ng', 'かけない']];
const LSTATUS_LABEL = Object.fromEntries(LSTATUS);
const VERDICT_LABEL = { ok: 'かけてよい', ng: 'かけない', hold: '保留' };
const RESULT_TONE = { 'アポ獲得': 'good', '資料送付': 'mid', '再コール': 'mid', '担当者不在': 'quiet', '見込みなし': 'bad', 'その他': 'quiet' };

async function renderLists(openId) {
  const pane = $('viewPane');
  clearInterval(LS.poll);
  if (openId || LS.cur) return openList(openId || LS.cur.id);
  LS.loadingId = null;
  pane.innerHTML = '<div class="empty-text">読み込み中…</div>';
  try {
    const d = await api('/api/sales/lists');
    if (S.view !== 'lists' || LS.cur || LS.loadingId) return;   // 待っている間にほかの画面へ移った
    LS.lists = d.lists; LS.vendors = d.vendors; LS.ai = d.ai;
  } catch (e) { if (S.view === 'lists') pane.innerHTML = `<div class="empty-text">${esc(e.message)}</div>`; return; }
  drawListIndex();
}

function pct(n, d) { return d ? Math.round(n / d * 100) : 0; }

function drawListIndex() {
  const pane = $('viewPane');
  const live = LS.lists.filter(l => !l.archived), old = LS.lists.filter(l => l.archived);
  const card = l => {
    const s = l.stats;
    const grades = Object.entries(s.grades || {}).filter(([, n]) => n).map(([g, n]) => `<span class="lg-grade g-${g}">${g}${n}</span>`).join('');
    return `<button class="lst-card ${l.archived ? 'is-archived' : ''}" data-list="${l.id}">
      <div class="lst-card-top"><span class="lst-name">${esc(l.name)}</span>
        ${l.vendor ? `<span class="lst-share">共有: ${esc(l.vendor)}</span>` : '<span class="lst-share is-private">自社だけ</span>'}</div>
      <div class="lst-meta">${esc(fmtDate(l.created_at))} 取り込み${l.uploaded_by ? `（${esc(l.uploaded_by)}がアップロード）` : ''}</div>
      <div class="lst-bar" title="${esc(LSTATUS.map(([k, l]) => `${l} ${(s.status || {})[k] || 0}`).join('・'))}">
        ${LSTATUS.map(([k]) => `<i class="b-${k}" style="flex:${(s.status || {})[k] || 0}"></i>`).join('')}</div>
      <div class="lst-nums">
        <span><b>${s.total}</b>社</span><span>架電 <b>${s.called}</b></span>
        <span class="is-appo">アポ <b>${s.appo}</b>${s.appo_rate != null ? `<em>${s.appo_rate}%</em>` : ''}</span>
        <span>完了 <b>${s.done || 0}</b></span>${s.researched ? `<span>AI調査 <b>${s.researched}</b></span>` : ''}${grades ? `<span class="lst-grades">${grades}</span>` : ''}
      </div></button>`;
  };
  pane.innerHTML = `
    <div class="lst-wrap">
      <div class="lst-head">
        <div><h2 class="lst-title">リスト</h2>
          <div class="lst-sub">テレアポ業者と共有するリストです。判定とAIの調査で「かける会社」を絞り、アポが取れたら自動で案件になります。</div></div>
        <div class="lst-actions">
          <button class="btn-sm" id="lsIcp">狙う会社の条件</button>
          <button class="btn-sm btn-sm-primary" id="lsUpload">＋ リストを取り込む</button>
        </div>
      </div>
      ${live.length ? `<div class="lst-grid">${live.map(card).join('')}</div>` : `<div class="lst-empty">
        <div class="lst-empty-t">まだリストがありません</div>
        <div>テレアポ業者からもらった Excel や CSV を取り込むと、会社ごとに判定・AIの調査・電話の記録ができます。<br>
        業者の入口（テレアポ報告）からも、業者がリストをアップロードできます。</div></div>`}
      ${old.length ? `<details class="lst-old"><summary>しまったリスト（${old.length}）</summary><div class="lst-grid">${old.map(card).join('')}</div></details>` : ''}
    </div>`;
  pane.querySelectorAll('[data-list]').forEach(b => b.onclick = () => openList(Number(b.dataset.list)));
  $('lsUpload').onclick = openUpload;
  $('lsIcp').onclick = openIcp;
}

// ── 取り込み ──
function openUpload() {
  const body = $('listsModalBody');
  $('listsModalTitle').textContent = 'リストを取り込む';
  body.innerHTML = `
    <div class="lm-form">
      <label class="lm-drop" id="lmDrop"><input type="file" id="lmFile" accept=".xlsx,.xlsm,.csv,.tsv,.txt">
        <span class="lm-drop-t">Excel（.xlsx）か CSV を選ぶ</span><span class="lm-drop-s" id="lmFileName">ここにドラッグしても取り込めます</span></label>
      <label class="lm-label">リストの名前</label>
      <input class="email-input compact" id="lmName" maxlength="80" placeholder="例: 第6弾（配電盤メーカー）">
      <label class="lm-label">テレアポ業者と共有</label>
      <select class="email-input compact" id="lmVendor"><option value="">共有しない（自社だけ）</option>
        ${LS.vendors.map(v => `<option value="${v.id}">${esc(v.name)}</option>`).join('')}</select>
      <label class="compose-check"><input type="checkbox" id="lmMemo" checked> 備考欄を「これまでの記録」として取り込む</label>
      <div class="lm-note">見出し（会社名・電話・住所・業種・URL・備考）がなくても、中身から列を見分けます。シートが複数あれば全部読みます。<br>
        行の色も読みます：<span class="lm-sw sw-yellow"></span>黄＝アポ <span class="lm-sw sw-blue"></span>青＝資料送付・再架電 <span class="lm-sw sw-red"></span>赤＝かけない <span class="lm-sw sw-green"></span>緑＝保留、<b class="lm-red">赤い文字</b>＝完了。</div>
    </div>`;
  $('listsModalFoot').innerHTML = `<button class="btn-sm" data-close="listsOverlay">やめる</button><button class="btn-sm btn-sm-primary" id="lmGo">取り込む</button>`;
  bindListsModal();
  const f = $('lmFile');
  const setName = () => { const x = f.files[0]; $('lmFileName').textContent = x ? x.name : 'ここにドラッグしても取り込めます';
    if (x && !$('lmName').dataset.typed) $('lmName').value = x.name.replace(/\.[^.]+$/, '').slice(0, 80); };
  $('lmName').oninput = () => { $('lmName').dataset.typed = '1'; };
  f.onchange = setName;
  const drop = $('lmDrop');
  drop.ondragover = e => { e.preventDefault(); drop.classList.add('is-over'); };
  drop.ondragleave = () => drop.classList.remove('is-over');
  drop.ondrop = e => { e.preventDefault(); drop.classList.remove('is-over'); if (e.dataTransfer.files.length) { f.files = e.dataTransfer.files; setName(); } };
  $('lmGo').onclick = async () => {
    if (!f.files[0]) { showToast('ファイルを選んでください', { type: 'warning' }); return; }
    const fd = new FormData();
    fd.append('file', f.files[0]); fd.append('name', $('lmName').value); fd.append('import_memo', $('lmMemo').checked ? 'true' : 'false');
    if ($('lmVendor').value) fd.append('vendor_id', $('lmVendor').value);
    $('lmGo').disabled = true; $('lmGo').textContent = '取り込み中…';
    try {
      const r = await fetch('/api/sales/lists', { method: 'POST', body: fd, credentials: 'include' });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
      $('listsOverlay').style.display = 'none';
      LS.cur = null; await openList(j.id);
      showImportResult(j);
    } catch (e) { showToast(e.message, { type: 'error', duration: 5000 }); $('lmGo').disabled = false; $('lmGo').textContent = '取り込む'; }
  };
  $('listsOverlay').style.display = 'flex';
}

// 取り込んだ結果（色・赤い文字・備考をどう読んだか）
function showImportResult(j) {
  const c = j.colors || {};
  const line = (cls, label, n, note) => n ? `<div class="ir-row"><span class="st-pill st-${cls}">${label}</span><b>${n}社</b><span>${note}</span></div>` : '';
  $('listsModalTitle').textContent = `${j.rows}社を取り込みました`;
  $('listsModalBody').innerHTML = `<div class="lm-form">
    <div class="lm-meta">新しい会社 ${j.added}・ほかのリストにもある会社 ${j.merged}（同じ会社は1社にまとめ、備考はつなげました）</div>
    ${Object.keys(c).length || j.done ? `<div class="lm-label">Excel の色から読み取ったこと</div><div class="ir-list">
      ${line('appo', 'アポ', c.appo, '黄色の行')}${line('follow', '資料送付・再架電', c.follow, '青の行')}
      ${line('ng', 'かけない', c.ng, '赤の行（架電不要）')}${line('hold', '保留', c.hold, '緑の行')}
      ${j.done ? `<div class="ir-row"><span class="st-pill st-done">完了</span><b>${j.done}社</b><span>文字が赤い行</span></div>` : ''}</div>
      <div class="lm-note">すでにこちらで判定している会社は、判定を変えていません。違っていれば会社を開いて直せます。</div>`
      : '<div class="lm-note">色の付いた行はありませんでした。</div>'}
    <div class="lm-note">備考欄（とほかの列のメモ）は、会社ごとの「これまでの記録」に入っています。</div></div>`;
  $('listsModalFoot').innerHTML = '<button class="btn-sm btn-sm-primary" data-close="listsOverlay">リストを見る</button>';
  bindListsModal();
  $('listsOverlay').style.display = 'flex';
}

function bindListsModal() {
  $('listsOverlay').querySelectorAll('[data-close]').forEach(b => b.onclick = () => { $('listsOverlay').style.display = 'none'; });
  $('listsOverlay').onclick = e => { if (e.target === $('listsOverlay')) $('listsOverlay').style.display = 'none'; };
}

// ── 狙う会社の条件 ──
async function openIcp() {
  let st;
  try { st = await api('/api/sales/prospects/settings'); } catch (e) { showToast(e.message, { type: 'error' }); return; }
  $('listsModalTitle').textContent = '狙う会社の条件（AIの調査で使います）';
  $('listsModalBody').innerHTML = `<div class="lm-form">
    <div class="lm-note" style="margin-top:0">AIはこの条件と、訪問評価（S〜F）・判定の理由を手本にして、会社ごとにマッチ度を出します。気づいたことを書き足していくほど精度が上がります。</div>
    <textarea class="email-input compact lm-icp" id="lmIcp">${esc(st.icp)}</textarea>
    <div class="lm-row"><label class="lm-label" style="margin:0">1日に調べる上限</label>
      <input type="number" class="email-input compact" id="lmCap" min="1" max="1000" value="${st.daily_cap}" style="width:110px"> 社
      <span class="lm-note" style="margin:0">（1社あたり数円〜十数円かかります）</span></div>
    <button class="link-btn" id="lmReset">初期の文に戻す</button></div>`;
  $('listsModalFoot').innerHTML = `<button class="btn-sm" data-close="listsOverlay">閉じる</button><button class="btn-sm btn-sm-primary" id="lmSave">保存</button>`;
  bindListsModal();
  $('lmReset').onclick = () => { $('lmIcp').value = st.icp_default; };
  $('lmSave').onclick = async () => {
    const cap = Number($('lmCap').value);
    if (!Number.isInteger(cap) || cap < 1 || cap > 1000) { showToast('1日に調べる上限は 1〜1000 社で入れてください', { type: 'warning' }); return; }
    try {
      await api('/api/sales/prospects/settings', { method: 'PUT', body: JSON.stringify({ icp: $('lmIcp').value, daily_cap: cap }) });
      $('listsOverlay').style.display = 'none'; showToast('保存しました', { type: 'success' });
    } catch (e) { showToast(e.message, { type: 'error' }); }
  };
  $('listsOverlay').style.display = 'flex';
}

// ── リストの中身 ──
async function openList(id, keepSel) {
  const pane = $('viewPane');
  LS.loadingId = id;
  if (!LS.cur || LS.cur.id !== id) { LS.sel = null; LS.detail = null; LS.checked = new Set(); LS.filter = 'all'; LS.q = ''; pane.innerHTML = '<div class="empty-text">読み込み中…</div>'; }
  try {
    const d = await api(`/api/sales/lists/${id}`);
    if (S.view !== 'lists' || LS.loadingId !== id) return;   // 待っている間にほかの画面・リストへ移った
    LS.cur = d.list; LS.rows = d.rows; LS.stats = d.stats;
    if (!LS.vendors.length) { const x = await api('/api/sales/lists'); LS.vendors = x.vendors; LS.ai = x.ai; LS.lists = x.lists; }
    if (S.view !== 'lists' || LS.loadingId !== id) return;
  } catch (e) { if (S.view === 'lists') { LS.cur = null; pane.innerHTML = `<div class="empty-text">${esc(e.message)}</div>`; } return; }
  drawList();
  if (LS.pendingSel) { const ps = LS.pendingSel; LS.pendingSel = null; await openProspect(ps); }
  else if (keepSel && LS.sel) await openProspect(LS.sel, true);
  schedulePoll();
}

function schedulePoll() {
  clearInterval(LS.poll);
  if (!LS.stats || !LS.stats.queued) return;
  LS.poll = setInterval(async () => {
    if (S.view !== 'lists' || !LS.cur) { clearInterval(LS.poll); return; }
    try {
      const id = LS.cur.id;
      const d = await api(`/api/sales/lists/${id}`);
      if (S.view !== 'lists' || !LS.cur || LS.cur.id !== id) return;   // 待っている間にほかのリストへ移った
      const was = LS.stats.queued;
      LS.rows = d.rows; LS.stats = d.stats; drawTable(); drawHead();
      if (LS.sel && LS.detail && ['queued', 'running'].includes(LS.detail.ai_status)) refreshDrawerAi(LS.sel);
      if (!d.stats.queued) { clearInterval(LS.poll); if (was) showToast('AIの調査が終わりました', { type: 'success' }); }
    } catch { /* 次の回でまた試す */ }
  }, 5000);
}

const LMORE = [
  ['', 'ほかの絞り込み', () => true],
  ['undecided', '未判定', r => !r.verdict],
  ['ok', '判定: かけてよい', r => r.verdict === 'ok'],
  ['exist', '名刺・案件がある', r => !!r.deal || !!(r.existing && (r.existing.deals.length || r.existing.cards))],
  ['A', 'AIマッチ A', r => r.rank === 'A'],
  ['B', 'AIマッチ B', r => r.rank === 'B'],
  ['unresearched', 'AI未調査', r => r.ai_status !== 'done'],
];
const LSORTS = [['row', 'リストの順'], ['recent', '最近の記録順'], ['status', '状況の順'], ['company', '会社名'], ['priority', 'AIの点数順']];
const LDONE = [['all', 'すべて'], ['open', '未完了'], ['done', '完了']];

function filteredRows() {
  const st = LS.filter;
  const more = (LMORE.find(x => x[0] === LS.more) || LMORE[0])[2];
  const q = LS.q.trim().toLowerCase();
  let list = LS.rows.filter(r => (st === 'all' || r.status === st) && more(r)
    && (LS.doneF === 'all' || (LS.doneF === 'done') === !!r.done)
    && (!q || [r.company, r.industry, r.address, r.tel, r.last_memo].some(v => (v || '').toLowerCase().includes(q))));
  const so = Object.fromEntries(LSTATUS.map(([k], i) => [k, i]));
  if (LS.sort === 'priority') { const vOrder = { ok: 0, hold: 1, undefined: 2, null: 2, ng: 3 }; list = list.sort((a, b) => (vOrder[a.verdict] - vOrder[b.verdict]) || (b.score - a.score)); }
  else if (LS.sort === 'recent') list = list.sort((a, b) => (b.last_call || '').localeCompare(a.last_call || ''));
  else if (LS.sort === 'status') list = list.sort((a, b) => (so[b.status] - so[a.status]) || (a.done - b.done));
  else if (LS.sort === 'company') list = list.sort((a, b) => a.company.localeCompare(b.company, 'ja'));
  return list;
}

function drawList() {
  const pane = $('viewPane');
  pane.innerHTML = `
    <div class="lst-wrap is-detail ${LS.sel ? 'has-sel' : ''}" id="lstWrap">
      <div class="lst-dhead" id="lstHead"></div>
      <div class="lst-tiles" id="lsTiles"></div>
      <div class="lst-tools">
        <input class="search-input lst-search" id="lsQ" placeholder="会社名・業種・住所・記録で探す" value="${esc(LS.q)}">
        <div class="lst-seg" id="lsDone" role="group" aria-label="完了"></div>
        <select class="email-input compact lst-sel" id="lsMoreF">${LMORE.filter(([k]) => !['A', 'B', 'unresearched'].includes(k) || LS.ai || LS.stats.researched)
          .map(([k, l]) => `<option value="${k}" ${LS.more === k ? 'selected' : ''}>${l}</option>`).join('')}</select>
        <select class="email-input compact lst-sel" id="lsSort">${LSORTS.filter(([k]) => k !== 'priority' || LS.stats.researched)
          .map(([k, l]) => `<option value="${k}" ${LS.sort === k ? 'selected' : ''}>${l}</option>`).join('')}</select>
      </div>
      <div class="lst-bulk" id="lsBulk"></div>
      <div class="lst-body">
        <div class="lst-table-wrap"><div class="lst-table" id="lsTable" role="table"></div></div>
        <aside class="lst-drawer" id="lsDrawer"></aside>
      </div>
    </div>`;
  drawHead();
  $('lsQ').oninput = debounce(() => { LS.q = $('lsQ').value; LS.checked.clear(); drawTable(); }, 150);
  $('lsSort').onchange = () => { LS.sort = $('lsSort').value; drawTable(); };
  $('lsMoreF').onchange = () => { LS.more = $('lsMoreF').value; LS.checked.clear(); drawTable(); };
  drawTable();
  if (LS.sel && LS.detail) drawDrawer();
}

function drawHead() {
  const l = LS.cur, s = LS.stats;
  const grades = Object.entries(s.grades).filter(([, n]) => n).map(([g, n]) => `<span class="lg-grade g-${g}">${g} ${n}</span>`).join('');
  $('lstHead').innerHTML = `
    <button class="link-btn lst-back" id="lsBack">← リスト一覧</button>
    <div class="lst-dtop">
      <div class="lst-dname"><span id="lsName">${esc(l.name)}</span><button class="icon-mini" id="lsRename" title="名前を変える" aria-label="名前を変える">${ico('note', 14)}</button></div>
      <div class="lst-dbtns">
        <select class="email-input compact lst-share-sel" id="lsShare" title="テレアポ業者と共有"><option value="">共有しない（自社だけ）</option>
          ${LS.vendors.map(v => `<option value="${v.id}" ${l.vendor_id === v.id ? 'selected' : ''}>共有: ${esc(v.name)}</option>`).join('')}
          ${l.vendor_id && !LS.vendors.some(v => v.id === l.vendor_id) ? `<option value="${l.vendor_id}" selected>共有: ${esc(l.vendor || '停止中の業者')}（停止中）</option>` : ''}</select>
        ${LS.ai ? `<button class="btn-sm" id="lsResearch">AIで調べる</button>` : ''}
        <a class="btn-sm" href="/api/sales/lists/${l.id}/export.csv">${ico('download', 13)} CSV</a>
        <button class="btn-sm" id="lsMore" aria-label="このリストの設定">⋯</button>
      </div>
    </div>
    <div class="lst-facts">
      <span><b>${s.total}</b>社</span>
      <span>架電 <b>${s.called}</b>社</span>
      <span>つながった <b>${s.reached}</b></span>
      <span class="is-key">アポ <b>${s.appo}</b>${s.appo_rate != null ? `<em>（架電の ${s.appo_rate}%）</em>` : ''}</span>
      <span>完了 <b>${s.done}</b></span>
      ${s.researched || s.queued ? `<span>AI調査 <b>${s.researched}</b>${s.queued ? `<em><span class="spin"></span>調査中 ${s.queued}</em>` : ''}</span>` : ''}
      ${grades ? `<span>訪問評価 ${grades}</span>` : ''}
      <button class="link-btn" id="lsInd">業種別${LS.showInd ? 'を閉じる' : 'に見る'}</button>
    </div>
    ${LS.showInd ? `<div class="lst-ind"><table><thead><tr><th>業種</th><th>社数</th><th>架電</th><th>アポ</th><th>アポ率</th></tr></thead><tbody>
      ${s.industries.map(i => `<tr><td>${esc(i.industry)}</td><td>${i.n}</td><td>${i.called}</td><td>${i.appo}</td><td>${i.called ? Math.round(i.appo / i.called * 100) + '%' : '—'}</td></tr>`).join('')}
      </tbody></table><div class="lst-muted">社数が少ない業種のアポ率は目安です。</div></div>` : ''}`;
  $('lsBack').onclick = () => { LS.cur = null; LS.sel = null; renderLists(); };
  $('lsInd').onclick = () => { LS.showInd = !LS.showInd; drawHead(); };
  $('lsShare').onchange = async () => {
    const v = $('lsShare').value;
    try {
      await api(`/api/sales/lists/${l.id}`, { method: 'PUT', body: JSON.stringify(v ? { vendor_id: Number(v) } : { share: false }) });
      l.vendor_id = v ? Number(v) : null;
      showToast(v ? '業者と共有しました。業者の入口の「リスト」に出ます' : '共有をやめました', { type: 'success' });
    } catch (e) { showToast(e.message, { type: 'error' }); }
  };
  $('lsRename').onclick = async () => {
    const nm = prompt('リストの名前', l.name);
    if (!nm || nm === l.name) return;
    try { await api(`/api/sales/lists/${l.id}`, { method: 'PUT', body: JSON.stringify({ name: nm }) }); l.name = nm; $('lsName').textContent = nm; }
    catch (e) { showToast(e.message, { type: 'error' }); }
  };
  if ($('lsResearch')) $('lsResearch').onclick = researchMenu;
  $('lsMore').onclick = moreMenu;
}

// 状況のタイル（押すと絞り込み）と、完了の切り替え
function drawChips() {
  const base = LS.rows.filter(r => LS.doneF === 'all' || (LS.doneF === 'done') === !!r.done);
  const n = k => k === 'all' ? base.length : base.filter(r => r.status === k).length;
  $('lsTiles').innerHTML = [['all', 'すべて'], ...LSTATUS].map(([k, l]) =>
    `<button class="lst-tile t-${k} ${LS.filter === k ? 'is-on' : ''} ${n(k) ? '' : 'is-zero'}" data-lf="${k}"><span>${l}</span><b>${n(k)}</b></button>`).join('');
  $('lsTiles').querySelectorAll('[data-lf]').forEach(b => b.onclick = () => { LS.filter = LS.filter === b.dataset.lf ? 'all' : b.dataset.lf; LS.checked.clear(); drawTable(); });
  $('lsDone').innerHTML = LDONE.map(([k, l]) => `<button class="${LS.doneF === k ? 'is-on' : ''}" data-df="${k}">${l}<em>${k === 'all' ? LS.rows.length : LS.rows.filter(r => (k === 'done') === !!r.done).length}</em></button>`).join('');
  $('lsDone').querySelectorAll('[data-df]').forEach(b => b.onclick = () => { LS.doneF = b.dataset.df; LS.checked.clear(); drawTable(); });
}

function scoreHtml(r) {
  if (r.ai_status === 'queued' || r.ai_status === 'running') return '<span class="sc sc-wait" title="AIが調べています"><span class="spin"></span></span>';
  const tone = r.score >= 75 ? 'hi' : r.score >= 55 ? 'mid' : r.score >= 25 ? 'lo' : 'out';
  return `<span class="sc sc-${tone} ${r.score_ai ? '' : 'is-guess'}" title="${r.score_ai ? 'AIの調査で出した点数' : '業種から付けた目安（AIで調べると正確になります）'}">
    ${r.score_ai && r.rank ? `<b>${esc(r.rank)}</b>` : ''}${r.score}</span>`;
}

function statusHtml(r) {
  return `<span class="st-pill st-${r.status}">${LSTATUS_LABEL[r.status] || ''}</span>`;
}

function tagsHtml(r) {
  const out = [];
  if (r.deal) out.push(`<span class="st st-deal">案件: ${esc(r.deal.stage)}${r.deal.grade ? ` <b class="g-${r.deal.grade}">${r.deal.grade}</b>` : ''}</span>`);
  else if (r.existing && (r.existing.deals.length || r.existing.cards)) out.push(`<span class="st st-exist" title="${esc(r.existing.deals.map(d => d.title + '（' + d.stage + '）').join('、'))}">${r.existing.deals.length ? '案件あり' : '名刺あり'}</span>`);
  if (r.score_ai && r.rank) out.push(scoreHtml(r));
  else if (r.ai_status === 'queued' || r.ai_status === 'running') out.push(scoreHtml(r));
  if (r.ai_status === 'error') out.push('<span class="st st-err" title="調べられませんでした">調査失敗</span>');
  return out.join('');
}

const LSRC = { import: 'リストの備考', vendor: '業者', us: '自社' };
function memoCellHtml(r) {
  if (!r.last_memo && !r.last_result) return '<span class="lst-muted">記録なし</span>';
  return `<div class="lm-memo">${r.last_memo ? esc(r.last_memo) : `<span class="lst-muted">${esc(r.last_result)}</span>`}</div>
    <div class="lm-when">${r.last_src ? esc(LSRC[r.last_src]) : ''}${r.last_call && r.last_src !== 'import' ? `・${esc(fmtDate(r.last_call.slice(0, 10)))}` : ''}${r.records > 1 ? `・記録 ${r.records}件` : ''}</div>`;
}

function drawTable() {
  drawChips();
  const list = filteredRows();
  const allOn = list.length && list.every(r => LS.checked.has(r.id));
  $('lsTable').innerHTML = `<div class="lt-row lt-head" role="row">
      <span class="lt-chk"><input type="checkbox" id="lsAll" ${allOn ? 'checked' : ''} aria-label="表示中をすべて選ぶ"></span>
      <span class="lt-co">会社</span><span class="lt-memo">最新の記録・備考</span><span class="lt-st">状況</span></div>
    ${list.map(r => `<div class="lt-row s-${r.status} ${r.done ? 'is-done' : ''} ${LS.sel === r.id ? 'is-sel' : ''}" role="row" data-pid="${r.id}" tabindex="0">
      <span class="lt-chk"><input type="checkbox" data-chk="${r.id}" ${LS.checked.has(r.id) ? 'checked' : ''} aria-label="選ぶ"></span>
      <span class="lt-co"><span class="co-n">${esc(r.company)}</span>
        <span class="co-s">${esc([r.industry, r.pref].filter(Boolean).join('・'))}${r.tel ? `<span class="co-tel">${esc(r.tel)}</span>` : ''}</span>
        ${tagsHtml(r) ? `<span class="co-tags">${tagsHtml(r)}</span>` : ''}</span>
      <span class="lt-memo">${memoCellHtml(r)}</span>
      <span class="lt-st">${statusHtml(r)}${r.done ? '<span class="st-pill st-done">完了</span>' : ''}</span>
    </div>`).join('')}
    ${list.length ? '' : `<div class="lst-none">${LS.rows.length ? '条件に合う会社はありません' : 'このリストは空です'}</div>`}`;
  $('lsAll').onchange = () => { list.forEach(r => $('lsAll').checked ? LS.checked.add(r.id) : LS.checked.delete(r.id)); drawTable(); };
  $('lsTable').querySelectorAll('[data-chk]').forEach(cb => cb.onclick = e => { e.stopPropagation(); cb.checked ? LS.checked.add(Number(cb.dataset.chk)) : LS.checked.delete(Number(cb.dataset.chk)); drawBulk(); });
  $('lsTable').querySelectorAll('.lt-row[data-pid]').forEach(tr => {
    tr.onclick = e => { if (e.target.closest('.lt-chk')) return; openProspect(Number(tr.dataset.pid)); };
    tr.onkeydown = e => { if (e.key === 'Enter') openProspect(Number(tr.dataset.pid)); };
  });
  bindStatusDrag();
  drawBulk();
}

function drawBulk() {
  const n = LS.checked.size;
  const el = $('lsBulk');
  if (!n) { el.innerHTML = ''; el.classList.remove('is-on'); return; }
  el.classList.add('is-on');
  el.innerHTML = `<b>${n}社を選択中</b>
    <span class="lst-bulk-l">状況を変える</span>${LSTATUS.map(([k, l]) => `<button class="btn-sm bs-${k}" data-bs="${k}">${l}</button>`).join('')}
    <span class="lst-bulk-sep"></span>
    <button class="btn-sm" data-bd="1">完了にする</button><button class="btn-sm" data-bd="0">完了を外す</button>
    <span class="lst-bulk-sep"></span>
    <button class="btn-sm" data-bv="ok">○ かけてよい</button>
    ${LS.ai ? '<button class="btn-sm" id="lsBulkAi">AIで調べる</button>' : ''}
    <button class="link-btn" id="lsBulkClear">選択をやめる</button>`;
  el.querySelectorAll('[data-bv]').forEach(b => b.onclick = () => bulkVerdict(b.dataset.bv));
  el.querySelectorAll('[data-bd]').forEach(b => b.onclick = () => bulkDone(b.dataset.bd === '1'));
  el.querySelectorAll('[data-bs]').forEach(b => b.onclick = () => setStatus([...LS.checked], b.dataset.bs));
  if ($('lsBulkAi')) $('lsBulkAi').onclick = () => queueResearch({ ids: [...LS.checked] });
  $('lsBulkClear').onclick = () => { LS.checked.clear(); drawTable(); };
}

// 状況を動かす（まとめて・ドラッグ・会社の画面）。元に戻すつき
async function setStatus(ids, status) {
  ids = ids.filter(id => { const r = LS.rows.find(x => x.id === id); return r && r.status !== status; });
  if (!ids.length) { showToast(`もう「${LSTATUS_LABEL[status]}」です`, { type: 'info', duration: 1500 }); return; }
  try {
    const r = await api('/api/sales/prospects/status', { method: 'POST', body: JSON.stringify({ ids, status }) });
    LS.checked.clear();
    await openList(LS.cur.id, true);
    showToast(`${r.updated}社を「${LSTATUS_LABEL[status]}」にしました`, { type: 'success', duration: 6000,
      action: { label: '元に戻す', onClick: async () => {
        try { await api('/api/sales/prospects/status', { method: 'POST', body: JSON.stringify({ restore: r.before }) }); await openList(LS.cur.id, true); showToast('元に戻しました', { type: 'success', duration: 1500 }); }
        catch (e) { showToast(e.message, { type: 'error' }); }
      } } });
  } catch (e) { showToast(e.message, { type: 'error', title: '状況を変えられませんでした' }); }
}

// 行 → 上の状況タイルへドラッグ（チェックした会社はまとめて）
let lsDragId = null;
function bindStatusDrag() {
  $('lsTable').querySelectorAll('.lt-row[data-pid]').forEach(tr => {
    tr.draggable = true;
    tr.ondragstart = e => {
      lsDragId = Number(tr.dataset.pid);
      tr.classList.add('is-dragging'); $('lstWrap').classList.add('is-dragging-row');
      e.dataTransfer.effectAllowed = 'move';
      try { e.dataTransfer.setData('text/plain', String(lsDragId)); } catch { /* 一部ブラウザ */ }
      if (typeof dragLabel === 'function') dragLabel(e, LS.checked.has(lsDragId) ? LS.checked.size : 1);
    };
    tr.ondragend = () => { tr.classList.remove('is-dragging'); $('lstWrap').classList.remove('is-dragging-row'); lsDragId = null;
      $('lsTiles').querySelectorAll('.is-drop').forEach(t => t.classList.remove('is-drop')); };
  });
  $('lsTiles').querySelectorAll('[data-lf]').forEach(t => {
    if (t.dataset.lf === 'all') return;
    t.ondragover = e => { if (lsDragId == null) return; e.preventDefault(); t.classList.add('is-drop'); };
    t.ondragleave = () => t.classList.remove('is-drop');
    t.ondrop = e => {
      e.preventDefault(); t.classList.remove('is-drop');
      const id = lsDragId || Number(e.dataTransfer.getData('text/plain'));
      if (!id) return;
      setStatus(LS.checked.has(id) && LS.checked.size > 1 ? [...LS.checked] : [id], t.dataset.lf);
    };
  });
}

async function bulkDone(done) {
  try {
    const r = await api('/api/sales/prospects/done', { method: 'POST', body: JSON.stringify({ ids: [...LS.checked], done }) });
    showToast(`${r.updated}社を${done ? '完了にしました' : '未完了に戻しました'}`, { type: 'success' });
    LS.checked.clear(); await openList(LS.cur.id, true);
  } catch (e) { showToast(e.message, { type: 'error' }); }
}

async function setDone(pid, done) {
  try {
    await api(`/api/sales/prospects/${pid}`, { method: 'PATCH', body: JSON.stringify({ done }) });
    const r = LS.rows.find(x => x.id === pid); if (r) r.done = done;
    LS.stats.done = LS.rows.filter(x => x.done).length;
    if (LS.detail && LS.detail.id === pid) LS.detail.done = done;
    drawTable(); drawHead(); if (LS.sel === pid) drawDrawer();
    showToast(done ? '完了にしました' : '未完了に戻しました', { type: 'success', duration: 1500 });
  } catch (e) { showToast(e.message, { type: 'error' }); }
}

async function setVerdict(pid, v, reason, fromDrawer) {
  const r = LS.rows.find(x => x.id === pid);
  const next = r.verdict === v ? (fromDrawer ? v : '') : v;   // 一覧では同じ判定をもう一度押すと外す。詳細では外さない
  if (fromDrawer && next === r.verdict && reason === undefined) return;
  const body = { verdict: next };
  if (reason !== undefined) body.verdict_reason = reason;
  try {
    await api(`/api/sales/prospects/${pid}`, { method: 'PATCH', body: JSON.stringify(body) });
    if (reason !== undefined) r.verdict_reason = reason || null;
    else if ((r.verdict || '') !== next) r.verdict_reason = null;
    r.verdict = next || null;
    await openList(LS.cur.id, true);   // 状況（色）も判定で変わるので読み直す
  } catch (e) { showToast(e.message, { type: 'error' }); }
}

async function bulkVerdict(v) {
  const reason = v === 'ng' ? prompt('かけない理由（業者にも見えます。空でもかまいません）', '') : null;
  if (reason === null && v === 'ng') return;
  try {
    const r = await api('/api/sales/prospects/verdict', { method: 'POST', body: JSON.stringify({ ids: [...LS.checked], verdict: v, reason: reason || null }) });
    showToast(`${r.updated}社を「${VERDICT_LABEL[v]}」にしました`, { type: 'success' });
    LS.checked.clear(); await openList(LS.cur.id, true);
  } catch (e) { showToast(e.message, { type: 'error' }); }
}

function recount() {
  const s = { ...LS.stats };
  s.ok = LS.rows.filter(r => r.verdict === 'ok').length; s.ng = LS.rows.filter(r => r.verdict === 'ng').length;
  s.hold = LS.rows.filter(r => r.verdict === 'hold').length; s.undecided = LS.rows.filter(r => !r.verdict).length;
  return s;
}

function researchMenu() {
  const un = LS.rows.filter(r => !['done', 'queued', 'running'].includes(r.ai_status) && r.verdict !== 'ng' && !(r.deal && !r.deal.closed)).length;
  if (!un) { showToast('まだ調べていない会社はありません（調査済み・調査中・「かけない」・案件中は除きます）', { type: 'info', duration: 4000 }); return; }
  const opts = [10, 20, 50, 100].filter(n => n < un);
  $('listsModalTitle').textContent = 'AIで調べる';
  $('listsModalBody').innerHTML = `<div class="lm-form">
    <div class="lm-note" style="margin-top:0">AIがウェブ検索で公式サイト・事業・大物を作るか・板金設備の有無を調べ、狙う会社の条件に合うかを点数（0〜100）と A/B/C で出します。
      電話で最初に確かめることも付けます。<br>「かけない」と案件になった会社は飛ばします。業種から見て見込みの高い会社から順に調べます。</div>
    <div class="lm-row"><label class="lm-label" style="margin:0">調べる数</label>
      <select class="email-input compact" id="lmN" style="width:160px">${opts.map(n => `<option value="${n}" ${n === Math.min(20, opts[opts.length - 1]) ? 'selected' : ''}>上位 ${n}社</option>`).join('')}
        <option value="${Math.min(un, 300)}">まだの ${Math.min(un, 300)}社すべて</option></select>
      <span class="lm-note" style="margin:0">まだ調べていない会社: ${un}社</span></div>
    <div class="lm-note">1社あたり数円〜十数円かかります。1社 20〜60秒ほどで、終わった会社から一覧に出ます。チェックを付けた会社だけ調べることもできます。</div></div>`;
  $('listsModalFoot').innerHTML = `<button class="btn-sm" data-close="listsOverlay">やめる</button><button class="btn-sm btn-sm-primary" id="lmRun">調べる</button>`;
  bindListsModal();
  $('lmRun').onclick = async () => { $('listsOverlay').style.display = 'none'; await queueResearch({ list_id: LS.cur.id, limit: Number($('lmN').value) }); };
  $('listsOverlay').style.display = 'flex';
}

async function queueResearch(body) {
  try {
    const r = await api('/api/sales/prospects/research', { method: 'POST', body: JSON.stringify(body) });
    if (!r.queued) showToast(r.left_today === 0 ? `今日の上限（${r.daily_cap}社）に達しました。「狙う会社の条件」で上限を変えられます` : '新しく調べる会社はありませんでした（調査済み・調査中は飛ばします）', { type: 'warning', duration: 5000 });
    else showToast(`${r.queued}社を調べ始めました${r.capped ? `（今日の上限 ${r.daily_cap}社まで）` : ''}`, { type: 'success' });
    LS.checked.clear(); await openList(LS.cur.id, true);
  } catch (e) { showToast(e.message, { type: 'error' }); }
}

function moreMenu() {
  const l = LS.cur;
  $('listsModalTitle').textContent = 'このリスト';
  $('listsModalBody').innerHTML = `<div class="lm-form">
    <div class="lm-meta">取り込み ${esc(fmtDateTime(l.created_at))}${l.filename ? `・${esc(l.filename)}` : ''}${l.uploaded_by ? `・${esc(l.uploaded_by)}がアップロード` : ''}</div>
    <label class="lm-label">メモ</label><textarea class="email-input compact" id="lmNote" maxlength="1000" placeholder="例: 配電盤メーカー中心。5月中にかけ終える">${esc(l.note || '')}</textarea>
    <div class="lm-row" style="justify-content:space-between;margin-top:16px">
      <button class="btn-sm" id="lmArchive">${l.archived ? '一覧に戻す' : 'しまう（一覧から隠す）'}</button>
      <button class="danger-link" id="lmDelete">リストを削除</button></div>
    <div class="lm-note">削除しても、判定・電話の記録・案件がある会社は残ります（ほかのリストに出てきたら引き継がれます）。</div></div>`;
  $('listsModalFoot').innerHTML = `<button class="btn-sm" data-close="listsOverlay">閉じる</button><button class="btn-sm btn-sm-primary" id="lmNoteSave">メモを保存</button>`;
  bindListsModal();
  $('lmNoteSave').onclick = async () => { try { await api(`/api/sales/lists/${l.id}`, { method: 'PUT', body: JSON.stringify({ note: $('lmNote').value }) }); l.note = $('lmNote').value; $('listsOverlay').style.display = 'none'; showToast('保存しました', { type: 'success' }); } catch (e) { showToast(e.message, { type: 'error' }); } };
  $('lmArchive').onclick = async () => { try { await api(`/api/sales/lists/${l.id}`, { method: 'PUT', body: JSON.stringify({ archived: !l.archived }) }); $('listsOverlay').style.display = 'none'; LS.cur = null; renderLists(); } catch (e) { showToast(e.message, { type: 'error' }); } };
  $('lmDelete').onclick = async () => {
    const ok = await showConfirm({ title: 'リストを削除', message: `「${l.name}」を削除します。業者の画面からも消えます。`, okLabel: '削除', danger: true });
    if (!ok) return;
    try { await api(`/api/sales/lists/${l.id}`, { method: 'DELETE' }); $('listsOverlay').style.display = 'none'; LS.cur = null; showToast('削除しました'); renderLists(); }
    catch (e) { showToast(e.message, { type: 'error' }); }
  };
  $('listsOverlay').style.display = 'flex';
}

// ── 会社の詳細（右の引き出し） ──
async function openProspect(pid, quiet) {
  const fresh = LS.sel !== pid;
  if (fresh && !LS.sel) LS.listScroll = window.scrollY;
  LS.sel = pid;
  if (!quiet) { document.querySelectorAll('#lsTable tr[data-pid]').forEach(tr => tr.classList.toggle('is-sel', Number(tr.dataset.pid) === pid)); }
  $('lstWrap') && $('lstWrap').classList.add('has-sel');
  const tok = (LS.openTok = (LS.openTok || 0) + 1);
  let d;
  try { d = await api(`/api/sales/prospects/${pid}`); } catch (e) { showToast(e.message, { type: 'error' }); return; }
  if (tok !== LS.openTok || LS.sel !== pid) return;   // あとから別の会社を開いた
  LS.detail = d;
  drawDrawer();
  if (fresh && isMobile()) window.scrollTo(0, 0);
}

function aiHtml(p) {
  const a = p.ai || {};
  if (p.ai_status === 'queued' || p.ai_status === 'running') return `<div class="pd-ai is-wait"><span class="spin"></span> AIが調べています（20〜60秒）</div>`;
  if (p.ai_status !== 'done') return `<div class="pd-ai is-none">
    ${p.ai_status === 'error' ? `<div class="pd-err">調べられませんでした: ${esc(p.ai_error || '')}</div>` : '<div class="lst-muted">まだAIで調べていません。いまの点数は業種から付けた目安です。</div>'}
    ${LS.ai ? `<button class="btn-sm btn-sm-primary" id="pdResearch">${p.ai_status === 'error' ? 'もう一度調べる' : 'AIで調べる'}</button>` : ''}</div>`;
  const ev = (a.evidence || []).map(e => `<li><a href="${esc(e.url)}" target="_blank" rel="noopener noreferrer">${esc(e.note || e.url)}</a></li>`).join('');
  return `<div class="pd-ai">
    <div class="pd-ai-top"><span class="sc sc-${p.score >= 75 ? 'hi' : p.score >= 55 ? 'mid' : p.score >= 25 ? 'lo' : 'out'}"><b>${esc(p.rank || '')}</b>${p.score}</span>
      <div class="pd-ai-sum">${esc(a.summary || '')}</div></div>
    <div class="pd-facts">
      <div><span>大物</span><b>${esc(a.size || '不明')}</b></div><div><span>自社板金</span><b>${esc(a.inhouse || '不明')}</b></div>
      <div><span>規模</span><b>${esc(a.employees || '不明')}</b></div><div><span>確かさ</span><b>${esc(a.confidence || '—')}</b></div></div>
    ${a.products ? `<div class="pd-line"><span>製品</span>${esc(a.products)}</div>` : ''}
    ${a.locations ? `<div class="pd-line"><span>拠点</span>${esc(a.locations)}</div>` : ''}
    <div class="pd-line"><span>理由</span>${esc(a.reason || '')}</div>
    ${a.talk_hint ? `<div class="pd-hint"><b>電話の切り口</b>${esc(a.talk_hint)}</div>` : ''}
    ${ev ? `<details class="pd-ev"><summary>根拠のページ（${(a.evidence || []).length}）</summary><ul>${ev}</ul></details>` : ''}
    <div class="pd-ai-foot">${esc(fmtDateTime(p.ai_at))} に調査${a.searched === false ? '（ウェブ検索なしの推定）' : ''}
      ${LS.ai ? '<button class="link-btn" id="pdResearch">調べ直す</button>' : ''}</div></div>`;
}

function drawDrawer() {
  const p = LS.detail, el = $('lsDrawer');
  if (!el || !p) return;
  const web = p.official_url || p.web;
  const row = LS.rows.find(x => x.id === p.id) || {};
  const calls = p.calls.map(c => `<div class="pd-call ${c.source === 'import' ? 'is-import' : c.vendor_id ? 'is-vendor' : 'is-us'}">
      <div class="pd-call-h">${c.source === 'import' ? `<b>リストの備考</b><span>${esc(fmtDate(c.created_at.slice(0, 10)))} 取り込み</span>`
        : `<b>${esc(c.who)}</b><span>${esc(fmtDateTime(c.called_at || c.created_at))}</span>`}
        ${c.result ? `<span class="rs rs-${RESULT_TONE[c.result] || 'quiet'}">${esc(c.result)}</span>` : ''}</div>
      ${c.contact || c.department ? `<div class="pd-call-who">${esc([c.department, c.contact].filter(Boolean).join(' '))}</div>` : ''}
      ${c.appo_at ? `<div class="pd-call-who">アポ ${esc(fmtDateTime(c.appo_at))}</div>` : ''}${c.next_call_on ? `<div class="pd-call-who">次にかける日 ${esc(fmtDate(c.next_call_on))}</div>` : ''}
      ${c.memo ? `<div class="pd-call-b">${esc(c.memo)}</div>` : ''}</div>`).join('') || '<div class="lst-muted">まだ記録はありません</div>';
  const ex = p.existing && (p.existing.deals.length || p.existing.cards) ? `<div class="pd-exist">すでに${p.existing.cards ? `名刺 ${p.existing.cards}枚` : ''}${p.existing.cards && p.existing.deals.length ? '・' : ''}${p.existing.deals.length ? `案件 ${p.existing.deals.map(d => `<a href="#" data-deal="${d.id}">${esc(d.title)}（${esc(d.stage)}）</a>`).join('、')}` : ''} があります</div>` : '';
  el.innerHTML = `
    <div class="pd-head"><button class="link-btn pd-close" id="pdClose">← 一覧に戻る</button>
      <h3>${esc(p.company)}</h3>
      <div class="pd-sub">${esc([p.industry, p.address].filter(Boolean).join('・'))}</div>
      <div class="pd-state">
        <div class="pd-stsel" role="group" aria-label="状況">${LSTATUS.map(([k, l]) => `<button class="st-pill st-${k} ${row.status === k ? 'is-on' : ''}" data-pst="${k}" aria-pressed="${row.status === k ? 'true' : 'false'}">${l}</button>`).join('')}</div>
        <button class="pd-done ${p.done ? 'is-on' : ''}" id="pdDone" aria-pressed="${p.done ? 'true' : 'false'}">${ico('check', 13)} ${p.done ? '完了（押すと戻す）' : '完了にする'}</button></div>
      <div class="pd-links">${p.tel ? `<a class="btn-sm" href="tel:${esc(p.tel.replace(/[^\d+]/g, ''))}">${ico('phone')} ${esc(p.tel)}</a>` : ''}
        ${web ? `<a class="btn-sm" href="${esc(web)}" target="_blank" rel="noopener noreferrer">公式サイト ↗</a>` : '<span class="lst-muted">公式サイト不明</span>'}
        <button class="link-btn" id="pdEdit">直す</button></div>
      <div id="pdEditBox"></div>
      ${ex}
    </div>
    ${p.deal ? `<div class="pd-deal"><div><b>${p.deal.closed ? '前の案件' : '案件になっています'}</b>　段階: ${esc(p.deal.stage)}${p.deal.grade ? `／訪問評価 <b class="g-${p.deal.grade}">${p.deal.grade}</b>` : ''}${p.deal.appo_at ? `／アポ日時 ${esc(fmtDateTime(p.deal.appo_at))}` : ''}</div>
      <button class="btn-sm btn-sm-primary" data-deal="${p.deal.id}">案件を開く</button></div>` : ''}
    <section class="pd-sec"><div class="pd-sec-t">これまでの記録（${p.calls.length}）</div><div class="pd-calls">${calls}</div></section>
    <section class="pd-sec"><div class="pd-sec-t">判定 <span class="lst-muted">理由は業者にも見えます。判定を変えると前の理由は消えます</span></div>
      <div class="vseg is-big" id="pdV">${['ok', 'hold', 'ng'].map(v => `<button class="${p.verdict === v ? 'on ' + v : ''}" data-v="${v}">${{ ok: '○ かけてよい', hold: '△ 保留', ng: '× かけない' }[v]}</button>`).join('')}</div>
      ${(p.hidden_from || []).length ? `<div class="pd-hidden">業者（${esc(p.hidden_from.join('・'))}）がアップロードする前に付けた判定・理由なので、業者には見えていません。<button class="link-btn" id="pdReveal">業者にも見せる</button></div>` : ''}
      <textarea class="email-input compact" id="pdReason" maxlength="1000" placeholder="理由（例: 盤メーカーで箱は外注。購買の永井さん宛てに）">${esc(p.verdict_reason || '')}</textarea>
      <div class="pd-right"><button class="btn-sm" id="pdReasonSave">理由を保存</button></div></section>
    ${p.deal && !p.deal.closed ? '' : `<section class="pd-sec"><div class="pd-sec-t">電話の記録を追加（自社）${p.deal ? '<span class="lst-muted">前の案件は終わっています。アポが取れたら新しい案件になります</span>' : ''}</div>
      <div class="pd-form">
        <div class="sales-grid-2"><input type="datetime-local" class="email-input compact" id="pcAt" value="${nowLocal()}">
          <select class="email-input compact" id="pcRes"><option value="">結果を選ぶ</option>${Object.keys(RESULT_TONE).map(r => `<option>${r}</option>`).join('')}</select></div>
        <div class="sales-grid-2"><input class="email-input compact" id="pcWho" placeholder="相手の名前" maxlength="60"><input class="email-input compact" id="pcDept" placeholder="部署・役職" maxlength="80"></div>
        <div class="sales-grid-2 pc-appo"><label class="lm-label">アポ日時</label><input type="datetime-local" class="email-input compact" id="pcAppo"></div>
        <textarea class="email-input compact" id="pcMemo" maxlength="5000" placeholder="電話のメモ"></textarea>
        <div class="pd-right"><button class="btn-sm" id="pdToDeal">案件にする</button><button class="btn-sm btn-sm-primary" id="pcSave">記録する</button></div>
        <div class="lst-muted">結果を「アポ獲得」にすると自動で案件になります（アポ日時はわかれば入れてください）。</div></div></section>`}
    <section class="pd-sec"><div class="pd-sec-t">AIの調査</div><div id="pdAi">${aiHtml(p)}</div></section>
    ${p.lists.length > 1 ? `<section class="pd-sec"><div class="pd-sec-t">載っているリスト</div>${p.lists.map(l => `<div class="pd-list">${esc(l.name)}${l.memo ? `<span class="lst-muted">${esc(l.memo.slice(0, 60))}</span>` : ''}</div>`).join('')}</section>` : ''}`;
  $('pdDone').onclick = () => setDone(p.id, !p.done);
  $('lsDrawer').querySelectorAll('[data-pst]').forEach(b => b.onclick = () => setStatus([p.id], b.dataset.pst));
  $('pdClose').onclick = () => { LS.sel = null; LS.detail = null; $('lstWrap').classList.remove('has-sel'); el.innerHTML = ''; drawTable();
    if (isMobile()) window.scrollTo(0, LS.listScroll || 0); };
  el.querySelectorAll('[data-deal]').forEach(b => b.onclick = e => { e.preventDefault(); showDeal(Number(b.dataset.deal)); });
  // 理由の欄を書き換えたときだけ、その理由を一緒に保存する（書き換えていなければ、判定を変えると前の理由は消える）
  $('pdReason').oninput = () => { $('pdReason').dataset.dirty = '1'; };
  el.querySelectorAll('#pdV [data-v]').forEach(b => b.onclick = () =>
    setVerdict(p.id, b.dataset.v, $('pdReason').dataset.dirty ? $('pdReason').value : undefined, true));
  if ($('pdReveal')) $('pdReveal').onclick = async () => {
    const ok = await showConfirm({ title: '業者にも見せる', message: `いまの判定${p.verdict_reason ? 'と理由' : ''}を、${p.hidden_from.join('・')}にも見せます。`, okLabel: '見せる' });
    if (!ok) return;
    try { await api(`/api/sales/prospects/${p.id}/reveal`, { method: 'POST' }); showToast('業者にも見えるようにしました', { type: 'success' }); await openProspect(p.id, true); }
    catch (e) { showToast(e.message, { type: 'error' }); }
  };
  $('pdReasonSave').onclick = async () => {
    try { await api(`/api/sales/prospects/${p.id}`, { method: 'PATCH', body: JSON.stringify({ verdict_reason: $('pdReason').value }) });
      const r = LS.rows.find(x => x.id === p.id); if (r) r.verdict_reason = $('pdReason').value; p.verdict_reason = $('pdReason').value;
      showToast('理由を保存しました', { type: 'success' }); } catch (e) { showToast(e.message, { type: 'error' }); }
  };
  bindResearchBtn(p);
  $('pdEdit').onclick = () => {
    $('pdEditBox').innerHTML = `<div class="pd-form" style="margin-top:8px">
      <div class="sales-grid-2"><input class="email-input compact" id="peTel" placeholder="電話" value="${esc(p.tel || '')}"><input class="email-input compact" id="peInd" placeholder="業種" value="${esc(p.industry || '')}"></div>
      <input class="email-input compact" id="peAddr" placeholder="住所" value="${esc(p.address || '')}">
      <input class="email-input compact" id="peWeb" placeholder="公式サイト https://…" value="${esc(p.web || '')}">
      <div class="pd-right"><button class="btn-sm" id="peCancel">やめる</button><button class="btn-sm btn-sm-primary" id="peSave">保存</button></div></div>`;
    $('peCancel').onclick = () => { $('pdEditBox').innerHTML = ''; };
    $('peSave').onclick = async () => {
      try { await api(`/api/sales/prospects/${p.id}`, { method: 'PATCH', body: JSON.stringify({ tel: $('peTel').value, industry: $('peInd').value, address: $('peAddr').value, web: $('peWeb').value }) });
        await openList(LS.cur.id, true); } catch (e) { showToast(e.message, { type: 'error' }); }
    };
  };
  if ($('pcSave')) {
    const toggle = () => { el.querySelector('.pc-appo').style.display = ($('pcRes').value === 'アポ獲得') ? '' : 'none'; };
    $('pcRes').onchange = toggle; toggle();
    $('pcSave').onclick = async () => {
      const body = { list_id: LS.cur.id, telapo_at: $('pcAt').value || null, result: $('pcRes').value || null, appo_at: $('pcAppo').value || null,
        contact: $('pcWho').value, department: $('pcDept').value, memo: $('pcMemo').value };
      if (!body.result && !body.memo.trim()) { showToast('結果かメモを入れてください', { type: 'warning' }); return; }
      $('pcSave').disabled = true;
      try {
        const r = await api(`/api/sales/prospects/${p.id}/calls`, { method: 'POST', body: JSON.stringify(body) });
        showToast(r.deal_id ? 'アポを記録して案件にしました' : '記録しました', { type: 'success' });
        if (r.deal_id) loadDeals();
        await openList(LS.cur.id, true);
      } catch (e) { showToast(e.message, { type: 'error' }); $('pcSave').disabled = false; }
    };
    $('pdToDeal').onclick = async () => {
      const ok = await showConfirm({ title: '案件にする', message: `「${p.company}」を案件（テレアポ）にします。電話の記録とAIの調査は案件に移ります。`, okLabel: '案件にする' });
      if (!ok) return;
      try { const r = await api(`/api/sales/prospects/${p.id}/deal`, { method: 'POST' }); await loadDeals(); showToast('案件にしました', { type: 'success', action: { label: '開く', onClick: () => showDeal(r.deal_id) } }); await openList(LS.cur.id, true); }
      catch (e) { showToast(e.message, { type: 'error' }); }
    };
  }
}

function bindResearchBtn(p) {
  const b = $('pdResearch');
  if (!b) return;
  b.onclick = async () => {
    const again = p.ai_status === 'done';
    const ok = await showConfirm({ title: again ? 'AIで調べ直す' : 'AIで調べる',
      message: `「${p.company}」をAIがウェブ検索で調べます（1社 数円〜十数円）。${again ? '前の調査結果は新しい結果に置き換わります。' : ''}`, okLabel: '調べる' });
    if (!ok) return;
    try {
      const r = await api('/api/sales/prospects/research', { method: 'POST', body: JSON.stringify({ ids: [p.id], force: true }) });
      if (!r.queued) { showToast(r.left_today === 0 ? '今日の上限に達しました' : '調査中です', { type: 'warning' }); return; }
      p.ai_status = 'queued'; $('pdAi').innerHTML = aiHtml(p);
      const row = LS.rows.find(x => x.id === p.id); if (row) row.ai_status = 'queued';
      LS.stats.queued = (LS.stats.queued || 0) + 1; drawTable(); drawHead(); schedulePoll();
    } catch (e) { showToast(e.message, { type: 'error' }); }
  };
}

// 調査中の会社は、AIの欄だけ新しくする（入力中の電話メモや理由を消さない）
async function refreshDrawerAi(pid) {
  try {
    const d = await api(`/api/sales/prospects/${pid}`);
    if (LS.sel !== pid || !LS.detail || !$('pdAi')) return;
    Object.assign(LS.detail, { ai: d.ai, ai_status: d.ai_status, ai_at: d.ai_at, ai_error: d.ai_error, score: d.score, rank: d.rank,
      score_ai: d.score_ai, official_url: d.official_url });
    $('pdAi').innerHTML = aiHtml(LS.detail); bindResearchBtn(LS.detail);
  } catch { /* 次の回でまた試す */ }
}

// ── 案件の詳細に出す: 訪問評価・リストから来た案件 ──
function gradeFieldsHtml(d) {
  if ((d.pipeline || 'sales') !== 'sales') return '';
  const g = d.visit_grade || '';
  return `<div class="k">訪問評価</div>
    <div class="v"><div class="gseg" role="group" aria-label="訪問評価">${['S', 'A', 'B', 'C', 'F'].map(x => `<button class="${g === x ? 'on g-' + x : ''}" data-grade="${x}">${x}</button>`).join('')}</div>
      <div class="kv-note">訪問してみての見込み（S 最高〜F 見込みなし）。リストとAIの調査の手本になります${d.prospect_id ? '・<a href="#" id="gList">リストで見る</a>' : ''}</div></div>`;
}

function bindGrade(root, d) {
  root.querySelectorAll('[data-grade]').forEach(b => b.onclick = async () => {
    const g = d.visit_grade === b.dataset.grade ? '' : b.dataset.grade;
    try {
      await api(`/api/sales/deals/${d.id}/grade`, { method: 'PUT', body: JSON.stringify({ grade: g }) });
      d.visit_grade = g || null;
      const x = S.allDeals.find(y => y.id === d.id); if (x) x.visit_grade = d.visit_grade;
      showToast(g ? `訪問評価を ${g} にしました` : '訪問評価を外しました', { type: 'success', duration: 1500 });
      selectDeal(d.id);
    } catch (e) { showToast(e.message, { type: 'error' }); }
  });
  const gl = root.querySelector('#gList');
  if (gl) gl.onclick = async e => {
    e.preventDefault();
    try {
      const info = await api(`/api/sales/deals/${d.id}/prospect`);
      const l = info.prospect && info.prospect.lists[0];
      if (!l) { showToast('元のリストは削除されています', { type: 'warning' }); return; }
      if ($('mobileSheet')) $('mobileSheet').classList.remove('is-open');
      LS.cur = { id: l.id }; LS.pendingSel = info.prospect.id; LS.sel = null; LS.detail = null;
      LS.checked = new Set(); LS.filter = 'all'; LS.q = ''; setView('lists');
    } catch (err) { showToast(err.message, { type: 'error' }); }
  };
}
