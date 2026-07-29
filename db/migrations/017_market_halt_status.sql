-- Mahdi 추가 (2026-07-29) — 서킷브레이커/거래정지 실시간 감지(mahdi/risk/market_halt.py,
-- KIS WS H0UNMKO0 "국내주식 장운영정보")의 상태를 COCKPIT(별도 프로세스)이 재시작 없이 바로
-- 볼 수 있게 한다. rate_limiter_status_log(014)/rate_limiter_status_history(016)와 동일한
-- 싱글턴+append-only 병행 패턴.

CREATE TABLE IF NOT EXISTS market_halt_status (
    id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),  -- 단일 행만 허용하는 싱글턴 트릭
    updated_at TIMESTAMPTZ NOT NULL,
    is_halted BOOLEAN NOT NULL,
    mkop_cls_code VARCHAR(3),
    label VARCHAR(50),
    halted_since TIMESTAMPTZ);

CREATE TABLE IF NOT EXISTS market_halt_event_history (
    recorded_at TIMESTAMPTZ NOT NULL,
    mkop_cls_code VARCHAR(3),
    label VARCHAR(50),
    is_halted BOOLEAN NOT NULL);

SELECT create_hypertable('market_halt_event_history', 'recorded_at', if_not_exists => TRUE);

COMMENT ON COLUMN market_halt_status.updated_at IS
    '실제로는 naive KST 벽시계 시각이 "+00"으로 잘못 라벨링된 값 — TIMESTAMPTZ지만 진짜 UTC 아님. '
    '정책 설명: mahdi/data/db.py local_now(). 2026-07-19 명문화(운영점검보고서 §3-4/§5-3).';
COMMENT ON COLUMN market_halt_status.mkop_cls_code IS
    'KIS H0UNMKO0 MKOP_CLS_CODE — 174/175/182/184/185(서킷브레이커)·164(시장임시정지). '
    '사이드카(387/388/397/398)는 신규진입 차단 대상이 아니라 이 테이블에 기록하지 않는다.';
COMMENT ON COLUMN market_halt_status.halted_since IS
    'is_halted=True로 최초 진입한 시각 — 174→182처럼 halted 상태에서 다른 halt 코드로 바뀌어도 '
    '유지된다(mahdi.risk.market_halt.MarketHaltMonitor.update() 참고). 정상 상태면 NULL.';
COMMENT ON COLUMN market_halt_event_history.recorded_at IS
    'market_halt_status.updated_at과 동일한 시각 정책 — 상태가 실제로 전이된 시점만 append된다 '
    '(매 WS 메시지가 아니라 MarketHaltMonitor.update()의 changed=True인 경우만).';
