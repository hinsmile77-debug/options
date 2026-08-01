-- Mahdi 추가 (2026-07-31, 운영점검보고서 2026-07-31 §2-2 / §4 우선순위 4) — CB 감지의
-- "생존 신호"와 "최근 수신 사실"을 분리한다.
--
-- 07-30에 넣은 하트비트는 WS 메시지 핸들러 안에 있어서 "메시지가 오지 않으면 하트비트도 멈추는"
-- 구조였다. 그런데 07-31 하루치 실측 결과 H0UNMKO0(국내주식 장운영정보)은 **세션 전이 시점에만**
-- 발신된다는 사실이 확인됐다 — 하루 총 2건(08:00 장전세션 / 09:00 정규장 개시)뿐이고 09:00 이후
-- 15:45까지 한 건도 오지 않았다. 그 결과 updated_at이 09:00:05에 멈춘 채 6시간 45분이 지났고,
-- 이는 "감지기가 죽었을 때"와 정확히 같은 모습이라 생존 증명이 되지 못했다.
--
-- 조치: 두 신호를 컬럼으로 분리한다.
--   updated_at       — poll_market_halt_heartbeat()가 300초마다 갱신. "관측 루프가 살아있다".
--                      오래되면 경고할 수 있다(COCKPIT 임계 600초).
--   last_message_at  — WS 핸들러가 실제 수신 시각을 남긴다. "감지기가 최근 무언가를 봤다".
--                      **정상일에도 6시간 공백이 정상**이므로 임계를 두지 않는다(두면 상시 오경보).

ALTER TABLE market_halt_status ADD COLUMN IF NOT EXISTS last_message_at TIMESTAMPTZ;

COMMENT ON COLUMN market_halt_status.updated_at IS
    '독립 하트비트(mahdi.main.poll_market_halt_heartbeat, 300초)가 메시지 수신과 무관하게 갱신하는 '
    '"관측 루프 생존" 시각 — 오래되면 관측 루프 정지를 의심해야 한다(2026-07-31 재정의). '
    '실제로는 naive KST 벽시계 시각이 "+00"으로 잘못 라벨링된 값 — TIMESTAMPTZ지만 진짜 UTC 아님. '
    '정책 설명: mahdi/data/db.py local_now(). 2026-07-19 명문화(운영점검보고서 §3-4/§5-3).';
COMMENT ON COLUMN market_halt_status.last_message_at IS
    'H0UNMKO0 장운영정보를 마지막으로 수신한 시각(mahdi.data.db.mark_market_halt_message_seen, '
    '60초 스로틀). 이 TR은 세션 전이 시에만 발신되므로(2026-07-31 실측: 하루 2건) 정상일에도 '
    '수 시간 공백이 정상이다 — "N분 이상 미수신 = 이상"이라는 임계를 두면 상시 오경보가 된다. '
    'updated_at과 동일한 naive KST 시각 정책.';
