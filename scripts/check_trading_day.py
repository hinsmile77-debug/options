"""오늘이 거래일인가 — 장전 기동이 뜰지 말지를 여기서 가른다 (2026-08-17 2차).

실행: `python scripts/check_trading_day.py`
종료 코드: **0 = 거래일(기동 진행)**, **1 = 비거래일(기동 건너뜀)**.

`start_mahdi_premarket.bat`이 `if errorlevel 1`로 받는다. 종료 코드를 쓰는 이유는 cmd.exe에서
문자열 출력을 파싱하는 것이 로케일·인코딩을 타기 때문이다(이 저장소가 여러 번 다친 지점 —
`daily_ops_report.py`가 `--out-dir`을 두는 것과 같은 이유).

## 이 스크립트가 얇은 이유

판정은 전부 `mahdi/market_calendar.py`에 있고 여기서는 **읽고, 인쇄하고, 종료 코드만** 낸다 —
`docs/동작점검/README.md`의 규약("로직은 파이썬 모듈에, `scripts/`는 얇게")이다. 그래야
"어느 날 안 여는가"를 pytest가 검사할 수 있다.

## 콘솔에 그대로 찍는다

`check_lever_due.py`와 같은 자리·같은 이유다. 로그 파일로 보내면 아침에 아무도 안 본다.
특히 **달력이 틀려 거래일을 건너뛴 날**에는 이 출력이 사람에게 도달하는 거의 유일한 신호다
(그날은 관측도 알림도 없다).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mahdi import market_calendar
from mahdi.data import db

EXIT_TRADING_DAY = 0
EXIT_NOT_TRADING_DAY = 1


def main() -> int:
    now = db.local_now()
    today = now.date()
    calendar = market_calendar.load_holiday_calendar()

    # `covered_through` 만료 경고 — **판정보다 먼저 인쇄한다.** 만료된 달력으로 낸 "거래일"은
    # 확인된 사실이 아니라 기본값이고, 그 구분이 다음 줄을 읽는 방식을 바꾼다.
    gap = market_calendar.coverage_gap_days(calendar, today)
    if gap is None:
        print("  ⚠ 휴장일 달력의 covered_through를 읽지 못했다 — mahdi/config/market_holidays.yaml 확인 필요")
    elif gap > 0:
        print(f"  ⚠ 휴장일 달력이 {gap}일째 만료 상태다 — KRX 공식 안내로 확인하고 covered_through를 옮길 것")
        print("     (미등재일은 «거래일»로 본다 — 오늘 휴장일이어도 그대로 뜬다)")

    if market_calendar.is_weekend(today):
        print(f"  ▪ {today:%Y-%m-%d}({'월화수목금토일'[today.weekday()]})은 주말이다 — 기동을 건너뛴다.")
        return EXIT_NOT_TRADING_DAY

    name = market_calendar.holiday_name(today, calendar)
    if name is not None:
        print(f"  ▪ {today:%Y-%m-%d}은 휴장일로 등재돼 있다({name}) — 기동을 건너뛴다.")
        print("     달력이 틀렸다면: scripts\\start_mahdi_premarket.bat force")
        return EXIT_NOT_TRADING_DAY

    return EXIT_TRADING_DAY


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        # **어떤 실패든 기동은 진행시킨다.** 달력을 못 읽었다고 그날 관측을 통째로 잃는 것이
        # 이 스크립트가 막으려는 것보다 훨씬 비싸다(market_calendar 모듈 docstring의 비대칭).
        print(f"  ⚠ 거래일 판정 실패({exc!r}) — 거래일로 보고 기동을 진행한다.")
        sys.exit(EXIT_TRADING_DAY)
