"""휴장일 달력 — 오늘 시장이 열리는가 (2026-08-17 2차).

08-15(토)·08-16(일)에 워치독이 **주말에 시스템 전체를 부팅**했고, 08-17(광복절 대체공휴일)에는
장전 기동이 정상적으로 떠서 종일 돌며 IV가 얼어붙은 9,640행을 적재했다. 아래 테스트의 절반은
**달력이 너무 많이 잡지 않는지**를 본다 — 빠뜨린 휴장일보다 잘못 넣은 날짜가 훨씬 비싸다
(거래일에 시스템이 통째로 안 뜨고, 그날은 경고를 낼 주체도 없다).
"""

from __future__ import annotations

from datetime import date, datetime

from mahdi import market_calendar

# 2026-08-17은 광복절(08-15 토요일) 대체공휴일이다. 이 저장소가 자체 실측으로 네 곳에 적고 있다.
_HOLIDAY = date(2026, 8, 17)
_CALENDAR = {
    "covered_through": "2026-08-17",
    "holidays": [{"date": "2026-08-17", "name": "광복절 대체공휴일"}],
}


# ===== 주말 =====


def test_weekend_needs_no_file():
    """주말은 계산으로 나온다 — 달력에 적기 시작하면 매년 104줄이 늘고 안 채운 해가 생긴다."""
    assert market_calendar.is_weekend(date(2026, 8, 15)) is True   # 토
    assert market_calendar.is_weekend(date(2026, 8, 16)) is True   # 일
    assert market_calendar.is_weekend(date(2026, 8, 14)) is False  # 금
    assert market_calendar.is_weekend(date(2026, 8, 17)) is False  # 월(휴장일이지만 주말은 아니다)


def test_datetime_is_accepted_as_well_as_date():
    """호출측이 `db.local_now()`를 그대로 넘길 수 있어야 한다."""
    assert market_calendar.is_weekend(datetime(2026, 8, 15, 7, 40)) is True


def test_the_2026_08_15_saturday_boot_would_not_happen():
    """**이 파일의 존재 이유 1.** 08-15 07:40:02에 워치독이 토요일에 시스템을 부팅했다."""
    assert market_calendar.is_trading_day(datetime(2026, 8, 15, 7, 40, 2), _CALENDAR) is False
    assert market_calendar.is_trading_day(datetime(2026, 8, 16, 10, 14, 39), _CALENDAR) is False


# ===== 휴장일 =====


def test_the_2026_08_17_holiday_is_not_a_trading_day():
    """**이 파일의 존재 이유 2.** 그날 IV 고유값 30(=레그 수)짜리 9,640행이 적재됐다."""
    assert market_calendar.is_trading_day(_HOLIDAY, _CALENDAR) is False
    assert market_calendar.holiday_name(_HOLIDAY, _CALENDAR) == "광복절 대체공휴일"


def test_a_weekend_is_not_reported_as_a_holiday():
    """둘 다 "안 여는 날"이지만 출처가 다르다 — 하나는 계산, 하나는 사람이 확인한 사실이다.

    섞으면 로그에서 「달력이 맞았는가」를 물을 수 없게 된다.
    """
    assert market_calendar.holiday_name(date(2026, 8, 15), _CALENDAR) is None
    assert market_calendar.is_trading_day(date(2026, 8, 15), _CALENDAR) is False


def test_an_ordinary_weekday_is_a_trading_day():
    assert market_calendar.is_trading_day(date(2026, 8, 18), _CALENDAR) is True
    assert market_calendar.is_trading_day(date(2026, 8, 13), _CALENDAR) is True


# ===== 미등재는 「거래일」로 접는다 =====
#
# 비대칭이 이 절의 전부다:
#   빠뜨린 휴장일  → 08-17까지의 상태와 같다(휴장일에 뜬다). 다음날 데이터로 잡힌다.
#   잘못 넣은 날짜 → **거래일에 시스템이 안 뜬다.** 그날은 관측도, 알림도, 경고할 주체도 없다.


def test_no_calendar_still_judges_weekends():
    assert market_calendar.is_trading_day(date(2026, 8, 15), None) is False
    assert market_calendar.is_trading_day(_HOLIDAY, None) is True  # 모르면 거래일이다


def test_an_empty_calendar_is_not_an_error():
    assert market_calendar.parse_holidays({}) == {}
    assert market_calendar.parse_holidays({"holidays": None}) == {}
    assert market_calendar.is_trading_day(_HOLIDAY, {"holidays": []}) is True


def test_a_broken_entry_does_not_discard_the_whole_calendar():
    """항목 하나가 깨졌다고 달력 전체를 버리면 남은 휴장일까지 전부 놓친다."""
    parsed = market_calendar.parse_holidays({
        "holidays": [
            {"date": "이건-날짜가-아니다", "name": "깨진 항목"},
            "문자열 항목",
            {"name": "날짜가 없다"},
            {"date": "2026-08-17", "name": "광복절 대체공휴일"},
        ]
    })
    assert parsed == {_HOLIDAY: "광복절 대체공휴일"}


def test_a_date_object_in_yaml_is_accepted():
    """PyYAML은 따옴표 없는 `2026-08-17`을 `date`로 파싱한다 — 문자열만 받으면 조용히 샌다."""
    assert market_calendar.parse_holidays({"holidays": [{"date": _HOLIDAY}]}) == {_HOLIDAY: "휴장일"}


# ===== covered_through =====


def test_coverage_is_shared_with_the_event_calendar():
    """같은 사실을 두 곳에 적지 않는다(규약 B) — 만료 규칙이 조용히 갈라지면 안 된다."""
    from mahdi.fusion import event_calendar

    assert market_calendar.coverage_gap_days is not event_calendar.coverage_gap_days
    for today in (date(2026, 8, 17), date(2026, 8, 20), date(2026, 1, 1)):
        assert market_calendar.coverage_gap_days(_CALENDAR, today) == \
            event_calendar.coverage_gap_days(_CALENDAR, today)


def test_the_boundary_day_still_counts_as_covered():
    assert market_calendar.coverage_gap_days(_CALENDAR, _HOLIDAY) == 0


def test_an_expired_calendar_is_visible_as_a_number():
    assert market_calendar.coverage_gap_days(_CALENDAR, date(2026, 8, 20)) == 3


def test_a_missing_field_is_unknown_not_zero():
    """0("확인했고 유효하다")과 None("확인 자체가 불가능하다")을 가른다 — 후자가 더 나쁘다."""
    assert market_calendar.coverage_gap_days({"holidays": []}, _HOLIDAY) is None
    assert market_calendar.coverage_gap_days(None, _HOLIDAY) is None


# ===== 실제 파일 =====


def test_the_shipped_calendar_lists_the_day_we_actually_lost():
    """08-17이 빠지면 이 작업 전체가 무의미하다."""
    calendar = market_calendar.load_holiday_calendar()
    assert market_calendar.holiday_name(_HOLIDAY, calendar) is not None


def test_the_shipped_calendar_declares_its_coverage():
    """`covered_through`가 없거나 안 읽히면 **만료 경고가 영영 안 뜬다.**

    그 침묵이 이 방식의 유일한 실패 모드다 — `event_calendar.yaml` 헤더가 같은 말을 한다
    ("안 채운 걸 아무도 모르는 것").
    """
    calendar = market_calendar.load_holiday_calendar()
    assert calendar.get("covered_through")
    assert market_calendar.coverage_gap_days(calendar, _HOLIDAY) is not None


def test_entries_beyond_covered_through_are_allowed():
    """**목록의 끝과 신뢰의 끝은 다르다** — `event_calendar.yaml`이 이미 명시한 규약이다.

    처음에는 `날짜 <= covered_through`를 강제했다가 뺐다. 그 검사는 **비대칭을 거꾸로 적용한
    것**이었다: 12-25가 휴장일임을 아는데 연간 전수 확인은 아직 못 한 사람에게 "아는 것조차
    적지 말라"고 막는다. 그러면 그날 시스템이 휴장일에 뜬다 — 즉 **막는 쪽이 더 나쁘다.**

    `covered_through`가 답하는 질문은 「이 목록이 완전한가」이고, 개별 항목이 답하는 질문은
    「이 날은 안 여는가」다. 후자가 전자를 기다릴 이유가 없다.
    """
    partial = {
        "covered_through": "2026-08-17",
        "holidays": [{"date": "2026-12-25", "name": "성탄절"}],
    }
    assert market_calendar.is_trading_day(date(2026, 12, 25), partial) is False
    # 그러면서도 목록은 여전히 「불완전하다」고 신고한다 — 두 축은 독립이다.
    assert market_calendar.coverage_gap_days(partial, date(2026, 12, 25)) > 0
