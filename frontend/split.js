// 一覧と詳細の幅をドラッグで変えられるようにする（名刺管理・営業の共通）
// <main class="main-grid" data-split="30"> の数字が既定の一覧の幅（%）。ダブルクリックで既定に戻す
(function () {
  function init() {
    const grid = document.querySelector('.main-grid');
    if (!grid || grid.querySelector('.split-handle')) return;
    const list = grid.children[0];
    if (!list) return;
    const key = 'split.' + location.pathname;
    const def = Number(grid.dataset.split) || 43;
    let pct = def;
    try { pct = Number(localStorage.getItem(key)) || def; } catch { /* 保存できない環境 */ }
    const clamp = v => Math.min(65, Math.max(18, v));
    const apply = () => grid.style.setProperty('--split', clamp(pct) + '%');
    const save = () => { try { localStorage.setItem(key, String(Math.round(clamp(pct)))); } catch { /* 無視 */ } };
    const h = document.createElement('div');
    h.className = 'split-handle';
    h.title = 'ドラッグで幅を変えられます（ダブルクリックで元に戻す）';
    h.setAttribute('role', 'separator');
    h.setAttribute('aria-orientation', 'vertical');
    h.tabIndex = 0;
    list.after(h);
    grid.classList.add('has-split');
    apply();
    h.addEventListener('pointerdown', e => {
      e.preventDefault();
      h.setPointerCapture(e.pointerId);
      document.body.classList.add('is-splitting');
      const rect = grid.getBoundingClientRect();
      const move = ev => { pct = (ev.clientX - rect.left) * 100 / rect.width; apply(); };
      const up = () => {
        h.removeEventListener('pointermove', move);
        h.removeEventListener('pointerup', up);
        document.body.classList.remove('is-splitting');
        save();
      };
      h.addEventListener('pointermove', move);
      h.addEventListener('pointerup', up);
    });
    h.addEventListener('dblclick', () => { pct = def; apply(); save(); });
    h.addEventListener('keydown', e => {
      if (e.key === 'ArrowLeft') { pct = clamp(pct - 2); apply(); save(); }
      if (e.key === 'ArrowRight') { pct = clamp(pct + 2); apply(); save(); }
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
