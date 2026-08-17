"""휴장일 달력 — **오늘 시장이 열리는가** (2026-08-17 2차 신설).

## 이 모듈이 생긴 이유

`liveness.in_watch_window()`는 `now.time()`만 봤다. 요일도 휴장일도 모른다. 그 사실이
2026-08-15~17 사흘에 걸쳐 세 가지 모양으로 나왔다:

    08-15(토) 07:40:02  워치독이 `missing`을 보고 **토요일에 시스템 전체를 부팅**했다.
                        장전 기동 작업은 월~금 트리거라 안 떴는데 워치독이 대신 띄웠다.
    08-16(일) 10:14:39  같은 일. 두 날 모두 재기동 상한 3회를 채우고 `ALERT_ONLY`를
                        94줄·113줄 쏟았다.
    08-17(대체공휴일)   장전 기동이 **정상적으로** 떠서 종일 돌았다. 가장 조용하고 가장 나빴다.

## 진짜 피해는 재기동이 아니라 적재였다

`option_analysis_1m` 실측:

    2026-08-13 목(영업)  9,194행   IV 고유값 3,040
    2026-08-17 월(휴장)  9,640행   IV 고유값    30    ← 30 = 레그 수. 값이 하루 종일 얼어 있었다

**행 수로는 구분이 안 된다.** 학습·백테스트가 이 하루를 거래일로 읽는다. 워치독을 고치는 것은
증상을 막고, 이 달력이 원인을 막는다.

## 왜 `session.py`가 아닌가

`session`은 **시각만 보는 모듈**이고 그것이 그 모듈의 정체성이다(파일 I/O도 외부 의존도 없다 —
`scripts/measure_order_roundtrip.py`가 그 사실에 기대어 경고문을 적어 뒀다). 거기에 달력을
넣으면 "세션 경계"와 "영업일"이라는 다른 두 질문이 한 파일에 섞인다.

## 왜 `fusion/event_calendar.py`가 아닌가

그쪽은 **판단 층**의 입력(`event_proximity_minutes`)이고 `mahdi/fusion/`이 소유한다. 워치독이
판단 층 모듈을 import하기 시작하면 **감시자가 감시 대상에 묶인다** — 규약 D가 금지하는 결합이고,
2026-08-14에 워치독의 DB 조회에 타임아웃을 건 것과 같은 이유다.

다만 `covered_through` **해석은 그쪽 것을 그대로 쓴다**(규약 B — 같은 사실을 두 곳에 적지
않는다). 그 함수는 캘린더 종류를 모르는 순수 함수이고 stdlib 말고는 아무것도 안 끌어온다.

## 미등재는 「거래일」로 본다

오류의 비용이 비대칭이다: 빠뜨린 휴장일은 **오늘까지의 상태와 같지만**, 잘못 넣은 날짜는
**거래일에 시스템을 통째로 재운다.** 그래서 못 읽은 달력·형식 오류·미등재는 전부 거래일이다.
`liveness.intentional_stop_at()`이 "의심스러우면 감시하는 쪽으로 넘어진다"고 정한 것과 같은
방향이고, 이유도 같다.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from mahdi.fusion.event_calendar import coverage_gap_days as _coverage_gap_days

logger = logging.getLogger("mahdi.market_calendar")

# 토(5)·일(6). **이 요일들은 달력 파일에 적지 않는다** — 계산으로 나오는 것을 수기로 적으면
# 매년 104줄이 늘고, 그 줄을 안 채운 해가 조용히 생긴다.
WEEKEND_DAYS = frozenset({5, 6})


def _as_date(value: date | datetime) -> date:
    """`datetime`도 `date`도 받는다 — 호출측이 `db.local_now()`를 그대로 넘길 수 있게."""
    return value.date() if isinstance(value, datetime) else value


def is_weekend(value: date | datetime) -> bool:
    """반환: 토요일이거나 일요일인가. 파일도 설정도 안 본다(순수 계산)."""
    return _as_date(value).weekday() in WEEKEND_DAYS


def parse_holidays(calendar: dict | None) -> dict[date, str]:
    """반환: `{날짜: 이름}`. 못 읽은 항목은 **조용히 버린다**.

    항목 하나가 깨졌다고 달력 전체를 버리면 그날 시스템이 휴장일에 뜬다 — 그것은 빠뜨림과
    같은 급의 실패지만, 예외를 밖으로 내면 **관측 루프가 아예 못 뜨는** 더 나쁜 실패가 된다.
    깨진 항목은 `covered_through` 경고가 아니라 이 로그로 잡는다.
    """
    if not calendar:
        return {}
    parsed: dict[date, str] = {}
    for entry in calendar.get("holidays") or []:
        if not isinstance(entry, dict):
            logger.warning("휴장일 항목이 dict가 아니다 — 건너뛴다: %r", entry)
            continue
        raw = entry.get("date")
        if isinstance(raw, datetime):
            day = raw.date()
        elif isinstance(raw, date):
            day = raw
        elif isinstance(raw, str):
            try:
                day = datetime.strptime(raw.strip(), "%Y-%m-%d").date()
            except ValueError:
                logger.warning("휴장일 날짜 형식 오류 — 건너뛴다: %r", raw)
                continue
        else:
            logger.warning("휴장일 날짜가 없다 — 건너뛴다: %r", entry)
            continue
        parsed[day] = str(entry.get("name") or "휴장일")
    return parsed


def holiday_name(value: date | datetime, calendar: dict | None) -> str | None:
    """반환: 그날이 **등재된 휴장일**이면 이름, 아니면 None.

    **주말은 여기서 None이다.** 주말과 휴장일은 둘 다 "안 여는 날"이지만 출처가 다르다 —
    하나는 계산이고 하나는 사람이 확인한 사실이다. 섞으면 로그에서 "달력이 맞았는가"를 물을 수
    없게 된다. 둘을 합친 판정은 `is_trading_day()`가 한다.
    """
    return parse_holidays(calendar).get(_as_date(value))


def is_trading_day(value: date | datetime, calendar: dict | None) -> bool:
    """반환: 그날 시장이 열리는가(= 주말이 아니고 등재된 휴장일도 아니다).

    입력: 날짜(또는 datetime)와 달력 원본 dict. **파일을 안 읽는다** — 순수 함수라 pytest가
         "어느 날 안 여는가"를 검사할 수 있다(`scripts/`를 얇게 두는 규약과 같은 이유).
    실패 조건: 없다. `calendar`가 None이면 등재된 휴장일이 없는 것으로 보고 주말만 판정한다 —
              **못 읽은 달력은 「거래일」쪽으로 접는다**(모듈 docstring의 비대칭 참고).
    """
    if is_weekend(value):
        return False
    return holiday_name(value, calendar) is None


def coverage_gap_days(calendar: dict | None, today: date) -> int | None:
    """반환: `covered_through`가 `today`보다 며칠 지났는지. 유효하면 0, 못 읽으면 None.

    구현은 `fusion.event_calendar.coverage_gap_days`를 그대로 쓴다 — 그 함수는 캘린더 종류를
    모르고 `covered_through` 한 필드만 해석하는 순수 함수다. 여기에 같은 파싱을 다시 적으면
    두 캘린더의 만료 규칙이 조용히 갈라진다(규약 B).

    **0과 None을 가른다**: 0은 "확인했고 아직 유효하다", None은 "확인 자체가 불가능하다"이고
    후자가 더 나쁘다. 둘을 섞으면 필드를 지운 날이 정상으로 보인다.
    """
    return _coverage_gap_days(calendar, today)


def load_holiday_calendar() -> dict:
    """달력 파일을 읽는다. 실패하면 **빈 dict** — 예외를 밖으로 내지 않는다.

    달력 하나 때문에 관측 루프나 워치독이 못 뜨면 본말이 뒤집힌다. 빈 dict는 "등재된 휴장일이
    없다" = 평일이면 거래일이고, 그것은 2026-08-17 이전의 동작과 정확히 같다.
    """
    try:
        from mahdi.config.settings import get_market_holidays

        return get_market_holidays()
    except Exception:  # noqa: BLE001 — 설정 로딩 실패가 감시를 멈추면 안 된다
        logger.warning("휴장일 달력을 읽지 못했다 — 모든 평일을 거래일로 본다", exc_info=True)
        return {}
