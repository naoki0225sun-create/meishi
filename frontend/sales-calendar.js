// ============================================================
// みんなの予定（共有カレンダー）
//   名前を選ぶだけ（アカウントは分けない）。日を選んで 午前／午後前半／午後後半／終日／時間を入力 で予定を入れる
//   案件のアポも自動で載る。テレアポ業者は時間帯と名前だけ見られる
// sales.js の S / api / esc などを使う
// ============================================================

const CAL = { people: [], slots: {}, events: [], month: null, day: null, me: null, hidden: {}, editing: null, adding: 'am' };
const CAL_ME_KEY = 'cal.me';
const WEEK = ['日', '月', '火', '水', '木', '金', '土'];
const ymd = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const hm = m => `${Math.floor(m / 60)}:${pad(m % 60)}`;

function calMe() {
  if (CAL.me === 'view') return 'view';
  return CAL.people.find(p => p.id === CAL.me) || null;
}

async function renderCalendar() {
  const pane = $('viewPane');
  if (!CAL.month) { const t = new Date(); CAL.month = new Date(t.getFullYear(), t.getMonth(), 1); CAL.day = ymd(t); }
  pane.innerHTML = '<div class="empty-text">読み込み中…</div>';
  try {
    const p = await api('/api/sales/cal/people');
    CAL.people = p.people; CAL.slots = p.slots;
    const saved = storeGet(CAL_ME_KEY);
    CAL.me = saved === 'view' ? 'view' : (CAL.people.find(x => String(x.id) === saved) ? Number(saved) : null);
    await calLoad();
  } catch (e) { pane.innerHTML = `<div class="empty-text">${esc(e.message)}</div>`; return; }
  drawCalendar();
  if (!CAL.me) calPickMe();
}

async function calLoad() {
  const first = new Date(CAL.month.getFullYear(), CAL.month.getMonth(), 1);
  const start = new Date(first); start.setDate(1 - first.getDay());
  const end = new Date(start); end.setDate(start.getDate() + 41);
  CAL.range = [ymd(start), ymd(end)];
  CAL.events = (await api(`/api/sales/cal/events?frm=${CAL.range[0]}&to=${CAL.range[1]}`)).events;
}

function calVisible(e) { return !CAL.hidden[e.person]; }

function drawCalendar() {
  const pane = $('viewPane');
  const m = CAL.month;
  const first = new Date(m.getFullYear(), m.getMonth(), 1);
  const start = new Date(first); start.setDate(1 - first.getDay());
  const today = ymd(new Date());
  const byDay = {};
  CAL.events.filter(calVisible).forEach(e => (byDay[e.day] = byDay[e.day] || []).push(e));
  let cells = '';
  for (let i = 0; i < 42; i++) {
    const d = new Date(start); d.setDate(start.getDate() + i);
    const k = ymd(d);
    const evs = byDay[k] || [];
    const out = d.getMonth() !== m.getMonth();
    cells += `<button class="cal-cell ${out ? 'is-out' : ''} ${k === today ? 'is-today' : ''} ${k === CAL.day ? 'is-sel' : ''} wd-${d.getDay()}" data-day="${k}">
      <span class="cal-num">${d.getDate()}</span>
      <span class="cal-evs">${evs.slice(0, 3).map(e => `<span class="cal-pill ${e.kind} ${e.kind === 'event' && e.all_day ? 'is-allday' : ''}" style="--c:${esc(e.color)}" ${e.kind === 'event' && e.id ? `data-ev="${e.id}" title="押すと変更・削除"` : ''}><i></i>${esc(e.person)} ${esc(e.kind === 'appo' ? 'アポ' : e.label)}</span>`).join('')}
        ${evs.length > 3 ? `<span class="cal-more">ほか${evs.length - 3}件</span>` : ''}</span>
    </button>`;
  }
  const me = calMe();
  const legend = CAL.people.map(p => `<button class="cal-person ${CAL.hidden[p.name] ? 'is-off' : ''}" data-person="${esc(p.name)}" style="--c:${esc(p.color)}" title="押すと表示・非表示">
    <i></i>${esc(p.name)}</button>`).join('');
  pane.innerHTML = `
    <div class="cal-wrap">
      <div class="cal-main">
        <div class="cal-head">
          <div class="cal-nav">
            <button class="icon-btn" id="calPrev" aria-label="前の月">‹</button>
            <div class="cal-title">${m.getFullYear()}年 ${m.getMonth() + 1}月</div>
            <button class="icon-btn" id="calNext" aria-label="次の月">›</button>
            <button class="btn-sm" id="calToday">今日</button>
          </div>
          <button class="cal-me" id="calMeBtn">${me === 'view' ? '見るだけ' : me ? `<i style="--c:${esc(me.color)}"></i>${esc(me.name)}として入力` : '名前を選ぶ'}<span>変更</span></button>
        </div>
        <div class="cal-legend">${legend || '<span class="card-result-sub">まだ誰も予定を入れていません</span>'}
          <span class="cal-legend-note">業者の画面には、名前と時間帯だけが出ます</span></div>
        <div class="cal-grid">
          ${WEEK.map((w, i) => `<div class="cal-wd wd-${i}">${w}</div>`).join('')}
          ${cells}
        </div>
      </div>
      <aside class="cal-side" id="calSide">${calDayHtml()}</aside>
    </div>`;
  bindCalendar();
}

function calDayHtml() {
  const k = CAL.day;
  if (!k) return '<div class="empty-text">日を選んでください</div>';
  const d = new Date(k + 'T00:00:00');
  const evs = CAL.events.filter(e => e.day === k);
  const me = calMe();
  const slotBtns = Object.entries(CAL.slots).map(([key, s]) => `
    <button class="slot-btn ${CAL.adding === key ? 'is-on' : ''} ${key === 'day' ? 'is-allday' : ''}" data-slot="${key}"><b>${esc(s.label)}</b><span>${key === 'day' ? '一日空いていない' : `${hm(s.start)}〜${hm(s.end)}`}</span></button>`).join('')
    + `<button class="slot-btn ${CAL.adding === 'custom' ? 'is-on' : ''}" data-slot="custom"><b>時間を入力</b><span>例 10:30〜11:15</span></button>`;
  const list = evs.map(e => `
    <div class="cal-item ${e.kind}" style="--c:${esc(e.color)}">
      <i></i>
      <div class="cal-item-body">
        <div class="cal-item-top"><b>${esc(e.person)}</b><span>${e.all_day ? (e.kind === 'appo' ? 'アポ（時間未定）' : '終日') : `${hm(e.start)}〜${hm(e.end)}`}</span>
          ${e.kind === 'event' && e.label && e.slot ? `<span class="cal-slot">${esc(e.label)}</span>` : ''}</div>
        ${e.title ? `<div class="cal-item-title">${esc(e.title)}</div>` : ''}
        ${e.memo ? `<div class="cal-item-memo">${esc(e.memo)}</div>` : ''}
        ${e.kind === 'appo' && e.address ? `<div class="cal-item-place">${ico('pin', 12)}<span>${esc(e.address)}</span><a href="https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(e.address.replace(/^〒\S+\s*/, ''))}" target="_blank" rel="noopener">地図</a></div>` : ''}
        ${e.kind === 'appo' && (e.contact || e.phone) ? `<div class="cal-item-memo">${esc([e.contact, e.phone].filter(Boolean).join('・'))}</div>` : ''}
        ${e.kind === 'appo' && e.note ? `<div class="cal-item-note">${esc(e.note)}</div>` : ''}
      </div>
      ${e.kind === 'appo' ? `<button class="btn-link" data-open-deal="${e.deal_id}">案件を開く</button>`
        : me && me !== 'view' ? `<span class="cal-item-btns"><button class="btn-sm" data-cal-edit="${e.id}">変更</button><button class="btn-sm cal-del" data-cal-del="${e.id}">削除</button></span>` : ''}
    </div>`).join('') || '<div class="cal-free">この日はまだ予定がありません</div>';
  const ed = CAL.editing ? CAL.events.find(e => e.kind === 'event' && e.id === CAL.editing) : null;
  const form = me === 'view' || !me ? `<div class="cal-viewonly">${me === 'view' ? '見るだけで開いています。予定を入れる・変えるときは右上で名前を選んでください。' : '予定を入れるには名前を選んでください。'}</div>` : ed ? `
    <div class="cal-add is-edit" id="calEditBox">
      <div class="cal-add-title">予定を変える</div>
      <div class="cal-edit-row"><label>だれ</label><select class="email-input compact" id="calWho">${CAL.people.map(p => `<option value="${p.id}" ${p.id === ed.person_id ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}</select></div>
      <div class="cal-edit-row"><label>日にち</label><input type="date" class="email-input compact" id="calDay" value="${esc(ed.day)}"></div>
      <div class="slot-grid">${slotBtns}</div>
      <div class="cal-custom" style="${CAL.adding === 'custom' ? '' : 'display:none'}">
        <input type="time" class="email-input compact" id="calStart" value="${hm(ed.start).padStart(5, '0')}" step="300"><span>〜</span>
        <input type="time" class="email-input compact" id="calEnd" value="${ed.end >= 1440 ? '23:59' : hm(ed.end).padStart(5, '0')}" step="300">
      </div>
      <input class="email-input compact" id="calTitle" maxlength="80" placeholder="件名（任意）" value="${esc(ed.title || '')}">
      <textarea class="email-input compact" id="calMemo" maxlength="500" rows="2" placeholder="メモ（任意）">${esc(ed.memo || '')}</textarea>
      <div class="cal-edit-btns"><button class="btn-sm cal-del" data-cal-del="${ed.id}">削除</button><span></span>
        <button class="btn-sm" id="calCancel">やめる</button><button class="btn-sm btn-sm-primary" id="calUpdate">変更を保存</button></div>
    </div>` : `
    <div class="cal-add">
      <div class="cal-add-title"><i style="--c:${esc(me.color)}"></i>${esc(me.name)}の予定を入れる</div>
      <div class="slot-grid">${slotBtns}</div>
      <div class="cal-custom" style="${CAL.adding === 'custom' ? '' : 'display:none'}">
        <input type="time" class="email-input compact" id="calStart" value="10:00" step="300"><span>〜</span>
        <input type="time" class="email-input compact" id="calEnd" value="11:00" step="300">
      </div>
      <input class="email-input compact" id="calTitle" maxlength="80" placeholder="件名（任意）例: 外出・社内会議">
      <button class="btn-sm btn-sm-primary cal-save" id="calSave">この日に入れる</button>
    </div>`;
  return `
    <div class="cal-day-head"><div class="cal-day-date">${d.getMonth() + 1}月${d.getDate()}日<span class="wd-${d.getDay()}">（${WEEK[d.getDay()]}）</span></div>
      <div class="cal-day-sum">${evs.length ? `予定 ${evs.length}件` : '空き'}</div></div>
    ${calSlotSummary(evs)}
    <div class="cal-list">${list}</div>
    ${form}`;
}

// 午前・午後前半・午後後半に誰の予定があるか（テレアポのアポ取りの目安）
function calSlotSummary(evs) {
  const ad = [...new Set(evs.filter(e => e.kind === 'event' && e.all_day).map(e => e.person))];
  const adRow = ad.length ? `<div class="cal-sum-row is-allday"><span>終日</span><em>${esc(ad.join('・'))}（一日空いていません）</em></div>` : '';
  const rows = adRow + ['am', 'pm1', 'pm2'].map(k => {
    const s = CAL.slots[k];
    if (!s) return '';
    const who = [...new Set(evs.filter(e => e.start < s.end && e.end > s.start).map(e => e.person))];
    return `<div class="cal-sum-row ${who.length ? 'is-busy' : ''}"><span>${esc(s.label)}</span><em>${who.length ? esc(who.join('・')) : '空き'}</em></div>`;
  }).join('');
  return `<div class="cal-sum">${rows}</div>`;
}

function bindCalendar() {
  const pane = $('viewPane');
  const move = async n => { CAL.month = new Date(CAL.month.getFullYear(), CAL.month.getMonth() + n, 1); await calLoad(); drawCalendar(); };
  $('calPrev').onclick = () => move(-1);
  $('calNext').onclick = () => move(1);
  $('calToday').onclick = async () => { const t = new Date(); CAL.month = new Date(t.getFullYear(), t.getMonth(), 1); CAL.day = ymd(t); await calLoad(); drawCalendar(); };
  $('calMeBtn').onclick = calPickMe;
  pane.querySelectorAll('[data-day]').forEach(b => b.onclick = e => {
    const pill = e.target.closest('[data-ev]');
    if (pill && calMe() && calMe() !== 'view') calStartEdit(Number(pill.dataset.ev));
    else if (CAL.editing) { CAL.editing = null; CAL.adding = 'am'; }
    CAL.day = b.dataset.day;
    pane.querySelectorAll('.cal-cell').forEach(x => x.classList.toggle('is-sel', x === b));
    $('calSide').innerHTML = calDayHtml(); bindCalSide();
    if (isMobile()) $('calSide').scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
  pane.querySelectorAll('[data-person]').forEach(b => b.onclick = () => { CAL.hidden[b.dataset.person] = !CAL.hidden[b.dataset.person]; drawCalendar(); });
  bindCalSide();
}

function bindCalSide() {
  const side = $('calSide');
  side.querySelectorAll('[data-slot]').forEach(b => b.onclick = () => {
    CAL.adding = b.dataset.slot;
    side.querySelectorAll('[data-slot]').forEach(x => x.classList.toggle('is-on', x === b));
    side.querySelector('.cal-custom').style.display = CAL.adding === 'custom' ? '' : 'none';
  });
  side.querySelectorAll('[data-open-deal]').forEach(b => b.onclick = () => { setView('deals'); showDeal(Number(b.dataset.openDeal)); });
  side.querySelectorAll('[data-cal-del]').forEach(b => b.onclick = async () => {
    const ev = CAL.events.find(x => x.kind === 'event' && x.id === Number(b.dataset.calDel));
    const ok = await showConfirm({ title: '予定を削除', message: ev ? `${ev.person}「${ev.title || ev.label}」（${ev.day.slice(5).replace('-', '/')}）を削除します。` : 'この予定を削除します。', okLabel: '削除', danger: true });
    if (!ok) return;
    try { await api(`/api/sales/cal/events/${b.dataset.calDel}`, { method: 'DELETE' }); CAL.editing = null; CAL.adding = 'am'; await calLoad(); drawCalendar(); showToast('予定を削除しました', { type: 'success', duration: 1500 }); }
    catch (e) { showToast(e.message, { type: 'error' }); }
  });
  side.querySelectorAll('[data-cal-edit]').forEach(b => b.onclick = () => { calStartEdit(Number(b.dataset.calEdit)); $('calSide').innerHTML = calDayHtml(); bindCalSide();
    const box = $('calEditBox'); if (box) box.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); });
  if (side.querySelector('#calCancel')) side.querySelector('#calCancel').onclick = () => { CAL.editing = null; CAL.adding = 'am'; $('calSide').innerHTML = calDayHtml(); bindCalSide(); };
  const upd = side.querySelector('#calUpdate');
  if (upd) upd.onclick = async () => {
    const day = side.querySelector('#calDay').value;
    if (!day) { showToast('日にちを入れてください', { type: 'warning' }); return; }
    const body = { person_id: Number(side.querySelector('#calWho').value), day, title: side.querySelector('#calTitle').value.trim() || null,
      memo: side.querySelector('#calMemo').value.trim() || null };
    if (CAL.adding === 'custom') { body.start = side.querySelector('#calStart').value; body.end = side.querySelector('#calEnd').value; }
    else body.slot = CAL.adding;
    upd.disabled = true;
    try {
      await api(`/api/sales/cal/events/${CAL.editing}`, { method: 'PUT', body: JSON.stringify(body) });
      CAL.editing = null; CAL.adding = 'am'; CAL.day = day;
      const d = new Date(day + 'T00:00:00');
      if (d.getFullYear() !== CAL.month.getFullYear() || d.getMonth() !== CAL.month.getMonth()) CAL.month = new Date(d.getFullYear(), d.getMonth(), 1);
      await calLoad(); drawCalendar(); showToast('予定を変えました', { type: 'success', duration: 1500 });
    } catch (e) { showToast(e.message, { type: 'error' }); upd.disabled = false; }
  };
  const save = side.querySelector('#calSave');
  if (save) save.onclick = async () => {
    const me = calMe();
    const body = { person_id: me.id, day: CAL.day, title: side.querySelector('#calTitle').value.trim() || null };
    if (CAL.adding === 'custom') { body.start = side.querySelector('#calStart').value; body.end = side.querySelector('#calEnd').value; }
    else body.slot = CAL.adding;
    save.disabled = true;
    try { await api('/api/sales/cal/events', { method: 'POST', body: JSON.stringify(body) }); await calLoad(); drawCalendar(); showToast('予定を入れました', { type: 'success', duration: 1500 }); }
    catch (e) { showToast(e.message, { type: 'error' }); save.disabled = false; }
  };
}

// 予定の変更を始める（時間帯の予定はその時間帯、それ以外は「時間を入力」）
function calStartEdit(id) {
  const ev = CAL.events.find(x => x.kind === 'event' && x.id === id);
  if (!ev) return;
  CAL.editing = id; CAL.day = ev.day;
  CAL.adding = ev.slot && CAL.slots[ev.slot] ? ev.slot : 'custom';
}

// 名前を選ぶ（初めての人は名前を入れる・見るだけ）
function calPickMe() {
  const ov = document.createElement('div');
  ov.className = 'cal-pick-ov';
  ov.innerHTML = `<div class="cal-pick" role="dialog" aria-label="名前を選ぶ">
    <div class="cal-pick-title">カレンダーを使う人</div>
    <div class="cal-pick-sub">このパソコン・スマホで使う名前を選んでください。次からは自動でこの名前になります。</div>
    ${CAL.people.length ? `<div class="cal-pick-list">${CAL.people.map(p => `<button class="cal-pick-person" data-pid="${p.id}" style="--c:${esc(p.color)}"><i></i>${esc(p.name)}</button>`).join('')}</div>` : ''}
    <div class="cal-pick-new"><input class="email-input" id="calNewName" maxlength="30" placeholder="${CAL.people.length ? '初めての人は名前を入力' : '名前を入力（例: 日野）'}">
      <button class="btn-sm btn-sm-primary" id="calNewBtn">この名前で使う</button></div>
    <button class="cal-pick-view" id="calViewOnly">見るだけ（予定は入れない）</button>
  </div>`;
  document.body.appendChild(ov);
  const done = v => { CAL.me = v; storeSet(CAL_ME_KEY, String(v)); ov.remove(); drawCalendar(); };
  ov.querySelectorAll('[data-pid]').forEach(b => b.onclick = () => done(Number(b.dataset.pid)));
  ov.querySelector('#calViewOnly').onclick = () => done('view');
  const add = async () => {
    const name = ov.querySelector('#calNewName').value.trim();
    if (!name) { ov.querySelector('#calNewName').focus(); return; }
    try {
      const p = await api('/api/sales/cal/people', { method: 'POST', body: JSON.stringify({ name }) });
      if (!CAL.people.some(x => x.id === p.id)) CAL.people.push(p);
      done(p.id);
    } catch (e) { showToast(e.message, { type: 'error' }); }
  };
  ov.querySelector('#calNewBtn').onclick = add;
  ov.querySelector('#calNewName').onkeydown = e => { if (e.key === 'Enter') add(); };
  ov.onclick = e => { if (e.target === ov && CAL.me) ov.remove(); };
  setTimeout(() => ov.querySelector('#calNewName').focus(), 50);
}
