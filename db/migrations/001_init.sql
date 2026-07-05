-- Mahdi Phase 1 — 초기 스키마 (MAHDI_ULTIMATE_SYSTEM_v6.md §18.1 기준)
-- 실시간 수집과 백테스트가 동일 스키마를 공유한다 (Single Source of Truth).

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- 1분봉 시장 원시 데이터
CREATE TABLE IF NOT EXISTS market_raw_1m (
    timestamp TIMESTAMPTZ NOT NULL, symbol VARCHAR(20),
    open DECIMAL(18,4), high DECIMAL(18,4), low DECIMAL(18,4), close DECIMAL(18,4),
    volume BIGINT, vwap DECIMAL(18,4),
    vpin DECIMAL(8,6), ofi DECIMAL(12,2), microprice DECIMAL(18,4),
    bid_ask_spread DECIMAL(8,4), buy_volume BIGINT, sell_volume BIGINT,
    usdkrw DECIMAL(10,4), quality_flag SMALLINT,
    PRIMARY KEY (timestamp, symbol));

-- 옵션 체인 1분 분석
CREATE TABLE IF NOT EXISTS option_analysis_1m (
    timestamp TIMESTAMPTZ NOT NULL, underlying VARCHAR(20),
    expiry DATE, strike DECIMAL(18,2), option_type CHAR(1),
    delta DECIMAL(8,6), gamma DECIMAL(10,8), theta DECIMAL(8,6),
    vega DECIMAL(8,6), vanna DECIMAL(10,8), charm DECIMAL(10,8),
    iv DECIMAL(8,6), rv_5d DECIMAL(8,6), vrp DECIMAL(8,6),
    skew_25d DECIMAL(8,6), gex DECIMAL(18,4), oi INTEGER, oi_change INTEGER,
    volume INTEGER, spread_state SMALLINT,
    PRIMARY KEY (timestamp, underlying, expiry, strike, option_type));

-- 레짐 상태
CREATE TABLE IF NOT EXISTS regime_state (
    timestamp TIMESTAMPTZ NOT NULL, regime SMALLINT,
    prob_vector DECIMAL(6,4)[], higher_tf_regime SMALLINT,
    stability_flag BOOLEAN, PRIMARY KEY (timestamp));

-- ML 피처 스토어 (실시간·백테스트 공용 피처 사전 기반)
CREATE TABLE IF NOT EXISTS feature_store (
    timestamp TIMESTAMPTZ NOT NULL, symbol VARCHAR(20),
    features JSONB,           -- 피처 사전 버전 태그 포함
    feature_version VARCHAR(20),
    PRIMARY KEY (timestamp, symbol));

-- 예측 로그 (자기강화 루프의 원료)
CREATE TABLE IF NOT EXISTS prediction_logs (
    pred_id UUID DEFAULT gen_random_uuid(), timestamp TIMESTAMPTZ NOT NULL,
    model_id VARCHAR(50), is_champion BOOLEAN,
    prediction SMALLINT, confidence DECIMAL(6,4),
    regime_at_pred SMALLINT, signal_features JSONB,
    actual_return_1m DECIMAL(8,6), actual_return_5m DECIMAL(8,6),
    was_correct BOOLEAN, PRIMARY KEY (pred_id));

-- 신호 결정 로그 (진입/보류/거절 — 거절 사유 필수)
CREATE TABLE IF NOT EXISTS signal_decisions (
    decision_id UUID DEFAULT gen_random_uuid(), timestamp TIMESTAMPTZ NOT NULL,
    conviction VARCHAR(20), decision VARCHAR(20),   -- ENTER/HOLD/REJECT
    reject_reason VARCHAR(50), risk_gate_state JSONB,
    exec_mode VARCHAR(10),                          -- AUTO/CONFIRM/ADVISORY
    PRIMARY KEY (decision_id));

-- 주문·체결 로그
CREATE TABLE IF NOT EXISTS execution_logs (
    order_id VARCHAR(40) PRIMARY KEY, timestamp TIMESTAMPTZ,
    symbol VARCHAR(30), side VARCHAR(6), order_type VARCHAR(10),
    intended_px DECIMAL(18,4), filled_px DECIMAL(18,4), qty INTEGER,
    state VARCHAR(15),          -- PENDING/PARTIAL/FILLED/CANCELLED/REJECTED
    slippage_ticks DECIMAL(8,2), latency_ms INTEGER);

-- 거래 기록 (이유코드 필수)
CREATE TABLE IF NOT EXISTS trade_history (
    trade_id UUID DEFAULT gen_random_uuid(), strategy_id VARCHAR(50),
    symbol VARCHAR(30), entry_time TIMESTAMPTZ, exit_time TIMESTAMPTZ,
    entry_price DECIMAL(18,4), exit_price DECIMAL(18,4), qty INTEGER,
    gross_pnl DECIMAL(18,4), commission DECIMAL(18,4),
    slippage DECIMAL(18,4), net_pnl DECIMAL(18,4),
    regime_entry SMALLINT, confidence_entry DECIMAL(6,4),
    exit_reason VARCHAR(50),    -- HARD_STOP/STRUCT/FLOW/BELIEF/TIME/FORCED_FLAT/MANUAL
    setup_fingerprint VARCHAR(64),
    PRIMARY KEY (trade_id));

-- 리스크 스냅샷 · C/C 스코어카드 · 연구 태그
CREATE TABLE IF NOT EXISTS risk_snapshots  (timestamp TIMESTAMPTZ PRIMARY KEY, greeks JSONB,
    loss_buffer DECIMAL(8,4), cb_state JSONB);
CREATE TABLE IF NOT EXISTS cc_scorecard    (date DATE, model_id VARCHAR(50), is_champion BOOLEAN,
    n_signals INTEGER, ev_after_cost DECIMAL(10,6), max_dd DECIMAL(8,4),
    cvar95 DECIMAL(8,4), regimes_positive SMALLINT, PRIMARY KEY (date, model_id));
CREATE TABLE IF NOT EXISTS research_tags   (tag_id UUID DEFAULT gen_random_uuid(),
    date DATE, category VARCHAR(30), note TEXT, status VARCHAR(20),
    PRIMARY KEY (tag_id));

-- 하이퍼테이블 전환: PK에 timestamp가 포함된 순수 시계열 테이블만 대상.
-- (prediction_logs/signal_decisions/execution_logs/trade_history/cc_scorecard/research_tags는
--  UUID·복합 자연키 PK라 그대로 두고, 필요 시 Phase2에서 별도 시간축 인덱스로 대응한다.)
SELECT create_hypertable('market_raw_1m',     'timestamp', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
SELECT create_hypertable('option_analysis_1m', 'timestamp', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
SELECT create_hypertable('feature_store',      'timestamp', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
SELECT create_hypertable('regime_state',       'timestamp', if_not_exists => TRUE);
SELECT create_hypertable('risk_snapshots',     'timestamp', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_execution_logs_timestamp ON execution_logs (timestamp);
CREATE INDEX IF NOT EXISTS idx_prediction_logs_timestamp ON prediction_logs (timestamp);
CREATE INDEX IF NOT EXISTS idx_signal_decisions_timestamp ON signal_decisions (timestamp);
CREATE INDEX IF NOT EXISTS idx_trade_history_entry_time ON trade_history (entry_time);
