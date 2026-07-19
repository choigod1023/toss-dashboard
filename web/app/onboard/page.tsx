"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";

export default function Onboard() {
  const r = useRouter();
  const [id, setId] = useState("");
  const [sec, setSec] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true); setErr(null);
    try {
      const res = await fetch("/api/onboard", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ clientId: id.trim(), clientSecret: sec.trim() }),
      });
      const d = await res.json();
      if (!res.ok) { setErr(d.error ?? "연결에 실패했습니다."); return; }
      r.push("/"); r.refresh();
    } catch {
      setErr("네트워크 오류가 발생했습니다.");
    } finally { setBusy(false); }
  }

  return (
    <div className="wrap" style={{ maxWidth: 560 }}>
      <div className="head"><h1>토스증권 계정 연결</h1></div>

      <div className="card">
        <p style={{ marginTop: 0, color: "var(--text-secondary)", lineHeight: 1.7 }}>
          토스증권 Open API 키를 입력하면 바로 시작됩니다. 별도 회원가입은 없습니다.
        </p>

        <form onSubmit={submit}>
          <label className="fld">
            <span>Client ID</span>
            <input value={id} onChange={(e) => setId(e.target.value)}
                   autoComplete="off" spellCheck={false} required
                   placeholder="토스증권 WTS → 설정 → Open API" />
          </label>
          <label className="fld">
            <span>Client Secret</span>
            <input type="password" value={sec} onChange={(e) => setSec(e.target.value)}
                   autoComplete="off" required placeholder="발급받은 시크릿" />
          </label>

          {err && <div className="err">{err}</div>}

          <button className="btn" disabled={busy || !id || !sec}>
            {busy ? "확인 중…" : "연결하고 시작하기"}
          </button>
        </form>
      </div>

      <div className="card" style={{ marginTop: 14 }}>
        <h2>연결 전에 알아두세요</h2>
        <ul className="bullets">
          <li>
            <b>다른 토스 API 도구를 쓰고 계시면 그쪽 연결이 끊깁니다.</b>{" "}
            토스는 Client ID 당 토큰을 하나만 유지해서, 여기서 연결하는 순간
            기존 토큰이 무효화됩니다.
          </li>
          <li>
            <b>이 서비스는 주문을 넣지 않습니다.</b> 조회와 분석만 합니다.
            다만 토스 API에는 읽기 전용 권한이 없어서, 전달하신 키 자체에는
            주문 권한이 포함되어 있습니다.
          </li>
          <li>
            <b>언제든 직접 끊을 수 있습니다.</b> 토스증권 WTS에서
            Client Secret을 재발급하면 이 서비스의 접근이 즉시 차단됩니다.
          </li>
          <li>
            Secret은 암호화해서 보관하며 화면·로그에 노출되지 않습니다.
            탈퇴 시 즉시 삭제됩니다.
          </li>
        </ul>
      </div>

      <div className="card" style={{ marginTop: 14 }}>
        <h2>키 발급 방법</h2>
        <ol className="bullets">
          <li>토스증권 PC 웹(WTS)에 로그인합니다.</li>
          <li>설정 → Open API 메뉴로 이동합니다.</li>
          <li>Client ID와 Client Secret을 발급받아 위에 붙여넣습니다.</li>
        </ol>
      </div>
    </div>
  );
}
