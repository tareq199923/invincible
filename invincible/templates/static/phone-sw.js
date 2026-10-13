/* B2 phone service worker: installability only (no offline cache).
   Network-first by design: chat and handoff must never serve stale. */
self.addEventListener("install", (event) => {
  self.skipWaiting();
});
self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});
self.addEventListener("fetch", () => {
  // Pass through: no caching, no rewriting.
});
