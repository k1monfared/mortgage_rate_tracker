/**
 * subscribe-proxy for the mortgage rate tracker.
 *
 * Self-hosted subscriber list (D1 `subscribers` table) plus per-day dedup
 * (D1 `sends` table). No Resend audiences used — we only touch Resend's
 * transactional /emails/batch API, so we never consume audience/segment
 * slots. Subscriber addresses never leave the Cloudflare account except at
 * send time (where Resend necessarily sees them).
 *
 * Endpoints:
 *   POST /                            subscribe: accepts email + list=ca|us,
 *                                     sends a double-opt-in confirmation email.
 *   GET  /confirm?token=…             confirmation click: writes a subscribers
 *                                     row with a fresh unsub_token.
 *   GET  /block?token=…               "never email me again" link in the
 *                                     confirmation email: silently blocks the
 *                                     address so future subscribe attempts are
 *                                     dropped.
 *   GET  /unsubscribe?token=…         one-click unsub from a broadcast email.
 *                                     Deletes the subscribers row and writes a
 *                                     permanent block:<email> KV entry.
 *   POST /unsubscribe                 same as GET for RFC 8058 list-unsub-post.
 *                                     Body is ignored; token is in the URL.
 *   POST /broadcast                   GH Actions → worker: sends a short email
 *                                     to every confirmed subscriber for a list,
 *                                     subject to per-day dedup. Auth-guarded.
 *
 * Env (via wrangler secret put unless noted):
 *   RESEND_API_KEY                    required, transactional sends.
 *   FROM_ADDR                         required, e.g. "Mortgage Rates <…>".
 *   SITE_NAME                         required, appears in confirmation email.
 *   ALLOWED_ORIGINS                   required (vars), comma-separated origins
 *                                     allowed to POST /.
 *   CONFIRMATION_SENT_URL             required (vars).
 *   BLOCKED_URL                       required (vars).
 *   UNSUBSCRIBED_URL                  required (vars), landing after /unsubscribe.
 *   SUCCESS_URL_CA                    required (vars).
 *   SUCCESS_URL_US                    required (vars).
 *   ERROR_URL / ERROR_URL_CA / ERROR_URL_US  optional (vars).
 *   BROADCAST_AUTH_KEY                required (secret), gates POST /broadcast.
 *
 * Bindings (wrangler.toml):
 *   SUBSCRIBE_KV                      KV: pending confirmation tokens,
 *                                     rate-limit markers, permanent blocks.
 *   AUDIT_DB                          D1: subscribe_logs, subscribers, sends.
 */

export interface Env {
  RESEND_API_KEY: string;
  FROM_ADDR: string;
  SITE_NAME: string;
  ALLOWED_ORIGINS: string;
  CONFIRMATION_SENT_URL: string;
  BLOCKED_URL: string;
  UNSUBSCRIBED_URL: string;
  SUCCESS_URL_CA: string;
  SUCCESS_URL_US: string;
  ERROR_URL?: string;
  BROADCAST_AUTH_KEY: string;
  SUBSCRIBE_KV: KVNamespace;
  AUDIT_DB: D1Database;
  [key: string]: string | KVNamespace | D1Database | undefined;
}

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const TOKEN_RE = /^[a-f0-9]{64}$/;
const TOKEN_TTL_SECONDS = 60 * 60 * 24;        // 24h
const RATE_LIMIT_TTL_SECONDS = 60 * 60 * 24;   // 24h
const BLOCK_TTL_SECONDS = 10 * 365 * 24 * 60 * 60; // 10y, effectively permanent
const RESEND_BATCH_CHUNK = 100;                // Resend's batch-endpoint limit

const LIST_LABELS: Record<string, string> = {
  ca: "Canadian mortgage prime rate",
  us: "US mortgage prime rate",
};

const CONFIRMATION_TEMPLATE = {
  subject: "Confirm your subscription to {site_name} ({list_label})",
  html:
    `<p>Someone (hopefully you) used the subscribe form on ` +
    `<strong>{site_name}</strong> to sign up <strong>{email}</strong> ` +
    `for <strong>{list_label}</strong> updates.</p>` +
    `<p>You will only receive an email when the rate actually changes — ` +
    `not a daily digest, not marketing.</p>` +
    `<p><a href="{confirm_url}" style="display:inline-block;padding:10px 16px;` +
    `background:#0b5394;color:#ffffff;text-decoration:none;border-radius:6px;">` +
    `Confirm subscription</a></p>` +
    `<p style="font-size:13px;color:#666;">Or paste this link into your browser: ` +
    `<br><a href="{confirm_url}">{confirm_url}</a></p>` +
    `<p>The confirm link is valid for 24 hours. If you don't click it, nothing happens.</p>` +
    `<hr style="margin:24px 0;border:none;border-top:1px solid #ddd;">` +
    `<p style="font-size:13px;color:#666;">Did not request this? ` +
    `<a href="{block_url}">Block this address permanently</a> and we will never ` +
    `contact you again, even if someone tries to subscribe you later.</p>`,
  text:
    `Someone (hopefully you) used the subscribe form on {site_name} to sign up ` +
    `{email} for {list_label} updates.\n\n` +
    `You will only receive an email when the rate actually changes — not a daily ` +
    `digest, not marketing.\n\n` +
    `Confirm your subscription:\n{confirm_url}\n\n` +
    `The confirm link is valid for 24 hours. If you don't click it, nothing happens.\n\n` +
    `Did not request this? Block this address permanently, and we will never\n` +
    `contact you again even if someone tries to subscribe you later:\n{block_url}\n`,
};

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------
function wantsJson(req: Request): boolean {
  return (req.headers.get("accept") || "").toLowerCase().includes("application/json");
}

function originAllowed(req: Request, env: Env): boolean {
  const allowed = (env.ALLOWED_ORIGINS || "")
    .split(",").map((s) => s.trim()).filter(Boolean);
  if (allowed.length === 0) return false;
  const origin = req.headers.get("origin") || "";
  const referer = req.headers.get("referer") || "";
  for (const a of allowed) {
    if (origin === a) return true;
    if (referer.startsWith(a)) return true;
  }
  return false;
}

async function parseSubscribeBody(req: Request): Promise<{ email: string; list: string } | null> {
  const ct = (req.headers.get("content-type") || "").toLowerCase();
  let email = "";
  let list = "";
  if (ct.includes("application/json")) {
    try {
      const body = (await req.json()) as Record<string, unknown>;
      email = String(body.email ?? "").trim();
      list = String(body.list ?? "").trim();
    } catch {
      return null;
    }
  } else {
    const form = await req.formData();
    email = String(form.get("email") ?? "").trim();
    list = String(form.get("list") ?? "").trim();
  }
  if (!EMAIL_RE.test(email)) return null;
  const cleanList = list.replace(/[^A-Za-z0-9_]/g, "").slice(0, 32).toLowerCase();
  if (cleanList !== "ca" && cleanList !== "us") return null;
  return { email: email.toLowerCase(), list: cleanList };
}

function resolveUrl(env: Env, base: string, list: string): string {
  if (list) {
    const key = `${base}_${list.toUpperCase()}`;
    const specific = env[key];
    if (typeof specific === "string" && specific) return specific;
  }
  const fallback = env[base];
  return typeof fallback === "string" ? fallback : "";
}

function generateToken(): string {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return Array.from(bytes).map((b) => b.toString(16).padStart(2, "0")).join("");
}

function fillTemplate(tpl: string, vars: Record<string, string>): string {
  return tpl.replace(/{(\w+)}/g, (_, k) => vars[k] ?? `{${k}}`);
}

/** Constant-time string equality for auth checks. */
function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let mismatch = 0;
  for (let i = 0; i < a.length; i++) {
    mismatch |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return mismatch === 0;
}

function utcDateStr(d: Date = new Date()): string {
  return d.toISOString().slice(0, 10);
}

async function logEvent(
  env: Env,
  req: Request,
  event: string,
  outcome: string,
  fields: { email?: string | null; list?: string | null; tokenPrefix?: string | null } = {},
): Promise<void> {
  const cf = ((req as unknown) as { cf?: { country?: string } }).cf || {};
  try {
    await env.AUDIT_DB.prepare(
      `INSERT INTO subscribe_logs
         (ts, event, outcome, email, list, ip, country, user_agent, referer, token_prefix)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    ).bind(
      new Date().toISOString(),
      event,
      outcome,
      fields.email ?? null,
      fields.list ?? null,
      req.headers.get("cf-connecting-ip") ?? null,
      cf.country ?? null,
      req.headers.get("user-agent") ?? null,
      req.headers.get("referer") ?? null,
      fields.tokenPrefix ?? null,
    ).run();
  } catch (e) {
    console.error("audit log failed:", e);
  }
}

function respondRedirect(req: Request, url: string, message: string, ok: boolean): Response {
  if (wantsJson(req)) {
    return new Response(JSON.stringify({ ok, message, redirect: url }), {
      status: ok ? 200 : 400,
      headers: { "Content-Type": "application/json" },
    });
  }
  return Response.redirect(url, 303);
}

async function sendConfirmationEmail(
  env: Env,
  to: string,
  confirmUrl: string,
  blockUrl: string,
  list: string,
): Promise<boolean> {
  const vars = {
    email: to,
    confirm_url: confirmUrl,
    block_url: blockUrl,
    site_name: env.SITE_NAME,
    list_label: LIST_LABELS[list] || list,
  };
  const payload = {
    from: env.FROM_ADDR,
    to,
    subject: fillTemplate(CONFIRMATION_TEMPLATE.subject, vars),
    html: fillTemplate(CONFIRMATION_TEMPLATE.html, vars),
    text: fillTemplate(CONFIRMATION_TEMPLATE.text, vars),
  };
  const resp = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${env.RESEND_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
  return resp.status >= 200 && resp.status < 300;
}

// ---------------------------------------------------------------------------
// POST /  — subscribe
// ---------------------------------------------------------------------------
async function handleSubscribe(req: Request, env: Env): Promise<Response> {
  if (!originAllowed(req, env)) {
    await logEvent(env, req, "subscribe_attempt", "bad_origin", {});
    return new Response("Forbidden", { status: 403 });
  }

  const parsed = await parseSubscribeBody(req);
  if (!parsed) {
    await logEvent(env, req, "subscribe_attempt", "invalid_email_or_list", {});
    const errorUrl = resolveUrl(env, "ERROR_URL", "")
      || `${resolveUrl(env, "CONFIRMATION_SENT_URL", "")}?err=1`;
    return respondRedirect(req, errorUrl, "Invalid email or list", false);
  }

  const list = parsed.list;
  const confirmationSentUrl = resolveUrl(env, "CONFIRMATION_SENT_URL", list);
  const errorUrl = resolveUrl(env, "ERROR_URL", list) || `${confirmationSentUrl}?err=1`;

  const blockKey = `block:${parsed.email}`;
  if (await env.SUBSCRIBE_KV.get(blockKey)) {
    await logEvent(env, req, "subscribe_attempt", "blocked", {
      email: parsed.email, list,
    });
    return respondRedirect(req, confirmationSentUrl, "Already pending", true);
  }

  const rateKey = `rl:${parsed.email}`;
  if (await env.SUBSCRIBE_KV.get(rateKey)) {
    await logEvent(env, req, "subscribe_attempt", "rate_limited", {
      email: parsed.email, list,
    });
    return respondRedirect(req, confirmationSentUrl, "Already pending", true);
  }

  const token = generateToken();
  const tokenPrefix = token.slice(0, 8);
  const record = JSON.stringify({
    email: parsed.email,
    list: parsed.list,
    created_at: Date.now(),
  });
  await env.SUBSCRIBE_KV.put(`token:${token}`, record, { expirationTtl: TOKEN_TTL_SECONDS });
  await env.SUBSCRIBE_KV.put(rateKey, "1", { expirationTtl: RATE_LIMIT_TTL_SECONDS });

  const workerUrl = new URL(req.url);
  const confirmUrl = `${workerUrl.origin}/confirm?token=${token}`;
  const blockUrl = `${workerUrl.origin}/block?token=${token}`;

  const sent = await sendConfirmationEmail(env, parsed.email, confirmUrl, blockUrl, parsed.list);
  if (!sent) {
    await logEvent(env, req, "subscribe_attempt", "send_failed", {
      email: parsed.email, list, tokenPrefix,
    });
    return respondRedirect(req, errorUrl, "Send failed", false);
  }

  await logEvent(env, req, "subscribe_attempt", "pending", {
    email: parsed.email, list, tokenPrefix,
  });
  return respondRedirect(req, confirmationSentUrl, "Confirmation sent", true);
}

// ---------------------------------------------------------------------------
// GET /confirm?token=…  — finalize subscription → insert into `subscribers`
// ---------------------------------------------------------------------------
async function handleConfirm(req: Request, env: Env): Promise<Response> {
  const url = new URL(req.url);
  const token = url.searchParams.get("token") || "";
  const tokenPrefix = token.slice(0, 8) || null;
  if (!TOKEN_RE.test(token)) {
    await logEvent(env, req, "confirm_attempt", "bad_token", { tokenPrefix });
    return Response.redirect(
      resolveUrl(env, "ERROR_URL", "") || `${resolveUrl(env, "SUCCESS_URL_CA", "")}?err=bad_token`,
      303,
    );
  }
  const tokenKey = `token:${token}`;
  const raw = await env.SUBSCRIBE_KV.get(tokenKey);
  if (!raw) {
    await logEvent(env, req, "confirm_attempt", "expired_token", { tokenPrefix });
    return Response.redirect(
      resolveUrl(env, "ERROR_URL", "") || `${resolveUrl(env, "SUCCESS_URL_CA", "")}?err=expired`,
      303,
    );
  }
  let record: { email: string; list?: string };
  try {
    record = JSON.parse(raw);
  } catch {
    await env.SUBSCRIBE_KV.delete(tokenKey);
    await logEvent(env, req, "confirm_attempt", "bad_token", { tokenPrefix });
    return Response.redirect(
      resolveUrl(env, "ERROR_URL", "") || `${resolveUrl(env, "SUCCESS_URL_CA", "")}?err=bad_token`,
      303,
    );
  }

  const list = record.list || "ca";
  const successUrl = resolveUrl(env, "SUCCESS_URL", list);
  const errorUrl = resolveUrl(env, "ERROR_URL", list) || `${successUrl}?err=1`;

  // Insert into D1 subscribers. If already present (unique constraint on
  // (email, list)) that's still a success — user is re-confirming.
  const unsubToken = generateToken();
  try {
    await env.AUDIT_DB.prepare(
      `INSERT OR IGNORE INTO subscribers (email, list, confirmed_at, unsub_token)
       VALUES (?, ?, ?, ?)`,
    ).bind(record.email, list, new Date().toISOString(), unsubToken).run();
  } catch (e) {
    console.error("confirm insert failed:", e);
    await logEvent(env, req, "confirm_attempt", "db_error", {
      email: record.email, list, tokenPrefix,
    });
    return Response.redirect(errorUrl, 303);
  }

  await env.SUBSCRIBE_KV.delete(tokenKey);
  await env.SUBSCRIBE_KV.delete(`rl:${record.email}`);

  await logEvent(env, req, "confirm_attempt", "confirmed", {
    email: record.email, list, tokenPrefix,
  });
  return Response.redirect(successUrl, 303);
}

// ---------------------------------------------------------------------------
// GET /block?token=…  — "never email me again" link in confirmation email
// ---------------------------------------------------------------------------
async function handleBlock(req: Request, env: Env): Promise<Response> {
  const url = new URL(req.url);
  const token = url.searchParams.get("token") || "";
  const tokenPrefix = token.slice(0, 8) || null;
  if (!TOKEN_RE.test(token)) {
    await logEvent(env, req, "block_attempt", "bad_token", { tokenPrefix });
    return Response.redirect(
      resolveUrl(env, "ERROR_URL", "") || `${resolveUrl(env, "BLOCKED_URL", "")}?err=bad_token`,
      303,
    );
  }
  const tokenKey = `token:${token}`;
  const raw = await env.SUBSCRIBE_KV.get(tokenKey);
  if (!raw) {
    await logEvent(env, req, "block_attempt", "expired_token", { tokenPrefix });
    return Response.redirect(resolveUrl(env, "BLOCKED_URL", ""), 303);
  }
  let record: { email: string; list?: string };
  try {
    record = JSON.parse(raw);
  } catch {
    await env.SUBSCRIBE_KV.delete(tokenKey);
    await logEvent(env, req, "block_attempt", "bad_token", { tokenPrefix });
    return Response.redirect(
      resolveUrl(env, "ERROR_URL", "") || `${resolveUrl(env, "BLOCKED_URL", "")}?err=bad_token`,
      303,
    );
  }

  const list = record.list || "";
  await env.SUBSCRIBE_KV.put(`block:${record.email}`, "1", { expirationTtl: BLOCK_TTL_SECONDS });
  await env.SUBSCRIBE_KV.delete(tokenKey);
  await env.SUBSCRIBE_KV.delete(`rl:${record.email}`);
  // Also remove any existing subscriptions (defense in depth).
  await env.AUDIT_DB.prepare(`DELETE FROM subscribers WHERE email = ?`)
    .bind(record.email).run();

  await logEvent(env, req, "block_attempt", "blocked", {
    email: record.email, list, tokenPrefix,
  });
  return Response.redirect(resolveUrl(env, "BLOCKED_URL", list), 303);
}

// ---------------------------------------------------------------------------
// GET/POST /unsubscribe?token=…  — one-click unsub from a broadcast email
// ---------------------------------------------------------------------------
async function handleUnsubscribe(req: Request, env: Env): Promise<Response> {
  const url = new URL(req.url);
  const token = url.searchParams.get("token") || "";
  const tokenPrefix = token.slice(0, 8) || null;
  if (!TOKEN_RE.test(token)) {
    await logEvent(env, req, "unsubscribe_attempt", "bad_token", { tokenPrefix });
    // RFC 8058 compliance: POST requests must return 2xx on success/no-op
    // so Gmail/Outlook don't retry and flag us.
    if (req.method === "POST") {
      return new Response("ok", { status: 200 });
    }
    return Response.redirect(env.UNSUBSCRIBED_URL, 303);
  }

  const row = await env.AUDIT_DB.prepare(
    `SELECT email, list FROM subscribers WHERE unsub_token = ? LIMIT 1`,
  ).bind(token).first<{ email: string; list: string }>();

  if (!row) {
    await logEvent(env, req, "unsubscribe_attempt", "not_found", { tokenPrefix });
    if (req.method === "POST") {
      return new Response("ok", { status: 200 });
    }
    return Response.redirect(env.UNSUBSCRIBED_URL, 303);
  }

  try {
    await env.AUDIT_DB.prepare(`DELETE FROM subscribers WHERE unsub_token = ?`)
      .bind(token).run();
    await env.SUBSCRIBE_KV.put(`block:${row.email}`, "1",
      { expirationTtl: BLOCK_TTL_SECONDS });
  } catch (e) {
    console.error("unsubscribe failed:", e);
  }

  await logEvent(env, req, "unsubscribe_attempt", "unsubscribed", {
    email: row.email, list: row.list, tokenPrefix,
  });

  if (req.method === "POST") {
    return new Response("ok", { status: 200 });
  }
  return Response.redirect(env.UNSUBSCRIBED_URL, 303);
}

// ---------------------------------------------------------------------------
// POST /broadcast  — send a broadcast to all confirmed subscribers for a list
// ---------------------------------------------------------------------------
interface BroadcastRequest {
  list: string;
  subject: string;
  html: string;
  text: string;
}

async function handleBroadcast(req: Request, env: Env): Promise<Response> {
  const auth = req.headers.get("authorization") || "";
  const expected = `Bearer ${env.BROADCAST_AUTH_KEY}`;
  if (!env.BROADCAST_AUTH_KEY || !safeEqual(auth, expected)) {
    await logEvent(env, req, "broadcast_attempt", "bad_auth");
    return new Response(JSON.stringify({ error: "forbidden" }), {
      status: 403, headers: { "Content-Type": "application/json" },
    });
  }

  let body: BroadcastRequest;
  try {
    body = await req.json() as BroadcastRequest;
  } catch {
    await logEvent(env, req, "broadcast_attempt", "bad_body");
    return new Response(JSON.stringify({ error: "bad_body" }), {
      status: 400, headers: { "Content-Type": "application/json" },
    });
  }

  const list = (body.list || "").toLowerCase();
  if (list !== "ca" && list !== "us") {
    await logEvent(env, req, "broadcast_attempt", "bad_list", { list });
    return new Response(JSON.stringify({ error: "bad_list" }), {
      status: 400, headers: { "Content-Type": "application/json" },
    });
  }
  if (!body.subject || !body.html || !body.text) {
    await logEvent(env, req, "broadcast_attempt", "missing_content", { list });
    return new Response(JSON.stringify({ error: "missing_content" }), {
      status: 400, headers: { "Content-Type": "application/json" },
    });
  }

  const workerOrigin = new URL(req.url).origin;
  const today = utcDateStr();

  const rows = await env.AUDIT_DB.prepare(
    `SELECT email, unsub_token FROM subscribers WHERE list = ?`,
  ).bind(list).all<{ email: string; unsub_token: string }>();
  const candidates = rows.results || [];

  let sent = 0;
  let skippedDedup = 0;
  let failed = 0;

  // Pre-filter using per-day dedup: INSERT OR IGNORE; accept rows whose
  // insert actually produced a new row (changes() === 1). D1 serializes
  // these inserts, so there is no race.
  const accepted: { email: string; unsub_token: string }[] = [];
  for (const row of candidates) {
    const result = await env.AUDIT_DB.prepare(
      `INSERT OR IGNORE INTO sends (email, list, date_sent) VALUES (?, ?, ?)`,
    ).bind(row.email, list, today).run();
    // meta.changes === 1 when a new row was inserted, 0 when blocked by unique.
    const changes = (result.meta && (result.meta.changes ?? 0)) || 0;
    if (changes > 0) {
      accepted.push(row);
    } else {
      skippedDedup++;
    }
  }

  if (accepted.length === 0) {
    await logEvent(env, req, "broadcast_attempt", "nothing_to_send", { list });
    return new Response(
      JSON.stringify({ sent: 0, skipped_dedup: skippedDedup, failed: 0 }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  }

  // Batch-send via Resend /emails/batch in chunks.
  for (let i = 0; i < accepted.length; i += RESEND_BATCH_CHUNK) {
    const chunk = accepted.slice(i, i + RESEND_BATCH_CHUNK);
    const envelopes = chunk.map((row) => {
      const unsubUrl = `${workerOrigin}/unsubscribe?token=${row.unsub_token}`;
      return {
        from: env.FROM_ADDR,
        to: [row.email],
        subject: body.subject,
        html: body.html.replace(/{{UNSUB_URL}}/g, unsubUrl),
        text: body.text.replace(/{{UNSUB_URL}}/g, unsubUrl),
        headers: {
          "List-Unsubscribe": `<${unsubUrl}>`,
          "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
      };
    });

    try {
      const resp = await fetch("https://api.resend.com/emails/batch", {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${env.RESEND_API_KEY}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(envelopes),
      });
      if (resp.status >= 200 && resp.status < 300) {
        sent += chunk.length;
        await logEvent(env, req, "broadcast_attempt", "sent_chunk", { list });
      } else {
        failed += chunk.length;
        // Roll back the sends rows for this chunk so the recipient can
        // still receive this change the next day (or on retry, but per-day
        // dedup still holds for the current UTC day).
        for (const row of chunk) {
          await env.AUDIT_DB.prepare(
            `DELETE FROM sends WHERE email = ? AND list = ? AND date_sent = ?`,
          ).bind(row.email, list, today).run();
        }
        await logEvent(env, req, "broadcast_attempt", "resend_error", { list });
      }
    } catch (e) {
      console.error("resend batch failed:", e);
      failed += chunk.length;
      for (const row of chunk) {
        await env.AUDIT_DB.prepare(
          `DELETE FROM sends WHERE email = ? AND list = ? AND date_sent = ?`,
        ).bind(row.email, list, today).run();
      }
      await logEvent(env, req, "broadcast_attempt", "fetch_error", { list });
    }
  }

  return new Response(
    JSON.stringify({ sent, skipped_dedup: skippedDedup, failed }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------
export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);
    if (req.method === "POST" && url.pathname === "/") {
      return handleSubscribe(req, env);
    }
    if (req.method === "GET" && url.pathname === "/confirm") {
      return handleConfirm(req, env);
    }
    if (req.method === "GET" && url.pathname === "/block") {
      return handleBlock(req, env);
    }
    if (url.pathname === "/unsubscribe"
        && (req.method === "GET" || req.method === "POST")) {
      return handleUnsubscribe(req, env);
    }
    if (req.method === "POST" && url.pathname === "/broadcast") {
      return handleBroadcast(req, env);
    }
    return new Response("Not Found", { status: 404 });
  },
};
