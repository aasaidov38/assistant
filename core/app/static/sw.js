// Service worker панели (версия 1.8): push-уведомления и страница «нет связи».
// Данные панели не кэшируем — они всегда свежие с сервера.
const OFFLINE = `<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Нет связи</title><body style="font-family:system-ui,sans-serif;display:grid;place-items:center;min-height:90vh;margin:0;background:#f3f4f8;color:#26303d;text-align:center">
<div><div style="font-size:48px">✦</div><h2>Нет связи с ассистентом</h2><p style="color:#667">Проверьте интернет и попробуйте снова.</p>
<button onclick="location.reload()" style="padding:10px 22px;border-radius:12px;border:1px solid #ccd;background:#fff;font:inherit">Обновить</button></div></body></html>`;

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

self.addEventListener('fetch', e => {
  if (e.request.mode !== 'navigate') return;          // всё, кроме открытия страниц, идёт как обычно
  e.respondWith(fetch(e.request).catch(() => new Response(OFFLINE, {headers: {'Content-Type': 'text/html; charset=utf-8'}})));
});

self.addEventListener('push', e => {
  let m = {};
  try { m = e.data ? e.data.json() : {}; } catch (_) { m = {title: 'Ассистент', body: e.data ? e.data.text() : ''}; }
  e.waitUntil(self.registration.showNotification(m.title || 'Ассистент', {
    body: m.body || '', icon: '/web/static/icon-192.png?v=3', badge: '/web/static/icon-192.png?v=3',
    data: {url: m.url || '/web/chat'}, tag: m.tag || undefined, lang: 'ru'}));
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = new URL((e.notification.data && e.notification.data.url) || '/web/chat', self.location.origin).href;
  e.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(list => {
    for (const c of list) if (c.url.startsWith(self.location.origin + '/web')) { c.navigate(url).catch(() => {}); return c.focus(); }
    return self.clients.openWindow(url);
  }));
});
