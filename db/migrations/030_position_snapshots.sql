-- Mahdi 추가 (2026-08-16, Block B — 모의투자 개시 준비) — 보유 포지션 스냅샷.
--
-- `get_balance()`(선물옵션 잔고현황, CTFO6118R/VTFO6118R) 응답의 `output1` 배열을 행별로
-- 적재한다. 015가 `output2`(계좌 합계)만 담고 `output1`은 **방향별 개수 두 개로 요약해
-- 버렸던** 자리를 메운다.
--
-- ## 이 테이블은 진실원천이 아니다
--
-- 포지션의 권위는 **브로커**다(L12/R12 — "프로세스는 무상태. 재시작 복원은 브로커 API
-- 재조회 → Reconciler 대사 → 구독 시작 순"). 그래서 라이브 판단은 이 테이블을 읽지 않고
-- 매 사이클 잔고 응답을 직접 쓴다. 이 테이블의 용도는 둘이다:
--   (a) 사후 재구성 — "그 분에 무엇을 들고 있었나"를 답한다(현재는 답할 수 없다).
--   (b) **필드 실측의 원재료** — `raw`가 KIS가 실제로 보낸 dict를 그대로 보존한다.
--
-- ## 왜 raw JSONB가 필수인가 (R8 / 계명 11)
--
-- 아래 타입 컬럼은 **공식 문서(docs/efriend xlsx "선물옵션 잔고현황" 시트)에서 Required로
-- 확인한 필드**로 만들었다. 그러나 이 저장소는 `output1`에 보유 종목이 담긴 응답을 **한 번도
-- 받아본 적이 없다** — 주문이 나간 적이 없기 때문이다(`execution_logs` 0행). 즉 값의 형식은
-- 아직 미실측이고, R8은 "실측 범위표 작성 후 스키마 확정"을 요구한다.
--
-- 그래서 순서를 이렇게 둔다: **문서 확인분으로 컬럼을 열고, 원본을 함께 보존한다.** 8/18에
-- 첫 포지션이 생기면 이 표의 첫 행 하나가 곧 범위표의 원재료가 되고, 그때
-- `docs/dev_memory/KIS_RAW_FIELD_RANGES.md`에 실측을 적고 컬럼을 확정한다. 2026-07-06에
-- `output1.gama`(gamma가 아니라 gama)를 실측으로 잡아낸 것과 같은 절차다.
--
-- ## PK를 (timestamp, symbol)로 두는 이유
--
-- 한 잔고 조회는 종목당 최대 한 행이다. 재처리·재조회에도 멱등해야 하므로 시간축과 종목을
-- 함께 잡는다(015가 timestamp 단독 PK로 멱등을 얻은 것과 같은 이유).

CREATE TABLE IF NOT EXISTS position_snapshots (
    timestamp TIMESTAMPTZ NOT NULL,
    symbol VARCHAR(30) NOT NULL,
    side VARCHAR(10),               -- BUY / SELL / UNKNOWN (account_tracker.classify_side)
    qty DECIMAL(18,4),
    avg_price DECIMAL(18,4),
    current_price DECIMAL(18,4),
    eval_pnl DECIMAL(18,4),
    liquidatable_qty DECIMAL(18,4),
    raw JSONB,
    PRIMARY KEY (timestamp, symbol));

SELECT create_hypertable('position_snapshots', 'timestamp', if_not_exists => TRUE);

-- 015에 방향 판정 실패분을 함께 남긴다. 이 값이 0이 아닌 날은 같은 행의
-- same_direction_buy_count/sell_count를 신뢰할 수 없다(그만큼이 어느 쪽에도 안 세어졌다).
ALTER TABLE account_balance_snapshots ADD COLUMN IF NOT EXISTS unknown_side_count INTEGER;

COMMENT ON COLUMN position_snapshots.timestamp IS
    '실제로는 naive KST 벽시계 시각이 "+00"으로 잘못 라벨링된 값 — TIMESTAMPTZ지만 진짜 UTC 아님. '
    '정책 설명: mahdi/data/db.py local_now(). 2026-07-19 명문화(운영점검보고서 §3-4/§5-3).';
COMMENT ON COLUMN position_snapshots.symbol IS
    'output1[].shtn_pdno — 단축상품번호(예: 101P09). 선물 6자리 / 옵션 9자리.';
COMMENT ON COLUMN position_snapshots.side IS
    'output1[].sll_buy_dvsn_name을 classify_side()로 정규화한 값. UNKNOWN = 값이 있는데 '
    '우리가 모르는 값(공식 문서는 "매수" 혹은 "BUY" 둘 다 가능하다고 적고 있고, 이 계좌는 '
    '아직 포지션을 가진 적이 없어 실제 값이 미실측이다). UNKNOWN은 동일방향 한도에서 '
    '후보 방향으로 세어진다 — 모르면 안전한 쪽.';
COMMENT ON COLUMN position_snapshots.avg_price IS
    'output1[].ccld_avg_unpr1(체결평균단가1) — 진입가 기준. 문서 확인분, 라이브 미실측.';
COMMENT ON COLUMN position_snapshots.current_price IS
    'output1[].idx_clpr(지수종가) — 문서 확인분, 라이브 미실측. 옵션 보유 시 이 값이 옵션 '
    '가격인지 기초자산 지수인지 8/18 실측으로 확인할 것(이름은 "지수"종가다).';
COMMENT ON COLUMN position_snapshots.liquidatable_qty IS
    'output1[].lqd_psbl_qty(청산가능수량) — 15:10 Forced Flat이 실제로 낼 수 있는 수량. '
    'qty와 다를 수 있으므로 강제청산은 이 값을 쓴다.';
COMMENT ON COLUMN position_snapshots.raw IS
    'KIS output1 행 원본. **지우지 말 것** — 위 컬럼들은 공식 문서 기준이고 실측 확정 전이다.';
COMMENT ON COLUMN account_balance_snapshots.unknown_side_count IS
    '방향 판정에 실패한 보유 종목 수(2026-08-16). 0이 아니면 같은 행의 '
    'same_direction_buy_count/sell_count가 실제 포지션 수를 밑돈다.';
