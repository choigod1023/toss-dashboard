"use client";

import { useEffect, useRef, useState } from "react";

type Msg = { role: "user" | "assistant"; content: string; pending?: boolean };

const SUGGESTIONS = [
  "지금 내 포트폴리오에서 가장 큰 위험은?",
  "현금 비중이 적절한가?",
  "손실 난 종목을 어떻게 봐야 할까?",
  "지금 시장 국면에서 뭘 점검해야 하나?",
];

export default function Advisor() {
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [followups, setFollowups] = useState<string[]>([]);
  const [open, setOpen] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    fetch("/api/advise")
      .then((r) => r.json())
      .then((d) => setMsgs(d.messages ?? []))
      .catch(() => {});
  }, [open]);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); },
    [msgs, busy]);

  async function send(text: string) {
    const question = text.trim();
    if (!question || busy) return;
    setQ(""); setFollowups([]); setBusy(true);
    setMsgs((m) => [...m, { role: "user", content: question },
                    { role: "assistant", content: "", pending: true }]);
    try {
      const res = await fetch("/api/advise", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
      });
      const d = await res.json();
      setMsgs((m) => [...m.slice(0, -1), {
        role: "assistant",
        content: d.answer ?? `⚠️ ${d.error ?? "답변을 받지 못했습니다."}`,
      }]);
      setFollowups(d.followups ?? []);
    } catch {
      setMsgs((m) => [...m.slice(0, -1),
        { role: "assistant", content: "⚠️ 요청에 실패했습니다." }]);
    } finally { setBusy(false); }
  }

  if (!open) {
    return (
      <button className="advisor-fab" onClick={() => setOpen(true)}>
        전문가에게 물어보기
      </button>
    );
  }

  return (
    <div className="advisor">
      <div className="advisor-head">
        <div>
          <b>포트폴리오 상담</b>
          <div className="sym">내 실제 보유·수익률·여론 데이터를 근거로 답합니다</div>
        </div>
        <button className="advisor-x" onClick={() => setOpen(false)}>✕</button>
      </div>

      <div className="advisor-body">
        {!msgs.length && (
          <div className="advisor-empty">
            <p>무엇이든 물어보세요. 예를 들면—</p>
            {SUGGESTIONS.map((sg) => (
              <button key={sg} className="chip-btn" onClick={() => send(sg)}>{sg}</button>
            ))}
          </div>
        )}

        {msgs.map((m, i) => (
          <div key={i} className={`bubble ${m.role}`}>
            {m.pending
              ? <span className="sym"><span className="spin" />답변을 준비하는 중…</span>
              : m.content}
          </div>
        ))}

        {!busy && followups.length > 0 && (
          <div className="advisor-followups">
            {followups.map((f) => (
              <button key={f} className="chip-btn" onClick={() => send(f)}>{f}</button>
            ))}
          </div>
        )}
        <div ref={endRef} />
      </div>

      <form className="advisor-input"
            onSubmit={(e) => { e.preventDefault(); send(q); }}>
        <input value={q} onChange={(e) => setQ(e.target.value)}
               placeholder="질문을 입력하세요" disabled={busy} maxLength={500} />
        <button disabled={busy || !q.trim()}>보내기</button>
      </form>

      <div className="advisor-foot">
        가격을 예측하지 않으며 매매를 권유하지 않습니다. 판단은 본인이 하세요.
      </div>
    </div>
  );
}
