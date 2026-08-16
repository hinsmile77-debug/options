"""오늘 발동일인 레버가 꺼진 채 기동하려는지 **기동 직전에** 알린다.

2026-08-13 고도화 4 + 2026-08-14 고도화 5.

## 이 스크립트가 생긴 이유

레버 F(`use_effective_member_count`)는 08-12·08-13·08-14 **세 번** 「오늘 켤 것」으로
지정되고 세 번 다 안 켜졌다. 레버 E는 **일곱 번** 미뤄졌다. 열 번 중 한 번도 「안 켜기로
했다」고 적힌 적이 없다 — 전부 아침에 잊은 것이다.

공통 원인은 하나다: **켜는 시점이 사람의 아침 기억에 달려 있고, 어긋나도 기동 시퀀스가
아무 말을 하지 않는다.** 설정은 기동 시 로드되므로 07:31에 창이 닫히고, 그 뒤에 알아채면
그날은 이미 늦다(08-14 장중 §6이 정확히 그 계산이었다 — 반나절 지나 켜면 기준선과 시간
길이가 달라 판정이 성립하지 않는다).

## 이 스크립트가 하지 않는 것

**레버를 켜지 않는다.** 판단은 사람이 한다 — 2026-07-08에 페이서 자동 적응이 500 폭주를
만들어 203분을 태운 전례가 있고, `DECISION_LOG` 결정 7이 그것을 규약으로 굳혔다.
여기서는 **콘솔에 크게 적고 종료 코드 0으로 끝난다**: 기동을 막지도 않는다. 아침에 기동이
막히면 그날 관측이 통째로 사라지고, 그 대가는 안 켠 레버보다 훨씬 크다.

실행: `python scripts/check_lever_due.py` (인자 없음)
      `scripts/start_mahdi_premarket.bat`이 관측 루프를 띄우기 전에 부른다.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mahdi.config.settings import PROJECT_ROOT
from mahdi.data import db

_BAR = "=" * 78
_HYPOTHESES = PROJECT_ROOT / "docs" / "동작점검" / "hypotheses.yaml"


def _shout(lines: list[str]) -> None:
    """콘솔에 **크게** 적는다 — 기동 로그 수십 줄 사이에서 눈에 걸려야 한다.

    ## 이모지를 쓰지 않는다

    이 스크립트는 `start_mahdi_premarket.bat`의 콘솔(cp949)에서 돈다. `⛔`/`⚠`는 cp949로
    인코딩되지 않아 `UnicodeEncodeError`로 죽는다 — 도입 당일 08-19를 흉내 내 돌려 보다
    실제로 터졌다. **경고를 내려다 경고가 사라지는 것**이 이 함수의 유일한 실패 모드이고,
    그래서 표식은 ASCII로 두고 stdout도 `errors="replace"`로 한 겹 더 막는다.
    한글 자체는 cp949에 있으므로 문제가 없다.
    """
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    print("")
    print(_BAR)
    for line in lines:
        print(line)
    print(_BAR)
    print("")
    _also_write_to_startup_log(lines)


def _also_write_to_startup_log(lines: list[str]) -> None:
    """같은 경고를 `logs/premarket_startup.log`에도 남긴다.

    ## 왜 콘솔만으로는 부족한가 (2026-08-16 통합 리허설에서 확인)

    배치는 이 스크립트를 **의도적으로 리다이렉트하지 않는다** — 그 자리에
    *"콘솔에 그대로 찍는다: 로그 파일로 보내면 아침에 아무도 안 본다"* 는 주석이 있다.
    맞는 판단이지만 절반이다: 기동이 **작업 스케줄러**로 돌면 그 콘솔을 보는 사람이 없고,
    리허설에서 실제로 확인한 대로 `premarket_startup.log`에는 배치 자신의
    `레버 발동일 점검` 한 줄만 남고 **경고 내용은 어디에도 없다.**

    그래서 콘솔 출력을 **없애지 않고** 로그를 더한다(리다이렉트로 하면 콘솔이 죽으므로
    파이썬 쪽에서 해야 한다 — R15: 배치는 호출 한 줄, 로직은 파이썬). 장전 점검과
    `collect_evidence.py`가 읽는 파일이 이것이므로, 사후에 「그날 아침 무엇이 경고됐나」를
    답할 수 있게 된다.
    실패 조건: 쓰기에 실패해도 **조용히 넘어간다** — 로그를 못 써서 기동이 막히면 그 대가가
              경고를 못 남기는 것보다 크다(이 파일의 「기동을 막지 않는다」 규약).
    """
    try:
        path = PROJECT_ROOT / "logs" / "premarket_startup.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = db.local_now().strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as f:
            f.write(f"[{stamp}] --- check_lever_due 경고 ---\n")
            for line in lines:
                f.write(f"[{stamp}] {line}\n")
    except Exception:  # noqa: BLE001
        pass


def _event_calendar_warning(today) -> list[str]:
    """반환: 이벤트 캘린더가 만료됐거나 확인 불가면 외칠 줄 목록. 유효하면 빈 목록.

    판정은 `fusion.event_calendar.coverage_gap_days()`가 한다 — 스크립트는 얇게 유지한다
    (`docs/동작점검/README.md` 규약: 로직은 모듈에, `scripts/`는 인용만).
    """
    try:
        from mahdi.config.settings import get_event_calendar
        from mahdi.fusion.event_calendar import coverage_gap_days

        gap = coverage_gap_days(get_event_calendar(), today)
    except Exception as exc:  # noqa: BLE001 — 이 점검이 기동을 막지 않는다
        return [f"??? 이벤트 캘린더 확인 실패: {exc!r}"]

    if gap is None:
        return [
            "*** 이벤트 캘린더에 `covered_through`가 없거나 형식이 잘못됐다",
            "",
            "   mahdi/config/event_calendar.yaml",
            "   → 이 필드가 없으면 「이벤트 없음」과 「안 채웠음」이 구분되지 않는다.",
        ]
    if gap > 0:
        return [
            f"*** 이벤트 메모 캘린더가 {gap}일 전에 만료됐다 - 오늘 판정에 이벤트 페널티가 안 걸린다",
            "",
            "   mahdi/config/event_calendar.yaml 의 `covered_through`를 갱신할 것",
            "   → 만료 상태면 `event_proximity_minutes`가 항상 None이고,",
            "     메타 라벨의 이벤트 근접 x0.5 페널티가 하루 종일 한 번도 걸리지 않는다.",
            "",
            "   ※ 일정 확인은 사람이 한다 - 이 스크립트는 날짜를 옮기지 않는다.",
        ]
    return []


def main() -> int:
    try:
        from mahdi.ops import hypotheses, levers
    except Exception as exc:  # noqa: BLE001
        print(f"[check_lever_due] 건너뜀 — 모듈 로드 실패: {exc!r}")
        return 0

    try:
        entries = hypotheses.load(_HYPOTHESES)
        lever_state = levers.collect(PROJECT_ROOT)
        today = db.local_now().date()
    except Exception as exc:  # noqa: BLE001
        # **기동을 막지 않는다.** 이 점검이 못 돌았다고 그날 관측을 통째로 잃을 수는 없다.
        print(f"[check_lever_due] 건너뜀 — 상태 수집 실패: {exc!r}")
        return 0

    breaches = hypotheses.lever_deadline_breaches(entries, today, lever_state)
    due = hypotheses.levers_due_today(entries, today, lever_state)

    if breaches:
        lines = ["*** 무조건발동일이 지났는데 레버가 꺼져 있다 - 오늘 켜거나, 날짜를 옮기고 사유를 적을 것", ""]
        for b in breaches:
            deferrals = b.get(hypotheses.FIELD_DEFERRALS)
            tail = f" · 유예 {deferrals}회차" if deferrals else ""
            lines.append(
                f"   {b['id']}  (기한 {b[hypotheses.FIELD_DEADLINE]}, {b['지난일수']}일 지남{tail})"
            )
            lines.append(f"     꺼진 레버: {', '.join(b['off'])}")
        lines += ["", "   ※ 설정은 기동 시 로드된다 - 지금 안 켜면 오늘은 창이 닫힌다."]
        _shout(lines)

    if due:
        lines = ["!!! 오늘이 발동일인 레버가 아직 꺼져 있다", ""]
        for d in due:
            lines.append(f"   {d['id']}  (발동일 {d[hypotheses.FIELD_ACTIVATE_ON]})")
            lines.append(f"     꺼진 레버: {', '.join(d['off'])}")
        lines += ["", "   ※ 안 켜기로 했다면 `hypotheses.yaml`에 **유예 사유를 문자로** 남길 것.",
                  "     08-12·08-13·08-14 세 번 다 그것을 안 적어서 유예가 무행동으로 성립했다."]
        _shout(lines)

    # 2026-08-16 (Block D / 08-14 Fix#7) — 이벤트 캘린더 만료를 **기동 전에** 한 번 크게 말한다.
    #
    # 런타임은 이미 만료를 알고 매분 WARNING을 남긴다. 그런데 08-14에 그 경고는 억제 로깅
    # 뒤에서 **484회** 발생하고 표시는 9줄이었으며 사람에게 도달한 것은 0건이었다.
    # 매분 한 줄은 아무도 안 읽는다 — 아침에 한 번이 낫다.
    #
    # 피해는 로그가 아니라 **판정**이다: 만료된 캘린더는 `event_proximity_minutes`를 None으로
    # 만들어 메타 라벨의 x0.5 이벤트 페널티를 **한 번도 걸리지 않게** 한다. 08-14 `signal_decisions`
    # 493행 전부가 그 상태로 산출됐다. 레버 F를 켜는 날 이 파일이 만료돼 있으면 그날 확신도
    # 판정이 통째로 오염된다(08-14 §1-2).
    calendar_lines = _event_calendar_warning(today)
    if calendar_lines:
        _shout(calendar_lines)

    if not breaches and not due and not calendar_lines:
        print("[check_lever_due] 오늘 발동일인 레버 없음 · 기한 초과 없음 · 이벤트 캘린더 유효")
    # **항상 0.** 기동을 막는 것은 이 점검의 일이 아니다(파일 docstring 참고).
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
