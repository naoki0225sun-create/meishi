/* ============================================================
   工程表（phase37）— 2か月・2週間（案件ごと）・担当者別の3つの表示と、工程の割り当て
   現場のリーダーがタブレットで使う前提：押せる場所は大きく、1画面で判断できる量だけ出す
   ============================================================ */
const PL = {
  mode: storeGet('zumen.plmode') || 'w2',
  cat: storeGet('zumen.plcat') || 'all',
  anchor: null,
  data: null,
};
const PL_CATS = [['all', 'すべて'], ['panel', '盤の筐体'], ['laser', 'レーザー加工'], ['none', '未分類']];
const PL_MODES = [['m2', '2か月'], ['w2', '2週間'], ['staff', '担当者別']];
const DOW = ['日', '月', '火', '水', '木', '金', '土'];

const plIso = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const plDate = s => new Date(s + 'T00:00:00');
const plAdd = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
const plMonday = d => plAdd(d, -((d.getDay() + 6) % 7));
const plWeekend = d => d.getDay() === 0 || d.getDay() === 6;
const plDays = n => (n % 1 ? n.toFixed(1) : String(n)) + '日';
function plStart(end, days) {   // 終わる日と工数から始める日（サーバーと同じ数え方。0.5日・1日はその日のうち）
  let d = plDate(end);
  while (plWeekend(d)) d = plAdd(d, -1);
  for (let n = Math.ceil(days) - 1; n > 0;) { d = plAdd(d, -1); if (!plWeekend(d)) n--; }
  return plIso(d);
}

function plRange() {
  const t = new Date(); t.setHours(0, 0, 0, 0);
  if (PL.mode === 'm2') {
    const a = PL.anchor || new Date(t.getFullYear(), t.getMonth(), 1);
    PL.anchor = new Date(a.getFullYear(), a.getMonth(), 1);
    const from = plMonday(PL.anchor), end = new Date(PL.anchor.getFullYear(), PL.anchor.getMonth() + 2, 0);
    return { from, to: plAdd(plMonday(end), 6), label: `${PL.anchor.getFullYear()}年${PL.anchor.getMonth() + 1}月〜${end.getMonth() + 1}月`, in: d => d >= PL.anchor && d <= end };
  }
  // 土日に開いたときは翌週の月曜から見せる（過ぎた5日分を並べない）
  PL.anchor = plMonday(PL.anchor || (plWeekend(t) && t.getDay() === 6 ? plAdd(t, 2) : t.getDay() === 0 ? plAdd(t, 1) : t));
  const to = plAdd(PL.anchor, 13);
  return { from: PL.anchor, to, label: `${PL.anchor.getMonth() + 1}/${PL.anchor.getDate()}（月）〜 ${to.getMonth() + 1}/${to.getDate()}（日）`, in: () => true };
}

function plShift(dir) {
  if (PL.mode === 'm2') PL.anchor = new Date(PL.anchor.getFullYear(), PL.anchor.getMonth() + dir, 1);
  else PL.anchor = plAdd(PL.anchor, dir * 7);
  ZVIEWS.schedule();
}

const plCatOf = j => j.category || 'none';
const plVisible = j => PL.cat === 'all' || plCatOf(j) === PL.cat;
const plStaffColor = (sid, staff) => (staff.find(s => s.id === sid) || {}).color || '#98A2B3';

ZVIEWS.schedule = async (arg = {}) => {
  const rg = plRange();
  VP().innerHTML = '<div class="empty-text">読み込み中…</div>';
  let r;
  try { r = await api(`/api/zumen/plan/board?from=${plIso(rg.from)}&to=${plIso(rg.to)}`); } catch (e) { VP().innerHTML = `<div class="empty-text">${esc(e.message)}</div>`; return; }
  PL.data = r;
  const active = r.jobs.filter(j => j.state !== 'shipped');
  const count = k => active.filter(j => k === 'all' || plCatOf(j) === k).length;
  const vis = active.filter(plVisible);
  const late = vis.filter(j => j.state === 'late').length, soon = vis.filter(j => j.state === 'soon').length, unplanned = vis.filter(j => !j.planned && j.due).length;
  VP().innerHTML = `
    <div class="pl-top">
      <div class="seg pl-mode" id="plMode">${PL_MODES.map(([k, l]) => `<button data-m="${k}" class="${PL.mode === k ? 'is-on' : ''}">${l}</button>`).join('')}</div>
      <div class="pl-nav"><button class="pl-navbtn" id="plPrev" aria-label="前へ">‹</button><b>${esc(rg.label)}</b><button class="pl-navbtn" id="plNext" aria-label="次へ">›</button><button class="btn-link" id="plToday">今日</button></div>
      <div class="pl-sum">${late ? `<span class="pill bad">遅れ ${late}</span>` : ''}${soon ? `<span class="pill warn">納期まで7日以内 ${soon}</span>` : ''}${unplanned ? `<span class="pill">担当未定 ${unplanned}</span>` : ''}</div>
    </div>
    <div class="pl-cats" id="plCats">${PL_CATS.map(([k, l]) => `<button data-c="${k}" class="pl-cat c-${k} ${PL.cat === k ? 'is-on' : ''}"><i></i>${l}<span>${count(k)}</span></button>`).join('')}</div>
    <div id="plBody"></div>
    ${!r.staff.some(s => s.active) ? `<div class="pl-hint">${ico('user', 14)} 社員を登録すると、工程ごとに担当者と工数を割り当てられます。<button class="btn-link" id="plGoStaff">社員を登録する</button></div>` : ''}`;
  $('plMode').querySelectorAll('button').forEach(b => b.onclick = () => { PL.mode = b.dataset.m; storeSet('zumen.plmode', PL.mode); PL.anchor = null; ZVIEWS.schedule(); });
  $('plCats').querySelectorAll('button').forEach(b => b.onclick = () => { PL.cat = b.dataset.c; storeSet('zumen.plcat', PL.cat); ZVIEWS.schedule(); });
  $('plPrev').onclick = () => plShift(-1);
  $('plNext').onclick = () => plShift(1);
  $('plToday').onclick = () => { PL.anchor = null; ZVIEWS.schedule(); };
  if ($('plGoStaff')) $('plGoStaff').onclick = () => setView('settings', { focus: 'staff' });
  if (PL.mode === 'm2') plMonth(r, rg);
  else if (PL.mode === 'staff') plStaff(r, rg);
  else plGantt(r, rg);
  if (arg.job) openJob(arg.job);
};

// ── 2か月：納期のカレンダー（区分ごとに色分け。区分を選ぶと分けて見られる） ──
function plMonth(r, rg) {
  const by = {};
  r.jobs.filter(plVisible).forEach(j => { if (j.due) (by[j.due] = by[j.due] || []).push(j); });
  const weeks = [];
  for (let d = rg.from; d <= rg.to; d = plAdd(d, 7)) weeks.push(d);
  const cell = d => {
    const k = plIso(d), list = (by[k] || []).sort((a, b) => plCatOf(a).localeCompare(plCatOf(b)));
    return `<div class="pm-cell ${plWeekend(d) ? 'we' : ''} ${rg.in(d) ? '' : 'out'} ${k === r.today ? 'today' : ''}">
      <div class="pm-d">${d.getDate() === 1 ? `<b>${d.getMonth() + 1}月</b>` : ''}${d.getDate()}</div>
      ${list.map(j => `<button class="pm-job c-${plCatOf(j)} s-${j.state}" data-job="${j.deal_id}" title="${esc(j.company || '')}　${esc(j.title || '')}">
        <span class="pm-co">${esc(j.company || '')}</span><span class="pm-ti">${esc(j.title || '')}</span>${!j.planned ? '<em class="pm-un" title="担当未定"></em>' : ''}</button>`).join('')}
    </div>`;
  };
  $('plBody').innerHTML = `<div class="pm">
      <div class="pm-head">${[1, 2, 3, 4, 5, 6, 0].map(i => `<div class="${i === 0 || i === 6 ? 'we' : ''}">${DOW[i]}</div>`).join('')}</div>
      ${weeks.map(w => `<div class="pm-week">${[0, 1, 2, 3, 4, 5, 6].map(i => cell(plAdd(w, i))).join('')}</div>`).join('')}
    </div>
    <div class="pl-legend"><span><i class="lg c-panel"></i>盤の筐体</span><span><i class="lg c-laser"></i>レーザー加工</span><span><i class="lg c-none"></i>未分類</span><span><em class="pm-un"></em>担当未定</span><span>日付の欄には納期を表示しています</span><span>案件を押すと工程を割り当てられます</span></div>`;
  plBindJobs();
}

// ── 2週間：案件ごとの工程（ガント） ──
function plCols(rg) {
  const days = [];
  for (let d = rg.from; d <= rg.to; d = plAdd(d, 1)) days.push(d);
  return { days, tpl: days.map(d => plWeekend(d) ? '.55fr' : '1fr').join(' '), idx: s => Math.round((plDate(s) - rg.from) / 864e5) };
}

function plHead(cols, today) {
  return cols.days.map(d => `<div class="pg-day ${plWeekend(d) ? 'we' : ''} ${plIso(d) === today ? 'today' : ''}"><small>${DOW[d.getDay()]}</small>${d.getDate() === 1 || d === cols.days[0] ? `${d.getMonth() + 1}/` : ''}${d.getDate()}</div>`).join('');
}

function lanes(tasks, idx) {
  const ends = [];
  return tasks.map(t => {
    const s = idx(t.start_on);
    let k = ends.findIndex(e => e < s);
    if (k < 0) { k = ends.length; ends.push(-1); }
    ends[k] = idx(t.end_on);
    return k;
  });
}

function plGantt(r, rg) {
  const cols = plCols(rg), n = cols.days.length;
  const jobs = r.jobs.filter(plVisible).filter(j => j.state !== 'shipped' || (j.due && cols.idx(j.due) >= 0));
  const rows = jobs.map(j => {
    const tasks = [...j.tasks].sort((a, b) => a.start_on.localeCompare(b.start_on));
    const ln = lanes(tasks, cols.idx), nl = Math.max(1, ...ln.map(x => x + 1));
    const bars = tasks.map((t, i) => {
      const s = cols.idx(t.start_on), e = cols.idx(t.end_on);
      if (e < 0 || s >= n) return '';
      const c = t.staff_id ? plStaffColor(t.staff_id, r.staff) : null;
      return `<div class="pg-bar ${t.done ? 'done' : t.late ? 'late' : ''} ${c ? '' : 'un'} ${Math.min(n - 1, e) - Math.max(0, s) < 1 ? 'one' : ''} ${s < 0 ? 'cut-l' : ''} ${e >= n ? 'cut-r' : ''}" style="grid-column:${Math.max(0, s) + 1}/${Math.min(n, e + 1) + 1};grid-row:${ln[i] + 1};${c ? `--c:${c}` : ''}"
        title="${esc(t.label)}　${esc(t.staff || '担当未定')}　${plDays(t.days)}　${fmtMD(t.start_on)}〜${fmtMD(t.end_on)}${t.done ? '（完了）' : t.late ? '（遅れ）' : ''}">
        ${t.done ? ico('check', 11) : ''}<b>${esc(t.label)}</b><span>${esc(t.staff || '未定')}</span></div>`;
    }).join('');
    const di = j.due ? cols.idx(j.due) : -1;
    const due = di >= 0 && di < n ? `<div class="pg-due" style="grid-column:${di + 1};grid-row:1/-1" title="納期 ${fmtDate(j.due)}"><span>◆</span></div>` : '';
    const ti = cols.idx(r.today);
    const today = ti >= 0 && ti < n ? `<div class="pg-now" style="grid-column:${ti + 1};grid-row:1/-1"></div>` : '';
    return `<div class="pg-row" data-job="${j.deal_id}">
      <div class="pg-label">
        <div class="pg-l1"><i class="cat c-${plCatOf(j)}"></i><b>${esc(j.company || '')}</b>${j.state === 'late' ? '<span class="pill bad">遅れ</span>' : j.state === 'soon' ? '<span class="pill warn">7日以内</span>' : ''}</div>
        <div class="pg-l2">${esc(j.title || '')}</div>
        <div class="pg-l3">${j.due ? `納期 ${fmtMD(j.due)}` : '納期未設定'}${j.cad_staff ? `　CAD ${esc(j.cad_staff)}` : ''}${!j.planned ? '　<em>担当未定</em>' : ''}</div>
      </div>
      <div class="pg-track" style="grid-template-columns:${cols.tpl};grid-template-rows:repeat(${nl}, var(--lane)) 1fr">${cols.days.map((d, i) => `<div class="pg-bg ${plWeekend(d) ? 'we' : ''}" style="grid-column:${i + 1};grid-row:1/-1"></div>`).join('')}${today}${due}${bars}</div>
    </div>`;
  });
  $('plBody').innerHTML = `<div class="pg">
      <div class="pg-row pg-headrow"><div class="pg-label pg-hl">案件</div><div class="pg-track pg-dates" style="grid-template-columns:${cols.tpl}">${plHead(cols, r.today)}</div></div>
      ${rows.join('') || '<div class="boss-empty" style="margin:20px">この期間に作る案件はありません</div>'}
    </div>
    <div class="pl-legend"><span>帯の色は担当者</span><span><i class="lg un"></i>担当未定</span><span><b class="lg-due">◆</b>納期</span><span>案件を押すと、担当者・工数・日程を決められます</span></div>`;
  plBindJobs();
}

// ── 担当者別：社員ごとの工程と1日の負荷 ──
function plStaff(r, rg) {
  const cols = plCols(rg);
  const staff = r.staff.filter(s => s.active);
  const per = {};   // staff_id|'un' → 日付 → [{job, task}]
  r.jobs.filter(plVisible).forEach(j => {
    if (j.state === 'shipped') return;
    j.tasks.forEach(t => {
      if (t.done) return;
      const key = t.staff_id || 'un';
      for (let d = plDate(t.start_on); d <= plDate(t.end_on); d = plAdd(d, 1)) {
        if (plWeekend(d) && t.start_on !== t.end_on) continue;
        const k = plIso(d);
        ((per[key] = per[key] || {})[k] = per[key][k] || []).push({ j, t });
      }
    });
  });
  const row = (key, name, color, sub) => {
    const load = r.load[String(key)] || {};
    const total = Object.entries(load).filter(([k]) => k >= plIso(rg.from) && k <= plIso(rg.to)).reduce((a, [, v]) => a + v, 0);
    return `<div class="ps-row">
      <div class="ps-label"><i style="background:${color}"></i><div><b>${esc(name)}</b>${sub ? `<small>${sub}</small>` : ''}</div>${key !== 'un' ? `<span class="ps-total" title="この2週間の工数の合計">${plDays(Math.round(total * 2) / 2)}</span>` : ''}</div>
      <div class="ps-track" style="grid-template-columns:${cols.tpl}">${cols.days.map(d => {
        const k = plIso(d), items = (per[key] || {})[k] || [], v = load[k] || 0;
        const lv = key === 'un' ? '' : v > 1.001 ? 'over' : v >= 0.999 ? 'full' : v > 0 ? 'some' : '';
        return `<div class="ps-cell ${plWeekend(d) ? 'we' : ''} ${k === r.today ? 'today' : ''} ${lv}">
          ${v && key !== 'un' ? `<div class="ps-load">${plDays(Math.round(v * 10) / 10)}</div>` : ''}
          ${items.slice(0, 3).map(({ j, t }) => `<button class="ps-item ${t.late ? 'late' : ''}" data-job="${j.deal_id}" style="--c:${key === 'un' ? '#98A2B3' : color}" title="${esc(j.company || '')}　${esc(j.title || '')}　${esc(t.label)}　${plDays(t.days)}"><b>${esc(t.label)}</b><span>${esc(j.title || j.company || '')}</span></button>`).join('')}
          ${items.length > 3 ? `<div class="ps-more">ほか${items.length - 3}件</div>` : ''}
        </div>`;
      }).join('')}</div>
    </div>`;
  };
  $('plBody').innerHTML = `<div class="ps">
      <div class="ps-row ps-headrow"><div class="ps-label ps-hl">担当者</div><div class="ps-track pg-dates" style="grid-template-columns:${cols.tpl}">${plHead(cols, r.today)}</div></div>
      ${staff.map(s => row(s.id, s.name, s.color, s.cad ? 'CAD担当' : '')).join('')}
      ${per.un ? row('un', '担当未定', '#98A2B3', '割り当てが必要な工程') : ''}
      ${!staff.length && !per.un ? '<div class="boss-empty" style="margin:20px">この期間の工程はありません</div>' : ''}
    </div>
    <div class="pl-legend"><span><i class="lg ld-some"></i>余裕あり</span><span><i class="lg ld-full"></i>1日分</span><span><i class="lg ld-over"></i>1日分を超えています</span><span>工数は、始める日から終わる日までの平日に均等に割って数えています</span></div>`;
  plBindJobs();
}

function plBindJobs() {
  $('plBody').querySelectorAll('[data-job]').forEach(el => el.onclick = ev => { ev.stopPropagation(); openJob(Number(el.dataset.job)); });
}

// ── 工程の割り当て（タブレットで指で操作できる大きさ） ──
async function openJob(dealId) {
  const box = $('zmModalBox');
  $('zmModal').style.display = 'flex';
  box.className = 'zm-modal-box pj';
  box.innerHTML = '<div class="empty-text" style="padding:40px">読み込み中…</div>';
  let r;
  try { r = await api(`/api/zumen/plan/jobs/${dealId}`); } catch (e) { box.innerHTML = `<div class="empty-text" style="padding:40px">${esc(e.message)}</div>`; return; }
  const j = r.job, staff = r.staff;
  const st = { category: j.category || '', cad: j.cad_staff_id, note: j.note || '', tasks: j.tasks.map(t => ({ ...t })) };
  const chip = (sid, on, attr) => {
    const s = staff.find(x => x.id === sid);
    return `<button class="pj-chip ${on ? 'is-on' : ''}" ${attr} style="--c:${s ? s.color : '#98A2B3'}">${s ? `<i></i>${esc(s.name)}` : '未定'}</button>`;
  };
  const draw = () => {
    const cadStaff = [...staff.filter(s => s.cad), ...staff.filter(s => !s.cad)];
    box.innerHTML = `
      <div class="pj-head">
        <div class="pj-title"><small>${esc(j.company || '')}</small><b>${esc(j.title || '')}</b></div>
        <div class="pj-meta">${j.due ? `<span class="pj-due ${j.state}">納期 ${fmtDate(j.due)}</span>` : '<span class="pj-due nodue">納期未設定</span>'}
          ${j.stage ? `<span class="stage-badge" data-stage="${esc(j.stage)}">${esc(j.stage)}</span>` : ''}
          <a class="btn-link" href="/sales.html?deal=${j.deal_id}">${ico('list', 13)} 営業で開く</a>
          <a class="btn-link" href="/zumen-print.html?deal=${j.deal_id}" target="_blank">${ico('printer', 13)} 製作指示書</a></div>
        <button class="dupe-modal-close" id="zmModalClose" aria-label="閉じる">×</button>
      </div>
      <div class="pj-body">
        ${!staff.length ? `<div class="pl-hint">${ico('user', 14)} 社員がまだ登録されていません。<button class="btn-link" id="pjGoStaff">社員を登録する</button></div>` : ''}
        <div class="pj-sec"><div class="pj-k">区分</div>
          <div class="seg pj-seg" id="pjCat">${Object.entries(r.categories).map(([k, l]) => `<button data-c="${k}" class="${st.category === k ? 'is-on' : ''}"><i class="cat c-${k}"></i>${l}</button>`).join('')}</div></div>
        <div class="pj-sec"><div class="pj-k">CAD担当</div>
          <div class="pj-chips" id="pjCad">${chip(null, !st.cad, 'data-cad=""')}${cadStaff.map(s => chip(s.id, st.cad === s.id, `data-cad="${s.id}"`)).join('')}</div></div>
        <div class="pj-sec pj-sec-tasks"><div class="pj-k">工程<small>${j.planned ? '' : '「設定」の工程から仮に並べています。担当者と工数を決めて保存してください'}</small></div>
          <div class="pj-tasks">${st.tasks.map((t, i) => `
            <div class="pj-task ${t.done ? 'done' : ''}" data-i="${i}">
              <div class="pj-t1">
                <input class="email-input pj-label" data-f="label" value="${esc(t.label)}" maxlength="20" aria-label="工程名">
                <div class="pj-step" aria-label="工数"><button data-step="-1" aria-label="工数を減らす">−</button><b>${plDays(t.days)}</b><button data-step="1" aria-label="工数を増やす">＋</button></div>
                <label class="pj-date"><span>終わる日</span><input type="date" class="email-input" data-f="end_on" value="${esc(t.end_on)}"></label>
                <span class="pj-span">${t.end_on ? (() => { const a = plStart(t.end_on, t.days); return a !== t.end_on ? `${fmtMD(a)}〜${fmtMD(t.end_on)}` : `${fmtMD(t.end_on)}のうち`; })() : ''}</span>
                ${t.done ? `<span class="pill good">${ico('check', 11)} 完了 ${fmtMD(t.done)}</span>` : ''}
                <button class="pj-del" data-del="${i}" aria-label="この工程を削除">×</button>
              </div>
              <div class="pj-chips">${chip(null, !t.staff_id, 'data-who=""')}${staff.map(s => chip(s.id, t.staff_id === s.id, `data-who="${s.id}"`)).join('')}</div>
            </div>`).join('')}</div>
          <div class="pj-acts-row"><button class="btn-link" id="pjAdd">＋ 工程を追加</button>${j.planned ? '<button class="btn-link" id="pjReset">「設定」の工程から作り直す</button>' : ''}</div>
        </div>
        <div class="pj-sec"><div class="pj-k">メモ</div><textarea class="email-input" id="pjNote" rows="2" maxlength="500" placeholder="現場への伝言など（任意）">${esc(st.note)}</textarea></div>
      </div>
      <div class="pj-foot"><span class="pj-upd">${j.updated_at && j.planned ? `最終更新 ${esc(j.updated_at.slice(0, 16).replace('T', ' '))}${j.updated_by ? `（${esc(j.updated_by)}）` : ''}` : ''}</span>
        <button class="btn-sm" id="pjCancel">キャンセル</button><button class="btn-sm btn-sm-primary pj-save" id="pjSave">保存</button></div>`;
    $('zmModalClose').onclick = zmCloseModal;
    $('pjCancel').onclick = zmCloseModal;
    if ($('pjGoStaff')) $('pjGoStaff').onclick = () => { zmCloseModal(); setView('settings', { focus: 'staff' }); };
    $('pjCat').querySelectorAll('button').forEach(b => b.onclick = () => { st.category = st.category === b.dataset.c ? '' : b.dataset.c; draw(); });
    $('pjCad').querySelectorAll('[data-cad]').forEach(b => b.onclick = () => { st.cad = b.dataset.cad ? Number(b.dataset.cad) : null; draw(); });
    box.querySelectorAll('.pj-task').forEach(el => {
      const t = st.tasks[Number(el.dataset.i)];
      el.querySelector('[data-f="label"]').oninput = e => { t.label = e.target.value; };
      el.querySelector('[data-f="end_on"]').onchange = e => { t.end_on = e.target.value; t.start_on = null; draw(); };
      el.querySelectorAll('[data-step]').forEach(b => b.onclick = () => { t.days = Math.min(60, Math.max(0.5, t.days + Number(b.dataset.step) * 0.5)); t.start_on = null; draw(); });
      el.querySelectorAll('[data-who]').forEach(b => b.onclick = () => { t.staff_id = b.dataset.who ? Number(b.dataset.who) : null; draw(); });
    });
    box.querySelectorAll('[data-del]').forEach(b => b.onclick = () => { st.tasks.splice(Number(b.dataset.del), 1); draw(); });
    $('pjAdd').onclick = () => {
      const last = st.tasks[st.tasks.length - 1];
      st.tasks.push({ label: '', staff_id: null, days: 1, end_on: last ? last.end_on : (j.due || todayStr()), start_on: null });
      draw();
      const inputs = box.querySelectorAll('.pj-label'); inputs[inputs.length - 1].focus();
    };
    if ($('pjReset')) $('pjReset').onclick = async () => {
      if (!await showConfirm({ title: '工程を作り直す', message: '担当者と工数の割り当てを消して、「設定」の工程から仮の予定を並べ直します。区分とCAD担当は残ります。', okLabel: '作り直す' })) return;
      try { await api(`/api/zumen/plan/jobs/${dealId}/tasks`, { method: 'DELETE' }); zmCloseModal(); showToast('仮の予定に戻しました'); ZVIEWS.schedule({ job: dealId }); } catch (e) { showToast(e.message, { type: 'error' }); }
    };
    $('pjNote').oninput = e => { st.note = e.target.value; };
    $('pjSave').onclick = async () => {
      const tasks = st.tasks.map(t => ({ label: (t.label || '').trim(), staff_id: t.staff_id, days: t.days, end_on: t.end_on }));
      if (tasks.some(t => !t.label)) { showToast('工程名が空欄の行があります', { type: 'warning' }); return; }
      if (tasks.some(t => !t.end_on)) { showToast('終わる日が空欄の行があります', { type: 'warning' }); return; }
      $('pjSave').disabled = true;
      try {
        await put(`/api/zumen/plan/jobs/${dealId}`, { category: st.category, cad_staff_id: st.cad, note: st.note, tasks, actor: Z.me || null });
        zmCloseModal();
        const un = tasks.filter(t => !t.staff_id).length;
        showToast(un ? `保存しました。担当未定の工程が${un}件あります` : '保存しました', { type: un ? 'warning' : 'success' });
        if (Z.view === 'schedule') ZVIEWS.schedule(); else if (Z.view === 'site') ZVIEWS.site();
      } catch (e) { showToast(e.message, { type: 'error' }); $('pjSave').disabled = false; }
    };
  };
  draw();
}

// ── 設定：社員 ──
async function staffCard() {
  const r = await api('/api/zumen/plan/staff');
  return { html: `<section class="boss-card c-blue" id="staffCard"><div class="section-label"><span>社員（工程の担当者）</span></div>
      <div class="zr-saved">工程表で担当者・CAD担当として選べる人です。ログインしない現場の方も登録できます。色は工程表の帯の色になります（押すと変わります）</div>
      <div id="stfRows" class="stf-rows"></div>
      <div class="zr-actions" style="position:static;background:none"><button class="btn-link" id="stfAdd">＋ 社員を追加</button><button class="btn-sm btn-sm-primary" id="stfSave">保存</button></div>
    </section>`, bind: () => bindStaff(r) };
}

function bindStaff(r) {
  const rows = r.staff.map(s => ({ id: s.id, name: s.name, color: s.color, cad: !!s.cad, active: !!s.active }));
  const draw = () => {
    $('stfRows').innerHTML = rows.map((s, i) => `<div class="stf-row ${s.active ? '' : 'off'}" data-i="${i}">
        <button class="stf-color" data-color style="background:${s.color || r.palette[i % r.palette.length]}" aria-label="色を変える"></button>
        <input class="email-input" data-name value="${esc(s.name)}" maxlength="30" placeholder="名前（例：田中）">
        <label class="zr-check"><input type="checkbox" data-cad ${s.cad ? 'checked' : ''}> CAD担当</label>
        <label class="zr-check"><input type="checkbox" data-active ${s.active ? 'checked' : ''}> 選べるようにする</label>
        <button class="pj-del" data-del aria-label="削除">×</button>
      </div>`).join('') || '<div class="boss-empty">まだ登録されていません。「＋ 社員を追加」から登録してください</div>';
    $('stfRows').querySelectorAll('.stf-row').forEach(el => {
      const s = rows[Number(el.dataset.i)];
      el.querySelector('[data-name]').oninput = e => { s.name = e.target.value; };
      el.querySelector('[data-cad]').onchange = e => { s.cad = e.target.checked; };
      el.querySelector('[data-active]').onchange = e => { s.active = e.target.checked; draw(); };
      el.querySelector('[data-color]').onclick = () => { const p = r.palette, k = p.indexOf(s.color); s.color = p[(k + 1) % p.length]; draw(); };
      el.querySelector('[data-del]').onclick = () => { rows.splice(Number(el.dataset.i), 1); draw(); };
    });
  };
  draw();
  $('stfAdd').onclick = () => { rows.push({ name: '', color: r.palette[rows.length % r.palette.length], cad: false, active: true }); draw(); const ins = $('stfRows').querySelectorAll('[data-name]'); ins[ins.length - 1].focus(); };
  $('stfSave').onclick = async () => {
    const list = rows.filter(s => s.name.trim());
    try {
      const res = await put('/api/zumen/plan/staff', { staff: list });
      rows.splice(0, rows.length, ...res.staff.map(s => ({ id: s.id, name: s.name, color: s.color, cad: !!s.cad, active: !!s.active })));
      draw();
      const kept = res.staff.filter(x => !x.active && !list.some(l => l.id === x.id)).length;
      showToast(kept ? `保存しました。工程に入ったことのある${kept}人は、記録を残すため「選べるようにする」を外して残しています` : '保存しました', { type: 'success', duration: kept ? 4200 : 1800 });
    } catch (e) { showToast(e.message, { type: 'error' }); }
  };
}
