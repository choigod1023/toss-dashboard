import { NextResponse } from "next/server";
import { createHash } from "crypto";
import { cookies } from "next/headers";
import { sql } from "@/lib/db";
import { SESSION_COOKIE } from "@/lib/session";

export async function POST() {
  const raw = (await cookies()).get(SESSION_COOKIE)?.value;
  if (raw) {
    const digest = createHash("sha256")
      .update((process.env.SESSION_PEPPER ?? "") + raw).digest();
    await sql()`DELETE FROM user_session WHERE token_hash = ${digest}`;
  }
  const res = NextResponse.json({ ok: true });
  res.cookies.set(SESSION_COOKIE, "", { path: "/", maxAge: 0 });
  return res;
}
