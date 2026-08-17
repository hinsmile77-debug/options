"""수기 이벤트 캘린더 (2026-08-05, 운영점검보고서 2026-08-05 §2 이상점 2 후속).

이 파일의 핵심은 "다음 이벤트까지 몇 분"이 아니라 **"모른다"와 "없다"를 가르는 것**이다 —
그 구분이 없으면 캘린더를 안 채운 날과 이벤트가 없는 날이 구분되지 않고, 시스템은
2026-08-05 이전 상태(페널티 한 번도 안 걸림)로 조용히 되돌아간다.
"""

from datetime import date, datetime

import pytest

from mahdi.fusion.event_calendar import (
    NEEDS_ATTENTION,
    STATUS_EMPTY,
    STATUS_NO_UPCOMING,
    STATUS_NOT_COVERED,
    STATUS_OK,
    minutes_to_next_event,
)


def _calendar(covered_through="2026-08-13", events=None):
    return {"version": "1", "covered_through": covered_through, "events": events or []}


# ===== 핵심: "모른다"와 "없다"의 구분 =====


def test_not_covered_and_no_upcoming_are_different_states():
    """둘 다 페널티는 안 걸지만(없는 값을 지어내지 않는다), **경고 대상은 하나뿐**이다."""
    not_covered = minutes_to_next_event(datetime(2026, 8, 20, 10, 0), _calendar())
    no_upcoming = minutes_to_next_event(datetime(2026, 8, 12, 10, 0), _calendar())

    assert not_covered.status == STATUS_NOT_COVERED
    assert no_upcoming.status == STATUS_NO_UPCOMING
    assert not_covered.minutes is None and no_upcoming.minutes is None  # 페널티는 둘 다 없다
    assert not_covered.status in NEEDS_ATTENTION  # 사람이 채워야 한다
    assert no_upcoming.status not in NEEDS_ATTENTION  # 이건 사실이다 — 조용해야 한다


def test_missing_or_malformed_calendar_is_flagged_not_silently_empty():
    for raw in (None, {}, {"events": []}, {"covered_through": "이상한값"}):
        result = minutes_to_next_event(datetime(2026, 8, 6, 10, 0), raw)
        assert result.status == STATUS_EMPTY
        assert result.minutes is None
        assert result.status in NEEDS_ATTENTION


# ===== 근접도 계산 =====


def test_minutes_to_the_nearest_upcoming_event():
    cal = _calendar(events=[
        {"when": "2026-08-13 15:35", "name": "먼슬리 만기"},
        {"when": "2026-08-06 15:35", "name": "위클리(목) 만기"},
    ])
    result = minutes_to_next_event(datetime(2026, 8, 6, 15, 20), cal)

    assert result.status == STATUS_OK
    assert result.minutes == pytest.approx(15.0)
    assert result.next_event == "위클리(목) 만기"  # 가장 가까운 것 — 목록 순서와 무관


def test_past_events_are_skipped():
    """이벤트가 지난 뒤에는 그 다음 이벤트를 본다."""
    cal = _calendar(events=[
        {"when": "2026-08-06 15:35", "name": "위클리(목) 만기"},
        {"when": "2026-08-10 15:35", "name": "위클리(월) 만기"},
    ])
    result = minutes_to_next_event(datetime(2026, 8, 6, 15, 40), cal)

    assert result.next_event == "위클리(월) 만기"


def test_proximity_crosses_the_penalty_threshold_exactly_as_classify_expects():
    """`classify()`는 `event_proximity_minutes < 15`일 때만 x0.5를 건다 — 경계를 고정한다."""
    from mahdi.fusion.meta_label import MetaLabelInputs, classify

    cal = _calendar(events=[{"when": "2026-08-06 15:35", "name": "만기"}])
    thresholds = {"event_proximity_penalty_minutes": 15, "event_proximity_penalty_factor": 0.5}
    base = dict(regime_confidence=1.0, signal_agreement_count=4, available_member_count=4)

    outside = minutes_to_next_event(datetime(2026, 8, 6, 15, 19), cal)  # 16분 전
    inside = minutes_to_next_event(datetime(2026, 8, 6, 15, 25), cal)   # 10분 전

    assert classify(MetaLabelInputs(**base, event_proximity_minutes=outside.minutes),
                    thresholds).conviction_score == pytest.approx(1.0)
    assert classify(MetaLabelInputs(**base, event_proximity_minutes=inside.minutes),
                    thresholds).conviction_score == pytest.approx(0.5)


# ===== 견고성: 캘린더가 판단을 죽이면 안 된다 =====


def test_covered_through_boundary_day_is_inclusive():
    """`covered_through`가 오늘이면 오늘까지는 확인된 것으로 본다."""
    result = minutes_to_next_event(datetime(2026, 8, 13, 9, 0), _calendar("2026-08-13"))
    assert result.status != STATUS_NOT_COVERED


def test_invalid_entries_are_counted_not_silently_dropped():
    """오타 하나로 이벤트가 사라진 것을 아무도 모르면 안 된다."""
    cal = _calendar(events=[
        {"when": "2026-08-06 15:35", "name": "정상"},
        {"when": "2026/08/07 15:35", "name": "형식 오류"},
        {"name": "when 누락"},
        "리스트가 아닌 항목",
    ])
    result = minutes_to_next_event(datetime(2026, 8, 6, 15, 0), cal)

    assert result.status == STATUS_OK
    assert result.next_event == "정상"
    assert result.invalid_entries == 3


def test_accepts_seconds_precision_and_native_datetime():
    """YAML이 `when`을 datetime으로 파싱해 줄 수도 있고, 초까지 적을 수도 있다."""
    assert minutes_to_next_event(
        datetime(2026, 8, 6, 15, 0), _calendar(events=[{"when": "2026-08-06 15:30:00", "name": "x"}])
    ).minutes == pytest.approx(30.0)
    assert minutes_to_next_event(
        datetime(2026, 8, 6, 15, 0),
        _calendar(events=[{"when": datetime(2026, 8, 6, 15, 30), "name": "x"}]),
    ).minutes == pytest.approx(30.0)


def test_covered_through_may_be_a_native_date():
    result = minutes_to_next_event(datetime(2026, 8, 6, 9, 0), _calendar(date(2026, 8, 13)))
    assert result.covered_through == date(2026, 8, 13)


# ===== 배포된 파일 자체의 계약 =====


def test_shipped_calendar_parses_and_declares_its_coverage():
    """저장소에 든 파일이 실제로 읽히는지 — 형식이 깨지면 전 판단이 조용히 페널티 없음이 된다."""
    from mahdi.config.settings import get_event_calendar

    raw = get_event_calendar()
    result = minutes_to_next_event(datetime(2026, 8, 6, 9, 0), raw)

    assert result.status != STATUS_EMPTY, "배포된 event_calendar.yaml이 파싱되지 않는다"
    assert result.invalid_entries == 0, "배포된 캘린더에 형식 오류 항목이 있다"
    assert raw.get("covered_through") is not None


# ===== 2026-08-17 — 만기 항목 생성기(옮겨 적기 자동화) =====

from datetime import date  # noqa: E402

from mahdi.fusion.event_calendar import EXPIRY_EVENT_TIME, render_expiry_events  # noqa: E402


def _observed(expiry: date, **overrides) -> dict:
    base = dict(expiry=expiry, rows=10_613, first_seen=date(2026, 8, 11),
                last_seen=date(2026, 8, 17), lead_days=7)
    base.update(overrides)
    return base


def test_renders_pasteable_event_block_with_expiry_time():
    lines = render_expiry_events([_observed(date(2026, 8, 18))])
    assert f'- when: "2026-08-18 {EXPIRY_EVENT_TIME}"' in "\n".join(lines)
    assert 'kind: "expiry"' in "\n".join(lines)


def test_weekday_is_taken_from_the_observed_date_not_a_rule():
    """08-18은 대체공휴일로 밀린 **화요일** 만기다 — 주기 규칙이면 못 맞힌다."""
    lines = render_expiry_events([_observed(date(2026, 8, 18))])
    assert "옵션 만기(화)" in "\n".join(lines)


def test_already_registered_expiry_is_not_suggested_again():
    calendar = {"events": [{"when": f"2026-08-18 {EXPIRY_EVENT_TIME}", "name": "x", "kind": "expiry"}]}
    assert render_expiry_events([_observed(date(2026, 8, 18))], calendar) == []


def test_observation_evidence_is_printed_so_the_human_can_name_it():
    """먼슬리 여부는 실측으로 알 수 없다 — 판단 재료만 준다."""
    joined = "\n".join(render_expiry_events([_observed(date(2026, 9, 10), rows=9663, lead_days=27)]))
    assert "9,663행" in joined and "리드 27일" in joined


def test_covered_through_is_never_generated():
    """`covered_through`는 사람의 선언이다 — 생성기가 만들면 안 된다."""
    joined = "\n".join(render_expiry_events([_observed(date(2026, 8, 18))]))
    assert "covered_through" not in joined
