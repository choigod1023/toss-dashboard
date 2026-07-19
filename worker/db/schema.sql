-- ============================================================
--  toss-dashboard 스키마
-- ============================================================
--  실측된 환경 제약 (추측 아님, 실제 확인함):
--
--   • Neon 무료 티어 상한 = 512MB  (neon.max_cluster_size)
--   • TimescaleDB 2.17.1 이지만 라이선스가 **apache** 라서
--       ❌ 압축(compress)        ❌ 보존정책(add_retention_policy)
--       ❌ 연속집계(continuous aggregate)
--     전부 사용 불가. 하이퍼테이블 파티셔닝과 drop_chunks() 만 됨.
--     → 보존은 워커가 drop_chunks() 를 직접 호출해서 처리한다.
--
--  ── 512MB 용량 예산 ──────────────────────────────────────
--   1분봉 1행 ≈ 150B(인덱스 포함). 200종목 × 390분 = 78,000행/일
--   → 하루 약 12MB. 압축이 없으므로 200종목 전량 장기보관은
--     40일이면 상한을 친다. 따라서:
--
--     • 원시 틱·호가 스냅샷: 저장하지 않음 (화면 표시 전용)
--     • 1분봉: 관심종목만, 보존 90일 (drop_chunks)
--     • 일봉:  전 종목 장기보관 (200종목×250일×10년 ≈ 75MB)
--     • 커뮤니티 원문: 본문 길이 제한 + 보존 180일
--
--  ── look-ahead bias 방지 ─────────────────────────────────
--   센티먼트·팩터 점수는 "그 시점에 알 수 있었던 값"으로 고정
--   저장한다. 나중에 재계산해 덮어쓰면 백테스팅이 오염된다.
--   그래서 UPDATE 하지 않고 (as_of, ...) 로 append 한다.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;


-- ────────────────────────────────────────────────────────────
--  1. 토큰 — 단일 발급 지점
-- ────────────────────────────────────────────────────────────
--  스펙 원문: "client 당 유효한 access token 은 1 개입니다.
--             재발급 시 이전에 발급된 token 은 즉시 무효화됩니다."
--  → 여러 프로세스가 각자 발급하면 서로를 401 로 만든다.
--    id=1 단일 행으로 강제하고, 발급은 pg_advisory_lock 으로 직렬화.
--    (락 키는 worker/toss/token.py 의 TOKEN_LOCK_KEY 와 일치)
CREATE TABLE IF NOT EXISTS toss_token (
    id           smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    access_token text        NOT NULL,
    token_type   text        NOT NULL DEFAULT 'Bearer',
    issued_at    timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE toss_token IS
    '토스 access token 단일 저장소. 재발급은 advisory lock 으로 직렬화할 것.';


-- ────────────────────────────────────────────────────────────
--  2. 종목 마스터 / 참조 데이터
-- ────────────────────────────────────────────────────────────
--  GET /api/v1/stocks (최대 200건 일괄). 영업일 단위 갱신이므로
--  짧은 주기로 폴링하지 말 것 (STOCK 그룹 5 TPS).
CREATE TABLE IF NOT EXISTS stock (
    symbol         text PRIMARY KEY,            -- KRX 6자리 / US 티커
    name           text,
    english_name   text,
    isin_code      text,
    market         text,                        -- KOSPI|KOSDAQ|NASDAQ|NYSE ...
    security_type  text,                        -- STOCK|ETF ...
    status         text,                        -- ACTIVE|DELISTED ...
    currency       text,
    list_date      date,
    delist_date    date,                        -- 생존편향 보정에 필요
    shares_outstanding numeric(24,0),           -- 시가총액 계산용
    -- koreanMarketDetail
    liquidation_trading    boolean,             -- 정리매매
    krx_trading_suspended  boolean,
    nxt_trading_suspended  boolean,
    nxt_supported          boolean,
    is_watched     boolean     NOT NULL DEFAULT false,  -- 1분봉 수집 대상
    sector         text,                        -- 토스 미제공 → 외부 소스
    raw            jsonb,
    updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_stock_watched ON stock (is_watched) WHERE is_watched;

--  GET /api/v1/stocks/{symbol}/warnings — VI 발동·투자경고·정리매매
--  주문 전 자동 차단 필터로 사용한다.
CREATE TABLE IF NOT EXISTS stock_warning (
    symbol     text        NOT NULL REFERENCES stock(symbol) ON DELETE CASCADE,
    kind       text        NOT NULL,
    start_date date        NOT NULL,
    end_date   date,
    detail     jsonb,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, kind, start_date)
);

--  GET /api/v1/commissions — 실제 수수료율.
--  백테스팅에서 추정치 대신 이 값을 쓴다.
CREATE TABLE IF NOT EXISTS commission (
    market     text PRIMARY KEY,                -- KR | US
    detail     jsonb       NOT NULL,
    fetched_at timestamptz NOT NULL DEFAULT now()
);


-- ────────────────────────────────────────────────────────────
--  3. 시세 — 캔들 (하이퍼테이블)
-- ────────────────────────────────────────────────────────────
--  GET /api/v1/candles — interval 은 '1m','1d' 2종뿐, 1회 최대 200봉.
--  before 커서로 과거를 거슬러 올라가 축적한다.
--  adjusted=true (수정주가) 로 받는다 — 백테스팅 정확도에 필수.
--  5분/15분/주봉 등은 저장하지 않고 1분봉에서 리샘플링한다.
CREATE TABLE IF NOT EXISTS candle (
    symbol   text        NOT NULL,
    interval text        NOT NULL CHECK (interval IN ('1m', '1d')),
    ts       timestamptz NOT NULL,
    open     numeric(20,6) NOT NULL,
    high     numeric(20,6) NOT NULL,
    low      numeric(20,6) NOT NULL,
    close    numeric(20,6) NOT NULL,
    volume   numeric(24,6),
    currency text,
    adjusted boolean     NOT NULL DEFAULT true,
    PRIMARY KEY (symbol, interval, ts)
);
SELECT create_hypertable('candle', 'ts',
                         chunk_time_interval => interval '7 days',
                         if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_candle_sym_int_ts ON candle (symbol, interval, ts DESC);

--  시장지표 — 심볼 카탈로그 8종 고정:
--    KOSPI, KOSDAQ, KR_BOND_2Y/3Y/5Y/10Y/20Y/30Y
--  ⚠️ S&P500·NASDAQ 등 미국 '지수'는 토스 API 에 없다 (개별종목은 있음).
--     필요하면 외부 소스로 별도 수집.
--  분봉은 지수(KOSPI/KOSDAQ)만 지원, 국채는 일봉만.
CREATE TABLE IF NOT EXISTS market_indicator_candle (
    symbol   text        NOT NULL,
    interval text        NOT NULL CHECK (interval IN ('1m', '1d')),
    ts       timestamptz NOT NULL,
    open     numeric(20,6),
    high     numeric(20,6),
    low      numeric(20,6),
    close    numeric(20,6) NOT NULL,
    PRIMARY KEY (symbol, interval, ts)
);
SELECT create_hypertable('market_indicator_candle', 'ts',
                         chunk_time_interval => interval '30 days',
                         if_not_exists => TRUE);

--  GET /api/v1/market-indicators/{symbol}/investor-trading
--  ⚠️ KOSPI/KOSDAQ '시장 전체' 단위만 제공된다.
--     명세서 §6.1 의 "개별종목 기관/외국인 순매수"는 이걸로 안 된다.
--     개별종목 수급이 필요하면 외부 소스가 필요하다.
CREATE TABLE IF NOT EXISTS investor_trading (
    market      text  NOT NULL,                 -- KOSPI | KOSDAQ
    interval    text  NOT NULL DEFAULT '1d'     -- 1d|1w|1mo|1y (필수 파라미터)
                CHECK (interval IN ('1d','1w','1mo','1y')),
    trade_date  date  NOT NULL,
    investor    text  NOT NULL,                 -- individual|foreigner|institution|otherCorporation
    buy_amount  numeric(24,2),
    sell_amount numeric(24,2),
    breakdown   jsonb,                          -- 기관 세부 항목
    updated_at  timestamptz,                    -- 당일치는 장중 갱신되는 잠정치
    PRIMARY KEY (market, interval, trade_date, investor)
);


-- ────────────────────────────────────────────────────────────
--  4. 계좌 — 일별 스냅샷
-- ────────────────────────────────────────────────────────────
--  ⚠️ 토스 API 는 '기간 수익률'을 제공하지 않는다.
--     명세서 §3.1 의 일간/주간/월간 수익률은 이 스냅샷을
--     워커가 매일 적재해서 자체 계산하는 수밖에 없다.
--     → 하루라도 빠지면 그날 수익률에 구멍이 생긴다.
CREATE TABLE IF NOT EXISTS account_snapshot (
    snapshot_date  date PRIMARY KEY,
    -- ⚠️ 토스의 krw/usd 필드는 '환산'이 아니라 **통화별 소계**다.
    --    marketValue.amount.krw = 국내주식 평가액, .usd = 미국주식 평가액.
    --    총액을 보려면 krw + usd*환율 로 직접 합쳐야 한다.
    --    (그냥 krw 만 쓰면 미국 주식이 통째로 누락된다)
    total_purchase_krw      numeric(24,2),   -- 국내분 매입
    total_purchase_usd      numeric(24,2),   -- 미국분 매입
    market_value_krw        numeric(24,2),   -- 국내분 평가
    market_value_usd        numeric(24,2),   -- 미국분 평가
    market_value_total_krw  numeric(24,2),   -- ★ krw + usd*fx (실제 총평가)
    total_purchase_total_krw numeric(24,2),  -- ★ 매입 총액 환산
    pnl_krw                 numeric(24,2),
    pnl_usd                 numeric(24,2),
    pnl_total_krw           numeric(24,2),   -- ★ 실제 총손익
    pnl_rate_api            numeric(12,6),   -- API 가 준 블렌디드 수익률
    pnl_rate_computed       numeric(12,6),   -- ★ 총액 기준 자체 계산
    daily_pnl_krw           numeric(24,2),
    daily_pnl_usd           numeric(24,2),
    daily_pnl_total_krw     numeric(24,2),
    daily_pnl_rate          numeric(12,6),
    cash_buying_power_krw   numeric(24,2),
    cash_buying_power_usd   numeric(24,2),
    exchange_rate           numeric(20,6),
    raw                     jsonb,
    created_at              timestamptz NOT NULL DEFAULT now()
);



--  GET /api/v1/holdings — 손익률은 원화(KRW) 환산 기준
--  실측된 응답 필드 (2026-07-19 확인):
--    symbol, name, marketCountry, currency, quantity,
--    averagePurchasePrice, cost, lastPrice, marketValue,
--    profitLoss, dailyProfitLoss
--  ※ dailyProfitLoss 가 실제로 제공된다 — '일간' 손익은 API 가 준다.
--    다만 주간/월간은 여전히 없으므로 account_snapshot 누적이 필요하다.
CREATE TABLE IF NOT EXISTS holding_snapshot (
    snapshot_date  date NOT NULL,
    symbol         text NOT NULL,
    name           text,
    market_country text,                        -- KR | US
    currency       text,
    quantity       numeric(24,6),
    avg_price      numeric(20,6),               -- averagePurchasePrice
    last_price     numeric(20,6),
    -- marketValue
    purchase_amount        numeric(24,2),
    market_value           numeric(24,2),
    market_value_after_cost numeric(24,2),      -- 비용 차감 후
    -- profitLoss
    pnl                    numeric(24,2),
    pnl_after_cost         numeric(24,2),
    pnl_rate               numeric(12,6),
    pnl_rate_after_cost    numeric(12,6),
    -- dailyProfitLoss
    daily_pnl              numeric(24,2),
    daily_pnl_rate         numeric(12,6),
    -- cost  ← 종목별 실제 수수료·세금. /commissions 보다 정확하다.
    commission             numeric(20,4),
    tax                    numeric(20,4),
    PRIMARY KEY (snapshot_date, symbol)
);



-- ────────────────────────────────────────────────────────────
--  5. 커뮤니티 · 센티먼트
-- ────────────────────────────────────────────────────────────
--  ⚠️ 토스 Open API 에 커뮤니티 엔드포인트는 없다 (27개 전수 확인).
--     네이버 뉴스 API / Reddit API / 크롤링으로 각각 수집한다.
--  ⚠️ 512MB 상한 때문에 본문을 통째로 넣지 않는다.
--     body 는 분석에 필요한 앞부분만 자르고, 원문은 URL 로 참조.
CREATE TABLE IF NOT EXISTS community_post (
    id          bigserial,
    source      text        NOT NULL,           -- naver_news|reddit|x|toss|...
    external_id text        NOT NULL,           -- 소스별 고유 ID (중복 수집 방지)
    symbol      text,                           -- 매핑된 종목 (없으면 NULL)
    posted_at   timestamptz NOT NULL,
    title       text,
    body        text CHECK (length(body) <= 2000),   -- 용량 보호
    url         text,
    collected_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id, posted_at),
    UNIQUE (source, external_id, posted_at)
);
SELECT create_hypertable('community_post', 'posted_at',
                         chunk_time_interval => interval '7 days',
                         if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_post_symbol_time
    ON community_post (symbol, posted_at DESC) WHERE symbol IS NOT NULL;

--  감성 점수 — 시점 고정(point-in-time), append-only.
--  같은 글을 다른 모델로 다시 채점하면 새 행으로 쌓는다 (UPDATE 금지).
--  model 컬럼이 있으므로 나중에 로컬 모델 ↔ Gemini 성능 비교가 가능하다.
CREATE TABLE IF NOT EXISTS sentiment_score (
    post_id    bigint      NOT NULL,
    posted_at  timestamptz NOT NULL,
    scored_at  timestamptz NOT NULL DEFAULT now(),
    model      text        NOT NULL,            -- 예: gemini-flash-lite-latest
    label      text        NOT NULL CHECK (label IN ('positive','negative','neutral')),
    score      numeric(6,4) NOT NULL CHECK (score BETWEEN -1 AND 1),
    confidence numeric(6,4),
    PRIMARY KEY (post_id, posted_at, model, scored_at)
);
SELECT create_hypertable('sentiment_score', 'posted_at',
                         chunk_time_interval => interval '7 days',
                         if_not_exists => TRUE);


-- ────────────────────────────────────────────────────────────
--  6. 팩터 점수 — 4축을 '합성하지 않고' 따로 저장
-- ────────────────────────────────────────────────────────────
--  명세서 v1.1 §6.1 은 센티먼트 40 / 기술 30 / 펀더멘털 20 / 수급 10
--  이라는 고정 가중치를 썼으나, 그 숫자에 근거가 없다.
--  → Phase 1 에서는 축별 점수만 적재하고 합성하지 않는다.
--    데이터가 쌓인 뒤 각 축의 실제 예측력(IC)을 측정해서 정한다.
--    합성이 필요해지면 weight 를 설정값으로 외부화하고 이력을 남긴다.
CREATE TABLE IF NOT EXISTS factor_score (
    symbol    text        NOT NULL,
    as_of     timestamptz NOT NULL,             -- 이 시점에 알 수 있었던 정보만 반영
    factor    text        NOT NULL CHECK (factor IN
                  ('sentiment','technical','fundamental','supply_demand')),
    score     numeric(8,4) NOT NULL,            -- 정규화된 축별 점수
    inputs    jsonb,                            -- 산출 근거 (RSI/MACD 값, PER 등)
    source    text,                             -- toss | dart | naver | ...
    PRIMARY KEY (symbol, as_of, factor)
);
CREATE INDEX IF NOT EXISTS idx_factor_symbol_time ON factor_score (symbol, as_of DESC);


-- ────────────────────────────────────────────────────────────
--  7. 주문 로그
-- ────────────────────────────────────────────────────────────
--  ⚠️ clientOrderId 는 토스가 제공하는 멱등성 키(10분 유효)다.
--     미전달 시 재요청이 '별개 주문'으로 처리되어 중복 체결된다.
--     → 항상 생성해서 보내고, 여기에 먼저 기록한 뒤 호출한다.
--  is_dry_run=true 면 실제 POST 를 하지 않은 시뮬레이션 기록.
CREATE TABLE IF NOT EXISTS order_log (
    client_order_id text PRIMARY KEY,           -- 멱등성 키 (최대 36자, [a-zA-Z0-9_-])
    toss_order_id   text,                       -- 응답으로 받은 실제 주문 ID
    symbol          text        NOT NULL,
    side            text        NOT NULL CHECK (side IN ('BUY','SELL')),
    order_type      text        NOT NULL CHECK (order_type IN ('LIMIT','MARKET')),
    time_in_force   text        NOT NULL DEFAULT 'DAY' CHECK (time_in_force IN ('DAY','CLS')),
    quantity        numeric(24,6),
    order_amount    numeric(24,2),              -- US MARKET 전용 금액주문
    price           numeric(20,6),
    is_dry_run      boolean     NOT NULL DEFAULT true,
    status          text        NOT NULL DEFAULT 'PENDING',
    reason          jsonb,                      -- 어떤 시그널로 냈는지 (감사 추적)
    request_body    jsonb,
    response_body   jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CHECK ((quantity IS NULL) <> (order_amount IS NULL))   -- 정확히 하나만
);
CREATE INDEX IF NOT EXISTS idx_order_created ON order_log (created_at DESC);
--  일일 킬스위치 카운트용 (실주문만)
CREATE INDEX IF NOT EXISTS idx_order_live_daily
    ON order_log (created_at) WHERE NOT is_dry_run;


-- ────────────────────────────────────────────────────────────
--  8. 운영 관측
-- ────────────────────────────────────────────────────────────
--  rate limit 한도는 "사전 공지 없이 조정될 수 있다"고 문서에 명시돼 있다.
--  하드코딩하지 말고 응답 헤더(X-RateLimit-*)를 기록해 추세를 본다.
CREATE TABLE IF NOT EXISTS rate_limit_observation (
    observed_at  timestamptz NOT NULL DEFAULT now(),
    group_name   text        NOT NULL,          -- MARKET_DATA | ORDER | ...
    limit_value  integer,                       -- X-RateLimit-Limit
    remaining    integer,                       -- X-RateLimit-Remaining
    reset_sec    numeric(10,3),                 -- X-RateLimit-Reset
    was_429      boolean     NOT NULL DEFAULT false,
    PRIMARY KEY (observed_at, group_name)
);

--  폴링/배치 실행 결과. 실패가 조용히 누적되는 걸 막는다.
CREATE TABLE IF NOT EXISTS job_run (
    id         bigserial PRIMARY KEY,
    job_name   text        NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    ended_at   timestamptz,
    ok         boolean,
    rows       integer,
    error      text
);
CREATE INDEX IF NOT EXISTS idx_job_recent ON job_run (job_name, started_at DESC);


-- ============================================================
--  9. 분석 레이어 — "HTS 와 다른 부분"
-- ============================================================
--  차트·호가는 HTS 가 이미 잘 보여준다. 이 도구의 존재 이유는
--  '남들이 이 종목을 뭐라고 하는가'를 모아서 구조화하는 것이다.
--  ⚠️ 토스 API 에는 리포트·컨센서스가 전혀 없다. 전부 외부 소스.

--  애널리스트 의견 — 뉴스 본문에서 LLM 으로 구조화 추출한다.
--  증권사 리포트 원문은 저작권이 있어 저장하지 않고,
--  '목표주가 / 투자의견 / 근거 요약'이라는 사실 정보만 남긴다.
CREATE TABLE IF NOT EXISTS analyst_view (
    id           bigserial PRIMARY KEY,
    symbol       text        NOT NULL,
    as_of        timestamptz NOT NULL,          -- 기사 발행 시각 (시점 고정)
    broker       text,                          -- 증권사
    analyst      text,
    rating       text,                          -- BUY|HOLD|SELL|기타 원문
    rating_norm  text CHECK (rating_norm IN
                   ('STRONG_BUY','BUY','HOLD','SELL','STRONG_SELL')),
    target_price numeric(20,2),
    currency     text,
    thesis       text,                          -- 핵심 논거 1~2문장
    source_url   text,
    source_title text,
    extracted_by text,                          -- 어떤 모델이 뽑았는지
    extracted_at timestamptz NOT NULL DEFAULT now(),
    confidence   numeric(4,3),
    UNIQUE (symbol, as_of, broker, target_price)
);
CREATE INDEX IF NOT EXISTS idx_analyst_symbol ON analyst_view (symbol, as_of DESC);

--  종목 브리핑 — LLM 이 생성한 자연어 요약 (명세서 §6.2)
--  ⚠️ LLM 은 '판단'이 아니라 '설명' 역할이다. 숫자는 결정론적 코드가
--     계산하고, LLM 은 그것을 서술만 한다. inputs 에 근거를 남긴다.
CREATE TABLE IF NOT EXISTS briefing (
    symbol      text        NOT NULL,
    as_of       timestamptz NOT NULL,
    headline    text,                           -- 한 줄 요약
    bullets     jsonb,                          -- 3줄 근거
    stance      text CHECK (stance IN ('positive','negative','mixed','neutral')),
    inputs      jsonb,                          -- 무엇을 보고 썼는지 (감사 추적)
    model       text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, as_of)
);

--  DART 공시 — 금감원 공식. 크롤링과 달리 합법·안정적.
CREATE TABLE IF NOT EXISTS disclosure (
    rcept_no    text PRIMARY KEY,               -- 접수번호
    symbol      text,
    corp_name   text,
    report_name text,
    rcept_date  date,
    submitter   text,
    url         text,
    fetched_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_disclosure_symbol ON disclosure (symbol, rcept_date DESC);

--  DART 재무지표 — PER/PBR 은 DART 에 없다(시장가 필요).
--  재무비율만 받아두고, PER/PBR 은 시세와 결합해 계산한다.
CREATE TABLE IF NOT EXISTS financial_indicator (
    symbol     text NOT NULL,
    bsns_year  text NOT NULL,
    reprt_code text NOT NULL,
    idx_code   text NOT NULL,
    idx_name   text,
    idx_value  numeric(20,4),
    fetched_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, bsns_year, reprt_code, idx_code, idx_name)
);


-- ============================================================
--  10. 시장 국면 · 전략
-- ============================================================
--  공포탐욕지수. 외부(CNN/크립토)는 그대로 받고,
--  국내는 우리가 직접 계산한다 (KOSPI 공식 지수는 존재하지 않음).
--  ⚠️ 'kr_composite' 는 **자체 산출값**이지 공인 지표가 아니다.
--     구성요소를 components 에 남겨 언제든 검증·재현할 수 있게 한다.
CREATE TABLE IF NOT EXISTS market_regime (
    source      text        NOT NULL,      -- cnn | crypto | kr_composite
    as_of       date        NOT NULL,
    score       numeric(6,2) NOT NULL,     -- 0(극단 공포) ~ 100(극단 탐욕)
    rating      text,                      -- extreme fear|fear|neutral|greed|extreme greed
    components  jsonb,                     -- 자체 산출 시 근거 (재현 가능하게)
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, as_of)
);

--  포트폴리오 진단 — 전부 결정론적 계산값. LLM 이 만지지 않는다.
CREATE TABLE IF NOT EXISTS portfolio_metrics (
    as_of              date PRIMARY KEY,
    n_positions        int,
    hhi                numeric(8,4),   -- 허핀달 집중도 (1=한 종목 몰빵)
    top_weight         numeric(6,4),   -- 최대 종목 비중
    krw_weight         numeric(6,4),   -- 통화 노출
    usd_weight         numeric(6,4),
    cash_weight        numeric(6,4),
    port_vol_20d       numeric(8,5),   -- 포트 일간변동성(연율화)
    kospi_vol_20d      numeric(8,5),
    beta_kospi         numeric(8,4),
    max_drawdown_60d   numeric(8,5),
    win_rate           numeric(6,4),   -- 평가익 종목 비율
    best_symbol        text,
    worst_symbol       text,
    detail             jsonb,
    created_at         timestamptz NOT NULL DEFAULT now()
);

--  전략 제안 — LLM 이 '위 숫자들을 해석'한 결과.
--  ⚠️ 예측이 아니라 현황 진단 + 점검 항목이다.
--     inputs 에 근거 숫자를 통째로 남겨 사후 검증이 가능하게 한다.
CREATE TABLE IF NOT EXISTS strategy_note (
    as_of        timestamptz PRIMARY KEY,
    regime       text,                     -- 시장 국면 한 줄
    diagnosis    text,                     -- 내 포트폴리오 진단
    actions      jsonb,                    -- [{title, why, caution}]
    risks        jsonb,                    -- 놓치기 쉬운 위험
    inputs       jsonb,                    -- 근거 숫자 전체
    model        text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);


-- ============================================================
--  11. 기관 포지션 (SEC 13F) · 영상
-- ============================================================
--  13F: 운용자산 1억달러 이상 기관은 분기말 보유내역을 공시해야 한다.
--  ⚠️ 한계 — 분기말 기준이고 45일 뒤에 공시된다. '지금' 보유가 아니다.
--     롱 주식만 포함(공매도·채권·해외종목 제외). 후행 지표로만 쓸 것.
--  ⚠️ value 단위: 2023년 이후 '달러'다 (그 이전은 천달러). 혼동 주의.
CREATE TABLE IF NOT EXISTS institution_holding (
    cik          text NOT NULL,
    institution  text,
    period       date NOT NULL,          -- 보고 기준일 (분기말)
    filed_at     date,                   -- 실제 공시일
    cusip        text NOT NULL,
    issuer       text,
    ticker       text,                   -- CUSIP→티커 매핑 시
    value_usd    numeric(24,2),
    shares       numeric(24,0),
    weight       numeric(8,5),           -- 해당 기관 포트 내 비중
    PRIMARY KEY (cik, period, cusip)
);
CREATE INDEX IF NOT EXISTS idx_inst_cusip ON institution_holding (cusip, period DESC);
CREATE INDEX IF NOT EXISTS idx_inst_ticker ON institution_holding (ticker, period DESC);

--  분기 대비 변화 (신규진입/증가/감소/청산) — 조회용 뷰
CREATE OR REPLACE VIEW institution_change AS
SELECT cur.cik, cur.institution, cur.period, cur.cusip, cur.issuer, cur.ticker,
       cur.value_usd, cur.shares, cur.weight,
       prev.shares AS prev_shares,
       CASE WHEN prev.shares IS NULL THEN 'NEW'
            WHEN cur.shares > prev.shares * 1.05 THEN 'ADD'
            WHEN cur.shares < prev.shares * 0.95 THEN 'TRIM'
            ELSE 'HOLD' END AS action
FROM institution_holding cur
LEFT JOIN LATERAL (
    SELECT shares FROM institution_holding p
    WHERE p.cik = cur.cik AND p.cusip = cur.cusip AND p.period < cur.period
    ORDER BY p.period DESC LIMIT 1
) prev ON TRUE;
