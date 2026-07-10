-- Mahdi Phase 1.5-③ 추가 — 만기북(series="regular"|"weekly")별 ATM±2 구간 유동성 스냅샷.
-- 2026-07-10: 위클리가 "weekly_mon"/"weekly_thu" 두 값으로 분리됨(둘 다 10자라 VARCHAR(10)
-- 그대로 수용 — 스키마 변경 없음). 컬럼 정의는 최초 적용 당시 그대로 유지.
-- 장전 선발 점수(docs/Dev_md/RESEARCH_EXPIRY_SELECTION_v1.md)의 20거래일 % 스프레드 기준선을
-- 쌓기 위함. % 스프레드는 Cao & Wei(2010) 권고에 따라 달러 스프레드가 아닌 상대(%) 스프레드를 쓴다.

CREATE TABLE IF NOT EXISTS expiry_liquidity_1m (
    timestamp TIMESTAMPTZ NOT NULL, underlying VARCHAR(20), series VARCHAR(10),
    expiry DATE, atm_spread_pct DECIMAL(10,6), depth DECIMAL(20,2), volume DECIMAL(20,2),
    days_to_expiry INT,
    PRIMARY KEY (timestamp, underlying, series, expiry));

SELECT create_hypertable('expiry_liquidity_1m', 'timestamp', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
