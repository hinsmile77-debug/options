-- Mahdi 추가 (2026-07-19, 운영점검보고서 §5-4) — 슬랙 알림 On/Off 토글.
-- COCKPIT(Streamlit, 별도 프로세스)과 관측 루프(mahdi.main, 별도 프로세스)가 메모리를 공유하지
-- 않으므로, 전역 변수가 아니라 이 싱글턴 테이블로 값을 주고받는다(COCKPIT 체크박스가 토글하면
-- mahdi.main의 notify()가 다음 알림 시도부터 바로 반영해서 본다 — 재시작 불필요).
--
-- 행이 아직 없으면(최초 기동, 아무도 토글한 적 없음) mahdi/config/settings.py의
-- SlackSettings.slack_alerts_enabled_default로 폴백한다 — mahdi/data/db.py의
-- is_slack_alerts_enabled() 참고. 그래서 이 마이그레이션은 시드 데이터를 넣지 않는다.

CREATE TABLE IF NOT EXISTS slack_alert_settings (
    id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),  -- 단일 행만 허용하는 싱글턴 트릭
    enabled BOOLEAN NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL);
