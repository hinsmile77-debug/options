-- 2026-08-06 고도화#5 — 진입 판단의 **사후 평가** 축.
--
-- ## 왜 필요한가
--
-- 08-05 `p1`(VRP 배선)이 팔레트를 연 뒤 ENTER 판단이 0 → 62건이 됐다. 그런데 **그 62건이
-- 옳았는지 재는 지표가 하나도 없다.** ADVISORY 전용이라 손익은 없지만, 진입 시점의 기초자산과
-- 이후 N분 궤적은 이미 DB에 있다(`underlying_spot_1m`).
--
-- 실거래로 전환하는 날 "이전보다 나아졌는가"를 물으려면 **그 전의 기준선**이 있어야 한다.
-- ADVISORY라는 이유로 미루면 전환 시점에 비교할 것이 없다(§14-3 주석이 적은 원칙 그대로:
-- "진입이 없어도 잴 수 있는 지표다").
--
-- ## 이 테이블이 하지 않는 것
--
-- **가중치를 바꾸지 않는다.** 이것은 평가이지 되먹임이 아니다. 성과로 배분을 조정하는 것은
-- Thompson Sampling(v6 §11.3, Phase 3)의 몫이고, 그 전에 "무엇을 성과로 볼 것인가"부터
-- 며칠 쌓아 사람이 정해야 한다 — 정상 범위를 모르는 채 임계를 먼저 정하면 그 임계가 곧
-- 결론이 된다(§16 괴리율에서 배운 것).
--
-- ## 왜 판단당 한 행인가
--
-- `signal_decisions`에 컬럼을 붙이지 않는 이유: 평가는 **판단보다 나중에**(장마감 배치) 계산되고,
-- 재계산될 수 있다(지평 추가 등). 판단 행을 사후에 UPDATE하면 "그 시점에 무엇을 알았는가"라는
-- signal_decisions의 성격이 흐려진다.

CREATE TABLE IF NOT EXISTS decision_outcomes (
    decision_id       UUID PRIMARY KEY REFERENCES signal_decisions(decision_id) ON DELETE CASCADE,
    timestamp         TIMESTAMPTZ NOT NULL,
    underlying        VARCHAR(20) NOT NULL,
    -- 판단 시점 방향(-1~+1). signal_decisions.risk_gate_state에서 뽑아 여기 복사해 둔다 —
    -- 적중 판정을 이 테이블만으로 재현할 수 있어야 한다(조인 없이 감사 가능).
    direction         DOUBLE PRECISION,
    entry_spot        NUMERIC(18,4),
    spot_after_5m     NUMERIC(18,4),
    spot_after_15m    NUMERIC(18,4),
    spot_after_30m    NUMERIC(18,4),
    -- 방향 x 이동의 부호. NULL은 "아직/영영 모른다"(장 마감으로 지평이 안 찼거나 스팟 결손).
    -- **0(무변동)은 적중도 실패도 아니다** — 세 값 모두 NULL 허용이고 집계에서 분모에 안 들어간다.
    hit_5m            BOOLEAN,
    hit_15m           BOOLEAN,
    hit_30m           BOOLEAN,
    computed_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_decision_outcomes_timestamp ON decision_outcomes ("timestamp");

COMMENT ON TABLE decision_outcomes IS
    '2026-08-06 고도화#5 — 진입 판단의 사후 평가(ADVISORY 기준선). 평가이지 되먹임이 아니다.';
COMMENT ON COLUMN decision_outcomes.hit_5m IS
    '방향 x (5분 뒤 스팟 - 진입 스팟) > 0. NULL = 지평 미충족 또는 스팟 결손, 무변동도 NULL.';
