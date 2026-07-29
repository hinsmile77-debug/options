-- Mahdi 추가 (2026-07-29, 운영점검보고서 §2-5/Fix#3) — rate_limiter_status_log(014)는 단일행
-- UPSERT 싱글턴이라 "지금 배율이 얼마인지"만 알 수 있고 시계열(언제 얼마였는지) 조회가 불가능하다.
-- 07-29 조사에서 계좌 잔고 폴러 추가로 인한 스케줄 밀림 재발을 로그 줄 위치로만 추정해야 했던 것도
-- 이 부재 때문 — 같은 값을 append-only로도 함께 남겨, COCKPIT 추세 차트나 운영점검 시 시각 기반
-- 분석(예: 백오프 배율이 언제부터 상승하기 시작했는지)이 가능하게 한다. rate_limiter_status_log
-- 자체(COCKPIT "현재 상태" 배지 용도)는 그대로 두고 별도 테이블로 병행한다.

CREATE TABLE IF NOT EXISTS rate_limiter_status_history (
    recorded_at TIMESTAMPTZ NOT NULL,
    backoff_multiplier DOUBLE PRECISION NOT NULL,
    last_cycle_overrun_seconds DOUBLE PRECISION NOT NULL);

SELECT create_hypertable('rate_limiter_status_history', 'recorded_at', if_not_exists => TRUE);

COMMENT ON COLUMN rate_limiter_status_history.recorded_at IS
    '실제로는 naive KST 벽시계 시각이 "+00"으로 잘못 라벨링된 값 — TIMESTAMPTZ지만 진짜 UTC 아님. '
    '정책 설명: mahdi/data/db.py local_now(). 2026-07-19 명문화(운영점검보고서 §3-4/§5-3).';
COMMENT ON COLUMN rate_limiter_status_history.backoff_multiplier IS
    '_RateLimiter.current_multiplier — rate_limiter_status_log.backoff_multiplier와 동일한 값을 '
    '같은 사이클에 append-only로도 남긴 것(시계열 분석용).';
COMMENT ON COLUMN rate_limiter_status_history.last_cycle_overrun_seconds IS
    'rate_limiter_status_log.last_cycle_overrun_seconds와 동일한 값의 시계열 기록.';
