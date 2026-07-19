"use client";

import { useState } from "react";

type Pt = { t: string; v: number };

const fmt = (n: number, d = 0) =>
  n.toLocaleString("ko-KR", { minimumFractionDigits: d, maximumFractionDigits: d });

/** 종가 라인 + 크로스헤어 툴팁. 단일 시리즈이므로 범례 없음(제목이 이름). */
export function LineChart({
  data, color = "var(--series-1)", height = 190, decimals = 0, unit = "",
}: { data: Pt[]; color?: string; height?: number; decimals?: number; unit?: string }) {
  const [hover, setHover] = useState<number | null>(null);
  if (data.length < 2) return <div className="empty">데이터 없음</div>;

  const W = 720, H = height, P = { t: 10, r: 14, b: 22, l: 52 };
  const iw = W - P.l - P.r, ih = H - P.t - P.b;
  const vs = data.map((d) => d.v);
  let lo = Math.min(...vs), hi = Math.max(...vs);
  const pad = (hi - lo) * 0.08 || Math.abs(hi) * 0.02 || 1;
  lo -= pad; hi += pad;

  const x = (i: number) => P.l + (i / (data.length - 1)) * iw;
  const y = (v: number) => P.t + (1 - (v - lo) / (hi - lo)) * ih;
  const path = data.map((d, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(d.v).toFixed(1)}`).join("");
  const ticks = [lo, lo + (hi - lo) / 2, hi];
  const hi_ = hover === null ? null : data[hover];

  return (
    <div style={{ position: "relative" }}>
      <svg
        viewBox={`0 0 ${W} ${H}`} role="img"
        onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const r = e.currentTarget.getBoundingClientRect();
          const px = ((e.clientX - r.left) / r.width) * W;
          const i = Math.round(((px - P.l) / iw) * (data.length - 1));
          setHover(i >= 0 && i < data.length ? i : null);
        }}
      >
        {ticks.map((t, i) => (
          <g key={i}>
            <line x1={P.l} x2={W - P.r} y1={y(t)} y2={y(t)} stroke="var(--grid)" strokeWidth="1" />
            <text x={P.l - 8} y={y(t) + 4} textAnchor="end" fontSize="10" fill="var(--text-muted)">
              {fmt(t, decimals)}
            </text>
          </g>
        ))}
        <path d={path} fill="none" stroke={color} strokeWidth="2"
              strokeLinejoin="round" strokeLinecap="round" />
        {[0, Math.floor(data.length / 2), data.length - 1].map((i) => (
          <text key={i} x={x(i)} y={H - 6} textAnchor={i === 0 ? "start" : i === data.length - 1 ? "end" : "middle"}
                fontSize="10" fill="var(--text-muted)">
            {data[i].t.slice(2, 10)}
          </text>
        ))}
        {hi_ && (
          <g>
            <line x1={x(hover!)} x2={x(hover!)} y1={P.t} y2={P.t + ih}
                  stroke="var(--axis)" strokeWidth="1" strokeDasharray="3 3" />
            <circle cx={x(hover!)} cy={y(hi_.v)} r="4.5" fill={color}
                    stroke="var(--surface-1)" strokeWidth="2" />
          </g>
        )}
      </svg>
      {hi_ && (
        <div style={{
          position: "absolute", top: 4,
          left: `${Math.min(Math.max((x(hover!) / W) * 100, 8), 74)}%`,
          background: "var(--surface-1)", border: "1px solid var(--border)",
          borderRadius: 7, padding: "6px 10px", fontSize: 12, pointerEvents: "none",
          boxShadow: "0 2px 10px rgba(0,0,0,.10)", whiteSpace: "nowrap",
        }}>
          <div style={{ color: "var(--text-muted)", fontSize: 11 }}>{hi_.t.slice(0, 10)}</div>
          <div style={{ fontWeight: 600, fontVariantNumeric: "tabular-nums" }}>
            {fmt(hi_.v, decimals)}{unit}
          </div>
        </div>
      )}
    </div>
  );
}

/** 투자자별 순매수 — 0 기준 발산 막대. 위=순매수, 아래=순매도. */
export function FlowChart({
  series,
}: { series: { name: string; color: string; points: Pt[] }[] }) {
  const [hv, setHv] = useState<{ s: number; i: number } | null>(null);
  const dates = series[0]?.points.map((p) => p.t) ?? [];
  if (dates.length < 2) return <div className="empty">데이터 없음</div>;

  const W = 720, H = 200, P = { t: 12, r: 14, b: 22, l: 58 };
  const iw = W - P.l - P.r, ih = H - P.t - P.b;
  const all = series.flatMap((s) => s.points.map((p) => p.v));
  const m = Math.max(...all.map(Math.abs)) || 1;
  const y0 = P.t + ih / 2;
  const y = (v: number) => y0 - (v / m) * (ih / 2);
  const gw = iw / dates.length;
  const bw = Math.max(2, (gw - 3) / series.length - 1.5);

  return (
    <div style={{ position: "relative" }}>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" onMouseLeave={() => setHv(null)}>
        {[m, 0, -m].map((t, i) => (
          <g key={i}>
            <line x1={P.l} x2={W - P.r} y1={y(t)} y2={y(t)}
                  stroke={t === 0 ? "var(--axis)" : "var(--grid)"} strokeWidth="1" />
            <text x={P.l - 8} y={y(t) + 4} textAnchor="end" fontSize="10" fill="var(--text-muted)">
              {(t / 1e12).toFixed(1)}조
            </text>
          </g>
        ))}
        {series.map((s, si) =>
          s.points.map((p, i) => {
            const bx = P.l + i * gw + 2 + si * (bw + 1.5);
            const h = Math.abs(y(p.v) - y0);
            const on = hv?.s === si && hv?.i === i;
            return (
              <rect key={`${si}-${i}`} x={bx} y={p.v >= 0 ? y(p.v) : y0}
                    width={bw} height={Math.max(h, 1)} rx="2" fill={s.color}
                    opacity={hv && !on ? 0.35 : 1}
                    onMouseEnter={() => setHv({ s: si, i })} />
            );
          })
        )}
      </svg>
      <div className="legend">
        {series.map((s) => (
          <span key={s.name}><i style={{ background: s.color }} />{s.name}</span>
        ))}
      </div>
      {hv && (
        <div style={{
          position: "absolute", top: 4, right: 8, background: "var(--surface-1)",
          border: "1px solid var(--border)", borderRadius: 7, padding: "6px 10px",
          fontSize: 12, pointerEvents: "none", boxShadow: "0 2px 10px rgba(0,0,0,.10)",
        }}>
          <div style={{ color: "var(--text-muted)", fontSize: 11 }}>
            {series[hv.s].points[hv.i].t.slice(0, 10)} · {series[hv.s].name}
          </div>
          <div style={{ fontWeight: 600, fontVariantNumeric: "tabular-nums" }}>
            {fmt(series[hv.s].points[hv.i].v / 1e8)}억
          </div>
        </div>
      )}
    </div>
  );
}
