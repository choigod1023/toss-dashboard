import { getWorkerIps } from "@/lib/db";
import OnboardForm from "./form";

export const dynamic = "force-dynamic";

export default async function OnboardPage() {
  // 워커가 보고한 현재 IP 를 읽어 폼에 넘긴다.
  // 하드코딩·환경변수를 쓰지 않으므로 호스팅이 바뀌어도 자동으로 맞는다.
  let workerIps: any[] = [];
  try {
    workerIps = await getWorkerIps();
  } catch {
    workerIps = [];
  }
  return <OnboardForm workerIps={workerIps as any} />;
}
