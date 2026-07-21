-- Mahdi 추가 (2026-07-21, 운영점검보고서 §5-3 "종료 신뢰성 배지") — 장마감 자동 종료
-- (scripts/log_marketclose_stop.py)가 taskkill/PowerShell 커맨드라인 fallback kill 이후에도
-- COCKPIT/관측 루프 프로세스가 남아있는지 확인한 결과를 기록하는 싱글턴 테이블.
--
-- 배경: 2026-07-21 15:45 자동 종료가 창 제목(WINDOWTITLE) 매칭 실패로 "No tasks running"만
-- 남기고 실제로는 두 프로세스가 계속 살아있었는데 아무도 알아채지 못한 채 넘어갔다(§3-1).
-- COCKPIT(Streamlit)과 log_marketclose_stop.py는 별도 프로세스라 이 값을 DB로 주고받는다
-- (slack_alert_settings, 009_slack_alert_settings.sql과 동일한 싱글턴 패턴).

CREATE TABLE IF NOT EXISTS shutdown_check_log (
    id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),  -- 단일 행만 허용하는 싱글턴 트릭
    checked_at TIMESTAMPTZ NOT NULL,
    remaining_process_count INTEGER NOT NULL);

COMMENT ON COLUMN shutdown_check_log.checked_at IS
    '실제로는 naive KST 벽시계 시각이 "+00"으로 잘못 라벨링된 값 — TIMESTAMPTZ지만 진짜 UTC 아님. '
    '정책 설명: mahdi/data/db.py local_now(). 2026-07-19 명문화(운영점검보고서 §3-4/§5-3).';
