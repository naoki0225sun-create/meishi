// ============================================================
// メモから記録・案件を更新（AI）
//   案件の上の「メモ」に書くだけ（図面・写真・資料も添付できる）。
//   AIが記録を整理し、今の状況（段階・失注理由）を割り振り、次のアクションと期限を提案する。
//   「これでいく」か「直す」を決めて反映。sales.js の S / api / esc などを使う
// ============================================================

const DIGEST_ACCEPT = '.txt,.md,.markdown,.docx,.pdf,.srt,.vtt,.json,.csv,.html,.htm,.rtf,.eml,.log,.xlsx,.pptx,.jpg,.jpeg,.png,.webp';
const DG = { byDeal: {}, files: {} };   // deal_id → { id, status, result, error, filename } / 添付（送る前）
const DG_MAX_FILES = 6;
const DG_SR = window.SpeechRecognition || window.webkitSpeechRecognition || null;   // 音声入力（Chrome・Safari）

function dgMicStop() { if (DG.rec) { try { DG.rec.abort(); } catch { /* 止まっている */ } } DG.rec = null; DG.mic = false; }
// 止めて、最後の言葉が入るのを待つ（「AIにまかせる」の前）
function dgMicFinish() {
  const rec = DG.rec;
  if (!rec) return Promise.resolve();
  return new Promise(res => {
    const t = setTimeout(res, 1500);
    rec.addEventListener('end', () => { clearTimeout(t); res(); }, { once: true });
    try { rec.stop(); } catch { clearTimeout(t); res(); }
  });
}
function dgMicStart(d, text, btn) {
  const rec = new DG_SR();
  rec.lang = 'ja-JP'; rec.continuous = true; rec.interimResults = true;
  const base = text.value ? text.value.replace(/\s+$/, '') + '\n' : '';
  let fixed = '';
  rec.onresult = e => {
    if (DG.rec !== rec) return;   // 止めた後に届いた結果は使わない
    let interim = '';
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const r = e.results[i];
      if (r.isFinal) fixed += r[0].transcript; else interim += r[0].transcript;
    }
    text.value = base + fixed + interim;
    DG.byDeal[d.id] = { status: 'idle', draft: base + fixed };
  };
  rec.onerror = e => {
    if (e.error === 'not-allowed' || e.error === 'service-not-allowed') showToast('マイクが使えません。ブラウザの設定でマイクを許可してください', { type: 'warning', duration: 4000 });
    else if (e.error !== 'no-speech' && e.error !== 'aborted') showToast('音声入力が止まりました', { type: 'warning' });
  };
  rec.onend = () => {
    if (DG.rec !== rec) return;
    DG.rec = null; DG.mic = false;
    DG.byDeal[d.id] = { status: 'idle', draft: text.value };
    if (btn.isConnected) { btn.classList.remove('is-on'); btn.innerHTML = `${ico('mic')} 話して入力`; }
  };
  try { rec.start(); } catch { showToast('音声入力を始められませんでした', { type: 'warning' }); return; }
  DG.rec = rec; DG.mic = true;
  btn.classList.add('is-on'); btn.innerHTML = `${ico('stop')} 止める`;
  text.classList.add('is-open');
}

// 「ほかにも見つけたもの」に出す項目（段階・失注・次のアクションは上で別に扱う）
const DG_OTHER = [
  ['title', '案件名', 'text'], ['amount', '金額（税抜）', 'number'], ['expected_close', '受注予定日', 'date'],
  ['segment', null, 'segment'], ['channel', null, 'channel'],
  ['site_name', '現場名', 'text'], ['end_user', 'エンドユーザー', 'text'], ['delivery_on', null, 'date'],
];

function dgLabel(key, label) {
  if (label) return label;
  return fieldLabel(key, { segment: '分野', channel: '商流', delivery_on: '納期' }[key]);
}

function dgShow(key, v) {
  if (v == null || v === '') return '<span class="dg-empty">なし</span>';
  if (key === 'amount') return esc(yen(v));
  if (/_due$|_close$|_on$/.test(key)) return esc(fmtDate(v));
  return esc(v);
}

const dgAddDays = n => { const t = new Date(); t.setDate(t.getDate() + n); return ymdLocal(t); };
function ymdLocal(t) { return `${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, '0')}-${String(t.getDate()).padStart(2, '0')}`; }

// ── メモの入力欄（案件の上に常に出す） ──
function digestHtml(d) {
  const st = DG.byDeal[d.id];
  if (!st || st.status === 'idle') {
    const files = DG.files[d.id] || [];
    return `<div class="mm" id="dgBox">
      <textarea class="mm-text" id="dgText" rows="2" maxlength="20000" placeholder="何があったか、メモするだけでOK（段階・次のアクション・アポ・業者への依頼までAIが判断して、確認画面を出します）&#10;例: 山田部長と打合せ。盤2面の更新で図面をもらった。来月中に見積がほしいとのこと">${esc((st && st.draft) || '')}</textarea>
      ${files.length ? `<div class="mm-files">${files.map((f, i) => `<span class="mm-file">${ico('clip', 12)} ${esc(f.name)}<button data-dg-rmfile="${i}" aria-label="外す">×</button></span>`).join('')}</div>` : ''}
      <div class="mm-row">
        <label class="mm-attach" title="図面・写真・PDF・文字起こしなど（${DG_MAX_FILES}個まで）"><input type="file" id="dgFile" accept="${DIGEST_ACCEPT}" multiple>${ico('clip')} 図面・写真・資料</label>
        ${DG_SR ? `<button class="mm-attach mm-mic ${DG.mic ? 'is-on' : ''}" id="dgMic" title="話した内容をメモに書き込みます">${DG.mic ? `${ico('stop')} 止める` : `${ico('mic')} 話して入力`}</button>` : ''}
        <span class="mm-sp"></span>
        <button class="btn-link mm-plain" id="dgPlain" title="書いたまま記録します（そのあとAIが段階と次のアクションだけ確かめます）">そのまま記録</button>
        <button class="btn-sm btn-sm-primary" id="dgRun" ${S.meta.ai_available ? 'title="⌘/Ctrl + Enter でも送れます"' : 'disabled title="AIのキーが未設定です"'}>AIにまかせる</button>
      </div>
    </div>`;
  }
  if (st.status === 'analyzing') {
    return `<div class="mm is-busy" id="dgBox"><div class="mm-busy">${st.revising ? 'いただいた回答をふまえて、AIが整理し直しています…' : st.suggestOnly ? '✓ 記録しました。AIが段階と次のアクションを確かめています…' : 'AIが整理しています…'}<span>${esc(st.filename || 'メモ')}</span></div>
      <div class="dg-progress"><div></div></div><div class="mm-note">長い文字起こしや図面は1分ほどかかります。この画面を離れても大丈夫です。</div></div>`;
  }
  if (st.status === 'error') {
    return `<div class="mm is-error" id="dgBox"><div class="mm-busy">整理できませんでした</div>
      <div class="mm-note">${esc(st.error || '')}</div>
      <div class="mm-row"><span class="mm-sp"></span><button class="btn-sm" id="dgReset">メモに戻る</button></div></div>`;
  }
  return dgResultHtml(d, st);
}

// ── AIの提案（今の状況 → 次のアクション → 記録 → ほか） ──
function dgResultHtml(d, st) {
  const r = st.result || {};
  const F = r.fields || {};
  const pl = pipeOf(d);
  const ed = st.edit || {};
  const stage = ed.stage ?? (F.stage || d.stage);
  const lost = stage === pl.lost;
  const closed = lost || stage === pl.won;
  const moved = stage !== d.stage;
  const lr = ed.lost_reason ?? (F.lost_reason || d.lost_reason || '');
  const lt = ed.lost_type ?? (F.lost_type || d.lost_type || '');
  const na = ed.next_action ?? (F.next_action ?? d.next_action ?? '');
  const due = ed.next_action_due ?? (F.next_action_due ?? d.next_action_due ?? '');
  const reap = ed.reapproach_on ?? (F.reapproach_on || (lt === 'competitor' ? dgAddDays(365) : dgAddDays(90)));
  const naNew = 'next_action' in F || 'next_action_due' in F;
  const kinds = S.meta.activity_kinds;

  // 1. 今の状況
  const stageSel = `<select class="email-input compact mm-stage" id="dgStage">${pl.stages.map(s => `<option ${s === stage ? 'selected' : ''}>${esc(s)}</option>`).join('')}</select>`;
  const lostBox = lost ? `<div class="mm-lost">
      <label>理由 <select class="email-input compact" id="dgLostReason">${S.meta.lost_reasons.map(x => `<option ${x === lr ? 'selected' : ''}>${esc(x)}</option>`).join('')}</select></label>
      <label class="mm-grow">くわしく <input class="email-input compact" id="dgLostDetail" maxlength="300" value="${esc(ed.lost_detail ?? (F.lost_detail || d.lost_detail || ''))}"></label>
      <div class="mm-lt">${Object.entries(S.meta.lost_types).map(([k, v]) => `<label><input type="radio" name="dgLostType" value="${k}" ${lt === k ? 'checked' : ''}> ${esc(v)}</label>`).join('')}</div>
    </div>` : '';
  // 2. 次のアクション
  let next;
  if (lost) {
    next = `<div class="mm-next"><div class="mm-next-t">再アプローチ</div>
      <div class="mm-next-row"><span class="mm-next-what">この会社にもう一度声をかける目安</span>
        <input type="date" class="email-input compact" id="dgReap" value="${esc(reap)}"></div></div>`;
  } else if (closed) {
    const canProd = dgCanProd(d, stage);
    const mk = ed.prod_use ?? true;
    next = `<div class="mm-next">${canProd ? `<label class="mm-use mm-use-l"><input type="checkbox" id="dgProdUse" ${mk ? 'checked' : ''}> 「受注後の進行」（承認図作成から）も作る</label>` : ''}
      <div class="mm-note">${canProd ? '名刺・見積明細・現場・納期を引き継ぎます。' : ''}受注になったら感想戦も書いておきましょう。</div></div>`;
  } else if (ed.editNext) {
    next = `<div class="mm-next is-edit"><div class="mm-next-t">次のアクション</div>
      <div class="mm-next-row"><input class="email-input compact mm-grow" id="dgNext" maxlength="200" value="${esc(na)}" placeholder="例: 見積を作って提出">
        <input type="date" class="email-input compact" id="dgDue" value="${esc(due)}"></div>
      <div class="mm-chips">${[['明日', 1], ['3日後', 3], ['1週間後', 7], ['2週間後', 14]].map(([l, n]) => `<button class="mm-chip" data-dg-due="${n}">${l}</button>`).join('')}</div></div>`;
  } else {
    next = `<div class="mm-next"><div class="mm-next-t">次のアクション${naNew ? '<span class="mm-ai">AIの提案</span>' : ''}</div>
      <div class="mm-next-row"><span class="mm-next-what">${na ? esc(na) : '<span class="dg-empty">なし</span>'}</span>
        <span class="mm-due">${due ? `期限 ${esc(fmtDate(due))}` : '期限なし'}</span></div>
      ${r.next_reason && naNew ? `<div class="mm-why">${esc(r.next_reason)}</div>` : ''}
      <div class="mm-decide"><button class="btn-sm" id="dgEditNext">直す</button><span class="mm-note">このままでよければ下の「この内容で反映」へ</span></div></div>`;
  }
  // 4. ほかにも見つけたもの
  const pf = profile();
  const others = DG_OTHER.filter(([k]) => k in F).map(([k, label, type]) => {
    const v = F[k];
    let input;
    if (type === 'segment') input = selectHtml('', pf.segments, v).replace('<select class="email-input compact" id=""', `<select class="email-input compact" data-dg-field="${k}"`);
    else if (type === 'channel') input = selectHtml('', pf.channels, v).replace('<select class="email-input compact" id=""', `<select class="email-input compact" data-dg-field="${k}"`);
    else input = `<input class="email-input compact" type="${type}" ${type === 'number' ? 'min="0" step="1000"' : ''} data-dg-field="${k}" value="${esc(v)}">`;
    return `<label class="dg-frow"><input type="checkbox" data-dg-use="${k}" checked>
      <span class="dg-flabel">${esc(dgLabel(k, label))}</span>
      <span class="dg-cur" title="今の値">${dgShow(k, d[k])}</span><span class="dg-arrow">→</span>
      <span class="dg-new">${input}</span></label>`;
  }).join('');
  const checkRows = (r.checks || []).map((ck, i) => `
    <label class="dg-crow"><input type="checkbox" data-dg-check="${i}" checked>
      <span class="dg-clabel">✓ ${esc(ck.label)}</span>
      <input class="email-input compact" data-dg-cnote="${i}" value="${esc(ck.note || '')}" placeholder="わかったこと"></label>`).join('');
  const L = itemLabels();
  const itemRows = (r.items || []).map((it, i) => `<tr>
      <td><input type="checkbox" data-dg-item="${i}" checked></td>
      ${['name', 'material', 'thickness', 'qty', 'unit_price'].map(f => `<td><input class="email-input compact" data-dg-if="${i}:${f}" type="${['qty', 'unit_price'].includes(f) ? 'number' : 'text'}" value="${esc(it[f] ?? '')}"></td>`).join('')}
    </tr>`).join('');
  const nOther = DG_OTHER.filter(([k]) => k in F).length + (r.checks || []).length + (r.items || []).length;
  const otherNames = [...DG_OTHER.filter(([k]) => k in F).map(([k, l]) => dgLabel(k, l)), (r.checks || []).length ? `確認項目${r.checks.length}` : '', (r.items || []).length ? `見積明細${r.items.length}行` : ''].filter(Boolean);

  return `<div class="mm is-ready" id="dgBox">
    <div class="mm-head"><span class="mm-title">${st.suggestOnly ? '記録しました。AIの提案' : 'AIの整理'}</span>
      <span class="dg-src">${esc(st.filename || 'メモ')}${st.has_source ? ` <a href="/api/sales/digests/${st.id}/source" target="_blank" rel="noopener">元の文章</a>` : ''}</span></div>
    <div class="mm-plan"><b>反映すると</b>${dgPlan(d, st).map(x => `<span>${esc(x)}</span>`).join('') || '<span>記録だけ残します</span>'}
      <em>違うところだけ直して「この内容で反映」</em></div>


    <div class="mm-step mm-s1"><div class="mm-step-n">1</div><div class="mm-step-b">
      <div class="mm-step-t">今の状況</div>
      ${r.situation ? `<div class="mm-situ">${esc(r.situation)}</div>` : ''}
      <div class="mm-stage-row">${moved ? `<span class="stage-badge" data-stage="${esc(d.stage)}">${esc(d.stage)}</span><span class="dg-arrow">→</span>` : ''}${stageSel}
        <span class="mm-note">${moved ? '段階を動かします' : '段階はそのまま'}</span></div>
      ${lostBox}
    </div></div>

    <div class="mm-step"><div class="mm-step-n">2</div><div class="mm-step-b">${next}${closed ? '' : dgAppoHtml(d, st)}</div></div>
    ${dgVendorStepHtml(d, st)}
    ${dgGroupStepHtml(d, st)}

    ${st.suggestOnly ? '' : `<div class="mm-step mm-s3"><div class="mm-step-n">3</div><div class="mm-step-b">
      <div class="mm-step-t">記録に残す内容</div>
      ${ed.editRec ? `<div class="dg-rec">
          <select class="email-input compact" id="dgKind">${['meeting', 'call', 'email', 'memo'].map(k => `<option value="${k}" ${(ed.kind || r.kind) === k ? 'selected' : ''}>${esc(kinds[k])}</option>`).join('')}</select>
          <input type="date" class="email-input compact" id="dgWhen" value="${esc(ed.when || r.occurred_at || todayStr())}">
          <input class="email-input compact" id="dgTitle" value="${esc(ed.title ?? r.title ?? '')}" placeholder="件名" maxlength="200"></div>
        <textarea class="email-input compact dg-minutes" id="dgMinutes" rows="8" maxlength="20000">${esc(ed.minutes ?? r.minutes ?? '')}</textarea>`
      : `<div class="mm-rec"><div class="mm-rec-h"><span class="latest-kind" data-kind="${esc(r.kind || 'memo')}">${esc(kinds[r.kind] || 'メモ')}</span><b>${esc(ed.title ?? r.title ?? '')}</b><span class="mm-note">${esc(fmtDate(ed.when || r.occurred_at || todayStr()))}</span></div>
          <div class="mm-rec-b">${esc(ed.minutes ?? r.minutes ?? '')}</div>
          <button class="btn-link" id="dgEditRec">全文を見る・直す</button></div>`}
    </div></div>`}

    ${nOther ? `<details class="mm-other" ${ed.openOther !== false ? 'open' : ''}><summary>ほかにも更新するもの（${esc(otherNames.join('・'))}）— 違うものはチェックを外す</summary>
      ${others ? `<div class="dg-fields">${others}</div>` : ''}
      ${checkRows ? `<div class="dg-sec"><div class="dg-sec-title">確認できた項目</div>${checkRows}</div>` : ''}
      ${itemRows ? `<div class="dg-sec"><div class="dg-sec-title">見積明細に追加</div><div class="items-scroll"><table class="items-table dg-items">
        <thead><tr><th></th><th>${esc(L.name)}</th><th>${esc(L.material)}</th><th>${esc(L.thickness)}</th><th>${esc(L.qty)}</th><th>単価</th></tr></thead>
        <tbody>${itemRows}</tbody></table></div></div>` : ''}
    </details>` : ''}
    ${(r.unclear || []).length ? `<div class="mm-unclear"><b>次に確かめること</b><span class="mm-note">わかるものは答えを書いてください（空欄のままでも大丈夫）</span>
      <ol>${r.unclear.map((u, i) => `<li><div class="mm-q">${esc(u)}</div>
        <input class="email-input compact mm-ans" data-dg-ans="${i}" maxlength="1000" value="${esc((ed.ans || {})[i] || '')}" placeholder="答え（例: 当社に来社。同席は加藤マネージャー）"></li>`).join('')}</ol></div>` : ''}
    <div class="mm-fix"><label for="dgFix">AIに直してほしいこと</label>
      <textarea class="email-input compact" id="dgFix" rows="2" maxlength="2000" placeholder="例: 段階はアポのまま。次のアクションは来週火曜に見積の内容を電話で確認">${esc(ed.fix || '')}</textarea>
      <div class="mm-note">答えや直してほしいことを書いたら「AIに直してもらう」。この画面の内容から作り直します。そのまま「この内容で反映」を押すと、答えは記録に残ります</div></div>

    <div class="dg-actions"><button class="btn-sm" id="dgDiscard">やめる</button>
      <button class="btn-sm mm-redo" id="dgRedo">AIに直してもらう</button>
      <button class="btn-sm btn-sm-primary" id="dgApply">この内容で反映</button></div>
  </div>`;
}

async function dgPoll(dealId, id) {
  for (let i = 0; i < 150; i++) {
    await new Promise(res => setTimeout(res, i < 5 ? 1500 : 3000));
    let job;
    try { job = await api(`/api/sales/digests/${id}`); } catch { continue; }
    if (job.status !== 'analyzing') {
      const prev = DG.byDeal[dealId] || {};
      if (prev.suggestOnly && !prev.revising && (job.status === 'error' || !dgHasChange(job.result))) {
        // 記録はもう保存済み。変えることがなければ何も出さない
        DG.byDeal[dealId] = { status: 'idle' };
        if (S.detail && S.detail.id === dealId) renderDetail();
        if (job.status !== 'error') showToast('AIが確かめました。段階と次のアクションはこのままでよさそうです', { type: 'info', duration: 2500 });
        return;
      }
      DG.byDeal[dealId] = { ...prev, ...job, revising: false, edit: {} };
      if (S.detail && S.detail.id === dealId) { renderDetail(); dgScroll(); }
      else showToast('AIの整理が終わりました。案件を開いて確認してください', { type: 'info', duration: 5000 });
      return;
    }
  }
}

// 「次に確かめること」に書いた答え
function dgAnswers(st) {
  const un = (st.result || {}).unclear || [];
  const ans = (st.edit || {}).ans || {};
  return Object.entries(ans).map(([i, a]) => ({ q: un[Number(i)] || '', a: String(a).trim() })).filter(x => x.a);
}

// 答え・直してほしいことを足して、AIに作り直してもらう
async function dgRedo(root, d) {
  dgKeep(root, d);
  const st = DG.byDeal[d.id];
  const qa = dgAnswers(st);
  const note = ((st.edit || {}).fix || '').trim();
  if (!qa.length && !note) {
    showToast('「次に確かめること」の答えか、「AIに直してほしいこと」を書いてください', { type: 'warning' });
    const el = root.querySelector('[data-dg-ans]') || root.querySelector('#dgFix'); if (el) el.focus();
    return;
  }
  const btn = root.querySelector('#dgRedo'); btn.disabled = true;
  try {
    const res = await api(`/api/sales/digests/${st.id}/revise`, { method: 'POST', body: JSON.stringify({ answers: qa, note }) });
    DG.byDeal[d.id] = { id: res.id, status: 'analyzing', filename: st.filename, suggestOnly: st.suggestOnly, revising: true, draft: st.draft };
    renderDetail(); dgScroll();
    dgPoll(d.id, res.id);
  } catch (e) { showToast(e.message, { type: 'error', title: '作り直せませんでした' }); btn.disabled = false; }
}

// AIの結果に、案件を変える提案があるか
function dgHasChange(r) {
  if (!r) return false;
  return Object.keys(r.fields || {}).length > 0 || (r.checks || []).length > 0 || (r.items || []).length > 0 || !!r.vendor_request || !!r.group_request;
}

// 受注にしたとき「受注後の進行」も作れるか
function dgCanProd(d, stage) {
  const pl = pipeOf(d);
  return pl.key === 'sales' && stage === pl.won && !d.prod_deal_id && (S.meta.pipelines || []).some(p => p.key === 'prod');
}

// アポ・訪問の予定（AIがメモから日時を見つけたとき）
function dgAppoHtml(d, st) {
  const F = (st.result || {}).fields || {};
  if (!('appo_at' in F)) return '';
  const ed = st.edit || {};
  const use = ed.appo_use ?? true;
  const v = F.appo_at || '';
  const day = ed.appo_day ?? v.slice(0, 10);
  const tm = ed.appo_time ?? (v.length >= 16 ? v.slice(11, 16) : '');
  return `<div class="mm-appo"><label class="mm-use mm-use-l"><input type="checkbox" id="dgAppoUse" ${use ? 'checked' : ''}> ${ico('meet')} アポ・訪問の予定に入れる</label>
    ${use ? `<div class="mm-next-row"><input type="date" class="email-input compact" id="dgAppoDay" value="${esc(day)}">
      <input type="time" class="email-input compact" id="dgAppoTime" value="${esc(tm)}" step="300">
      <span class="mm-note">${d.appo_at ? `今は ${esc(fmtDate(d.appo_at.slice(0, 10)))}${d.appo_at.length >= 16 ? ' ' + esc(d.appo_at.slice(11, 16)) : ''}` : 'カレンダーにも出ます'}</span></div>` : ''}</div>`;
}

// 和宏電機：ワコーメタルに筐体を依頼（メモに頼むと書いてあったとき）
function dgGroupStepHtml(d, st) {
  const g = (st.result || {}).group_request;
  if (!g || d.group_deal_id) return '';
  const ed = st.edit || {};
  const use = ed.grp_use ?? true;
  return `<div class="mm-step"><div class="mm-step-n">${ico('factory', 13)}</div><div class="mm-step-b">
    <div class="mm-step-t">ワコーメタルに筐体（板金）を依頼する<span class="mm-ai">AIの提案</span>
      <label class="mm-use"><input type="checkbox" id="dgGrpUse" ${use ? 'checked' : ''}> 依頼する</label></div>
    ${use ? `<textarea class="email-input compact" id="dgGrpNote" rows="3" maxlength="2000" placeholder="依頼内容 例: 屋外自立形 2面・SUS・図面は来週送付">${esc(ed.grp_note ?? g.note ?? '')}</textarea>
      <div class="mm-note">ワコーメタルの営業に「ヒアリング」の案件ができます（現場名・納期・明細も渡します）</div>` : '<div class="mm-note">今回は依頼しません</div>'}
  </div></div>`;
}

// 「反映すると」何が起きるか（チェックの状態から）
function dgPlan(d, st) {
  const r = st.result || {};
  const F = r.fields || {};
  const ed = st.edit || {};
  const pl = pipeOf(d);
  const stage = ed.stage ?? (F.stage || d.stage);
  const out = [];
  if (stage !== d.stage) out.push(`段階 ${d.stage}→${stage}`);
  if (stage === pl.lost) out.push('再アプローチ日を設定');
  else if (stage !== pl.won) {
    const na = ed.next_action ?? (F.next_action ?? d.next_action ?? '');
    if (na && (na !== (d.next_action || '') || (ed.next_action_due ?? F.next_action_due ?? d.next_action_due) !== d.next_action_due)) out.push(`次のアクション「${na.length > 18 ? na.slice(0, 18) + '…' : na}」`);
    if ('appo_at' in F && (ed.appo_use ?? true)) out.push('アポの予定');
  }
  if (r.vendor_request && dgVqOn(d, st)) out.push('業者に再テレアポを依頼');
  if (dgCanProd(d, stage) && (ed.prod_use ?? true)) out.push('受注後の進行を作る');
  if (r.group_request && !d.group_deal_id && (ed.grp_use ?? true)) out.push('ワコーメタルに依頼');
  const DGO = DG_OTHER.filter(([k]) => k in F).length + (r.checks || []).length + (r.items || []).length;
  if (DGO) out.push(`ほか${DGO}件`);
  if (!st.suggestOnly) out.push('記録を残す');
  return out;
}

function dgVqOn(d, st) {
  const vendors = ((VQ.byDeal[d.id] || {}).vendors) || [];
  if (!vendors.length) return false;
  const openReq = ((VQ.byDeal[d.id] || {}).requests || []).find(x => x.kind === 'recall' && x.status === 'open');
  return (st.edit || {}).vq_use ?? !openReq;
}

// メモに「業者に再アポを頼む」があったとき：依頼の中身（用件・業者・電話・ひとこと）を出す
function dgVendorStepHtml(d, st) {
  const r = st.result || {};
  const vr = r.vendor_request;
  const vendors = ((VQ.byDeal[d.id] || {}).vendors) || [];
  if (!vr || !vendors.length) return '';
  const ed = st.edit || {};
  const openReq = ((VQ.byDeal[d.id] || {}).requests || []).find(x => x.kind === 'recall' && x.status === 'open');
  const use = dgVqOn(d, st);   // もう頼んでいる（電話待ち）ときは、最初は頼まない
  const card = d.card || {};
  const purpose = ed.vq_purpose ?? vr.purpose;
  const purposes = (VQ.meta && VQ.meta.purposes) || ['再訪問の連絡', 'お仕事ないですかの連絡', '現状の確認', 'その他'];
  return `<div class="mm-step"><div class="mm-step-n">${ico('phone', 13)}</div><div class="mm-step-b">
    <div class="mm-step-t">テレアポ業者に再テレアポを頼む<span class="mm-ai">AIの提案</span>
      <label class="mm-use"><input type="checkbox" id="dgVqUse" ${use ? 'checked' : ''}> 頼む</label></div>
    ${use ? `<div class="vq-purposes">${purposes.map(p => `<button class="vq-purpose ${p === purpose ? 'is-on' : ''}" data-dg-vqp="${esc(p)}">${esc(p)}</button>`).join('')}</div>
    <div class="vq-grid2" style="margin-top:8px">
      <select class="email-input compact" id="dgVqVendor">${vendors.map(v => `<option value="${v.id}" ${String(v.id) === String(ed.vq_vendor || '') ? 'selected' : ''}>${esc(v.name)}</option>`).join('')}</select>
      <input class="email-input compact" id="dgVqPhone" maxlength="40" inputmode="tel" placeholder="業者がかける電話番号（必須）" value="${esc(ed.vq_phone ?? (card.phone || card.mobile || d.phone_snapshot || ''))}">
    </div>
    <textarea class="email-input compact" id="dgVqNote" rows="2" maxlength="2000" style="margin-top:6px" placeholder="業者へのひとこと（業者の画面に出ます）">${esc(ed.vq_note ?? vr.note ?? '')}</textarea>
    <div class="mm-note">業者には会社名・担当者・電話とこのひとことだけが届きます。${vr.due_on ? `いつまでに: ${esc(fmtDate(vr.due_on))}` : ''}</div>`
      : `<div class="mm-note">${openReq ? `${esc(openReq.vendor_name || '業者')}にはもう再テレアポを頼んでいて、電話待ちです。重ねて頼むときはチェックを入れてください` : '今回は頼みません'}</div>`}
  </div></div>`;
}

// AIを使わずに記録したあとも、AIに段階と次のアクションを確かめてもらう（記録は二重に作らない）
async function dgSuggest(dealId, text) {
  if (!S.meta.ai_available || !text || text.trim().length < 6) return;
  const st = DG.byDeal[dealId];
  if (st && ['analyzing', 'ready'].includes(st.status)) return;   // 別の整理が進んでいるときは邪魔しない
  try {
    const fd = new FormData(); fd.append('text', text.trim());
    const res = await fetch(`/api/sales/deals/${dealId}/digest`, { method: 'POST', body: fd, credentials: 'include' });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) return;
    DG.byDeal[dealId] = { id: body.id, status: 'analyzing', filename: '記録した内容', suggestOnly: true };
    if (S.detail && S.detail.id === dealId) renderDetail();
    dgPoll(dealId, body.id);
  } catch { /* 提案が出ないだけ */ }
}

function dgScroll() { const b = detailContainer().querySelector('#dgBox'); if (b) b.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); }

// いま画面に入っている値を覚える（再描画しても消えないように）
function dgKeep(root, d) {
  const st = DG.byDeal[d.id];
  if (!st || st.status !== 'ready') return;
  const ed = st.edit = st.edit || {};
  const v = sel => { const el = root.querySelector(sel); return el ? el.value : undefined; };
  const set = (k, x) => { if (x !== undefined) ed[k] = x; };
  set('stage', v('#dgStage')); set('lost_reason', v('#dgLostReason')); set('lost_detail', v('#dgLostDetail'));
  const lt = root.querySelector('input[name="dgLostType"]:checked'); if (lt) ed.lost_type = lt.value;
  set('next_action', v('#dgNext')); set('next_action_due', v('#dgDue')); set('reapproach_on', v('#dgReap'));
  set('kind', v('#dgKind')); set('when', v('#dgWhen')); set('title', v('#dgTitle')); set('minutes', v('#dgMinutes'));
  const o = root.querySelector('.mm-other'); if (o) ed.openOther = o.open;
  const vu = root.querySelector('#dgVqUse'); if (vu) ed.vq_use = vu.checked;
  set('vq_vendor', v('#dgVqVendor')); set('vq_phone', v('#dgVqPhone')); set('vq_note', v('#dgVqNote'));
  const ans = root.querySelectorAll('[data-dg-ans]');
  if (ans.length) { ed.ans = {}; ans.forEach(x => { if (x.value.trim()) ed.ans[x.dataset.dgAns] = x.value; }); }
  set('fix', v('#dgFix'));
  const ck = (id, k) => { const el = root.querySelector(id); if (el) ed[k] = el.checked; };
  ck('#dgAppoUse', 'appo_use'); ck('#dgProdUse', 'prod_use'); ck('#dgGrpUse', 'grp_use');
  set('appo_day', v('#dgAppoDay')); set('appo_time', v('#dgAppoTime')); set('grp_note', v('#dgGrpNote'));
}

function bindDigest(root, d) {
  const q = sel => root.querySelector(sel);
  if (DG.mic) dgMicStop();   // 画面を描き直したら音声入力は止める（古い入力欄に書き込まないように）
  const f = q('#dgFile');
  const text = q('#dgText');
  if (text) {
    text.oninput = e => { DG.byDeal[d.id] = { status: 'idle', draft: e.target.value }; };
    text.onfocus = () => text.classList.add('is-open');
    text.onkeydown = e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && q('#dgRun') && !q('#dgRun').disabled) { e.preventDefault(); q('#dgRun').click(); } };
    if (text.value) text.classList.add('is-open');
  }
  if (f) f.onchange = () => {
    const cur = DG.files[d.id] || [];
    const add = [...f.files];
    if (cur.length + add.length > DG_MAX_FILES) showToast(`添付は${DG_MAX_FILES}個までです`, { type: 'warning' });
    DG.files[d.id] = [...cur, ...add].slice(0, DG_MAX_FILES);
    if (text) DG.byDeal[d.id] = { status: 'idle', draft: text.value };
    renderDetail();
    const t = detailContainer().querySelector('#dgText'); if (t) t.classList.add('is-open');
  };
  root.querySelectorAll('[data-dg-rmfile]').forEach(b => b.onclick = () => {
    (DG.files[d.id] || []).splice(Number(b.dataset.dgRmfile), 1);
    if (text) DG.byDeal[d.id] = { status: 'idle', draft: text.value };
    renderDetail();
  });
  if (q('#dgMic')) q('#dgMic').onclick = () => { if (DG.mic) dgMicFinish(); else dgMicStart(d, text, q('#dgMic')); };   // 止めるときは最後の言葉まで入れる
  if (q('#dgPlain')) q('#dgPlain').onclick = async () => {
    const body = text.value.trim();
    if (!body) { showToast('メモを書いてください', { type: 'warning' }); return; }
    if ((DG.files[d.id] || []).length) { showToast('添付ファイルはAIが読むときに使います。「AIにまかせる」を押してください', { type: 'warning' }); return; }
    const first = body.split('\n')[0];
    q('#dgPlain').disabled = true;
    try {
      const one = !body.includes('\n') && body.length <= 30;   // 1行の短いメモは件名だけにする
      await api(`/api/sales/deals/${d.id}/activities`, { method: 'POST', body: JSON.stringify({ kind: 'memo', title: first.length > 30 ? first.slice(0, 30) + '…' : first, body: one ? '' : body }) });
      DG.byDeal[d.id] = { status: 'idle' };
      await refreshCurrent();
      showToast('メモを記録しました', { type: 'success', duration: 1500 });
      dgSuggest(d.id, body);
    } catch (e) { showToast(e.message, { type: 'error' }); q('#dgPlain').disabled = false; }
  };
  if (q('#dgRun')) q('#dgRun').onclick = async () => {
    q('#dgRun').disabled = true;
    await dgMicFinish();
    q('#dgRun').disabled = false;
    const t = text.value.trim();
    const files = DG.files[d.id] || [];
    if (!t && !files.length) { showToast('メモを書くか、ファイルを添付してください', { type: 'warning' }); text.focus(); return; }
    const fd = new FormData();
    if (t) fd.append('text', t);
    files.forEach(x => fd.append('files', x));
    q('#dgRun').disabled = true;
    try {
      const res = await fetch(`/api/sales/deals/${d.id}/digest`, { method: 'POST', body: fd, credentials: 'include' });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
      DG.byDeal[d.id] = { id: body.id, status: 'analyzing', filename: files.length ? `メモ＋${files.map(x => x.name).join('・')}` : 'メモ', draft: t };
      DG.files[d.id] = [];
      renderDetail();
      dgPoll(d.id, body.id);
    } catch (e) { showToast(e.message, { type: 'error', title: '整理できませんでした' }); q('#dgRun').disabled = false; }
  };
  if (q('#dgReset')) q('#dgReset').onclick = () => { DG.byDeal[d.id] = { status: 'idle', draft: (DG.byDeal[d.id] || {}).draft }; renderDetail(); };
  if (q('#dgDiscard')) q('#dgDiscard').onclick = () => {
    dgKeep(root, d);
    const ed = (DG.byDeal[d.id] || {}).edit || {};
    if ((Object.keys(ed.ans || {}).length || (ed.fix || '').trim()) && !confirm('書いた答えも消えます。やめますか？')) return;
    DG.byDeal[d.id] = { status: 'idle' }; renderDetail();
  };
  if (q('#dgRedo')) q('#dgRedo').onclick = () => dgRedo(root, d);
  const redraw = () => { dgKeep(root, d); renderDetail(); };
  if (q('#dgStage')) q('#dgStage').onchange = redraw;
  root.querySelectorAll('input[name="dgLostType"]').forEach(x => x.onchange = () => {
    dgKeep(root, d); const ed = DG.byDeal[d.id].edit; ed.reapproach_on = dgAddDays(x.value === 'competitor' ? 365 : 90); renderDetail();
  });
  if (q('#dgEditNext')) q('#dgEditNext').onclick = () => { dgKeep(root, d); DG.byDeal[d.id].edit.editNext = true; renderDetail();
    const n = detailContainer().querySelector('#dgNext'); if (n) n.focus(); };
  root.querySelectorAll('[data-dg-due]').forEach(b => b.onclick = () => { q('#dgDue').value = dgAddDays(Number(b.dataset.dgDue)); });
  if (q('#dgEditRec')) q('#dgEditRec').onclick = () => { dgKeep(root, d); DG.byDeal[d.id].edit.editRec = true; renderDetail(); };
  ['#dgVqUse', '#dgAppoUse', '#dgProdUse', '#dgGrpUse'].forEach(id => { if (q(id)) q(id).onchange = redraw; });
  root.querySelectorAll('[data-dg-vqp]').forEach(b => b.onclick = () => { dgKeep(root, d); DG.byDeal[d.id].edit.vq_purpose = b.dataset.dgVqp; renderDetail(); });
  if (q('#dgApply')) q('#dgApply').onclick = () => dgApply(root, d);
}

async function dgApply(root, d) {
  dgKeep(root, d);
  const st = DG.byDeal[d.id];
  const r = st.result || {};
  const F = r.fields || {};
  const ed = st.edit || {};
  const pl = pipeOf(d);
  const patch = {};
  // 1. 段階・失注
  const stage = ed.stage || F.stage || d.stage;
  if (stage !== d.stage) patch.stage = stage;
  if (stage === pl.lost) {
    patch.lost_reason = ed.lost_reason || F.lost_reason || d.lost_reason || 'その他';
    const det = (ed.lost_detail ?? F.lost_detail ?? '').trim();
    if (det !== (d.lost_detail || '')) patch.lost_detail = det || null;
    const lt = ed.lost_type || F.lost_type;
    if (lt) patch.lost_type = lt;
    const reap = ed.reapproach_on || F.reapproach_on;
    if (reap) patch.reapproach_on = reap;
  } else if (stage !== pl.won) {
    // 2. 次のアクション
    const na = (ed.next_action ?? F.next_action ?? d.next_action ?? '').trim();
    const due = ed.next_action_due ?? F.next_action_due ?? d.next_action_due ?? '';
    if (na !== (d.next_action || '')) patch.next_action = na || null;
    if ((due || '') !== (d.next_action_due || '')) patch.next_action_due = due || null;
    if ('appo_at' in F && (ed.appo_use ?? true)) {
      const day = ed.appo_day ?? (F.appo_at || '').slice(0, 10);
      const tm = ed.appo_time ?? ((F.appo_at || '').length >= 16 ? F.appo_at.slice(11, 16) : '');
      if (!day) { showToast('アポの日にちを入れてください（入れないならチェックを外す）', { type: 'warning' }); return; }
      const ap = tm ? `${day}T${tm}` : day;
      if (ap !== (d.appo_at || '')) patch.appo_at = ap;
    }
  }
  // 4. ほか
  root.querySelectorAll('[data-dg-use]').forEach(cb => {
    if (!cb.checked) return;
    const k = cb.dataset.dgUse;
    const el = root.querySelector(`[data-dg-field="${k}"]`);
    let v = el ? el.value : null;
    if (k === 'amount') v = v === '' ? null : Math.max(0, parseInt(v, 10) || 0);
    else if (v === '') v = null;
    patch[k] = v;
  });
  const checks = {};
  root.querySelectorAll('[data-dg-check]').forEach(cb => {
    if (!cb.checked) return;
    const ck = r.checks[Number(cb.dataset.dgCheck)];
    checks[ck.key] = { done: !!ck.done, note: root.querySelector(`[data-dg-cnote="${cb.dataset.dgCheck}"]`).value };
  });
  const items = [];
  root.querySelectorAll('[data-dg-item]').forEach(cb => {
    if (!cb.checked) return;
    const i = cb.dataset.dgItem;
    const g = f => (root.querySelector(`[data-dg-if="${i}:${f}"]`) || {}).value || '';
    const n = f => g(f) === '' ? null : Math.max(0, parseInt(g(f), 10) || 0);
    if (g('name').trim()) items.push({ name: g('name').trim(), material: g('material').trim() || null,
      thickness: g('thickness').trim() || null, qty: n('qty'), unit_price: n('unit_price') });
  });
  // 3. 記録
  const minutes = (ed.minutes ?? r.minutes ?? '').trim();
  const title = (ed.title ?? r.title ?? '').trim();
  const when = ed.when || r.occurred_at || todayStr();
  const qa = dgAnswers(st);
  const body = { fields: patch, checks, items };
  if (!st.suggestOnly && (minutes || title)) {   // 記録を先に保存したとき（suggestOnly）は記録を二重に作らない
    body.record = { kind: ed.kind || r.kind || 'memo', title: title || '面談の記録', body: qa.length ? `${minutes}\n\n■確認したこと\n${qa.map(x => `・${x.q} → ${x.a}`).join('\n')}`.trim() : minutes,
      occurred_at: when ? `${when}T${when === todayStr() ? nowLocal().slice(11) : '09:00'}:00` : null };
  }
  if (st.suggestOnly && qa.length) {   // 記録はもう保存済み。答えだけ別の記録に残す
    body.record = { kind: 'memo', title: '確認したこと', body: qa.map(x => `・${x.q} → ${x.a}`).join('\n'), occurred_at: null };
  }
  const vr = r.vendor_request;
  if (vr && (ed.vq_use ?? true) && root.querySelector('#dgVqVendor')) {
    const card = d.card || {};
    const phone = (ed.vq_phone ?? '').trim();
    if (!phone) { showToast('業者がかける電話番号を入れてください（頼まないなら「頼む」のチェックを外す）', { type: 'warning' }); root.querySelector('#dgVqPhone').focus(); return; }
    const purpose = ed.vq_purpose ?? vr.purpose;
    if (purpose === 'その他' && !(ed.vq_note ?? vr.note ?? '').trim()) { showToast('「その他」のときは業者へのひとことに用件を書いてください', { type: 'warning' }); return; }
    body.vendor_request = { vendor_id: Number(ed.vq_vendor || root.querySelector('#dgVqVendor').value), purpose,
      note: (ed.vq_note ?? vr.note ?? '').trim() || null, due_on: vr.due_on || null, phone,
      company: card.company || d.company_snapshot || null, contact: card.name || d.contact_snapshot || null,
      department: card.department_position || null };
  }
  if (dgCanProd(d, stage) && (ed.prod_use ?? true)) body.make_prod = true;
  if (r.group_request && !d.group_deal_id && (ed.grp_use ?? true)) body.group_request = { note: (ed.grp_note ?? r.group_request.note ?? '').trim() || null };
  const btn = root.querySelector('#dgApply');
  btn.disabled = true;
  try {
    const res = await api(`/api/sales/digests/${st.id}/apply`, { method: 'POST', body: JSON.stringify(body) });
    DG.byDeal[d.id] = { status: 'idle' };
    if (body.vendor_request) await vqLoad(d.id);
    if (res.prod_id) await loadDeals();
    await refreshCurrent();
    const done = [patch.stage ? `段階を「${patch.stage}」に` : '', body.vendor_request ? '業者に再テレアポを依頼' : '',
      res.prod_id ? `受注後の進行 #${res.prod_id} を作成` : '', res.group_id ? `ワコーメタルに依頼（#${res.group_id}）` : '',
      patch.appo_at ? 'アポの予定を登録' : ''].filter(Boolean);
    showToast(done.length ? `反映しました: ${done.join('・')}` : '記録と案件を更新しました', { type: 'success', duration: 3500 });
  } catch (e) {
    showToast(e.message, { type: 'error', title: '反映できませんでした（何も変えていません）' });
    btn.disabled = false;
  }
}
