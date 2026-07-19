-- ============================================================
--  멀티유저 전환 — 온보딩에서 토스 키를 받아 바로 동작
-- ============================================================
--  인증 설계
--    별도 로그인 없음. **자격증명이 곧 신원**이다.
--    키 검증 성공 → 세션 발급 → 쿠키로 식별.
--
--  ⚠️ 토스 API 에는 읽기 전용 scope 가 없다 (/oauth2/token 은
--     grant_type/client_id/client_secret 3개만 받는다).
--     즉 조회용으로 받은 자격증명에도 **주문 권한이 포함**된다.
--     → 배포 빌드에서는 주문 코드 경로를 제거한다.
--
--  ⚠️ 토큰은 client_id 당 1개다. 같은 client_id 가 두 행이면
--     워커가 자기 토큰을 자기가 무효화한다 → client_id UNIQUE 필수.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- 사용자 — 비밀번호 없음. 자격증명 등록으로 생성된다.
CREATE TABLE IF NOT EXISTS app_user (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    nickname    text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz
);

-- 자격증명 — 사용자당 1개, client_id 는 전역 유일
CREATE TABLE IF NOT EXISTS user_credential (
    user_id           uuid PRIMARY KEY REFERENCES app_user(id) ON DELETE CASCADE,
    client_id         text UNIQUE NOT NULL,     -- ★ 한 토스 계정 = 한 사용자
    client_secret_enc bytea NOT NULL,           -- Fernet 봉인 (마스터키는 DB 밖)
    account_seq       text,
    verified_at       timestamptz,              -- 최초 검증 성공 시각
    last_ok_at        timestamptz,              -- 마지막 정상 호출
    status            text NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active','locked','revoked')),
    -- 탈취 탐지: 만료 전인데 401 이 나면 제3자가 같은 client_id 로
    -- 토큰을 발급했다는 뜻이다 (토큰 1개 제약의 부작용을 역이용)
    premature_401_count int NOT NULL DEFAULT 0,
    locked_reason     text,
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- 세션 — 쿠키에 담을 것은 이 토큰의 해시뿐
CREATE TABLE IF NOT EXISTS user_session (
    token_hash  bytea PRIMARY KEY,
    user_id     uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    user_agent  text
);
CREATE INDEX IF NOT EXISTS idx_session_user ON user_session (user_id);

-- ── 토스 토큰: 단일행(id=1) → 사용자별 ──────────────────────
DROP TABLE IF EXISTS toss_token;
CREATE TABLE toss_token (
    user_id      uuid PRIMARY KEY REFERENCES app_user(id) ON DELETE CASCADE,
    access_token text        NOT NULL,
    token_type   text        NOT NULL DEFAULT 'Bearer',
    issued_at    timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- ── 계좌 스코프 테이블에 user_id 부여 ───────────────────────
--  시세·뉴스·감성·13F·공포탐욕은 **공용**이다.
--  사용자마다 중복 수집하면 rate limit 만 낭비한다.
ALTER TABLE holding_snapshot   ADD COLUMN IF NOT EXISTS user_id uuid REFERENCES app_user(id) ON DELETE CASCADE;
ALTER TABLE account_snapshot   ADD COLUMN IF NOT EXISTS user_id uuid REFERENCES app_user(id) ON DELETE CASCADE;
ALTER TABLE portfolio_metrics  ADD COLUMN IF NOT EXISTS user_id uuid REFERENCES app_user(id) ON DELETE CASCADE;
ALTER TABLE strategy_note      ADD COLUMN IF NOT EXISTS user_id uuid REFERENCES app_user(id) ON DELETE CASCADE;
ALTER TABLE order_log          ADD COLUMN IF NOT EXISTS user_id uuid REFERENCES app_user(id) ON DELETE CASCADE;

-- 기존 PK 를 (user_id, ...) 복합키로 교체
ALTER TABLE holding_snapshot  DROP CONSTRAINT IF EXISTS holding_snapshot_pkey;
ALTER TABLE account_snapshot  DROP CONSTRAINT IF EXISTS account_snapshot_pkey;
ALTER TABLE portfolio_metrics DROP CONSTRAINT IF EXISTS portfolio_metrics_pkey;
ALTER TABLE strategy_note     DROP CONSTRAINT IF EXISTS strategy_note_pkey;

-- 기존 단일 사용자 데이터는 마이그레이션 스크립트가 user_id 를 채운 뒤
-- 아래 제약을 건다 (worker/db/migrate_multiuser.py 참조)
SQL_MARKER_SPLIT

-- ── 2단계: user_id 채운 뒤 실행 ─────────────────────────────
ALTER TABLE holding_snapshot  ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE account_snapshot  ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE portfolio_metrics ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE strategy_note     ALTER COLUMN user_id SET NOT NULL;

ALTER TABLE holding_snapshot  ADD PRIMARY KEY (user_id, snapshot_date, symbol);
ALTER TABLE account_snapshot  ADD PRIMARY KEY (user_id, snapshot_date);
ALTER TABLE portfolio_metrics ADD PRIMARY KEY (user_id, as_of);
ALTER TABLE strategy_note     ADD PRIMARY KEY (user_id, as_of);
CREATE INDEX IF NOT EXISTS idx_order_user ON order_log (user_id, created_at DESC);
