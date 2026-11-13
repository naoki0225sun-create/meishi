// ============================================================
// 案件をまとめて動かす
//   ・一覧／ボードのチェックで複数選び、下のバーで 保留・失注・ほかの段階へ一度に移す
//   ・一覧の行を上の段階チップへドラッグ（選んだものはまとめて）。ボードは列へドラッグ
//   ・どれも「元に戻す」つき。失注の理由はあとからでよい
// sales.js の S / api / esc / showToast / loadDeals などを使う
// ============================================================

function pickBoxHtml(d) {
  return `<label class="pick" data-pick="${d.id}" title="選ぶ（まとめて動かす）" onclick="event.stopPropagation()">
    <input type="checkbox" ${S.bulk.has(d.id) ? 'checked' : ''} aria-label="選ぶ"></label>`;
}

// 今の一覧で見えている案件（選ぶ対象）
function pickVisible() {
  return S.layout === 'board' ? boardDeals() : filteredDeals();
}

function bindPicks(root) {
  root.querySelectorAll('[data-pick] input').forEach(cb => cb.onchange = e => {
    const id = Number(cb.closest('[data-pick]').dataset.pick);
    cb.checked ? S.bulk.add(id) : S.bulk.delete(id);
    cb.closest('.deal-row, .board-card')?.classList.toggle('is-picked', cb.checked);
    drawBulkBar();
  });
}

function drawBulkBar() {
  let bar = $('bulkBar');
  // 消えた案件（絞り込み・読み直し）は外す
  const ids = new Set(S.allDeals.map(d => d.id));
  [...S.bulk].forEach(id => { if (!ids.has(id)) S.bulk.delete(id); });
  if (!S.bulk.size || S.view !== 'deals') { if (bar) bar.remove(); document.body.classList.remove('has-bulk'); return; }
  if (!bar) {
    bar = document.createElement('div');
    bar.id = 'bulkBar'; bar.className = 'bulk-bar'; bar.setAttribute('role', 'region'); bar.setAttribute('aria-label', 'まとめて動かす');
    document.body.appendChild(bar);
  }
  document.body.classList.add('has-bulk');
  const pl = P();
  const vis = pickVisible();
  const allOn = vis.length && vis.every(d => S.bulk.has(d.id));
  const hold = pl.stages.includes('保留') ? '保留' : null;
  bar.innerHTML = `<b class="bulk-n">${S.bulk.size}件を選択中</b>
    <div class="bulk-acts">
      ${hold ? `<button class="btn-sm bulk-hold" data-bulk-stage="${esc(hold)}">保留にする</button>` : ''}
      <button class="btn-sm bulk-lost" data-bulk-stage="${esc(pl.lost)}">${esc(pl.lost)}にする</button>
      <select class="email-input compact bulk-sel" id="bulkStage" aria-label="段階を選んで移す">
        <option value="">ほかの段階へ…</option>
        ${pl.stages.map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join('')}
      </select>
    </div>
    <div class="bulk-sub">
      <button class="link-btn" id="bulkAll">${allOn ? '表示中の選択を外す' : `表示中の${vis.length}件をすべて選ぶ`}</button>
      <button class="link-btn" id="bulkClear">選択をやめる</button>
    </div>`;
  bar.querySelectorAll('[data-bulk-stage]').forEach(b => b.onclick = () => bulkMove([...S.bulk], b.dataset.bulkStage));
  $('bulkStage').onchange = e => { if (e.target.value) bulkMove([...S.bulk], e.target.value); };
  $('bulkAll').onclick = () => { vis.forEach(d => allOn ? S.bulk.delete(d.id) : S.bulk.add(d.id)); renderList(); drawBulkBar(); };
  $('bulkClear').onclick = () => { S.bulk.clear(); renderList(); drawBulkBar(); };
}

// まとめて段階を移す（元に戻すつき）
async function bulkMove(ids, stage) {
  ids = ids.filter(id => { const d = S.allDeals.find(x => x.id === id); return d && d.stage !== stage; });
  if (!ids.length) { showToast(`もう「${stage}」です`, { type: 'info', duration: 1500 }); return; }
  try {
    const r = await api('/api/sales/deals/bulk-stage', { method: 'POST', body: JSON.stringify({ ids, stage }) });
    S.bulk.clear();
    await loadDeals();
    if (S.view === 'today') renderToday();
    const undo = Object.fromEntries(r.before.map(x => [String(x.id), x.stage]));
    showToast(`${r.updated}件を「${stage}」にしました${r.skipped.length ? `（${r.skipped.length}件は種類が違うので飛ばしました）` : ''}`, {
      type: 'success', duration: 6000,
      action: r.updated ? { label: '元に戻す', onClick: () => bulkUndo(undo) } : null,
    });
  } catch (e) { showToast(e.message, { type: 'error', title: 'まとめて動かせませんでした' }); }
}

async function bulkUndo(stages) {
  try {
    await api('/api/sales/deals/bulk-stage', { method: 'POST', body: JSON.stringify({ ids: Object.keys(stages).map(Number), stages }) });
    await loadDeals();
    if (S.view === 'today') renderToday();
    showToast('元に戻しました', { type: 'success', duration: 1500 });
  } catch (e) { showToast(e.message, { type: 'error' }); }
}

// 一覧の行 → 上の段階チップへドラッグ
let dragDealId = null;
function bindRowDrag(root) {
  root.querySelectorAll('.deal-row[data-id]').forEach(el => {
    el.draggable = true;
    el.ondragstart = e => {
      dragDealId = Number(el.dataset.id);
      const n = S.bulk.has(dragDealId) ? S.bulk.size : 1;
      el.classList.add('is-dragging');
      document.body.classList.add('is-deal-drag');
      e.dataTransfer.effectAllowed = 'move';
      try { e.dataTransfer.setData('text/plain', String(dragDealId)); } catch { /* 一部ブラウザ */ }
      dragLabel(e, n);
    };
    el.ondragend = () => { el.classList.remove('is-dragging'); document.body.classList.remove('is-deal-drag'); dragDealId = null;
      document.querySelectorAll('.chip.is-drop').forEach(c => c.classList.remove('is-drop')); };
  });
}

function bindChipDrop() {
  const stages = P().stages;
  $('stageFilter').querySelectorAll('[data-filter]').forEach(ch => {
    if (!stages.includes(ch.dataset.filter)) return;
    ch.ondragover = e => { if (dragDealId == null) return; e.preventDefault(); ch.classList.add('is-drop'); };
    ch.ondragleave = () => ch.classList.remove('is-drop');
    ch.ondrop = e => {
      e.preventDefault(); ch.classList.remove('is-drop');
      const id = dragDealId || Number(e.dataTransfer.getData('text/plain'));
      if (!id) return;
      if (S.bulk.has(id) && S.bulk.size > 1) bulkMove([...S.bulk], ch.dataset.filter);
      else moveStage(id, ch.dataset.filter);
    };
  });
}

// ドラッグ中の「N件」表示
function dragLabel(e, n) {
  if (n < 2 || !e.dataTransfer.setDragImage) return;
  const g = document.createElement('div');
  g.className = 'drag-ghost'; g.textContent = `${n}件をまとめて移動`;
  document.body.appendChild(g);
  e.dataTransfer.setDragImage(g, 10, 10);
  setTimeout(() => g.remove(), 0);
}

// 1件の小さなメニュー（今日やること などから: 保留・失注・ほかの段階）
function stageMenu(btn, id) {
  document.querySelectorAll('.stage-menu').forEach(m => m.remove());
  const d = S.allDeals.find(x => x.id === id);
  if (!d) return;
  const pl = pipeOf(d);
  const m = document.createElement('div');
  m.className = 'stage-menu'; m.setAttribute('role', 'menu');
  const quick = [pl.stages.includes('保留') ? '保留' : null, pl.lost].filter(s => s && s !== d.stage);
  m.innerHTML = quick.map(s => `<button role="menuitem" data-sm="${esc(s)}" class="${s === pl.lost ? 'is-lost' : 'is-hold'}">${esc(s)}にする</button>`).join('')
    + `<div class="stage-menu-sep"></div><div class="stage-menu-t">段階を変える</div>`
    + pl.stages.filter(s => s !== d.stage && !quick.includes(s)).map(s => `<button role="menuitem" data-sm="${esc(s)}">${esc(s)}</button>`).join('');
  document.body.appendChild(m);
  const r = btn.getBoundingClientRect();
  const w = m.offsetWidth, h = m.offsetHeight;
  m.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, r.right - w)) + 'px';
  m.style.top = (r.bottom + h + 8 > window.innerHeight ? Math.max(8, r.top - h - 4) : r.bottom + 4) + 'px';
  m.querySelectorAll('[data-sm]').forEach(b => b.onclick = e => { e.stopPropagation(); m.remove(); bulkMove([id], b.dataset.sm); });
  setTimeout(() => document.addEventListener('click', function off(e) { if (!m.contains(e.target)) { m.remove(); document.removeEventListener('click', off); } }), 0);
}
