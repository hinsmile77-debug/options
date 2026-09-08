"""증거 수집기 §8-4 — 사전 대응 규칙이 **아직 안 덮은 시간대가 며칠째인가** (2026-09-08 Fix D).

09-08에 같은 결정(위클리 폴링 09시 포함 여부)이 사람 개입 없이 세 번째로 지나갔고 그날
오후에 28분 절벽이 났다. 그런데 그 「세 번째」는 **보고서 산문의 수**였다 — 어느 산출물에도
남지 않아 다음 회차가 이어 세지 못했고, 사이드카로 재집계하면 다른 수가 나온다(실제로 5다).

이 파일이 지키는 것 셋:

    1. **재현된다** — 같은 사이드카에서 매번 같은 수가 나온다.
    2. **규약 C** — 「못 읽었다」가 「0일째」로 접히지 않는다. 비거래일 사이드카도 마찬가지다.
    3. ⛔ **판정하지 않는다** — 이 절에는 임계도 경보선도 없다. §8-2·§8-3과 같은 규약이고,
       그것을 소스 수준에서 못 박는 테스트가 아래 마지막 두 건이다.
"""

from __future__ import annotations

import importlib.util
import json
import re
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COLLECTOR = PROJECT_ROOT / "docs" / "동작점검" / "tools" / "collect_evidence.py"


@pytest.fixture(scope="module")
def collector():
    spec = importlib.util.spec_from_file_location("collect_evidence_fix_d", COLLECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- 픽스처 헬퍼
def _write_lever(root: Path, literal: str) -> None:
    """`mahdi/main.py`에 혼잡 시간대 레버 줄을 심는다."""
    main = root / "mahdi" / "main.py"
    main.parent.mkdir(parents=True, exist_ok=True)
    main.write_text(
        "# 주석 줄 — OPTION_CHAIN_SLOW_SERIES_CONGESTED_HOURS = {23: 4}\n"
        f"OPTION_CHAIN_SLOW_SERIES_CONGESTED_HOURS: dict[int, int] = {literal}\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_sidecar(auto: Path, day: str, p95_by_hour: dict, threshold=2.5) -> None:
    auto.mkdir(parents=True, exist_ok=True)
    blob = {
        "date": day,
        "rest_latency": {"p95_by_hour": p95_by_hour, "p95_warn_threshold": threshold},
    }
    (auto / f"{day}_지표.json").write_text(
        json.dumps(blob, ensure_ascii=False), encoding="utf-8", newline="\n"
    )


def _slow(*hours):
    """`inquire-price`가 임계를 넘은 시간대만 담은 `p95_by_hour`."""
    return {str(h): {"inquire-price": 3.0} for h in hours}


# ---------------------------------------------------------------- 레버 읽기
def test_lever_hours_are_parsed_from_source(collector, tmp_path):
    _write_lever(tmp_path, "{10: 4, 11: 4, 12: 4, 13: 4, 14: 4}")
    assert collector.congested_lever_hours(tmp_path) == {10, 11, 12, 13, 14}


def test_commented_out_lever_line_is_not_read(collector, tmp_path):
    """주석 줄의 `{23: 4}`를 읽으면 23시가 「덮인 것」이 된다 — §7의 레버 표와 같은 규칙이다."""
    _write_lever(tmp_path, "{10: 4}")
    assert 23 not in collector.congested_lever_hours(tmp_path)


def test_empty_lever_is_an_empty_set_not_none(collector, tmp_path):
    """규약 C — 「등록된 시간대가 없다」와 「못 읽었다」는 다른 값이다."""
    _write_lever(tmp_path, "{}")
    assert collector.congested_lever_hours(tmp_path) == set()


def test_unreadable_lever_is_none_not_empty(collector, tmp_path):
    """`mahdi/main.py`가 없으면 `None`이다. 빈 집합으로 접으면 **전 시간대가 거짓 미덮임**이 된다."""
    assert collector.congested_lever_hours(tmp_path) is None

    (tmp_path / "mahdi").mkdir()
    (tmp_path / "mahdi" / "main.py").write_text("X = 1\n", encoding="utf-8", newline="\n")
    assert collector.congested_lever_hours(tmp_path) is None


# ---------------------------------------------------------------- 사이드카 읽기
def test_sidecar_without_latency_is_not_a_trading_day(collector, tmp_path):
    """`p95_by_hour`가 빈 사이드카는 **거래일로 세지 않는다**(08-17 대체공휴일이 그 형태였다)."""
    auto = tmp_path / "auto"
    _write_sidecar(auto, "2026-08-17", {})
    assert collector._sidecar_latency(auto, date(2026, 8, 17)) is None


def test_previous_latency_day_skips_the_empty_sidecar(collector, tmp_path):
    """08-18의 직전 거래일은 08-17이 아니라 08-14다 — §5의 🔔이 고른 날과 같아야 한다."""
    auto = tmp_path / "auto"
    _write_sidecar(auto, "2026-08-14", _slow(10))
    _write_sidecar(auto, "2026-08-17", {})
    assert collector._previous_latency_day(auto, date(2026, 8, 18)) == date(2026, 8, 14)


def test_threshold_comes_from_that_days_sidecar(collector, tmp_path):
    """그날 실제로 걸려 있던 임계를 쓴다(`two_day_p95_overlap`과 같은 원칙)."""
    auto = tmp_path / "auto"
    _write_sidecar(auto, "2026-09-08", {"9": {"inquire-price": 3.0}}, threshold=5.0)
    latency = collector._sidecar_latency(auto, date(2026, 9, 8))
    assert collector._price_breach_hours(latency) == set()


def test_only_inquire_price_is_counted(collector, tmp_path):
    """조치가 **위클리 옵션 조회 주기**라, 잔고만 걸린 시간대는 이 레버로 덮을 대상이 아니다."""
    auto = tmp_path / "auto"
    _write_sidecar(auto, "2026-09-08", {"15": {"inquire-balance": 3.6}})
    latency = collector._sidecar_latency(auto, date(2026, 9, 8))
    assert collector._price_breach_hours(latency) == set()


# ---------------------------------------------------------------- 연속일
def test_streak_counts_consecutive_uncovered_days_and_stops(collector, tmp_path):
    """09-02~09-08 실측과 같은 모양 — 연속이 끊긴 날 하나까지만 담고 멈춘다."""
    auto = tmp_path / "auto"
    _write_lever(tmp_path, "{10: 4, 11: 4, 12: 4, 13: 4, 14: 4}")
    # 08-31은 10~14시만 넘었다 — 그래서 09-01은 **자기 날은 나빴어도 이틀 연속이 안 서고**,
    # 연속은 09-02부터 시작한다. 실측(09-01 미덮임 없음 · 09-02부터 닷새)이 정확히 이 모양이다.
    _write_sidecar(auto, "2026-08-31", _slow(10, 11, 12, 13, 14))
    for day in ("2026-09-01", "2026-09-02", "2026-09-03",
                "2026-09-04", "2026-09-07", "2026-09-08"):
        _write_sidecar(auto, day, _slow(9, 10, 11, 12, 13, 14, 15))

    covered, rows = collector.preemptive_uncovered_history(auto, tmp_path, date(2026, 9, 8))

    assert covered == {10, 11, 12, 13, 14}
    assert collector.preemptive_uncovered_streak(rows) == 5
    assert rows[0] == (date(2026, 9, 8), [9, 15])
    # 끊긴 날이 목록 끝에 남아 「언제부터인가」를 말해 준다.
    assert rows[-1] == (date(2026, 9, 1), [])


def test_streak_is_zero_when_the_lever_covers_everything(collector, tmp_path):
    """사람이 그 시간대를 레버에 넣으면 **다음 거래일에 저절로 0이 된다.**"""
    auto = tmp_path / "auto"
    _write_lever(tmp_path, "{9: 4, 10: 4, 11: 4, 12: 4, 13: 4, 14: 4, 15: 4}")
    for day in ("2026-09-07", "2026-09-08"):
        _write_sidecar(auto, day, _slow(9, 10, 11, 12, 13, 14, 15))

    _covered, rows = collector.preemptive_uncovered_history(auto, tmp_path, date(2026, 9, 8))
    assert collector.preemptive_uncovered_streak(rows) == 0


def test_a_day_is_uncovered_only_when_both_days_breached(collector, tmp_path):
    """규칙은 **이틀 연속**에만 발동을 묻는다 — 「오늘만 나쁨」은 안 센다."""
    auto = tmp_path / "auto"
    _write_lever(tmp_path, "{10: 4}")
    _write_sidecar(auto, "2026-09-07", _slow(10))
    _write_sidecar(auto, "2026-09-08", _slow(10, 15))

    _covered, rows = collector.preemptive_uncovered_history(auto, tmp_path, date(2026, 9, 8))
    assert rows[0] == (date(2026, 9, 8), [])


def test_missing_previous_sidecar_is_unknown_not_zero(collector, tmp_path):
    """규약 C — 이틀 연속 판정을 못 하면 `None`이고, `streak`는 그 지점에서 멈춘다."""
    auto = tmp_path / "auto"
    _write_lever(tmp_path, "{10: 4}")
    _write_sidecar(auto, "2026-09-08", _slow(9, 10))

    _covered, rows = collector.preemptive_uncovered_history(auto, tmp_path, date(2026, 9, 8))
    assert rows == [(date(2026, 9, 8), None)]
    assert collector.preemptive_uncovered_streak(rows) == 0


def test_unreadable_lever_yields_unknown_rows(collector, tmp_path):
    """레버를 못 읽으면 안 덮인 시를 **셀 수 없다** — 빈 목록이 아니라 `None`이다."""
    auto = tmp_path / "auto"
    _write_sidecar(auto, "2026-09-07", _slow(9))
    _write_sidecar(auto, "2026-09-08", _slow(9))

    covered, rows = collector.preemptive_uncovered_history(auto, tmp_path, date(2026, 9, 8))
    assert covered is None
    assert rows[0] == (date(2026, 9, 8), None)


def test_history_is_bounded(collector, tmp_path):
    """연속이 아무리 길어도 거슬러 오르는 날 수에 상한이 있다."""
    auto = tmp_path / "auto"
    _write_lever(tmp_path, "{}")
    day = date(2026, 9, 8)
    for back in range(0, collector.PREEMPTIVE_STREAK_MAX_DAYS + 10):
        from datetime import timedelta

        _write_sidecar(auto, (day - timedelta(days=back)).isoformat(), _slow(10))

    _covered, rows = collector.preemptive_uncovered_history(auto, tmp_path, day)
    assert len(rows) <= collector.PREEMPTIVE_STREAK_MAX_DAYS


# ---------------------------------------------------------------- ⛔ 판정 무변경
def _section_source() -> str:
    """§8-4 렌더 블록만 떼어 온다."""
    text = COLLECTOR.read_text(encoding="utf-8")
    start = text.index('A("## 8-4.')
    end = text.index("# ---- 9. 산출물 ----", start)
    return text[start:end]


def test_section_raises_a_flag_only_when_it_could_not_measure(collector):
    """⛔ **판정하지 않는다.** 이 절이 적신호를 내는 경우는 **「못 읽었다」 하나**여야 한다.

    §8-2·§8-3과 같은 규약이다 — 여기에 「N일 넘으면 경고」가 생기면 표본이 얕은 채로 임계가
    서고, 그것이 08-05 스팟 괴리율에서 한 번 뒤집힌 형태다.
    """
    block = _section_source()
    assert block.count("flags.append(") == 1
    guard = block.index("if covered_hours is None:")
    branch_end = block.index("elif not uncovered_rows:")
    assert guard < block.index("flags.append(") < branch_end


def test_section_declares_no_threshold(collector):
    """⛔ **임계를 만들지 않는다** — 이 축에는 경보선이 없다."""
    block = _section_source()
    assert not re.search(r"(ALERT|WARN|THRESHOLD)", block)
    for name in dir(collector):
        if name.startswith("PREEMPTIVE"):
            assert not re.search(r"(ALERT|WARN|THRESHOLD)", name)
