// MJT SmartFactory Service Worker
// 정적 자산 캐시 + 네트워크 우선 (API/페이지) 전략
const CACHE_NAME = 'mjt-v1.0.0';
const STATIC_ASSETS = [
  '/static/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/apple-touch-icon.png',
];

// 설치 시 정적 자산 캐시
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS))
      .then(() => self.skipWaiting())
  );
});

// 활성화 시 이전 캐시 정리
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// fetch: 정적 자산은 캐시 우선, 그 외(페이지/API)는 네트워크 우선
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET') return;

  // 정적 자산
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(event.request).then((cached) =>
        cached || fetch(event.request).then((resp) => {
          if (resp.ok) {
            const copy = resp.clone();
            caches.open(CACHE_NAME).then((c) => c.put(event.request, copy));
          }
          return resp;
        }).catch(() => cached)
      )
    );
    return;
  }

  // 페이지/API — 네트워크 우선, 실패 시 캐시
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});
