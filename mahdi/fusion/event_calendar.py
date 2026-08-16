"""매크로 이벤트 캘린더 — 메타 라벨의 `event_proximity_minutes` 입력 (v6 §11.2).

2026-08-05(운영점검보고서 2026-08-05 §2 이상점 2 후속). `classify()`는 다음 이벤트까지 남은
시간이 임계(기본 15분)보다 짧으면 확신도에 x0.5 페널티를 거는데, 캘린더 소스가 프로젝트에
없어 이 입력이 **전 이력 None**이었다 — FOMC·옵션만기 직전에도 페널티가 걸린 적이 없다.

**수기 테이블(`mahdi/config/event_calendar.yaml`)을 고른 이유와 그 대가**: 외부 API는
yfinance 폴백과 같은 운영 신뢰도 문제를 하나 더 만들고(2026-07-20 결정 참고), 만기일만
자동 배선하면 "이벤트 근접도가 배선됐다"는 **커버리지 착시**가 생긴다(FOMC는 여전히 안 걸리는데
표에는 값이 찍힌다). 수기 테이블의 대가는 **사람이 안 채우면 조용히 페널티가 사라지는 것**이고,
그것은 이번 점검에서 네 번 반복해 발견한 결함 형태(배선은 됐고 데이터가 상수를 만든다)와 같다.

그래서 이 모듈의 핵심은 "다음 이벤트까지 몇 분"이 아니라 **`covered_through`로
"모른다"와 "없다"를 가르는 것**이다:

    not_covered  사람이 확인한 범위를 지났다 → **경고한다.** 페널티는 걸지 않는다(모르니까).
    no_upcoming  범위 안이고 남은 이벤트가 없다 → 조용하다. 이건 사실이다.

이 둘을 합치면 캘린더를 안 채운 날과 이벤트가 없는 날이 구분되지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

# `when` 필드가 받는 형식. 초 단위까지 적어도 되게 둘 다 허용한다.
_WHEN_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S")

STATUS_OK = "ok"
STATUS_NO_UPCOMING = "no_upcoming"
STATUS_NOT_COVERED = "not_covered"
STATUS_EMPTY = "empty"

# 사람이 손대야 하는 상태 — 호출측은 이 집합으로 경고 여부를 판단한다(문자열 비교를 흩뿌리지
# 않기 위해 여기 모아둔다).
NEEDS_ATTENTION = frozenset({STATUS_NOT_COVERED, STATUS_EMPTY})


@dataclass(frozen=True, slots=True)
class EventProximity:
    """`minutes`는 페널티 계산에 그대로 들어가고, `status`는 사람에게 알릴지를 정한다."""

    minutes: float | None
    status: str
    next_event: str | None = None
    covered_through: date | None = None
    invalid_entries: int = 0


def _parse_when(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    for fmt in _WHEN_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


def _parse_covered_through(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def coverage_gap_days(calendar: dict | None, today: date) -> int | None:
    """
    입력: `event_calendar.yaml` 원본 dict, 기준 날짜.
    반환: `covered_through`가 `today`보다 **며칠 지났는지**. 아직 유효하면 0,
          `covered_through` 자체가 없거나 못 읽으면 **None("모른다")**.
    해석: 2026-08-16 (Block D / 08-14 Fix#7) — 만료 판정을 **기동 전에** 물을 수 있게 공개한다.

         런타임은 이미 만료를 안다(`minutes_to_next_event()`가 `STATUS_NOT_COVERED`를 낸다).
         없던 것은 **기동 시퀀스가 그것을 말해 주는 경로**다. 08-14에 이 파일은 만료된 채
         하루를 돌며 억제 로깅 뒤에서 **484회** 경고했지만 사람에게 도달한 것은 0건이었고,
         표시된 줄 수는 9였다. 매분 한 줄은 아무도 안 읽는다 — **아침에 한 번 크게** 말해야 한다.

         **0과 None을 가른다**(규약 C): 0은 "확인했고 아직 유효하다", None은 "확인 자체가
         불가능하다"(필드 없음·형식 오류)이고 후자가 더 나쁘다. 둘을 섞으면 필드를 지운 날이
         정상으로 보인다.
    실패 조건: 없음.
    """
    if not calendar:
        return None
    covered_through = _parse_covered_through(calendar.get("covered_through"))
    if covered_through is None:
        return None
    # 경계일은 포함한다 — `covered_through`가 오늘이면 오늘까지는 확인된 것이다
    # (`minutes_to_next_event()`가 같은 규칙을 쓴다: `now.date() > covered_through`일 때만 만료).
    return max(0, (today - covered_through).days)


def minutes_to_next_event(now: datetime, calendar: dict | None) -> EventProximity:
    """
    입력: 현재 시각(`db.local_now()`와 같은 naive 로컬 기준), `event_calendar.yaml` 원본 dict.
    계산: `covered_through`를 먼저 보고, 범위 안이면 `now` 이후 가장 가까운 이벤트까지의 분을 낸다.
    해석: 상세 근거는 모듈 docstring. **`minutes`가 None인 이유는 두 가지이고 그 둘은 다르다** —
         `no_upcoming`(확인했고 없다)과 `not_covered`/`empty`(모른다). 페널티는 어느 쪽이든
         걸지 않지만(없는 값을 지어내지 않는다), 후자만 경고 대상이다.

         **경계일은 포함한다**: `covered_through`가 오늘이면 오늘까지는 확인된 것으로 본다.

         파싱 실패한 항목은 건너뛰되 `invalid_entries`로 **개수를 알린다** — 조용히 버리면
         오타 하나 때문에 이벤트가 사라진 것을 아무도 모른다.
    실패 조건: 없음 — 어떤 입력이 와도 `EventProximity`를 돌려준다(캘린더가 판단을 죽이면 안 된다).
    """
    if not isinstance(calendar, dict) or not calendar:
        return EventProximity(minutes=None, status=STATUS_EMPTY)

    covered_through = _parse_covered_through(calendar.get("covered_through"))
    if covered_through is None:
        return EventProximity(minutes=None, status=STATUS_EMPTY)
    if now.date() > covered_through:
        return EventProximity(minutes=None, status=STATUS_NOT_COVERED, covered_through=covered_through)

    raw_events = calendar.get("events") or []
    invalid = 0
    upcoming: list[tuple[datetime, str]] = []
    for entry in raw_events:
        if not isinstance(entry, dict):
            invalid += 1
            continue
        when = _parse_when(entry.get("when"))
        if when is None:
            invalid += 1
            continue
        if when >= now:
            upcoming.append((when, str(entry.get("name") or "이름 없음")))

    if not upcoming:
        return EventProximity(
            minutes=None, status=STATUS_NO_UPCOMING,
            covered_through=covered_through, invalid_entries=invalid,
        )

    when, name = min(upcoming, key=lambda item: item[0])
    return EventProximity(
        minutes=(when - now).total_seconds() / 60.0,
        status=STATUS_OK,
        next_event=name,
        covered_through=covered_through,
        invalid_entries=invalid,
    )
