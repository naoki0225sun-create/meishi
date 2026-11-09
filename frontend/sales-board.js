// ============================================================
// ボード表示（段階ごとの列・ドラッグで段階を変える）と
// 段階ごとのガイダンス（Salesforce の「パス」にあたるもの）
// sales.js の S / api / esc などを使う
// ============================================================

const LAYOUT_KEY = 'sales.layout';

// この段階でやること（マニュアル2章・14章の要約）
const STAGE_GUIDE = {
  テレアポ: 'アポが取れたら日時を入れて「アポ」へ。取れなければ次にかける日を決め、聞けたこと（外注の状況・困りごと）を残す',
  名刺交換: '当日〜翌営業日に御礼。次に会う理由（工場見学・図面の相談）を一つ入れる',
  御礼済: '打合せの約束を取る。困りごとや外注のきっかけを聞ける場をつくる',
  アポ: '困りごと・外注のきっかけを聞き、お客様に課題を会社の問題として認識してもらう',
  ヒアリング: '図面・数量・材質・納期・予算・決め方をそろえる。発注を決める時期も聞く',
  見積提出: '金額の納得・相見積・決裁者の意向を確かめ「買わない理由」を消す',
  受注: '感想戦を書き、次の発注見込み時期を入れる。紹介もお願いする',
  失注: '「競合負け」か「何も買わなかった」かを記録。再アプローチ日を決める',
  保留: '再開の条件と時期を決めて確認日を入れる',
  最初の打ち合わせ: '取材の趣旨を伝え、テーマと切り口を決める',
  日程調整: '話す人・日時・場所を決める。候補日を3つ出すと早い',
  質問事項送付: '質問を送り、先方の確認と撮影の可否・NG箇所を確かめる',
  取材: '初稿の確認者と期限を決める。外注の困りごとが聞けたら営業案件も作る',
  初稿提出: '修正点と公開日をもらう。1週間返事がなければやさしく催促',
  アップロード完了: '掲載のお知らせとお礼。営業につなげる',
  見送り: '理由を一言残す。時期を変えて再打診できるか考える',
};

// 段階の道のり（一直線の段階を細いバーで。どこまで進んだか・止まった所・結果がひと目でわかる）
function routeHtml(d) {
  const pl = pipeOf(d);
  const lin = pl.linear || [];
  if (!lin.length) return '';
  const cur = lin.indexOf(d.stage);
  const reached = cur >= 0 ? cur : Math.min(d.max_stage_idx || 0, lin.length - 1);
  const state = d.stage === pl.won ? 'won' : d.stage === pl.lost ? 'lost' : cur < 0 ? 'hold' : d.stage_down ? 'down' : 'open';
  const segs = lin.map((st, i) => `<i class="${i <= reached ? 'on' : ''} ${i === reached ? 'cur' : ''}"></i>`).join('');
  const title = state === 'lost' ? `${d.lost_from_stage || ''}で${pl.lost}` : state === 'hold' ? `${d.stage}（${lin[reached]}まで進んだ）` : d.stage;
  return `<span class="route st-${state}" title="${esc(title)}" aria-label="段階 ${esc(title)}">${segs}</span>`;
}

function guideHtml(d) {
  const g = (pipeOf(d).guide || {})[d.stage] || STAGE_GUIDE[d.stage];
  if (!g) return '';
  const miss = (d.checks_missing || []).length;
  return `<div class="path-guide"><b>この段階でやること</b>${esc(g)}
    ${d.is_open && miss ? `<button class="btn-link" id="goChecks">確認項目があと${miss}つ</button>` : ''}</div>`;
}

function setLayout(layout) {
  S.layout = layout;
  try { localStorage.setItem(LAYOUT_KEY, layout); } catch { /* 保存できなくても動く */ }
  document.body.classList.toggle('board-mode', layout === 'board');
  sheetLabel();
  if (layout === 'board') {
    $('detailPane').innerHTML = '<div class="detail-empty"><p class="detail-empty-text">案件を選択すると詳細が表示されます</p></div>';
  } else {
    $('mobileSheet').classList.remove('is-open');
    if (S.detail && !isMobile()) renderDetail();
  }
  renderFilters();
  renderList();
}

function boardDeals() {
  // ボードでは段階の絞り込みは使わず、検索だけ効かせる。終わった案件は直近90日だけ
  const since = new Date(Date.now() - 90 * 864e5).toISOString().slice(0, 10);
  let list = S.deals.filter(d => d.is_open || (d.closed_at || '').slice(0, 10) >= since);
  if (S.filter === 'action') list = list.filter(d => d.needs_action);
  if (S.q) {
    const q = S.q.toLowerCase();
    list = list.filter(d => [d.title, d.company_snapshot, d.contact_snapshot, d.next_action,
      d.card && d.card.name, d.card && d.card.company, d.site_name, d.end_user].some(v => (v || '').toLowerCase().includes(q)));
  }
  return list;
}

function renderBoard() {
  const pl = P();
  const list = boardDeals();
  const cols = pl.stages.map(stage => {
    const inCol = list.filter(d => d.stage === stage);
    const ds = S.sort && S.sort !== 'smart' ? sortDeals(inCol)
      : inCol.sort((a, b) => (a.next_action_due || '9999').localeCompare(b.next_action_due || '9999') || b.id - a.id);
    const sum = ds.reduce((n, d) => n + (d.amount || 0), 0);
    const closed = stage === pl.won || stage === pl.lost;
    return `<div class="board-col ${closed ? 'is-closed' : ''}" data-stage="${esc(stage)}">
      <div class="board-col-head">
        <span class="stage-badge" data-stage="${esc(stage)}">${esc(stage)}</span>
        <span class="board-col-n">${ds.length}</span>
        ${pl.key === 'sales' && sum ? `<span class="board-col-sum">${yen(sum)}</span>` : ''}
      </div>
      <div class="board-col-body">
        ${ds.map(boardCard).join('') || '<div class="board-empty">ここに移動</div>'}
      </div>
    </div>`;
  }).join('');
  $('listPane').innerHTML = `<div class="board">${cols}</div>
    <div class="board-hint">カードをドラッグして段階を変えられます。クリックで詳細を開きます</div>`;
  bindBoard();
}

function boardCard(d) {
  const company = (d.card && d.card.company) || d.company_snapshot || '(会社不明)';
  const person = (d.card && d.card.name) || d.contact_snapshot || '';
  const alert = d.alerts.find(a => a.level !== 'info');
  const boss = (d.advice || []).find(a => a.level === 'high' || a.level === 'mid');
  let due = '';
  if (d.is_open && d.next_action_due) {
    const late = d.overdue ? 'is-late' : d.due_today ? 'is-today' : '';
    due = `<span class="bc-due ${late}">${ico('clock', 12)} ${esc(d.due_today ? '今日' : fmtDate(d.next_action_due))}</span>`;
  }
  return `<div class="board-card ${d.id === S.selectedId ? 'is-active' : ''}" draggable="true" data-id="${d.id}">
    <div class="bc-company">${esc(company)}${person ? ` <span>${esc(person)}</span>` : ''}</div>
    <div class="bc-title"><span class="deal-no">#${d.id}</span> ${esc(d.title)}</div>
    ${routeHtml(d)}
    <div class="bc-meta">${d.amount != null ? `<span class="bc-amt">${yen(d.amount)}</span>` : ''}${due}
      ${d.no_card ? '<span class="nocard-badge">名刺なし</span>' : ''}${ownerBadge(d)}${noNextBadge(d)}${vendorBadge(d)}${vqBadge(d)}${panelBadges(d)}</div>
    ${alert ? `<div class="bc-alert">${ico('alert', 12)} ${esc(alert.message)}</div>` : boss ? `<div class="bc-boss">部長: ${esc(boss.title)}</div>` : ''}
  </div>`;
}

function bindBoard() {
  const root = $('listPane');
  let dragId = null;
  root.querySelectorAll('.board-card').forEach(el => {
    el.onclick = () => selectDeal(Number(el.dataset.id));
    el.ondragstart = e => {
      dragId = Number(el.dataset.id);
      el.classList.add('is-dragging');
      e.dataTransfer.effectAllowed = 'move';
      try { e.dataTransfer.setData('text/plain', String(dragId)); } catch { /* 一部ブラウザ */ }
    };
    el.ondragend = () => { el.classList.remove('is-dragging'); root.querySelectorAll('.board-col').forEach(c => c.classList.remove('is-over')); };
  });
  root.querySelectorAll('.board-col').forEach(col => {
    col.ondragover = e => { e.preventDefault(); col.classList.add('is-over'); };
    col.ondragleave = e => { if (!col.contains(e.relatedTarget)) col.classList.remove('is-over'); };
    col.ondrop = e => {
      e.preventDefault();
      col.classList.remove('is-over');
      const id = dragId || Number(e.dataTransfer.getData('text/plain'));
      if (id) moveStage(id, col.dataset.stage);
    };
  });
}

// 段階を変える（ボードのドラッグ・詳細のステッパー共通）。元に戻すつき
async function moveStage(id, stage) {
  const d = S.allDeals.find(x => x.id === id);
  if (!d || d.stage === stage) return;
  const pl = pipeOf(d);
  if (stage === pl.lost && pl.reason_required) {
    // 失注は理由が要るので詳細で選んでもらう
    await selectDeal(id);
    S.showLostBox = true;
    renderDetail();
    showToast('失注の理由を選んでください', { type: 'info', duration: 2500 });
    return;
  }
  const prev = { stage: d.stage, lost_reason: d.lost_reason, lost_detail: d.lost_detail, max_stage_idx: d.max_stage_idx || 0 };
  d.stage = stage; d.is_open = !(stage === pl.won || stage === pl.lost);   // 先に画面を動かす
  renderFilters(); renderList();
  try {
    await api(`/api/sales/deals/${id}`, { method: 'PATCH', body: JSON.stringify({ stage }) });
    await refreshDeal(id);
    showToast(`#${id} を「${stage}」にしました`, {
      type: 'success', duration: 5000,
      action: { label: '元に戻す', onClick: () => undoStage(id, prev) },
    });
  } catch (e) {
    showToast(e.message, { type: 'error', title: '段階を変えられませんでした' });
    await loadDeals();
  }
}

async function undoStage(id, prev) {
  const body = { stage: prev.stage, undo_max_stage_idx: prev.max_stage_idx };
  if (prev.lost_reason) { body.lost_reason = prev.lost_reason; body.lost_detail = prev.lost_detail; }
  try {
    await api(`/api/sales/deals/${id}`, { method: 'PATCH', body: JSON.stringify(body) });
    await refreshDeal(id);
    showToast('元に戻しました', { type: 'success', duration: 1500 });
  } catch (e) { showToast(e.message, { type: 'error' }); }
}

// 1件だけ読み直して一覧・詳細に反映する（全件の読み直しをしない）
async function refreshDeal(id) {
  const fresh = await api(`/api/sales/deals/${id}`);
  const i = S.allDeals.findIndex(x => x.id === id);
  if (i >= 0) S.allDeals[i] = fresh; else S.allDeals.unshift(fresh);
  if (S.detail && S.detail.id === id) S.detail = fresh;
  applyPipelineKeepSelection();
  if (S.detail && S.detail.id === id) renderDetail();
  return fresh;
}

function applyPipelineKeepSelection() {
  const p = P();
  S.deals = S.allDeals.filter(d => (d.pipeline || 'sales') === p.key && ownerMatch(d) && vendorMatch(d));
  renderPipelineTabs();
  renderFilters();
  renderList();
  renderAppNav();
}

// キーボード操作（入力中は無効）
document.addEventListener('keydown', e => {
  const tag = (e.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (document.querySelector('.dupe-modal-overlay[style*="flex"]')) return;
  if (e.key === '/') { e.preventDefault(); if (S.view !== 'deals') setView('deals'); $('searchInput').focus(); }
  else if (e.key === 'n') { e.preventDefault(); openNewDeal(); }
  else if (e.key === 'b') { if (S.view !== 'deals') setView('deals'); setLayout(S.layout === 'board' ? 'list' : 'board'); }
  else if (e.key === '?') { showToast('/ 検索　n 新規案件　b ボード⇔リスト　Esc 閉じる', { title: 'ショートカット', duration: 5000 }); }
  else if (e.key === 'Escape' && $('mobileSheet').classList.contains('is-open')) $('mobileSheet').classList.remove('is-open');
});

function sheetLabel() {
  const span = document.querySelector('#mobileBackBtn span');
  if (span) span.textContent = S.layout === 'board' && !isMobile() ? '閉じる' : '戻る';
}
