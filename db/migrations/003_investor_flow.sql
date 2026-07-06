-- Mahdi Phase 1 추가 — KOSPI200 파생상품시장 투자자별(외국인/기관계/개인) 순매수 거래대금 스냅샷.
-- "시장별 투자자매매동향(시세)"는 세션 누적치이므로, 이 테이블의 각 행은 그 폴링 시점까지의
-- 누적 수급 우위를 나타낸다(1분간의 델타가 아님).

CREATE TABLE IF NOT EXISTS investor_flow_1m (
    timestamp TIMESTAMPTZ NOT NULL, underlying VARCHAR(20),
    foreign_net DECIMAL(20,2), institution_net DECIMAL(20,2), individual_net DECIMAL(20,2),
    PRIMARY KEY (timestamp, underlying));

SELECT create_hypertable('investor_flow_1m', 'timestamp', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
