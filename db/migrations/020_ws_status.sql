-- Mahdi 추가 (2026-08-01, 운영점검보고서 2026-07-31 §5-4) — WS 연결/재연결 감지의 생존 신호.
--
-- 07-31 하루 WS 재연결 **0회**. 그 자체는 좋은 일이지만, 재연결 로직이 살아있는지는 여전히
-- 증명되지 않았다 — CB 감지가 "정상일에 아무 흔적도 안 남겨" 생겼던 문제와 같은 구조다.
-- WS 수신 자체는 market_raw_1m 적재로 간접 증명되지만 그건 "재연결 감지"가 아니다.
--
-- 원칙(2026-08-01 DECISION_LOG): **생존 신호는 감시 대상과 독립한 타이머에서 나와야 한다.**
--   updated_at            poll_ws_heartbeat()가 300초마다 갱신. "관측 루프의 WS 파트가 살아있다".
--   connected_since       현재 연결이 수립된 시각. 재연결이 있었으면 갱신된다.
--   last_message_at       마지막 WS 메시지 수신 시각. **장중에만** 의미가 있다
--                         (장외에는 체결이 없어 비어 있는 게 정상 — 임계를 걸면 상시 오경보).
--   reconnect_count_today 오늘 재연결 횟수. 0이 정상이며, 늘어나면 KIS/네트워크를 본다.

CREATE TABLE IF NOT EXISTS ws_status (
    id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),  -- 단일 행만 허용하는 싱글턴 트릭
    updated_at TIMESTAMPTZ NOT NULL,
    connected_since TIMESTAMPTZ,
    last_message_at TIMESTAMPTZ,
    reconnect_count_today INTEGER NOT NULL DEFAULT 0);

COMMENT ON COLUMN ws_status.updated_at IS
    '독립 하트비트(mahdi.main.poll_ws_heartbeat, 300초)가 메시지 수신과 무관하게 갱신하는 '
    '"관측 루프 WS 파트 생존" 시각 — 오래되면 관측 루프 정지를 의심한다. '
    'naive KST 벽시계가 "+00"으로 라벨링된 값(mahdi/data/db.py local_now() 정책).';
COMMENT ON COLUMN ws_status.connected_since IS
    '현재 WS 연결이 수립된 시각. 재연결이 일어나면 갱신되므로 "이 연결이 얼마나 오래 유지됐는가"를 본다.';
COMMENT ON COLUMN ws_status.last_message_at IS
    '마지막 WS 메시지 수신 시각. **장중에만** 임계 판정 대상이다 — 장외에는 체결이 없어 '
    '비어 있는 것이 정상이고, 여기에 임계를 걸면 상시 오경보가 된다(2026-07-31 CB 감지에서 겪은 교훈).';
COMMENT ON COLUMN ws_status.reconnect_count_today IS
    '오늘 재연결 횟수. 0이 정상이며 프로세스 재기동 시 0으로 시작한다 — 이 값이 늘어나면 '
    'KIS/네트워크 상태를 확인한다(재연결 로직 자체는 2026-07-19 도입).';
