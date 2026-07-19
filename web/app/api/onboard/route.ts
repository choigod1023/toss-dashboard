import { NextResponse } from "next/server";
import { randomBytes, createHash } from "crypto";
import { sql } from "@/lib/db";
import { SESSION_COOKIE } from "@/lib/session";

const BASE = "https://openapi.tossinvest.com";

/** 토스 키를 검증하고 세션을 발급한다. 별도 로그인은 없다 —
 *  자격증명이 곧 신원이다. 검증 실패 시 아무것도 저장하지 않는다. */
export async function POST(req: Request) {
  let clientId = "", clientSecret = "";
  try {
    const body = await req.json();
    clientId = String(body.clientId ?? "").trim();
    clientSecret = String(body.clientSecret ?? "").trim();
  } catch {
    return NextResponse.json({ error: "요청 형식이 올바르지 않습니다." }, { status: 400 });
  }
  if (!clientId || !clientSecret) {
    return NextResponse.json({ error: "Client ID 와 Secret 을 모두 입력해주세요." }, { status: 400 });
  }

  // 1) 실제로 동작하는 키인지 확인 — 검증 전에는 저장하지 않는다
  const tokRes = await fetch(`${BASE}/oauth2/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "client_credentials",
      client_id: clientId, client_secret: clientSecret,
    }),
    cache: "no-store",
  });
  if (!tokRes.ok) {
    return NextResponse.json(
      { error: "자격증명이 유효하지 않습니다. 토스증권 WTS 에서 발급한 값인지 확인해주세요." },
      { status: 401 });
  }
  const tok = await tokRes.json();

  // 2) 계좌 확보
  const accRes = await fetch(`${BASE}/api/v1/accounts`, {
    headers: { Authorization: `Bearer ${tok.access_token}` }, cache: "no-store",
  });
  const accounts = accRes.ok ? ((await accRes.json())?.result ?? []) : [];
  if (!accounts.length) {
    return NextResponse.json({ error: "조회 가능한 계좌가 없습니다." }, { status: 400 });
  }
  const accountSeq = String(accounts[0].accountSeq);

  // 3) 저장 — client_secret 은 워커의 마스터키로 봉인해야 하므로
  //    여기서는 pgcrypto 로 봉인한다 (같은 키를 공유)
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
    // 이미 등록된 client_id — 새 secret 을 제시했다는 건 토스에서
    // 재발급받을 수 있는 실소유자라는 뜻이다.
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

  const expiresAt = new Date(Date.now() + Number(tok.expires_in ?? 3600) * 1000);
  await sql()`
    INSERT INTO toss_token (user_id, access_token, expires_at)
    VALUES (${userId}, ${tok.access_token}, ${expiresAt.toISOString()})
    ON CONFLICT (user_id) DO UPDATE SET
      access_token = EXCLUDED.access_token, expires_at = EXCLUDED.expires_at,
      issued_at = now(), updated_at = now()`;

  // 4) 세션 — 쿠키엔 원문, DB엔 해시
  const raw = randomBytes(32).toString("base64url");
  const digest = createHash("sha256")
    .update((process.env.SESSION_PEPPER ?? "") + raw).digest();
  await sql()`
    INSERT INTO user_session (token_hash, user_id, expires_at, user_agent)
    VALUES (${digest}, ${userId}, now() + interval '30 days',
            ${req.headers.get("user-agent") ?? null})`;

  const res = NextResponse.json({ ok: true, accountSeq });
  res.cookies.set(SESSION_COOKIE, raw, {
    httpOnly: true, secure: process.env.NODE_ENV === "production",
    sameSite: "lax", path: "/", maxAge: 60 * 60 * 24 * 30,
  });
  return res;
}
