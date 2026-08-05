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


# 2026-08-05(운영점검보고서 §2-4) — 예외 유형당 하루 몇 건까지 트레이스백 전문을 남길지.
#
# 08-05 로그 33,737줄 중 **16,577줄(49%)이 트레이스백**이었다. ReadTimeout 210건 x 78.9줄.
# 그 결과 자동 리포트 §0은 `log_volume.human_lines` 21,176줄을 근거로 가설 `2026-08-04-p4`를
# **반증 판정했는데, 실제 사람이 읽는 줄은 4,599줄로 예측치(<=6,500)를 통과했다** — 08-04 Fix#6
# (ATM 히스테리시스)의 성공이 트레이스백에 묻혀 실패로 보고된 것이다.
#
# 원인은 두 가지가 겹친 것이다:
#   1. `_log_kis_call_failure()`가 HTTPStatusError가 **아닌** 예외에 항상 `exc_info=True`를 붙인다.
#      그 결정(08-03 §2-8)은 "예외는 드무니 어디서 났는지가 곧 원인"이라는 전제 위에 있었다.
#   2. 08-04 Fix#8이 read 타임아웃을 10초 → 4초로 낮췄다. ReadTimeout이 **예외적 사건에서
#      일상적 사건**이 됐다(08-04 3건 → 08-05 210건, 70배). 전제가 fix 하나로 무너졌는데
#      그 fix가 전제를 재검토하지 않았다.
#
# **끄지 않고 표본만 남기는 이유**: 트레이스백을 완전히 없애면 새로운 예외 유형이 나타났을 때
# 원인 추적이 불가능해진다. 08-03이 HTTPStatusError의 트레이스백을 뗄 수 있었던 것은 "원인이
# 응답 바디에 전부 있다"는 근거가 있었기 때문이고, ReadTimeout에는 그런 근거가 없다 — 다만
# **210번째 트레이스백이 3번째보다 알려주는 것이 없다**는 것은 확실하다.
TRACEBACK_SAMPLES_PER_EXCEPTION_TYPE = 3


class TracebackBudget:
    """예외 유형별로 하루 N건까지만 트레이스백 전문을 허용한다(2026-08-05 §2-4).

    프로세스 수명이 곧 하루다(장전 07:30 기동 → 장마감 15:45 종료)이므로 별도의 날짜 리셋을
    두지 않는다 — 리셋이 필요해지는 날은 프로세스가 자정을 넘겨 사는 날이고, 그때는 이
    카운터보다 먼저 검토할 것이 많다.
    """

    def __init__(self, samples_per_type: int = TRACEBACK_SAMPLES_PER_EXCEPTION_TYPE) -> None:
        self._samples = samples_per_type
        self._seen: dict[str, int] = {}

    @staticmethod
    def type_name(exc: BaseException) -> str:
        """`httpx.ReadTimeout` — 트레이스백 마지막 줄이 쓰는 것과 **같은 표기**다.

        `log_metrics._EXCEPTION_PREFIXES`가 그 표기로 집계하므로, 트레이스백을 생략한 요약 줄도
        같은 문자열을 실어야 카운터가 유형을 잃지 않는다(08-04 §2-1의 재발 방지).
        """
        cls = type(exc)
        return f"{cls.__module__}.{cls.__qualname__}"

    def take(self, exc: BaseException) -> tuple[bool, int]:
        """입력: 예외. 반환: (트레이스백을 남길 것인가, 오늘 이 유형의 누적 건수).

        누적 건수를 함께 돌려주는 이유: 생략된 줄에 "오늘 N번째"를 찍으면 **트레이스백을 껐다는
        사실 자체가 로그에 남는다.** 그것이 없으면 "예외가 줄었다"와 "예외를 안 찍는다"가
        구분되지 않는다 — 08-04 §2-1이 정확히 그 구분을 잃어 벌어진 사고였다.
        """
        name = self.type_name(exc)
        count = self._seen.get(name, 0) + 1
        self._seen[name] = count
        return count <= self._samples, count
