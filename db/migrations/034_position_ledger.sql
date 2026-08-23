-- Mahdi 추가 (2026-08-23, 실행 배선 ① — 포지션 생애주기 추적) — 포지션 원장.
--
-- `execution/*` 전체가 라이브에 배선될 수 없었던 **단일 최상위 블로커**를 여는 표다
-- (2026-08-06 `docs/동작흐름과상태` §2 "배선 선행 조건": *"포지션 생애주기 추적이 없어
-- `has_open_position_same_direction`을 채울 곳이 없다"*).
--
-- ## 030 `position_snapshots`와 무엇이 다른가 — 겹치지 않는다
--
-- 030은 **브로커가 말한 것의 미러**다. 매 잔고 조회마다 한 행씩 쌓이고, 「그 분에 무엇을
-- 들고 있었나」를 답한다. 시간축 표다.
--
-- 이 표는 **우리가 아는 것의 원장**이다. 포지션 하나당 한 행이고, 열릴 때 생겨 닫힐 때
-- 닫힌다. 브로커가 **모르는 것**을 든다:
--
--     언제 들어갔는가 · 어느 전략으로 · 그때 레짐은 무엇이었나 · 확신도는 얼마였나
--
-- 브로커는 평균단가는 알아도 「09:14에 vol_expansion_long 전략이 확신도 0.71로 들어갔다」를
-- 모른다. 그 절반이 없으면 `exit_stack.PositionState`를 만들 수 없고(진입시각·레짐이 필수
-- 필드다) 청산 평가가 성립하지 않는다.
--
-- ## 진실원천은 여전히 브로커다 (L12/R12)
--
-- 이 표가 「무엇을 들고 있는가」의 답이 되면 안 된다 — 프로세스가 죽은 사이에 체결이 나거나
-- 사람이 HTS로 청산하면 이 표는 즉시 거짓이 된다. 그래서 매 잔고 조회마다
-- `position_ledger.reconcile()`이 브로커 응답과 이 표를 맞추고, **갈리면 브로커가 이긴다.**
-- 030의 헤더가 적은 *"재시작 복원은 브로커 API 재조회 → Reconciler 대사 → 구독 시작 순"*이
-- 그대로 적용된다.
--
-- ## `opened_at_exact`가 왜 컬럼인가 — 모르는 것을 지어내지 않는다
--
-- 브로커에는 있는데 원장에 없는 포지션(사람의 수동 거래, 원장 유실, 이 표가 생기기 전에 열린
-- 포지션)의 진입 시각을 우리는 **모른다**. 그때 `opened_at`에 **하한**(직전 잔고 조회 시각,
-- 없으면 세션 시작)을 넣고 이 플래그를 FALSE로 둔다.
--
-- 하한을 쓰면 보유 시간이 과대평가되고 타임스톱이 **더 일찍** 걸린다 — 안전한 쪽으로만
-- 틀린다. 플래그가 없으면 그 값이 추정이라는 사실이 사라지고, 리포트는 그 포지션의 보유
-- 시간을 실측인 양 인쇄한다(계명 12: 조용한 폴백 금지).
--
-- ## PK와 부분 유니크 인덱스
--
-- PK는 (symbol, opened_at) — 같은 종목을 하루에 여러 번 여닫을 수 있으므로 종목 단독은 안 된다.
-- 그리고 **열려 있는 행은 종목당 하나뿐**이라는 것이 이 표의 불변식이라, 부분 유니크 인덱스로
-- DB가 그것을 강제한다. 코드 버그로 같은 종목이 두 번 열리면 조용히 이중 계상되는 대신
-- 적재가 실패한다 — 그 실패가 로그에 남는 쪽이 낫다.

CREATE TABLE IF NOT EXISTS position_ledger (
    symbol VARCHAR(30) NOT NULL,
    opened_at TIMESTAMPTZ NOT NULL,
    side VARCHAR(10) NOT NULL,
    qty DECIMAL(18,4),
    entry_price DECIMAL(18,4),
    opened_at_exact BOOLEAN NOT NULL DEFAULT TRUE,
    origin VARCHAR(20) NOT NULL DEFAULT 'order',
    strategy_id VARCHAR(50),
    entry_order_id VARCHAR(40),
    regime_entry SMALLINT,
    exit_rules_key VARCHAR(30),
    confidence_entry DECIMAL(6,4),
    last_seen_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    exit_price DECIMAL(18,4),
    exit_reason VARCHAR(50),
    PRIMARY KEY (symbol, opened_at));

-- 열려 있는 행은 종목당 하나 — 위 헤더의 불변식을 DB가 강제한다.
CREATE UNIQUE INDEX IF NOT EXISTS idx_position_ledger_open_symbol
    ON position_ledger (symbol) WHERE closed_at IS NULL;

-- 「오늘 몇 건 열고 닫았나」를 리포트가 매일 묻는다.
CREATE INDEX IF NOT EXISTS idx_position_ledger_opened_at ON position_ledger (opened_at);

COMMENT ON COLUMN position_ledger.opened_at IS
    '실제로는 naive KST 벽시계 시각이 "+00"으로 잘못 라벨링된 값 — TIMESTAMPTZ지만 진짜 UTC 아님. '
    '정책 설명: mahdi/data/db.py local_now(). opened_at_exact=FALSE면 이 값은 실제 진입 시각이 '
    '아니라 하한이다(그 시각 이후에 열렸다는 것만 안다).';
COMMENT ON COLUMN position_ledger.opened_at_exact IS
    'FALSE = 진입 시각을 모른다. opened_at은 하한이고 보유 시간은 과대평가된다(타임스톱이 더 '
    '일찍 걸리는 안전 방향). 원인은 origin=orphan — 사람의 수동 거래이거나 원장 유실.';
COMMENT ON COLUMN position_ledger.origin IS
    'order = 우리 주문의 체결로 열렸다(진입 맥락 전부 있음). '
    'orphan = 브로커에만 있었다(진입 시각·전략·레짐·확신도를 모른다).';
COMMENT ON COLUMN position_ledger.qty IS
    '브로커 잔고의 cblc_qty를 미러링한다 — 대사 때마다 갱신된다. 원장이 소유하는 값이 아니다.';
COMMENT ON COLUMN position_ledger.entry_price IS
    '브로커 잔고의 ccld_avg_unpr1(체결평균단가1)을 미러링한다. 추가 진입이 있으면 브로커가 '
    '평균을 다시 내므로 이 값도 따라 바뀐다 — 즉 "최초 체결가"가 아니라 "현재 평균단가"다.';
COMMENT ON COLUMN position_ledger.exit_rules_key IS
    '진입 시점에 exit_stack.exit_rules_key()가 낸 값. 레짐이 바뀌어도 청산 규칙을 진입 시점 '
    '기준으로 유지할지 매 분 재평가할지는 ④ 청산 루프가 정한다 — 이 컬럼은 진입 시점의 기록이다.';
COMMENT ON COLUMN position_ledger.closed_at IS
    'NULL = 열려 있다. 값이 있으면 그 시각까지는 확실히 닫혔다는 뜻이고, 실제 체결은 직전 '
    '잔고 조회와 이 시각 사이 어딘가다(우리 주문으로 닫은 경우에는 체결 시각으로 덮어쓴다).';
COMMENT ON COLUMN position_ledger.exit_reason IS
    'trade_history.exit_reason과 같은 어휘 — HARD_STOP/STRUCT/FLOW/BELIEF/TIME/FORCED_FLAT/MANUAL. '
    'RECONCILED_FLAT = 대사에서 사라진 것을 발견했다(누가 닫았는지 모른다). '
    'SIDE_FLIPPED = 같은 종목이 반대 방향으로 바뀌어 있었다.';
