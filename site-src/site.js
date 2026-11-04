/* Семейный ассистент — общий скрипт: тема, меню, подстановка данных из data.js. */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));

// ---------- тема ----------
function setTheme(t, save) {
  const r = document.documentElement;
  if (t === 'light' || t === 'dark') r.dataset.theme = t; else delete r.dataset.theme;
  $$('.theme button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.t === (t || 'system'))));
  if (save) try { localStorage.setItem('site-theme', t); } catch (e) {}
}
setTheme(document.documentElement.dataset.theme || 'system', false);
$$('.theme button').forEach(b => b.onclick = () => setTheme(b.dataset.t, true));

// ---------- меню ----------
const hdr = $('.top');
$('.burger') && ($('.burger').onclick = () => {
  const o = hdr.classList.toggle('open'); $('.burger').setAttribute('aria-expanded', String(o));
});
$$('.dd > button').forEach(b => b.onclick = e => {
  e.stopPropagation(); const d = b.parentElement, o = !d.classList.contains('open');
  $$('.dd').forEach(x => x.classList.remove('open')); d.classList.toggle('open', o); b.setAttribute('aria-expanded', String(o));
});
document.addEventListener('click', () => $$('.dd').forEach(x => x.classList.remove('open')));
document.addEventListener('keydown', e => { if (e.key === 'Escape') $$('.dd').forEach(x => x.classList.remove('open')); });

// ---------- карточки возможностей ----------
const featCard = ([ic, t, d, say, v]) => `<div class="card feat"><div class="t"><span class="ic">${ic}</span><h3>${esc(t)}</h3></div>
  <p>${esc(d)}</p><div class="say">${say.map(s => `<code>${esc(s)}</code>`).join('')}</div><span class="since">с версии ${v}</span></div>`;
function renderFeat(sel, group) {
  const box = $(sel); if (!box) return;
  const list = FEATURES.filter(f => !group || group === 'all' || f[5] === group);
  box.innerHTML = list.map(featCard).join('');
}
function renderFeatHub() {
  const bar = $('#filters'); if (!bar) return;
  const opts = [['all', 'Все'], ...GROUPS.map(g => [g[0], g[2]])];
  bar.innerHTML = opts.map(([k, n], i) => `<button data-g="${k}" aria-pressed="${i === 0}">${esc(n)}</button>`).join('');
  const show = g => { renderFeat('#grid', g); $$('#filters button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.g === g)));
    const c = $('#count'); if (c) c.textContent = (g === 'all' ? FEATURES.length : FEATURES.filter(f => f[5] === g).length) + ' возможностей'; };
  $$('#filters button').forEach(b => b.onclick = () => show(b.dataset.g)); show('all');
}

// ---------- примеры переписки ----------
const chatBlock = ([q, a, b]) => `<div class="chat"><div class="msg me">${esc(q)}</div>
  <div class="msg bot">${a}${b && b.length ? `<div class="btns">${b.map(x => `<span>${esc(x)}</span>`).join('')}</div>` : ''}</div></div>`;
function renderEx(sel, group, max) {
  const box = $(sel); if (!box) return;
  let list = EXAMPLES.filter(e => !group || group === 'all' || e[3] === group); if (max) list = list.slice(0, max);
  box.innerHTML = list.map(chatBlock).join('');
}

// ---------- скриншоты ----------
function renderShots(sel, names, phone) {
  const box = $(sel); if (!box) return;
  const src = [...DESK, ...PHONE]; const list = names.map(n => src.find(s => s[0] === n)).filter(Boolean);
  box.className = 'shots ' + (phone ? 'phone' : 'mini');
  box.innerHTML = list.map(([f, t, d]) => shotHtml(f, t, d, phone)).join(''); bindZoom(box);
}
const shotHtml = (f, t, d, phone) => `<figure class="shot"><button data-f="${f}" data-c="${esc(t + ' — ' + d)}" aria-label="Увеличить: ${esc(t)}">
  <img src="img/shots/${f}.jpg" alt="${esc(t + ': ' + d)}" loading="lazy"></button>
  <figcaption><b>${esc(t)}</b> — ${esc(d)}</figcaption></figure>`;
function bindZoom(box) {
  const z = $('#zoom'); if (!z) return;
  $$('button', box).forEach(b => b.onclick = () => { $('#zimg').src = `img/shots/${b.dataset.f}.jpg`; $('#zcap').textContent = b.dataset.c;
    try { z.showModal(); } catch (e) { z.setAttribute('open', ''); } });
}
function screensPage() {
  const draw = phone => { const box = $('#shots'); box.className = 'shots' + (phone ? ' phone' : '');
    box.innerHTML = (phone ? PHONE : DESK).map(([f, t, d]) => shotHtml(f, t, d, phone)).join(''); bindZoom(box); };
  draw(false);
  [['#tDesk', false], ['#tPhone', true]].forEach(([id, ph]) => $(id).onclick = () => {
    ['#tDesk', '#tPhone'].forEach(x => { const on = x === id; $(x).classList.toggle('on', on); $(x).setAttribute('aria-selected', on); }); draw(ph); });
}
if ($('#zoom')) { $('#zx').onclick = () => $('#zoom').close(); $('#zoom').onclick = e => { if (e.target.id === 'zoom') $('#zoom').close(); }; }

// ---------- история и дорожная карта ----------
function renderLog() {
  const box = $('#log'); if (!box) return;
  box.innerHTML = LOG.map(([v, n, d, items, now]) => `<div class="entry${now ? ' now' : ''}"><div class="card">
    <div class="hd"><span class="ver">${v}</span><span class="name">${esc(n)}</span>${now ? '<span class="tag">выходит сейчас</span>' : ''}<span class="date">${d}</span></div>
    <ul>${items.map(i => `<li>${esc(i)}</li>`).join('')}</ul></div></div>`).join('');
  const data = [...LOG].reverse().map(([v, , , items]) => ({v, n: items.length}));
  let acc = 0; data.forEach(d => d.total = (acc += d.n));
  const W = 760, H = 250, L = 34, R = 10, T = 22, B = 30, max = Math.ceil(acc / 10) * 10;
  const bw = (W - L - R) / data.length, y = v => T + (H - T - B) * (1 - v / max); let s = '';
  for (let g = 0; g <= max; g += 10) s += `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(g)}" y2="${y(g)}"/><text x="${L - 6}" y="${y(g) + 4}" text-anchor="end">${g}</text>`;
  data.forEach((d, i) => { const x = L + i * bw + bw * .18, w = bw * .64, last = i === data.length - 1;
    s += `<rect class="b${last ? ' last' : ''}" x="${x}" y="${y(d.total)}" width="${w}" height="${y(0) - y(d.total)}" rx="4"><title>${d.v}: ${d.total} (+${d.n})</title></rect>`;
    s += `<text class="v" x="${x + w / 2}" y="${y(d.total) - 6}" text-anchor="middle">${d.total}</text><text x="${x + w / 2}" y="${H - 10}" text-anchor="middle">${d.v}</text>`; });
  const svg = $('#growth'); svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.innerHTML = s;
}
function renderLanes() {
  const box = $('#lanes'); if (!box) return;
  box.innerHTML = LANES.map(([v, d, next, items]) => `<div class="lane${next ? ' next' : ''}"><div class="lh"><b>${esc(v)}</b><span>${esc(d)}</span></div>
    ${items.map(([t, s, z]) => `<div class="item"><span class="size">${z}</span><b>${esc(t)}</b><small>${esc(s)}</small></div>`).join('')}</div>`).join('');
}

// ---------- оживление чата на первом экране ----------
if (!matchMedia('(prefers-reduced-motion: reduce)').matches) {
  const bots = $$('.hero .msg.bot'); let i = 0;
  if (bots.length) setInterval(() => { bots.forEach(b => b.classList.remove('hl')); void document.body.offsetWidth; bots[i % bots.length].classList.add('hl'); i++; }, 2600);
}
