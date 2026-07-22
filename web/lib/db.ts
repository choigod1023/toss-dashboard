import { Pool } from "pg";

/** ⚠️ 드라이버 선택이 성능을 좌우한다.
 *
 *  처음엔 @neondatabase/serverless(HTTP)를 썼는데, 쿼리마다 왕복을 새로
 *  맺어서 한국↔Neon 지연 200ms 가 쿼리 수만큼 곱해졌다.
 *  15개를 Promise.all 로 묶어도 3.1초 — 병렬이 되지 않았다.
 *
 *  Neon 문서 권장대로 **모듈 스코프 pg 풀**로 바꾼다. 로컬 dev 와
 *  Vercel Fluid compute 는 프로세스가 요청 간에 살아있어서 연결을
 *  재사용할 수 있다. (완전 격리형 서버리스라면 HTTP 드라이버가 맞다)
 */
let _pool: Pool | null = null;

function pool(): Pool {
  if (!_pool) {
    _pool = new Pool({
      connectionString: process.env.DATABASE_URL!,
      max: 12,
      idleTimeoutMillis: 30_000,
      connectionTimeoutMillis: 15_000,
    });
    _pool.on("error", (e) => console.error("[pg pool]", e.message));
    // Vercel Fluid compute 는 인스턴스가 잠들기 전에 유휴 연결을 정리해야 한다
    if (process.env.VERCEL) {
      import("@vercel/functions")
        .then((m) => m.attachDatabasePool?.(_pool as any))
        .catch(() => {});
    }
  }
  return _pool;
}

/** 태그드 템플릿을 그대로 쓰기 위한 얇은 래퍼.
 *  sql`select ... ${v}` → $1 파라미터 바인딩 (문자열 연결 아님) */
export function sql() {
  return async (strings: TemplateStringsArray, ...values: unknown[]) => {
    const text = strings.reduce(
      (acc, s, i) => acc + s + (i < values.length ? `$${i + 1}` : ""), "");
    const r = await pool().query(text, values as any[]);
    return r.rows;
  };
}

export type Account = {
  snapshot_date: string;
  market_value_total_krw: number | null;
  total_purchase_total_krw: number | null;
  pnl_total_krw: number | null;
  pnl_rate_computed: number | null;
  pnl_rate_api: number | null;
  daily_pnl_total_krw: number | null;
  daily_pnl_rate: number | null;
  cash_buying_power_krw: number | null;
  market_value_krw: number | null;
  market_value_usd: number | null;
  exchange_rate: number | null;
};

export type Holding = {
  symbol: string; name: string; market_country: string; currency: string;
  quantity: number; avg_price: number; last_price: number;
  market_value: number; pnl: number; pnl_rate: number;
  daily_pnl: number; daily_pnl_rate: number;
  commission: number | null; tax: number | null;
};

export type Candle = { ts: string; close: number; open: number; high: number; low: number; volume: number | null };

export const num = (v: unknown): number =>
  v === null || v === undefined ? 0 : typeof v === "number" ? v : Number(v);

export async function getAccount(userId: string): Promise<Account | null> {
  const r = await sql()`
    SELECT * FROM account_snapshot WHERE user_id = ${userId}
    ORDER BY snapshot_date DESC LIMIT 1` as Account[];
  return r[0] ?? null;
}

export async function getHoldings(userId: string): Promise<Holding[]> {
  return await sql()`
    SELECT * FROM holding_snapshot
    WHERE user_id = ${userId} AND snapshot_date = (
      SELECT max(snapshot_date) FROM holding_snapshot WHERE user_id = ${userId})
    ORDER BY market_value DESC NULLS LAST` as Holding[];
}

export async function getCandles(symbol: string, days = 120): Promise<Candle[]> {
  return await sql()`
    SELECT ts, open, high, low, close, volume FROM candle
    WHERE symbol = ${symbol} AND interval = '1d'
    ORDER BY ts DESC LIMIT ${days}` as Candle[];
}

/** 여러 종목 캔들을 **한 번의 왕복**으로. 종목마다 쿼리를 날리면
 *  DB 가 멀리 있을 때(왕복 0.2~0.4s) 그대로 지연이 곱해진다. */
export async function getCandlesBulk(symbols: string[], days = 120) {
  if (!symbols.length) return new Map<string, Candle[]>();
  const rows = (await sql()`
    SELECT symbol, ts, open, high, low, close, volume FROM (
      SELECT *, row_number() OVER (PARTITION BY symbol ORDER BY ts DESC) AS rn
      FROM candle WHERE symbol = ANY(${symbols}) AND interval = '1d'
    ) t WHERE rn <= ${days}
    ORDER BY symbol, ts DESC`) as (Candle & { symbol: string })[];
  const m = new Map<string, Candle[]>();
  for (const r of rows) {
    if (!m.has(r.symbol)) m.set(r.symbol, []);
    m.get(r.symbol)!.push(r);
  }
  return m;
}

export async function getWatched(): Promise<{ symbol: string; name: string }[]> {
  return await sql()`
    SELECT symbol, name FROM stock WHERE is_watched ORDER BY symbol` as any;
}

export async function getIndicator(symbol: string, days = 120): Promise<Candle[]> {
  return await sql()`
    SELECT ts, open, high, low, close, NULL::numeric AS volume
    FROM market_indicator_candle
    WHERE symbol = ${symbol} AND interval = '1d'
    ORDER BY ts DESC LIMIT ${days}` as Candle[];
}

export async function getInvestorFlow(market = "KOSPI", days = 20) {
  return await sql()`
    SELECT trade_date, investor, (buy_amount - sell_amount) AS net
    FROM investor_trading
    WHERE market = ${market} AND interval = '1d'
      AND investor IN ('individual','foreigner','institution')
    ORDER BY trade_date DESC LIMIT ${days * 3}` as
    { trade_date: string; investor: string; net: number }[];
}

export async function getSystem() {
  const jobs = await sql()`
    SELECT DISTINCT ON (job_name) job_name, started_at, ended_at, ok, rows, error
    FROM job_run ORDER BY job_name, started_at DESC` as any[];
  const size = await sql()`
    SELECT pg_database_size(current_database()) AS bytes` as any[];
  const rl = await sql()`
    SELECT group_name, max(limit_value) AS lim, min(remaining) AS worst,
           bool_or(was_429) AS hit_429
    FROM rate_limit_observation
    WHERE observed_at > now() - interval '24 hours'
    GROUP BY group_name ORDER BY group_name` as any[];
  return { jobs, dbBytes: num(size[0]?.bytes), rateLimits: rl };
}

// ── 분석 레이어 (HTS 에 없는 부분) ──
export async function getBriefings() {
  return await sql()`
    SELECT DISTINCT ON (symbol) b.symbol, s.name, b.as_of, b.headline,
           b.bullets, b.stance, b.inputs, b.model
    FROM briefing b LEFT JOIN stock s ON s.symbol = b.symbol
    ORDER BY b.symbol, b.as_of DESC` as any[];
}

export async function getAnalystViews() {
  return await sql()`
    SELECT a.symbol, s.name, a.broker, a.analyst, a.rating, a.rating_norm,
           a.target_price, a.currency, a.thesis, a.source_url, a.source_title, a.as_of
    FROM analyst_view a LEFT JOIN stock s ON s.symbol = a.symbol
    ORDER BY a.as_of DESC LIMIT 20` as any[];
}

export async function getSentimentBySymbol() {
  return await sql()`
    SELECT p.symbol, s2.name,
           count(*)::int AS n,
           round(avg(s.score)::numeric, 3) AS avg_score,
           sum((s.label='positive')::int)::int AS pos,
           sum((s.label='neutral')::int)::int  AS neu,
           sum((s.label='negative')::int)::int AS neg
    FROM sentiment_score s
    JOIN community_post p ON p.id = s.post_id AND p.posted_at = s.posted_at
    LEFT JOIN stock s2 ON s2.symbol = p.symbol
    WHERE p.symbol IS NOT NULL AND s.posted_at > now() - interval '30 days'
    GROUP BY p.symbol, s2.name ORDER BY n DESC` as any[];
}

export async function getRecentPosts(limit = 14) {
  return await sql()`
    SELECT p.source, p.symbol, p.title, p.url, p.posted_at,
           s.label, s.score
    FROM community_post p
    LEFT JOIN sentiment_score s ON s.post_id = p.id AND s.posted_at = p.posted_at
    ORDER BY p.posted_at DESC LIMIT ${limit}` as any[];
}

export async function getSourceStats() {
  return await sql()`
    SELECT source, count(*)::int AS n, count(symbol)::int AS matched,
           max(posted_at) AS latest
    FROM community_post GROUP BY source ORDER BY n DESC` as any[];
}

export async function getStrategy(userId: string) {
  const r = await sql()`
    SELECT as_of, regime, diagnosis, actions, risks, inputs, model
    FROM strategy_note WHERE user_id = ${userId}
    ORDER BY as_of DESC LIMIT 1` as any[];
  return r[0] ?? null;
}

export async function getRegimes() {
  return await sql()`
    SELECT DISTINCT ON (source) source, as_of, score, rating, components
    FROM market_regime ORDER BY source, as_of DESC` as any[];
}

export async function getMetrics(userId: string) {
  const r = await sql()`
    SELECT * FROM portfolio_metrics WHERE user_id = ${userId}
    ORDER BY as_of DESC LIMIT 1` as any[];
  return r[0] ?? null;
}

export async function getInstitutionsFor(tickers: string[]) {
  if (!tickers.length) return [];
  return await sql()`
    SELECT ticker, issuer, institution, period, value_usd, weight, shares
    FROM institution_holding
    WHERE ticker = ANY(${tickers}) ORDER BY value_usd DESC LIMIT 40` as any[];
}

export async function getInstitutionTop() {
  // ⚠️ 원래는 DISTINCT ON 각 행마다 상관 서브쿼리 2개를 13,879행 테이블에
  //    돌려서 **18.7초** 걸렸다. 집계를 CTE 로 한 번만 계산하고 조인한다.
  //    (cik, period) 인덱스와 함께 213ms 로 줄었다.
  return await sql()`
    WITH agg AS (
      SELECT cik, period, count(*) AS n_holdings, sum(value_usd) AS aum
      FROM institution_holding GROUP BY cik, period
    ), top AS (
      SELECT DISTINCT ON (institution)
             cik, institution, issuer, ticker, value_usd, weight, period, filed_at
      FROM institution_holding ORDER BY institution, weight DESC
    )
    SELECT t.institution, t.issuer, t.ticker, t.value_usd, t.weight,
           t.period, t.filed_at, a.n_holdings::int AS n_holdings, a.aum
    FROM top t JOIN agg a ON a.cik = t.cik AND a.period = t.period
    ORDER BY t.institution` as any[];
}

/** 워커가 보고한 outbound IP. 사용자가 토스 허용 IP 에 등록해야 할 주소.
 *  env 에 박지 않는다 — 호스팅이 IP 를 바꾸면 워커가 다음 실행에서
 *  자동으로 갱신하고, 여기 읽는 값도 따라 바뀐다. */
export async function getWorkerIps() {
  // ⚠️ source='local' 은 개발자 노트북에서 돌린 기록이다.
  //    사용자가 그 IP 를 토스에 등록해봐야 아무 소용이 없다
  //    (실제 수집은 배포 서버에서 돈다). 운영 소스만 노출한다.
  return await sql()`
    SELECT host(ip) AS ip, source, last_seen, run_count
    FROM worker_ip
    WHERE last_seen > now() - interval '30 days'
      AND source NOT IN ('unknown', 'actions')   -- actions 는 IP 가 매번 바뀌어 등록 불가
    ORDER BY last_seen DESC` as
    { ip: string; source: string; last_seen: string; run_count: number }[];
}

/** 리밸런싱 계획. 목표 비중은 코드가 계산한 값이고
 *  target.moves 의 설명만 LLM 이 붙였다. */
export async function getRebalance(userId: string) {
  const r = (await sql()`
    SELECT as_of, current, target, rationale, guardrails, model
    FROM rebalance_plan WHERE user_id = ${userId}
    ORDER BY as_of DESC LIMIT 1`) as any[];
  return r[0] ?? null;
}
