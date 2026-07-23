-- Mahdi 추가 (2026-07-23, 운영점검보고서 §2-1/§4 Fix#4 "레이트리밋 근접도 배지") — 공유
-- _RateLimiter(mahdi/broker/rest_client.py)의 현재 백오프 배율과 최근 폴링 사이클 스케줄 밀림을
-- 기록하는 싱글턴 테이블.
--
-- 배경: 07-22 저녁에 회복 임계값을 20->8로 낮췄는데, 07-23 하루치 실측 결과 EGW00201 비율·
-- 스케줄 밀림·평균/최대 지연이 전부 악화됐다(§2-1). 그런데도 그 원인(배율이 실제로 언제 얼마나
-- 늘고 줄었는지)은 다음날 로그를 정밀분석해야만 추정할 수 있었다 — 관측 루프(mahdi.main)와
-- COCKPIT(Streamlit)은 별도 프로세스라 레이트리미터의 실시간 상태를 COCKPIT이 직접 읽을 방법이
-- 없다(shutdown_check_log/slack_alert_settings와 동일한 프로세스 분리 문제).
--
-- 관측 루프가 옵션체인 폴링 사이클(60초)마다 이 값을 갱신하면, COCKPIT이 "오늘의 점검 요약"에서
-- 사후 로그 분석 없이도 그날 바로 레이트리밋 근접도를 볼 수 있다.

CREATE TABLE IF NOT EXISTS rate_limiter_status_log (
    id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),  -- 단일 행만 허용하는 싱글턴 트릭
    checked_at TIMESTAMPTZ NOT NULL,
    backoff_multiplier DOUBLE PRECISION NOT NULL,
    last_cycle_overrun_seconds DOUBLE PRECISION NOT NULL);

COMMENT ON COLUMN rate_limiter_status_log.checked_at IS
    '실제로는 naive KST 벽시계 시각이 "+00"으로 잘못 라벨링된 값 — TIMESTAMPTZ지만 진짜 UTC 아님. '
    '정책 설명: mahdi/data/db.py local_now(). 2026-07-19 명문화(운영점검보고서 §3-4/§5-3).';
COMMENT ON COLUMN rate_limiter_status_log.backoff_multiplier IS
    '_RateLimiter.current_multiplier — 1.0=백오프 없음, 4.0(_MAX_INTERVAL_MULTIPLIER)에 가까울수록 '
    '레이트리밋에 강하게 걸려 있는 상태.';
COMMENT ON COLUMN rate_limiter_status_log.last_cycle_overrun_seconds IS
    '직전 옵션체인 폴링 사이클이 주기(60초)를 초과해 밀린 초 — 0이면 정상 주기 내 완료.';
