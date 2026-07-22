import { redirect } from "next/navigation";
import { LineChart, FlowChart } from "./charts";
import Advisor from "./advisor";
import { currentUserId } from "@/lib/session";
import {
  getAccount, getHoldings, getCandles, getWatched,
  getIndicator, getInvestorFlow, getSystem, num,
  getBriefings, getAnalystViews, getSentimentBySymbol,
  getRecentPosts, getSourceStats,
  getStrategy, getRegimes, getMetrics,
  getInstitutionsFor, getInstitutionTop, getCandlesBulk, getRebalance,
} from "@/lib/db";

export const dynamic = "force-dynamic";

const won = (n: number) => n.toLocaleString("ko-KR", { maximumFractionDigits: 0 });
const pct = (n: number) => `${n >= 0 ? "+" : ""}${(n * 100).toFixed(2)}%`;
const sign = (n: number) => (n > 0 ? "up" : n < 0 ? "down" : "");

function Tile({ label, value, sub, tone }: {
  label: string; value: string; sub?: string; tone?: string;
}) {
  return (
    <div className="card">
      <div className="tile-label">{label}</div>
      <div className={`tile-value ${tone ?? ""}`}>{value}</div>
      {sub && <div className="tile-sub">{sub}</div>}
    </div>
  );
}

export default async function Page() {
  // 자격증명이 곧 신원이다 — 세션이 없으면 온보딩으로 보낸다
  const userId = await currentUserId();
  if (!userId) redirect("/onboard");

  // DB 가 멀어 왕복 1회가 0.2~0.4s 다. 독립 쿼리는 전부 한 번에 던진다.
  // (전엔 4단계로 순차 대기해서 페이지가 10초 넘게 걸렸다)
  const [acc, holdings, watched, kospi, flow, sys,
         briefs, views, sentiment, posts, srcStats,
         strat, regimes, metrics, instTop, rebal] = await Promise.all([
    getAccount(userId), getHoldings(userId), getWatched(),
    getIndicator("KOSPI", 120), getInvestorFlow("KOSPI", 20), getSystem(),
    getBriefings(), getAnalystViews(), getSentimentBySymbol(),
    getRecentPosts(14), getSourceStats(),
    getStrategy(userId), getRegimes(), getMetrics(userId), getInstitutionTop(),
    getRebalance(userId),
  ]);
  const inp = strat?.inputs ?? {};
  const arr = (v: any) => (Array.isArray(v) ? v : v ? JSON.parse(v) : []);

  // 아래 둘만 위 결과에 의존한다 → 2단계에서 병렬로
  const usTickers = holdings.filter((h) => h.market_country === "US").map((h) => h.symbol);
  const [instMine, candleMap] = await Promise.all([
    getInstitutionsFor(usTickers),
    getCandlesBulk(watched.slice(0, 4).map((w) => w.symbol), 120),
  ]);
  const charts = watched.slice(0, 4).map((w) => ({ ...w, rows: candleMap.get(w.symbol) ?? [] }));

  const total = num(acc?.market_value_total_krw);
  const pnl = num(acc?.pnl_total_krw);
  const rate = num(acc?.pnl_rate_computed);
  const dPnl = num(acc?.daily_pnl_total_krw);
  const dRate = num(acc?.daily_pnl_rate);
  const flowBy = (who: string) =>
    flow.filter((f) => f.investor === who).reverse()
        .map((f) => ({ t: String(f.trade_date), v: num(f.net) }));

  return (
    <div className="wrap">
      <div className="head">
        <h1>토스 트레이딩 대시보드</h1>
        <div className="stamp">
          스냅샷 {acc?.snapshot_date ? String(acc.snapshot_date).slice(0, 10) : "—"}
          {acc?.exchange_rate ? ` · USD/KRW ${num(acc.exchange_rate).toFixed(1)}` : ""}
        </div>
      </div>


      {/* ══ 맞춤 전략 — 페이지의 주인공 ══ */}
      {strat ? (
        <div className="hero">
          <div className="hero-kicker">당신의 투자 성향</div>
          <p className="hero-type">{inp.investor_type ?? "—"}</p>

          {inp.themes?.length > 0 && (
            <div className="chips">
              {inp.themes.map((t: string) => <span className="chip" key={t}>{t}</span>)}
            </div>
          )}

          <div className="hero-split">
            <div>
              <div className="hero-label">지금 시장은</div>
              <p className="hero-text">{strat.regime}</p>
              <div className="fg-row">
                {regimes.map((r: any) => {
                  const sc = num(r.score);
                  const kr = r.source === "kr_composite";
                  return (
                    <div className="fg" key={r.source} title={kr ? "자체 산출값 (공인 지표 아님)" : ""}>
                      <div className="fg-name">
                        {{ cnn: "미국증시", crypto: "크립토", kr_composite: "국내(자체)" }[r.source as string] ?? r.source}
                      </div>
                      <div className="fg-bar">
                        <i style={{ left: `${sc}%` }} />
                      </div>
                      <div className="fg-val">
                        <b>{sc.toFixed(0)}</b>
                        <span>{{ "extreme fear": "극단적 공포", fear: "공포", neutral: "중립", greed: "탐욕", "extreme greed": "극단적 탐욕" }[r.rating as string] ?? r.rating}</span>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
            <div>
              <div className="hero-label">내 포트폴리오는</div>
              <p className="hero-text">{strat.diagnosis}</p>
            </div>
          </div>
        </div>
      ) : (
        <div className="card" style={{ marginBottom: 14 }}>
          <div className="empty">
            전략이 아직 생성되지 않았습니다 —
            <code> python3 worker/main.py analyze</code>
          </div>
        </div>
      )}

      {strat && (
        <div className="grid two">
          <div className="card">
            <h2>지금 점검할 것</h2>
            <div className="rows">
              {arr(strat.actions).map((a: any, i: number) => (
                <div key={i} className="action">
                  <div className="action-title"><span className="n">{i + 1}</span>{a.title}</div>
                  <div className="action-why">{a.why}</div>
                  {a.caution && <div className="action-caution">⚠ {a.caution}</div>}
                </div>
              ))}
            </div>
          </div>

          <div className="card">
            <h2>이 테마를 좋아하는 당신께</h2>
            {inp.watchlist?.length ? (
              <div className="rows">
                {inp.watchlist.map((w: any, i: number) => (
                  <div key={i} className="action">
                    <div className="action-title">
                      {w.name} {w.symbol && <span className="sym">{w.symbol}</span>}
                    </div>
                    <div className="action-why">{w.reason}</div>
                  </div>
                ))}
              </div>
            ) : <div className="empty">같은 테마의 관찰 후보가 아직 없습니다</div>}

            <h2 style={{ marginTop: 18 }}>놓치기 쉬운 위험</h2>
            <ul className="bullets">
              {arr(strat.risks).map((r: string, i: number) => <li key={i}>{r}</li>)}
            </ul>

            <details className="fold">
              <summary>근거 수치 보기</summary>
              <div className="rows" style={{ marginTop: 10 }}>
                {metrics && [
                  ["집중도 HHI", num(metrics.hhi).toFixed(3)],
                  ["최대 종목 비중", `${(num(metrics.top_weight) * 100).toFixed(1)}%`],
                  ["원화 / 달러 노출", `${(num(metrics.krw_weight) * 100).toFixed(0)}% / ${(num(metrics.usd_weight) * 100).toFixed(0)}%`],
                  ["현금 비중", `${(num(metrics.cash_weight) * 100).toFixed(1)}%`],
                  ["포트 변동성 (연율)", `${(num(metrics.port_vol_20d) * 100).toFixed(1)}%`],
                  ["KOSPI 변동성", `${(num(metrics.kospi_vol_20d) * 100).toFixed(1)}%`],
                  ["KOSPI 베타", num(metrics.beta_kospi).toFixed(2)],
                  ["60일 최대낙폭", `${(num(metrics.max_drawdown_60d) * 100).toFixed(1)}%`],
                  ["평가익 종목 비율", `${(num(metrics.win_rate) * 100).toFixed(0)}%`],
                ].map(([k, v]) => (
                  <div className="row" key={k as string}><span>{k}</span><span>{v}</span></div>
                ))}
              </div>
              <div className="note">
                이 숫자들은 <b>결정론적 코드가 계산</b>합니다. LLM 은 계산에 관여하지 않고
                해석·서술만 합니다. 생성 {strat.model} · {String(strat.as_of).slice(0, 16).replace("T", " ")}
                <br /><b>미래 예측이 아니라 현황 진단이며, 투자 권유가 아닙니다.</b>
              </div>
            </details>
          </div>
        </div>
      )}


      {inp.expert_views?.length > 0 && (
        <div className="card" style={{ marginBottom: 14 }}>
          <div className="chart-title">
            <h2 style={{ margin: 0 }}>전문가라면 이렇게 봅니다</h2>
            <span className="sym">같은 데이터, 세 직군의 다른 관점</span>
          </div>
          <div className="experts">
            {inp.expert_views.map((e: any, i: number) => (
              <div className="expert" key={i}>
                <div className="expert-role">{e.role}</div>
                <div className="expert-block">
                  <span className="expert-k">읽는 법</span>
                  <p>{e.reads}</p>
                </div>
                <div className="expert-block">
                  <span className="expert-k">실무라면</span>
                  <p>{e.would_do}</p>
                </div>
                <div className="expert-block blind">
                  <span className="expert-k">개인이 놓치는 것</span>
                  <p>{e.blind_spot}</p>
                </div>
              </div>
            ))}
          </div>
          <div className="note">
            직군별 표준 관행을 서술한 것이며 <b>가격 예측이 아닙니다.</b>
            인용된 수치는 전부 결정론적 코드가 계산한 값입니다.
          </div>
        </div>
      )}

      <div className="grid tiles">
        <Tile label="총 평가금액" value={`${won(total)}원`}
              sub={`국내 ${won(num(acc?.market_value_krw))}원 · 미국 $${won(num(acc?.market_value_usd))}`} />
        <Tile label="총 손익" value={`${pnl >= 0 ? "+" : ""}${won(pnl)}원`}
              tone={sign(pnl)} sub={`${pct(rate)} (총액 기준 자체계산)`} />
        <Tile label="일간 손익" value={`${dPnl >= 0 ? "+" : ""}${won(dPnl)}원`}
              tone={sign(dPnl)} sub={pct(dRate)} />
        <Tile label="예수금" value={`${won(num(acc?.cash_buying_power_krw))}원`}
              sub="미수 제외 현금 매수가능" />
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <h2>보유 종목</h2>
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th>종목</th><th>수량</th><th>평단</th><th>현재가</th>
                <th>평가금액</th><th>손익</th><th>수익률</th><th>일간</th><th>비용</th>
              </tr>
            </thead>
            <tbody>
              {holdings.map((h) => (
                <tr key={h.symbol}>
                  <td>
                    <div>{h.name}</div>
                    <div className="sym">{h.symbol} · {h.market_country}</div>
                  </td>
                  <td>{num(h.quantity).toLocaleString()}</td>
                  <td>{won(num(h.avg_price))}</td>
                  <td>{won(num(h.last_price))}</td>
                  <td>{won(num(h.market_value))}</td>
                  <td className={sign(num(h.pnl))}>{won(num(h.pnl))}</td>
                  <td className={sign(num(h.pnl_rate))}>{pct(num(h.pnl_rate))}</td>
                  <td className={sign(num(h.daily_pnl))}>{won(num(h.daily_pnl))}</td>
                  <td className="sym">
                    수수료 {won(num(h.commission))} / 세금 {won(num(h.tax))}
                  </td>
                </tr>
              ))}
              {!holdings.length && <tr><td colSpan={9} className="empty">보유 종목 없음</td></tr>}
            </tbody>
          </table>
        </div>
      </div>

      <div className="grid two">
        {charts.map((c) => {
          const pts = c.rows.slice().reverse().map((r) => ({ t: String(r.ts), v: num(r.close) }));
          const last = pts.at(-1)?.v ?? 0;
          const first = pts[0]?.v ?? 0;
          const chg = first ? last / first - 1 : 0;
          return (
            <div className="card" key={c.symbol}>
              <div className="chart-title">
                <h2 style={{ margin: 0 }}>{c.name} · 일봉 120일</h2>
                <div className="chart-now">
                  {won(last)} <span className={sign(chg)} style={{ fontSize: 13 }}>{pct(chg)}</span>
                </div>
              </div>
              <LineChart data={pts} />
            </div>
          );
        })}

        <div className="card">
          <div className="chart-title">
            <h2 style={{ margin: 0 }}>KOSPI · 일봉 120일</h2>
            <div className="chart-now">
              {num(kospi[0]?.close).toFixed(2)}
            </div>
          </div>
          <LineChart
            data={kospi.slice().reverse().map((r) => ({ t: String(r.ts), v: num(r.close) }))}
            color="var(--series-2)" decimals={0}
          />
        </div>
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <h2>KOSPI 투자자별 순매수 · 최근 20영업일</h2>
        <FlowChart series={[
          { name: "개인", color: "var(--series-1)", points: flowBy("individual") },
          { name: "외국인", color: "var(--series-2)", points: flowBy("foreigner") },
          { name: "기관", color: "var(--series-3)", points: flowBy("institution") },
        ]} />
        <div className="note">
          ⚠️ 토스 API 는 <b>시장 전체</b> 수급만 제공합니다. 개별종목 기관·외국인 순매수는
          제공되지 않으므로, 종목 단위 수급이 필요하면 외부 소스를 붙여야 합니다.
        </div>
      </div>


      {/* ── 분석 레이어: HTS 에 없는 부분 ── */}
      <div style={{ margin: "26px 0 12px" }}>
        <h2 style={{ fontSize: 13, marginBottom: 2 }}>분석 · 여론</h2>
        <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
          뉴스·커뮤니티를 수집해 Gemini 로 감성 분류·의견 추출·브리핑 생성
        </div>
      </div>

      {briefs.length > 0 && (
        <div className="grid two">
          {briefs.map((b: any) => (
            <div className="card" key={b.symbol}>
              <div className="chart-title">
                <h2 style={{ margin: 0 }}>{b.name ?? b.symbol} · 브리핑</h2>
                <span className={`badge ${b.stance}`}>{
                  { positive: "긍정", negative: "부정", mixed: "혼조", neutral: "중립" }[b.stance as string] ?? b.stance
                }</span>
              </div>
              <p style={{ margin: "8px 0 12px", fontSize: 15, lineHeight: 1.55 }}>{b.headline}</p>
              <ul className="bullets">
                {(Array.isArray(b.bullets) ? b.bullets : JSON.parse(b.bullets || "[]")).map((x: string, i: number) => (
                  <li key={i}>{x}</li>
                ))}
              </ul>
              <div className="note" style={{ marginTop: 12 }}>
                근거: 뉴스 {b.inputs?.news_titles ?? 0}건 · 감성분석 {b.inputs?.sentiment_posts ?? 0}건 ·
                애널리스트 {b.inputs?.analyst_views ?? 0}건 · 60일 등락 {b.inputs?.price_change_60d_pct ?? "—"}%
                <br />생성 {b.model} · {String(b.as_of).slice(0, 16).replace("T", " ")}
                <br /><b>투자 추천이 아니라 수집된 정보의 요약입니다.</b>
              </div>
            </div>
          ))}
        </div>
      )}


      {rebal && (() => {
        const moves = arr(rebal.target?.moves ?? rebal.target);
        const g = rebal.guardrails ?? {};
        const label: Record<string, string> = {
          trim: "축소", hold: "유지", add: "추가", new: "신규",
        };
        return (
          <div className="card" style={{ marginBottom: 14 }}>
            <div className="chart-title">
              <h2 style={{ margin: 0 }}>리밸런싱 제안</h2>
              <span className="sym">목표 비중은 규칙으로 계산 · 설명만 AI</span>
            </div>
            <p style={{ margin: "8px 0 14px", fontSize: 14, lineHeight: 1.6 }}>
              {rebal.rationale}
            </p>

            <div className="scroll">
              <table>
                <thead>
                  <tr><th>종목</th><th>조치</th><th>목표 비중</th><th>이유</th></tr>
                </thead>
                <tbody>
                  {moves.map((m: any, i: number) => (
                    <tr key={i}>
                      <td>
                        <div>{m.name}</div>
                        {m.symbol && <div className="sym">{m.symbol}</div>}
                      </td>
                      <td>
                        <span className={`badge mv-${m.action}`}>
                          {label[m.action] ?? m.action}
                        </span>
                      </td>
                      <td>{m.target_weight != null
                        ? `${(num(m.target_weight) * 100).toFixed(1)}%` : "—"}</td>
                      <td style={{ textAlign: "left", whiteSpace: "normal",
                                   maxWidth: 380, fontSize: 12.5,
                                   color: "var(--text-secondary)" }}>{m.why}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {arr(rebal.target?.theme_gaps).length > 0 && (
              <div style={{ marginTop: 14 }}>
                <div className="hero-label">비어 있는 자산군·테마</div>
                <div className="chips">
                  {arr(rebal.target?.theme_gaps).map((t: string) => (
                    <span className="chip" key={t}>{t}</span>
                  ))}
                </div>
              </div>
            )}

            {arr(rebal.target?.cautions).length > 0 && (
              <ul className="bullets" style={{ marginTop: 12 }}>
                {arr(rebal.target?.cautions).map((c: string, i: number) => (
                  <li key={i}>{c}</li>
                ))}
              </ul>
            )}

            <details className="fold">
              <summary>적용된 규칙 보기</summary>
              <div className="rows" style={{ marginTop: 10 }}>
                {[
                  ["단일 종목 상한", `${(num(g.max_single_weight) * 100).toFixed(0)}%`, g.max_single_weight_why],
                  ["최소 현금 비중", `${(num(g.min_cash_weight) * 100).toFixed(0)}%`, g.min_cash_weight_why],
                  ["최소 보유 종목", `${g.min_positions}개`, g.min_positions_why],
                  ["회차당 최대 교체", `${(num(g.max_turnover_per_round) * 100).toFixed(0)}%`, g.max_turnover_per_round_why],
                ].map(([k, v, why]) => (
                  <div key={k as string} style={{ paddingBottom: 8 }}>
                    <div className="row"><span>{k}</span><span>{v}</span></div>
                    {why && <div className="sym" style={{ lineHeight: 1.5 }}>{why}</div>}
                  </div>
                ))}
              </div>
              <div className="note">
                {g.note}
                <br />레버리지·인버스 ETF는 후보에서 제외했고, 실재가 확인되지 않은
                종목은 자동으로 걸러냅니다.
                <br /><b>매매 권유가 아니며 세금·거래비용은 반영되어 있지 않습니다.</b>
              </div>
            </details>
          </div>
        );
      })()}

      <div className="grid two">
        <div className="card">
          <h2>종목별 여론 · 최근 30일</h2>
          {sentiment.length ? (
            <div className="rows">
              {sentiment.map((s: any) => {
                const tot = s.pos + s.neu + s.neg || 1;
                return (
                  <div key={s.symbol} style={{ marginBottom: 4 }}>
                    <div className="row" style={{ marginBottom: 5 }}>
                      <span>{s.name ?? s.symbol} <span className="sym">{s.n}건</span></span>
                      <span className={num(s.avg_score) > 0.05 ? "up" : num(s.avg_score) < -0.05 ? "down" : ""}>
                        평균 {num(s.avg_score) >= 0 ? "+" : ""}{num(s.avg_score).toFixed(2)}
                      </span>
                    </div>
                    <div className="stack">
                      <i style={{ width: `${(s.pos / tot) * 100}%`, background: "var(--series-2)" }} title={`긍정 ${s.pos}`} />
                      <i style={{ width: `${(s.neu / tot) * 100}%`, background: "var(--axis)" }} title={`중립 ${s.neu}`} />
                      <i style={{ width: `${(s.neg / tot) * 100}%`, background: "var(--series-4)" }} title={`부정 ${s.neg}`} />
                    </div>
                    <div className="sym" style={{ marginTop: 3 }}>
                      긍정 {s.pos} · 중립 {s.neu} · 부정 {s.neg}
                    </div>
                  </div>
                );
              })}
            </div>
          ) : <div className="empty">분석된 글 없음</div>}
          <div className="legend">
            <span><i style={{ background: "var(--series-2)" }} />긍정</span>
            <span><i style={{ background: "var(--axis)" }} />중립</span>
            <span><i style={{ background: "var(--series-4)" }} />부정</span>
          </div>
        </div>

        <div className="card">
          <h2>애널리스트 의견 (뉴스에서 추출)</h2>
          {views.length ? (
            <div className="rows">
              {views.map((v: any, i: number) => (
                <div key={i} style={{ paddingBottom: 9, borderBottom: "1px solid var(--grid)" }}>
                  <div className="row">
                    <span>
                      <b>{v.name ?? v.symbol}</b> · {v.broker ?? "증권사 미상"}
                      {v.rating_norm && <span className="badge rating">{v.rating_norm}</span>}
                    </span>
                    <span>{v.target_price ? `목표 ${num(v.target_price).toLocaleString()}` : ""}</span>
                  </div>
                  {v.thesis && <div className="sym" style={{ marginTop: 4, lineHeight: 1.5 }}>{v.thesis}</div>}
                  {v.source_url && (
                    <a className="src" href={v.source_url} target="_blank" rel="noreferrer">
                      {(v.source_title ?? "").slice(0, 52)} ↗
                    </a>
                  )}
                </div>
              ))}
            </div>
          ) : <div className="empty">추출된 의견 없음</div>}
          <div className="note">
            증권사 리포트 <b>원문은 저장하지 않습니다</b>(저작권). 뉴스 기사에서
            목표주가·투자의견 같은 사실 정보만 구조화해 보관하고 원문은 링크로 참조합니다.
          </div>
        </div>
      </div>


      <div className="grid two">
        <div className="card">
          <h2>내 종목을 담은 기관 (SEC 13F)</h2>
          {instMine.length ? (
            <div className="scroll">
              <table>
                <thead><tr><th>종목</th><th>기관</th><th>평가액</th><th>포트 비중</th><th>기준</th></tr></thead>
                <tbody>
                  {instMine.map((r: any, i: number) => (
                    <tr key={i}>
                      <td><b>{r.ticker}</b> <span className="sym">{(r.issuer ?? "").slice(0, 18)}</span></td>
                      <td className="sym">{r.institution}</td>
                      <td>${(num(r.value_usd) / 1e6).toFixed(1)}M</td>
                      <td>{(num(r.weight) * 100).toFixed(2)}%</td>
                      <td className="sym">{String(r.period).slice(0, 10)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : <div className="empty">보유 미국 종목을 담은 기관이 없습니다</div>}
          <div className="note">
            <b>후행 지표입니다.</b> 13F 는 분기말 기준이고 최대 45일 뒤 공시됩니다 —
            &lsquo;지금&rsquo; 보유가 아닙니다. 롱 주식만 담기고 공매도·채권은 빠집니다.
            매매 신호로 쓰지 마세요.
          </div>
        </div>

        <div className="card">
          <h2>추적 중인 기관 · 최대 비중 종목</h2>
          <div className="scroll">
            <table>
              <thead><tr><th>기관</th><th>최대 비중 종목</th><th>비중</th><th>보유수</th><th>기준</th></tr></thead>
              <tbody>
                {instTop.map((r: any, i: number) => {
                  const stale = new Date(r.period) < new Date(Date.now() - 200 * 864e5);
                  return (
                    <tr key={i}>
                      <td>
                        <div>{r.institution}</div>
                        <div className="sym">AUM ${(num(r.aum) / 1e9).toFixed(0)}B</div>
                      </td>
                      <td className="sym">{(r.issuer ?? "").slice(0, 22)} {r.ticker && <b>{r.ticker}</b>}</td>
                      <td>{(num(r.weight) * 100).toFixed(1)}%</td>
                      <td>{r.n_holdings}</td>
                      <td className={stale ? "down" : "sym"}>
                        {String(r.period).slice(0, 10)}{stale ? " ⚠" : ""}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="note">
            ⚠ 표시는 기준일이 200일 이상 지난 묵은 데이터입니다.
            대형 운용사일수록 공시가 늦습니다.
          </div>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <h2>수집된 최신 글</h2>
        <div className="scroll">
          <table>
            <thead><tr><th>제목</th><th>소스</th><th>종목</th><th>감성</th><th>시각</th></tr></thead>
            <tbody>
              {posts.map((p: any, i: number) => (
                <tr key={i}>
                  <td style={{ maxWidth: 460, whiteSpace: "normal" }}>
                    {p.url ? <a className="src" href={p.url} target="_blank" rel="noreferrer">{p.title}</a> : p.title}
                  </td>
                  <td className="sym">{p.source}</td>
                  <td className="sym">{p.symbol ?? "—"}</td>
                  <td className={p.label === "positive" ? "up" : p.label === "negative" ? "down" : ""}>
                    {p.label ? `${p.label === "positive" ? "긍정" : p.label === "negative" ? "부정" : "중립"} ${num(p.score) >= 0 ? "+" : ""}${num(p.score).toFixed(2)}` : "미분석"}
                  </td>
                  <td className="sym">{String(p.posted_at).slice(5, 16).replace("T", " ")}</td>
                </tr>
              ))}
              {!posts.length && <tr><td colSpan={5} className="empty">수집된 글 없음</td></tr>}
            </tbody>
          </table>
        </div>
        <div className="note">
          소스: {srcStats.map((s: any) => `${s.source} ${s.n}건`).join(" · ")}
          <br />Reddit 은 2025-11 Responsible Builder Policy 로 API 앱 승인이 필요해져
          <b> RSS 피드</b>로 수집합니다(승인 불필요·배포 목적 발행물).
        </div>
      </div>

      <div className="grid two">
        <div className="card">
          <h2>수집 작업 상태</h2>
          <div className="rows">
            {sys.jobs.map((j: any) => (
              <div className="row" key={j.job_name}>
                <span>
                  <i className="dot" style={{ background: j.ok ? "var(--good)" : "var(--critical)" }} />
                  {j.job_name}
                </span>
                <span>{j.rows ?? 0}행 · {j.started_at ? String(j.started_at).slice(5, 16).replace("T", " ") : "—"}</span>
              </div>
            ))}
            {!sys.jobs.length && <div className="empty">실행 이력 없음</div>}
          </div>
          <div className="note">
            DB {(sys.dbBytes / 1024 / 1024).toFixed(1)}MB / 512MB (Neon 무료 상한)
            <div className="meter"><i style={{ width: `${Math.min(100, sys.dbBytes / (512 * 1024 * 1024) * 100)}%` }} /></div>
          </div>
        </div>

        <div className="card">
          <h2>Rate limit 관측 · 24시간</h2>
          <div className="rows">
            {sys.rateLimits.map((r: any) => (
              <div className="row" key={r.group_name}>
                <span>
                  <i className="dot" style={{ background: r.hit_429 ? "var(--critical)" : "var(--good)" }} />
                  {r.group_name}
                </span>
                <span>한도 {r.lim}/s · 최저잔량 {r.worst}</span>
              </div>
            ))}
            {!sys.rateLimits.length && <div className="empty">관측 없음</div>}
          </div>
          <div className="note">
            한도는 사전 공지 없이 조정될 수 있어 하드코딩하지 않고
            <code> X-RateLimit-*</code> 헤더를 그대로 기록합니다.
          </div>
        </div>
      </div>
          <Advisor />
</div>
  );
}
