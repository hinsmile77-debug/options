"""Slack 알림 발송 (2026-07-19, 운영점검보고서 §5-4 "능동 알림 도입").

C:\\Users\\82108\\PycharmProjects\\futures(미륵이)의 utils/notify.py + utils/slack_queue.py 패턴을
따른다 — 다만 미륵이는 PyQt+threading 기반이라 threading.Queue+워커 스레드를 쓰지만, 마흐디는
전부 asyncio(main()의 asyncio.gather) 기반이라 asyncio.Queue+워커 태스크로 이식했다. 메시지를
큐에 넣고 별도 태스크가 순차 처리하는 이유도 동일하다 — Slack API가 채널당 초당 1건 권장이라,
알림을 호출한 자리에서 바로 HTTP 요청을 기다리면 관측 루프가 그만큼 멈춘다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime

import httpx

from mahdi.config.settings import get_slack_settings
from mahdi.data import db

logger = logging.getLogger("mahdi.notify")

_SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
_SLACK_SEND_INTERVAL_SECONDS = 1.0  # Slack 레이트리밋(채널당 1 req/sec 권장) — 미륵이와 동일 근거

_LEVEL_ICON = {"INFO": "ℹ️", "WARNING": "⚠️", "CRITICAL": "🚨"}

_queue: asyncio.Queue[str] | None = None

# ===== 2026-08-26 (08-26 §1-17 / P1-5) — **꺼진 스위치가 여덟 번 조용히 버렸다** =====
#
# 08-26에 워치독이 DEGRADED를 77분 연속 발령했고 `liveness.ALERT_COOLDOWN_SECONDS=600`으로
# `notify_sync(…, level="CRITICAL")`이 **8회 발동**했다(`.watchdog_state.json`의 `last_alert_at`이
# 14:30:02 → 15:23:02으로 움직인 것이 증거다). 아래 두 함수의 `if not enabled: return`이
# **로그 한 줄도 안 남기고** 그 8건을 전부 버렸다.
#
# 그 결과 그날 장후 회차는 「경보를 냈는데 안 갔다」와 「애초에 안 냈다」를 **구분할 방법이
# 없었다.** 다음에 진짜로 사람을 불러야 할 때 같은 자리에서 같은 것을 못 가른다.
#
# ⛔ **Slack 토글을 켜는 것이 아니다.** `NEXT_TODO.md`의 「Slack 알림 — 2026-08-01 결정,
# 보류 유지. **매 점검 보고서에서 다시 올리지 말 것**」은 그대로다(실거래 전환 검토 시점에
# 자동으로 재검토 대상이 된다). **이 fix는 「안 울린 것」을 「안 울린 채 기록되는 것」으로
# 바꿀 뿐이다.**
#
# 포맷을 상수로 두는 이유는 `mahdi/broker/rest_client.LOG_SLOW_CALL`과 같다 —
# `mahdi/ops/log_metrics.py`가 이 문구를 부분문자열로 세고, 계약 테스트가 양쪽을 묶는다.
LOG_ALERT_SUPPRESSED_TOGGLE_OFF = "알림 스킵(토글 꺼짐) — level=%s · %s"

# level별 억제 창. 폭주 시 이 줄 자체가 소음이 되면 안 된다.
#
# ⚠ **억제해도 건수는 안 잃는다** — `mahdi/logutil.WarningThrottle`과 같은 규약으로 다음 줄에
# 「최근 N초간 M건 추가 억제됨」을 붙이고, `log_metrics`가 그 M을 지표에 도로 더한다.
# 억제가 지표를 먹으면 이 fix가 스스로를 눈멀게 한다(08-06 Fix#4가 그 자리에서 배운 것이다).
#
# ⚠ **일회성 스크립트에서는 이 딕셔너리가 사실상 무효다.** 워치독은 1분마다 **새 프로세스**로
# 뜨므로 프로세스 메모리에 든 억제 상태가 매번 초기화된다 — 08-19 §4 Fix#1이 제안한 억제가
# 정확히 그 이유로 「작동하지 않는다」로 기각됐다(`NEXT_TODO.md`의 「다시 올리지 말 것」).
# **그쪽의 진짜 억제는 호출측의 `liveness.ALERT_COOLDOWN_SECONDS`(600초)다.**
# 여기 억제는 장중 관측 루프(`notify()`)처럼 **한 프로세스가 오래 사는 경로**를 위한 것이다.
_TOGGLE_OFF_LOG_WINDOW_SECONDS = 300.0
_toggle_off_last_logged_at: dict[str, float] = {}
_toggle_off_suppressed: dict[str, int] = {}


def _get_queue() -> asyncio.Queue[str]:
    global _queue
    if _queue is None:
        _queue = asyncio.Queue()
    return _queue


def _log_alert_dropped_by_toggle(message: str, level: str) -> None:
    """토글이 꺼져 버려지는 경보를 **한 줄 남긴다.**

    입력: 버려지는 메시지 본문과 레벨.
    계산: 같은 레벨로 `_TOGGLE_OFF_LOG_WINDOW_SECONDS` 안에 이미 남겼으면 건수만 올리고
         돌아간다. 다시 남길 차례가 되면 그동안 억제된 건수를 문구 끝에 붙인다.
    해석: 상세 근거는 `LOG_ALERT_SUPPRESSED_TOGGLE_OFF` 위 주석.
    실패 조건: 없다 — 이 함수는 예외를 던지지 않는다. 알림 경로의 로깅이 관측 루프를 죽이면
              안 된다는 원칙은 `notify()` 본문과 같다.
    """
    now = time.monotonic()
    last = _toggle_off_last_logged_at.get(level)
    if last is not None and now - last < _TOGGLE_OFF_LOG_WINDOW_SECONDS:
        _toggle_off_suppressed[level] = _toggle_off_suppressed.get(level, 0) + 1
        return
    suppressed = _toggle_off_suppressed.pop(level, 0)
    _toggle_off_last_logged_at[level] = now
    # 메시지를 통째로 싣지 않는다 — 경보 본문이 길면 이 줄이 로그를 덮는다.
    body = message[:120]
    if suppressed:
        logger.info(
            LOG_ALERT_SUPPRESSED_TOGGLE_OFF + " (최근 %.0f초간 %d건 추가 억제됨)",
            level, body, _TOGGLE_OFF_LOG_WINDOW_SECONDS, suppressed,
        )
    else:
        logger.info(LOG_ALERT_SUPPRESSED_TOGGLE_OFF, level, body)


def notify(message: str, level: str = "INFO") -> None:
    """
    입력: 메시지 본문, 레벨("INFO"|"WARNING"|"CRITICAL").
    계산: .env에 토큰/채널이 설정돼 있고, DB(slack_alert_settings — COCKPIT 체크박스가 토글하는
         값)가 켜져 있으면 큐에 메시지를 넣는다. 실제 HTTP 전송은 run_slack_worker()가 별도로
         순차 처리한다(호출한 자리에서 API 응답을 기다리지 않음).
    실패 조건: 이 함수는 절대 예외를 던지지 않는다 — 알림 실패/DB 조회 실패가 관측 루프(WS 수신,
              REST 폴링)를 죽이면 안 된다. 토큰/채널 미설정 시 조용히 무시(.env 미구성 상태에서도
              나머지 시스템은 정상 동작해야 하므로 에러가 아니라 정상적인 "알림 기능 꺼짐" 상태).
    """
    settings = get_slack_settings()
    if not settings.is_configured:
        return
    try:
        with db.get_connection() as conn:
            enabled = db.is_slack_alerts_enabled(conn)
    except Exception:
        logger.warning("Slack On/Off 설정 조회 실패 — 이번 알림 스킵", exc_info=True)
        return
    if not enabled:
        # 2026-08-26 P1-5 — 조용히 버리지 않는다. 근거는 `LOG_ALERT_SUPPRESSED_TOGGLE_OFF` 주석.
        _log_alert_dropped_by_toggle(message, level)
        return

    icon = _LEVEL_ICON.get(level, "")
    ts = datetime.now().strftime("%H:%M:%S")
    full_message = f"{icon} [{ts}] [마흐디] {message}"
    logger.info("Slack 알림: %s", full_message)
    try:
        _get_queue().put_nowait(full_message)
    except asyncio.QueueFull:
        logger.warning("Slack 알림 큐가 가득 참 — 메시지 버림: %s", full_message)


def notify_sync(message: str, level: str = "INFO") -> None:
    """
    입력: 메시지 본문, 레벨("INFO"|"WARNING"|"CRITICAL").
    계산: notify()와 같은 설정/On-off 확인을 거쳐 즉시(블로킹) Slack으로 전송한다.
         scripts/log_marketclose_stop.py처럼 asyncio 이벤트 루프 없이 한 번 실행되고 끝나는
         일회성 스크립트용 — notify()의 큐+run_slack_worker() 패턴은 워커 태스크가 이벤트
         루프 안에서 계속 돌고 있어야 큐가 실제로 비워지는데, 일회성 스크립트는 그 워커를
         띄울 이유가 없다(2026-07-21, 운영점검보고서 §4 "종료 결과 검증 알림").
    실패 조건: notify()와 동일하게 이 함수는 절대 예외를 던지지 않는다 — 알림 실패가
              호출측(장마감 스크립트)의 나머지 로직을 막으면 안 된다.
    """
    settings = get_slack_settings()
    if not settings.is_configured:
        return
    try:
        with db.get_connection() as conn:
            enabled = db.is_slack_alerts_enabled(conn)
    except Exception:
        logger.warning("Slack On/Off 설정 조회 실패 — 이번 알림 스킵", exc_info=True)
        return
    if not enabled:
        # 2026-08-26 P1-5 — 08-26에 여덟 건이 정확히 이 자리에서 사라졌다(워치독 CRITICAL).
        # ⚠ 워치독은 1분마다 새 프로세스라 위 억제 딕셔너리가 이 경로에서는 무효다 —
        #   그쪽의 억제는 호출측 `liveness.ALERT_COOLDOWN_SECONDS`(600초)가 이미 하고 있다.
        _log_alert_dropped_by_toggle(message, level)
        return

    icon = _LEVEL_ICON.get(level, "")
    ts = datetime.now().strftime("%H:%M:%S")
    full_message = f"{icon} [{ts}] [마흐디] {message}"
    logger.info("Slack 알림(동기): %s", full_message)
    try:
        body = json.dumps(
            {"channel": settings.slack_channel_id, "text": full_message}, ensure_ascii=False
        ).encode("utf-8")
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(
                _SLACK_POST_MESSAGE_URL,
                headers={
                    "Authorization": f"Bearer {settings.slack_bot_token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                content=body,
            )
        result = resp.json()
        if not result.get("ok"):
            logger.warning("Slack API 오류: %s", result.get("error", result))
    except Exception:
        logger.warning("Slack 전송 실패(동기)", exc_info=True)


async def run_slack_worker() -> None:
    """
    계산: 큐를 순차 처리하는 백그라운드 태스크 — main()의 asyncio.gather에 다른 폴러들과
         나란히 추가된다(.env에 토큰/채널이 설정된 경우에만, main() 참고). 메시지 사이
         _SLACK_SEND_INTERVAL_SECONDS만큼 대기해 Slack 레이트리밋을 지킨다.
    실패 조건: 전송 실패(API 오류·네트워크 예외)는 로그만 남기고 큐 처리를 계속한다 — 알림 전송
              실패가 이 태스크 자체를 죽이면 이후 모든 알림이 영구히 멈춘다.
    구현 메모: httpx의 json= 편의 파라미터는 Content-Type을 "application/json"으로만 보내고
              charset을 안 붙인다 — 2026-07-19 실제 채널로 테스트 발송해보니 Slack이 이 경우
              본문을 UTF-8이 아닌 다른 인코딩으로 잘못 해석해 한글이 깨져 도착함(응답에
              "missing_charset" 경고 동반). 미륵이 utils/slack_queue.py가 이미 이 문제를
              겪어 json.dumps(...).encode("utf-8") + "charset=utf-8" 헤더로 우회한 바로 그
              패턴을 그대로 가져온다.
    """
    settings = get_slack_settings()
    queue = _get_queue()
    async with httpx.AsyncClient(timeout=5.0) as client:
        while True:
            message = await queue.get()
            try:
                body = json.dumps(
                    {"channel": settings.slack_channel_id, "text": message}, ensure_ascii=False
                ).encode("utf-8")
                resp = await client.post(
                    _SLACK_POST_MESSAGE_URL,
                    headers={
                        "Authorization": f"Bearer {settings.slack_bot_token}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    content=body,
                )
                result = resp.json()
                if not result.get("ok"):
                    logger.warning("Slack API 오류: %s", result.get("error", result))
            except Exception:
                logger.warning("Slack 전송 실패", exc_info=True)
            finally:
                queue.task_done()
            await asyncio.sleep(_SLACK_SEND_INTERVAL_SECONDS)
