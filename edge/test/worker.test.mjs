// Runs the Worker in plain Node with a fake cache and a fake origin.
import test from "node:test";
import assert from "node:assert/strict";
import worker from "../src/index.js";

function setup({ originStatus = 200, body = { generated_at: "2026-10-09T12:00:00Z" } } = {}) {
  const store = new Map();
  const state = { originStatus, body, originCalls: [], pending: [] };
  globalThis.caches = {
    default: {
      async match(req) {
        const hit = store.get(req.url);
        return hit ? hit.clone() : undefined;
      },
      async put(req, res) {
        store.set(req.url, res);
      },
    },
  };
  globalThis.fetch = async (url) => {
    state.originCalls.push(String(url));
    return new Response(JSON.stringify(state.body), { status: state.originStatus });
  };
  const env = {
    ORIGIN_BASE: "https://acct.blob.core.windows.net/weather",
    ORIGIN_SAS: "sv=2024&sig=REAL",
    EDGE_TTL_SECONDS: "120",
    STALE_MAX_SECONDS: "86400",
    FAULT_MODE: "none",
  };
  const ctx = { waitUntil: (p) => state.pending.push(p) };
  const get = async (path, method = "GET") => {
    const res = await worker.fetch(new Request(`https://weather.vilnico.com${path}`, { method }), env, ctx);
    await Promise.all(state.pending);
    return res;
  };
  return { state, env, get, store };
}

test("first request is a MISS, second is a HIT without touching the origin", async () => {
  const { state, get } = setup();
  const first = await get("/v1/index.json");
  assert.equal(first.status, 200);
  assert.equal(first.headers.get("x-cache"), "MISS");
  assert.equal((await first.json()).generated_at, "2026-10-09T12:00:00Z");
  const second = await get("/v1/index.json");
  assert.equal(second.headers.get("x-cache"), "HIT");
  assert.equal(state.originCalls.length, 1);
  assert.match(state.originCalls[0], /\/weather\/v1\/index\.json\?sv=2024&sig=REAL$/);
});

test("query strings share one cache entry", async () => {
  const { state, get } = setup();
  await get("/v1/index.json?a=1");
  const res = await get("/v1/index.json?b=2");
  assert.equal(res.headers.get("x-cache"), "HIT");
  assert.equal(state.originCalls.length, 1);
});

test("past the soft TTL it revalidates and picks up new data", async () => {
  const { state, env, get } = setup();
  await get("/v1/index.json");
  env.EDGE_TTL_SECONDS = "0";
  state.body = { generated_at: "2026-10-09T13:00:00Z" };
  const res = await get("/v1/index.json");
  assert.equal(res.headers.get("x-cache"), "REVALIDATED");
  assert.equal((await res.json()).generated_at, "2026-10-09T13:00:00Z");
});

test("origin failure with a cached copy serves STALE and reports the origin status", async () => {
  const { state, env, get } = setup();
  await get("/v1/index.json");
  env.EDGE_TTL_SECONDS = "0";
  state.originStatus = 403;
  const res = await get("/v1/index.json");
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("x-cache"), "STALE");
  assert.equal(res.headers.get("x-origin-status"), "403");
});

test("origin failure with no cached copy is a 502", async () => {
  const { get } = setup({ originStatus: 503 });
  const res = await get("/v1/index.json");
  assert.equal(res.status, 502);
  assert.equal((await res.json()).error, "origin_unavailable");
});

test("an oversized soft TTL keeps serving old data (the ttl_misconfig drill)", async () => {
  const { state, env, get } = setup();
  await get("/v1/index.json");
  env.EDGE_TTL_SECONDS = "86400";
  state.body = { generated_at: "2026-10-09T13:00:00Z" };
  const res = await get("/v1/index.json");
  assert.equal(res.headers.get("x-cache"), "HIT");
  assert.equal(res.headers.get("x-edge-ttl"), "86400");
  assert.equal((await res.json()).generated_at, "2026-10-09T12:00:00Z");
});

test("origin_auth fault corrupts only the signature", async () => {
  const { state, env, get } = setup();
  env.FAULT_MODE = "origin_auth";
  await get("/v1/index.json");
  assert.match(state.originCalls[0], /\?sv=2024&sig=AAAA/);
});

test("routing: unknown paths, bad ids, methods, missing blobs", async () => {
  const { state, get } = setup();
  assert.equal((await get("/v1/nope.json")).status, 404);
  assert.equal((await get("/v1/current/..%2Fsecret.json")).status, 404);
  assert.equal((await get("/v1/index.json", "POST")).status, 405);
  assert.equal(state.originCalls.length, 0);
  state.originStatus = 404;
  assert.equal((await get("/v1/current/atlantis.json")).status, 404);
  assert.equal((await get("/healthz")).status, 200);
});

test("HEAD returns headers without a body", async () => {
  const { get } = setup();
  const res = await get("/v1/index.json", "HEAD");
  assert.equal(res.status, 200);
  assert.equal(await res.text(), "");
});

test("error_500 fault breaks only the per-location route", async () => {
  const { env, get } = setup();
  env.FAULT_MODE = "error_500";
  await assert.rejects(() => get("/v1/current/chicago.json"), TypeError);
  assert.equal((await get("/v1/index.json")).status, 200);
});
