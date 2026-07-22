import { spawn } from "child_process";
import path from "path";
import { existsSync } from "fs";

/** 온보딩 직후 첫 수집을 붙인다.
 *
 *  로컬 실행에서는 워커가 같은 머신에 있으므로 그냥 프로세스를 띄우면 된다.
 *  응답을 기다리지 않고(detached) 바로 반환한다 — 수집은 수 분 걸리는데
 *  사용자를 그동안 세워둘 이유가 없다.
 *
 *  ⚠️ 배포(Vercel)에서는 동작하지 않는다. 서버리스 함수는 응답 후 죽고
 *     파이썬 런타임도 없다. 그래서 워커가 있을 때만 실행한다.
 */
/** 최근 수집이 있으면 다시 긁지 않는다.
 *  일봉·지표·13F 는 하루 단위로 바뀌는 데이터라 재온보딩마다 다시 받을 이유가
 *  없다. 토스 rate limit 과 Gemini 호출만 낭비된다. */
const FRESH_MINUTES = 30;

export async function isDataFresh(): Promise<boolean> {
  const { sql } = await import("./db");
  try {
    const r = (await sql()`
      SELECT max(started_at) AS last FROM job_run
      WHERE job_name = 'portfolio_all' AND ok`) as { last: string | null }[];
    const last = r[0]?.last;
    if (!last) return false;
    return Date.now() - new Date(last).getTime() < FRESH_MINUTES * 60_000;
  } catch {
    return false;
  }
}

export function kickoffCollection(): { started: boolean; reason?: string } {
  if (process.env.DISABLE_LOCAL_KICKOFF === "1") {
    return { started: false, reason: "disabled" };
  }
  // web/ 의 부모가 저장소 루트
  const root = path.resolve(process.cwd(), "..");
  const entry = path.join(root, "worker", "main.py");
  if (!existsSync(entry)) {
    return { started: false, reason: "워커 없음 (배포 환경)" };
  }

  try {
    const child = spawn("python3", [entry, "daily"], {
      cwd: root,
      detached: true,
      stdio: "ignore",
      env: { ...process.env },
    });
    child.unref();          // 부모(Next.js)가 자식을 기다리지 않게
    return { started: true };
  } catch (e) {
    return { started: false, reason: String(e).slice(0, 120) };
  }
}
