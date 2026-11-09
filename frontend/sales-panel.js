// ============================================================
// 会社ごとの案件の項目と画面部品
//   和宏電機（盤）: 分野・商流・現場名・エンドユーザー・納期（受電日）、受注後の進行、ワコーメタルへの依頼
//   ワコーメタル（板金）: お客様の業種・引合の経路・希望納期、和宏電機からの依頼の表示
// sales.js の S / api / esc などを使う
// ============================================================

function profile() {
  return (S.meta && S.meta.profile) || { key: 'sheet', deal_fields: false, items: {}, fields: [], labels: {} };
}
function isPanel() { return profile().key === 'panel'; }
function hasField(f) { return profile().deal_fields && (profile().fields || []).includes(f); }
function fieldLabel(f, fallback) { return (profile().labels || {})[f] || fallback; }

const ITEM_DEFAULT = { name: '品名', material: '材質', thickness: '板厚', qty: '数量', unit: '個',
  ph_name: '品名', ph_material: 'SS400', ph_thickness: '2.3', quote_spec: '材質・板厚' };
function itemLabels() { return { ...ITEM_DEFAULT, ...(profile().items || {}) }; }

function selectHtml(id, list, value, empty = '— 未設定 —') {
  return `<select class="email-input compact" id="${id}"><option value="">${esc(empty)}</option>${(list || []).map(x =>
    `<option ${x === value ? 'selected' : ''}>${esc(x)}</option>`).join('')}</select>`;
}

// メールのドメインから公式サイトの候補（フリーメール・携帯・プロバイダは除く）
const FREE_MAIL = /(^|\.)(gmail\.com|googlemail\.com|yahoo\.co\.jp|yahoo\.com|ymail\.ne\.jp|icloud\.com|me\.com|mac\.com|outlook\.(com|jp)|hotmail\.(com|co\.jp)|live\.(jp|com)|msn\.com|docomo\.ne\.jp|ezweb\.ne\.jp|au\.com|softbank\.ne\.jp|i\.softbank\.jp|ymobile\.ne\.jp|rakuten\.jp|nifty\.(com|ne\.jp)|ocn\.ne\.jp|so-net\.ne\.jp|biglobe\.ne\.jp|plala\.or\.jp|eonet\.ne\.jp|zaq\.ne\.jp|kcn\.ne\.jp|dion\.ne\.jp|odn\.ne\.jp|infoweb\.ne\.jp|aol\.com|wakwak\.com|hi-ho\.ne\.jp|home\.ne\.jp|people\.or\.jp|sannet\.ne\.jp)$/i;
function siteFromEmail(email) {
  const m = String(email || '').trim().match(/@([a-z0-9.-]+\.[a-z]{2,})$/i);
  if (!m || FREE_MAIL.test(m[1])) return '';
  return 'https://' + m[1].toLowerCase().replace(/^(mail|mx|smtp)\./, '');
}

function daysLeft(ymd) {
  return Math.round((new Date(ymd + 'T00:00:00') - new Date(todayStr() + 'T00:00:00')) / 864e5);
}

// 一覧・ボードに出す小さな札（分野・現場名・納期）
function panelBadges(d) {
  if (!profile().deal_fields) return '';
  const out = [];
  if (d.segment) out.push(`<span class="seg-badge">${esc(d.segment)}</span>`);
  if (d.site_name && hasField('site_name')) out.push(`<span class="site-badge" title="現場名">${ico('site', 12)} ${esc(d.site_name)}</span>`);
  if (d.is_open && d.delivery_on) {
    const left = daysLeft(d.delivery_on);
    out.push(`<span class="due-badge ${left < 0 ? 'is-late' : left <= 21 ? 'is-today' : ''}" title="${esc(fieldLabel('delivery_on', '納期'))}">${ico('box', 12)} 納期 ${esc(fmtDate(d.delivery_on))}</span>`);
  }
  if ((d.same_site || []).length) out.push(`<span class="dup-site-badge" title="同じ現場の引合がほかにもあります">同じ現場 ${d.same_site.length}件</span>`);
  if (d.group_deal_id) out.push(`<span class="group-badge" title="グループ会社とつながっています">${isPanel() ? '板金依頼済' : '和宏電機から'}</span>`);
  return out.join('');
}

// 詳細の「案件情報」に足す欄
function panelFieldsHtml(d) {
  if (!profile().deal_fields) return '';
  const pf = profile();
  const pl = pipeOf(d);
  const same = (d.same_site || []).map(id => `<button class="btn-link" data-open-deal="${id}">#${id}</button>`).join(' ');
  const rows = [];
  if (pl.key === 'sales' && hasField('segment'))
    rows.push(`<div class="k">${esc(fieldLabel('segment', '分野'))}</div><div class="v">${selectHtml('fSegment', pf.segments, d.segment || '')}</div>`);
  if (pl.key === 'sales' && hasField('channel'))
    rows.push(`<div class="k">${esc(fieldLabel('channel', '商流'))}</div><div class="v">${selectHtml('fChannel', pf.channels, d.channel || '')}
      ${isPanel() ? '<div class="kv-note">誰から誰へ発注が流れるか（エンドユーザー → ゼネコン/サブコン → メーカー/商社 → 当社）</div>' : ''}</div>`);
  if (hasField('site_name'))
    rows.push(`<div class="k">現場名</div><div class="v"><input class="email-input compact" id="fSite" value="${esc(d.site_name || '')}" maxlength="120" placeholder="例: 梅田DC 新築工事">
      ${same ? `<div class="kv-note warn">同じ現場の引合: ${same}</div>` : ''}</div>`);
  if (hasField('end_user'))
    rows.push(`<div class="k">エンドユーザー</div><div class="v"><input class="email-input compact" id="fEndUser" value="${esc(d.end_user || '')}" maxlength="120" placeholder="最終的に設備を使う会社・施設"></div>`);
  if (hasField('delivery_on'))
    rows.push(`<div class="k">${esc(fieldLabel('delivery_on', '納期'))}</div><div class="v"><input type="date" class="email-input compact" id="fDelivery" value="${esc(d.delivery_on || '')}">
      <div class="kv-note">カレンダーに出ます。${pl.key === 'prod' ? '近づくと承認図・製作の遅れを部長が知らせます' : isPanel() ? '受注後の進行に引き継がれます' : ''}</div></div>`);
  return rows.join('') + linkedHtml(d) + groupHtml(d);
}

function linkedHtml(d) {
  return (d.linked_deals || []).map(x => {
    const rel = x.id === d.source_deal_id ? '元の受注案件' : '受注後の進行';
    return `<div class="k">${rel}</div><div class="v"><button class="btn-link" data-open-deal="${x.id}">#${x.id} ${esc(x.title)}</button>
      <span class="stage-badge" data-stage="${esc(x.stage)}">${esc(x.stage)}</span></div>`;
  }).join('');
}

// グループ会社とのつながり（和宏電機 ⇄ ワコーメタル）
function groupHtml(d) {
  const g = d.group_link;
  if (g) {
    return `<div class="k">${isPanel() ? 'ワコーメタルへの依頼' : '和宏電機からの依頼'}</div><div class="v">
      <span class="group-link">${esc(g.tenant)} #${g.id} ${esc(g.title)} <span class="stage-badge" data-stage="${esc(g.stage)}">${esc(g.stage)}</span></span>
      <div class="kv-note">${g.next_action_due ? `先方の次の期限 ${esc(fmtDate(g.next_action_due))}・` : ''}段階は先方の画面で進みます</div></div>`;
  }
  if (isPanel() && profile().group_target && ['sales', 'prod'].includes(pipeOf(d).key) && d.is_open !== false) {
    return `<div class="k">筐体の板金</div><div class="v"><button class="btn-sm" id="groupReq">ワコーメタルに依頼する</button>
      <div class="kv-note">ワコーメタルの営業画面に案件ができます（現場名・納期・盤の明細を引き継ぎ）</div></div>`;
  }
  return '';
}

function bindPanelFields(root, d) {
  const q = sel => root.querySelector(sel);
  if (q('#fSegment')) q('#fSegment').onchange = e => patchDeal({ segment: e.target.value || null });
  if (q('#fChannel')) q('#fChannel').onchange = e => patchDeal({ channel: e.target.value || null }, true);
  if (q('#fSite')) q('#fSite').onchange = e => patchDeal({ site_name: e.target.value.trim() || null }, true);
  if (q('#fEndUser')) q('#fEndUser').onchange = e => patchDeal({ end_user: e.target.value.trim() || null }, true);
  if (q('#fDelivery')) q('#fDelivery').onchange = e => patchDeal({ delivery_on: e.target.value || null }, true);
  if (q('#makeProd')) q('#makeProd').onclick = async () => {
    q('#makeProd').disabled = true;
    try {
      const res = await api(`/api/sales/deals/${d.id}/to-prod`, { method: 'POST' });
      await loadDeals();
      showDeal(res.id);
      showToast(`受注後の進行 #${res.id} を作りました。承認図の提出日を決めましょう`, { type: 'success', duration: 4000 });
    } catch (e) { showToast(e.message, { type: 'error' }); q('#makeProd').disabled = false; }
  };
  if (q('#groupReq')) q('#groupReq').onclick = () => {
    const cell = q('#groupReq').parentElement;
    cell.innerHTML = `<textarea class="email-input compact" id="groupNote" rows="3" maxlength="2000"
        placeholder="依頼内容（任意）例: 屋外自立形 2面・SUS・塗装色 5Y7/1・図面は来週送付"></textarea>
      <div class="group-actions"><button class="btn-sm" id="groupCancel">やめる</button>
        <button class="btn-sm btn-sm-primary" id="groupSend">ワコーメタルに送る</button></div>`;
    cell.querySelector('#groupNote').focus();
    cell.querySelector('#groupCancel').onclick = () => renderDetail();
    cell.querySelector('#groupSend').onclick = async e => {
      e.target.disabled = true;
      try {
        const res = await api(`/api/sales/deals/${d.id}/group-request`, { method: 'POST',
          body: JSON.stringify({ note: cell.querySelector('#groupNote').value }) });
        await refreshCurrent();
        showToast(`ワコーメタルに依頼しました（先方の案件 #${res.id}）`, { type: 'success', duration: 4000 });
      } catch (err) { showToast(err.message, { type: 'error' }); e.target.disabled = false; }
    };
  };
}

// 受注した盤の案件 → 受注後の進行
function prodRowHtml(d) {
  if (!S.meta.pipelines.some(p => p.key === 'prod')) return '';
  const pl = pipeOf(d);
  if (pl.key !== 'sales' || d.stage !== pl.won) return '';
  if (d.prod_deal_id) return `<div class="closed-row"><span class="k">受注後の進行</span>
    <button class="btn-link" data-open-deal="${d.prod_deal_id}">#${d.prod_deal_id} を開く</button></div>`;
  return `<div class="closed-row"><span class="k">受注後の進行</span>
    <button class="btn-sm btn-sm-primary" id="makeProd">受注後の進行を作る</button>
    <span class="card-result-sub">承認図・機器の手配・立会検査・出荷を追いかけます（現場・納期・明細を引き継ぎ）</span></div>`;
}

// 新規案件の画面
function newDealPanelInit() {
  const box = $('newPanelFields');
  if (!box) return;
  const show = profile().deal_fields && S.pipeline === 'sales';
  box.style.display = show ? '' : 'none';
  if (!show) return;
  const pf = profile();
  $('newSegmentBox').innerHTML = selectHtml('newSegment', pf.segments, '');
  $('newChannelBox').innerHTML = selectHtml('newChannel', pf.channels, '');
  $('newSegmentLabel').textContent = fieldLabel('segment', '分野');
  $('newChannelLabel').textContent = fieldLabel('channel', '商流');
  $('newDeliveryLabel').textContent = fieldLabel('delivery_on', '納期') + '（任意）';
  $('newSiteRow').style.display = hasField('site_name') ? '' : 'none';
  ['newSite', 'newEndUser', 'newDelivery'].forEach(id => { $(id).value = ''; });
  if (isPanel()) $('newTitle').placeholder = '例: 梅田DC 高圧受電盤 2面（空欄なら接点名から自動）';
}

function newDealPanelPayload() {
  if (!profile().deal_fields || S.pipeline !== 'sales' || !$('newSegment')) return {};
  const out = { segment: $('newSegment').value || null, channel: $('newChannel').value || null,
    delivery_on: $('newDelivery').value || null };
  if (hasField('site_name')) { out.site_name = $('newSite').value.trim() || null; out.end_user = $('newEndUser').value.trim() || null; }
  return out;
}

// 部長の目：分野別・商流別
function mixTableHtml(title, rows) {
  if (!rows || !rows.length) return '';
  const mx = Math.max(1, ...rows.map(r => r.open_amount + r.won_amount_365));
  const cell = (n, amt) => `${n}件${amt ? `<div class="mix-amt">${yen(amt)}</div>` : ''}`;
  return `<section class="boss-card">
    <div class="section-label"><span>${esc(title)}</span></div>
    <table class="boss-table mix-table"><thead><tr><th></th><th class="num">進行中</th><th class="num">受注（1年）</th><th class="num">失注（1年）</th><th></th></tr></thead>
    <tbody>${rows.map(r => `<tr>
      <td class="mix-name">${esc(r.name)}</td><td class="num">${cell(r.open, r.open_amount)}</td>
      <td class="num">${cell(r.won_365, r.won_amount_365)}</td><td class="num">${r.lost_365}件</td>
      <td class="mix-bar"><div class="mini-bar wide"><div style="width:${(r.open_amount + r.won_amount_365) * 100 / mx}%"></div></div></td></tr>`).join('')}</tbody></table>
  </section>`;
}

function panelBossHtml(all) {
  if (!all.segments && !all.channels) return '';
  const segT = isPanel() ? '分野別（DC・再エネは成長分野）' : `${fieldLabel('segment', '業種')}別`;
  const chT = `${fieldLabel('channel', '商流')}別`;
  return `<div class="boss-grid">${mixTableHtml(segT, all.segments)}${mixTableHtml(chT, all.channels)}</div>`;
}
