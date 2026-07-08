/*
 * A2 Remote relay, a Cloudflare Worker.
 *
 * A dumb mailbox between the companion app on the gaming PC (A2 Remote.exe)
 * and the static status web page. It stores exactly two things per install,
 * both short-lived, both keyed by install_id (the SHA-256 hex of the
 * per-install secret token):
 *
 *   status:<install_id>  latest signed status blob, TTL 180 s
 *   cmd:<install_id>     at most one pending signed command, TTL 60 s
 *
 * The worker NEVER sees the raw token, so it cannot forge commands and it
 * cannot verify them either; verification happens at the endpoints:
 *   - the web page verifies status signatures with WebCrypto,
 *   - the companion verifies command signatures, freshness and nonces.
 * Whoever operates this worker could read the stored status blobs (macro
 * status text and recent log lines, no secrets) but can never command an
 * install and cannot find an install without knowing its install_id.
 *
 * Signature scheme (must match remote_agent.py and pages/index.html):
 *   sig = HMAC-SHA256(token, canonical_json(blob)) as lowercase hex, where
 *   canonical_json is Python's json.dumps(obj, sort_keys=True,
 *   separators=(",", ":")) with default ensure_ascii=True. The worker only
 *   relays blob and sig verbatim; it never canonicalises anything itself.
 *
 * Endpoints (all JSON, CORS open, auth is capability based):
 *   GET  /                     health check, {"ok":true,...}
 *   POST /push                 {install_id, blob, sig} -> stores status
 *   GET  /status?id=<iid>      -> {blob, sig} or 404 {"error":"no_status"}
 *   POST /cmd                  {install_id, blob, sig} -> queues command
 *   GET  /cmd?id=<iid>         -> {blob, sig} AND deletes it (single
 *                                 consume), or 404 {"error":"no_cmd"}
 *
 * Rate limiting: per-IP and per-install counters, checked on every request.
 * Counters live in isolate memory (free and fast); when a scope exceeds its
 * limit the worker writes a penalty-box flag to KV so every other isolate
 * and colo converges to 429 as well. Doing the per-request counting in
 * memory instead of KV keeps the normal path at zero extra KV operations,
 * which matters: on the Workers free plan KV allows only 1,000 writes per
 * day, and the status pushes themselves already spend that budget (see
 * DEPLOY.md, "Usage limits"). Volumetric attacks never reach your home
 * network at all; they hit Cloudflare's edge and stop here.
 */

const ALLOWED_COMMANDS = ["start", "stop", "sleep_phone", "close_emulator"];

const STATUS_TTL = 300;      // seconds a status blob lives in KV (must exceed
                             // the companion's idle push interval so the blob
                             // never lapses to 404 between slow idle pushes)
const CMD_TTL = 60;          // seconds a pending command lives in KV
const CMD_TS_WINDOW = 300;   // loose worker-side freshness check, seconds
                             // (the companion enforces the strict 60 s one)

const MAX_PUSH_BYTES = 65536;
const MAX_CMD_BYTES = 4096;

// Requests per minute. Normal traffic is low: the companion polls for commands
// every ~10 s (6/min) and pushes status about once a minute while idle (faster
// only in a short burst after a command); a visible web page polls every 15 s
// (4/min) and pauses entirely when hidden. The limits keep generous headroom
// for the post-command bursts while still cutting off a flood.
const LIMIT_PER_INSTALL = 90;
const LIMIT_PER_IP = 60;

const HEX64 = /^[0-9a-f]{64}$/;
const NONCE_RE = /^[0-9a-f]{8,64}$/;

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
  "Access-Control-Max-Age": "86400",
};

function json(status, obj, extraHeaders) {
  return new Response(JSON.stringify(obj), {
    status: status,
    headers: Object.assign({
      "Content-Type": "application/json",
      "Cache-Control": "no-store",
    }, CORS_HEADERS, extraHeaders || {}),
  });
}

// Return a stored {blob, sig} JSON string verbatim.
function jsonRaw(stored) {
  return new Response(stored, {
    status: 200,
    headers: Object.assign({
      "Content-Type": "application/json",
      "Cache-Control": "no-store",
    }, CORS_HEADERS),
  });
}

// ---------------------------------------------------------------- rate limit

// In-memory counters, per isolate: "scope|minuteBucket" -> count.
const rlCounts = new Map();

async function rateLimited(env, ctx, scope, limit) {
  const bucket = Math.floor(Date.now() / 60000);
  const key = scope + "|" + bucket;
  const n = (rlCounts.get(key) || 0) + 1;
  rlCounts.set(key, n);

  // Opportunistic pruning of stale buckets so the map cannot grow forever.
  if (rlCounts.size > 4096) {
    const suffix = "|" + bucket;
    for (const k of rlCounts.keys()) {
      if (!k.endsWith(suffix)) rlCounts.delete(k);
    }
  }

  if (n > limit) {
    // First offence in this bucket: flag the scope in KV so other isolates
    // and colos also start returning 429 (one KV write per abusive scope
    // per minute, nothing on the normal path).
    if (n === limit + 1) {
      ctx.waitUntil(
        env.RELAY_KV.put("rl:" + scope, "1", { expirationTtl: 120 }));
    }
    return true;
  }
  // Only consult the shared penalty box once the local count is elevated,
  // so well-behaved traffic costs zero KV operations here.
  if (n > limit / 2) {
    const blocked = await env.RELAY_KV.get("rl:" + scope, { cacheTtl: 60 });
    if (blocked) return true;
  }
  return false;
}

function tooMany() {
  return json(429, { error: "rate_limited" }, { "Retry-After": "30" });
}

// ---------------------------------------------------------------- validation

function validInstallId(id) {
  return typeof id === "string" && HEX64.test(id);
}

function validSig(sig) {
  return typeof sig === "string" && HEX64.test(sig);
}

function validNonce(nonce) {
  return typeof nonce === "string" && NONCE_RE.test(nonce);
}

function validTs(ts) {
  return Number.isInteger(ts) && ts > 0;
}

async function readBody(request, maxBytes) {
  const text = await request.text();
  if (text.length > maxBytes) return { error: json(413, { error: "too_large" }) };
  let body;
  try {
    body = JSON.parse(text);
  } catch (e) {
    return { error: json(400, { error: "bad_json" }) };
  }
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    return { error: json(400, { error: "bad_json" }) };
  }
  return { body: body };
}

// Shared shape check for push and cmd envelopes.
function checkEnvelope(body) {
  if (!validInstallId(body.install_id)) return "bad_install_id";
  if (!validSig(body.sig)) return "bad_sig_format";
  const blob = body.blob;
  if (typeof blob !== "object" || blob === null || Array.isArray(blob)) {
    return "bad_blob";
  }
  if (!validTs(blob.ts)) return "bad_ts";
  if (!validNonce(blob.nonce)) return "bad_nonce";
  return null;
}

// ---------------------------------------------------------------- handlers

async function handlePush(request, env, ctx, ip) {
  const r = await readBody(request, MAX_PUSH_BYTES);
  if (r.error) return r.error;
  const body = r.body;
  const bad = checkEnvelope(body);
  if (bad) return json(400, { error: bad });
  if (typeof body.blob.status !== "object" || body.blob.status === null) {
    return json(400, { error: "bad_status" });
  }
  if (await rateLimited(env, ctx, "i:" + body.install_id, LIMIT_PER_INSTALL)) {
    return tooMany();
  }
  await env.RELAY_KV.put(
    "status:" + body.install_id,
    JSON.stringify({ blob: body.blob, sig: body.sig }),
    { expirationTtl: STATUS_TTL });
  return json(200, { ok: true });
}

async function handleStatusGet(url, env, ctx) {
  const id = url.searchParams.get("id") || "";
  if (!validInstallId(id)) return json(400, { error: "bad_install_id" });
  if (await rateLimited(env, ctx, "i:" + id, LIMIT_PER_INSTALL)) {
    return tooMany();
  }
  const stored = await env.RELAY_KV.get("status:" + id);
  if (!stored) return json(404, { error: "no_status" });
  return jsonRaw(stored);
}

async function handleCmdPost(request, env, ctx) {
  const r = await readBody(request, MAX_CMD_BYTES);
  if (r.error) return r.error;
  const body = r.body;
  const bad = checkEnvelope(body);
  if (bad) return json(400, { error: bad });
  const blob = body.blob;
  if (!ALLOWED_COMMANDS.includes(blob.command)) {
    return json(400, { error: "bad_command" });
  }
  // Loose freshness sanity check; the companion enforces the strict 60 s
  // window against its own clock. This only drops obviously stale junk.
  const now = Math.floor(Date.now() / 1000);
  if (Math.abs(now - blob.ts) > CMD_TS_WINDOW) {
    return json(400, { error: "stale_ts" });
  }
  if (await rateLimited(env, ctx, "i:" + body.install_id, LIMIT_PER_INSTALL)) {
    return tooMany();
  }
  // Overwrite semantics: at most ONE pending command per install.
  await env.RELAY_KV.put(
    "cmd:" + body.install_id,
    JSON.stringify({ blob: blob, sig: body.sig }),
    { expirationTtl: CMD_TTL });
  return json(200, { ok: true });
}

async function handleCmdGet(url, env, ctx) {
  const id = url.searchParams.get("id") || "";
  if (!validInstallId(id)) return json(400, { error: "bad_install_id" });
  if (await rateLimited(env, ctx, "i:" + id, LIMIT_PER_INSTALL)) {
    return tooMany();
  }
  const key = "cmd:" + id;
  const stored = await env.RELAY_KV.get(key);
  if (!stored) return json(404, { error: "no_cmd" });
  // Single consume: delete before answering. (KV is eventually consistent
  // across colos, but the companion is the only reader and always polls
  // from the same place, and it also de-duplicates by nonce.)
  await env.RELAY_KV.delete(key);
  return jsonRaw(stored);
}

// ---------------------------------------------------------------- entry

export default {
  async fetch(request, env, ctx) {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }
    const url = new URL(request.url);
    const path = url.pathname;
    const ip = request.headers.get("CF-Connecting-IP") || "unknown";

    try {
      // The per-IP limit guards every route, before bodies are even read.
      if (await rateLimited(env, ctx, "ip:" + ip, LIMIT_PER_IP)) {
        return tooMany();
      }

      if (request.method === "GET" && path === "/") {
        return json(200, { ok: true, service: "a2-remote-relay" });
      }
      if (request.method === "POST" && path === "/push") {
        return await handlePush(request, env, ctx, ip);
      }
      if (request.method === "GET" && path === "/status") {
        return await handleStatusGet(url, env, ctx);
      }
      if (request.method === "POST" && path === "/cmd") {
        return await handleCmdPost(request, env, ctx);
      }
      if (request.method === "GET" && path === "/cmd") {
        return await handleCmdGet(url, env, ctx);
      }
      return json(404, { error: "not_found" });
    } catch (e) {
      return json(500, { error: "internal" });
    }
  },
};
