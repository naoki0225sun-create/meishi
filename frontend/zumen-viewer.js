// ============================================================
// 図面の表示（拡大・移動・回転・ページ送り）と、改訂前後の差分
// サーバーが作ったページ画像を使う（PDF.js は使わない）。拡大すると高解像度の画像に差し替える
// ============================================================

const MK_SHOW_DEFAULT = true;
const MK_COLOR = { red: '#D42828', blue: '#2556E8', green: '#18804A', black: '#172033', orange: '#C2560C' };
const ZV = {
  pageUrl: (fileId, page, size = 'view') => `/api/zumen/files/${fileId}/page/${page}?size=${size}`,
  rawUrl: fileId => `/api/zumen/files/${fileId}/raw`,
};

// <div class="zv"> を作って返す。opts: {fileId, pages, page, pageFrom, pageTo, stamp, onPage}
function zvCreate(host, opts) {
  const st = {
    fileId: opts.fileId, page: opts.page || opts.pageFrom || 1, from: opts.pageFrom || 1, to: opts.pageTo || opts.pages || 1,
    scale: 1, x: 0, y: 0, rot: 0, hi: false, fit: 1, w: 0, h: 0,
  };
  host.innerHTML = `
    <div class="zv ${opts.compact ? 'zv-compact' : ''}">
      <div class="zv-bar">
        <div class="zv-pages">${st.to > st.from ? `<button class="zv-btn" data-zv="prev" title="前のページ">‹</button>
          <span class="zv-pn"></span><button class="zv-btn" data-zv="next" title="次のページ">›</button>` : '<span class="zv-pn"></span>'}</div>
        <div class="zv-tools">
          <button class="zv-btn" data-zv="out" title="縮小（-）">−</button>
          <button class="zv-btn zv-pct" data-zv="fit" title="全体を表示（0）">100%</button>
          <button class="zv-btn" data-zv="in" title="拡大（+）">＋</button>
          <button class="zv-btn" data-zv="rot" title="回転（R）">${ico('rot', 14)}</button>
          ${opts.big !== false ? `<button class="zv-btn" data-zv="big" title="大きく見る">${ico('expand', 14)}</button>` : ''}
          ${opts.raw !== false ? `<a class="zv-btn" href="${ZV.rawUrl(st.fileId)}" target="_blank" rel="noopener" title="原本を開く・保存">${ico('download', 14)}</a>` : ''}
        </div>
      </div>
      ${opts.annotate ? `<div class="zv-anno">
        <button class="zv-tool" data-tool="stamp" title="スタンプ（押したい場所をクリック）">${ico('stamp', 14)}<span>スタンプ</span></button>
        <select class="zv-stamp-sel" title="スタンプの文字">${(ZV.stamps || ['検図済', '承認', '確認済', '旧版', '社外秘', '製作OK', '保留', '要確認']).map(x => `<option>${x}</option>`).join('')}</select>
        <button class="zv-tool" data-tool="text" title="文字">${ico('type', 14)}<span>文字</span></button>
        <button class="zv-tool" data-tool="pen" title="手書き">${ico('note', 14)}<span>ペン</span></button>
        <button class="zv-tool" data-tool="rect" title="囲む">${ico('square', 14)}<span>囲む</span></button>
        <button class="zv-tool" data-tool="arrow" title="矢印">${ico('arrow', 14)}<span>矢印</span></button>
        <button class="zv-tool" data-tool="erase" title="消す（書き込みをクリック）">${ico('eraser', 14)}<span>消す</span></button>
        <span class="zv-colors">${['red', 'blue', 'green', 'black'].map(c => `<button class="zv-color c-${c}" data-color="${c}" title="色"></button>`).join('')}</span>
        <label class="zv-show"><input type="checkbox" ${MK_SHOW_DEFAULT ? 'checked' : ''} data-show-marks> 書き込みを表示</label>
      </div>` : ''}
      <div class="zv-stage" tabindex="0">
        ${opts.stamp ? `<div class="zv-stamp">${esc(opts.stamp)}</div>` : ''}
        <div class="zv-layer"><img class="zv-img" alt="" draggable="false"><svg class="zv-marks" xmlns="http://www.w3.org/2000/svg"></svg></div>
        <div class="zv-loading">読み込み中…</div>
      </div>
    </div>`;
  const root = host.querySelector('.zv');
  const stage = root.querySelector('.zv-stage');
  const img = root.querySelector('.zv-img');
  const layer = root.querySelector('.zv-layer');
  const svg = root.querySelector('.zv-marks');
  const MK = { list: [], show: opts.marks !== false, tool: null, color: 'red', stamp: '検図済', draft: null };
  const pct = root.querySelector('.zv-pct');

  function apply() {
    layer.style.transform = `translate(${st.x}px, ${st.y}px) scale(${st.scale}) rotate(${st.rot}deg)`;
    pct.textContent = Math.round(st.scale / st.fit * 100) + '%';
    if (!st.hi && st.scale / st.fit > 1.6) {
      st.hi = true;
      const u = ZV.pageUrl(st.fileId, st.page, 'hi');
      const pre = new Image();
      pre.onload = () => { if (st.hi) { img.src = u; } };
      pre.src = u;
    }
  }
  function fit() {
    const r = stage.getBoundingClientRect();
    if (!st.w || !r.width) return;
    const rotated = st.rot % 180 !== 0;
    const w = rotated ? st.h : st.w, h = rotated ? st.w : st.h;
    st.fit = Math.min(r.width / w, r.height / h) * 0.98;
    st.scale = st.fit;
    st.x = (r.width - st.w * st.scale) / 2;
    st.y = (r.height - st.h * st.scale) / 2;
    apply();
  }
  function load() {
    root.querySelector('.zv-pn').textContent = st.to > st.from ? `${st.page} / ${st.to}` : (opts.pages > 1 ? `${st.page}ページ` : '');
    root.querySelector('.zv-loading').style.display = '';
    st.hi = false;
    img.onload = () => {
      root.querySelector('.zv-loading').style.display = 'none';
      if (!st.w || img.dataset.page !== String(st.page)) {
        st.w = img.naturalWidth; st.h = img.naturalHeight; img.dataset.page = String(st.page);
        layer.style.width = st.w + 'px'; layer.style.height = st.h + 'px';
        svg.setAttribute('viewBox', `0 0 ${st.w} ${st.h}`);
        fit();
        loadMarks();
      }
    };
    img.onerror = () => { root.querySelector('.zv-loading').textContent = 'この形式（DWG・STEPなど）は画面に表示できません。右上のボタンから原本を開いてください'; };
    st.w = 0;
    img.src = ZV.pageUrl(st.fileId, st.page, 'view');
    if (opts.onPage) opts.onPage(st.page);
  }
  function zoomAt(f, cx, cy) {
    const ns = Math.min(Math.max(st.scale * f, st.fit * 0.5), st.fit * 12);
    const k = ns / st.scale;
    st.x = cx - (cx - st.x) * k; st.y = cy - (cy - st.y) * k; st.scale = ns; apply();
  }
  function center(f) { const r = stage.getBoundingClientRect(); zoomAt(f, r.width / 2, r.height / 2); }

  root.querySelectorAll('[data-zv]').forEach(b => b.onclick = () => {
    const a = b.dataset.zv;
    if (a === 'in') center(1.4);
    if (a === 'out') center(1 / 1.4);
    if (a === 'fit') fit();
    if (a === 'rot') { st.rot = (st.rot + 90) % 360; layer.style.transformOrigin = 'center center'; fit(); }
    if (a === 'prev' && st.page > st.from) { st.page--; load(); }
    if (a === 'next' && st.page < st.to) { st.page++; load(); }
    if (a === 'big') zvBig({ ...opts, page: st.page });
  });
  stage.addEventListener('wheel', e => {
    e.preventDefault();
    const r = stage.getBoundingClientRect();
    zoomAt(e.deltaY < 0 ? 1.15 : 1 / 1.15, e.clientX - r.left, e.clientY - r.top);
  }, { passive: false });
  // ドラッグで移動・2本指で拡大
  const pts = new Map();
  let last = null, pinch = null;
  const toImg = e => {
    const r = stage.getBoundingClientRect();
    return [((e.clientX - r.left - st.x) / st.scale) / st.w, ((e.clientY - r.top - st.y) / st.scale) / st.h];
  };
  stage.addEventListener('pointerdown', e => {
    if (MK.tool && MK.tool !== 'erase' && e.isPrimary && (e.button === 0 || e.pointerType !== 'mouse')) {
      e.preventDefault(); stage.setPointerCapture(e.pointerId);
      const [x, y] = toImg(e);
      if (MK.tool === 'stamp') { saveMark('stamp', { x, y, text: MK.stamp, color: MK.color }); return; }
      if (MK.tool === 'text') {
        const t = prompt('書き込む文字を入力してください'); if (t && t.trim()) saveMark('text', { x, y, text: t.trim(), color: MK.color }); return;
      }
      MK.draft = { kind: MK.tool, x, y, x2: x, y2: y, points: [[x, y]] };
      drawMarks(); return;
    }
    stage.setPointerCapture(e.pointerId); pts.set(e.pointerId, [e.clientX, e.clientY]); last = [e.clientX, e.clientY]; if (pts.size === 2) pinch = null;
  });
  stage.addEventListener('pointermove', e => {
    if (MK.draft) {
      const [x, y] = toImg(e);
      MK.draft.x2 = x; MK.draft.y2 = y;
      if (MK.draft.kind === 'pen') MK.draft.points.push([Number(x.toFixed(4)), Number(y.toFixed(4))]);
      drawMarks(); return;
    }
    if (!pts.has(e.pointerId)) return;
    pts.set(e.pointerId, [e.clientX, e.clientY]);
    if (pts.size === 2) {
      const [a, b] = [...pts.values()];
      const d = Math.hypot(a[0] - b[0], a[1] - b[1]);
      const r = stage.getBoundingClientRect();
      if (pinch) zoomAt(d / pinch, (a[0] + b[0]) / 2 - r.left, (a[1] + b[1]) / 2 - r.top);
      pinch = d;
      return;
    }
    st.x += e.clientX - last[0]; st.y += e.clientY - last[1]; last = [e.clientX, e.clientY]; apply();
  });
  const up = e => {
    if (MK.draft) {
      const d = MK.draft; MK.draft = null;
      if (d.kind === 'pen' && d.points.length > 2) saveMark('pen', { points: d.points, color: MK.color });
      else if (d.kind === 'rect' && Math.abs(d.x2 - d.x) > 0.005) saveMark('rect', { x: Math.min(d.x, d.x2), y: Math.min(d.y, d.y2), w: Math.abs(d.x2 - d.x), h: Math.abs(d.y2 - d.y), color: MK.color });
      else if (d.kind === 'arrow' && Math.hypot(d.x2 - d.x, d.y2 - d.y) > 0.01) saveMark('arrow', { x: d.x, y: d.y, x2: d.x2, y2: d.y2, color: MK.color });
      else drawMarks();
      return;
    }
    pts.delete(e.pointerId); pinch = null; if (pts.size === 1) last = [...pts.values()][0];
  };
  stage.addEventListener('pointerup', up); stage.addEventListener('pointercancel', up);
  stage.addEventListener('dblclick', e => {
    const r = stage.getBoundingClientRect();
    if (st.scale / st.fit > 1.5) fit(); else zoomAt(2.5, e.clientX - r.left, e.clientY - r.top);
  });
  stage.addEventListener('keydown', e => {
    if (e.key === '+' || e.key === '=') center(1.4);
    if (e.key === '-') center(1 / 1.4);
    if (e.key === '0') fit();
    if (e.key === 'r' || e.key === 'R') { st.rot = (st.rot + 90) % 360; fit(); }
    if (e.key === 'ArrowRight' && st.page < st.to) { st.page++; load(); }
    if (e.key === 'ArrowLeft' && st.page > st.from) { st.page--; load(); }
  });
  async function loadMarks() {
    if (!MK.show || opts.marks === false) { svg.innerHTML = ''; return; }
    try {
      const r = await api(`/api/zumen/files/${st.fileId}/marks?page=${st.page}`);
      MK.list = r.marks; ZV.stamps = r.stamps;
    } catch { MK.list = []; }
    drawMarks();
  }
  function drawMarks() {
    svg.innerHTML = MK.show ? zvMarksSvg(MK.list.concat(MK.draft ? [{ id: 0, kind: MK.draft.kind, data: { ...MK.draft, w: Math.abs(MK.draft.x2 - MK.draft.x), h: Math.abs(MK.draft.y2 - MK.draft.y), x: Math.min(MK.draft.x, MK.draft.kind === 'rect' ? MK.draft.x2 : MK.draft.x), y: Math.min(MK.draft.y, MK.draft.kind === 'rect' ? MK.draft.y2 : MK.draft.y), color: MK.color } }] : []), st.w, st.h) : '';
    svg.classList.toggle('is-erasing', MK.tool === 'erase');
    svg.querySelectorAll('[data-mark]').forEach(g => g.onclick = async ev => {
      if (MK.tool !== 'erase') return;
      ev.stopPropagation();
      const id = Number(g.dataset.mark);
      await api(`/api/zumen/marks/${id}`, { method: 'DELETE' });
      showToast('書き込みを消しました', { action: { label: '元に戻す', onClick: async () => { await api(`/api/zumen/marks/${id}/restore`, { method: 'POST' }); loadMarks(); } } });
      loadMarks();
    });
  }
  async function saveMark(kind, data) {
    try {
      await api(`/api/zumen/files/${st.fileId}/marks`, { method: 'POST', body: JSON.stringify({ page: st.page, kind, data, author: (window.Z && Z.me) || null }) });
      loadMarks();
    } catch (err) { showToast(err.message, { type: 'error' }); }
  }
  root.querySelectorAll('[data-tool]').forEach(b => b.onclick = () => {
    MK.tool = MK.tool === b.dataset.tool ? null : b.dataset.tool;
    if (MK.tool && st.rot) { st.rot = 0; fit(); }
    root.querySelectorAll('[data-tool]').forEach(x => x.classList.toggle('is-on', x.dataset.tool === MK.tool));
    stage.classList.toggle('is-drawing', !!MK.tool && MK.tool !== 'erase');
    drawMarks();
  });
  root.querySelectorAll('[data-color]').forEach(b => b.onclick = () => {
    MK.color = b.dataset.color; root.querySelectorAll('[data-color]').forEach(x => x.classList.toggle('is-on', x === b));
  });
  const ss = root.querySelector('.zv-stamp-sel');
  if (ss) ss.onchange = () => { MK.stamp = ss.value; if (MK.tool !== 'stamp') root.querySelector('[data-tool="stamp"]').click(); };
  const sm = root.querySelector('[data-show-marks]');
  if (sm) sm.onchange = () => { MK.show = sm.checked; loadMarks(); };
  root.querySelector('[data-color="red"]')?.classList.add('is-on');

  const ro = new ResizeObserver(() => { if (st.scale === st.fit || !st.scale) fit(); });
  ro.observe(stage);
  load();
  return { root, goto: p => { st.page = p; load(); }, fit, state: st, reloadMarks: loadMarks };
}

// 書き込み → SVG の中身（座標は画像に対する割合 0〜1。w・h は画像の画素数）
function zvMarksSvg(list, w, h) {
  const u = Math.max(w, h) / 1000;
  const esc2 = v => String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  return `<defs>${Object.entries(MK_COLOR).map(([k, c]) => `<marker id="zva-${k}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto"><path d="M0 0L10 5L0 10z" fill="${c}"/></marker>`).join('')}</defs>` + list.map(m => {
    const d = m.data, c = MK_COLOR[d.color] || MK_COLOR.red, k = d.size || 1;
    const X = v => (v * w).toFixed(1), Y = v => (v * h).toFixed(1);
    let body = '';
    if (m.kind === 'stamp') {
      const fs = 26 * u * k, bw = (String(d.text).length * fs * 1.05 + fs * 1.2), bh = fs * 2.4;
      const day = (m.created_at || '').slice(0, 10).replace(/-/g, '.');
      body = `<g transform="translate(${X(d.x)} ${Y(d.y)})"><rect x="${-bw / 2}" y="${-bh / 2}" width="${bw}" height="${bh}" rx="${6 * u}" fill="rgba(255,255,255,.75)" stroke="${c}" stroke-width="${3.2 * u}"/>
        <text x="0" y="${-fs * 0.05}" text-anchor="middle" font-size="${fs}" font-weight="700" fill="${c}">${esc2(d.text)}</text>
        <text x="0" y="${fs * 0.95}" text-anchor="middle" font-size="${fs * 0.45}" fill="${c}">${esc2(day)} ${esc2(m.author || '')}</text></g>`;
    } else if (m.kind === 'text') {
      body = `<text x="${X(d.x)}" y="${Y(d.y)}" font-size="${22 * u * k}" font-weight="600" fill="${c}" paint-order="stroke" stroke="#fff" stroke-width="${4 * u}">${esc2(d.text)}</text>`;
    } else if (m.kind === 'pen') {
      body = `<polyline points="${(d.points || []).map(p => `${X(p[0])},${Y(p[1])}`).join(' ')}" fill="none" stroke="${c}" stroke-width="${3 * u * k}" stroke-linecap="round" stroke-linejoin="round"/>`;
    } else if (m.kind === 'rect') {
      body = `<rect x="${X(d.x)}" y="${Y(d.y)}" width="${X(d.w)}" height="${Y(d.h)}" fill="none" stroke="${c}" stroke-width="${3 * u * k}" rx="${4 * u}"/>`;
    } else if (m.kind === 'arrow') {
      body = `<line x1="${X(d.x)}" y1="${Y(d.y)}" x2="${X(d.x2)}" y2="${Y(d.y2)}" stroke="${c}" stroke-width="${3.2 * u * k}" marker-end="url(#zva-${d.color || 'red'})"/>`;
    }
    return `<g class="zv-mark" data-mark="${m.id}">${body}</g>`;
  }).join('');
}

function zvBig(opts) {
  const box = $('zmModalBox');
  $('zmModal').style.display = 'flex';
  box.className = 'zm-modal-box zm-modal-big';
  box.innerHTML = `<div class="zm-modal-head"><b>${esc(opts.title || '図面')}</b><button class="dupe-modal-close" id="zmModalClose" aria-label="閉じる">×</button></div><div class="zm-modal-body" id="zmBigHost"></div>`;
  $('zmModalClose').onclick = zmCloseModal;
  zvCreate($('zmBigHost'), { ...opts, big: false });
}

function zmCloseModal() { $('zmModal').style.display = 'none'; $('zmModalBox').innerHTML = ''; }
document.addEventListener('keydown', e => { if (e.key === 'Escape' && $('zmModal') && $('zmModal').style.display !== 'none') zmCloseModal(); });

// ── 差分（旧版を赤・新版を青・同じ線を薄い灰で重ねる） ──
function loadImg(src) {
  return new Promise((res, rej) => { const i = new Image(); i.onload = () => res(i); i.onerror = rej; i.src = src; });
}

async function zvDiff(older, newer) {
  // older/newer: {fileId, page, label}
  const box = $('zmModalBox');
  $('zmModal').style.display = 'flex';
  box.className = 'zm-modal-box zm-modal-big';
  box.innerHTML = `<div class="zm-modal-head"><b>版の比較　<span class="zd-old">${esc(older.label)}</span> → <span class="zd-new">${esc(newer.label)}</span></b>
      <div class="zd-tools">
        <div class="seg" id="zdMode"><button data-m="overlay" class="is-on">重ねて表示</button><button data-m="side">左右に並べる</button><button data-m="slider">スライドで比較</button></div>
        <button class="btn-sm" id="zdAlign" title="新旧の線がいちばん重なる位置を探します">位置を自動で合わせる</button>
        <span class="zd-hint">矢印キーで位置を微調整できます（Shiftを押しながらで大きく移動）<b id="zdOff"></b></span>
        <button class="dupe-modal-close" id="zmModalClose" aria-label="閉じる">×</button></div></div>
      <div class="zm-modal-body zd-body" id="zdBody"><div class="zv-loading">画像を重ねています…</div></div>
      <div class="zd-legend"><span class="zd-sw red"></span>旧版にだけある線（削除された部分）<span class="zd-sw blue"></span>新版にだけある線（追加された部分）<span class="zd-sw gray"></span>変更のない線</div>`;
  $('zmModalClose').onclick = zmCloseModal;
  let a, b;
  try {
    [a, b] = await Promise.all([loadImg(ZV.pageUrl(older.fileId, older.page)), loadImg(ZV.pageUrl(newer.fileId, newer.page))]);
  } catch { $('zdBody').innerHTML = '<div class="boss-empty">画像を読み込めませんでした</div>'; return; }
  const W = Math.max(a.width, b.width), H = Math.max(a.height, b.height);
  const gray = (img, dx = 0, dy = 0) => {
    const c = document.createElement('canvas'); c.width = W; c.height = H;
    const x = c.getContext('2d'); x.fillStyle = '#fff'; x.fillRect(0, 0, W, H);
    x.drawImage(img, dx + (W - img.width) / 2, dy + (H - img.height) / 2);
    return x.getImageData(0, 0, W, H).data;
  };
  const ga = gray(a);
  let off = [0, 0], mode = 'overlay';
  const canvas = document.createElement('canvas'); canvas.width = W; canvas.height = H; canvas.className = 'zd-canvas';
  function render() {
    const gb = gray(b, off[0], off[1]);
    const ctx = canvas.getContext('2d');
    const out = ctx.createImageData(W, H);
    const o = out.data;
    for (let i = 0; i < o.length; i += 4) {
      const da = (ga[i] + ga[i + 1] + ga[i + 2]) / 3 < 160, db = (gb[i] + gb[i + 1] + gb[i + 2]) / 3 < 160;
      if (da && db) { o[i] = 170; o[i + 1] = 176; o[i + 2] = 186; }
      else if (da) { o[i] = 214; o[i + 1] = 40; o[i + 2] = 40; }
      else if (db) { o[i] = 37; o[i + 1] = 86; o[i + 2] = 232; }
      else { o[i] = 255; o[i + 1] = 255; o[i + 2] = 255; }
      o[i + 3] = 255;
    }
    ctx.putImageData(out, 0, 0);
  }
  function show() {
    const body = $('zdBody');
    if (mode === 'overlay') {
      body.innerHTML = ''; const h = document.createElement('div'); h.className = 'zd-scroll'; h.appendChild(canvas); body.appendChild(h); render();
    } else if (mode === 'side') {
      body.innerHTML = `<div class="zd-side"><figure><figcaption class="zd-old">${esc(older.label)}</figcaption><img src="${ZV.pageUrl(older.fileId, older.page)}"></figure>
        <figure><figcaption class="zd-new">${esc(newer.label)}</figcaption><img src="${ZV.pageUrl(newer.fileId, newer.page)}"></figure></div>`;
    } else {
      body.innerHTML = `<div class="zd-slider"><div class="zd-sl-wrap"><img src="${ZV.pageUrl(older.fileId, older.page)}"><img class="zd-top" src="${ZV.pageUrl(newer.fileId, newer.page)}"></div>
        <input type="range" min="0" max="100" value="50" id="zdRange"></div>`;
      const top = body.querySelector('.zd-top');
      const set = v => { top.style.clipPath = `inset(0 0 0 ${v}%)`; };
      set(50); $('zdRange').oninput = e => set(e.target.value);
    }
  }
  $('zdMode').querySelectorAll('button').forEach(btn => btn.onclick = () => {
    mode = btn.dataset.m; $('zdMode').querySelectorAll('button').forEach(x => x.classList.toggle('is-on', x === btn)); show();
  });
  const showOff = () => { const el = $('zdOff'); if (el) el.textContent = off[0] || off[1] ? `　ずれ：横 ${off[0]}・縦 ${off[1]}` : ''; };
  const onKey = e => {
    if ($('zmModal').style.display === 'none') { document.removeEventListener('keydown', onKey); return; }
    if (mode !== 'overlay') return;
    const s = e.shiftKey ? 10 : 1;
    const m = { ArrowLeft: [-s, 0], ArrowRight: [s, 0], ArrowUp: [0, -s], ArrowDown: [0, s] }[e.key];
    if (m) { e.preventDefault(); off = [off[0] + m[0], off[1] + m[1]]; render(); showOff(); }
  };
  document.addEventListener('keydown', onKey);
  $('zdAlign').onclick = () => {
    const btn = $('zdAlign'); btn.disabled = true; btn.textContent = '位置を合わせています…';
    setTimeout(() => {
      off = zvAlign(a, b, W, H);
      if (mode !== 'overlay') { mode = 'overlay'; $('zdMode').querySelectorAll('button').forEach(x => x.classList.toggle('is-on', x.dataset.m === 'overlay')); show(); } else render();
      showOff(); btn.disabled = false; btn.textContent = '位置を自動で合わせる';
    }, 30);
  };
  show();
  // スキャンの図面はずれていることが多いので、開いたときに一度だけ自動で合わせる
  setTimeout(() => { const o = zvAlign(a, b, W, H); if (o[0] || o[1]) { off = o; if (mode === 'overlay') render(); showOff(); } }, 60);
}

// 新旧の黒い線がいちばん重なる平行移動を探す（縮小して粗く探し、元の大きさで細かく詰める）
function zvAlign(a, b, W, H, range = 40) {
  const mask = (img, w, h) => {
    const c = document.createElement('canvas'); c.width = w; c.height = h;
    const x = c.getContext('2d'); x.fillStyle = '#fff'; x.fillRect(0, 0, w, h);
    const k = w / W;
    x.drawImage(img, (W - img.width) / 2 * k, (H - img.height) / 2 * k, img.width * k, img.height * k);
    const d = x.getImageData(0, 0, w, h).data, m = new Uint8Array(w * h);
    for (let i = 0, j = 0; i < d.length; i += 4, j++) m[j] = (d[i] + d[i + 1] + d[i + 2]) < 480 ? 1 : 0;
    return m;
  };
  const score = (ma, mb, w, h, dx, dy, step) => {
    let n = 0;
    for (let y = Math.max(0, dy); y < Math.min(h, h + dy); y += step) {
      const ra = y * w, rb = (y - dy) * w;
      for (let x = Math.max(0, dx); x < Math.min(w, w + dx); x += step) if (ma[ra + x] && mb[rb + x - dx]) n++;
    }
    return n;
  };
  const search = (k, cx, cy, r, st) => {
    const w = Math.max(1, Math.round(W * k)), h = Math.max(1, Math.round(H * k));
    const ma = mask(a, w, h), mb = mask(b, w, h);
    let best = [cx, cy], bs = -1;
    for (let dy = cy - r; dy <= cy + r; dy++) for (let dx = cx - r; dx <= cx + r; dx++) {
      const sc = score(ma, mb, w, h, dx, dy, st);
      if (sc > bs) { bs = sc; best = [dx, dy]; }
    }
    return best;
  };
  const k1 = Math.min(1, 320 / W);
  const c1 = search(k1, 0, 0, Math.ceil(range * k1), 1);
  const k2 = Math.min(1, 900 / W);
  const c2 = search(k2, Math.round(c1[0] / k1 * k2), Math.round(c1[1] / k1 * k2), 3, 2);
  return [Math.round(c2[0] / k2), Math.round(c2[1] / k2)];
}
