-- Mahdi Phase 1 추가 — 기초자산(지수/선물) 스팟 1분 스냅샷.
-- option_analysis_1m 폴링과 같은 주기로 REST(선물옵션 시세) 응답의 output3(KOSPI200 지수)을 저장한다.
-- market_raw_1m은 종목(주로 옵션)별 틱 집계용이라 지수 자체를 담기에 부적절해 별도 테이블로 분리.

CREATE TABLE IF NOT EXISTS underlying_spot_1m (
    timestamp TIMESTAMPTZ NOT NULL, underlying VARCHAR(20),
    spot DECIMAL(18,4),
    PRIMARY KEY (timestamp, underlying));

SELECT create_hypertable('underlying_spot_1m', 'timestamp', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
