-- Mahdi Phase 1 추가 (2026-07-20) — v6 §4.2/§7.3/§8 "글로벌 확인 신호" 나머지 항목(S&P500 선물,
-- MOVE, USDKRW) 원시 데이터 수집 추가.
--   usdkrw: 해외주식 종목_지수_환율기간별시세(환율구분 X, FX@KRW) — US10Y와 동일한 무료 엔드포인트,
--           계좌 게이트 없음. us10y_yield처럼 일봉이라 하루 중 값이 드물게만 바뀐다.
--   es_front / es_front_source: CME E-mini S&P500 선물 근월가 — ZN과 동일하게 KIS 유료 항목
--           (CME|CME 서브거래소, 월 228.8불)이라 모의투자 개발 단계에서는 yfinance 폴백 사용.
--   move_index / move_index_source: ICE BofA MOVE Index — 장외 파생 인덱스라 KIS 경로 자체가
--           없음, yfinance 폴백 전용.
ALTER TABLE macro_snapshot_5m ADD COLUMN IF NOT EXISTS usdkrw DECIMAL(10,4);
ALTER TABLE macro_snapshot_5m ADD COLUMN IF NOT EXISTS es_front DECIMAL(12,4);
ALTER TABLE macro_snapshot_5m ADD COLUMN IF NOT EXISTS es_front_source VARCHAR(20);
ALTER TABLE macro_snapshot_5m ADD COLUMN IF NOT EXISTS move_index DECIMAL(10,4);
ALTER TABLE macro_snapshot_5m ADD COLUMN IF NOT EXISTS move_index_source VARCHAR(20);
