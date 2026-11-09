// ============================================================
// テレアポ業者とのやりとり（こちら側の画面）
//   案件の「業者に頼む」: 再テレアポ（用件を選ぶ）／資料を送った（到着確認を頼む）
//   案件の概要: 依頼ごとのやりとり・送付しました・コメント・確認しました
//   一覧: 業者から新着がある案件は一番上に。「確認しました」で通常の並びに戻る
// sales.js の S / api / esc / $ / showToast などを使う
// ============================================================

const VQ = { byDeal: {}, meta: null, comment: {} };
const VQ_STAFF_ACT = { create: '依頼しました', sent: '資料を送付しました', comment: 'コメント', cancel: '取り消しました', reopen: 'もう一度依頼しました' };
const VQ_VENDOR_ACT = { create: '資料送付を頼まれました', report: '電話しました', arrival: '資料の到着確認の電話', comment: 'コメント' };
const VQ_PURPOSE_HINT = {
  '再訪問の連絡': '例: 前回は図面待ちで終わっています。来月あたりで再訪問の日程を取りたいです',
  'お仕事ないですかの連絡': '例: 半年ほど連絡が空いています。大物の板金で困っている案件がないか聞いてください',
  '現状の確認': '例: 見積を出したまま止まっています。検討の状況を確かめてください',
  'その他': '用件を書いてください（業者の画面にそのまま出ます）',
};

async function vqMeta() {
  if (!VQ.meta) VQ.meta = await api('/api/sales/vendor-requests/meta');
  return VQ.meta;
}

async function vqLoad(dealId) {
  try { VQ.byDeal[dealId] = await api(`/api/sales/deals/${dealId}/vendor-requests`); }
  catch { VQ.byDeal[dealId] = VQ.byDeal[dealId] || { requests: [], vendors: [], new: false }; }
  return VQ.byDeal[dealId];
}

// ── 一覧 ──
function vqIsNew(d) { return !!(d.vreq && d.vreq.new); }
function vqPin(list) {
  // 業者から新着がある案件を一番上に（その中の並びはそのまま）
  return [...list.filter(vqIsNew), ...list.filter(d => !vqIsNew(d))];
}
function vqBadge(d) {
  const v = d.vreq;
  if (!v) return '';
  let out = v.new ? `<span class="vq-new">${ico('phone', 11)} 業者から新着</span>` : '';
  out += (v.labels || []).map(l => `<span class="vq-chip ${l === '資料送付待ち' ? 'is-todo' : ''}">${ico(l === '資料送付待ち' || l === '到着確認待ち' ? 'doc' : 'phone', 11)} ${esc(l)}</span>`).join('');
  return out;
}
function vqBanner() {
  const n = (S.deals || []).filter(vqIsNew).length;
  return n ? `<div class="vq-banner">${ico('phone')} テレアポ業者から新着が <b>${n}</b> 件あります（一覧の一番上）。案件を開いて「確認しました」を押すと通常の並びに戻ります</div>` : '';
}

// ── 案件の画面 ──
function vqHeadHtml(d) {
  const v = VQ.byDeal[d.id];
  if (!v || !v.new) return '';
  const r = v.requests.find(x => x.new);
  const e = r ? [...r.events].reverse().find(x => x.new) : null;
  const what = e ? `${VQ_VENDOR_ACT[e.action] || ''}${e.result ? `：${e.result}` : ''}` : '';
  return `<div class="vq-strip"><span>${ico('phone')} <b>${esc(r ? r.vendor_name || '業者' : '業者')}</b> から新着${what ? `（${esc(what)}）` : ''}</span>
    <span class="vq-strip-btns"><button class="btn-sm" data-goto-vq="1">見る</button><button class="btn-sm btn-sm-primary" data-vq-seen="1">確認しました</button></span></div>`;
}

function vqSectionHtml(d) {
  if ((d.pipeline || 'sales') !== 'sales') return '';
  const v = VQ.byDeal[d.id];
  const reqs = v ? v.requests : [];
  const live = reqs.filter(r => r.status !== 'cancelled');
  const gone = reqs.filter(r => r.status === 'cancelled');
  const head = `<div class="section-label"><span>テレアポ業者とのやりとり${v && v.new ? ' <span class="vq-new">新着</span>' : ''}</span>
    <span>${v && v.new ? '<button class="section-action vq-seen-btn" data-vq-seen="1">確認しました</button>' : ''}
      <button class="section-action" data-vq-open="1">＋ 業者に頼む</button></span></div>`;
  if (!v) return '';
  if (!reqs.length) {
    // まだ頼んだことがない案件は1行だけ（画面をすっきりさせる）
    return `<div class="vq-mini" id="vqSec"><span>${ico('phone')} テレアポ業者に再テレアポや資料の到着確認を頼めます</span><button class="btn-link" data-vq-open="1">業者に頼む →</button></div>`;
  }
  const body = live.length ? live.map(vqCardHtml).join('')
    : `<div class="vq-empty">業者に「再テレアポ（再訪問の連絡・お仕事ないですか・現状の確認）」や「資料の到着確認」を頼めます。
       <button class="btn-sm btn-sm-primary" data-vq-open="1">${ico('phone')} 再テレアポを頼む</button></div>`;
  return `<div class="detail-section vq-sec ${v.new ? 'is-new' : ''}" id="vqSec">${head}${body}
    ${gone.length ? `<details class="vq-gone"><summary>取り消した依頼（${gone.length}）</summary>${gone.map(vqCardHtml).join('')}</details>` : ''}</div>`;
}

function vqCardHtml(r) {
  const s = r.share || {}, dc = r.docs || {};
  const title = r.kind === 'recall' ? `${ico('phone')} 再テレアポ・${esc(r.purpose || '')}` : `${ico('doc')} 資料送付`;
  const info = [
    [s.company, [s.department, s.contact].filter(Boolean).join(' '), s.phone].filter(Boolean).map(esc).join('　'),
    r.kind === 'recall' && r.due_on ? `いつまでに ${esc(fmtDate(r.due_on))}` : '',
    r.kind === 'docs' ? [dc.what ? `資料: ${esc(dc.what)}` : '', dc.to ? `宛先: ${esc(dc.to)}` : '', dc.method ? `送り方: ${esc(dc.method)}` : '',
      dc.address ? `送り先: ${esc(dc.address)}` : '', r.status === 'sent' && r.follow_on ? `到着確認の電話 ${esc(fmtDate(r.follow_on))}ごろ` : ''].filter(Boolean).join('　') : '',
  ].filter(Boolean).map(x => `<div class="vq-info">${x}</div>`).join('');
  const evs = r.events.map(e => {
    const ours = e.actor === 'staff';
    const label = ours ? VQ_STAFF_ACT[e.action] : VQ_VENDOR_ACT[e.action];
    const res = e.result ? `${e.action === 'sent' ? '（' + esc(e.result) + '）' : `：<b>${esc(e.result)}</b>`}` : '';
    return `<div class="vq-ev ${ours ? 'is-ours' : 'is-theirs'} ${e.new ? 'is-new' : ''}">
      <div class="vq-ev-h"><span class="vq-who">${ours ? '自社' : esc(r.vendor_name || '業者')}</span><span>${esc(fmtDateTime(e.at || e.created_at))}</span>${e.new ? '<span class="vq-new">新着</span>' : ''}</div>
      <div class="vq-ev-t">${esc(label || e.action)}${res}</div>${e.body ? `<div class="vq-ev-b">${esc(e.body)}</div>` : ''}</div>`;
  }).join('');
  const btns = [];
  if (r.kind === 'docs' && r.status === 'requested') btns.push(`<button class="btn-sm btn-sm-primary" data-vq-sent="${r.id}">送付しました</button>`);
  if (r.kind === 'recall' && r.status === 'done') btns.push(`<button class="btn-sm" data-vq-reopen="${r.id}">もう一度頼む</button>`);
  if (r.status !== 'cancelled') btns.push(`<button class="btn-sm" data-vq-comment="${r.id}">コメント</button>`);
  if (!['done', 'cancelled'].includes(r.status)) btns.push(`<button class="btn-link vq-cancel" data-vq-cancel="${r.id}">取り消す</button>`);
  const box = VQ.comment[r.id];
  return `<div class="vq-card ${r.new ? 'is-new' : ''} st-${esc(r.status)}">
    <div class="vq-card-h"><span class="vq-title">${title}</span><span class="vendor-badge">${ico('phone', 11)} ${esc(r.vendor_name || '')}</span>
      <span class="vq-st st-${esc(r.status)} ${r.todo ? 'is-todo' : ''}">${esc(r.todo ? `${r.status_label}（こちらで${r.todo}）` : r.status_label)}</span></div>
    ${info}
    <div class="vq-evs">${evs}</div>
    ${box ? `<div class="vq-form">
      ${box.mode === 'sent' ? `<label class="lm-label">送り方</label><select class="email-input compact" id="vqMethod${r.id}">${(VQ.meta ? VQ.meta.methods : ['メール', '郵送', 'FAX']).filter(m => m !== 'どれでも')
        .map(m => `<option ${m === dc.method ? 'selected' : ''}>${esc(m)}</option>`).join('')}</select>` : ''}
      <textarea class="email-input compact" id="vqText${r.id}" rows="2" maxlength="2000" placeholder="${box.mode === 'sent' ? '例: 11/2 会社案内と設備一覧をメールで送付（任意）' : box.mode === 'reopen' ? '業者へのひとこと（任意）' : '業者へのコメント'}"></textarea>
      <div class="vq-form-btns"><button class="btn-sm" data-vq-x="${r.id}">やめる</button><button class="btn-sm btn-sm-primary" data-vq-go="${r.id}">${box.mode === 'sent' ? '送付済みにする' : box.mode === 'reopen' ? 'もう一度頼む' : '送る'}</button></div>
      ${box.mode === 'sent' ? '<div class="lm-note">業者の画面に「送付しました」が届き、1週間後ぐらいに到着確認の電話をしてもらいます。</div>' : ''}</div>`
      : btns.length ? `<div class="vq-btns">${btns.join('')}</div>` : ''}
  </div>`;
}

function bindVq(root, d) {
  const reload = async (msg) => {
    await vqLoad(d.id);
    try { await refreshCurrent(false); } catch {}
    renderDetail();
    if (msg) showToast(msg, { type: 'success', duration: 2000 });
  };
  root.querySelectorAll('[data-vq-open]').forEach(b => b.onclick = () => vqOpenDialog(d));
  root.querySelectorAll('[data-goto-vq]').forEach(b => b.onclick = () => {
    showDTab('overview');
    const sec = root.querySelector('#vqSec'); if (sec) sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
  root.querySelectorAll('[data-vq-seen]').forEach(b => b.onclick = async () => {
    try {
      await api(`/api/sales/deals/${d.id}/vendor-requests/seen`, { method: 'POST' });
      const x = S.allDeals.find(z => z.id === d.id); if (x && x.vreq) x.vreq.new = false;
      await reload('確認しました');
      applyPipelineKeepSelection();
    } catch (e) { showToast(e.message, { type: 'error' }); }
  });
  const open = (id, mode) => { VQ.comment = { [id]: { mode } }; renderDetail(); const t = detailContainer().querySelector(`#vqText${id}`); if (t) t.focus(); };
  root.querySelectorAll('[data-vq-comment]').forEach(b => b.onclick = () => open(b.dataset.vqComment, 'comment'));
  root.querySelectorAll('[data-vq-sent]').forEach(b => b.onclick = async () => { await vqMeta().catch(() => {}); open(b.dataset.vqSent, 'sent'); });
  root.querySelectorAll('[data-vq-reopen]').forEach(b => b.onclick = () => open(b.dataset.vqReopen, 'reopen'));
  root.querySelectorAll('[data-vq-x]').forEach(b => b.onclick = () => { VQ.comment = {}; renderDetail(); });
  root.querySelectorAll('[data-vq-go]').forEach(b => b.onclick = async () => {
    const id = b.dataset.vqGo, mode = (VQ.comment[id] || {}).mode;
    const text = root.querySelector(`#vqText${id}`).value.trim();
    if (mode === 'comment' && !text) { showToast('コメントを入れてください', { type: 'warning' }); return; }
    const body = { action: mode, body: text || null };
    if (mode === 'sent') body.method = root.querySelector(`#vqMethod${id}`).value;
    b.disabled = true;
    try {
      await api(`/api/sales/vendor-requests/${id}/events`, { method: 'POST', body: JSON.stringify(body) });
      VQ.comment = {};
      await reload(mode === 'sent' ? '送付済みにしました。業者に届きます' : mode === 'reopen' ? 'もう一度頼みました' : 'コメントを送りました');
    } catch (e) { showToast(e.message, { type: 'error' }); b.disabled = false; }
  });
  root.querySelectorAll('[data-vq-cancel]').forEach(b => b.onclick = async () => {
    if (!await showConfirm({ title: 'この依頼を取り消しますか？', message: '業者の画面から消えます（記録は残ります）。', okLabel: '取り消す', danger: true })) return;
    try { await api(`/api/sales/vendor-requests/${b.dataset.vqCancel}/events`, { method: 'POST', body: JSON.stringify({ action: 'cancel' }) }); await reload('取り消しました'); }
    catch (e) { showToast(e.message, { type: 'error' }); }
  });
}

// ── 依頼のダイアログ ──
async function vqOpenDialog(d, kind = 'recall') {
  let meta, v;
  try { [meta, v] = await Promise.all([vqMeta(), VQ.byDeal[d.id] ? Promise.resolve(VQ.byDeal[d.id]) : vqLoad(d.id)]); }
  catch (e) { showToast(e.message, { type: 'error' }); return; }
  const card = d.card || {};
  const share = { company: card.company || d.company_snapshot || '', contact: card.name || d.contact_snapshot || '',
    department: card.department_position || '', phone: card.phone || card.mobile || d.phone_snapshot || '' };
  const st = { kind, purpose: '' };
  $('listsModalTitle').textContent = 'テレアポ業者に頼む';
  const draw = () => {
    const vendors = v.vendors || [];
    $('listsModalBody').innerHTML = !vendors.length
      ? '<div class="lm-note">テレアポ業者がまだ登録されていません。右上の「設定」→「テレアポ業者」から追加してください。</div>'
      : `<div class="lm-form vq-dlg">
      <div class="vq-kinds">
        <button class="vq-kind ${st.kind === 'recall' ? 'is-on' : ''}" data-k="recall"><b>${ico('phone')} 再テレアポを頼む</b><span>用件を伝えて電話してもらう</span></button>
        <button class="vq-kind ${st.kind === 'docs' ? 'is-on' : ''}" data-k="docs"><b>${ico('doc')} 資料を送った</b><span>1週間後ぐらいに到着確認の電話を頼む</span></button>
      </div>
      ${st.kind === 'recall' ? `<label class="lm-label">用件</label>
      <div class="vq-purposes">${meta.purposes.map(p => `<button class="vq-purpose ${st.purpose === p ? 'is-on' : ''}" data-p="${esc(p)}">${esc(p)}</button>`).join('')}</div>` : ''}
      <label class="lm-label">業者</label>
      <select class="email-input compact" id="vqVendor">${vendors.map(x => `<option value="${x.id}">${esc(x.name)}</option>`).join('')}</select>
      ${st.kind === 'docs' ? `<div class="vq-grid2">
        <div><label class="lm-label">送り方</label><select class="email-input compact" id="vqDocMethod">${meta.methods.filter(m => m !== 'どれでも').map(m => `<option>${esc(m)}</option>`).join('')}</select></div>
        <div><label class="lm-label">送った資料</label><input class="email-input compact" id="vqDocWhat" maxlength="500" placeholder="例: 会社案内・設備一覧"></div></div>` : ''}
      <label class="lm-label">業者に渡す連絡先 <span class="vq-sub">（業者の画面に出ます）</span></label>
      <div class="vq-grid2">
        <input class="email-input compact" id="vqCo" maxlength="120" placeholder="会社名" value="${esc(share.company)}">
        <input class="email-input compact" id="vqPhone" maxlength="40" placeholder="電話番号（必須）" value="${esc(share.phone)}" inputmode="tel">
        <input class="email-input compact" id="vqDept" maxlength="80" placeholder="部署・役職" value="${esc(share.department)}">
        <input class="email-input compact" id="vqContact" maxlength="60" placeholder="担当者" value="${esc(share.contact)}">
      </div>
      ${st.kind === 'recall' ? `<label class="lm-label">いつまでに（任意）</label><input type="date" class="email-input compact vq-date" id="vqDue">` : ''}
      <label class="lm-label">業者へのひとこと${st.purpose === 'その他' ? '（必須）' : '（任意）'}</label>
      <textarea class="email-input compact" id="vqNote" rows="3" maxlength="2000" placeholder="${esc(st.kind === 'docs' ? '例: 11/2 にメールで送りました。届いたか、見てもらえたか確かめてください' : VQ_PURPOSE_HINT[st.purpose] || '用件の補足があれば')}"></textarea>
      <div class="lm-note">業者には、ここに入れた連絡先とひとことだけが見えます（金額・名刺・社内の記録は見えません）。結果は業者が記録すると、この案件に「新着」で届きます。</div>
    </div>`;
    $('listsModalFoot').innerHTML = `<button class="btn-sm" data-close="listsOverlay">やめる</button>${vendors.length ? '<button class="btn-sm btn-sm-primary" id="vqGo">業者に頼む</button>' : ''}`;
    bindListsModal();
    const keep = () => { share.company = $('vqCo').value; share.phone = $('vqPhone').value; share.department = $('vqDept').value; share.contact = $('vqContact').value; st.note = $('vqNote').value; st.vendor = $('vqVendor').value; };
    $('listsModalBody').querySelectorAll('[data-k]').forEach(b => b.onclick = () => { keep(); st.kind = b.dataset.k; draw(); restore(); });
    $('listsModalBody').querySelectorAll('[data-p]').forEach(b => b.onclick = () => { keep(); st.purpose = b.dataset.p; draw(); restore(); });
    const restore = () => { if (st.note != null && $('vqNote')) $('vqNote').value = st.note; if (st.vendor && $('vqVendor')) $('vqVendor').value = st.vendor; };
    if (!$('vqGo')) return;
    $('vqGo').onclick = async () => {
      if (st.kind === 'recall' && !st.purpose) { showToast('用件を選んでください', { type: 'warning' }); return; }
      if (!$('vqPhone').value.trim()) { showToast('電話番号を入れてください（業者がかける番号です）', { type: 'warning' }); $('vqPhone').focus(); return; }
      const body = { vendor_id: Number($('vqVendor').value), kind: st.kind, purpose: st.purpose || null, note: $('vqNote').value,
        share: { company: $('vqCo').value, phone: $('vqPhone').value, department: $('vqDept').value, contact: $('vqContact').value } };
      if (st.kind === 'recall') body.due_on = $('vqDue').value || null;
      else body.docs = { method: $('vqDocMethod').value, what: $('vqDocWhat').value };
      $('vqGo').disabled = true;
      try {
        await api(`/api/sales/deals/${d.id}/vendor-requests`, { method: 'POST', body: JSON.stringify(body) });
        $('listsOverlay').style.display = 'none';
        await vqLoad(d.id);
        try { await refreshCurrent(false); } catch {}
        S.dtab = 'overview'; renderDetail();
        showToast(st.kind === 'recall' ? '業者に再テレアポを頼みました' : '業者に到着確認を頼みました', { type: 'success', duration: 2500 });
      } catch (e) { showToast(e.message, { type: 'error' }); $('vqGo').disabled = false; }
    };
  };
  draw();
  $('listsOverlay').style.display = 'flex';
}
