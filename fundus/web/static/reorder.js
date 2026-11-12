// Übersicht: Suchen per Langdruck auf die Infokarte und Ziehen sortieren (Handy: ~0,45 s halten, Computer: Maus halten).
// Kurzes Tippen öffnet weiterhin die Suche, Wischen scrollt wie gewohnt. Reihenfolge → POST /searches/order.
(function () {
  const list = document.querySelector('.ov');
  if (!list) return;
  const HOLD_MS = 450, SLOP = 8, EDGE = 90;
  let timer = null, startX = 0, startY = 0, drag = null, suppressClick = false;

  const rows = () => [...list.querySelectorAll('.ov-strip[data-id]')];

  function lift(row, y) {
    drag = {row, startY: y, y};
    row.classList.add('lifted');
    document.body.classList.add('reordering');
    if (navigator.vibrate) navigator.vibrate(25);
  }

  function move(y) {
    if (!drag) return;
    drag.y = y;
    const row = drag.row;
    row.style.transform = `translateY(${y - drag.startY}px)`;
    const prev = row.previousElementSibling, next = row.nextElementSibling;
    const swap = (target, before) => {
      const top = row.getBoundingClientRect().top - (y - drag.startY);
      list.insertBefore(row, before ? target : target.nextElementSibling);
      row.style.transform = '';
      drag.startY += row.getBoundingClientRect().top - top;
      row.style.transform = `translateY(${y - drag.startY}px)`;
    };
    if (prev && y < prev.getBoundingClientRect().top + prev.offsetHeight / 2) swap(prev, true);
    else if (next && y > next.getBoundingClientRect().top + next.offsetHeight / 2) swap(next, false);
  }

  function autoscroll() {  // am Rand mitscrollen, solange gezogen wird
    if (!drag) return;
    const dy = drag.y < EDGE ? -8 : drag.y > innerHeight - EDGE - 60 ? 8 : 0;
    if (dy) { scrollBy(0, dy); drag.startY -= dy; move(drag.y); }
    requestAnimationFrame(autoscroll);
  }

  async function drop() {
    clearTimeout(timer); timer = null;
    if (!drag) return;
    const row = drag.row;
    row.classList.remove('lifted'); row.style.transform = '';
    document.body.classList.remove('reordering');
    drag = null;
    suppressClick = true; setTimeout(() => { suppressClick = false; }, 350);
    try {
      await fetch('/searches/order', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                       body: JSON.stringify({ids: rows().map(r => +r.dataset.id)})});
    } catch (e) { /* Mac kurz weg: Reihenfolge gilt bis zum Neuladen */ }
  }

  function begin(e, x, y) {
    const row = e.target.closest('.ov-strip[data-id]');
    if (!row || !e.target.closest('.ov-info')) return;
    startX = x; startY = y;
    timer = setTimeout(() => { timer = null; lift(row, startY); requestAnimationFrame(autoscroll); }, HOLD_MS);
  }

  // Touch: erst nach dem Halten das Scrollen sperren (passive: false), vorher normal scrollen lassen
  list.addEventListener('touchstart', e => begin(e, e.touches[0].clientX, e.touches[0].clientY), {passive: true});
  // Nur auf den Infokarten blockierend – sonst müsste jedes Scrollen der Übersicht auf dieses Skript warten (ruckelt)
  const onMove = e => {
    const t = e.touches[0];
    if (drag) { e.preventDefault(); move(t.clientY); return; }
    if (timer && Math.hypot(t.clientX - startX, t.clientY - startY) > SLOP) { clearTimeout(timer); timer = null; }
  };
  list.querySelectorAll('.ov-info').forEach(n => n.addEventListener('touchmove', onMove, {passive: false}));
  list.addEventListener('touchmove', e => { if (!e.target.closest('.ov-info')) onMove(e); }, {passive: true});
  list.addEventListener('touchend', drop);
  list.addEventListener('touchcancel', drop);

  // Maus
  list.addEventListener('mousedown', e => { if (e.button === 0) begin(e, e.clientX, e.clientY); });
  addEventListener('mousemove', e => {
    if (drag) { e.preventDefault(); move(e.clientY); }
    else if (timer && Math.hypot(e.clientX - startX, e.clientY - startY) > SLOP) { clearTimeout(timer); timer = null; }
  });
  addEventListener('mouseup', drop);

  // Nach dem Ziehen keinen Klick auslösen, kein Kontextmenü beim Halten
  list.addEventListener('click', e => { if (suppressClick) { e.preventDefault(); e.stopPropagation(); } }, true);
  list.addEventListener('contextmenu', e => { if (e.target.closest('.ov-info')) e.preventDefault(); });
  list.addEventListener('dragstart', e => { if (e.target.closest('.ov-info')) e.preventDefault(); });
})();
