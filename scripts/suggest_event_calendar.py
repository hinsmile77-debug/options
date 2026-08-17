"""이벤트 캘린더 만기 항목 생성기 — 붙여넣을 YAML을 인쇄한다(파일은 고치지 않는다).

2026-08-17. `mahdi/config/event_calendar.yaml`의 만기 항목을 사람이 **DB를 열어보고 옮겨
적고** 있었다. 그 노동이 2026-08-14~08-17 나흘 만료의 실제 원인이다 — 매크로 일정을 못
찾아서가 아니라 만기를 안 옮겨 적어서였고, 그동안 확신도 이벤트 페널티(×0.5)는 한 번도
걸리지 않았다.

## 이 스크립트가 자동화하는 것과 하지 않는 것

    자동화한다    "언제 만기인가"를 DB에서 읽어 YAML 블록으로 만드는 일 (= 옮겨 적기)
    자동화 안 한다 `covered_through` (= 사람의 선언)

`covered_through`를 기계가 옮기면 "확인했다"를 기계가 대신 말하는 것이고, 그 순간 이 파일이
존재하는 유일한 이유가 사라진다. 2026-08-05 9차 결정이 만기 자동 배선을 거부한 근거가 정확히
그것이다 — 만기만 자동으로 채워지면 **"이벤트 근접도가 배선됐다"는 커버리지 착시**가 생긴다
(FOMC는 여전히 안 걸리는데 표에는 값이 찍힌다).

그래서 이 스크립트는 **파일을 쓰지 않는다.** 인쇄만 하고 붙여넣기는 사람이 한다.

## 왜 주기 규칙이 아니라 실측인가

2026-08-18 만기는 **화요일**이다. 08-15 광복절이 토요일이라 08-17(월)이 대체공휴일이 되면서
위클리(월)가 하루 밀렸다. "매주 월·목, 매월 둘째 목"이라는 규칙으로는 절대 나오지 않는
날짜이고, 이 저장소는 같은 형태로 이미 다쳤다(2026-08-11 북 선택 역전 — "위클리는 늘
먼슬리보다 가깝다"는 전제가 깨져 ATM IV가 홀수분/짝수분 0.168 격차로 교대했다). 공휴일
캘린더는 이 코드베이스에 없고, 만들지 않기로 한 결정이 여러 곳에 적혀 있다.

`option_analysis_1m.expiry`는 브로커가 매분 알려주는 **실측**이다. 08-18은 08-11부터 이미
DB에 있었다.

## 실행

    uv run python scripts/suggest_event_calendar.py

출력 블록을 `mahdi/config/event_calendar.yaml`의 `events:` 아래에 붙여넣고,
**매크로 일정까지 확인한 뒤** `covered_through`를 직접 옮긴다.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mahdi.config.settings import get_event_calendar  # noqa: E402
from mahdi.data import db  # noqa: E402
from mahdi.fusion.event_calendar import (  # noqa: E402
    coverage_gap_days,
    render_expiry_events,
)


def main() -> int:
    today = db.local_now().date()

    try:
        calendar = get_event_calendar()
    except Exception as exc:  # noqa: BLE001 — 캘린더가 깨져 있어도 제안은 할 수 있다
        print(f"경고: 현재 캘린더를 읽지 못했다({exc!r}) — 중복 제거 없이 전부 제안한다")
        calendar = None

    try:
        with db.get_connection() as conn:
            observed = db.observed_future_expiries(conn, today)
    except Exception as exc:  # noqa: BLE001
        print(f"DB 조회 실패: {exc!r}")
        print("체인 수집이 죽었거나 DB가 안 떠 있다 — 이 경우 '만기가 없다'가 아니라 '모른다'다.")
        return 1

    print(f"[{today}] option_analysis_1m 실측 미래 만기 {len(observed)}건")

    gap = coverage_gap_days(calendar, today)
    if gap is None:
        print("  covered_through: 없거나 형식 오류 — 이 값이 없으면 「이벤트 없음」과 「안 채웠음」이 구분되지 않는다")
    elif gap > 0:
        print(f"  covered_through: {gap}일 전에 만료됨 — 오늘 판정에 이벤트 페널티가 걸리지 않는다")
    else:
        print("  covered_through: 아직 유효")

    if not observed:
        print("\n미래 만기가 하나도 관측되지 않았다.")
        print("**'만기가 없다'는 뜻이 아니다** — 체인 수집이 죽었을 수 있다. 폴러 상태를 먼저 볼 것.")
        return 1

    lines = render_expiry_events(observed, calendar)
    if not lines:
        print("\n관측된 미래 만기가 모두 이미 캘린더에 있다 — 추가할 것이 없다.")
    else:
        print("\n아래를 `mahdi/config/event_calendar.yaml`의 `events:` 아래에 붙여넣을 것:\n")
        for line in lines:
            print(line)

    print("\n" + "-" * 78)
    print("`covered_through`는 이 스크립트가 정하지 않는다.")
    print("만기 외 매크로 일정(FOMC·고용지표 등)까지 확인한 뒤 사람이 직접 옮길 것 —")
    print("그 선언을 기계가 대신하면 「확인했다」가 거짓이 되고, 경고는 조용해진다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
