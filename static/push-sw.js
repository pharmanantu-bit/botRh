/* Service worker botRh — notifications push de l'Agent RH (portée /admin/).
   Ne met rien en cache : il ne sert qu'à afficher les notifications et à
   ouvrir la page au clic. */
self.addEventListener('install', function () { self.skipWaiting(); });
self.addEventListener('activate', function (e) { e.waitUntil(self.clients.claim()); });

self.addEventListener('push', function (e) {
  var d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = { corps: e.data ? e.data.text() : '' }; }
  var titre = d.titre || 'botRh — Agent RH';
  e.waitUntil(self.registration.showNotification(titre, {
    body: d.corps || '',
    icon: '/static/icon-192.png',
    badge: '/static/icon-192.png',
    tag: d.tag || 'botrh-agent',
    renotify: true,
    data: { url: d.url || '/admin/agent' }
  }));
});

self.addEventListener('notificationclick', function (e) {
  e.notification.close();
  var cible = new URL((e.notification.data && e.notification.data.url) || '/admin/agent', self.location.origin).href;
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (fenetres) {
    for (var i = 0; i < fenetres.length; i++) {
      var f = fenetres[i];
      if (f.url.indexOf(self.location.origin) === 0 && 'focus' in f) {
        if ('navigate' in f) { f.navigate(cible); }
        return f.focus();
      }
    }
    return self.clients.openWindow(cible);
  }));
});
