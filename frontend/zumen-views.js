// ============================================================
// 図面管理 — 会社・図面・書類・納期・現場・数字・設定
// ============================================================
const VP = () => $('viewPane');

// ── 書類（確定済み）1件 ──
ZVIEWS.doc = async ({ id }) => {
  history.replaceState(null, '', `/zumen.html?doc=${id}`);
  VP().innerHTML = '<div class="empty-text">読み込み中…</div>';
  let d;
  try { d = await api(`/api/zumen/docs/${id}`); } catch (e) { VP().innerHTML = `<div class="empty-text">${esc(e.message)}</div>`; return; }
  if (d.status === 'review') { setView('inbox'); selectDoc(d.id); return; }
  const v = d.values;
  const deals = (d.links || []).filter(x => x.target_type === 'deal');
  const docs = (d.links || []).filter(x => x.target_type === 'doc');
  VP().innerHTML = `
    <div class="zd-page">
      <div class="zd-top-row"><button class="btn-link" onclick="history.length > 1 ? history.back() : setView('companies')">← 戻る</button>
        ${d.status === 'trashed' ? '<span class="zr-st zst-warn">ごみ箱</span>' : ''}</div>
      <div class="zr">
        <div class="zr-view" id="docView"></div>
        <div class="zr-form">
          <div class="zd-title">${kindBadge(v.kind)} <b>${esc(v.title || v.doc_no || d.file.filename)}</b></div>
          <div class="kv-grid zd-kv">
            <div class="k">会社</div><div class="v">${d.company ? `<a href="/zumen.html?company=${d.company.id}">${esc(d.company.name)}</a>` : '—'}</div>
            <div class="k">受け取り／発行</div><div class="v">${esc(v.direction || '')}</div>
            ${v.doc_no ? `<div class="k">番号</div><div class="v">${esc(v.doc_no)}</div>` : ''}
            ${v.ref_no ? `<div class="k">関連する番号</div><div class="v">${esc(v.ref_no)}</div>` : ''}
            ${v.doc_date ? `<div class="k">日付</div><div class="v">${fmtDate(v.doc_date)}</div>` : ''}
            ${v.amount != null ? `<div class="k">金額（税抜）</div><div class="v">${yen(v.amount)}</div>` : ''}
            ${v.due_on ? `<div class="k">納期</div><div class="v">${fmtDate(v.due_on)}</div>` : ''}
            <div class="k">確定</div><div class="v">${esc(fmtDate(d.confirmed_at || ''))} ${esc(d.confirmed_by || '')}</div>
            <div class="k">ファイル</div><div class="v"><a href="${ZV.rawUrl(d.file.id)}" target="_blank">${esc(d.file.filename)}</a>（${bytes(d.file.size)}）</div>
          </div>
          ${deals.length ? `<div class="zr-sec"><div class="zr-label">案件</div>${deals.map(x => `<a class="zr-rel" href="/sales.html?deal=${x.target_id}">${ico('box', 13)} ${esc(x.label)} ${x.stage ? `<span class="stage-badge" data-stage="${esc(x.stage)}">${esc(x.stage)}</span>` : ''}</a>`).join('')}</div>` : ''}
          ${docs.length ? `<div class="zr-sec"><div class="zr-label">関連する書類</div>${docs.map(x => `<a class="zr-rel" href="/zumen.html?doc=${x.target_id}">${ico('doc', 13)} ${esc(x.label)}</a>`).join('')}</div>` : ''}
          ${(d.revisions || []).length ? `<div class="zr-sec"><div class="zr-label">この書類の図面</div>${d.revisions.map(x => `<a class="zr-rel" href="/zumen.html?drawing=${x.drawing_id}">${ico('drawing', 13)} ${esc(x.drawing_no)} ${esc(x.rev || '初版')}</a>`).join('')}</div>` : ''}
          ${(v.lines || []).length ? `<div class="zr-sec"><div class="zr-label">明細</div><div class="zr-table-wrap"><table class="zr-table ro"><thead><tr><th>図番</th><th>版</th><th>品名</th><th class="num">数量</th><th class="num">単価</th><th class="num">金額</th><th>納期</th></tr></thead>
            <tbody>${v.lines.map(l => `<tr><td>${esc(l.drawing_no)}</td><td>${esc(l.rev)}</td><td>${esc(l.name)}</td><td class="num">${l.qty ?? ''}</td><td class="num">${yen(l.unit_price)}</td><td class="num">${yen(l.amount)}</td><td>${fmtMD(l.due_on)}</td></tr>`).join('')}</tbody></table></div></div>` : ''}
          <div class="zr-actions">
            ${d.status === 'confirmed' ? `<button class="btn-sm" id="shareDocBtn">${ico('share', 13)} 社外に共有</button>` : ''}
            ${d.undo_event ? `<button class="btn-sm" id="undoBtn">${ico('undo', 13)} 確定を取り消して確認待ちに戻す</button>` : ''}
            ${d.status === 'trashed' ? '<button class="btn-sm" id="restoreBtn">ごみ箱から戻す</button>' : '<button class="danger-link" id="trashBtn">ごみ箱に移す</button>'}
          </div>
        </div>
      </div>
    </div>`;
  if (d.file.mime === 'text/html') $('docView').innerHTML = `<iframe class="zv-html" sandbox src="/api/zumen/files/${d.file.id}/raw" title="${esc(d.file.filename)}"></iframe>`;
  else zvCreate($('docView'), { fileId: d.file.id, pages: d.file.pages, pageFrom: d.page_from, pageTo: d.page_to, title: d.file.filename, annotate: d.status === 'confirmed' });
  if ($('shareDocBtn')) $('shareDocBtn').onclick = () => openShare('doc', d.id, `${v.kind} ${v.doc_no || v.title || ''}`);
  if ($('undoBtn')) $('undoBtn').onclick = () => undoEvent(d.undo_event, d.id).then(() => { setView('inbox'); selectDoc(d.id); });
  if ($('trashBtn')) $('trashBtn').onclick = async () => {
    if (!await showConfirm({ title: 'ごみ箱に移す', message: 'ごみ箱に移しても、30日以内なら元に戻せます。図面の場合は、この版を除いて最新版を選び直します。', okLabel: 'ごみ箱に移す', danger: true })) return;
    await post(`/api/zumen/docs/${d.id}/trash${Z.me ? `?actor=${encodeURIComponent(Z.me)}` : ''}`);
    showToast('ごみ箱に移しました', { action: { label: '元に戻す', onClick: async () => { await post(`/api/zumen/docs/${d.id}/restore`); ZVIEWS.doc({ id: d.id }); } } });
    ZVIEWS.doc({ id: d.id });
  };
  if ($('restoreBtn')) $('restoreBtn').onclick = async () => { await post(`/api/zumen/docs/${d.id}/restore`); ZVIEWS.doc({ id: d.id }); };
};

// ── 会社 ──
const CO = { q: '', sel: null, tab: 'drawings' };
ZVIEWS.companies = async ({ id } = {}) => {
  if (id) CO.sel = id;
  VP().innerHTML = `<div class="zc">
      <aside class="zc-list"><input type="search" class="email-input compact" id="coQ" placeholder="会社名・別名で探す" value="${esc(CO.q)}">
        <div id="coItems"><div class="empty-text">読み込み中…</div></div></aside>
      <section class="zc-detail" id="coDetail"><div class="detail-empty"><p class="detail-empty-text">会社を選んでください</p></div></section></div>`;
  $('coQ').oninput = debounce(e => { CO.q = e.target.value; drawCompanyList(); }, 200);
  await drawCompanyList();
  if (CO.sel) openCompany(CO.sel);
};

async function drawCompanyList() {
  const r = await api(`/api/zumen/companies?q=${encodeURIComponent(CO.q)}`);
  COMPANIES = null;
  $('coItems').innerHTML = r.companies.map(c => `<button class="zc-item ${c.id === CO.sel ? 'is-sel' : ''}" data-co="${c.id}">
      ${ico('folder', 15)}<span class="zc-name">${esc(c.name)}${c.is_group ? '<em class="tag-card">グループ</em>' : ''}</span>
      <span class="zc-n">${c.drawings ? `図面 ${c.drawings}` : ''}${c.docs ? `　書類 ${c.docs}` : ''}</span></button>`).join('')
    || '<div class="boss-empty" style="margin:12px">まだ会社のフォルダはありません。書類を確定すると自動で作られます</div>';
  $('coItems').querySelectorAll('[data-co]').forEach(b => b.onclick = () => openCompany(Number(b.dataset.co)));
}

async function openCompany(id) {
  CO.sel = id;
  history.replaceState(null, '', `/zumen.html?company=${id}`);
  document.querySelectorAll('[data-co]').forEach(b => b.classList.toggle('is-sel', Number(b.dataset.co) === id));
  $('coDetail').innerHTML = '<div class="empty-text">読み込み中…</div>';
  let r;
  try { r = await api(`/api/zumen/companies/${id}`); } catch (e) { $('coDetail').innerHTML = `<div class="empty-text">${esc(e.message)}</div>`; return; }
  const co = r.company;
  const by = k => r.docs.filter(d => k.includes(d.kind));
  const tabs = [['drawings', '図面', r.drawings.length], ['orders', '注文書', by(['注文書', '注文変更書']).length], ['quotes', '見積書', by(['見積書']).length],
    ['others', '請求書・その他', r.docs.filter(d => !['注文書', '注文変更書', '見積書', '図面'].includes(d.kind)).length], ['deals', '案件', r.deals.length], ['contacts', '担当者', r.contacts.length]];
  const docRows = list => list.length ? `<table class="zr-table ro zc-docs"><thead><tr><th></th><th>種類</th><th>日付</th><th>番号</th><th>件名</th><th class="num">金額</th><th>納期</th></tr></thead><tbody>
      ${list.map(d => `<tr data-open-doc="${d.id}"><td>${thumb(d.file_id, d.page_from, 'sm')}</td><td>${kindBadge(d.kind)}</td><td>${fmtDate(d.doc_date)}</td><td>${esc(d.doc_no || '')}</td><td>${esc(d.title || d.filename)}</td><td class="num">${yen(d.amount)}</td><td>${fmtMD(d.due_on)}</td></tr>`).join('')}</tbody></table>`
    : '<div class="boss-empty">まだありません</div>';
  const panes = {
    drawings: r.drawings.length ? `<div class="zg">${r.drawings.map(dw => drawingTile(dw)).join('')}</div>` : '<div class="boss-empty">まだ図面はありません</div>',
    orders: docRows(by(['注文書', '注文変更書'])), quotes: docRows(by(['見積書'])),
    others: docRows(r.docs.filter(d => !['注文書', '注文変更書', '見積書', '図面'].includes(d.kind))),
    deals: r.deals.length ? r.deals.map(x => `<a class="zr-rel" href="/sales.html?deal=${x.id}">#${x.id} ${esc(x.title)} <span class="stage-badge" data-stage="${esc(x.stage)}">${esc(x.stage)}</span>${x.delivery_on ? `<small>納期 ${fmtDate(x.delivery_on)}</small>` : ''}</a>`).join('') : '<div class="boss-empty">案件はありません</div>',
    contacts: r.contacts.length ? r.contacts.map(x => `<div class="zc-contact">${ico('user', 13)} <a href="/app.html?card=${encodeURIComponent(x.id)}" title="名刺を開く"><b>${esc(x.name)}</b></a> <small>${esc(x.department_position || '')}</small> ${x.email ? `<a href="mailto:${esc(x.email)}">${esc(x.email)}</a>` : ''} ${x.phone ? `<a href="tel:${esc(x.phone)}">${esc(x.phone)}</a>` : ''}
        <a class="btn-link zc-newdeal" href="/sales.html?new=${encodeURIComponent(x.id)}">この人で案件を作る</a></div>`).join('') : '<div class="boss-empty">名刺管理にこの会社の名刺はありません</div>',
  };
  if (!tabs.some(t => t[0] === CO.tab)) CO.tab = 'drawings';
  $('coDetail').innerHTML = `
    <div class="zc-head">
      <div class="zc-title">${ico('folder', 20)}<input class="zc-title-in" id="coName" value="${esc(co.name)}"></div>
      <div class="zc-meta">
        <div class="zc-aliases">${r.aliases.map(a => `<span class="zc-alias" title="${esc(a.source === 'confirm' ? '確定時に記録した表記' : a.source === 'merge' ? '統合した会社の名前' : '手入力した別名')}（${a.hits}回）">${esc(a.alias)}<button data-del-alias="${a.id}" title="別名を削除">×</button></span>`).join('')}
          <input class="email-input compact zc-alias-in" id="aliasIn" placeholder="＋ 別名を追加" title="略称や旧社名などを入力して Enter キーで追加します"></div>
        <div class="zc-fields">
          <label>名刺の会社名<input class="email-input compact" id="coCard" value="${esc(co.card_company || '')}" placeholder="名刺に書かれた会社名"></label>
          <label>メールのドメイン<input class="email-input compact" id="coDom" value="${esc(co.mail_domains || '')}" placeholder="example.co.jp"></label>
          <label>原価率（見込み）<span class="zc-rate"><input type="number" step="1" min="0" max="150" class="email-input compact" id="coRate" value="${co.cost_rate != null ? Math.round(co.cost_rate * 100) : ''}" placeholder="—" title="空欄のときは「業績」の画面で設定した原価率を使います">%</span></label>
          <label class="zr-check"><input type="checkbox" id="coGroup" ${co.is_group ? 'checked' : ''}> グループ会社</label>
          <label class="zr-check" title="秘密保持契約などで、外部のAIに書類を送れない客先に使います"><input type="checkbox" id="coNoAi" ${co.no_ai ? 'checked' : ''}> この会社の書類はAIに送らない</label>
        </div>
        <div class="zc-merge">ほかの会社を統合する <select class="email-input compact" id="coMerge"><option value="">選択してください</option></select></div>
      </div>
    </div>
    <nav class="dtabs dtabs-wrap">${tabs.map(([k, l, n]) => `<button class="dtab ${k === CO.tab ? 'is-active' : ''}" data-ctab="${k}">${l}${n ? `<span class="dtab-n">${n}</span>` : ''}</button>`).join('')}</nav>
    ${tabs.map(([k]) => `<div class="dpane ${k === CO.tab ? 'is-active' : ''}" data-cpane="${k}">${panes[k]}</div>`).join('')}`;
  const root = $('coDetail');
  root.querySelectorAll('[data-ctab]').forEach(b => b.onclick = () => {
    CO.tab = b.dataset.ctab;
    root.querySelectorAll('[data-ctab]').forEach(x => x.classList.toggle('is-active', x === b));
    root.querySelectorAll('[data-cpane]').forEach(x => x.classList.toggle('is-active', x.dataset.cpane === CO.tab));
  });
  root.querySelectorAll('[data-open-doc]').forEach(tr => tr.onclick = () => setView('doc', { id: Number(tr.dataset.openDoc) }));
  root.querySelectorAll('[data-open-drawing]').forEach(b => b.onclick = () => setView('drawing', { id: Number(b.dataset.openDrawing) }));
  const upd = async body => { try { await put(`/api/zumen/companies/${id}`, body); showToast('保存しました', { type: 'success', duration: 1200 }); drawCompanyList(); } catch (e) { showToast(e.message, { type: 'error' }); } };
  $('coName').onchange = e => upd({ name: e.target.value });
  $('coCard').onchange = e => upd({ card_company: e.target.value });
  $('coDom').onchange = e => upd({ mail_domains: e.target.value });
  $('coRate').onchange = e => upd({ cost_rate: e.target.value === '' ? null : Number(e.target.value) / 100 });
  $('coGroup').onchange = e => upd({ is_group: e.target.checked });
  $('coNoAi').onchange = e => upd({ no_ai: e.target.checked });
  $('aliasIn').onkeydown = async e => {
    if (e.key !== 'Enter' || !e.target.value.trim()) return;
    try { await post(`/api/zumen/companies/${id}/aliases`, { alias: e.target.value.trim() }); openCompany(id); } catch (err) { showToast(err.message, { type: 'error' }); }
  };
  root.querySelectorAll('[data-del-alias]').forEach(b => b.onclick = async () => { await api(`/api/zumen/aliases/${b.dataset.delAlias}`, { method: 'DELETE' }); openCompany(id); });
  const all = await companyList();
  $('coMerge').innerHTML = '<option value="">選択してください</option>' + all.filter(x => x.id !== id).map(x => `<option value="${x.id}">${esc(x.name)}</option>`).join('');
  $('coMerge').onchange = async e => {
    const rid = Number(e.target.value); if (!rid) return;
    const other = all.find(x => x.id === rid);
    if (!await showConfirm({ title: '会社を統合する', message: `「${other.name}」の書類・図面・別名を「${co.name}」に移します。同じ図番は1つにまとめます。あとから元に戻せます。`, okLabel: '統合する' })) { e.target.value = ''; return; }
    try {
      const res = await post('/api/zumen/companies/merge', { keep: id, remove: rid, actor: Z.me || null });
      COMPANIES = null;
      showToast('統合しました', { type: 'success', duration: 8000, action: { label: '元に戻す', onClick: () => undoEvent(res.event_id) } });
      drawCompanyList(); openCompany(id);
    } catch (err) { showToast(err.message, { type: 'error' }); }
  };
}

function drawingTile(dw, badge = '') {
  return `<button class="zg-tile" data-open-drawing="${dw.id}">${badge ? `<span class="zg-badge">${esc(badge)}</span>` : ''}
    <div class="zg-img">${dw.file_id ? thumb(dw.file_id, dw.page_no || 1) : `<div class="zg-noimg">${ico('drawing', 22)}<small>図面なし</small></div>`}</div>
    <div class="zg-no">${esc(dw.drawing_no)} <span class="zg-rev">${esc(dw.rev || (dw.file_id ? '初版' : ''))}</span></div>
    <div class="zg-sub">${esc([dw.company, dw.name].filter(Boolean).join('　'))}</div>
    <div class="zg-sub2">${esc([dw.material, dw.thickness ? `t${dw.thickness}` : ''].filter(Boolean).join(' '))}${dw.revs > 1 ? `<span>全${dw.revs}版</span>` : ''}${dw.children ? `<span>部品 ${dw.children}</span>` : ''}</div>
  </button>`;
}

// ── 図面（一覧と検索） ──
const DW = { q: storeGet('zumen.dq') || '', company: '', material: '', mode: 'drawings' };
ZVIEWS.drawings = async () => {
  const cos = await companyList();
  VP().innerHTML = `
    <div class="zs-bar">
      <div class="search-field zs-q">${ico('search', 15)}<input type="search" id="dwQ" placeholder="図番・品名・材質・会社名、書類内の文字で検索" value="${esc(DW.q)}"></div>
      <select class="email-input compact" id="dwCo"><option value="">すべての会社</option>${cos.map(c => `<option value="${c.id}" ${String(c.id) === DW.company ? 'selected' : ''}>${esc(c.name)}</option>`).join('')}</select>
      <div class="seg" id="dwMode"><button data-m="drawings" class="${DW.mode === 'drawings' ? 'is-on' : ''}">図番</button><button data-m="docs" class="${DW.mode === 'docs' ? 'is-on' : ''}">書類の本文まで</button>
        <button data-m="ask" class="${DW.mode === 'ask' ? 'is-on' : ''}">${ico('sparkle', 12)} AIに質問</button></div>
      <label class="btn-sm" title="お手元の図面やスマホで撮った写真と、形が似た図面を探します（登録はされません）">${ico('image', 13)} 画像で探す<input type="file" id="dwImg" accept="application/pdf,image/*" hidden></label>
    </div>
    <div id="dwBody"><div class="empty-text">読み込み中…</div></div>`;
  $('dwQ').oninput = debounce(e => { DW.q = e.target.value; storeSet('zumen.dq', DW.q); if (DW.mode !== 'ask') drawDrawings(); }, 250);
  $('dwQ').onkeydown = e => { if (e.key === 'Enter' && DW.mode === 'ask') askAi(); };
  $('dwImg').onchange = async e => {
    const f = e.target.files[0]; if (!f) return; e.target.value = '';
    $('dwBody').innerHTML = '<div class="empty-text">画像を比較しています…</div>';
    const fd = new FormData(); fd.append('file', f);
    try {
      const r = await api('/api/zumen/search-image', { method: 'POST', body: fd });
      $('dwBody').innerHTML = `<div class="zr-label">「${esc(f.name)}」に形が似た図面</div>` + (r.items.length ? `<div class="zg">${r.items.map(x => drawingTile(x, `${x.score}%`)).join('')}</div>` : '<div class="boss-empty">似た図面は見つかりませんでした</div>');
      $('dwBody').querySelectorAll('[data-open-drawing]').forEach(b => b.onclick = () => setView('drawing', { id: Number(b.dataset.openDrawing) }));
    } catch (err) { $('dwBody').innerHTML = `<div class="boss-empty">${esc(err.message)}</div>`; }
  };
  $('dwCo').onchange = e => { DW.company = e.target.value; drawDrawings(); };
  $('dwMode').querySelectorAll('button').forEach(b => b.onclick = () => { DW.mode = b.dataset.m; $('dwMode').querySelectorAll('button').forEach(x => x.classList.toggle('is-on', x === b)); drawDrawings(); });
  drawDrawings();
};

async function askAi() {
  const q = DW.q.trim(), body = $('dwBody');
  if (!q) { showToast('質問を入力してください', { type: 'warning' }); return; }
  body.innerHTML = `<div class="zai"><div class="zai-q">${ico('user', 14)} ${esc(q)}</div><div class="zai-a"><span class="zm-spin"></span> 書類と図面を調べています…</div></div>`;
  try {
    const r = await post('/api/zumen/ask', { q });
    const html = esc(r.answer).replace(/\[(書類|図面)#(\d+)\]/g, (_, k, n) => `<a class="zai-ref" href="/zumen.html?${k === '書類' ? 'doc' : 'drawing'}=${n}">${k}#${n}</a>`).replace(/\n/g, '<br>');
    body.innerHTML = `<div class="zai"><div class="zai-q">${ico('user', 14)} ${esc(q)}</div><div class="zai-a">${ico('sparkle', 14)}<div>${html}</div></div>
      ${r.sources.length ? `<div class="zr-label">回答の根拠になった書類・図面</div>${r.sources.map(x => `<a class="zr-rel" href="/zumen.html?${x.type === 'doc' ? 'doc' : 'drawing'}=${x.id}">${ico(x.type === 'doc' ? 'doc' : 'drawing', 13)} ${esc(x.label)}</a>`).join('')}` : ''}
      <div class="zai-note">AIの回答は、必ず根拠の書類で確認してから使ってください</div></div>`;
  } catch (e) { body.innerHTML = `<div class="boss-empty">${esc(e.message)}</div>`; }
}

async function drawDrawings() {
  const body = $('dwBody');
  if (DW.mode === 'ask') {
    $('dwQ').placeholder = '例：テスト工業の WM-1203 の前回の単価は？ ／ SUS 2mm の筐体を作ったことは？（Enterで質問）';
    body.innerHTML = `<div class="zai-empty">${ico('sparkle', 22)}<b>書類と図面の内容から回答します</b><span>図番・会社名・品名・材質など、書類に書かれている言葉を使って質問してください。回答には根拠となった書類が添えられます。</span>
      <div class="zai-ex">${['WM-1203 の前回の単価は？', '北摂精機の今月の注文は？', '不具合メモがある図面は？'].map(x => `<button class="chip-btn" data-ex="${esc(x)}">${esc(x)}</button>`).join('')}</div></div>`;
    body.querySelectorAll('[data-ex]').forEach(b => b.onclick = () => { $('dwQ').value = DW.q = b.dataset.ex; askAi(); });
    return;
  }
  $('dwQ').placeholder = '図番・品名・材質・会社名、書類内の文字で検索';
  if (DW.mode === 'docs') {
    const r = await api(`/api/zumen/search?q=${encodeURIComponent(DW.q)}${DW.company ? `&company_id=${DW.company}` : ''}`);
    body.innerHTML = r.docs.length ? `<table class="zr-table ro zc-docs"><thead><tr><th></th><th>種類</th><th>会社</th><th>日付</th><th>番号</th><th>件名</th><th class="num">金額</th></tr></thead><tbody>
      ${r.docs.map(d => `<tr data-open-doc="${d.id}"><td>${thumb(d.file_id, d.page_from, 'sm')}</td><td>${kindBadge(d.kind)}</td><td>${esc(d.company || '')}</td><td>${fmtDate(d.doc_date)}</td><td>${esc(d.doc_no || '')}</td><td>${esc(d.title || d.filename)}</td><td class="num">${yen(d.amount)}</td></tr>`).join('')}</tbody></table>`
      : `<div class="boss-empty">${DW.q ? '見つかりませんでした' : '書類はまだありません'}</div>`;
    body.querySelectorAll('[data-open-doc]').forEach(tr => tr.onclick = () => setView('doc', { id: Number(tr.dataset.openDoc) }));
    return;
  }
  const r = await api(`/api/zumen/drawings?q=${encodeURIComponent(DW.q)}${DW.company ? `&company_id=${DW.company}` : ''}`);
  body.innerHTML = r.drawings.length ? `<div class="zg">${r.drawings.map(drawingTile).join('')}</div>`
    : `<div class="boss-empty">${DW.q ? '図番では見つかりませんでした。「書類の本文まで」に切り替えると、図面に書かれた文字も検索します' : 'まだ図面はありません。図面のPDFを追加して確定すると、ここに表示されます'}</div>`;
  body.querySelectorAll('[data-open-drawing]').forEach(b => b.onclick = () => setView('drawing', { id: Number(b.dataset.openDrawing) }));
}

// ── 図面1件 ──
const DT = { tab: 'revs' };
ZVIEWS.drawing = async ({ id, rev }) => {
  history.replaceState(null, '', `/zumen.html?drawing=${id}`);
  VP().innerHTML = '<div class="empty-text">読み込み中…</div>';
  let r;
  try { r = await api(`/api/zumen/drawings/${id}`); } catch (e) { VP().innerHTML = `<div class="empty-text">${esc(e.message)}</div>`; return; }
  const dw = r.drawing, cur = r.current;
  if (dw.id !== id) { id = dw.id; history.replaceState(null, '', `/zumen.html?drawing=${id}`); }   // まとめた図番は、まとめ先を開く
  const childNewer = r.parents.some(p => p.child_newer);
  const tabs = [['revs', '版の履歴', r.revisions.length], ['docs', '書類', r.docs.length], ['prices', '単価', r.prices.length], ['notes', 'メモ・写真', r.notes.length],
    ['bom', '組立・部品', r.children.length + r.parents.length], ['similar', '似た図面', '']];
  if (!tabs.some(t => t[0] === DT.tab)) DT.tab = 'revs';
  const noteKinds = Z.meta.note_kinds.filter(k => k !== '現場写真');
  const panes = {
    revs: r.revisions.length ? r.revisions.map((rv, i) => `<div class="zt-rev ${i === 0 ? 'is-cur' : ''}">
        ${thumb(rv.file_id, rv.page_no || 1, 'sm')}
        <div class="zt-rev-main"><div><b>${esc(rv.rev || '初版')}</b>${i === 0 ? '<span class="zr-st zst-good">最新</span>' : '<span class="zr-st zst-old">旧版</span>'}</div>
          <small>${fmtDate(rv.doc_date || rv.created_at)} ${esc(rv.confirmed_by || '')}　${esc(rv.filename)}</small>${rv.change_note ? `<div class="zt-note">${esc(rv.change_note)}</div>` : ''}</div>
        <div class="zt-rev-act"><button class="btn-link" data-show-rev="${i}">表示</button>${i < r.revisions.length - 1 ? `<button class="btn-link" data-diff-idx="${i}">前の版と比べる</button>` : ''}
          <button class="btn-link" data-edit-rev="${rv.id}" data-rev="${esc(rv.rev || '')}">版の表記を修正</button><a class="btn-link" href="/zumen.html?doc=${rv.doc_id}">書類</a></div></div>`).join('')
      : '<div class="boss-empty">まだ図面の版はありません（注文書や見積書の明細から作られた図番です）。図面のPDFを追加すると版が登録されます</div>',
    docs: r.docs.length ? r.docs.map(d => `<a class="zr-rel" href="/zumen.html?doc=${d.id}">${kindBadge(d.kind)} ${esc(d.doc_no || '')} ${fmtDate(d.doc_date)} ${d.amount != null ? yen(d.amount) : ''} ${d.due_on ? `納期 ${fmtMD(d.due_on)}` : ''}</a>`).join('')
      + (r.deals.length ? `<div class="zr-label" style="margin-top:12px">案件</div>${r.deals.map(x => `<a class="zr-rel" href="/sales.html?deal=${x.id}">#${x.id} ${esc(x.title)} <span class="stage-badge" data-stage="${esc(x.stage)}">${esc(x.stage)}</span></a>`).join('')}` : '')
      : '<div class="boss-empty">まだありません</div>',
    prices: r.prices.length ? `<table class="zr-table ro"><thead><tr><th>日付</th><th>書類</th><th>版</th><th class="num">数量</th><th class="num">単価</th><th class="num">金額</th></tr></thead><tbody>
        ${r.prices.map(p => `<tr data-open-doc="${p.doc_id}"><td>${fmtDate(p.doc_date)}</td><td>${kindBadge(p.kind)} ${esc(p.doc_no || '')}</td><td>${esc(p.rev || '')}</td><td class="num">${p.qty ?? ''}</td><td class="num">${yen(p.unit_price)}</td><td class="num">${yen(p.amount)}</td></tr>`).join('')}</tbody></table>`
      : '<div class="boss-empty">注文書・見積書の明細にこの図番が出てくると、単価の履歴がたまります</div>',
    notes: `<div class="zt-note-form"><select class="email-input compact" id="nKind">${noteKinds.map(k => `<option>${k}</option>`).join('')}</select>
        <input class="email-input compact" id="nBody" placeholder="加工のコツ・不具合の内容など">
        <button class="btn-sm" id="nAdd">記録する</button>
        <label class="btn-sm zt-photo">${ico('camera', 13)} 写真<input type="file" accept="image/*" capture="environment" id="nPhoto" hidden></label></div>
      ${r.notes.map(n => `<div class="zt-memo k-${esc(n.kind)}">${n.photo_file_id ? `<img src="${ZV.pageUrl(n.photo_file_id, 1, 'thumb')}" data-photo="${n.photo_file_id}">` : ''}
        <div><span class="zt-mk">${esc(n.kind)}</span> ${esc(n.body || '')}<small>${fmtDate(n.created_at)} ${esc(n.author || '')}</small></div>
        <button class="zr-x" data-del-note="${n.id}" title="消す">×</button></div>`).join('') || '<div class="boss-empty">まだメモはありません。不具合を記録しておくと、次に同じ図番の注文が来たときにお知らせします</div>'}`,
    bom: `${r.parents.length ? `<div class="zr-label">この図番を使う組立図</div>${r.parents.map(p => `<button class="zr-rel" data-open-drawing="${p.id}">${ico('layers', 13)} ${esc(p.drawing_no)} ${esc(p.name || '')} <small>${esc(p.rev || '初版')}　×${p.qty ?? 1}</small>${p.child_newer ? '<span class="zr-st zst-warn">部品のほうが新しい版です</span>' : ''}</button>`).join('')}` : ''}
      <div class="zr-label" style="margin-top:10px">この組立図の部品</div>
      ${r.children.map(ch => `<div class="zt-child"><button class="zr-rel" data-open-drawing="${ch.id}">${ico('drawing', 13)} ${esc(ch.drawing_no)} ${esc(ch.name || '')} <small>${esc(ch.rev || '版なし')}</small></button><span>×${ch.qty ?? 1}</span><button class="zr-x" data-del-bom="${ch.bom_id}">×</button></div>`).join('') || '<div class="boss-empty">部品の登録はありません</div>'}
      <div class="zt-note-form"><input class="email-input compact" id="bNo" placeholder="部品の図番"><input class="email-input compact" id="bName" placeholder="品名"><input type="number" class="email-input compact w-s" id="bQty" value="1" min="1"><button class="btn-sm" id="bAdd">部品を追加</button></div>`,
    similar: `<div class="zr-label">形が似た図面</div><div id="simImg"><div class="empty-text">探しています…</div></div>
      <div class="zr-label" style="margin-top:12px">材質・板厚・品名が近い図面</div>
      ${r.similar.length ? `<div class="zg">${r.similar.map(drawingTile).join('')}</div>` : '<div class="boss-empty">条件の近い図面はありません</div>'}`,
  };
  VP().innerHTML = `
    <div class="zd-page">
      <div class="zd-top-row"><button class="btn-link" onclick="history.length > 1 ? history.back() : setView('drawings')">← 戻る</button></div>
      <div class="zr">
        <div class="zr-view" id="dwView">${cur ? '' : `<div class="detail-empty"><p class="detail-empty-text">図面のPDFはまだありません</p></div>`}</div>
        <div class="zr-form">
          <div class="zt-head"><input class="zt-no" id="dwNo" value="${esc(dw.drawing_no)}"><span class="zt-rev-big">${esc(cur ? cur.rev || '初版' : '版なし')}</span></div>
          <a class="zt-co" href="/zumen.html?company=${dw.company_id}">${ico('folder', 13)} ${esc(dw.company)}</a>
          ${rev ? (cur && cur.id === rev ? `<div class="zr-warn ok">${ico('check', 14)}<span>読み取った紙は<b>最新版（${esc(cur.rev || '初版')}）</b>です。このまま製作できます</span></div>`
            : `<div class="zr-warn bad big">${ico('alert', 16)}<span><b>この紙は旧版です。</b>最新版は ${esc(cur ? cur.rev || '初版' : '—')} です。最新の図面に差し替えてください</span></div>`) : ''}
          <div class="zt-acts">
            ${cur ? `<a class="btn-sm" href="/zumen-print.html?drawing=${id}" target="_blank">${ico('printer', 13)} QRコード付きで印刷</a>` : ''}
            ${cur ? `<button class="btn-sm" id="shareBtn">${ico('share', 13)} 社外に共有</button>` : ''}
          </div>
          ${childNewer ? '<div class="zr-warn warn">この図番を使う組立図より、部品の図面のほうが新しい版になっています</div>' : ''}
          <div class="zr-grid zt-fields">
            <label class="span2">品名<input class="email-input compact" data-dwf="name" value="${esc(dw.name || '')}"></label>
            <label>材質<input class="email-input compact" data-dwf="material" value="${esc(dw.material || '')}"></label>
            <label>板厚<input class="email-input compact" data-dwf="thickness" value="${esc(dw.thickness || '')}"></label>
            <label class="span2">表面処理<input class="email-input compact" data-dwf="surface" value="${esc(dw.surface || '')}"></label>
            <label class="span2">タグ<input class="email-input compact" data-dwf="tags" value="${esc(dw.tags || '')}" placeholder="例：リピート品、曲げ注意"></label>
          </div>
          <nav class="dtabs dtabs-wrap">${tabs.map(([k, l, n]) => `<button class="dtab ${k === DT.tab ? 'is-active' : ''}" data-ttab="${k}">${l}${n ? `<span class="dtab-n">${n}</span>` : ''}</button>`).join('')}</nav>
          ${tabs.map(([k]) => `<div class="dpane ${k === DT.tab ? 'is-active' : ''}" data-tpane="${k}">${panes[k]}</div>`).join('')}
        </div>
      </div>
    </div>`;
  let viewer = null;
  if (cur) viewer = zvCreate($('dwView'), { fileId: cur.file_id, page: cur.page_no || 1, pageFrom: cur.page_no || 1, pageTo: cur.page_no || 1, title: `${dw.drawing_no} ${cur.rev || ''}`, annotate: true });
  if ($('shareBtn')) $('shareBtn').onclick = () => openShare('drawing', id, `${dw.drawing_no} ${dw.name || ''}`);
  api(`/api/zumen/drawings/${id}/similar-image`).then(x => {
    const el = $('simImg'); if (!el) return;
    el.innerHTML = x.items.length ? `<div class="zg">${x.items.map(it => drawingTile(it, `${it.score}%`)).join('')}</div>` : `<div class="boss-empty">${esc(x.note || '形が似た図面はありません')}</div>`;
    el.querySelectorAll('[data-open-drawing]').forEach(b => b.onclick = () => setView('drawing', { id: Number(b.dataset.openDrawing) }));
  }).catch(() => {});
  const root = VP();
  root.querySelectorAll('[data-ttab]').forEach(b => b.onclick = () => {
    DT.tab = b.dataset.ttab;
    root.querySelectorAll('[data-ttab]').forEach(x => x.classList.toggle('is-active', x === b));
    root.querySelectorAll('[data-tpane]').forEach(x => x.classList.toggle('is-active', x.dataset.tpane === DT.tab));
  });
  root.querySelectorAll('[data-dwf]').forEach(inp => inp.onchange = async () => { await put(`/api/zumen/drawings/${id}`, { [inp.dataset.dwf]: inp.value }); showToast('保存しました', { type: 'success', duration: 1000 }); });
  $('dwNo').onchange = async e => { try { await put(`/api/zumen/drawings/${id}`, { drawing_no: e.target.value }); showToast('図番を修正しました', { type: 'success' }); } catch (err) { showToast(err.message, { type: 'error' }); e.target.value = dw.drawing_no; } };
  root.querySelectorAll('[data-show-rev]').forEach(b => b.onclick = () => {
    const rv = r.revisions[Number(b.dataset.showRev)];
    $('dwView').innerHTML = '';
    zvCreate($('dwView'), { fileId: rv.file_id, page: rv.page_no || 1, pageFrom: rv.page_no || 1, pageTo: rv.page_no || 1, stamp: Number(b.dataset.showRev) ? `旧版（${rv.rev || '初版'}）` : null, annotate: !Number(b.dataset.showRev) });
  });
  root.querySelectorAll('[data-diff-idx]').forEach(b => b.onclick = () => {
    const i = Number(b.dataset.diffIdx), nw = r.revisions[i], od = r.revisions[i + 1];
    zvDiff({ fileId: od.file_id, page: od.page_no || 1, label: od.rev || '初版' }, { fileId: nw.file_id, page: nw.page_no || 1, label: nw.rev || '初版' });
  });
  root.querySelectorAll('[data-edit-rev]').forEach(b => b.onclick = async () => {
    const v = prompt('版の表記を入力してください（例：B、改2、第3版。空欄にすると初版になります）', b.dataset.rev);
    if (v == null) return;
    await put(`/api/zumen/revisions/${b.dataset.editRev}`, { rev: v });
    ZVIEWS.drawing({ id });
  });
  root.querySelectorAll('[data-open-doc]').forEach(tr => tr.onclick = () => setView('doc', { id: Number(tr.dataset.openDoc) }));
  root.querySelectorAll('[data-open-drawing]').forEach(b => b.onclick = () => setView('drawing', { id: Number(b.dataset.openDrawing) }));
  root.querySelectorAll('[data-photo]').forEach(img => img.onclick = () => zvBig({ fileId: Number(img.dataset.photo), pages: 1, title: '写真' }));
  $('nAdd').onclick = async () => {
    const body = $('nBody').value.trim(); if (!body) return;
    await post('/api/zumen/notes', { drawing_id: id, kind: $('nKind').value, body, author: Z.me || null });
    DT.tab = 'notes'; ZVIEWS.drawing({ id });
  };
  $('nPhoto').onchange = async e => {
    const f = e.target.files[0]; if (!f) return;
    const fd = new FormData(); fd.append('file', f); fd.append('drawing_id', id); fd.append('body', $('nBody').value.trim()); if (Z.me) fd.append('author', Z.me);
    try { await api('/api/zumen/photo', { method: 'POST', body: fd }); showToast('写真を付けました', { type: 'success' }); DT.tab = 'notes'; ZVIEWS.drawing({ id }); } catch (err) { showToast(err.message, { type: 'error' }); }
  };
  root.querySelectorAll('[data-del-note]').forEach(b => b.onclick = async () => { await api(`/api/zumen/notes/${b.dataset.delNote}`, { method: 'DELETE' }); ZVIEWS.drawing({ id }); });
  $('bAdd').onclick = async () => {
    const no = $('bNo').value.trim(); if (!no) return;
    try { await post(`/api/zumen/drawings/${id}/bom`, { child_no: no, name: $('bName').value.trim() || null, qty: Number($('bQty').value) || 1 }); DT.tab = 'bom'; ZVIEWS.drawing({ id }); }
    catch (err) { showToast(err.message, { type: 'error' }); }
  };
  root.querySelectorAll('[data-del-bom]').forEach(b => b.onclick = async () => { await api(`/api/zumen/bom/${b.dataset.delBom}`, { method: 'DELETE' }); ZVIEWS.drawing({ id }); });
};

// ── 納期（カレンダー・ガント） ──
const SC = { mode: storeGet('zumen.scmode') || 'gantt', month: null };
const STATE_LABEL = { late: '遅れ', soon: '納期まで7日以内', ok: '', nodue: '納期未設定', shipped: '出荷済み' };
ZVIEWS.schedule = async () => {
  if (!SC.month) { const t = new Date(); SC.month = new Date(t.getFullYear(), t.getMonth(), 1); }
  const f = new Date(SC.month.getFullYear(), SC.month.getMonth() - 1, 1), to = new Date(SC.month.getFullYear(), SC.month.getMonth() + 3, 0);
  const iso = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const r = await api(`/api/zumen/schedule?from=${iso(f)}&to=${iso(to)}`);
  const late = r.rows.filter(x => x.state === 'late').length, soon = r.rows.filter(x => x.state === 'soon').length, nodue = r.rows.filter(x => x.state === 'nodue').length;
  VP().innerHTML = `
    <div class="zs-bar">
      <div class="seg" id="scMode"><button data-m="gantt" class="${SC.mode === 'gantt' ? 'is-on' : ''}">工程表</button><button data-m="month" class="${SC.mode === 'month' ? 'is-on' : ''}">カレンダー</button></div>
      <button class="btn-sm" id="scPrev">‹</button><b class="sc-month">${SC.month.getFullYear()}年${SC.month.getMonth() + 1}月</b><button class="btn-sm" id="scNext">›</button><button class="btn-link" id="scToday">今日</button>
      <div class="sc-sum">${late ? `<span class="pill bad">遅れ ${late}</span>` : ''}${soon ? `<span class="pill warn">納期まで7日以内 ${soon}</span>` : ''}${nodue ? `<span class="pill warn">納期未設定 ${nodue}</span>` : ''}</div>
    </div>
    ${weekSteps(r)}
    <div id="scBody"></div>
    <div class="sc-foot">注文書を確定すると、この画面と共有カレンダーに納期が表示されます。◆は納期から逆算した工程の予定です。押すと完了になります。日数は「設定」で変更できます。</div>`;
  $('scMode').querySelectorAll('button').forEach(b => b.onclick = () => { SC.mode = b.dataset.m; storeSet('zumen.scmode', SC.mode); ZVIEWS.schedule(); });
  $('scPrev').onclick = () => { SC.month = new Date(SC.month.getFullYear(), SC.month.getMonth() - 1, 1); ZVIEWS.schedule(); };
  $('scNext').onclick = () => { SC.month = new Date(SC.month.getFullYear(), SC.month.getMonth() + 1, 1); ZVIEWS.schedule(); };
  $('scToday').onclick = () => { SC.month = null; ZVIEWS.schedule(); };
  const open = x => x.deal_id ? location.href = `/sales.html?deal=${x.deal_id}` : setView('doc', { id: x.doc_id || x.order_ids[0] });
  if (SC.mode === 'month') drawMonth(r, open); else drawGantt(r, open);
};

function drawGantt(r, open) {
  const start = new Date(SC.month.getFullYear(), SC.month.getMonth(), 1);
  const days = 70;
  const dayIdx = s => Math.round((new Date(s + 'T00:00:00') - start) / 864e5);
  const today = dayIdx(r.today);
  const head = [];
  for (let i = 0; i < days; i++) {
    const d = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
    head.push(`<div class="g-day ${d.getDay() === 0 || d.getDay() === 6 ? 'we' : ''} ${d.getDate() === 1 ? 'm1' : ''}">${d.getDate() === 1 ? `<b>${d.getMonth() + 1}月</b>` : ''}${d.getDate()}</div>`);
  }
  const rows = r.rows.filter(x => x.state !== 'shipped' || (x.due && dayIdx(x.due) >= 0));
  $('scBody').innerHTML = `<div class="gantt" style="--days:${days}">
      <div class="g-row g-head"><div class="g-label"></div><div class="g-track">${head.join('')}</div></div>
      ${rows.map((x, i) => {
        const s = Math.max(0, x.start ? dayIdx(x.start) : (x.due ? dayIdx(x.due) - 7 : 0)), e = x.due ? dayIdx(x.due) : s;
        const vis = e >= 0 && s < days;
        const ms = (x.milestones || []).map(m => { const k = dayIdx(m.plan); return k >= 0 && k < days ? `<button class="g-ms ${m.done ? 'done' : m.late ? 'late' : ''}" style="left:calc(${k} * var(--d) + var(--d) / 2)" data-ms="${i}|${esc(m.label)}|${m.done ? 1 : 0}" title="${esc(m.label)}　予定 ${fmtMD(m.plan)}${m.done ? `（完了 ${fmtMD(m.done)}）` : m.late ? '（遅れ）' : ''}　クリックで完了にする"></button>` : ''; }).join('');
        return `<div class="g-row" data-i="${i}"><div class="g-label"><b>${esc(x.company || '')}</b><span>${esc(x.title || '')}</span>
            ${x.deal_id ? `<a class="g-print" href="/zumen-print.html?deal=${x.deal_id}" target="_blank" title="製作指示書を印刷">${ico('printer', 12)}</a>` : ''}
            ${x.stage ? `<span class="stage-badge" data-stage="${esc(x.stage)}">${esc(x.stage)}</span>` : ''}${STATE_LABEL[x.state] ? `<span class="pill ${x.state === 'late' ? 'bad' : x.state === 'shipped' ? 'good' : 'warn'}">${STATE_LABEL[x.state]}</span>` : ''}</div>
          <div class="g-track">${vis ? `<div class="g-bar s-${x.state}" style="left:calc(${Math.max(0, s)} * var(--d));width:calc(${Math.max(1, Math.min(days, e + 1) - Math.max(0, s))} * var(--d))" title="${esc(fmtDate(x.start))} 〜 納期 ${esc(fmtDate(x.due))}">
            <span>${x.due ? `納期 ${fmtMD(x.due)}` : ''}</span></div>` : ''}${ms}${today >= 0 && today < days ? `<div class="g-today" style="left:calc(${today} * var(--d))"></div>` : ''}</div></div>`;
      }).join('') || '<div class="boss-empty" style="margin:16px">この期間の納期はありません</div>'}
    </div>`;
  $('scBody').querySelectorAll('[data-i]').forEach(el => el.onclick = ev => { if (ev.target.closest('.g-ms, .g-print')) return; open(rows[Number(el.dataset.i)]); });
  $('scBody').querySelectorAll('[data-ms]').forEach(b => b.onclick = async ev => {
    ev.stopPropagation();
    const [i, label, done] = b.dataset.ms.split('|');
    await post('/api/zumen/schedule/done', { deal_id: rows[Number(i)].deal_id, label, done: done !== '1' });
    showToast(done !== '1' ? `「${label}」を完了にしました` : `「${label}」を未完了に戻しました`, { duration: 1500 });
    ZVIEWS.schedule();
  });
}

function weekSteps(r) {
  const today = r.today, wk = new Date(Date.parse(today) + 7 * 864e5).toISOString().slice(0, 10);
  const items = [];
  r.rows.forEach(x => (x.milestones || []).forEach(m => { if (!m.done && m.plan <= wk && x.state !== 'shipped') items.push({ x, m }); }));
  items.sort((a, b) => a.m.plan.localeCompare(b.m.plan));
  if (!items.length) return '';
  return `<div class="sc-week"><div class="zr-label">${ico('clock', 13)} 今週中の工程（${items.length}）</div><div class="sc-week-list">${items.slice(0, 12).map(({ x, m }) => `
    <div class="sc-step ${m.late ? 'late' : ''}"><span class="sc-step-d">${fmtMD(m.plan)}</span><b>${esc(m.label)}</b><span>${esc(x.company || '')} ${esc(x.title || '')}</span>${m.late ? '<em>遅れ</em>' : ''}</div>`).join('')}</div></div>`;
}

function drawMonth(r, open) {
  const y = SC.month.getFullYear(), m = SC.month.getMonth();
  const first = new Date(y, m, 1), startDow = first.getDay(), n = new Date(y, m + 1, 0).getDate();
  const by = {};
  r.rows.forEach((x, i) => { if (x.due) (by[x.due] = by[x.due] || []).push([x, i]); });
  const cells = [];
  for (let i = 0; i < startDow; i++) cells.push('<div class="mc-cell is-off"></div>');
  for (let d = 1; d <= n; d++) {
    const key = `${y}-${pad(m + 1)}-${pad(d)}`;
    cells.push(`<div class="mc-cell ${key === r.today ? 'is-today' : ''}"><div class="mc-d">${d}</div>${(by[key] || []).map(([x, i]) => `<button class="mc-item s-${x.state}" data-i="${i}">${esc(x.company || '')} ${esc(x.title || '')}</button>`).join('')}</div>`);
  }
  $('scBody').innerHTML = `<div class="mcal"><div class="mc-dow">${['日', '月', '火', '水', '木', '金', '土'].map(w => `<div>${w}</div>`).join('')}</div><div class="mc-grid">${cells.join('')}</div></div>`;
  $('scBody').querySelectorAll('[data-i]').forEach(el => el.onclick = () => open(r.rows[Number(el.dataset.i)]));
}

// ── 現場（タブレット） ──
ZVIEWS.site = async ({ deal } = {}) => {
  VP().innerHTML = '<div class="empty-text">読み込み中…</div>';
  const r = await api('/api/zumen/site');
  VP().innerHTML = `<div class="st-wrap">
    <div class="st-head"><b>いま製作するもの</b><span>${r.items.length}件</span><button class="btn-sm" id="stReload">更新</button></div>
    ${r.hidden_old ? `<div class="zr-warn warn" style="margin-bottom:10px">${ico('alert', 14)}<span>納期から60日以上たっても出荷済みになっていない仕事が${r.hidden_old}件あります（この一覧には表示していません）。<a href="/zumen.html?view=schedule">「納期」の画面</a>で出荷済みにしてください</span></div>` : ''}
    ${r.items.map((x, i) => `<div class="st-card s-${x.state} ${deal && x.deal_id === deal ? 'is-focus' : ''}" ${x.deal_id ? `id="st-${x.deal_id}"` : ''}>
      <div class="st-top"><div><div class="st-co">${esc(x.company || '')}</div><div class="st-title">${esc(x.title || '')}</div></div>
        <div class="st-due ${x.days_left != null && x.days_left < 0 ? 'bad' : x.days_left != null && x.days_left <= 7 ? 'warn' : ''}">${x.due ? `納期 ${fmtMD(x.due)}<small>${x.days_left < 0 ? `${-x.days_left}日遅れ` : x.days_left === 0 ? '今日' : `あと${x.days_left}日`}</small>` : '納期未設定'}</div></div>
      ${x.lines.length ? `<div class="st-lines">${x.lines.map(l => `<span>${esc(l.drawing_no || '')} ${esc(l.name || '')}${l.qty ? ` <b>×${l.qty}</b>` : ''}</span>`).join('')}</div>` : ''}
      <div class="st-dws">${x.drawings.map(dw => `<button class="st-dw" data-site-dw="${i}:${dw.id}">
          ${dw.file_id ? thumb(dw.file_id, dw.page_no || 1) : `<div class="zg-noimg">${ico('drawing', 26)}</div>`}
          <div class="st-dw-no">${esc(dw.drawing_no)} <b>${esc(dw.rev || '初版')}</b></div>
          ${dw.rev_mismatch ? `<div class="st-flag bad">注文書は${esc(dw.ordered_rev)}版</div>` : ''}${dw.bad_notes ? `<div class="st-flag warn">不具合メモ ${dw.bad_notes}</div>` : ''}</button>`).join('') || '<div class="boss-empty">図面がまだ紐づいていません</div>'}</div>
      ${(x.milestones || []).length ? `<div class="st-steps">${x.milestones.map(m => `<span class="${m.done ? 'done' : m.late ? 'late' : ''}">${esc(m.label)} ${fmtMD(m.plan)}${m.done ? ' 完了' : ''}</span>`).join('')}</div>` : ''}
      <div class="st-acts">${x.deal_id ? `<a class="st-btn" href="/zumen-print.html?deal=${x.deal_id}" target="_blank">${ico('printer', 16)} 製作指示書</a>` : ''}${x.deal_id ? `${x.started_at ? '<span class="pill good">製作中</span>' : `<button class="st-btn" data-site-act="start:${x.deal_id}">製作を始める</button>`}
        <button class="st-btn primary" data-site-act="ship:${x.deal_id}">出荷済みにする</button>` : ''}</div>
    </div>`).join('') || '<div class="boss-empty" style="margin:20px">いま製作するものはありません</div>'}</div>`;
  $('stReload').onclick = () => ZVIEWS.site();
  if (deal && $(`st-${deal}`)) $(`st-${deal}`).scrollIntoView({ block: 'center' });
  VP().querySelectorAll('[data-site-dw]').forEach(b => b.onclick = () => {
    const [i, did] = b.dataset.siteDw.split(':').map(Number);
    siteDrawing(r.items[i], r.items[i].drawings.find(x => x.id === did));
  });
  VP().querySelectorAll('[data-site-act]').forEach(b => b.onclick = async () => {
    const [action, deal] = b.dataset.siteAct.split(':');
    if (action === 'ship' && !await showConfirm({ title: '出荷済みにする', message: 'この案件を出荷済みにして、現場の一覧から外します。', okLabel: '出荷済みにする' })) return;
    await post('/api/zumen/site/state', { deal_id: Number(deal), action });
    showToast(action === 'ship' ? '出荷済みにしました' : '製作中にしました', { type: 'success', action: action === 'ship' ? { label: '元に戻す', onClick: async () => { await post('/api/zumen/site/state', { deal_id: Number(deal), action: 'unship' }); ZVIEWS.site(); } } : null });
    ZVIEWS.site();
  });
};

function siteDrawing(item, dw) {
  const box = $('zmModalBox');
  $('zmModal').style.display = 'flex';
  box.className = 'zm-modal-box zm-modal-big site-modal';
  box.innerHTML = `<div class="zm-modal-head"><b>${esc(dw.drawing_no)}　<span class="st-revtag">${esc(dw.rev || '初版')}（最新）</span>　${esc(dw.name || '')}</b>
      <div class="zd-tools"><label class="st-btn">${ico('camera', 16)} 写真<input type="file" accept="image/*" capture="environment" id="stPhoto" hidden></label>
      <button class="st-btn" id="stMemo">メモ</button><button class="dupe-modal-close" id="zmModalClose">×</button></div></div>
    ${dw.rev_mismatch ? `<div class="zr-warn bad" style="margin:0 12px">注文書の版は${esc(dw.ordered_rev)}、図面の最新は${esc(dw.rev || '初版')}です。事務所に確認してから製作してください</div>` : ''}
    <div class="st-memo" id="stMemoBox" hidden>
      <div class="seg" id="stMemoKind"><button data-k="加工メモ" class="is-on">加工メモ</button><button data-k="不具合" id="stMemoBad">不具合</button><button data-k="ノウハウ">ノウハウ</button></div>
      <textarea class="email-input" id="stMemoBody" rows="2" maxlength="1000" placeholder="気づいたことを書いてください（次に同じ図番の注文が来たときにお知らせします）"></textarea>
      <div class="st-memo-acts"><button class="st-btn" id="stMemoCancel">キャンセル</button><button class="st-btn st-btn-primary" id="stMemoSave">保存</button></div>
    </div>
    <div class="zm-modal-body" id="stHost"></div>`;
  $('zmModalClose').onclick = zmCloseModal;
  if (dw.file_id) zvCreate($('stHost'), { fileId: dw.file_id, page: dw.page_no || 1, pageFrom: dw.page_no || 1, pageTo: dw.page_no || 1, big: false, annotate: true });
  else $('stHost').innerHTML = '<div class="boss-empty" style="margin:20px">この図番の図面はまだありません</div>';
  $('stPhoto').onchange = async e => {
    const f = e.target.files[0]; if (!f) return;
    const fd = new FormData(); fd.append('file', f); fd.append('drawing_id', dw.id); if (item.deal_id) fd.append('deal_id', item.deal_id); if (Z.me) fd.append('author', Z.me);
    try { await api('/api/zumen/photo', { method: 'POST', body: fd }); showToast('写真を保存しました', { type: 'success' }); } catch (err) { showToast(err.message, { type: 'error' }); }
  };
  // 現場のメモ（タブレットでは prompt が使いにくいので画面の中で書く）
  let memoKind = '加工メモ';
  $('stMemoKind').querySelectorAll('button').forEach(b => b.onclick = () => {
    memoKind = b.dataset.k; $('stMemoKind').querySelectorAll('button').forEach(x => x.classList.toggle('is-on', x === b));
  });
  $('stMemo').onclick = () => { $('stMemoBox').hidden = !$('stMemoBox').hidden; if (!$('stMemoBox').hidden) $('stMemoBody').focus(); };
  $('stMemoCancel').onclick = () => { $('stMemoBox').hidden = true; };
  $('stMemoSave').onclick = async () => {
    const body = $('stMemoBody').value.trim();
    if (!body) { $('stMemoBody').focus(); return; }
    try {
      await post('/api/zumen/notes', { drawing_id: dw.id, deal_id: item.deal_id, kind: memoKind, body, author: Z.me || null });
      $('stMemoBody').value = ''; $('stMemoBox').hidden = true;
      showToast(memoKind === '不具合' ? '不具合として記録しました。次に同じ図番の注文が来たときにお知らせします' : 'メモを保存しました', { type: 'success' });
    } catch (e) { showToast(e.message, { type: 'error' }); }
  };
}

// ── 今月の数字と目標 ──
const NM = { month: null, basis: null, ex: false };
ZVIEWS.numbers = async () => {
  if (!NM.month) NM.month = todayStr().slice(0, 7);
  let r;
  try { r = await api(`/api/zumen/numbers?month=${NM.month}${NM.basis ? `&basis=${NM.basis}` : ''}&exclude_group=${NM.ex ? 1 : 0}${NM.group ? '&scope=group' : ''}`); }
  catch (e) { if (NM.group) { NM.group = false; return ZVIEWS.numbers(); } throw e; }
  const isG = r.scope === 'group';
  NM.basis = r.basis;
  const f = r.finance;
  const cur = r.current;
  const tileHtml = t => {
    const rate = t.target ? Math.min(1.5, (t.actual || 0) / t.target) : null;
    const fr = t.target && t.forecast != null ? Math.min(1.5, t.forecast / t.target) : null;
    return `<div class="nm-tile ${t.state ? 'tile-' + t.state : ''}">
      <div class="nm-label">${t.label}</div>
      <div class="nm-val">${t.actual == null ? '<span class="nm-na">—</span>' : man(t.actual)}</div>
      ${t.target ? `<div class="nm-bar"><div class="nm-fc" style="width:${(fr || 0) / 1.5 * 100}%"></div><div class="nm-fill" style="width:${(rate || 0) / 1.5 * 100}%"></div><div class="nm-goal" style="left:${100 / 1.5}%"></div></div>
        <div class="nm-sub"><span>目標 ${man(t.target)}（${t.actual != null && t.actual < 0 ? '赤字' : `達成率 ${pct(t.rate)}`}）</span>${t.forecast != null ? `<span>月末見込み <b>${man(t.forecast)}</b></span>` : ''}</div>
        ${t.remain ? `<div class="nm-sub"><span>目標まで あと${man(t.remain)}</span></div>` : ''}`
        : `<div class="nm-sub">${t.label === '売上' ? '目標が未設定です' : t.actual == null ? '原価率か固定費を入力すると表示されます' : '目標が未設定です'}</div>`}
    </div>`;
  };
  const maxS = Math.max(1, ...r.months.map(x => Math.max(x.sales, x.before_start ? 0 : x.target || 0)));
  VP().innerHTML = `
    <div class="nm-wrap">
      <div class="zs-bar">
        <button class="btn-sm" id="nmPrev">‹</button><input type="month" class="email-input compact" id="nmMonth" value="${NM.month}"><button class="btn-sm" id="nmNext">›</button>
        <div class="seg" id="nmBasis"><button data-b="order" class="${r.basis === 'order' ? 'is-on' : ''}">受注ベース</button><button data-b="ship" class="${r.basis === 'ship' ? 'is-on' : ''}">出荷ベース</button></div>
        ${isG ? '' : `<label class="zr-check"><input type="checkbox" id="nmEx" ${NM.ex ? 'checked' : ''}> グループ内の取引を除く</label>`}
        ${r.group_available || isG ? `<div class="seg" id="nmScope"><button data-s="" class="${isG ? '' : 'is-on'}">自社</button><button data-s="g" class="${isG ? 'is-on' : ''}">グループ全体</button></div>` : ''}
      </div>
      <div class="nm-note">${esc(r.note)}${r.basis === 'order' ? '受注ベースは注文書の日付で集計します。' : '出荷ベースは明細の納期で集計します。'}今日は今月の稼働日${r.pace.business_days}日のうち${r.pace.elapsed}日目です。${cur.covered_ratio != null ? `原価のうち実績の割合 <b class="${cur.covered_ratio < 0.5 ? 'warn' : ''}">${pct(cur.covered_ratio)}</b>（残りは見込みの原価率で計算）` : ''}</div>
      <div class="nm-tiles">${r.tiles.map(tileHtml).join('')}</div>
      ${isG ? `<div class="nm-cos">${r.companies.map(x => `<div><b>${esc(x.tenant)}</b><span>売上 ${man(x.sales)}</span><span>粗利 ${man(x.gross)}</span><span>営業利益 ${man(x.op)}</span>${x.rate != null ? `<span>達成 ${pct(x.rate)}</span>` : ''}</div>`).join('')}</div>` : ''}
      <div class="nm-mini">注文 ${cur.count}件　原価 ${man(cur.cost)}（実績 ${man(cur.cost_actual)}・見込み ${man(cur.cost_estimated)}）　粗利率 ${pct(cur.gross_rate)}　固定費 ${man(f.fixed_cost)}</div>

      <div class="boss-grid">
        <section class="boss-card c-blue"><div class="section-label"><span>直近12か月</span></div>
          <div class="nm-chart">${r.months.map(x => `<div class="nm-col" title="${x.month} ${man(x.sales)}${x.target ? ` / 目標 ${man(x.target)}` : ''}">
              <div class="nm-colbar" style="height:${x.sales / maxS * 100}%"></div>${x.target && !x.before_start ? `<div class="nm-coltgt" style="bottom:${x.target / maxS * 100}%"></div>` : ''}<span>${Number(x.month.slice(5))}</span></div>`).join('')}</div>
          <table class="boss-table"><thead><tr><th>月</th><th class="num">売上</th><th class="num">粗利率</th><th class="num">営業利益</th><th class="num">達成率</th></tr></thead><tbody>
            ${r.months.slice().reverse().filter(x => !x.before_start).map(x => `<tr><td>${x.month.replace('-', '/')}</td><td class="num">${man(x.sales)}</td><td class="num">${pct(x.gross_rate)}</td><td class="num">${man(x.op)}</td>
              <td class="num"><span class="${x.achieve == null ? '' : x.achieve >= 1 ? 'good' : x.achieve >= 0.9 ? 'warn' : 'bad'}">${pct(x.achieve)}</span></td></tr>`).join('')}</tbody></table></section>
        <section class="boss-card c-teal"><div class="section-label"><span>取引先別（${NM.month.replace('-', '/')}）</span></div>
          <table class="boss-table"><thead><tr><th>会社</th><th class="num">売上</th><th class="num">粗利率</th><th class="num">原価の実績割合</th></tr></thead><tbody>
            ${r.customers.map(x => `<tr><td>${isG ? `<small class="nm-ten">${esc(x.tenant)}</small>` : ''}${esc(x.company)}${x.is_group ? ' <em class="tag-card">グループ</em>' : ''}</td><td class="num">${man(x.sales)}</td>
              <td class="num"><span class="${x.gross_rate != null && f.gross_rate_target != null && x.gross_rate < f.gross_rate_target ? 'bad' : ''}">${pct(x.gross_rate)}</span></td><td class="num">${pct(x.covered_ratio)}</td></tr>`).join('') || '<tr><td colspan="4" class="boss-empty">この月の注文書はありません</td></tr>'}</tbody></table></section>
      </div>

      ${isG ? '<div class="zr-saved">目標と前提は会社ごとに設定します。「自社」に切り替えてから入力してください</div>' : `<section class="boss-card c-violet nm-set"><div class="section-label"><span>目標と前提（${NM.month.replace('-', '/')}）</span>${f.inherited_from ? `<small>${f.inherited_from.replace('-', '/')}の設定を引き継いでいます</small>` : ''}</div>
        <div class="zr-grid">
          <label>売上目標（営業の画面の月間受注目標と共通）<input type="number" class="email-input compact" id="fSales" value="${f.sales_target ?? ''}" step="10000" placeholder="未設定"></label>
          <label>粗利の目標（額）<input type="number" class="email-input compact" id="fGross" value="${f.gross_target ?? ''}" step="10000" placeholder="未設定"></label>
          <label>粗利率の目標（%）<input type="number" class="email-input compact" id="fGrossRate" value="${f.gross_rate_target != null ? Math.round(f.gross_rate_target * 100) : ''}" placeholder="額か率のどちらかを入力"></label>
          <label>営業利益の目標<input type="number" class="email-input compact" id="fOp" value="${f.op_target ?? ''}" step="10000" placeholder="未設定"></label>
          <label>月の固定費（人件費・家賃など）<input type="number" class="email-input compact" id="fFixed" value="${f.fixed_cost ?? ''}" step="10000" placeholder="未設定"></label>
          <label>見込みの原価率（%）<input type="number" class="email-input compact" id="fCost" value="${f.cost_rate != null ? Math.round(f.cost_rate * 100) : ''}" placeholder="例：70"></label>
        </div>
        <div class="zr-actions"><button class="btn-sm btn-sm-primary" id="fSave">この月から適用する</button><span class="zr-saved">翌月以降も、変更するまで同じ値を使います。会社ごとの原価率は「会社」の画面で設定できます</span></div>
      </section>`}
    </div>`;
  const shift = n => { const [y, m] = NM.month.split('-').map(Number); const d = new Date(y, m - 1 + n, 1); NM.month = `${d.getFullYear()}-${pad(d.getMonth() + 1)}`; ZVIEWS.numbers(); };
  $('nmPrev').onclick = () => shift(-1); $('nmNext').onclick = () => shift(1);
  $('nmMonth').onchange = e => { if (e.target.value) { NM.month = e.target.value; ZVIEWS.numbers(); } };
  $('nmBasis').querySelectorAll('button').forEach(b => b.onclick = () => { NM.basis = b.dataset.b; ZVIEWS.numbers(); });
  if ($('nmEx')) $('nmEx').onchange = e => { NM.ex = e.target.checked; ZVIEWS.numbers(); };
  if ($('nmScope')) $('nmScope').querySelectorAll('button').forEach(b => b.onclick = () => { NM.group = !!b.dataset.s; ZVIEWS.numbers(); });
  if ($('fSave')) $('fSave').onclick = async () => {
    const n = id => $(id).value === '' ? null : Number($(id).value);
    try {
      await put('/api/zumen/finance', { month: NM.month, sales_target: n('fSales') ?? 0, gross_target: n('fGross'), gross_rate_target: n('fGrossRate') == null ? null : n('fGrossRate') / 100,
        op_target: n('fOp'), fixed_cost: n('fFixed'), cost_rate: n('fCost') == null ? null : n('fCost') / 100 });
      showToast('保存しました', { type: 'success' }); ZVIEWS.numbers();
    } catch (err) { showToast(err.message, { type: 'error' }); }
  };
};

// ── 設定・状態 ──
ZVIEWS.settings = async () => {
  const [s, st, tr, tp, ml] = await Promise.all([api('/api/zumen/settings'), api('/api/zumen/admin/status'), api('/api/zumen/trash'),
    api('/api/zumen/templates'), api('/api/zumen/mail/status').catch(() => ({ configured: false }))]);
  const b = st.backup;
  const backupAge = b && b.finished_at ? (Date.now() - new Date(b.finished_at).getTime()) / 36e5 : null;
  const acc = Object.fromEntries((st.accuracy || []).map(x => [x.field, x]));
  VP().innerHTML = `<div class="nm-wrap">
    <div class="boss-grid">
      <section class="boss-card c-blue"><div class="section-label"><span>確認待ちの進め方</span></div>
        <label class="zr-check"><input type="checkbox" id="sBulk" ${s.bulk_confirm ? 'checked' : ''}> 「AIの読み取りが確かな書類をまとめて確定」を使う</label>
        <label class="zr-line">まとめて確定する基準（AIの読み取りの確かさ）<input type="number" min="50" max="100" class="email-input compact w-s" id="sTh" value="${Math.round(s.bulk_threshold * 100)}">%以上</label>
        <div class="zr-saved">ズメーンと並行して使う最初の1か月はオフにしておき、下の「AIの読み取り精度」を確認してからオンにすることをおすすめします</div>
        <label class="zr-check"><input type="checkbox" id="sSlack" ${s.slack_notify ? 'checked' : ''}> 注文書を確定したらSlackに知らせる（初期設定はオフ）</label>
        <div class="zr-line zk-edit">書類の種類を追加<span class="zk-chips" id="kChips">${(s.extra_kinds || []).map(k => `<span class="zk-chip">${esc(k)}<button data-kdel="${esc(k)}" title="消す">×</button></span>`).join('')}
          <input class="email-input compact" id="kAdd" maxlength="10" placeholder="例：承認図" title="種類の名前を入力して Enter キーで追加します"></span></div>
        <label class="zr-check"><input type="checkbox" id="sGroupNm" ${s.group_numbers ? 'checked' : ''}> 「業績」でグループ全体の合計を表示する（グループ会社側でもオンにしたときだけ表示されます）</label>
        <label class="zr-line">業績の集計方法（初期値） <select class="email-input compact" id="sBasis"><option value="order" ${s.basis === 'order' ? 'selected' : ''}>受注ベース</option><option value="ship" ${s.basis === 'ship' ? 'selected' : ''}>出荷ベース</option></select></label>
      </section>
      <section class="boss-card c-violet"><div class="section-label"><span>AIの読み取り精度</span></div>
        ${['company', 'deal'].map(k => `<div class="zr-line">${k === 'company' ? 'AIが最初に挙げた会社' : 'AIが最初に挙げた案件'}：${acc[k] ? `<b>${pct(acc[k].top / acc[k].count)}</b>（${acc[k].count}件中 ${acc[k].top}件を修正なしで確定）` : 'まだ記録がありません'}</div>`).join('')}
        <div class="zr-line">AI：${st.ai_available ? '<span class="good">使えます</span>' : '<span class="bad">APIキーが未設定（手入力になります）</span>'}　検索：${st.search_mode === 'fts5' ? '全文検索' : '部分一致（サーバーの環境では全文検索が使えないため）'}</div>
      </section>
    </div>
    <section class="boss-card c-violet"><div class="section-label"><span>過去の書類の取り込み（ズメーンからの移行）</span></div>
      <div class="zim">
        <div class="zim-help">ズメーンから書き出したフォルダや、共有フォルダにある図面をまとめて取り込みます。<b>いちばん上のフォルダ名を会社名にしておく</b>と、会社を判別しやすくなります（例：北摂精機／HK-200.pdf）。取り込んだ書類は「確認待ち」の「過去の書類」に表示され、会社と図番だけをまとめて確定できます。過去の書類からは、案件や納期の予定は作りません。</div>
        <div class="zim-acts">
          <label class="btn-sm btn-sm-primary">${ico('folder', 14)} フォルダを選ぶ<input type="file" id="imDir" webkitdirectory multiple hidden></label>
          <label class="btn-sm">${ico('upload', 14)} ZIPやファイルを選ぶ<input type="file" id="imZip" accept=".zip,application/zip,application/pdf,image/*,.dxf,.dwg" multiple hidden></label>
        </div>
        <div id="imProg"></div>
        <div class="zr-saved">最初は100件ほどで試し、AIの読み取り精度と費用を確認してから残りを取り込むことをおすすめします。ZIPファイルは1つ200MBまでです。それより大きい場合は、フォルダを選んで取り込んでください</div>
      </div>
    </section>
    <div class="boss-grid">
      <section class="boss-card c-teal"><div class="section-label"><span>納期から逆算する工程</span></div>
        <div class="zr-saved">納期の何営業日前にどの工程を行うかを設定します。工程表の◆、現場の画面、製作指示書に表示されます</div>
        <div id="tplRows">${tp.default.map(x => tplRow(x)).join('')}</div>
        <div class="zr-actions" style="position:static;background:none"><button class="btn-link" id="tplAdd">＋ 工程を追加</button><button class="btn-sm btn-sm-primary" id="tplSave">保存</button>
          ${tp.is_custom_default ? '<button class="btn-link" id="tplReset">初期設定に戻す</button>' : ''}</div>
      </section>
      <section class="boss-card c-blue"><div class="section-label"><span>メールからの取り込み</span></div>
        ${ml.configured ? `<div class="zr-line">受信箱 <b>${esc(ml.address)}</b> を5分ごとに確認しています。届いたメールの添付ファイルは「確認待ち」に表示されます</div>
          <div class="zr-line">これまでの受信 ${ml.received}件　最終受信 ${esc((ml.last || '—').replace('T', ' '))}</div>
          <div class="zr-saved">注文書のメールをこのアドレスへ転送してください。差出人のメールアドレスから会社を判別しやすくなります</div>`
        : `<div class="zr-line"><span class="warn">未設定</span></div><div class="zr-saved">取り込み用のメールアドレス（例：zumen@wakometal.com）を用意すると、届いた添付ファイルが自動で「確認待ち」に表示されます。受信箱の設定はサーバー側で行うため、システム担当者にご相談ください</div>`}
      </section>
    </div>
    <section class="boss-card c-amber"><div class="section-label"><span>保存先とバックアップ</span></div>
      <div class="zr-line">保存先：${st.storage === 'r2' ? 'Cloudflare R2' : 'このサーバーのディスク'}　ファイル ${st.files.count}件・${bytes(st.files.bytes)}</div>
      <div class="zr-line">ディスク：${bytes(st.disk.used)} / ${bytes(st.disk.total)}（<b class="${st.disk.ratio >= 0.7 ? 'bad' : ''}">${pct(st.disk.ratio)}</b>）${st.disk.ratio >= 0.7 ? '　使用量が70%を超えました。保存先を外部（Cloudflare R2）へ切り替えてください' : ''}</div>
      <div class="zr-line">外部へのバックアップ：${st.r2_configured ? '<span class="good">Cloudflare R2に保存しています</span>' : '<span class="warn">未設定（このサーバー内にのみ保存）</span>。ズメーンを解約する前に、必ず外部への保存を設定してください'}</div>
      <div class="zr-line">最後のバックアップ：${b ? `<span class="${b.ok === false || backupAge > 36 ? 'bad' : 'good'}">${esc(b.finished_at || '')}</span>　${esc(b.target || '')}　データベース ${bytes(b.db_bytes)}・新規ファイル ${b.files_sent ?? 0}件${b.ok === false ? `　失敗：${esc(b.error || '')}` : ''}` : '<span class="warn">まだ実行されていません</span>'}</div>
      <div class="zr-line">読み取り状況：待機中 ${st.queue.queued || 0}件・読み取り中 ${st.queue.reading || 0}件・失敗 ${st.queue.failed || 0}件${st.stuck ? `・停止中 ${st.stuck}件` : ''} <button class="btn-link" id="sRetry">失敗したものを読み直す</button></div>
    </section>
    <section class="boss-card c-red"><div class="section-label"><span>ごみ箱（${tr.days}日後に完全に削除されます）</span></div>
      ${tr.items.map(x => `<div class="zt-child">${thumb(x.file_id, x.page_from || 1, 'sm')}<span>${kindBadge(x.kind)} ${esc(x.title || x.doc_no || x.filename)} <small>${fmtDate(x.trashed_at)}</small></span><button class="btn-link" data-restore="${x.id}">戻す</button></div>`).join('') || '<div class="boss-empty">ごみ箱は空です</div>'}
    </section>
  </div>`;
  const save = async body => { try { await put('/api/zumen/settings', body); showToast('保存しました', { type: 'success', duration: 1200 }); } catch (e) { showToast(e.message, { type: 'error' }); } };
  $('sBulk').onchange = e => save({ bulk_confirm: e.target.checked });
  $('sTh').onchange = e => save({ bulk_threshold: Number(e.target.value) / 100 });
  $('sSlack').onchange = e => save({ slack_notify: e.target.checked });
  $('sBasis').onchange = e => save({ basis: e.target.value });
  const readTpl = () => [...document.querySelectorAll('.tpl-row')].map(r => ({ label: r.querySelector('[data-tl]').value.trim(), days: Number(r.querySelector('[data-td]').value) })).filter(x => x.label);
  const bindTpl = () => document.querySelectorAll('[data-tdel]').forEach(b => b.onclick = () => { b.closest('.tpl-row').remove(); });
  bindTpl();
  $('tplAdd').onclick = () => { $('tplRows').insertAdjacentHTML('beforeend', tplRow({ label: '', days: 3 })); bindTpl(); };
  $('tplSave').onclick = async () => { try { await put('/api/zumen/templates', { company_id: 0, steps: readTpl() }); showToast('保存しました', { type: 'success' }); ZVIEWS.settings(); } catch (e) { showToast(e.message, { type: 'error' }); } };
  if ($('tplReset')) $('tplReset').onclick = async () => { await put('/api/zumen/templates', { company_id: 0, steps: [] }); ZVIEWS.settings(); };
  const saveKinds = async ks => { try { await put('/api/zumen/settings', { extra_kinds: ks }); Z.meta = await api('/api/zumen/meta'); ZVIEWS.settings(); } catch (e) { showToast(e.message, { type: 'error' }); } };
  $('sGroupNm').onchange = async e => { await put('/api/zumen/settings', { group_numbers: e.target.checked }); showToast('保存しました', { type: 'success' }); };
  $('kAdd').onkeydown = e => { if (e.key === 'Enter' && !e.isComposing && e.target.value.trim()) saveKinds([...(s.extra_kinds || []), e.target.value.trim()]); };
  VP().querySelectorAll('[data-kdel]').forEach(b => b.onclick = () => saveKinds((s.extra_kinds || []).filter(k => k !== b.dataset.kdel)));
  $('imDir').onchange = e => { importFiles([...e.target.files]); e.target.value = ''; };
  $('imZip').onchange = e => { importFiles([...e.target.files]); e.target.value = ''; };
  $('sRetry').onclick = async () => { const r = await post('/api/zumen/admin/retry'); showToast(`${r.requeued}件を読み直します`); };
  VP().querySelectorAll('[data-restore]').forEach(b => b.onclick = async () => { await post(`/api/zumen/docs/${b.dataset.restore}/restore`); showToast('元に戻しました', { type: 'success' }); ZVIEWS.settings(); });
};

// ── 社外への共有リンク ──
async function openShare(kind, id, label) {
  const box = $('zmModalBox');
  $('zmModal').style.display = 'flex';
  box.className = 'zm-modal-box zm-share';
  const draw = async () => {
    const r = await api(`/api/zumen/shares?kind=${kind}&target_id=${id}`);
    box.innerHTML = `<div class="zm-modal-head"><b>${ico('share', 15)} 社外に共有　${esc(label)}</b><button class="dupe-modal-close" id="zmModalClose">×</button></div>
      <div class="zsh-body">
        <div class="zsh-help">ログインせずに見られるリンクを作成します。期限が過ぎるか、リンクを取り消すと見られなくなります。${kind === 'drawing' ? '図面の場合は、常に<b>その時点の最新版</b>が表示されます。' : ''}</div>
        <div class="zr-grid">
          <label>公開期間<select class="email-input compact" id="shDays">${[[1, '1日'], [3, '3日'], [7, '1週間'], [14, '2週間'], [30, '1か月'], [90, '3か月']].map(([v, l]) => `<option value="${v}" ${v === 7 ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
          <label class="zr-check" style="align-self:end"><input type="checkbox" id="shDl"> 原本のダウンロードを許可する</label>
          <label class="span2">相手へのメッセージ（任意）<input class="email-input compact" id="shNote" maxlength="200" placeholder="例：お見積りのご確認用です"></label>
        </div>
        <div class="zr-actions" style="position:static;background:none"><button class="btn-sm btn-sm-primary" id="shMake">${ico('share', 13)} リンクを作成</button></div>
        <div class="zr-label" style="margin-top:6px">作成済みのリンク</div>
        ${r.shares.map(x => `<div class="zsh-row ${x.active ? '' : 'is-off'}"><div><b>${x.active ? '有効' : x.revoked_at ? '取り消し済み' : '期限切れ'}</b>　${esc(x.expires_at.slice(0, 16).replace('T', ' '))} まで　閲覧 ${x.views}回${x.allow_download ? '・ダウンロード可' : ''}
            <small>${esc(x.note || '')} ${esc(x.created_by || '')}</small></div>
          ${x.active ? `<button class="btn-link" data-copy="${esc(location.origin + x.url)}">コピー</button><button class="btn-link danger" data-revoke="${x.id}">取り消す</button>` : ''}</div>`).join('') || '<div class="boss-empty">まだありません</div>'}
      </div>`;
    $('zmModalClose').onclick = zmCloseModal;
    $('shMake').onclick = async () => {
      try {
        const x = await post('/api/zumen/shares', { kind, target_id: id, days: Number($('shDays').value), allow_download: $('shDl').checked, note: $('shNote').value, actor: Z.me || null });
        const url = location.origin + x.url;
        try { await navigator.clipboard.writeText(url); showToast('リンクをコピーしました。メールやLINEに貼って送ってください', { type: 'success', duration: 5000 }); }
        catch { prompt('このリンクをコピーして相手に送ってください', url); }
        draw();
      } catch (e) { showToast(e.message, { type: 'error' }); }
    };
    box.querySelectorAll('[data-copy]').forEach(b => b.onclick = async () => { try { await navigator.clipboard.writeText(b.dataset.copy); showToast('コピーしました', { type: 'success' }); } catch { prompt('このリンクをコピーしてください', b.dataset.copy); } });
    box.querySelectorAll('[data-revoke]').forEach(b => b.onclick = async () => { await api(`/api/zumen/shares/${b.dataset.revoke}`, { method: 'DELETE' }); showToast('リンクを取り消しました'); draw(); });
  };
  draw();
}

function tplRow(x) {
  return `<div class="tpl-row"><input class="email-input compact" data-tl value="${esc(x.label)}" placeholder="工程の名前" maxlength="20">
    <span>納期の</span><input type="number" class="email-input compact w-s" data-td value="${x.days}" min="0" max="250"><span>営業日前</span><button class="zr-x" data-tdel title="消す">×</button></div>`;
}

// ── 過去分をまとめて送る（10件ずつ。途中で画面を離れると止まる）──
const IMPORT_OK = /\.(pdf|png|jpe?g|tiff?|heic|heif|webp|gif|bmp|dxf|dwg|stp|step|zip)$/i;
async function importFiles(files) {
  const list = files.filter(f => IMPORT_OK.test(f.name) && !/(^|\/)(\.|__MACOSX)/.test(f.webkitRelativePath || f.name));
  const el = $('imProg');
  if (!list.length) { showToast('取り込めるファイル（PDF・画像・CAD・ZIP）がありません', { type: 'warning' }); return; }
  const total = list.length, sum = { queued: 0, duplicates: 0, errors: [] };
  let sent = 0, stop = false;
  const draw = (done) => {
    el.innerHTML = `<div class="zim-prog ${done ? 'is-done' : ''}"><div class="zim-bar"><i style="width:${Math.round(sent / total * 100)}%"></i></div>
      <div class="zim-num"><b>${sent.toLocaleString()}</b> / ${total.toLocaleString()} 件${done ? 'をアップロードしました' : 'をアップロードしています…'}
        <span>受付 ${sum.queued.toLocaleString()}件　登録済み ${sum.duplicates.toLocaleString()}件　失敗 ${sum.errors.length}件</span>
        ${done ? '' : '<button class="btn-link" id="imStop">中止する</button>'}</div>
      ${sum.errors.length ? `<details class="zim-err"><summary>アップロードできなかったファイル</summary>${sum.errors.slice(0, 50).map(x => `<div>${esc(x.filename)}：${esc(x.error)}</div>`).join('')}</details>` : ''}
      ${done ? `<button class="btn-sm" id="imGo">${ico('inbox', 13)} 確認待ちの「過去の書類」を開く</button>` : ''}</div>`;
    if ($('imStop')) $('imStop').onclick = () => { stop = true; };
    if ($('imGo')) $('imGo').onclick = () => { Z.inboxFilter = 'import'; setView('inbox'); };
  };
  window.onbeforeunload = () => 'アップロードの途中です';
  draw(false);
  for (let i = 0; i < list.length && !stop; i += 10) {
    const chunk = list.slice(i, i + 10), fd = new FormData();
    chunk.forEach(f => fd.append('files', f, f.name));
    fd.append('paths', JSON.stringify(chunk.map(f => f.webkitRelativePath || f.name)));
    fd.append('mode', 'import');
    if (Z.me) fd.append('actor', Z.me);
    try {
      const r = await api('/api/zumen/upload', { method: 'POST', body: fd });
      sum.queued += r.queued.length; sum.duplicates += r.duplicates.length; sum.errors.push(...r.errors);
    } catch (e) { sum.errors.push(...chunk.map(f => ({ filename: f.name, error: e.message }))); }
    sent += chunk.length;
    draw(false);
  }
  window.onbeforeunload = null;
  draw(true);
  refreshCount();
}
