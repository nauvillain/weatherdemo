// weather-edge Worker: serves pipeline output from a private Azure Blob origin.
//
// Caching model (the part worth explaining in an interview):
//   soft TTL  EDGE_TTL_SECONDS   - younger than this, serve from cache (HIT)
//   hard TTL  STALE_MAX_SECONDS  - how long a copy is kept as a fallback
// Past the soft TTL the Worker revalidates against the origin. If the origin
// is down or rejects us, it serves the old copy and says so (X-Cache: STALE)
// instead of failing. Availability is protected, and staleness is visible.

const ROUTES = [
  [/^\/v1\/index\.json$/, () => "v1/index.json"],
  [/^\/v1\/current\/([a-z0-9-]{1,40})\.json$/, (m) => `v1/current/${m[1]}.json`],
];

function int(value, fallback) {
  const n = Number.parseInt(value, 10);
  return Number.isFinite(n) && n >= 0 ? n : fallback;
}

function json(body, status = 200, extra = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", ...extra },
  });
}

function originPathFor(pathname) {
  for (const [pattern, build] of ROUTES) {
    const match = pattern.exec(pathname);
    if (match) return build(match);
  }
  return null;
}

function originUrl(env, path) {
  let sas = (env.ORIGIN_SAS || "").replace(/^\?/, "");
  if (env.FAULT_MODE === "origin_auth") {
    sas = sas.replace(/sig=[^&]*/, "sig=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA%3D");
  }
  return `${env.ORIGIN_BASE.replace(/\/$/, "")}/${path}${sas ? `?${sas}` : ""}`;
}

function respond(source, request, state, ageSeconds, softTtl, extra = {}) {
  const headers = new Headers(source.headers);
  headers.delete("x-stored-at");
  headers.set("cache-control", `public, max-age=${Math.min(softTtl, 60)}`);
  headers.set("access-control-allow-origin", "*");
  headers.set("x-cache", state);
  headers.set("x-cache-age", String(Math.round(ageSeconds)));
  headers.set("x-edge-ttl", String(softTtl));
  for (const [k, v] of Object.entries(extra)) headers.set(k, v);
  return new Response(request.method === "HEAD" ? null : source.body, { status: 200, headers });
}

async function serve(request, env, ctx, url, path) {
  const softTtl = int(env.EDGE_TTL_SECONDS, 120);
  const hardTtl = Math.max(int(env.STALE_MAX_SECONDS, 86400), softTtl);
  const cache = caches.default;
  // Key on origin + path only, so query strings cannot be used to bust or bloat the cache.
  const key = new Request(`${url.origin}${url.pathname}`, { method: "GET" });
  const now = Date.now();

  const cached = await cache.match(key);
  const cachedAge = cached ? (now - Number(cached.headers.get("x-stored-at") || 0)) / 1000 : null;
  if (cached && cachedAge < softTtl) return respond(cached, request, "HIT", cachedAge, softTtl);

  let origin = null;
  let originStatus = "unreachable";
  try {
    origin = await fetch(originUrl(env, path), { headers: { accept: "application/json" } });
    originStatus = String(origin.status);
  } catch (err) {
    console.log(JSON.stringify({ event: "origin_fetch_failed", path, error: String(err) }));
  }

  if (origin && origin.ok) {
    const body = await origin.arrayBuffer();
    const stored = new Response(body, {
      headers: {
        "content-type": "application/json; charset=utf-8",
        "cache-control": `public, max-age=${hardTtl}`,
        "x-stored-at": String(now),
      },
    });
    ctx.waitUntil(cache.put(key, stored.clone()));
    return respond(stored, request, cached ? "REVALIDATED" : "MISS", 0, softTtl);
  }

  if (origin && origin.status === 404) return json({ error: "not_found" }, 404);

  console.log(JSON.stringify({ event: "origin_error", path, origin_status: originStatus, had_cache: Boolean(cached) }));
  if (cached) {
    return respond(cached, request, "STALE", cachedAge, softTtl, { "x-origin-status": originStatus });
  }
  return json({ error: "origin_unavailable", origin_status: originStatus }, 502, { "x-origin-status": originStatus });
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (request.method !== "GET" && request.method !== "HEAD") {
      return json({ error: "method_not_allowed" }, 405, { allow: "GET, HEAD" });
    }
    if (url.pathname === "/healthz") return json({ ok: true, service: "weather-edge" });
    if (url.pathname === "/") {
      return json({ service: "weather-edge", routes: ["/v1/index.json", "/v1/current/{location}.json", "/healthz"] });
    }
    const path = originPathFor(url.pathname);
    if (!path) return json({ error: "not_found" }, 404);
    if (env.FAULT_MODE === "error_500" && path.startsWith("v1/current/")) {
      // Drill: stands in for a bad deploy that breaks one route.
      throw new TypeError("Cannot read properties of undefined (reading 'units')");
    }
    return serve(request, env, ctx, url, path);
  },
};
