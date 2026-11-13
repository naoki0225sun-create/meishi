// ============================================================
// テレアポ業者との連携（こちら側の画面）
//   設定：業者の追加・専用URLとパスワードの発行・再発行・停止
//   一覧：テレアポ（業者）で絞り込み・札
//   詳細：業者・架電日時・アポ日時、記録ごとに「業者にも見せる／社内だけ」
// sales.js の S / api / esc などを使う
// ============================================================

const VEN = { list: null, shown: null };   // shown: { id, name, url, password } 発行直後だけ表示

function partnerUrl(slug) { return `${location.origin}/partner.html?k=${encodeURIComponent(slug)}`; }

async function loadVendors() {
  try { VEN.list = (await api('/api/sales/vendors')).vendors; } catch { VEN.list = VEN.list || []; }
  return VEN.list;
}

function shareText(v) {
  return `${v.name} 様\n\nテレアポ報告の専用ページです。\nURL: ${v.url}\n${v.password ? `パスワード: ${v.password}\n` : ''}\n`
    + '新しくお電話した会社は「＋ 新しいテレアポを記録」から、2回目以降のお電話は各社の画面の「電話の記録を追加」から入力してください。\n'
    + 'こちらで商談が進むと、各社の画面に「更新あり」と表示されます。\n'
    + 'こちらから再テレアポや資料の到着確認をお願いするときは「依頼」として一覧の一番上に届きます。電話したら各社の画面から結果を記録してください。\n'
    + 'URLとパスワードは社外に共有しないようお願いいたします。';
}

function vendorSettingsHtml() {
  const list = VEN.list || [];
  const sh = VEN.shown;
  const rows = list.map(v => `
    <div class="set-item vendor-item ${v.active ? '' : 'is-off'}">
      <div>
        <div class="card-result-name">${ico('phone')} ${esc(v.name)} ${v.active ? '' : '<span class="missing-badge">停止中</span>'}</div>
        <div class="card-result-sub">記録 ${v.stats.total}件・アポ ${v.stats.appo}件・受注 ${v.stats.won}件${v.stats.won_amount ? `（${yen(v.stats.won_amount)}）` : ''}
          ${v.last_login_at ? `／最終ログイン ${esc(fmtDateTime(v.last_login_at))}` : '／まだログインしていません'}</div>
        <div class="vendor-url"><code>${esc(partnerUrl(v.slug))}</code><button class="btn-link" data-v-copy-url="${v.id}">URLをコピー</button></div>
      </div>
      <div class="set-btns">
        <button class="btn-sm" data-v-pw="${v.id}" title="今のパスワードは使えなくなります">パスワード再発行</button>
        <button class="btn-sm" data-v-url="${v.id}" title="今のURLは使えなくなります">URL再発行</button>
        <button class="btn-sm" data-v-active="${v.id}" data-on="${v.active ? 0 : 1}">${v.active ? '停止' : '再開'}</button>
      </div>
    </div>
    ${sh && sh.id === v.id ? `<div class="vendor-issued">
      <div class="vendor-issued-title">${ico('key')} ${esc(v.name)} の入口を発行しました（パスワードはこの画面でしか見られません）</div>
      <div class="kv-grid"><div class="k">URL</div><div class="v"><code>${esc(sh.url)}</code></div>
        ${sh.password ? `<div class="k">パスワード</div><div class="v"><code class="pw">${esc(sh.password)}</code></div>` : ''}</div>
      <textarea class="email-input compact" rows="7" readonly id="vShareText">${esc(shareText(sh))}</textarea>
      <div class="set-btns"><button class="btn-sm btn-sm-primary" id="vCopyShare">案内文をコピー</button><button class="btn-sm" id="vHide">閉じる</button></div>
    </div>` : ''}`).join('');
  return `
    <div class="section-label" style="margin-top:22px"><span>テレアポ業者</span></div>
    <div class="card-result-sub" style="margin-bottom:8px">業者ごとに専用のURLとパスワードを発行します。業者は自分が記録した会社だけを見られ、こちらの金額・名刺・社内のメモ・ほかの案件は見えません。</div>
    ${rows || ''}
    <div class="vendor-add"><input class="email-input compact" id="vNewName" placeholder="業者名（例: テレアポ株式会社A）" maxlength="60">
      <button class="btn-sm btn-sm-primary" id="vAdd">＋ 業者を追加して入口を発行</button></div>`;
}

function bindVendorSettings(rerender) {
  const B = $('settingsBody');
  const after = async (msg) => { await loadVendors(); rerender(); if (msg) showToast(msg, { type: 'success', duration: 2000 }); };
  if ($('vAdd')) $('vAdd').onclick = async () => {
    const name = $('vNewName').value.trim();
    if (!name) { showToast('業者名を入れてください', { type: 'warning' }); return; }
    try {
      const v = await api('/api/sales/vendors', { method: 'POST', body: JSON.stringify({ name }) });
      VEN.shown = { id: v.id, name: v.name, url: partnerUrl(v.slug), password: v.password };
      await after('入口を発行しました');
    } catch (e) { showToast(e.message, { type: 'error' }); }
  };
  B.querySelectorAll('[data-v-copy-url]').forEach(b => b.onclick = () => {
    const v = VEN.list.find(x => x.id === Number(b.dataset.vCopyUrl));
    copyText(partnerUrl(v.slug), 'URL');
  });
  B.querySelectorAll('[data-v-pw]').forEach(b => b.onclick = async () => {
    const v = VEN.list.find(x => x.id === Number(b.dataset.vPw));
    if (!await showConfirm({ title: `${v.name} のパスワードを再発行しますか？`, message: '今のパスワードは使えなくなり、ログイン中の人もログアウトされます。', okLabel: '再発行' })) return;
    try {
      const r = await api(`/api/sales/vendors/${v.id}/password`, { method: 'POST' });
      VEN.shown = { id: v.id, name: v.name, url: partnerUrl(v.slug), password: r.password };
      await after('新しいパスワードを発行しました');
    } catch (e) { showToast(e.message, { type: 'error' }); }
  });
  B.querySelectorAll('[data-v-url]').forEach(b => b.onclick = async () => {
    const v = VEN.list.find(x => x.id === Number(b.dataset.vUrl));
    if (!await showConfirm({ title: `${v.name} のURLを再発行しますか？`, message: '今のURLは使えなくなります。パスワードは変わりません。', okLabel: '再発行' })) return;
    try {
      const r = await api(`/api/sales/vendors/${v.id}/url`, { method: 'POST' });
      VEN.shown = { id: v.id, name: v.name, url: partnerUrl(r.slug), password: null };
      await after('新しいURLを発行しました');
    } catch (e) { showToast(e.message, { type: 'error' }); }
  });
  B.querySelectorAll('[data-v-active]').forEach(b => b.onclick = async () => {
    const on = b.dataset.on === '1';
    const v = VEN.list.find(x => x.id === Number(b.dataset.vActive));
    if (!on && !await showConfirm({ title: `${v.name} の入口を止めますか？`, message: 'ログインできなくなります。記録した案件はそのまま残ります。', okLabel: '停止', danger: true })) return;
    try { await api(`/api/sales/vendors/${v.id}`, { method: 'PUT', body: JSON.stringify({ active: on }) }); await after(on ? '再開しました' : '停止しました'); }
    catch (e) { showToast(e.message, { type: 'error' }); }
  });
  if ($('vCopyShare')) $('vCopyShare').onclick = () => copyText($('vShareText').value, '案内文');
  if ($('vHide')) $('vHide').onclick = () => { VEN.shown = null; rerender(); };
}

// ── 一覧の絞り込み ──
const VENDOR_FILTER_KEY = 'sales.vendorFilter';
function vendorMatch(d) {
  const f = S.vendorFilter;
  if (!f) return true;
  if (f === '@all') return !!d.vendor_id;
  if (f === '@none') return !d.vendor_id;
  return String(d.vendor_id || '') === f;
}
function vendorFilterHtml() {
  const names = {};
  (S.allDeals || []).forEach(d => { if (d.vendor_id) names[d.vendor_id] = d.vendor_name || `業者#${d.vendor_id}`; });
  const ids = Object.keys(names);
  if (!ids.length && !S.vendorFilter) return '';
  const opt = (v, label) => `<option value="${esc(v)}" ${S.vendorFilter === v ? 'selected' : ''}>${esc(label)}</option>`;
  return `<select class="owner-filter ${S.vendorFilter ? 'is-on' : ''}" id="vendorFilter" title="テレアポ業者で絞り込み">
    ${opt('', '経路: すべて')}${opt('@all', 'テレアポ全部')}${ids.map(id => opt(id, `テレアポ: ${names[id]}`)).join('')}${opt('@none', 'テレアポ以外')}
  </select>`;
}
function bindVendorFilter() {
  const el = $('vendorFilter');
  if (!el) return;
  el.onchange = () => {
    S.vendorFilter = el.value;
    storeSet(VENDOR_FILTER_KEY, S.vendorFilter);
    applyPipelineKeepSelection();
  };
}

function vendorBadge(d) {
  if (!d.vendor_id) return '';
  let out = `<span class="vendor-badge" title="テレアポ業者が記録">${ico('phone', 11)} ${esc(d.vendor_name || 'テレアポ')}</span>`;
  if (d.is_open && d.appo_at) out += `<span class="due-badge ${d.appo_at.slice(0, 10) < todayStr() ? 'is-late' : ''}" title="アポ日時">${ico('meet', 12)} アポ ${esc(fmtDate(d.appo_at))}${d.appo_at.length > 10 ? ' ' + esc(d.appo_at.slice(11, 16)) : ''}</span>`;
  return out;
}

// ── 詳細 ──
function vendorFieldsHtml(d) {
  const show = d.vendor_id || d.appo_at || d.telapo_at || ['テレアポ', 'アポ'].includes(d.stage);
  if (!show || pipeOf(d).key !== 'sales') return '';
  const dt = v => (v || '').length >= 16 ? v.slice(0, 16) : (v ? v.slice(0, 10) + 'T09:00' : '');
  return `
    ${d.vendor_id ? `<div class="k">テレアポ業者</div><div class="v"><span class="vendor-badge">${ico('phone', 11)} ${esc(d.vendor_name || '')}</span>
      <div class="kv-note">「業者にも見せる」記録と段階の動きは、業者の画面にも出ます（金額・名刺・社内だけの記録は出ません）</div></div>` : ''}
    ${vendorInfoHtml(d)}
    <div class="k">架電日時</div><div class="v"><input type="datetime-local" class="email-input compact" id="fTelapo" value="${esc(dt(d.telapo_at))}"></div>
    <div class="k">アポ日時</div><div class="v"><input type="datetime-local" class="email-input compact" id="fAppo" value="${esc(dt(d.appo_at))}">
      <div class="kv-note">カレンダーに出ます。過ぎたら部長が「結果を記録」と声をかけます</div></div>`;
}

// 業者が入れた会社の情報（住所・公式サイトなど）
const VINFO = [['department', '部署・役職'], ['zip', '郵便番号'], ['address', '住所'], ['industry', '業種'], ['web', '公式サイト'], ['fax', 'FAX'], ['mobile', '携帯']];
function vendorInfoHtml(d) {
  const vi = d.vendor_info || {};
  const rows = VINFO.filter(([k]) => vi[k]);
  if (!rows.length) return '';
  return `<div class="k">業者が入れた会社情報</div><div class="v"><dl class="vinfo">${rows.map(([k, l]) => `<dt>${l}</dt><dd>${k === 'web'
    ? `<a href="${esc(vi.web)}" target="_blank" rel="noopener">${esc(vi.web.replace(/^https?:\/\//, '').replace(/\/$/, ''))}</a>`
    : k === 'address' ? `${esc(vi.address)} <a class="vinfo-map" href="https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(vi.address)}" target="_blank" rel="noopener">地図</a>`
    : esc(vi[k])}</dd>`).join('')}</dl></div>`;
}

function bindVendorFields(root, d) {
  const q = sel => root.querySelector(sel);
  if (q('#fTelapo')) q('#fTelapo').onchange = e => patchDeal({ telapo_at: e.target.value || null }, true);
  if (q('#fAppo')) q('#fAppo').onchange = e => patchDeal({ appo_at: e.target.value || null }, true);
  root.querySelectorAll('[data-vis]').forEach(b => b.onclick = async () => {
    try {
      await api(`/api/sales/activities/${b.dataset.vis}`, { method: 'PATCH', body: JSON.stringify({ vendor_visible: b.dataset.on === '1' }) });
      await refreshCurrent();
    } catch (e) { showToast(e.message, { type: 'error' }); }
  });
  const kindBtns = root.querySelectorAll('#recordSection .act-kind');
  kindBtns.forEach(b => b.addEventListener('click', () => {
    const cb = q('#aVendorVis');
    if (cb) cb.checked = b.dataset.kind === 'meeting';   // 打合せだけ最初から見せる（電話・メモ・メールは社内だけ）
  }));
}

// 記録フォームの「業者にも見せる」
function recordVisHtml(d) {
  if (!d.vendor_id) return '';
  return `<label class="compose-check"><input type="checkbox" id="aVendorVis" ${S.actKind === 'meeting' ? 'checked' : ''}>
    テレアポ業者（${esc(d.vendor_name || '')}）にも見せる</label>`;
}

// タイムラインの各記録に付ける札
function actVendorHtml(a, d) {
  if (!d.vendor_id || a.kind === 'stage') return '';
  if (a.vendor_id) return `<span class="tl-badge vendor">${ico('phone', 11)} 業者が記録</span>`;
  return a.vendor_visible
    ? `<button class="tl-vis is-on" data-vis="${a.id}" data-on="0" title="押すと業者から見えなくなります">${ico('eye', 12)} 業者に見せている</button>`
    : `<button class="tl-vis" data-vis="${a.id}" data-on="1" title="押すと業者にも見えます">${ico('lock', 12)} 社内だけ</button>`;
}

// 部長の目：業者別
function vendorBossHtml(all) {
  const rows = all.vendors || [];
  if (!rows.length) return '';
  return `<section class="boss-card c-teal">
    <div class="section-label"><span>テレアポ業者別</span></div>
    <table class="boss-table mix-table"><thead><tr><th></th><th class="num">記録</th><th class="num">アポ</th><th class="num">アポ率</th><th class="num">受注</th><th class="num">受注額</th></tr></thead>
    <tbody>${rows.map(r => `<tr><td class="mix-name">${esc(r.name)}</td><td class="num">${r.total}件</td><td class="num">${r.appo}件</td>
      <td class="num">${r.total ? Math.round(r.appo * 100 / r.total) + '%' : '—'}</td><td class="num">${r.won}件</td><td class="num">${r.won_amount ? yen(r.won_amount) : ''}</td></tr>`).join('')}</tbody></table>
  </section>`;
}
