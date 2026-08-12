const CACHE_NAME = "swt-pwa-v7-native-history";
const APP_SHELL = [
  "/manifest.webmanifest",
  "/static/js/smooth-navigation.js",
  "/static/js/locale-datetime.js",
  "/static/pwa/icon-192.png",
  "/static/pwa/icon-512.png",
  "/static/pwa/icon-maskable-512.png",
  "/static/pwa/apple-touch-icon.png",
  "/static/pwa/tab-favicon.svg",
  "/static/pwa/brand-logo.svg",
  "/static/marketing/smart-water-tank-hero-ai-960.webp",
  "/static/marketing/smart-water-tank-hero-ai-1280.webp",
  "/static/marketing/smart-water-tank-controls-ai-960.webp",
  "/static/marketing/smart-water-tank-controls-ai-1280.webp",
  "/static/marketing/smart-water-tank-service-ai-960.webp",
  "/static/marketing/smart-water-tank-service-ai-1280.webp",
  "/static/marketing/smart-water-tank-lifestyle-ai-960.webp",
  "/static/marketing/smart-water-tank-lifestyle-ai-1280.webp"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") {
    return;
  }

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) {
    return;
  }

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .catch(async () => {
          return new Response("SaleWell Smart Tank is offline. Please reconnect and refresh.", {
            status: 503,
            headers: { "Content-Type": "text/plain; charset=utf-8" },
          });
        })
    );
    return;
  }

  if (url.pathname.startsWith("/static/") || url.pathname === "/manifest.webmanifest") {
    event.respondWith(
      caches.match(request).then((cached) => {
        if (cached) {
          return cached;
        }
        return fetch(request).then((response) => {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, copy)).catch(() => {});
          return response;
        });
      })
    );
  }
});
