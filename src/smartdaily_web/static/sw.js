/* Service Worker for iOS 16.4+ & Desktop Web Push PWA */

self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  let payload = {
    title: "🔔 社區新通知",
    body: "您有一則新的社區公告或個人通知",
    url: "/",
    tag: "smartdaily-notification",
  };

  if (event.data) {
    try {
      const parsed = event.data.json();
      payload = { ...payload, ...parsed };
    } catch (err) {
      payload.body = event.data.text() || payload.body;
    }
  }

  const options = {
    body: payload.body,
    icon: "/static/icon@2x.png",
    badge: "/static/icon.png",
    tag: payload.tag || "smartdaily",
    renotify: true,
    data: {
      url: payload.url || "/",
    },
  };

  event.waitUntil(
    (async () => {
      await self.registration.showNotification(payload.title, options);
      if ("setAppBadge" in self.navigator) {
        try {
          await self.navigator.setAppBadge(1);
        } catch (_) {}
      }
    })()
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const targetUrl = (event.notification.data && event.notification.data.url) || "/";

  event.waitUntil(
    (async () => {
      if ("clearAppBadge" in self.navigator) {
        try {
          await self.navigator.clearAppBadge();
        } catch (_) {}
      }
      const allClients = await self.clients.matchAll({
        type: "window",
        includeUncontrolled: true,
      });
      for (const client of allClients) {
        if ("focus" in client) {
          await client.focus();
          client.postMessage({ type: "NAVIGATE", url: targetUrl });
          return;
        }
      }
      if (self.clients.openWindow) {
        await self.clients.openWindow(targetUrl);
      }
    })()
  );
});
