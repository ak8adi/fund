// Übersicht ordnen und Gruppen pflegen.
//  · «Ordnen»: Suchen werden zu schmalen Zeilen; am Griff ≡ sofort ziehen. Über eine Gruppe ziehen = hinein,
//    über ihr Ende hinaus = heraus; Griff am Gruppenkopf = ganze Gruppe verschieben. «Fertig» beendet.
//  · Ohne «Ordnen» geht es weiterhin per Langdruck auf die Infokarte (~0,45 s).
//  · Gruppe anlegen/bearbeiten: Blatt mit Name und Auswahl der Suchen (POST /groups), danach neu laden.
// Reihenfolge und Gruppen nach dem Ziehen → POST /searches/order.
(function () {
  const list = document.querySelector('.ov');
  if (!list) return;
  const HOLD_MS = 450, SLOP = 8, EDGE = 90;
  const ITEM = '.ov-strip[data-id], .ov-grp';
  const body = document.body;
  let timer = null, startX = 0, startY = 0, drag = null, suppressClick = false;

  const ordering = () => body.classList.contains('ordering');
  const handleOf = t => t.closest && (ordering() ? t.closest('.handle') : t.closest('.ov-info, .ov-grp-head'));
  const strips = () => [...list.querySelectorAll('.ov-strip[data-id]')];
  const markEmpty = () => list.querySelectorAll('.ov-grp').forEach(g => g.classList.toggle('is-empty', !g.querySelector('.ov-strip')));
  const sib = (el, dir) => { let n = el[dir]; while (n && !n.matches(ITEM)) n = n[dir]; return n; };
  const post = (url, data) => fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});

  // ---------- Ziehen ----------
  function lift(item, y) {
    drag = {row: item, startY: y, y};
    item.classList.add('lifted');
    body.classList.add('reordering');
    if (navigator.vibrate) navigator.vibrate(20);
    requestAnimationFrame(autoscroll);
  }

  // DOM umstellen, ohne dass die gezogene Zeile unter dem Finger springt
  function place(fn) {
    const row = drag.row;
    const top = row.getBoundingClientRect().top - (drag.y - drag.startY);
    fn();
    row.style.transform = '';
    drag.startY += row.getBoundingClientRect().top - top;
    row.style.transform = `translateY(${drag.y - drag.startY}px)`;
    markEmpty();
  }

  function move(y) {
    if (!drag) return;
    drag.y = y;
    const row = drag.row;
    row.style.transform = `translateY(${y - drag.startY}px)`;
    if (row.matches('.ov-strip')) {
      // Gruppe unter dem Finger = dort hinein; ausserhalb jeder Gruppe = oberste Ebene
      const grp = [...list.querySelectorAll('.ov-grp')].find(g => { const r = g.getBoundingClientRect(); return y >= r.top && y <= r.bottom; });
      const home = grp || list;
      if (row.parentElement !== home) {
        const kids = [...home.children].filter(n => n !== row && n.matches(grp ? '.ov-strip' : ITEM));
        const before = kids.find(n => { const r = n.getBoundingClientRect(); return y < r.top + r.height / 2; });
        place(() => home.insertBefore(row, before || null));
        return;
      }
    }
    const prev = sib(row, 'previousElementSibling'), next = sib(row, 'nextElementSibling');
    if (prev && y < prev.getBoundingClientRect().top + prev.offsetHeight / 2) place(() => row.parentElement.insertBefore(row, prev));
    else if (next && y > next.getBoundingClientRect().top + next.offsetHeight / 2) place(() => row.parentElement.insertBefore(row, next.nextSibling));
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
    body.classList.remove('reordering');
    drag = null;
    suppressClick = true; setTimeout(() => { suppressClick = false; }, 350);
    // Gruppen kommen nur aus Namen, die der Mac selbst gesetzt hat (data-group) – nie aus halb getippten
    const groups = {};
    strips().forEach(r => { const g = r.closest('.ov-grp'); groups[r.dataset.id] = g ? g.dataset.group : ''; });
    try { await post('/searches/order', {ids: strips().map(r => +r.dataset.id), groups}); }
    catch (e) { /* Mac kurz weg: gilt bis zum Neuladen */ }
  }

  function begin(e, x, y) {
    const handle = handleOf(e.target);
    if (!handle || !list.contains(handle)) return;
    if (!ordering() && e.target.closest('button, input, a.gear')) return;
    const item = handle.closest('.ov-grp-head') ? handle.closest('.ov-grp') : handle.closest('.ov-strip[data-id]');
    if (!item) return;
    startX = x; startY = y;
    if (ordering()) { lift(item, y); return; }           // im Ordnen-Modus: sofort, ohne Halten
    timer = setTimeout(() => { timer = null; lift(item, startY); }, HOLD_MS);
  }

  // Touch: nur auf den Griffen blockierend – sonst müsste jedes Scrollen der Übersicht auf dieses Skript warten
  const onMove = e => {
    const t = e.touches[0];
    if (drag) { e.preventDefault(); move(t.clientY); return; }
    if (timer && Math.hypot(t.clientX - startX, t.clientY - startY) > SLOP) { clearTimeout(timer); timer = null; }
  };
  list.querySelectorAll('.ov-info, .ov-grp-head').forEach(n => n.addEventListener('touchmove', onMove, {passive: false}));
  list.addEventListener('touchstart', e => begin(e, e.touches[0].clientX, e.touches[0].clientY), {passive: true});
  list.addEventListener('touchmove', e => { if (!e.target.closest('.ov-info, .ov-grp-head')) onMove(e); }, {passive: true});
  list.addEventListener('touchend', drop);
  list.addEventListener('touchcancel', drop);

  list.addEventListener('mousedown', e => { if (e.button === 0) begin(e, e.clientX, e.clientY); });
  addEventListener('mousemove', e => {
    if (drag) { e.preventDefault(); move(e.clientY); }
    else if (timer && Math.hypot(e.clientX - startX, e.clientY - startY) > SLOP) { clearTimeout(timer); timer = null; }
  });
  addEventListener('mouseup', drop);

  list.addEventListener('click', e => {
    if (suppressClick || ordering() && e.target.closest('.ov-info a')) { e.preventDefault(); e.stopPropagation(); }
  }, true);
  list.addEventListener('contextmenu', e => { if (e.target.closest('.ov-info, .ov-grp-head')) e.preventDefault(); });
  list.addEventListener('dragstart', e => { if (e.target.closest('.ov-info, .ov-grp-head')) e.preventDefault(); });

  // ---------- Ordnen-Modus ----------
  const toggle = document.getElementById('order-toggle');
  if (toggle) toggle.addEventListener('click', () => {
    const on = body.classList.toggle('ordering');
    toggle.textContent = on ? 'Fertig' : 'Ordnen';
    toggle.classList.toggle('ghost', !on);
  });

  // ---------- Gruppe anlegen / bearbeiten ----------
  const dlg = document.getElementById('grp-dlg');
  if (!dlg) return;
  const form = dlg.querySelector('form'), nameIn = dlg.querySelector('#grp-name'), msg = dlg.querySelector('#grp-msg');
  const boxes = [...dlg.querySelectorAll('.grp-list input')];
  let old = '';

  function openGroup(name) {
    old = name || '';
    dlg.querySelector('#grp-title').textContent = old ? 'Gruppe bearbeiten' : 'Neue Gruppe';
    nameIn.value = old;
    boxes.forEach(b => { b.checked = !!old && b.dataset.group.toLowerCase() === old.toLowerCase(); });
    dlg.querySelector('#grp-del').hidden = !old;
    msg.hidden = true;
    dlg.showModal();
    if (!old) setTimeout(() => nameIn.focus(), 50);
  }
  async function send(name, ids) {
    try { await post('/groups', {old, name, ids}); location.reload(); }
    catch (e) { msg.textContent = 'Mac nicht erreichbar – nochmals versuchen.'; msg.hidden = false; }
  }
  form.addEventListener('submit', e => {
    e.preventDefault();
    const name = nameIn.value.trim(), ids = boxes.filter(b => b.checked).map(b => +b.value);
    if (!name) { msg.textContent = 'Bitte einen Namen eingeben.'; msg.hidden = false; nameIn.focus(); return; }
    if (!ids.length) { msg.textContent = 'Mindestens eine Suche auswählen.'; msg.hidden = false; return; }
    send(name, ids);
  });
  dlg.querySelector('#grp-del').addEventListener('click', () => send('', []));
  dlg.querySelector('#grp-cancel').addEventListener('click', () => dlg.close());
  dlg.addEventListener('click', e => { if (e.target === dlg) dlg.close(); });   // Tipp neben das Blatt schliesst
  nameIn.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); nameIn.blur(); } });

  list.addEventListener('click', e => {
    const b = e.target.closest('.g-name, .g-edit');
    if (b && !suppressClick) openGroup(b.closest('.ov-grp').dataset.group);
  });
  const add = document.getElementById('new-group');
  if (add) add.addEventListener('click', () => openGroup(''));
})();
