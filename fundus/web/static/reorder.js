// Übersicht ordnen per Langdruck und Ziehen (Handy: ~0,45 s halten, Computer: Maus halten):
//  · Infokarte einer Suche ziehen = Reihenfolge ändern; über eine Gruppe ziehen = hinein, darüber hinaus = heraus
//  · Kopf einer Gruppe ziehen = ganze Gruppe verschieben; Name antippen = umbenennen (leer = auflösen)
//  · «Neue Gruppe» legt oben eine leere Gruppe an, in die man Suchen zieht
// Kurzes Tippen öffnet weiterhin die Suche, Wischen scrollt wie gewohnt. Speichern → POST /searches/order.
(function () {
  const list = document.querySelector('.ov');
  if (!list) return;
  const HOLD_MS = 450, SLOP = 8, EDGE = 90;
  const ITEM = '.ov-strip[data-id], .ov-grp';
  let timer = null, startX = 0, startY = 0, drag = null, suppressClick = false;

  const handleOf = t => t.closest && t.closest('.ov-info, .ov-grp-head');
  const strips = () => [...list.querySelectorAll('.ov-strip[data-id]')];
  const markEmpty = () => list.querySelectorAll('.ov-grp').forEach(g => g.classList.toggle('is-empty', !g.querySelector('.ov-strip')));
  const sib = (el, dir) => { let n = el[dir]; while (n && !n.matches(ITEM)) n = n[dir]; return n; };
  const save = body => fetch('/searches/order', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});

  function lift(item, y) {
    drag = {row: item, startY: y, y};
    item.classList.add('lifted');
    document.body.classList.add('reordering');
    if (navigator.vibrate) navigator.vibrate(25);
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
    document.body.classList.remove('reordering');
    drag = null;
    suppressClick = true; setTimeout(() => { suppressClick = false; }, 350);
    const groups = {};
    strips().forEach(r => { const g = r.closest('.ov-grp'); groups[r.dataset.id] = g ? g.dataset.group || '' : ''; });
    try { await save({ids: strips().map(r => +r.dataset.id), groups}); }
    catch (e) { /* Mac kurz weg: gilt bis zum Neuladen */ }
  }

  function begin(e, x, y) {
    const handle = handleOf(e.target);
    if (!handle || !list.contains(handle) || e.target.closest('button, input, a.gear')) return;
    const item = handle.matches('.ov-grp-head') ? handle.closest('.ov-grp') : handle.closest('.ov-strip[data-id]');
    if (!item) return;
    startX = x; startY = y;
    timer = setTimeout(() => { timer = null; lift(item, startY); requestAnimationFrame(autoscroll); }, HOLD_MS);
  }

  // Touch: nur auf den Griffen blockierend – sonst müsste jedes Scrollen der Übersicht auf dieses Skript warten (ruckelt)
  const onMove = e => {
    const t = e.touches[0];
    if (drag) { e.preventDefault(); move(t.clientY); return; }
    if (timer && Math.hypot(t.clientX - startX, t.clientY - startY) > SLOP) { clearTimeout(timer); timer = null; }
  };
  const arm = n => n.addEventListener('touchmove', onMove, {passive: false});
  list.querySelectorAll('.ov-info, .ov-grp-head').forEach(arm);
  list.addEventListener('touchstart', e => begin(e, e.touches[0].clientX, e.touches[0].clientY), {passive: true});
  list.addEventListener('touchmove', e => { if (!handleOf(e.target)) onMove(e); }, {passive: true});
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
  list.addEventListener('contextmenu', e => { if (handleOf(e.target)) e.preventDefault(); });
  list.addEventListener('dragstart', e => { if (handleOf(e.target)) e.preventDefault(); });

  // Gruppenname antippen = umbenennen; leer = Gruppe auflösen
  function rename(sec) {
    const nameEl = sec.querySelector('.g-name');
    if (!nameEl) return;
    const old = sec.dataset.group || '';
    const inp = Object.assign(document.createElement('input'), {className: 'g-input', value: old, placeholder: 'Name der Gruppe', enterKeyHint: 'done'});
    nameEl.replaceWith(inp); inp.focus();
    let done = false;
    const finish = async () => {
      if (done) return; done = true;
      const name = inp.value.trim();
      const members = [...sec.querySelectorAll('.ov-strip[data-id]')];
      if (!members.length) {           // leere Gruppe: Name gilt, sobald eine Suche hineingezogen wird
        if (!name) { sec.remove(); return; }
        sec.dataset.group = name;
        inp.replaceWith(Object.assign(document.createElement('span'), {className: 'g-name', textContent: name}));
        return;
      }
      if (name === old) { inp.replaceWith(nameEl); return; }
      const groups = {}; members.forEach(r => groups[r.dataset.id] = name);
      try { await save({groups}); } catch (e) { /* egal, Neuladen zeigt den Stand */ }
      location.reload();
    };
    inp.addEventListener('keydown', e => { if (e.key === 'Enter') inp.blur(); else if (e.key === 'Escape') { inp.value = old; inp.blur(); } });
    inp.addEventListener('blur', finish);
  }
  list.addEventListener('click', e => { const n = e.target.closest('.g-name'); if (n && !suppressClick) rename(n.closest('.ov-grp')); });

  // Neue Gruppe: oben eine leere Gruppe, Name eintippen, dann Suchen hineinziehen
  const add = document.getElementById('new-group');
  if (add) add.addEventListener('click', () => {
    const sec = document.createElement('section');
    sec.className = 'ov-grp is-empty'; sec.dataset.group = '';
    sec.innerHTML = '<div class="ov-grp-head"><span class="g-name"></span></div>';
    list.prepend(sec); arm(sec.querySelector('.ov-grp-head'));
    scrollTo({top: list.getBoundingClientRect().top + scrollY - 80, behavior: 'smooth'});
    rename(sec);
  });
})();
