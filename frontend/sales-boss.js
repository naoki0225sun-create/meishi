// ============================================================
// 営業部長 — 全案件を見張る「部長の目」と、案件ごとの助言の表示
// sales.js の S / api / esc などを使う
// ============================================================

const LEVEL_LABEL = { high: '至急', mid: '要注意', low: 'できれば', info: '参考' };

function adviceItemsHtml(list, { compact = false } = {}) {
  if (!list || !list.length) return '<div class="boss-empty">いまのところ指摘はありません</div>';
  return list.map(a => `
    <div class="boss-item level-${esc(a.level)}">
      <span class="boss-level">${LEVEL_LABEL[a.level] || ''}</span>
      <div class="boss-body">
        <div class="boss-title">${esc(a.title)}</div>
        ${compact ? '' : `<div class="boss-detail">${esc(a.detail)}</div>`}
        ${!compact && a.principle ? `<button class="boss-principle" data-manual="${esc(a.principle)}" title="マニュアルの該当箇所を開く">${ico('book', 12)} ${esc(a.principle)}</button>` : ''}
      </div>
    </div>`).join('');
}

// 案件詳細に入れる「営業部長より」
function bossDealHtml(d) {
  const ai = d.ai_advice;
  const aiHtml = ai ? `
    <div class="boss-ai">
      <div class="boss-ai-head"><span>部長の見立て（AI）</span><span class="boss-ai-time">${esc(fmtDateTime(ai.created_at))}</span></div>
      <div class="boss-ai-headline">${esc(ai.headline)}</div>
      ${ai.stage_view ? `<div class="boss-ai-row"><b>本当の段階</b>${esc(ai.stage_view)}</div>` : ''}
      ${ai.risks?.length ? `<div class="boss-ai-row"><b>買わない理由になりそうなこと</b><ul>${ai.risks.map(x => `<li>${esc(x)}</li>`).join('')}</ul></div>` : ''}
      ${ai.next_actions?.length ? `<div class="boss-ai-row"><b>今週やること</b><ul>${ai.next_actions.map(x => `<li>${esc(x)}</li>`).join('')}</ul></div>` : ''}
      ${ai.questions?.length ? `<div class="boss-ai-row"><b>お客様に聞くこと</b><ul>${ai.questions.map(x => `<li>${esc(x)}<button class="btn-link" data-copy-q="${esc(x)}">コピー</button></li>`).join('')}</ul></div>` : ''}
    </div>` : '';
  return `
    <div class="detail-section boss-section">
      <div class="section-label"><span>営業部長より</span>
        ${S.meta.ai_available ? `<button class="section-action" id="bossAsk">${ai ? '相談し直す（AI）' : '部長に相談（AI）'}</button>` : ''}</div>
      ${adviceItemsHtml((d.advice || []).slice(0, 3))}
      ${(d.advice || []).length > 3 ? `<details class="boss-more"><summary>ほかの指摘 ${d.advice.length - 3}件</summary>${adviceItemsHtml(d.advice.slice(3))}</details>` : ''}
      ${aiHtml}
    </div>`;
}

function bindBossDeal(root, d) {
  root.querySelectorAll('[data-manual]').forEach(b => b.onclick = () => openManual(b.dataset.manual));
  root.querySelectorAll('[data-copy-q]').forEach(b => b.onclick = () => copyText(b.dataset.copyQ, '質問'));
  const ask = root.querySelector('#bossAsk');
  if (ask) ask.onclick = async () => {
    ask.disabled = true; ask.textContent = '部長が考えています…';
    try {
      await api(`/api/sales/advisor/deals/${d.id}`, { method: 'POST' });
      await refreshCurrent();
    } catch (e) {
      showToast(e.message, { type: 'error', title: '相談に失敗' });
      ask.disabled = false; ask.textContent = '部長に相談（AI）';
    }
  };
}

// ── 部長の目（全体） ──
const BOSS = { data: null, open: {}, owner: '' };

async function renderBoss() {
  const pane = $('viewPane');
  pane.innerHTML = '<div class="empty-text">集計中…</div>';
  try {
    BOSS.data = await api(`/api/sales/advisor/team${BOSS.owner ? `?owner=${encodeURIComponent(BOSS.owner)}` : ''}`);
  } catch (e) {
    pane.innerHTML = `<div class="empty-text">${esc(e.message)}</div>`;
    return;
  }
  drawBoss();
}

function drawBoss() {
  const pane = $('viewPane');
  const all = BOSS.data;
  const pkey = all.pipelines[S.pipeline] ? S.pipeline : 'sales';
  const pv = all.pipelines[pkey];
  if (!pv) { pane.innerHTML = '<div class="empty-text">データがありません</div>'; return; }
  const s = pv.summary;
  const isSales = pkey === 'sales';
  const byId = Object.fromEntries(S.allDeals.map(d => [d.id, d]));
  const dealLink = id => {
    const d = byId[id];
    if (!d) return '';
    const company = (d.card && d.card.company) || d.company_snapshot || '';
    return `<button class="boss-deal" data-open-deal="${id}"><span class="deal-no">#${id}</span> ${esc(company)} ${esc(d.title)} <span class="stage-badge" data-stage="${esc(d.stage)}">${esc(d.stage)}</span></button>`;
  };
  const pct = v => v == null ? '—' : Math.round(v * 100) + '%';
  const tiles = isSales ? [
    ['進行中', `${s.open}件`],
    ['進行中の金額', yen(s.open_amount) || '¥0'],
    ['見込み額（金額×確度）', yen(s.weighted) || '¥0'],
    ['受注率（直近90日）', (s.won_90 + s.lost_90) < 3 ? (s.win_rate_90 == null ? '—' : `${pct(s.win_rate_90)}（${s.won_90 + s.lost_90}件）`) : pct(s.win_rate_90),
      (s.won_90 + s.lost_90) < 3 ? '' : (s.win_rate_90 < 0.2 ? 'bad' : s.win_rate_90 > 0.8 ? 'warn' : 'good')],
    ['目標に対するパイプライン', s.coverage == null ? '目標未設定' : `${s.coverage}倍`, s.coverage == null ? '' : (s.coverage < 3 ? 'warn' : 'good')],
    ['平均受注日数', s.avg_days_to_win == null ? '—' : `${s.avg_days_to_win}日`],
    ['次の一手が入っている案件', s.next_ratio == null ? '—' : pct(s.next_ratio), s.next_ratio == null ? '' : s.next_ratio < 0.8 ? 'warn' : 'good'],
    ['見積の速さ（ヒアリング→見積提出）', all.quote && all.quote.count ? `${all.quote.median}日` : '—',
      all.quote && all.quote.count ? (all.quote.median > 14 ? 'warn' : 'good') : ''],
  ] : pkey === 'prod' ? [
    ['進行中', `${s.open}件`],
    ['進行中の金額', yen(s.open_amount) || '¥0'],
    ['納期が危ない', `${(pv.leaks.find(l => l.key === 'delivery_risk') || { ids: [] }).ids.length}件`,
      (pv.leaks.find(l => l.key === 'delivery_risk') || { ids: [] }).ids.length ? 'bad' : 'good'],
    ['承認図の返却待ち（10日以上）', `${(pv.leaks.find(l => l.key === 'drawing_wait') || { ids: [] }).ids.length}件`],
    ['完了（直近90日）', `${s.won_90}件`],
  ] : [
    ['進行中', `${s.open}件`],
    ['完了（直近90日）', `${s.won_90}件`],
    ['見送り（直近90日）', `${s.lost_90}件`],
  ];
  const maxCount = Math.max(1, ...pv.stages.map(x => x.count));
  const stageRows = pv.stages.map(st => `
    <tr class="boss-stage-row" data-stage-filter="${esc(st.stage)}" title="この段階の案件を一覧で見る">
      <td><span class="stage-badge" data-stage="${esc(st.stage)}">${esc(st.stage)}</span></td>
      <td class="num"><div class="mini-bar"><div style="width:${st.count * 100 / maxCount}%"></div></div>${st.count}</td>
      ${isSales ? `<td class="num">${st.amount ? yen(st.amount) : ''}</td>
      <td class="num">${st.share == null ? '' : st.share + '%'}</td>
      <td class="num">${st.prob}%</td>
      <td class="num">${st.weighted ? yen(st.weighted) : ''}</td>` : ''}
      <td class="num">${st.avg_days == null ? '' : st.avg_days + '日'}</td>
    </tr>`).join('');
  const momCls = v => v == null ? '' : v < 100 ? 'mom-down' : v <= 150 ? 'mom-up' : 'mom-big';
  const monthRows = pv.months.map(m => `
    <tr><td>${esc(m.month.replace('-', '/'))}</td><td class="num">${m.new}</td><td class="num">${m.won}</td>
      ${isSales ? `<td class="num">${m.won_amount ? yen(m.won_amount) : ''}</td>
      <td class="num"><span class="mom ${momCls(m.won_mom)}">${m.won_mom == null ? '' : m.won_mom + '%'}</span></td>` : ''}
      <td class="num">${m.lost}</td></tr>`).join('');
  const leaks = pv.leaks.map(lk => `
    <div class="leak">
      <button class="leak-head" data-leak="${esc(lk.key)}"><span>${esc(lk.label)}</span><span class="leak-n">${lk.ids.length}</span></button>
      <div class="leak-list" style="${BOSS.open[lk.key] ? '' : 'display:none'}">${lk.ids.map(dealLink).join('')}</div>
    </div>`).join('') || '<div class="boss-empty">抜け漏れはありません</div>';
  const focus = pv.focus.map(f => `
    <div class="focus-row" data-open-deal="${f.id}">
      <div class="focus-main"><span class="deal-no">#${f.id}</span> <b>${esc(f.company || '')}</b> ${esc(f.title)}
        ${f.advice ? `<div class="focus-advice">部長: ${esc(f.advice)}</div>` : ''}</div>
      <span class="stage-badge" data-stage="${esc(f.stage)}">${esc(f.stage)}</span>
      ${isSales ? `<span class="focus-amt">${f.amount ? yen(f.amount) : '金額未入力'}</span>` : ''}
    </div>`).join('') || '<div class="boss-empty">進行中の案件はありません</div>';
  const bars = (obj, cls = '') => {
    const ent = Object.entries(obj || {});
    if (!ent.length) return '<div class="boss-empty">まだありません</div>';
    const mx = Math.max(1, ...ent.map(([, v]) => v));
    return ent.sort((a, b) => b[1] - a[1]).map(([k, v]) => `
      <div class="bar-row"><span>${esc(S.meta.lost_types[k] || k)}</span>
        <div class="bar-track"><div class="bar-fill ${cls}" style="width:${v * 100 / mx}%"></div></div><span class="bar-num">${v}</span></div>`).join('');
  };
  const mix = s.activity_30 || {};
  const ai = all.ai;

  pane.innerHTML = `
    <div class="boss-wrap">
      <div class="boss-hero">
        <div class="boss-hero-head">
          <div><div class="boss-hero-title">部長の目（${esc(pv.label)}${BOSS.owner ? `・${esc(BOSS.owner === '-' ? '担当なし' : BOSS.owner)}` : ''}）
            ${BOSS.owner ? '<button class="btn-link" id="bossAll">全員に戻す</button>' : ''}</div>
            <div class="boss-hero-sub">全案件を毎日チェックしています。抜け漏れをゼロに、案件を前へ。</div></div>
          <div class="boss-hero-actions"><button class="btn-sm" id="bossCsv" title="Excelで開けるCSV">${ico('download', 13)} Excelに書き出す</button>
          ${all.ai_available && !BOSS.owner ? `<button class="btn-sm btn-sm-primary" id="bossTeamAi">${ai ? '今週の3件を更新（AI）' : '今週動く3件をAIに聞く'}</button>` : ''}</div>
        </div>
        ${ai ? `<div class="boss-ai team">
          <div class="boss-ai-head"><span>今週動く3件</span><span class="boss-ai-time">${esc(fmtDateTime(ai.created_at))}</span></div>
          <div class="boss-ai-headline">${esc(ai.headline)}</div>
          ${ai.top3?.length ? `<div class="boss-top3">${ai.top3.map((x, i) => `<button class="boss-top" data-open-deal="${x.id}">
              <span class="boss-top-n">${i + 1}</span><span class="boss-top-b">${dealLink(x.id) ? `<span class="boss-top-d">#${x.id} ${esc((byId[x.id] && ((byId[x.id].card && byId[x.id].card.company) || byId[x.id].company_snapshot)) || '')}</span>` : `<span class="boss-top-d">#${x.id}</span>`}
              <b>${esc(x.action)}</b>${x.why ? `<em>${esc(x.why)}</em>` : ''}</span></button>`).join('')}</div>`
            : ai.points?.length ? `<ul>${ai.points.map(x => `<li>${linkifyDeals(esc(x))}</li>`).join('')}</ul>` : ''}
          ${ai.praise ? `<div class="boss-praise">${esc(ai.praise)}</div>` : ''}
        </div>` : ''}
      </div>

      <div class="stat-tiles boss-tiles">${tiles.map(([k, v, c]) => `
        <div class="stat-tile ${c ? 'tile-' + c : ''}"><div class="stat-tile-label">${esc(k)}</div><div class="stat-tile-value">${esc(v)}</div></div>`).join('')}
      </div>
      ${isSales && s.target == null ? '<div class="boss-hint">月の受注目標を設定すると「パイプラインは目標の何倍か」を判定します（設定 → アラート）</div>' : ''}

      ${ownerBreakdownHtml(all.owners, isSales)}

      ${isSales ? panelBossHtml(all) : ''}
      ${isSales ? vendorBossHtml(all) : ''}

      <div class="boss-grid">
        <section class="boss-card c-violet">
          <div class="section-label"><span>部長からのひとこと</span></div>
          ${adviceItemsHtml(pv.coach)}
        </section>
        <section class="boss-card c-red">
          <div class="section-label"><span>抜け漏れチェック</span></div>
          ${leaks}
        </section>
      </div>

      <section class="boss-card c-blue">
        <div class="section-label"><span>段階別（行をクリックで一覧へ）</span></div>
        <table class="boss-table"><thead><tr><th>段階</th><th>件数</th>
          ${isSales ? '<th>金額</th><th>構成比</th><th>確度</th><th>見込み額</th>' : ''}<th>平均滞在</th></tr></thead>
          <tbody>${stageRows}</tbody></table>
      </section>

      <section class="boss-card c-amber">
        <div class="section-label"><span>重要案件${isSales ? '（見込み額の大きい順）' : ''}</span></div>
        ${focus}
      </section>

      <div class="boss-grid">
        <section class="boss-card c-teal">
          <div class="section-label"><span>月別</span></div>
          <table class="boss-table"><thead><tr><th>月</th><th>新規</th><th>${isSales ? '受注' : '完了'}</th>
            ${isSales ? '<th>受注額</th><th>前月比</th>' : ''}<th>${isSales ? '失注' : pkey === 'prod' ? '中止' : '見送り'}</th></tr></thead>
            <tbody>${monthRows}</tbody></table>
          ${isSales ? '<div class="boss-legend"><span class="mom mom-down">100%未満</span><span class="mom mom-up">100〜150%</span><span class="mom mom-big">150%超</span></div>' : ''}
        </section>
        <section class="boss-card c-green">
          <div class="section-label"><span>この30日の活動</span></div>
          <div class="mix">
            <div><b>${mix.meeting || 0}</b><span>打合せ・訪問</span></div>
            <div><b>${mix.call || 0}</b><span>電話</span></div>
            <div><b>${mix.email || 0}</b><span>メール</span></div>
            <div><b>${mix.memo || 0}</b><span>メモ</span></div>
          </div>
        </section>
      </div>

      ${isSales ? `<div class="boss-grid">
        <section class="boss-card c-red"><div class="section-label"><span>失注はどの段階から</span></div>${bars(pv.lost.from_stage, 'is-lost')}</section>
        <section class="boss-card c-red"><div class="section-label"><span>失注の理由・種類</span></div>${bars(pv.lost.reasons, 'is-lost')}
          <div style="margin-top:10px">${bars(pv.lost.types)}</div></section>
      </div>` : ''}
    </div>`;

  pane.querySelectorAll('[data-open-deal]').forEach(el => el.onclick = () => { setView('deals'); showDeal(Number(el.dataset.openDeal)); });
  pane.querySelectorAll('[data-leak]').forEach(b => b.onclick = () => { BOSS.open[b.dataset.leak] = !BOSS.open[b.dataset.leak]; drawBoss(); });
  pane.querySelectorAll('[data-stage-filter]').forEach(tr => tr.onclick = () => {
    S.filter = tr.dataset.stageFilter; setView('deals'); renderFilters(); renderList();
  });
  pane.querySelectorAll('[data-manual]').forEach(b => b.onclick = () => openManual(b.dataset.manual));
  pane.querySelectorAll('[data-owner-row]').forEach(tr => tr.onclick = () => {
    BOSS.owner = BOSS.owner === tr.dataset.ownerRow ? '' : tr.dataset.ownerRow; renderBoss();
  });
  if ($('bossAll')) $('bossAll').onclick = () => { BOSS.owner = ''; renderBoss(); };
  $('bossCsv').onclick = exportCsv;
  const btn = $('bossTeamAi');
  if (btn) btn.onclick = async () => {
    btn.disabled = true; btn.textContent = '部長が全案件を見ています…';
    try { await api('/api/sales/advisor/team/ai', { method: 'POST' }); await renderBoss(); }
    catch (e) { showToast(e.message, { type: 'error', title: 'AIに失敗' }); btn.disabled = false; btn.textContent = '今週動く3件をAIに聞く'; }
  };
}

// 「#12」を案件へのリンクに
function linkifyDeals(html) {
  return html.replace(/#(\d+)/g, (m, id) => `<button class="boss-inline-deal" data-open-deal="${id}">#${id}</button>`);
}
