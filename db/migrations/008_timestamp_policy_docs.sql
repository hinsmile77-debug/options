-- Mahdi 타임스탬프 정책 명문화 (2026-07-19, 운영점검보고서 §3-4/§5-3)
--
-- 스키마/데이터는 그대로 두고 문서화만 한다 — 사용자 확인 후 결정(2026-07-19). 이 마이그레이션은
-- COMMENT ON COLUMN만 추가하며, 컬럼 타입/데이터를 전혀 바꾸지 않는다(락 걸리지 않는 메타데이터
-- 변경). 실제 정책/전체 배경 설명은 mahdi/data/db.py의 local_now() 함수 docstring 참고.
--
-- 요약: 이 프로젝트가 DB에 쓰는 모든 시각(mahdi/data/db.py의 local_now()를 거쳐 생성됨)은
-- naive(타임존 정보 없는) 서버 로컬 벽시계 시각(KST)이다. 아래 컬럼들은 전부 TIMESTAMPTZ로
-- 선언돼 있어 "UTC"처럼 보이지만, naive datetime이 Postgres 세션 타임존(이 프로젝트는 기본값
-- UTC) 기준으로 해석되어 저장되므로 실제로는 KST 벽시계 시각에 "+00"이 잘못 붙은 값이다
-- (2026-07-16 점검에서 14:20 KST 조회 결과가 "...14:20:00+00"으로 나와 확인 — 진짜 UTC라면
-- 05:20이어야 함). 애플리케이션 코드가 이 규약을 항상 일관되게 쓰는 한 self-consistent하며
-- 지금 당장 고장난 동작은 없다. 해외선물(VIX/CNH/ZN) 등 다른 시간대 데이터와 교차분석하거나
-- Postgres 서버 함수(NOW()/CURRENT_DATE, 진짜 UTC 기준)와 섞어 쓸 때는 9시간 오차에 주의할 것.

COMMENT ON COLUMN market_raw_1m.timestamp IS
    '실제로는 naive KST 벽시계 시각이 "+00"으로 잘못 라벨링된 값 — TIMESTAMPTZ지만 진짜 UTC 아님. '
    '정책 설명: mahdi/data/db.py local_now(). 2026-07-19 명문화(운영점검보고서 §3-4/§5-3).';

COMMENT ON COLUMN option_analysis_1m.timestamp IS
    '실제로는 naive KST 벽시계 시각이 "+00"으로 잘못 라벨링된 값 — TIMESTAMPTZ지만 진짜 UTC 아님. '
    '정책 설명: mahdi/data/db.py local_now(). 2026-07-19 명문화(운영점검보고서 §3-4/§5-3).';

COMMENT ON COLUMN regime_state.timestamp IS
    '실제로는 naive KST 벽시계 시각이 "+00"으로 잘못 라벨링된 값 — TIMESTAMPTZ지만 진짜 UTC 아님. '
    '정책 설명: mahdi/data/db.py local_now(). 2026-07-19 명문화(운영점검보고서 §3-4/§5-3).';

COMMENT ON COLUMN feature_store.timestamp IS
    '실제로는 naive KST 벽시계 시각이 "+00"으로 잘못 라벨링된 값 — TIMESTAMPTZ지만 진짜 UTC 아님. '
    '정책 설명: mahdi/data/db.py local_now(). 2026-07-19 명문화(운영점검보고서 §3-4/§5-3).';

COMMENT ON COLUMN prediction_logs.timestamp IS
    '실제로는 naive KST 벽시계 시각이 "+00"으로 잘못 라벨링된 값 — TIMESTAMPTZ지만 진짜 UTC 아님. '
    '정책 설명: mahdi/data/db.py local_now(). 2026-07-19 명문화(운영점검보고서 §3-4/§5-3).';

COMMENT ON COLUMN signal_decisions.timestamp IS
    '실제로는 naive KST 벽시계 시각이 "+00"으로 잘못 라벨링된 값 — TIMESTAMPTZ지만 진짜 UTC 아님. '
    '정책 설명: mahdi/data/db.py local_now(). 2026-07-19 명문화(운영점검보고서 §3-4/§5-3).';

COMMENT ON COLUMN execution_logs.timestamp IS
    '실제로는 naive KST 벽시계 시각이 "+00"으로 잘못 라벨링된 값 — TIMESTAMPTZ지만 진짜 UTC 아님. '
    '정책 설명: mahdi/data/db.py local_now(). 2026-07-19 명문화(운영점검보고서 §3-4/§5-3).';

COMMENT ON COLUMN trade_history.entry_time IS
    '실제로는 naive KST 벽시계 시각이 "+00"으로 잘못 라벨링된 값 — TIMESTAMPTZ지만 진짜 UTC 아님. '
    '정책 설명: mahdi/data/db.py local_now(). 2026-07-19 명문화(운영점검보고서 §3-4/§5-3).';

COMMENT ON COLUMN trade_history.exit_time IS
    '실제로는 naive KST 벽시계 시각이 "+00"으로 잘못 라벨링된 값 — TIMESTAMPTZ지만 진짜 UTC 아님. '
    '정책 설명: mahdi/data/db.py local_now(). 2026-07-19 명문화(운영점검보고서 §3-4/§5-3).';

COMMENT ON COLUMN risk_snapshots.timestamp IS
    '실제로는 naive KST 벽시계 시각이 "+00"으로 잘못 라벨링된 값 — TIMESTAMPTZ지만 진짜 UTC 아님. '
    '정책 설명: mahdi/data/db.py local_now(). 2026-07-19 명문화(운영점검보고서 §3-4/§5-3).';

COMMENT ON COLUMN underlying_spot_1m.timestamp IS
    '실제로는 naive KST 벽시계 시각이 "+00"으로 잘못 라벨링된 값 — TIMESTAMPTZ지만 진짜 UTC 아님. '
    '정책 설명: mahdi/data/db.py local_now(). 2026-07-19 명문화(운영점검보고서 §3-4/§5-3).';

COMMENT ON COLUMN investor_flow_1m.timestamp IS
    '실제로는 naive KST 벽시계 시각이 "+00"으로 잘못 라벨링된 값 — TIMESTAMPTZ지만 진짜 UTC 아님. '
    '정책 설명: mahdi/data/db.py local_now(). 2026-07-19 명문화(운영점검보고서 §3-4/§5-3).';

COMMENT ON COLUMN active_futures_symbol.updated_at IS
    '실제로는 naive KST 벽시계 시각이 "+00"으로 잘못 라벨링된 값 — TIMESTAMPTZ지만 진짜 UTC 아님. '
    '정책 설명: mahdi/data/db.py local_now(). 2026-07-19 명문화(운영점검보고서 §3-4/§5-3).';

COMMENT ON COLUMN expiry_liquidity_1m.timestamp IS
    '실제로는 naive KST 벽시계 시각이 "+00"으로 잘못 라벨링된 값 — TIMESTAMPTZ지만 진짜 UTC 아님. '
    '정책 설명: mahdi/data/db.py local_now(). 2026-07-19 명문화(운영점검보고서 §3-4/§5-3).';

COMMENT ON COLUMN macro_snapshot_5m.timestamp IS
    '실제로는 naive KST 벽시계 시각이 "+00"으로 잘못 라벨링된 값 — TIMESTAMPTZ지만 진짜 UTC 아님. '
    '정책 설명: mahdi/data/db.py local_now(). 2026-07-19 명문화(운영점검보고서 §3-4/§5-3).';
