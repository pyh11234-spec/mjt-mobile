// MJT SmartCompany Service Worker
// 정적 자산도 네트워크 우선(허브 sw.js와 동일 전략, 2026-07-31 수정) — 오프라인일 때만 캐시 사용.
// ★이전엔 정적 자산이 "캐시 우선"이라, manifest.json·아이콘을 새로 바꿔도 이미 설치된 사용자
// 기기에서는 캐시가 영원히 안 갱신돼(CACHE_NAME도 안 바뀜) PWA 설치 아이콘이 계속 옛날 걸로
// 뜨는 문제가 있었음(팀장님 실사용 중 발견 — "허접하게 링크 아이콘으로 뜬다"). 아이콘/매니페스트를
// 다시 바꿀 일이 생기면 이 CACHE_NAME도 함께 올릴 것(캐시 강제 무효화).
const CACHE_NAME = 'mjt-v1.1.0-20260731';
const STATIC_ASSETS = [
  '/static/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/apple-touch-icon.png',
];

// 설치 시 정적 자산 캐시(오프라인 폴백용 — 우선순위는 아래 fetch에서 네트워크가 항상 이김)
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS))
      .then(() => self.skipWaiting())
  );
});

// 활성화 시 이전 캐시(버전 다른 것) 전부 정리
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// fetch: 전부 네트워크 우선, 실패(오프라인) 시에만 캐시로 폴백
self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;
  event.respondWith(
    fetch(event.request).then((resp) => {
      if (resp.ok && new URL(event.request.url).pathname.startsWith('/static/')) {
        const copy = resp.clone();
        caches.open(CACHE_NAME).then((c) => c.put(event.request, copy));
      }
      return resp;
    }).catch(() => caches.match(event.request))
  );
});
