import { NextResponse } from "next/server";
import { sql } from "@/lib/db";
import { SESSION_COOKIE } from "@/lib/session";
import { kickoffCollection, isDataFresh } from "@/lib/kickoff";

export const maxDuration = 60;

/** 온보딩 — 토스 키를 받아 검증하고 세션을 발급한다.
 *
 *  ⚠️ 토스는 IP 화이트리스트 방식인데 Vercel 은 요청마다 IP 가 달라서
 *     직접 호출하면 403 ip-not-allowed 가 난다.
 *     → 고정 egress IP 를 가진 Fly 워커(/verify)에 위임한다.
 *       사용자는 **그 서버 IP 하나만** 토스에 등록하면 된다.
 *
 *  WORKER_API_URL 이 없으면(로컬 개발) 직접 호출로 폴백한다.
 */
const TOSS = "https://openapi.tossinvest.com";

export async function POST(req: Request) {
  let clientId = "", clientSecret = "";
  try {
    const b = await req.json();
    clientId = String(b.clientId ?? "").trim();
    clientSecret = String(b.clientSecret ?? "").trim();
  } catch {
    return NextResponse.json({ error: "요청 형식이 올바르지 않습니다." }, { status: 400 });
  }
  if (!clientId || !clientSecret) {
    return NextResponse.json({ error: "Client ID 와 Secret 을 모두 입력해주세요." }, { status: 400 });
  }

  const workerUrl = process.env.WORKER_API_URL;
  const token = process.env.INTERNAL_API_TOKEN;

  // ── 경로 A: 워커 프록시 (배포 환경) ──
  if (workerUrl && token) {
    let r: Response;
    try {
      r = await fetch(`${workerUrl}/verify`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
          "X-Forwarded-UA": req.headers.get("user-agent") ?? "",
        },
        body: JSON.stringify({ clientId, clientSecret }),
        cache: "no-store",
      });
    } catch {
      return NextResponse.json(
        { error: "수집 서버에 연결할 수 없습니다. 잠시 후 다시 시도해주세요." },
        { status: 503 });
    }
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      if (d.error === "IP_NOT_ALLOWED") {
        return NextResponse.json({ error: "IP_NOT_ALLOWED", message: d.message },
                                 { status: 403 });
      }
      return NextResponse.json({ error: d.error ?? "자격증명이 유효하지 않습니다." },
                               { status: r.status });
    }

    const res = NextResponse.json({
      ok: true, accountSeq: d.accountSeq,
      collecting: !!d.collecting, reusedData: !!d.reusedData,
    });
    res.cookies.set(SESSION_COOKIE, d.sessionToken, {
      httpOnly: true, secure: process.env.NODE_ENV === "production",
      sameSite: "lax", path: "/", maxAge: 60 * 60 * 24 * 30,
    });
    return res;
  }

  // ── 경로 B: 직접 호출 (로컬 개발 — 이 머신 IP 가 등록돼 있어야 한다) ──
  const tokRes = await fetch(`${TOSS}/oauth2/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "client_credentials", client_id: clientId, client_secret: clientSecret,
    }),
    cache: "no-store",
  });
  if (!tokRes.ok) {
    let code = "";
    try { code = (await tokRes.clone().json())?.error?.code ?? ""; } catch { /* noop */ }
    if (tokRes.status === 403 || code === "ip-not-allowed") {
      return NextResponse.json({
        error: "IP_NOT_ALLOWED",
        message: "토스증권에 서버 IP 가 등록되지 않았습니다.",
      }, { status: 403 });
    }
    return NextResponse.json(
      { error: "자격증명이 유효하지 않습니다. 토스증권 WTS 에서 발급한 값인지 확인해주세요." },
      { status: 401 });
  }
  const tok = await tokRes.json();

  const accRes = await fetch(`${TOSS}/api/v1/accounts`, {
    headers: { Authorization: `Bearer ${tok.access_token}` }, cache: "no-store",
  });
  const accounts = accRes.ok ? ((await accRes.json())?.result ?? []) : [];
  if (!accounts.length) {
    return NextResponse.json({ error: "조회 가능한 계좌가 없습니다." }, { status: 400 });
  }
  const accountSeq = String(accounts[0].accountSeq);

  const masterKey = process.env.CREDENTIAL_MASTER_KEY;
  if (!masterKey) {
    return NextResponse.json({ error: "서버 설정 오류 (마스터 키 없음)" }, { status: 500 });
  }

  const existing = (await sql()`
    SELECT user_id, status FROM user_credential WHERE client_id = ${clientId}`) as any[];
  if (existing[0]?.status === "revoked") {
    return NextResponse.json({ error: "해지된 계정입니다." }, { status: 403 });
  }

  let userId: string;
  if (existing.length) {
    userId = existing[0].user_id;
    await sql()`
      UPDATE user_credential
         SET client_secret_enc = pgp_sym_encrypt(${clientSecret}, ${masterKey})::bytea,
             account_seq = ${accountSeq}, status = 'active',
             premature_401_count = 0, locked_reason = NULL, last_ok_at = now()
       WHERE user_id = ${userId}`;
  } else {
    const created = (await sql()`
      INSERT INTO app_user DEFAULT VALUES RETURNING id`) as any[];
    userId = created[0].id;
    await sql()`
      INSERT INTO user_credential
        (user_id, client_id, client_secret_enc, account_seq, verified_at, last_ok_at)
      VALUES (${userId}, ${clientId},
              pgp_sym_encrypt(${clientSecret}, ${masterKey})::bytea,
              ${accountSeq}, now(), now())`;
  }

  // 방금 검증에 성공한 자격증명을 수집용으로 승격 (죽은 키가 남아있으면 안 된다)
  await sql()`UPDATE user_credential SET is_collector = false WHERE is_collector`;
  await sql()`UPDATE user_credential SET is_collector = true WHERE user_id = ${userId}`;

  const expiresAt = new Date(Date.now() + Number(tok.expires_in ?? 3600) * 1000);
  await sql()`
    INSERT INTO toss_token (user_id, access_token, expires_at)
    VALUES (${userId}, ${tok.access_token}, ${expiresAt.toISOString()})
    ON CONFLICT (user_id) DO UPDATE SET
      access_token = EXCLUDED.access_token, expires_at = EXCLUDED.expires_at,
      issued_at = now(), updated_at = now()`;

  const { randomBytes, createHash } = await import("crypto");
  const raw = randomBytes(32).toString("base64url");
  const digest = createHash("sha256")
    .update((process.env.SESSION_PEPPER ?? "") + raw).digest();
  await sql()`
    INSERT INTO user_session (token_hash, user_id, expires_at, user_agent)
    VALUES (${digest}, ${userId}, now() + interval '30 days',
            ${req.headers.get("user-agent") ?? null})`;

  const fresh = await isDataFresh();
  const kick = fresh ? { started: false } : kickoffCollection();

  const res = NextResponse.json({
    ok: true, accountSeq, collecting: kick.started, reusedData: fresh,
  });
  res.cookies.set(SESSION_COOKIE, raw, {
    httpOnly: true, secure: process.env.NODE_ENV === "production",
    sameSite: "lax", path: "/", maxAge: 60 * 60 * 24 * 30,
  });
  return res;
}
