// ============================================================
// 今日やること（最初の画面）
//   次のアクション（次の一手）を期限順に1列で。
//   「完了」→ やったことを一言 → 「次は？」と期限を決める（その場で次の一手が決まる）
//   次の一手が決まっていない案件もここに出す（決めるまで残る）
// 一覧の札（次の一手なし・動きのない日数の色分け）もここ
// sales.js の S / api / esc などを使う
// ============================================================

const TD = { open: null, mode: null, owner: null };
const TD_OWNER_KEY = 'today.owner';

// ── 一覧の札 ──
function idleDays(d) {
  const s = (d.last_activity_at || d.created_at || '').slice(0, 10);
  if (!s) return 0;
  return Math.max(0, Math.round((Date.parse(todayStr()) - Date.parse(s)) / 86400000));
}
function idleClass(d) {
  if (!d.is_open) return '';
  const st = (S.settings && S.settings.stale_days) || 14;
  const n = idleDays(d);
  return n >= st * 2 ? 'idle-r' : n >= st ? 'idle-y' : '';
}
function noNextBadge(d) {
  return d.is_open && !d.next_action ? '<span class="nonext-badge" title="次にやることが決まっていません">次の一手なし</span>' : '';
}

// ── 今日の画面 ──
const tdAddDays = n => { const t = new Date(); t.setDate(t.getDate() + n); return `${t.getFullYear()}-${pad(t.getMonth() + 1)}-${pad(t.getDate())}`; };
function tdGuessKind(text) {
  const t = text || '';
  if (/電話|架電|TEL|コール|かけ/.test(t)) return 'call';
  if (/訪問|打合せ|打ち合わせ|面談|アポ|来社|同行|見学/.test(t)) return 'meeting';
  if (/メール|送付|送る|提出|御礼|お礼/.test(t)) return 'email';
  return 'memo';
}
function tdOwnerOk(d) {
  const o = TD.owner;
  if (!o) return true;
  return (d.owner || '') === (o === '-' ? '' : o);
}
function tdGroups() {
  const today = todayStr(), week = tdAddDays(7);
  const open = (S.allDeals || []).filter(d => d.is_open && tdOwnerOk(d));
  const g = { news: [], late: [], today: [], week: [], none: [], later: 0 };
  for (const d of open) {
    if (d.vreq && d.vreq.new) g.news.push(d);
    const due = d.next_action_due;
    if (!d.next_action || !due) g.none.push(d);
    else if (due < today) g.late.push(d);
    else if (due === today) g.today.push(d);
    else if (due <= week) g.week.push(d);
    else g.later++;
  }
  const byDue = (a, b) => (a.next_action_due || '').localeCompare(b.next_action_due || '') || b.id - a.id;
  g.late.sort(byDue); g.today.sort(byDue); g.week.sort(byDue);
  // 決まっていない案件は、長く動いていない順
  g.none.sort((a, b) => idleDays(b) - idleDays(a) || b.id - a.id);
  return g;
}
function todayBadgeCount() {
  if (!S.allDeals) return 0;
  const g = tdGroups();
  return g.late.length + g.today.length;
}

function renderToday() {
  if (TD.owner === null) TD.owner = storeGet(TD_OWNER_KEY) || '';
  if (TD.owner && !S.members.some(m => m.name === TD.owner) && TD.owner !== '-') TD.owner = '';
  const g = tdGroups();
  const d0 = new Date();
  const WD = ['日', '月', '火', '水', '木', '金', '土'];
  const ownerSel = S.members.length ? `<select class="email-input compact td-owner" id="tdOwner" title="担当で絞る">
      <option value="">全員</option>${S.members.map(m => `<option value="${esc(m.name)}" ${TD.owner === m.name ? 'selected' : ''}>${esc(m.name)}</option>`).join('')}
      <option value="-" ${TD.owner === '-' ? 'selected' : ''}>担当なし</option></select>` : '';
  const sec = (key, title, list, cls, empty) => list.length ? `
    <section class="td-sec ${cls}" id="td-${key}"><div class="td-sec-h"><span>${title}</span><b>${list.length}</b></div>
      ${list.map(d => tdRowHtml(d, key)).join('')}</section>` : (empty ? `<section class="td-sec"><div class="td-sec-h"><span>${title}</span><b>0</b></div><div class="td-empty">${empty}</div></section>` : '');
  const total = g.late.length + g.today.length;
  $('viewPane').innerHTML = `
    <div class="td-wrap">
      <div class="td-head">
        <div><div class="td-title">今日やること</div>
          <div class="td-date">${d0.getMonth() + 1}月${d0.getDate()}日（${WD[d0.getDay()]}）・${total ? `今日までに ${total}件` : '今日までの分は終わっています'}</div></div>
        ${ownerSel}
      </div>
      <div class="td-tiles">
        ${[['late', '期限切れ', g.late.length, 't-red'], ['today', '今日', g.today.length, 't-amber'], ['none', '次の一手なし', g.none.length, 't-orange'], ['week', 'この先7日', g.week.length, 't-blue']]
          .map(([k, l, n, c]) => `<button class="td-tile ${c} ${n ? '' : 'is-zero'}" data-td-jump="${k}" ${n ? '' : 'disabled'}><b>${n}</b><span>${l}</span></button>`).join('')}
      </div>
      ${g.news.length ? `<section class="td-sec td-news"><div class="td-sec-h"><span>${ico('phone')} テレアポ業者から新着</span><b>${g.news.length}</b></div>
        ${g.news.map(d => `<button class="td-news-row" data-td-open="${d.id}"><span class="deal-no">#${d.id}</span> <b>${esc(tdCompany(d))}</b>
          <span>${esc((d.vreq.labels || []).join('・'))}</span><span class="td-go">開く →</span></button>`).join('')}</section>` : ''}
      ${sec('late', '期限が過ぎている', g.late, 'td-late')}
      ${sec('today', '今日', g.today, 'td-today', g.late.length ? '' : '今日が期限のものはありません')}
      ${sec('none', '次の一手が決まっていない', g.none, 'td-none')}
      ${sec('week', 'この先7日', g.week, 'td-week')}
      ${g.later ? `<div class="td-later">8日以降の予定が ${g.later}件あります（「案件」で見られます）</div>` : ''}
      ${!(S.allDeals || []).some(d => d.is_open) ? '<div class="td-empty">進行中の案件はまだありません。右上の「＋ 新規案件」から始めましょう</div>' : ''}
    </div>`;
  bindToday();
}

function tdCompany(d) { return (d.card && d.card.company) || d.company_snapshot || '(会社不明)'; }

function tdRowHtml(d, key) {
  const today = todayStr();
  const due = d.next_action_due;
  let dueTxt = '';
  if (due) {
    const n = Math.round((Date.parse(due) - Date.parse(today)) / 86400000);
    dueTxt = n < 0 ? `${fmtDate(due)}（${-n}日遅れ）` : n === 0 ? '今日' : n === 1 ? '明日' : fmtDate(due);
  }
  const pl = (d.pipeline || 'sales') !== 'sales' ? (S.meta.pipelines.find(p => p.key === d.pipeline) || {}).label : '';
  const idle = idleDays(d);
  const open = TD.open === d.id;
  const head = `<div class="td-row ${open ? 'is-open' : ''} ${idleClass(d)}" data-row="${d.id}">
    <div class="td-main" data-td-open="${d.id}" title="案件を開く">
      <div class="td-act">${d.next_action ? `${esc(d.next_action)}${!due ? '<span class="td-nodue">期限なし</span>' : ''}` : '<span class="td-noact">次の一手が決まっていません</span>'}</div>
      <div class="td-sub"><span class="deal-no">#${d.id}</span><b>${esc(tdCompany(d))}</b><span class="td-title-s">${esc(d.title || '')}</span>
        <span class="stage-badge" data-stage="${esc(d.stage)}">${esc(d.stage)}</span>${pl ? `<span class="td-pl">${esc(pl)}</span>` : ''}
        ${key === 'none' && idle >= 7 ? `<span class="td-idle">${idle}日動きなし</span>` : ''}${d.owner ? `<span class="td-owner-b">${esc(d.owner)}</span>` : ''}</div>
    </div>
    ${dueTxt ? `<span class="td-due ${key === 'late' ? 'is-late' : key === 'today' ? 'is-today' : ''}">${esc(dueTxt)}</span>` : ''}
    <div class="td-btns">${!open ? (d.next_action && d.next_action_due ? `<button class="btn-sm btn-sm-primary" data-td-done="${d.id}">完了</button>` : `<button class="btn-sm btn-sm-primary" data-td-set="${d.id}">決める</button>`) : ''}
      <button class="btn-sm td-more" data-td-more="${d.id}" title="保留・失注・段階を変える" aria-label="保留・失注・段階を変える">⋯</button></div>
  </div>`;
  if (!open) return head;
  const kinds = S.meta.activity_kinds;
  const k0 = tdGuessKind(d.next_action);
  const chips = [['明日', 1], ['3日後', 3], ['1週間後', 7], ['2週間後', 14], ['1か月後', 30]].map(([l, n]) => `<button class="mm-chip" data-td-due="${n}">${l}</button>`).join('');
  return head + `<div class="td-form" data-form="${d.id}">
    ${TD.mode === 'done' ? `
      <div class="td-f-t">✓ 「${esc(d.next_action)}」を完了</div>
      <div class="td-kinds">${['call', 'meeting', 'email', 'memo'].map(k => `<label><input type="radio" name="tdKind" value="${k}" ${k === k0 ? 'checked' : ''}> ${esc(kinds[k])}</label>`).join('')}</div>
      <textarea class="email-input compact" id="tdMemo" rows="2" maxlength="5000" placeholder="どうだったか一言（任意）例: 見積を送付。来週中に回答もらえる"></textarea>` : ''}
    <div class="td-f-t">${TD.mode === 'done' ? '次は？' : '次の一手を決める'}</div>
    <div class="td-next"><input class="email-input compact" id="tdNext" maxlength="200" placeholder="例: 見積の回答を電話で聞く" value="${TD.mode === 'set' ? esc(d.next_action || '') : ''}">
      <input type="date" class="email-input compact" id="tdDue" value="${esc(tdAddDays(7))}"></div>
    <div class="mm-chips">${chips}</div>
    <div class="td-f-btns">
      <button class="btn-link" data-td-deal="${d.id}">受注・失注になった → 案件を開く</button>
      <span class="mm-sp"></span>
      <button class="btn-sm" data-td-x="1">やめる</button>
      ${TD.mode === 'done' ? '<button class="btn-sm" id="tdNoNext" title="次の一手は後で決める">次はまだ決めない</button>' : ''}
      <button class="btn-sm btn-sm-primary" id="tdGo">${TD.mode === 'done' ? '記録して次へ' : '決める'}</button>
    </div>
  </div>`;
}

function bindToday() {
  const pane = $('viewPane');
  if ($('tdOwner')) $('tdOwner').onchange = e => { TD.owner = e.target.value; storeSet(TD_OWNER_KEY, TD.owner); renderToday(); };
  pane.querySelectorAll('[data-td-open]').forEach(b => b.onclick = () => { setView('deals'); showDeal(Number(b.dataset.tdOpen)); });
  pane.querySelectorAll('[data-td-jump]').forEach(b => b.onclick = () => { const el = $(`td-${b.dataset.tdJump}`); if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' }); });
  pane.querySelectorAll('[data-td-deal]').forEach(b => b.onclick = () => { TD.open = null; setView('deals'); showDeal(Number(b.dataset.tdDeal)); });
  const openForm = (id, mode) => {
    TD.open = id; TD.mode = mode; renderToday();
    const f = pane.querySelector(`[data-form="${id}"]`);
    if (f) { f.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); const el = f.querySelector(mode === 'done' ? '#tdMemo' : '#tdNext'); if (el) el.focus(); }
  };
  pane.querySelectorAll('[data-td-done]').forEach(b => b.onclick = () => openForm(Number(b.dataset.tdDone), 'done'));
  pane.querySelectorAll('[data-td-more]').forEach(b => b.onclick = e => { e.stopPropagation(); stageMenu(b, Number(b.dataset.tdMore)); });
  pane.querySelectorAll('[data-td-set]').forEach(b => b.onclick = () => openForm(Number(b.dataset.tdSet), 'set'));
  pane.querySelectorAll('[data-td-x]').forEach(b => b.onclick = () => { TD.open = null; renderToday(); });
  pane.querySelectorAll('[data-td-due]').forEach(b => b.onclick = () => { $('tdDue').value = tdAddDays(Number(b.dataset.tdDue)); });
  const d = (S.allDeals || []).find(x => x.id === TD.open);
  if (!d) return;
  const submit = async (withNext) => {
    const next = $('tdNext').value.trim();
    const due = $('tdDue').value || null;
    if (withNext && !next) { showToast('次にやることを書いてください（後で決めるなら「次はまだ決めない」）', { type: 'warning' }); $('tdNext').focus(); return; }
    if (withNext && !due) { showToast('期限を入れてください', { type: 'warning' }); $('tdDue').focus(); return; }
    const btns = pane.querySelectorAll('#tdGo, #tdNoNext'); btns.forEach(x => { x.disabled = true; });
    try {
      if (TD.mode === 'done') {
        const kind = (pane.querySelector('input[name="tdKind"]:checked') || {}).value || 'memo';
        const memo = $('tdMemo').value.trim();
        await api(`/api/sales/deals/${d.id}/activities`, { method: 'POST', body: JSON.stringify({
          kind, title: `完了: ${d.next_action}`.slice(0, 200), body: memo || null,
          vendor_visible: false,   // 社内のメモ。業者に見せたいときは案件の記録から
          next_action: withNext ? next : null, next_action_due: withNext ? due : null }) });
      } else {
        await api(`/api/sales/deals/${d.id}`, { method: 'PATCH', body: JSON.stringify({ next_action: next, next_action_due: due }) });
      }
      TD.open = null;
      await loadDeals();
      renderAppNav();
      if (S.view === 'today') renderToday();
      showToast(withNext ? `次は「${next}」${due ? `（${fmtDate(due)}）` : ''}` : '記録しました。次の一手は「決まっていない」に出ます', { type: 'success', duration: 2500 });
    } catch (e) { showToast(e.message, { type: 'error' }); btns.forEach(x => { x.disabled = false; }); }
  };
  if ($('tdGo')) $('tdGo').onclick = () => submit(true);
  if ($('tdNoNext')) $('tdNoNext').onclick = () => submit(false);
  if ($('tdNext')) $('tdNext').onkeydown = e => { if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); submit(true); } };
}
