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
 *   POST /                            subscribe: accepts email + lists (ca/us, one or more)
 *                                     sends a single double-opt-in email.
 *   GET  /confirm?token=…             confirmation click: writes one subscribers
 *                                     row per selected list with a fresh unsub_token.
 *   GET  /block?token=…               "never email me again" link from confirmation
 *                                     email: permanent block of the address.
 *   GET  /unsubscribe?token=…         one-click unsub from a broadcast email —
 *                                     removes the one list this email was for.
 *   POST /unsubscribe                 same as GET, for RFC 8058 list-unsub-post.
 *   GET  /preferences?token=…         full preference center: shows every list
 *                                     this address is subscribed to with
 *                                     checkboxes; user picks what to keep.
 *   POST /preferences                 saves the selected checkboxes — deletes
 *                                     rows for lists the user unchecked.
 *   POST /broadcast                   GH Actions → worker: sends a short email
 *                                     to every confirmed subscriber for a list,
 *                                     with per-day dedup. Auth-guarded.
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
const TOKEN_TTL_SECONDS = 60 * 60 * 24;
const RATE_LIMIT_TTL_SECONDS = 60 * 60 * 24;
const BLOCK_TTL_SECONDS = 10 * 365 * 24 * 60 * 60;
const RESEND_BATCH_CHUNK = 100;
const VALID_LISTS = new Set(["ca", "us"]);

const LIST_LABELS: Record<string, string> = {
  ca: "Canadian mortgage prime rate",
  us: "US mortgage prime rate",
};

const SITE_HOME = "https://k1monfared.github.io/mortgage_rate_tracker/";

const CONFIRMATION_TEMPLATE = {
  subject: "Confirm your subscription to {site_name}",
  html:
    `<p>Someone (hopefully you) used the subscribe form on ` +
    `<strong>{site_name}</strong> to sign up <strong>{email}</strong> ` +
    `for updates to {list_labels}.</p>` +
    `<p>You will only receive an email when a rate actually changes — ` +
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
    `{email} for updates to {list_labels}.\n\n` +
    `You will only receive an email when a rate actually changes — not a daily ` +
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

function normalizeLists(raw: string[]): string[] {
  const cleaned = raw
    .map((s) => s.trim().replace(/[^A-Za-z0-9_]/g, "").slice(0, 32).toLowerCase())
    .filter((s) => VALID_LISTS.has(s));
  return Array.from(new Set(cleaned));
}

async function parseSubscribeBody(req: Request): Promise<{ email: string; lists: string[] } | null> {
  const ct = (req.headers.get("content-type") || "").toLowerCase();
  let email = "";
  let rawListsTokens: string[] = [];
  if (ct.includes("application/json")) {
    try {
      const body = (await req.json()) as Record<string, unknown>;
      email = String(body.email ?? "").trim();
      if (Array.isArray(body.lists)) {
        rawListsTokens = body.lists.map(String);
      } else if (typeof body.lists === "string") {
        rawListsTokens = (body.lists as string).split(",");
      } else if (typeof body.list === "string") {
        rawListsTokens = (body.list as string).split(",");
      }
    } catch {
      return null;
    }
  } else {
    const form = await req.formData();
    email = String(form.get("email") ?? "").trim();
    const multiLists = form.getAll("lists").map(String);
    const multiList  = form.getAll("list").map(String);
    const combined = [...multiLists, ...multiList];
    rawListsTokens = [];
    for (const v of combined) {
      for (const part of v.split(",")) rawListsTokens.push(part);
    }
  }
  if (!EMAIL_RE.test(email)) return null;
  const lists = normalizeLists(rawListsTokens);
  if (lists.length === 0) return null;
  return { email: email.toLowerCase(), lists };
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

function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let mismatch = 0;
  for (let i = 0; i < a.length; i++) mismatch |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return mismatch === 0;
}

function utcDateStr(d: Date = new Date()): string {
  return d.toISOString().slice(0, 10);
}

function formatListLabels(lists: string[]): string {
  const labels = lists.map((l) => LIST_LABELS[l] || l);
  if (labels.length <= 1) return labels[0] || "";
  if (labels.length === 2) return `${labels[0]} and ${labels[1]}`;
  return `${labels.slice(0, -1).join(", ")}, and ${labels[labels.length - 1]}`;
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
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
  lists: string[],
): Promise<boolean> {
  const vars = {
    email: to,
    confirm_url: confirmUrl,
    block_url: blockUrl,
    site_name: env.SITE_NAME,
    list_labels: formatListLabels(lists),
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

  // Use the first list for per-language redirect resolution.
  const primaryList = parsed.lists[0];
  const confirmationSentUrl = resolveUrl(env, "CONFIRMATION_SENT_URL", primaryList);
  const errorUrl = resolveUrl(env, "ERROR_URL", primaryList) || `${confirmationSentUrl}?err=1`;

  const blockKey = `block:${parsed.email}`;
  if (await env.SUBSCRIBE_KV.get(blockKey)) {
    await logEvent(env, req, "subscribe_attempt", "blocked", {
      email: parsed.email, list: parsed.lists.join(","),
    });
    return respondRedirect(req, confirmationSentUrl, "Already pending", true);
  }

  const rateKey = `rl:${parsed.email}`;
  if (await env.SUBSCRIBE_KV.get(rateKey)) {
    await logEvent(env, req, "subscribe_attempt", "rate_limited", {
      email: parsed.email, list: parsed.lists.join(","),
    });
    return respondRedirect(req, confirmationSentUrl, "Already pending", true);
  }

  const token = generateToken();
  const tokenPrefix = token.slice(0, 8);
  const record = JSON.stringify({
    email: parsed.email,
    lists: parsed.lists,
    created_at: Date.now(),
  });
  await env.SUBSCRIBE_KV.put(`token:${token}`, record, { expirationTtl: TOKEN_TTL_SECONDS });
  await env.SUBSCRIBE_KV.put(rateKey, "1", { expirationTtl: RATE_LIMIT_TTL_SECONDS });

  const workerUrl = new URL(req.url);
  const confirmUrl = `${workerUrl.origin}/confirm?token=${token}`;
  const blockUrl = `${workerUrl.origin}/block?token=${token}`;

  const sent = await sendConfirmationEmail(env, parsed.email, confirmUrl, blockUrl, parsed.lists);
  if (!sent) {
    await logEvent(env, req, "subscribe_attempt", "send_failed", {
      email: parsed.email, list: parsed.lists.join(","), tokenPrefix,
    });
    return respondRedirect(req, errorUrl, "Send failed", false);
  }

  await logEvent(env, req, "subscribe_attempt", "pending", {
    email: parsed.email, list: parsed.lists.join(","), tokenPrefix,
  });
  return respondRedirect(req, confirmationSentUrl, "Confirmation sent", true);
}

// ---------------------------------------------------------------------------
// GET /confirm?token=…  — finalize subscription → insert one row per list
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
  let record: { email: string; lists?: string[]; list?: string };
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

  // Back-compat: older records had `list` (single).
  const lists: string[] = record.lists
    || (record.list ? [record.list] : []);
  const validLists = lists.filter((l) => VALID_LISTS.has(l));
  if (validLists.length === 0) {
    await logEvent(env, req, "confirm_attempt", "bad_token", { tokenPrefix });
    return Response.redirect(
      resolveUrl(env, "ERROR_URL", "") || `${resolveUrl(env, "SUCCESS_URL_CA", "")}?err=bad_token`,
      303,
    );
  }

  const primaryList = validLists[0];
  const successUrl = resolveUrl(env, "SUCCESS_URL", primaryList);
  const errorUrl = resolveUrl(env, "ERROR_URL", primaryList) || `${successUrl}?err=1`;

  try {
    const nowIso = new Date().toISOString();
    for (const list of validLists) {
      const unsubToken = generateToken();
      await env.AUDIT_DB.prepare(
        `INSERT OR IGNORE INTO subscribers (email, list, confirmed_at, unsub_token)
         VALUES (?, ?, ?, ?)`,
      ).bind(record.email, list, nowIso, unsubToken).run();
    }
  } catch (e) {
    console.error("confirm insert failed:", e);
    await logEvent(env, req, "confirm_attempt", "db_error", {
      email: record.email, list: validLists.join(","), tokenPrefix,
    });
    return Response.redirect(errorUrl, 303);
  }

  await env.SUBSCRIBE_KV.delete(tokenKey);
  await env.SUBSCRIBE_KV.delete(`rl:${record.email}`);

  await logEvent(env, req, "confirm_attempt", "confirmed", {
    email: record.email, list: validLists.join(","), tokenPrefix,
  });
  return Response.redirect(successUrl, 303);
}

// ---------------------------------------------------------------------------
// GET /block?token=…  — "never email me again" from confirmation email
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
  let record: { email: string; lists?: string[]; list?: string };
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

  await env.SUBSCRIBE_KV.put(`block:${record.email}`, "1", { expirationTtl: BLOCK_TTL_SECONDS });
  await env.SUBSCRIBE_KV.delete(tokenKey);
  await env.SUBSCRIBE_KV.delete(`rl:${record.email}`);
  await env.AUDIT_DB.prepare(`DELETE FROM subscribers WHERE email = ?`)
    .bind(record.email).run();

  const listStr = (record.lists || (record.list ? [record.list] : [])).join(",");
  await logEvent(env, req, "block_attempt", "blocked", {
    email: record.email, list: listStr, tokenPrefix,
  });
  return Response.redirect(resolveUrl(env, "BLOCKED_URL", ""), 303);
}

// ---------------------------------------------------------------------------
// GET/POST /unsubscribe?token=…  — one-click unsub from ONE list
// ---------------------------------------------------------------------------
async function handleUnsubscribe(req: Request, env: Env): Promise<Response> {
  const url = new URL(req.url);
  const token = url.searchParams.get("token") || "";
  const tokenPrefix = token.slice(0, 8) || null;
  const okResponse = () =>
    req.method === "POST"
      ? new Response("ok", { status: 200 })
      : Response.redirect(env.UNSUBSCRIBED_URL, 303);

  if (!TOKEN_RE.test(token)) {
    await logEvent(env, req, "unsubscribe_attempt", "bad_token", { tokenPrefix });
    return okResponse();
  }

  const row = await env.AUDIT_DB.prepare(
    `SELECT email, list FROM subscribers WHERE unsub_token = ? LIMIT 1`,
  ).bind(token).first<{ email: string; list: string }>();

  if (!row) {
    await logEvent(env, req, "unsubscribe_attempt", "not_found", { tokenPrefix });
    return okResponse();
  }

  try {
    await env.AUDIT_DB.prepare(`DELETE FROM subscribers WHERE unsub_token = ?`)
      .bind(token).run();
    // Only blocks future subscribe attempts for THIS address if they also
    // requested a hard block. A per-list unsubscribe should not nuke their
    // other subscriptions — so we don't write to the block: KV here.
  } catch (e) {
    console.error("unsubscribe failed:", e);
  }

  await logEvent(env, req, "unsubscribe_attempt", "unsubscribed", {
    email: row.email, list: row.list, tokenPrefix,
  });

  return okResponse();
}

// ---------------------------------------------------------------------------
// GET/POST /preferences?token=…  — manage all of one address's subscriptions
// ---------------------------------------------------------------------------
function preferencesPage(token: string, email: string, currentLists: string[], savedNote: string | null): string {
  const tokenEsc = escapeHtml(token);
  const emailEsc = escapeHtml(email);
  const checkboxRows = Array.from(VALID_LISTS).map((list) => {
    const label = LIST_LABELS[list] || list;
    const checked = currentLists.includes(list) ? "checked" : "";
    return `<label class="row">
      <input type="checkbox" name="keep" value="${list}" ${checked}>
      <span>${escapeHtml(label)}</span>
    </label>`;
  }).join("\n");

  const savedBanner = savedNote
    ? `<p class="saved">${escapeHtml(savedNote)}</p>`
    : "";

  const currentNote = currentLists.length === 0
    ? `<p class="muted">You currently have no active subscriptions.</p>`
    : `<p class="muted">Uncheck any list you want to stop receiving emails for, then click Save.</p>`;

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Email preferences · Mortgage Rate Tracker</title>
  <style>
    :root { color-scheme: dark light; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #1a1a2e; color: #e0e0e0;
      margin: 0; padding: 40px 20px;
      min-height: 100vh;
    }
    .box {
      background: #16213e; border: 1px solid #30363d;
      border-radius: 12px; padding: 32px;
      max-width: 560px; margin: 24px auto;
      box-shadow: 0 1px 4px rgba(0,0,0,.25);
    }
    h1 { margin: 0 0 6px; font-size: 1.4rem; }
    .email { font-size: 13px; color: #8b949e; margin: 0 0 20px; }
    .muted { font-size: 13px; color: #8b949e; line-height: 1.55; margin: 0 0 16px; }
    .saved {
      background: rgba(100,200,160,0.12); border: 1px solid rgba(100,200,160,0.35);
      color: #7ee5b1; padding: 10px 14px; border-radius: 6px; font-size: 14px;
      margin: 0 0 16px;
    }
    .row {
      display: flex; align-items: center; gap: 10px;
      padding: 12px 14px; margin: 6px 0;
      background: #1a1a2e; border: 1px solid #30363d; border-radius: 8px;
      cursor: pointer;
    }
    .row input { margin: 0; width: 18px; height: 18px; cursor: pointer; }
    .row span { font-size: 15px; }
    button {
      display: inline-block; padding: 10px 20px; font-size: 14px; font-weight: 500;
      background: #58a6ff; color: #0d1117; border: none; border-radius: 6px;
      cursor: pointer; margin-top: 10px;
    }
    button:hover { opacity: .9; }
    a { color: #58a6ff; text-decoration: none; }
    a:hover { text-decoration: underline; }
    .footer { text-align: center; font-size: 11px; color: #8b949e; margin-top: 24px; opacity: .85; }
  </style>
</head>
<body>
  <div class="box">
    <h1>Email preferences</h1>
    <p class="email">For: ${emailEsc}</p>
    ${savedBanner}
    ${currentNote}
    <form method="POST" action="/preferences">
      <input type="hidden" name="token" value="${tokenEsc}">
      ${checkboxRows}
      <button type="submit">Save changes</button>
    </form>
    <p class="muted" style="margin-top: 20px;">
      <a href="${SITE_HOME}">&larr; Back to the tracker</a>
    </p>
  </div>
  <div class="footer">
    <a href="https://github.com/k1monfared/mortgage_rate_tracker">GitHub</a>
    &middot; <a href="https://k1monfared.github.io/sponsor.html">Sponsor</a>
  </div>
</body>
</html>`;
}

async function lookupByToken(env: Env, token: string): Promise<{ email: string } | null> {
  if (!TOKEN_RE.test(token)) return null;
  return await env.AUDIT_DB.prepare(
    `SELECT email FROM subscribers WHERE unsub_token = ? LIMIT 1`,
  ).bind(token).first<{ email: string }>();
}

async function listsForEmail(env: Env, email: string): Promise<string[]> {
  const rows = await env.AUDIT_DB.prepare(
    `SELECT list FROM subscribers WHERE email = ?`,
  ).bind(email).all<{ list: string }>();
  return (rows.results || []).map((r) => r.list);
}

async function handleGetPreferences(req: Request, env: Env): Promise<Response> {
  const url = new URL(req.url);
  const token = url.searchParams.get("token") || "";
  const tokenPrefix = token.slice(0, 8) || null;

  const owner = await lookupByToken(env, token);
  if (!owner) {
    await logEvent(env, req, "preferences_attempt", "bad_or_expired_token", { tokenPrefix });
    return Response.redirect(env.UNSUBSCRIBED_URL, 303);
  }
  const lists = await listsForEmail(env, owner.email);
  await logEvent(env, req, "preferences_attempt", "viewed", {
    email: owner.email, tokenPrefix,
  });
  return new Response(preferencesPage(token, owner.email, lists, null), {
    status: 200,
    headers: { "Content-Type": "text/html; charset=utf-8" },
  });
}

async function handlePostPreferences(req: Request, env: Env): Promise<Response> {
  const form = await req.formData();
  const token = String(form.get("token") || "");
  const tokenPrefix = token.slice(0, 8) || null;
  const keepRaw = form.getAll("keep").map(String);
  const keep = normalizeLists(keepRaw);

  const owner = await lookupByToken(env, token);
  if (!owner) {
    await logEvent(env, req, "preferences_attempt", "bad_or_expired_token", { tokenPrefix });
    return Response.redirect(env.UNSUBSCRIBED_URL, 303);
  }

  const current = await listsForEmail(env, owner.email);
  const toRemove = current.filter((l) => !keep.includes(l));

  for (const list of toRemove) {
    await env.AUDIT_DB.prepare(
      `DELETE FROM subscribers WHERE email = ? AND list = ?`,
    ).bind(owner.email, list).run();
  }

  const remaining = await listsForEmail(env, owner.email);

  await logEvent(env, req, "preferences_attempt", "saved", {
    email: owner.email,
    list: `kept=${keep.join(",")},removed=${toRemove.join(",")}`,
    tokenPrefix,
  });

  if (remaining.length === 0) {
    return Response.redirect(env.UNSUBSCRIBED_URL, 303);
  }

  // The token they arrived with may belong to a row we just deleted. If so,
  // they won't be able to re-visit /preferences with it. That's fine —
  // their remaining emails still carry valid tokens. We still render the
  // current page from the token they used (lookup may fail on re-GET but
  // we have the email loaded here).
  const savedNote = toRemove.length
    ? `Saved. Removed: ${toRemove.map((l) => LIST_LABELS[l] || l).join(", ")}.`
    : `Saved. No changes.`;
  return new Response(preferencesPage(token, owner.email, remaining, savedNote), {
    status: 200,
    headers: { "Content-Type": "text/html; charset=utf-8" },
  });
}

// ---------------------------------------------------------------------------
// POST /broadcast  — fan-out to all confirmed subscribers of one list
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
  if (!VALID_LISTS.has(list)) {
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

  let sent = 0, skippedDedup = 0, failed = 0;

  const accepted: { email: string; unsub_token: string }[] = [];
  for (const row of candidates) {
    const result = await env.AUDIT_DB.prepare(
      `INSERT OR IGNORE INTO sends (email, list, date_sent) VALUES (?, ?, ?)`,
    ).bind(row.email, list, today).run();
    const changes = (result.meta && (result.meta.changes ?? 0)) || 0;
    if (changes > 0) accepted.push(row);
    else skippedDedup++;
  }

  if (accepted.length === 0) {
    await logEvent(env, req, "broadcast_attempt", "nothing_to_send", { list });
    return new Response(
      JSON.stringify({ sent: 0, skipped_dedup: skippedDedup, failed: 0 }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  }

  for (let i = 0; i < accepted.length; i += RESEND_BATCH_CHUNK) {
    const chunk = accepted.slice(i, i + RESEND_BATCH_CHUNK);
    const envelopes = chunk.map((row) => {
      const unsubUrl = `${workerOrigin}/unsubscribe?token=${row.unsub_token}`;
      const prefsUrl = `${workerOrigin}/preferences?token=${row.unsub_token}`;
      return {
        from: env.FROM_ADDR,
        to: [row.email],
        subject: body.subject,
        html: body.html
          .replace(/{{UNSUB_URL}}/g, unsubUrl)
          .replace(/{{PREFS_URL}}/g, prefsUrl),
        text: body.text
          .replace(/{{UNSUB_URL}}/g, unsubUrl)
          .replace(/{{PREFS_URL}}/g, prefsUrl),
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
    if (req.method === "POST" && url.pathname === "/") return handleSubscribe(req, env);
    if (req.method === "GET"  && url.pathname === "/confirm") return handleConfirm(req, env);
    if (req.method === "GET"  && url.pathname === "/block") return handleBlock(req, env);
    if (url.pathname === "/unsubscribe" && (req.method === "GET" || req.method === "POST")) {
      return handleUnsubscribe(req, env);
    }
    if (req.method === "GET"  && url.pathname === "/preferences") return handleGetPreferences(req, env);
    if (req.method === "POST" && url.pathname === "/preferences") return handlePostPreferences(req, env);
    if (req.method === "POST" && url.pathname === "/broadcast") return handleBroadcast(req, env);
    return new Response("Not Found", { status: 404 });
  },
};
