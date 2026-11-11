// Auswertungen einer Suche (Daten aus views.py). Zeichnet in jede Fläche, die es auf der Seite gibt:
// Shortlist (#vw-cards, #vw-rows, #vw-ko, #vw-funnel), Vergleich (#vw-matrix), Preis-Wert-Karte (#vw-map),
// Preisleiter (#vw-ladder), Lieferwege (#vw-lanes), Marktlage (#vw-market). Diagramme in zugeklappten
// Bereichen werden erst beim Aufklappen gezeichnet.
window.FundusViews = (function () {
  const chf = v => v == null ? '–' : Math.round(v).toLocaleString('de-CH').replace(/[,’]/g, "'") + ' CHF';
  const go = o => '/go/' + encodeURIComponent(o.key);
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const age = h => h == null ? '' : h < 24 ? 'vor ' + Math.max(1, Math.round(h)) + ' h' : 'vor ' + Math.round(h / 24) + ' T.';
  const img = (o, big) => (big ? o.img : o.thumb)
    ? `<img src="${esc(big ? o.img : o.thumb)}" loading="lazy" decoding="async" referrerpolicy="no-referrer" alt="">`
    : '<span class="noimg"></span>';
  const COND = ['neu', 'neuwertig', 'sehr gut', 'gut', 'gebraucht', 'defekt', 'unbekannt'];
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const $ = id => document.getElementById(id);
  const ns = 'http://www.w3.org/2000/svg';
  function el(tag, attrs, parent, text) {
    const e = document.createElementNS(ns, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (text != null) e.textContent = text;
    if (parent) parent.appendChild(e);
    return e;
  }

  function render(D, opts = {}) {
    const O = D.offers, S = D.search, SID = S.id;
    const front = O.filter(o => o.front), rest = O.filter(o => !o.front && !o.ko), ko = O.filter(o => o.ko);
    let tip = $('vw-tip');
    if (!tip) { tip = document.createElement('div'); tip.id = 'vw-tip'; document.body.appendChild(tip); }

    function hover(node, o) {
      node.addEventListener('mousemove', ev => {
        tip.innerHTML = `<b>${chf(o.pay)}</b> · ${esc(o.cond)}${o.year ? ' · ' + o.year : ''}<br>${esc(o.title)}<br>` +
          `<span style="opacity:.75">${esc(o.delivery)}${o.ko ? ' · aussortiert: ' + esc(o.ko) : ''}</span>`;
        tip.style.left = Math.min(ev.clientX + 14, innerWidth - 300) + 'px'; tip.style.top = ev.clientY + 14 + 'px'; tip.style.opacity = 1;
      });
      node.addEventListener('mouseleave', () => tip.style.opacity = 0);
      node.addEventListener('click', () => window.open(go(o), '_blank'));
    }
    function whyFront(o) {
      if (!front.some(p => p.pay < o.pay)) return 'Günstigstes passendes Angebot';
      return `Bestes unter ${chf(o.pay)}` + (o.cond !== 'unbekannt' ? ` (Zustand ${o.cond}${o.year ? ', ' + o.year : ''})` : '');
    }
    function catches(o) {
      const c = [];
      if (o.req === 'unklar') c.push('Anforderung unklar' + (o.req_note ? ': ' + o.req_note : ''));
      if (o.cond === 'unbekannt') c.push('Zustand unbekannt');
      if (o.pickup) c.push('nur Abholung');
      if (o.route === 'import') c.push('Import');
      if (o.auction) c.push('Auktion' + (o.bids != null ? ` (${o.bids} Gebote)` : '') + ' – Preis kann steigen');
      if (o.trust === 'check') c.push('Verkäufer prüfen');
      return c;
    }

    // Shortlist
    if ($('vw-funnel')) $('vw-funnel').innerHTML = D.counts.open
      ? `<b>${D.counts.front}</b> von ${D.counts.open} verfügbaren Angeboten – kein anderes ist zugleich günstiger und besser` : '';
    if ($('vw-cards')) {
      $('vw-cards').innerHTML = front.map(o => `
        <div class="card2">
          <a href="${go(o)}" target="_blank" rel="noopener">${img(o, true)}
            <div class="b"><span class="p">${chf(o.pay)}${o.breakdown ? `<small class="bd">(${esc(o.breakdown)})</small>` : ''}</span>
              <span>${esc(o.title)}</span>
              <span class="why">✓ ${esc(whyFront(o))}</span>
              ${catches(o).map(c => `<span class="catch">! ${esc(c)}</span>`).join('')}
              <span class="muted">${esc(o.platform)}${o.location ? ' · ' + esc(o.location) : ''}${o.age_h != null ? ' · ' + age(o.age_h) : ''}</span>
            </div></a>
          ${o.new ? '<span class="newtag">neu</span>' : ''}
          <button class="x" title="Ausblenden" aria-label="Ausblenden" data-key="${esc(o.key)}">×</button>
        </div>`).join('') || '<p class="muted">Noch keine Auswahl – es braucht mindestens 3 bewertete Angebote mit Preis.</p>';
      $('vw-cards').querySelectorAll('.x').forEach(b => b.addEventListener('click', async () => {
        b.closest('.card2').style.opacity = .3;
        await fetch(`/search/${SID}/hide`, {method: 'POST', body: new URLSearchParams({key: b.dataset.key})});
        location.reload();
      }));
    }
    if ($('vw-rows')) $('vw-rows').innerHTML = rest.length ? '<h2 style="margin:18px 0 4px">Knapp dahinter</h2>' + rest.slice(0, 6).map(o => `
      <a class="row" href="${go(o)}" target="_blank" rel="noopener">${img(o)}<b>${chf(o.pay)}</b><span>${esc(o.title)}</span>` +
      `<span class="muted c4">${esc(o.cond)}${o.year ? ' · ' + o.year : ''}</span><span class="muted c5">${age(o.age_h)}</span></a>`).join('') : '';
    if ($('vw-ko')) {
      const kos = {}; ko.forEach(o => kos[o.ko] = (kos[o.ko] || 0) + 1);
      $('vw-ko').textContent = Object.keys(kos).length ? 'Aussortiert: ' + Object.entries(kos).map(([k, n]) => `${n}× ${k}`).join(' · ') : '';
    }

    // Vergleich der Finalisten
    if ($('vw-matrix')) {
      const cols = front.concat(rest.slice(0, Math.max(0, 4 - front.length)));
      if (!cols.length) $('vw-matrix').innerHTML = '<tr><td class="muted">Noch keine Finalisten.</td></tr>';
      else {
        const rows = [
          ['Endpreis', o => chf(o.pay), o => -o.pay],
          ['Aufschlüsselung', o => o.breakdown || '–', null],
          ['Fairer Wert', o => chf(o.value), o => o.value || 0],
          ['Wert ÷ Preis', o => o.ratio ? '×' + o.ratio.toFixed(2) : '–', o => o.ratio || 0],
          ['Zustand', o => o.cond, o => -COND.indexOf(o.cond)],
          ['Jahrgang', o => o.year || '–', o => o.year || 0],
          ['Anforderung', o => o.req === 'ja' ? '✓ ' + (o.req_note || '') : o.req === 'unklar' ? '? ' + (o.req_note || 'unklar') : '–', o => o.req === 'ja' ? 1 : 0],
          ['Lieferweg', o => o.delivery || '–', o => o.route === 'ch' ? 2 : o.route === 'pickup' ? 1 : 0],
          ['Ort', o => o.location || '–', null],
          ['Inseriert', o => age(o.age_h) || '–', o => -(o.age_h ?? 1e9)],
          ['Art', o => o.auction ? 'Auktion' + (o.bids != null ? ` (${o.bids} Gebote)` : '') : 'Festpreis', null],
          ['Haken', o => catches(o).join(', ') || '–', o => -catches(o).length],
        ];
        let h = '<tr><th></th>' + cols.map(o => `<th><a href="${go(o)}" target="_blank" rel="noopener">${img(o, true)}${o.front ? '★ ' : ''}` +
          `${esc(o.title.slice(0, 48))}</a><br><span class="muted">${esc(o.platform)}</span></th>`).join('') + '</tr>';
        for (const [name, show, rank] of rows) {
          const best = rank ? Math.max(...cols.map(rank)) : null, tie = rank && cols.every(c => rank(c) === best);
          h += `<tr><th>${name}</th>` + cols.map(o => `<td class="${rank && !tie && rank(o) === best ? 'best' : ''}">${esc(show(o))}</td>`).join('') + '</tr>';
        }
        $('vw-matrix').innerHTML = h;
      }
    }

    // Lieferwege
    if ($('vw-lanes')) $('vw-lanes').innerHTML = [['pickup', 'Abholen'], ['ch', 'Versand Schweiz'], ['import', 'Import (Grenzadresse)']].map(([k, name]) => {
      const list = O.filter(o => o.route === k && !o.ko).sort((a, b) => (b.front - a.front) || ((b.ratio || 0) - (a.ratio || 0)));
      const n = opts.laneItems || 5;
      return `<div class="lane"><h4>${name} <span class="muted">· ${list.length}${list.length ? ', ab ' + chf(Math.min(...list.map(o => o.pay))) : ''}</span></h4>` +
        list.slice(0, n).map(o => `<a class="${o.front ? 'front' : ''}" href="${go(o)}" target="_blank" rel="noopener">${img(o)}<span>${esc(o.title.slice(0, 60))}` +
          `<br><span class="muted">${esc(o.cond)}${o.location ? ' · ' + esc(o.location) : ''}</span></span><b>${chf(o.pay)}</b></a>`).join('') +
        (list.length > n ? `<span class="muted small">+ ${list.length - n} weitere</span>` : '') +
        (list.length ? '' : '<span class="muted small">keine Angebote auf diesem Weg</span>') + '</div>';
    }).join('');

    // Diagramme
    const DRAW = {
      map() {
        const svg = $('vw-map'), W = 900, H = 400, L = 70, R = 20, T = 15, B = 40;
        const pts = O.filter(o => o.value);
        if (!pts.length) { el('text', {x: 20, y: 40}, svg, 'Noch kein Marktpreis – dafür braucht es mindestens 3 bewertete Preise.'); return; }
        const xs = pts.map(o => o.pay), ys = pts.map(o => o.value);
        const q = D.band || [Math.min(...xs), 0, Math.max(...xs)];
        const xmax = Math.min(Math.max(...xs), q[2] * 2.2) * 1.05, ymax = Math.max(...ys) * 1.1, ymin = Math.min(...ys) * 0.7;
        const X = v => L + Math.min(v, xmax) / xmax * (W - L - R);
        const Y = v => H - B - (v - ymin) / (ymax - ymin || 1) * (H - T - B);
        if (D.band) el('rect', {x: X(q[0]), y: T, width: Math.max(0, X(q[2]) - X(q[0])), height: H - T - B, fill: css('--surface-2')}, svg);
        for (let i = 0; i <= 5; i++) {
          const xv = xmax * i / 5, yv = ymin + (ymax - ymin) * i / 5;
          el('line', {x1: X(xv), x2: X(xv), y1: T, y2: H - B, class: 'grid'}, svg);
          el('text', {x: X(xv), y: H - B + 16, 'text-anchor': 'middle'}, svg, chf(xv));
          el('line', {x1: L, x2: W - R, y1: Y(yv), y2: Y(yv), class: 'grid'}, svg);
          el('text', {x: L - 8, y: Y(yv) + 4, 'text-anchor': 'end'}, svg, chf(yv));
        }
        const d0 = Math.max(0, ymin), d1 = Math.min(xmax, ymax);
        el('line', {x1: X(d0), y1: Y(d0), x2: X(d1), y2: Y(d1), stroke: css('--muted'), 'stroke-dasharray': '4 4'}, svg);
        el('text', {x: X(d1) - 4, y: Y(d1) - 6, 'text-anchor': 'end'}, svg, 'Wert = Preis');
        el('text', {x: W - R, y: H - 4, 'text-anchor': 'end'}, svg, 'Endpreis bis zu dir →');
        el('text', {x: L + 6, y: T + 12}, svg, '↑ fairer Wert');
        const f = front.filter(o => o.value).sort((a, b) => a.pay - b.pay);
        if (f.length > 1) el('polyline', {points: f.map(o => `${X(o.pay)},${Y(o.value)}`).join(' '), fill: 'none', stroke: css('--tier-s'), 'stroke-width': 2}, svg);
        pts.sort((a, b) => (a.front - b.front) || (!!b.ko - !!a.ko)).forEach(o => {
          const r = o.age_h == null ? 5 : o.age_h < 24 ? 8 : o.age_h < 24 * 7 ? 6 : 4;
          const color = o.front ? css('--tier-s') : o.ko ? css('--warn') : css('--tier-b'), op = o.front ? 1 : .55;
          hover(o.auction
            ? el('rect', {x: X(o.pay) - r, y: Y(o.value) - r, width: 2 * r, height: 2 * r, transform: `rotate(45 ${X(o.pay)} ${Y(o.value)})`,
                          fill: color, 'fill-opacity': op, class: 'dot'}, svg)
            : el('circle', {cx: X(o.pay), cy: Y(o.value), r, fill: color, 'fill-opacity': op, class: 'dot'}, svg), o);
        });
      },
      ladder() {
        const svg = $('vw-ladder'), rows = COND.filter(c => O.some(o => o.cond === c && !o.ko));
        const W = 900, L = 90, R = 20, rowH = 36, T = 14;
        svg.setAttribute('viewBox', `0 0 ${W} ${T + rows.length * rowH + 28}`);
        const xs = O.filter(o => !o.ko).map(o => o.pay); if (!xs.length) return;
        const xmax = Math.min(Math.max(...xs), D.band ? D.band[2] * 2.2 : Infinity) * 1.05;
        const X = v => L + Math.min(v, xmax) / xmax * (W - L - R);
        rows.forEach((c, i) => {
          const y = T + i * rowH + rowH / 2, list = O.filter(o => o.cond === c && !o.ko), ps = list.map(o => o.pay).sort((a, b) => a - b);
          el('text', {x: L - 10, y: y + 4, 'text-anchor': 'end'}, svg, c);
          el('line', {x1: L, x2: W - R, y1: y, y2: y, class: 'grid'}, svg);
          if (ps.length >= 4) {
            const a = ps[Math.floor(ps.length / 4)], b = ps[Math.floor(ps.length * 3 / 4)], m = ps[Math.floor(ps.length / 2)];
            el('rect', {x: X(a), y: y - 11, width: Math.max(2, X(b) - X(a)), height: 22, rx: 6, fill: css('--surface-2')}, svg);
            el('line', {x1: X(m), x2: X(m), y1: y - 11, y2: y + 11, stroke: css('--muted')}, svg);
          }
          list.forEach(o => hover(el('circle', {cx: X(o.pay), cy: y + (o.front ? 0 : ((o.key.length * 7) % 13) - 6), r: o.front ? 7 : 4.5,
            fill: o.front ? css('--tier-s') : css('--tier-b'), 'fill-opacity': o.front ? 1 : .6, class: 'dot'}, svg), o));
        });
        const yb = T + rows.length * rowH + 16;
        for (let i = 0; i <= 5; i++) el('text', {x: X(xmax * i / 5), y: yb, 'text-anchor': 'middle'}, svg, chf(xmax * i / 5));
        if (S.new_price && S.new_price < xmax) {
          el('line', {x1: X(S.new_price), x2: X(S.new_price), y1: T - 4, y2: yb - 12, stroke: css('--accent'), 'stroke-dasharray': '3 3'}, svg);
          el('text', {x: X(S.new_price) + 4, y: T + 2}, svg, 'neu ' + chf(S.new_price));
        }
      },
      market() {
        const svg = $('vw-market'), tl = D.timeline, big = svg.dataset.big === '1';
        const W = big ? 900 : 600, H = big ? 220 : 70, B = big ? 22 : 0, mid = (H - B) / 2;
        svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
        const max = Math.max(1, ...tl.map(d => Math.max(d.new, d.gone))), bw = Math.min(W / tl.length, 30), Y = v => (mid - 4) * Math.sqrt(v / max);
        const every = Math.max(1, Math.ceil(tl.length / 9));   // höchstens ~9 Datumsbeschriftungen
        el('line', {x1: 0, x2: W, y1: mid, y2: mid, class: 'grid'}, svg);
        tl.forEach((d, i) => {
          const x = i * bw + 1;
          if (d.new) el('rect', {x, y: mid - Y(d.new), width: bw - 2, height: Y(d.new), rx: 2, fill: css('--accent')}, svg)
            .appendChild(el('title', {}, null, `${d.days === 7 ? 'Woche ab ' : ''}${d.day}: ${d.new} neu`));
          if (d.gone) el('rect', {x, y: mid, width: bw - 2, height: Y(d.gone), rx: 2, fill: css('--warn')}, svg)
            .appendChild(el('title', {}, null, `${d.days === 7 ? 'Woche ab ' : ''}${d.day}: ${d.gone} weg`));
          if (big && i % every === 0) el('text', {x: x + bw / 2, y: H - 6, 'text-anchor': 'middle'}, svg, d.day.slice(8, 10) + '.' + d.day.slice(5, 7) + '.');
        });
      },
    };
    const drawn = {};
    function draw() {
      for (const v of Object.keys(DRAW)) {
        const node = $('vw-' + v);
        if (!node || drawn[v]) continue;
        const box = node.closest('details');
        if (box && !box.open) continue;
        drawn[v] = true; DRAW[v]();
      }
    }
    // Ausklapp-Zustand pro Bereich merken
    document.querySelectorAll('.vw details.view').forEach(d => {
      const k = 'fundus-view-' + d.dataset.view;
      try { if (localStorage.getItem(k) === '1') d.open = true; } catch (e) { /* privates Fenster */ }
      d.addEventListener('toggle', () => { try { localStorage.setItem(k, d.open ? '1' : '0'); } catch (e) { /* egal */ } draw(); });
    });
    draw();

    // Liste aller Angebote wird beim Aufklappen nachgeladen (hx-trigger="toggle once"); war sie schon offen, bevor
    // htmx bereit war, hier sicherstellen
    const listBox = document.querySelector('.vw details.view[data-view="list"]');
    const ensureList = () => {
      const slot = $('list-slot');
      if (listBox && listBox.open && slot && slot.dataset.loaded !== '1' && window.htmx && !slot.querySelector('#search-results-container')) {
        slot.dataset.loaded = '1';
        htmx.ajax('GET', `/search/${SID}/results`, {target: '#list-slot', swap: 'innerHTML'});
      }
    };
    if (listBox) {
      listBox.addEventListener('toggle', () => setTimeout(ensureList, 300));
      addEventListener('load', () => setTimeout(ensureList, 300));
    }

    // Verfügbarkeitsprüfung beim Öffnen: warten, und nur neu laden, wenn sich oben etwas geändert hat
    if (opts.watching) {
      const shown = front.map(o => o.key).concat(rest.slice(0, 6).map(o => o.key)).join('|');
      (function poll(n) {
        setTimeout(async () => {
          try {
            const r = await fetch(`/search/${SID}/fresh`).then(r => r.json());
            if (r.watching && n < 60) return poll(n + 1);
            if (r.top.join('|') !== shown) location.reload();
            else if ($('vw-watch')) $('vw-watch').remove();
          } catch (e) { /* Mac kurz nicht erreichbar: beim nächsten Öffnen wieder */ }
        }, 2500);
      })(0);
    }
  }
  return {render, chf};
})();
