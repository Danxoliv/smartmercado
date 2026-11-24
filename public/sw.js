/* Service worker: app shell em cache para abrir offline/instalar. A API (/api) nunca é cacheada. */
const VERSAO = "sm-v4";
const SHELL = [
  "/",
  "/index.html",
  "/app.js",
  "/graficos.js",
  "/caixa.js",
  "/styles.css",
  "/manifest.webmanifest",
  "/vendor/html5-qrcode.min.js",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/favicon-32.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(VERSAO).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((nomes) => Promise.all(nomes.filter((n) => n !== VERSAO).map((n) => caches.delete(n))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;

  if (req.mode === "navigate") {
    // Rede primeiro para pegar versões novas; cache se estiver offline.
    e.respondWith(fetch(req).catch(() => caches.match("/index.html")));
    return;
  }
  // Estáticos: responde do cache e atualiza em segundo plano.
  e.respondWith(
    caches.open(VERSAO).then(async (cache) => {
      const emCache = await cache.match(req);
      const rede = fetch(req)
        .then((resp) => { if (resp.ok) cache.put(req, resp.clone()); return resp; })
        .catch(() => emCache);
      return emCache || rede;
    })
  );
});
