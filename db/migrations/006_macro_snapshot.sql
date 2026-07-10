-- Mahdi Phase 1 추가 (2026-07-10) — Cross-asset stress 피처(v6 §7.3: VIX 기간구조·USDCNH·US10Y
-- 급변)의 5분 주기 매크로 스냅샷. VIX(CBOE VX 선물)·USDCNH(HKEx CNH 선물)는 해외선물옵션
-- 도메인에서 계좌 무관으로 얻지만, US10Y는 계좌에 CBOT 거래소 신청이 안 되어 있는 동안은 일봉
-- (해외주식 국채구분 API)만 얻을 수 있어 us10y_yield는 하루 중 대부분의 행에서 NULL이고
-- 장중 첫 갱신 시점에만 값이 채워진다(마지막 값을 그대로 들고 있고 싶으면 조회 시
-- LOCF(forward-fill)로 처리할 것 — 테이블 자체는 원본값만 적재).

CREATE TABLE IF NOT EXISTS macro_snapshot_5m (
    timestamp TIMESTAMPTZ NOT NULL,
    vix_front DECIMAL(10,4), vix_next DECIMAL(10,4), vix_term_structure DECIMAL(10,6),
    usdcnh DECIMAL(10,4), us10y_yield DECIMAL(8,4),
    quality_flag SMALLINT,
    PRIMARY KEY (timestamp));

SELECT create_hypertable('macro_snapshot_5m', 'timestamp', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
