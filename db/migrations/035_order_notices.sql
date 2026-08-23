-- Mahdi 추가 (2026-08-23, 실행 배선 ② — 체결통보 WS) — 실시간 체결통보 원문 보존.
--
-- ## 이 표의 첫 행이 곧 실측이다 (R8 / 계명 11)
--
-- `mahdi/broker/order_notice.py`의 `_NOTICE_FIELDS`(22개 위치 기반 필드)는 **공식 문서의
-- 「복호화 후」 예시에서 옮긴 것이고 라이브 미실측이다.** 이 계좌는 주문이 체결된 적이 없어
-- 통보를 받아본 적이 없다 — 08-18 왕복도 **일부러 체결을 피한** 왕복이었다(원거리 지정가).
--
-- 위치 기반 파싱에서 순서가 틀리면 **이름이 통째로 밀린다.** 체결수량 자리에서 체결단가를
-- 읽는 식이고, 그 값은 형식이 그럴듯해서 조용히 통과한다. 2026-07-06 `output1.gama`
-- (gamma가 아니라 gama)와 같은 계열이되, 그때보다 나쁘다 — 이름이 아니라 **위치**라서
-- 필드 하나가 밀리면 그 뒤가 전부 밀린다.
--
-- 그래서 파싱 결과가 아니라 **복호문 원문을 통째로 남긴다.** 첫 통보가 오는 날 이 표의
-- 한 행이 `_NOTICE_FIELDS`를 실측으로 확정할 유일한 근거이고, `docs/dev_memory/
-- KIS_RAW_FIELD_RANGES.md`에 적을 원재료다.
--
-- ## 왜 execution_logs에 안 넣는가
--
-- `execution_logs`는 **우리가 낸 주문**의 상태 표(PK가 order_id)다. 체결통보는 다르다:
--   · 우리가 안 낸 주문에도 온다(사람이 HTS로 낸 것 — 원장의 「고아」와 같은 사건이다).
--   · 한 주문에 여러 건이 온다(부분체결·접수·거부가 각각 한 건).
--   · **파싱을 못 믿는 동안에도 남겨야 한다** — execution_logs는 파싱된 값만 담는 스키마다.
-- 둘을 합치면 파싱이 틀린 날 원문이 사라진다.
--
-- ## PK
--
-- (received_at, seq) — 같은 밀리초에 여러 건이 올 수 있고, 주문번호는 파싱 결과라 **믿을 수
-- 없는 값이다**(그것을 검증하려고 이 표를 만들었다). 파싱 실패도 행을 남겨야 하므로 파싱된
-- 어떤 필드도 키에 넣지 않는다.

CREATE TABLE IF NOT EXISTS order_notices (
    received_at TIMESTAMPTZ NOT NULL,
    seq INTEGER NOT NULL,
    tr_id VARCHAR(20),
    -- 아래는 전부 **파싱 결과**다. 위치 기반이라 미실측 상태에서는 밀렸을 수 있다.
    symbol VARCHAR(30),
    order_no VARCHAR(40),
    sell_buy_code VARCHAR(4),
    filled_qty VARCHAR(20),
    filled_price VARCHAR(20),
    filled_time VARCHAR(20),
    rejected_flag VARCHAR(4),
    filled_flag VARCHAR(4),
    accepted_flag VARCHAR(4),
    field_count INTEGER,
    -- **지우지 말 것.** 위 컬럼이 전부 틀려도 이 값 하나로 복원할 수 있다.
    plaintext TEXT,
    PRIMARY KEY (received_at, seq));

SELECT create_hypertable('order_notices', 'received_at', if_not_exists => TRUE);

COMMENT ON COLUMN order_notices.received_at IS
    '실제로는 naive KST 벽시계 시각이 "+00"으로 잘못 라벨링된 값 — TIMESTAMPTZ지만 진짜 UTC 아님. '
    '정책 설명: mahdi/data/db.py local_now(). 통보에 실린 filled_time과 다르다 — 이쪽은 우리가 '
    '받은 시각이고 저쪽은 거래소가 체결시킨 시각이다.';
COMMENT ON COLUMN order_notices.seq IS
    '같은 received_at 안의 일련번호. 한 프레임에 여러 건이 실려 올 수 있는지는 미실측이라 '
    '(문서의 데이터 건수 필드가 2 이상일 수 있다) 0부터 부여한다.';
COMMENT ON COLUMN order_notices.field_count IS
    '복호문을 |로 가른 실제 필드 수. _NOTICE_FIELDS(22)와 다르면 위치 기반 파싱이 밀린 것이고 '
    '같은 행의 다른 컬럼을 믿으면 안 된다. **이 값이 이 표의 자기검증이다.**';
COMMENT ON COLUMN order_notices.plaintext IS
    'AES 복호 후 원문(| 구분). R8 실측 확정의 유일한 근거 — 파싱 컬럼이 전부 틀려도 이것으로 '
    '복원한다. 계좌번호·고객ID가 포함될 수 있으므로 외부로 공유하지 말 것.';
COMMENT ON COLUMN order_notices.rejected_flag IS
    '거부여부 원값. 문서 예시는 "0"이지만 의미는 미실측이라 해석하지 않고 원값을 든다 — '
    '상태 판정의 권위는 REST 조회(parse_fill_status)에 있다.';
