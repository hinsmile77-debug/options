"""로그 위생 유틸 (2026-07-19, 운영점검보고서 §5-5).

`logs/observation_loop.log`가 로테이션 없이 105MB까지 누적됐고, 그 상당 부분이 반복되는 WARNING
(예: 얇은 옵션 종목의 NumericValueOutOfRange가 60초 사이클마다 레그 단위로 계속 재발 — §3-1)이라
진짜 새로운 문제가 파묻히기 쉬웠다. 이 모듈은 그 문제의 두 축 중 "반복 경고 압축"을 담당한다
(로테이션 자체는 mahdi/main.py의 logging.handlers.RotatingFileHandler 설정 쪽).
"""

from __future__ import annotations

import logging
import time


class WarningThrottle:
    """같은 카테고리의 WARNING을 window_seconds당 최초 1건만 실제로 남기고, 그 사이 억제된
    나머지는 다음 로그 시점에 "N건 추가 억제됨"으로 요약해 붙인다."""

    def __init__(self, logger: logging.Logger, window_seconds: float = 60.0) -> None:
        self._logger = logger
        self._window = window_seconds
        self._last_logged_at: dict[str, float] = {}
        self._suppressed_count: dict[str, int] = {}

    def warning(self, category: str, message: str, *args, exc_info: bool = False) -> None:
        """
        입력: category(같은 종류의 반복 경고를 묶는 키 — 메시지 내용 자체는 매번 달라도 된다,
             예: 행사가/종목코드가 매번 바뀌는 레그 삽입 실패), message(%-포맷 문자열), 그 인자들.
        계산: 같은 category로 이미 window_seconds 이내에 로깅했으면 이번 호출은 억제하고 카운트만
             올린다. window가 지나 다시 로깅할 차례가 되면, 그동안 억제됐던 건수가 있는 경우
             메시지 끝에 "(최근 N초간 M건 추가 억제됨)"을 붙여 손실 없이 알린다(완전히 숨기면
             "그동안 계속 실패하고 있었다"는 사실 자체를 놓치게 됨).
        해석: category는 메시지 문자열 자체가 아니라 호출측이 명시적으로 넘기는 라벨이다 — 옵션체인
             레그 실패처럼 strike/type이 매번 바뀌어 메시지 문자열 자체가 매번 다른 경우에도 "같은
             종류의 반복 문제"로 묶어 억제하기 위함.
        """
        now = time.monotonic()
        last = self._last_logged_at.get(category)
        if last is not None and now - last < self._window:
            self._suppressed_count[category] = self._suppressed_count.get(category, 0) + 1
            return
        suppressed = self._suppressed_count.pop(category, 0)
        self._last_logged_at[category] = now
        if suppressed:
            self._logger.warning(
                message + " (최근 %.0f초간 %d건 추가 억제됨)",
                *args, self._window, suppressed,
                exc_info=exc_info,
            )
        else:
            self._logger.warning(message, *args, exc_info=exc_info)
