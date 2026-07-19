import { cookies } from "next/headers";
import { createHash } from "crypto";
import { sql } from "./db";

export const SESSION_COOKIE = "td_session";

/** 쿠키 원문 → user_id. DB에는 해시만 있으므로 위조 불가. */
export async function currentUserId(): Promise<string | null> {
  const raw = (await cookies()).get(SESSION_COOKIE)?.value;
  if (!raw) return null;
  const pepper = process.env.SESSION_PEPPER ?? "";
  const digest = createHash("sha256").update(pepper + raw).digest();
  const rows = (await sql()`
    SELECT user_id FROM user_session
    WHERE token_hash = ${digest} AND expires_at > now()`) as any[];
  return rows[0]?.user_id ?? null;
}
