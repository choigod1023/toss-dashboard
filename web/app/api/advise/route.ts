import { NextResponse } from "next/server";
import { spawn } from "child_process";
import path from "path";
import { existsSync } from "fs";
import { currentUserId } from "@/lib/session";

export const maxDuration = 120;

/** 상담 질문 → 파이썬 어드바이저 실행 → 답변.
 *
 *  분석 로직(사실 수집 SQL + 프롬프트 가드레일)이 전부 파이썬에 있으므로
 *  TS 로 옮겨 이중 관리하지 않고 그대로 호출한다. */
export async function POST(req: Request) {
  const userId = await currentUserId();
  if (!userId) return NextResponse.json({ error: "로그인이 필요합니다." }, { status: 401 });

  let question = "";
  try {
    question = String((await req.json()).question ?? "").trim();
  } catch { /* noop */ }
  if (!question) return NextResponse.json({ error: "질문을 입력해주세요." }, { status: 400 });
  if (question.length > 500)
    return NextResponse.json({ error: "질문이 너무 깁니다 (500자 이내)." }, { status: 400 });

  const root = path.resolve(process.cwd(), "..");
  const entry = path.join(root, "worker", "ask.py");
  if (!existsSync(entry)) {
    return NextResponse.json(
      { error: "상담 기능은 로컬 실행에서만 사용할 수 있습니다." }, { status: 503 });
  }

  const out = await new Promise<string>((resolve, reject) => {
    const c = spawn("python3", [entry, userId, question], { cwd: root });
    let so = "", se = "";
    c.stdout.on("data", (d) => (so += d));
    c.stderr.on("data", (d) => (se += d));
    c.on("close", (code) =>
      code === 0 ? resolve(so) : reject(new Error(se.slice(-400) || `exit ${code}`)));
    setTimeout(() => { c.kill(); reject(new Error("시간 초과")); }, 110_000);
  }).catch((e) => JSON.stringify({ error: String(e.message).slice(0, 300) }));

  try {
    const d = JSON.parse(out);
    if (d.error) return NextResponse.json(d, { status: 500 });
    return NextResponse.json(d);
  } catch {
    return NextResponse.json({ error: "응답을 해석하지 못했습니다." }, { status: 500 });
  }
}

export async function GET() {
  const userId = await currentUserId();
  if (!userId) return NextResponse.json({ messages: [] });
  const { sql } = await import("@/lib/db");
  const rows = (await sql()`
    SELECT role, content, created_at FROM advice_thread
    WHERE user_id = ${userId} ORDER BY created_at DESC LIMIT 40`) as any[];
  return NextResponse.json({ messages: rows.reverse() });
}
