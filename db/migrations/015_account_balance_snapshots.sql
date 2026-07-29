-- Mahdi 추가 (2026-07-28 8차) — 계좌 손익/포지션 상태 추적기(mahdi/execution/account_tracker.py)의
-- 원료 테이블. get_balance()(선물옵션 잔고현황, CTFO6118R/VTFO6118R)를 주기 폴링한 결과를
-- 그대로 스냅샷한다 — 2026-07-28 7차 실측(DECISION_LOG 참고)으로 확인된 필드만 담는다.
--
-- prsm_dpast(추정예탁자산)를 시계열로 쌓아두면, 오늘/이번주 자정 이전 마지막 스냅샷(어제/지난주
-- 금요일 종가에 해당)과 비교해 daily_pnl_pct/weekly_pnl_pct를, 역대 최고치와 비교해
-- drawdown_pct를 계산할 수 있다(별도 손익 계산 API 불필요 — RiskEngine.evaluate_entry()가
-- 필요로 하는 AccountState를 이 테이블 하나로 채운다).

CREATE TABLE IF NOT EXISTS account_balance_snapshots (
    timestamp TIMESTAMPTZ NOT NULL,
    prsm_dpast DECIMAL(18,4),
    evlu_pfls_amt_smtl DECIMAL(18,4),
    trad_pfls_amt_smtl DECIMAL(18,4),
    dnca_cash DECIMAL(18,4),
    ord_psbl_cash DECIMAL(18,4),
    mgna_tota DECIMAL(18,4),
    same_direction_buy_count INTEGER,
    same_direction_sell_count INTEGER,
    PRIMARY KEY (timestamp));

SELECT create_hypertable('account_balance_snapshots', 'timestamp', if_not_exists => TRUE);

COMMENT ON COLUMN account_balance_snapshots.timestamp IS
    '실제로는 naive KST 벽시계 시각이 "+00"으로 잘못 라벨링된 값 — TIMESTAMPTZ지만 진짜 UTC 아님. '
    '정책 설명: mahdi/data/db.py local_now(). 2026-07-19 명문화(운영점검보고서 §3-4/§5-3).';
COMMENT ON COLUMN account_balance_snapshots.prsm_dpast IS
    '추정예탁자산(get_balance() output2.prsm_dpast) — 일간/주간 손익·드로우다운 계산의 기준값.';
COMMENT ON COLUMN account_balance_snapshots.same_direction_buy_count IS
    'output1[] 중 sll_buy_dvsn_name="BUY"인 보유 종목 수.';
COMMENT ON COLUMN account_balance_snapshots.same_direction_sell_count IS
    'output1[] 중 sll_buy_dvsn_name="SLL"인 보유 종목 수.';
