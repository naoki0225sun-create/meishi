// ============================================================
// 担当者（メンバー・自分・絞り込み）と連携（カレンダー・見積書・CSV・会社情報）
// sales.js の S / api / esc などを使う
// ============================================================

const ME_KEY = 'sales.me';
const OWNER_FILTER_KEY = 'sales.ownerFilter';

async function loadMembers() {
  try {
    const res = await api('/api/sales/members');
    S.members = res.members;
    S.memberSuggestions = res.suggestions;
  } catch { S.members = []; S.memberSuggestions = []; }
  try { S.me = localStorage.getItem(ME_KEY) || ''; S.ownerFilter = localStorage.getItem(OWNER_FILTER_KEY) || ''; } catch { /* なくても動く */ }
  if (S.me && !S.members.some(m => m.name === S.me)) S.me = '';
  renderMeSelect();
}

function memberNames() { return (S.members || []).map(m => m.name); }

// 「自分」は誰か（ブラウザごとに覚える）
function renderMeSelect() {
  const el = $('meSelect');
  if (!el) return;
  if (!S.members || !S.members.length) { el.style.display = 'none'; return; }
  el.style.display = '';
  el.innerHTML = `<option value="">自分は？</option>` + memberNames().map(n =>
    `<option value="${esc(n)}" ${n === S.me ? 'selected' : ''}>${esc(n)}</option>`).join('');
  el.onchange = () => {
    S.me = el.value;
    try { localStorage.setItem(ME_KEY, S.me); } catch { /* 保存できなくても動く */ }
    if (S.ownerFilter === '@me') applyPipelineKeepSelection();
    showToast(S.me ? `「${S.me}」として使います。新しい案件の担当になります` : '自分の設定を外しました', { duration: 2000 });
  };
}

// 担当者の絞り込み（'' すべて / '@me' 自分 / '-' 担当なし / 名前）
function ownerMatch(d) {
  const f = S.ownerFilter;
  if (!f) return true;
  if (f === '@me') return S.me ? (d.owner || '') === S.me : true;
  if (f === '-') return !d.owner;
  return (d.owner || '') === f;
}

function ownerFilterHtml() {
  if (!S.members || !S.members.length) return '';
  const opt = (v, label) => `<option value="${esc(v)}" ${S.ownerFilter === v ? 'selected' : ''}>${esc(label)}</option>`;
  return `<select class="owner-filter ${S.ownerFilter ? 'is-on' : ''}" id="ownerFilter" title="担当者で絞り込み">
    ${opt('', '担当: 全員')}${opt('@me', `担当: 自分${S.me ? `（${S.me}）` : ''}`)}
    ${memberNames().filter(n => n !== S.me).map(n => opt(n, `担当: ${n}`)).join('')}${opt('-', '担当: なし')}
  </select><div class="filter-sep"></div>`;
}

function bindOwnerFilter() {
  const el = $('ownerFilter');
  if (!el) return;
  el.onchange = () => {
    S.ownerFilter = el.value;
    if (S.ownerFilter === '@me' && !S.me) showToast('右上の「自分は？」で自分を選んでください', { type: 'warning' });
    try { localStorage.setItem(OWNER_FILTER_KEY, S.ownerFilter); } catch { /* 保存できなくても動く */ }
    applyPipelineKeepSelection();
  };
}

function ownerSelectHtml(id, value, { emptyLabel = '担当なし' } = {}) {
  const names = memberNames();
  if (!names.length) return `<div class="kv-note">設定 → メンバーで担当者を登録すると選べます</div>`;
  const known = !value || names.includes(value);
  return `<select class="email-input compact" id="${id}">
    <option value="">${esc(emptyLabel)}</option>
    ${names.map(n => `<option value="${esc(n)}" ${n === value ? 'selected' : ''}>${esc(n)}</option>`).join('')}
    ${known ? '' : `<option value="${esc(value)}" selected>${esc(value)}（メンバー外）</option>`}
  </select>`;
}

function ownerBadge(d) {
  if (!d.owner) return '';
  return `<span class="owner-badge ${d.owner === S.me ? 'is-me' : ''}" title="自社の担当">${esc(d.owner)}</span>`;
}

// ── 設定：メンバー・連携 ──
function calUrl(token) { return `${location.origin}/api/sales/calendar/${token}.ics`; }

function teamSettingsHtml() {
  const ms = S.members || [];
  const ex = S.extra || {};
  const rows = ms.map(m => S.editMember === m.id ? memberForm(m) : `
    <div class="set-item"><div style="min-width:0">
      <div class="card-result-name">${esc(m.name)} ${m.name === S.me ? '<span class="owner-badge is-me">自分</span>' : ''}
        <span class="card-result-sub">進行中 ${m.open_deals}件</span></div>
      <div class="card-result-sub">${m.slack_name ? `Slack: ${esc(m.slack_name)}${m.slack_id ? '（つながり済み）' : '（最初の投稿でつながります）'}` : 'Slack: 未設定'}</div>
      <div class="cal-url"><code>${esc(calUrl(m.ics_token))}</code>
        <button class="btn-link" data-copy-cal="${esc(calUrl(m.ics_token))}">カレンダーURLをコピー</button>
        <button class="btn-link" data-reset-cal="${m.id}">作り直す</button></div>
    </div>
    <div class="set-btns"><button class="btn-sm" data-edit-member="${m.id}">編集</button>
      <button class="btn-sm" data-del-member="${m.id}">削除</button></div></div>`).join('');
  const sugg = (S.memberSuggestions || []).filter(n => !ms.some(m => m.name === n));
  return `
    <div class="section-label" style="margin-top:22px"><span>メンバー（自社の担当者）</span>
      ${S.editMember === 'new' ? '' : '<button class="section-action" id="newMember">＋ 追加</button>'}</div>
    <div class="card-result-sub" style="margin-bottom:8px">名刺管理の「所有者」と同じ名前にすると、案件の担当が自動で入ります。Slackの表示名を入れておくと、Slackで報告した人がその案件の担当になります</div>
    ${sugg.length && S.editMember !== 'new' ? `<div class="card-link-sugg"><span>名刺の所有者から追加:</span>${sugg.map(n => `<button class="chip-btn" data-add-member="${esc(n)}">＋ ${esc(n)}</button>`).join('')}</div>` : ''}
    ${S.editMember === 'new' ? memberForm({ name: '', slack_name: '', email: '' }) : ''}
    ${rows || (S.editMember === 'new' ? '' : '<div class="card-result-sub">まだいません</div>')}

    <div class="section-label" style="margin-top:22px"><span>連携</span></div>
    <div class="set-alert">
      <div><b>カレンダー</b>（次のアクション・受注予定日・打合せ・再アプローチ・リピートの時期が出ます）</div>
      <div class="card-result-sub">Googleカレンダー：左の「他のカレンダー ＋」→「URLで追加」に貼り付け。iPhone：設定 → カレンダー → アカウント → 照会するカレンダーを追加。更新は数時間ごとです</div>
      <div class="cal-url"><span class="card-result-sub">全員分:</span><code>${esc(ex.ics_token ? calUrl(ex.ics_token) : '')}</code>
        <button class="btn-link" data-copy-cal="${esc(ex.ics_token ? calUrl(ex.ics_token) : '')}">コピー</button>
        <button class="btn-link" id="resetTeamCal">作り直す</button></div>
      <div class="card-result-sub">URLを知っている人は誰でも見られます。社外に漏れたら「作り直す」を押してください</div>

      <div style="margin-top:8px"><b>見積書に載せる自社情報</b></div>
      <textarea class="email-input compact" id="companyInfo" rows="4" placeholder="ワコーメタル株式会社&#10;〒566-0000 大阪府摂津市〇〇&#10;TEL 06-0000-0000　FAX 06-0000-0000&#10;担当 日野">${esc(ex.company_info || '')}</textarea>
      <div style="text-align:right"><button class="btn-sm btn-sm-primary" id="saveExtra">保存</button></div>
    </div>`;
}

function memberForm(m) {
  return `<div class="set-form">
    <input class="email-input compact" id="memName" placeholder="名前（名刺管理の所有者と同じ表記）" maxlength="30" value="${esc(m.name)}">
    <input class="email-input compact" id="memSlack" placeholder="Slackの表示名（任意）例: hino naoki" maxlength="60" value="${esc(m.slack_name || '')}">
    <input class="email-input compact" id="memEmail" placeholder="メール（任意）" maxlength="200" value="${esc(m.email || '')}">
    <div class="set-btns" style="justify-content:flex-end"><button class="btn-sm" id="memCancel">キャンセル</button><button class="btn-sm btn-sm-primary" id="memSave">保存</button></div>
  </div>`;
}

function bindTeamSettings(rerender) {
  const B = $('settingsBody');
  const q = sel => B.querySelector(sel);
  const reload = async () => { await loadMembers(); rerender(); };
  if (q('#newMember')) q('#newMember').onclick = () => { S.editMember = 'new'; rerender(); };
  B.querySelectorAll('[data-add-member]').forEach(b => b.onclick = async () => {
    try { await api('/api/sales/members', { method: 'POST', body: JSON.stringify({ name: b.dataset.addMember }) }); await reload(); await loadDeals(); }
    catch (e) { showToast(e.message, { type: 'error' }); }
  });
  B.querySelectorAll('[data-edit-member]').forEach(b => b.onclick = () => { S.editMember = Number(b.dataset.editMember); rerender(); });
  B.querySelectorAll('[data-del-member]').forEach(b => b.onclick = async () => {
    const m = S.members.find(x => x.id === Number(b.dataset.delMember));
    if (!await showConfirm({ title: `「${m.name}」を削除しますか？`, message: 'この人が担当の案件は「担当なし」になります。', okLabel: '削除', danger: true })) return;
    try { await api(`/api/sales/members/${m.id}`, { method: 'DELETE' }); if (S.me === m.name) S.me = ''; await reload(); await loadDeals(); }
    catch (e) { showToast(e.message, { type: 'error' }); }
  });
  if (q('#memCancel')) q('#memCancel').onclick = () => { S.editMember = null; rerender(); };
  if (q('#memSave')) q('#memSave').onclick = async () => {
    const body = { name: q('#memName').value.trim(), slack_name: q('#memSlack').value.trim() || null, email: q('#memEmail').value.trim() || null };
    try {
      if (S.editMember === 'new') await api('/api/sales/members', { method: 'POST', body: JSON.stringify(body) });
      else {
        const old = S.members.find(x => x.id === S.editMember);
        await api(`/api/sales/members/${S.editMember}`, { method: 'PUT', body: JSON.stringify(body) });
        if (old && S.me === old.name) { S.me = body.name; try { localStorage.setItem(ME_KEY, S.me); } catch { /* */ } }
      }
      S.editMember = null; await reload(); await loadDeals();
    } catch (e) { showToast(e.message, { type: 'error' }); }
  };
  B.querySelectorAll('[data-copy-cal]').forEach(b => b.onclick = () => copyText(b.dataset.copyCal, 'カレンダーURL'));
  B.querySelectorAll('[data-reset-cal]').forEach(b => b.onclick = async () => {
    if (!await showConfirm({ title: 'カレンダーURLを作り直しますか？', message: '今のURLは使えなくなります。カレンダーには新しいURLを登録し直してください。', okLabel: '作り直す' })) return;
    try { await api(`/api/sales/members/${b.dataset.resetCal}/token`, { method: 'POST' }); await reload(); } catch (e) { showToast(e.message, { type: 'error' }); }
  });
  if (q('#resetTeamCal')) q('#resetTeamCal').onclick = async () => {
    if (!await showConfirm({ title: '全員分のカレンダーURLを作り直しますか？', message: '今のURLは使えなくなります。', okLabel: '作り直す' })) return;
    try { await api('/api/sales/settings/extra/token', { method: 'POST' }); S.extra = await api('/api/sales/settings/extra'); rerender(); }
    catch (e) { showToast(e.message, { type: 'error' }); }
  };
  if (q('#saveExtra')) q('#saveExtra').onclick = async () => {
    try {
      await api('/api/sales/settings/extra', { method: 'PUT', body: JSON.stringify({
        company_info: q('#companyInfo').value }) });
      S.extra = await api('/api/sales/settings/extra');
      showToast('保存しました', { type: 'success', duration: 1500 });
    } catch (e) { showToast(e.message, { type: 'error' }); }
  };
}

// ── 部長の目：担当者別 ──
function ownerBreakdownHtml(rows, isSales) {
  if (!rows || rows.length < 2 && !(rows[0] && rows[0].owner)) return '';
  return `<section class="boss-card">
    <div class="section-label"><span>担当者別（行を押すとその人だけの部長の目）</span></div>
    <table class="boss-table"><thead><tr><th>担当</th><th>進行中</th>${isSales ? '<th>金額</th><th>見込み額</th>' : ''}
      <th>要対応</th><th>部長の指摘</th><th>受注（90日）</th><th>失注（90日）</th></tr></thead><tbody>
      ${rows.map(r => `<tr class="boss-stage-row ${BOSS.owner === (r.owner || '-') ? 'is-sel' : ''}" data-owner-row="${esc(r.owner || '-')}">
        <td>${r.owner ? esc(r.owner) : '<span class="card-result-sub">担当なし</span>'}</td><td class="num">${r.open}</td>
        ${isSales ? `<td class="num">${r.amount ? yen(r.amount) : ''}</td><td class="num">${r.weighted ? yen(r.weighted) : ''}</td>` : ''}
        <td class="num">${r.needs_action || ''}</td><td class="num">${r.flags || ''}</td><td class="num">${r.won_90 || ''}</td><td class="num">${r.lost_90 || ''}</td></tr>`).join('')}
    </tbody></table></section>`;
}

function exportCsv() {
  const params = new URLSearchParams({ pipeline: S.pipeline });
  const o = S.view === 'boss' ? (BOSS.owner || '') : (S.ownerFilter === '@me' ? S.me : S.ownerFilter);
  if (o) params.set('owner', o);
  location.href = `/api/sales/export.csv?${params}`;
}
