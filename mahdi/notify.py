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
from datetime import datetime

import httpx

from mahdi.config.settings import get_slack_settings
from mahdi.data import db

logger = logging.getLogger("mahdi.notify")

_SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
_SLACK_SEND_INTERVAL_SECONDS = 1.0  # Slack 레이트리밋(채널당 1 req/sec 권장) — 미륵이와 동일 근거

_LEVEL_ICON = {"INFO": "ℹ️", "WARNING": "⚠️", "CRITICAL": "🚨"}

_queue: asyncio.Queue[str] | None = None


def _get_queue() -> asyncio.Queue[str]:
    global _queue
    if _queue is None:
        _queue = asyncio.Queue()
    return _queue


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
        return

    icon = _LEVEL_ICON.get(level, "")
    ts = datetime.now().strftime("%H:%M:%S")
    full_message = f"{icon} [{ts}] [마흐디] {message}"
    logger.info("Slack 알림: %s", full_message)
    try:
        _get_queue().put_nowait(full_message)
    except asyncio.QueueFull:
        logger.warning("Slack 알림 큐가 가득 참 — 메시지 버림: %s", full_message)


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
