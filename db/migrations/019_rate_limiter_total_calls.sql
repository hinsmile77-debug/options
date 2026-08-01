-- Mahdi 추가 (2026-08-01, 운영점검보고서 2026-07-31 §5-2/§5-5) — 총 REST 수요를 DB에서
-- 계산할 수 있게 한다.
--
-- 07-31에 처음 계량된 "총 REST 수요 43.6%"는 **로그(httpx 줄)를 세야만** 알 수 있는 값이었다.
-- 그래서 COCKPIT은 배율(backoff_multiplier)만 볼 수 있었고, 정작 더 중요한 "용량 대비 수요"와
-- "적자 시작 배율"은 다음날 사람이 로그를 집계해야 나왔다.
--
-- `KISRestClient.rate_limit_total_calls`(2026-07-28 추가, 프로세스 기동 이래 누적)를 매 사이클
-- 함께 기록하면 두 행의 차이로 그 구간 호출 수가 나온다:
--     수요(건/초) = Δtotal_calls / Δrecorded_at
--     적자 시작 배율 = 페이서 용량(1.0건/초) / 수요
--
-- **누적 카운터라 프로세스 재시작 시 되감긴다** — 읽는 쪽(mahdi/ops/db_metrics.rest_demand)이
-- 감소 구간을 건너뛰어야 한다. NULL 허용인 이유는 이 마이그레이션 적용 전 행들 때문이다.

ALTER TABLE rate_limiter_status_log ADD COLUMN IF NOT EXISTS total_calls BIGINT;
ALTER TABLE rate_limiter_status_history ADD COLUMN IF NOT EXISTS total_calls BIGINT;

COMMENT ON COLUMN rate_limiter_status_log.total_calls IS
    '공유 _RateLimiter를 통과한 누적 호출 수(프로세스 기동 이래). 두 시점의 차이로 그 구간의 '
    'REST 수요(건/초)를 구한다 — 재시작하면 되감기므로 읽는 쪽이 감소 구간을 건너뛰어야 한다.';
COMMENT ON COLUMN rate_limiter_status_history.total_calls IS
    'rate_limiter_status_log.total_calls와 동일 의미. append-only라 하루치 시계열로 수요 추이를 '
    '볼 수 있다(2026-08-01 신규, 운영점검보고서 2026-07-31 §5-5 "REST 수요 배지").';
