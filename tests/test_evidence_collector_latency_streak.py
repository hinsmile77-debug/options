"""증거 수집기 §5-1 — 「이틀 연속」이 실제로 **며칠째**인가 (2026-09-17 제4부 Fix B).

09-17 §1-6: §5-1의 🔔 줄은 조건이 며칠째 서 있든 언제나 「이틀 연속」이라고 적는다 —
전날 사이드카 하나와만 대조하기 때문이다. 09-14부터 이어진 조건을 09-15·09-16·09-17이
전부 「이틀」로 보고했고, 그래서 세 회차가 매번 「오늘도 그냥 이틀이네」로 읽었다.

이 파일이 지키는 것 넷:

    1. **§5-1과 같은 축이다** — 전 엔드포인트(2026-08-25 P1-1 ④). `inquire-price`만 세는
       §8-4(`preemptive_uncovered_streak`)와 **다른 축**이고, 그 구분이 여기서 깨지면
       두 절이 서로 다른 수를 내면서 둘 다 맞다고 주장한다.
    2. **규약 C** — 「못 읽었다」가 「0일째」나 「연속이 끊겼다」로 접히지 않는다.
    3. **임계는 그날 사이드카에 적힌 값**이다(`two_day_p95_overlap`과 같은 원칙).
    4. ⛔ **판정하지 않는다** — 이 축에는 임계도 경보선도 없고, 기존 🔔 줄의 문구와
       발동 조건(「이틀 연속」)은 한 글자도 안 바뀐다. 마지막 세 건이 그것을 소스 수준에서
       못 박는다.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COLLECTOR = PROJECT_ROOT / "docs" / "동작점검" / "tools" / "collect_evidence.py"


@pytest.fixture(scope="module")
def collector():
    spec = importlib.util.spec_from_file_location("collect_evidence_fix_b", COLLECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def source() -> str:
    return COLLECTOR.read_text(encoding="utf-8")


def _write_sidecar(auto: Path, day: str, p95_by_hour: dict, threshold=2.5) -> None:
    auto.mkdir(parents=True, exist_ok=True)
    blob = {
        "date": day,
        "rest_latency": {"p95_by_hour": p95_by_hour, "p95_warn_threshold": threshold},
    }
    (auto / f"{day}_지표.json").write_text(
        json.dumps(blob, ensure_ascii=False), encoding="utf-8", newline="\n"
    )


def _slow(*hours, endpoint="inquire-price", value=3.6) -> dict:
    return {str(h): {endpoint: value} for h in hours}


# ---------------------------------------------------------------- 세는 것
def test_the_streak_walks_past_the_previous_day(collector, tmp_path):
    """이 항목의 요점 — 나흘 내리 성립하면 「이틀」이 아니라 **4거래일째**다."""
    auto = tmp_path / "auto"
    # 09-11(끊는 날)은 09-10과 겹치는 시간대가 없다 — 여기서 연속이 시작된다.
    _write_sidecar(auto, "2026-09-10", _slow(9))
    _write_sidecar(auto, "2026-09-11", _slow(13))
    for day in ("2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17"):
        _write_sidecar(auto, day, _slow(13))

    days, since, stop = collector.consecutive_latency_days(auto, date(2026, 9, 17))

    assert days == 4
    assert since == date(2026, 9, 14)
    assert stop == "끊김"


def test_today_only_is_zero_days(collector, tmp_path):
    """「오늘만 나쁨」은 0거래일째다 — 규칙은 이틀 연속에만 발동을 묻는다."""
    auto = tmp_path / "auto"
    _write_sidecar(auto, "2026-09-16", _slow(9))
    _write_sidecar(auto, "2026-09-17", _slow(15))

    days, since, stop = collector.consecutive_latency_days(auto, date(2026, 9, 17))
    assert (days, since, stop) == (0, None, "끊김")


def test_non_trading_days_are_skipped_not_counted(collector, tmp_path):
    """주말·휴장일은 걸음이 건너뛴다 — 거래일로 세면 연속이 거짓으로 끊긴다(08-17 사고)."""
    auto = tmp_path / "auto"
    _write_sidecar(auto, "2026-09-10", _slow(13))
    _write_sidecar(auto, "2026-09-11", _slow(13))
    # 09-12(금)·09-13(토) 사이드카 없음 — 09-14(월)의 직전 거래일은 09-11이다.
    _write_sidecar(auto, "2026-09-14", _slow(13))

    days, since, stop = collector.consecutive_latency_days(auto, date(2026, 9, 14))
    # 09-14(prev 09-11)와 09-11(prev 09-10) 둘 다 성립한다. 09-10은 **대조할 직전 거래일이
    # 없어** 성립 여부를 못 따지므로 세지 않는다 — 그래서 사유가 「계측없음」이고, 이 수는
    # 「2일째가 전부」가 아니라 **「최소 2일째」**다(규약 C).
    assert days == 2
    assert since == date(2026, 9, 11)
    assert stop == "계측없음"


# ---------------------------------------------------------------- 규약 C
def test_missing_measurement_is_unknown_not_a_break(collector, tmp_path):
    """규약 C — 사이드카가 끊긴 지점은 **「최소 N일째」**이지 「연속이 끊겼다」가 아니다."""
    auto = tmp_path / "auto"
    _write_sidecar(auto, "2026-09-16", _slow(13))
    _write_sidecar(auto, "2026-09-17", _slow(13))

    days, since, stop = collector.consecutive_latency_days(auto, date(2026, 9, 17))
    assert days == 1
    assert since == date(2026, 9, 17)
    assert stop == "계측없음"          # ⚠ 「끊김」이었다면 「1일째가 전부」라는 뜻이 된다


def test_an_empty_sidecar_is_not_a_trading_day(collector, tmp_path):
    """`p95_by_hour`가 빈 날(08-17 대체공휴일 형태)을 거래일로 세지 않는다."""
    auto = tmp_path / "auto"
    _write_sidecar(auto, "2026-08-13", _slow(13))
    _write_sidecar(auto, "2026-08-14", _slow(13))
    _write_sidecar(auto, "2026-08-17", {})          # 대체공휴일 — `p95_by_hour`가 비었다
    _write_sidecar(auto, "2026-08-18", _slow(13))

    days, since, _stop = collector.consecutive_latency_days(auto, date(2026, 8, 18))
    # 08-18의 직전 거래일은 08-17이 아니라 08-14다. 빈 사이드카를 거래일로 셌다면 겹침이
    # 0이 되어 연속이 **여기서 거짓으로 끊겼을** 것이다 — 08-17이 정확히 그 사고였다.
    assert days == 2
    assert since == date(2026, 8, 14)


# ---------------------------------------------------------------- 축과 임계
def test_the_axis_is_every_endpoint_not_just_price(collector, tmp_path):
    """§5-1의 축은 **전 엔드포인트**다 — 08-25 성립 6구간에 `inquire-balance`가 있었다.

    §8-4(`preemptive_uncovered_streak`)는 `inquire-price`만 센다. 두 축이 다르다는 것이
    이 항목의 전제이고, 여기서 흐려지면 두 절이 서로 다른 수를 내면서 둘 다 맞다고 한다.
    """
    auto = tmp_path / "auto"
    balance = {"15": {"inquire-balance": 3.6}}
    _write_sidecar(auto, "2026-09-16", balance)
    _write_sidecar(auto, "2026-09-17", balance)

    days, _since, _stop = collector.consecutive_latency_days(auto, date(2026, 9, 17))
    assert days == 1                   # 전 엔드포인트 축이라 센다

    latency = collector._sidecar_latency(auto, date(2026, 9, 17))
    assert collector._all_endpoint_breach_hours(latency) == {("inquire-balance", 15)}
    assert collector._price_breach_hours(latency) == set()   # §8-4 축은 안 센다


def test_threshold_comes_from_that_days_sidecar(collector, tmp_path):
    """그날 실제로 걸려 있던 임계를 쓴다 — 오늘 임계로 과거를 다시 재지 않는다."""
    auto = tmp_path / "auto"
    _write_sidecar(auto, "2026-09-16", _slow(13, value=3.0), threshold=5.0)
    _write_sidecar(auto, "2026-09-17", _slow(13, value=3.0), threshold=2.5)

    days, _since, stop = collector.consecutive_latency_days(auto, date(2026, 9, 17))
    # 09-16은 그날 임계(5.0) 밑이라 안 걸렸다 — 겹침이 없으므로 0일째다.
    assert (days, stop) == (0, "끊김")


# ---------------------------------------------------------------- 판정 무변경
def test_the_rule_and_its_threshold_are_untouched(collector):
    """⛔ 바꾼 것은 「무엇을 재는가」이지 「얼마부터 위험한가」가 아니다."""
    assert collector.P95_WARN_THRESHOLD_SECONDS == 2.5
    assert collector.P95_TWO_DAY_RULE_ID == "2026-08-04-p5"


def test_the_existing_alarm_wording_is_byte_identical(source):
    """기존 🔔 줄은 부분문자열 파서가 세는 문구다 — 새 줄은 **뒤에만 덧붙는다.**"""
    assert "사전 대응 규칙 발동 조건 성립 — 규칙 `{P95_TWO_DAY_RULE_ID}`" in source
    assert "이틀 연속(직전 거래일 {prev51_day}) 같은 구간 **{len(both)}개** / " in source
    # 발동 창 문구(08-26 P1-3)도 그대로여야 한다 — 내일 아침 회차가 그 줄을 받는다.
    assert "발동 창은 「다음 거래일 장전(07:30 기동 전)」이다" in source


def test_this_axis_declares_no_threshold(source):
    """⛔ 「N일 넘으면 발동」을 긋지 않는다 — §8-4와 같은 규약이다."""
    start = source.index("def consecutive_latency_days")
    body = source[start:source.index("\ndef ", start + 10)]
    assert "임계" not in body.replace("그날의 임계", "").replace("p95 임계", "")
    # 새 출력 줄이 그 규약을 사람에게도 말한다.
    assert "⛔ **여기에 임계는 없다**" in source
