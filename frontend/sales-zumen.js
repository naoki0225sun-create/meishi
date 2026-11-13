// ============================================================
// 営業から図面タブへのつなぎ
// - 案件画面の「書類」タブ（紐づいた見積書・注文書・図面。ここに落とすとその案件に紐づく前提で取り込む）
// - 部長の目の頭に「今月の数字」（注文書から見た売上・粗利・営業利益と目標の達成度）
// ============================================================
async function loadDealDocs(dealId) {
  const box = document.getElementById('dealDocs');
  if (!box) return;
  let r;
  try { r = await api(`/api/zumen/deals/${dealId}/docs`); } catch { box.innerHTML = '<div class="boss-empty">図面の情報を読み込めませんでした</div>'; return; }
  const tab = document.querySelector('[data-dtab="docs"]');
  if (tab && r.docs.length && !tab.querySelector('.dtab-n')) tab.insertAdjacentHTML('beforeend', `<span class="dtab-n">${r.docs.length}</span>`);
  const kindTag = k => `<span class="zk" data-k="${esc(k)}">${esc(k)}</span>`;
  box.innerHTML = `
    <div class="zx-drop" data-drop-deal="${dealId}">ここに図面・注文書・見積書をドラッグすると、図面タブの「確認待ち」に追加されます（紐づけ先の候補として、この案件が最初に表示されます）
      <label class="btn-link" style="cursor:pointer">ファイルを選択<input type="file" multiple accept="application/pdf,image/*,.dxf,.dwg,.step,.stp" hidden id="dealDocFile"></label></div>
    <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap"><a class="btn-sm" href="/zumen-print.html?deal=${dealId}" target="_blank">製作指示書を印刷</a>
      <a class="btn-sm" href="/zumen.html?view=site&deal=${dealId}">現場の画面で開く</a>
      ${r.company_id ? `<a class="btn-sm" href="/zumen.html?company=${r.company_id}">会社フォルダを開く</a>` : ''}</div>
    ${r.pending ? `<div class="zr-warn info" style="margin-top:8px">確認待ち・読み取り中の書類が${r.pending}件あります <a class="btn-link" href="/zumen.html">図面タブで確認する</a></div>` : ''}
    <div class="zx-docs" style="margin-top:10px">${r.docs.map(d => `<a class="zr-rel" href="/zumen.html?doc=${d.id}">
      <img class="zm-thumb sm" src="/api/zumen/files/${d.file_id}/page/${d.page_from || 1}?size=thumb" alt="" onerror="this.style.visibility='hidden'">
      ${kindTag(d.kind)} ${esc(d.doc_no || d.title || d.filename)} ${d.doc_date ? esc(fmtDate(d.doc_date)) : ''} ${d.amount != null ? yen(d.amount) : ''}
      ${d.due_on ? `<small>納期 ${esc(fmtDate(d.due_on))}</small>` : ''}</a>`).join('') || '<div class="boss-empty">まだ書類はありません</div>'}</div>
    ${r.drawings.length ? `<div class="section-label" style="margin-top:12px"><span>図面（最新版）</span></div><div class="zx-dws">${r.drawings.map(dw => `<a class="zr-rel" href="/zumen.html?drawing=${dw.id}">
      ${esc(dw.drawing_no)} <b style="color:var(--green)">${esc(dw.rev || (dw.file_id ? '初版' : ''))}</b><small>${esc(dw.name || '')}</small></a>`).join('')}</div>` : ''}`;
  api(`/api/zumen/group/deals/${dealId}/docs`).then(g => {
    if (!g.tenant || !(g.drawings.length || g.docs.length)) return;
    const pg = (fid, n, size) => `/api/zumen/group/deals/${dealId}/page/${fid}/${n}?size=${size}`;
    box.insertAdjacentHTML('beforeend', `<div class="section-label" style="margin-top:14px"><span>${esc(g.tenant)}の図面と書類（閲覧のみ）</span></div>
      <div class="zx-group">${g.drawings.map(dw => `<a class="zx-gtile" href="${pg(dw.file_id, dw.page_no, 'hi')}" target="_blank" rel="noopener">
        <img src="${pg(dw.file_id, dw.page_no, 'thumb')}" alt="" loading="lazy"><b>${esc(dw.drawing_no)}</b><span>${esc(dw.rev || '初版')}　${esc(dw.name || '')}</span></a>`).join('')}
        ${g.docs.filter(x => x.kind !== '図面').map(x => `<a class="zx-gtile" href="${pg(x.file_id, x.page_from || 1, 'hi')}" target="_blank" rel="noopener">
        <img src="${pg(x.file_id, x.page_from || 1, 'thumb')}" alt="" loading="lazy" onerror="this.style.visibility='hidden'"><b>${esc(x.kind)}</b><span>${esc(x.doc_no || x.title || '')}</span></a>`).join('')}</div>`);
  }).catch(() => {});
  const drop = box.querySelector('[data-drop-deal]');
  const send = async files => {
    if (!files.length) return;
    const fd = new FormData();
    [...files].forEach(f => fd.append('files', f, f.name));
    fd.append('deal_id', dealId);
    if (S.me) fd.append('actor', S.me);
    try {
      const res = await fetch('/api/zumen/upload', { method: 'POST', body: fd, credentials: 'include' }).then(x => x.json());
      showToast(`${(res.queued || []).length}件を受け付けました。図面タブの「確認待ち」で確定してください`, { type: 'success' });
      loadDealDocs(dealId);
    } catch (e) { showToast(e.message, { type: 'error' }); }
  };
  drop.ondragover = e => { e.preventDefault(); drop.classList.add('over'); };
  drop.ondragleave = () => drop.classList.remove('over');
  drop.ondrop = e => { e.preventDefault(); e.stopPropagation(); drop.classList.remove('over'); send(e.dataTransfer.files); };
  box.querySelector('#dealDocFile').onchange = e => send(e.target.files);
}

async function fillBossNumbers() {
  const host = document.getElementById('bossNumbers');
  if (!host) return;
  let r;
  try { r = await api('/api/zumen/numbers'); } catch { host.remove(); return; }
  if (!r.current.count && !r.tiles.some(t => t.target)) { host.innerHTML = ''; return; }
  const man = n => n == null ? '—' : (Math.abs(n) >= 1e4 ? Math.round(n / 1e4).toLocaleString('ja-JP') + '万円' : n.toLocaleString('ja-JP') + '円');
  const pc = v => v == null ? '—' : Math.round(v * 100) + '%';
  host.innerHTML = `<div class="boss-numbers-head"><span>今月の業績（注文書から集計した概算・${r.basis === 'order' ? '受注' : '出荷'}ベース）</span><a class="btn-link" href="/zumen.html?view=numbers">詳しく見る</a></div>
    <div class="stat-tiles boss-tiles">${r.tiles.map(t => `<div class="stat-tile ${t.state ? 'tile-' + t.state : ''}"><div class="stat-tile-label">${t.label}${t.target ? `（目標 ${man(t.target)}）` : ''}</div>
      <div class="stat-tile-value">${man(t.actual)}</div>${t.target ? `<div class="stat-tile-label">達成 ${pc(t.rate)}${t.forecast != null ? `・月末見込み ${man(t.forecast)}` : ''}</div>` : ''}</div>`).join('')}</div>`;
}
