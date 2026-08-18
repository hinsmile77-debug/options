"""검증 캠페인 — 「아직 모른다」와 「틀렸다」를 자료구조가 가르는지 본다 (2026-08-18 신설).

이 파일이 지키는 것은 판정의 정확도가 아니다. **표본이 안 찼을 때 판정하지 않는가**, 그리고
**규약 F/G를 채널에도 적용하는가** 둘이다.
"""

from datetime import date
from pathlib import Path

import pytest

from mahdi.ops import campaign

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CAMPAIGN_PATH = PROJECT_ROOT / "docs" / "동작점검" / "validation_campaign.yaml"

_SAMPLE = "db.decisions.selected_instruments.enter_minutes"
_CLAIM = "db.decisions.selected_instruments.reason.no_strike_match"


def _channel(**overrides):
    base = {
        "id": "c1",
        "개시일": date(2026, 8, 18),
        "질문": "질문",
        "표본": {"metric": _SAMPLE, "min_samples": 100, "min_days": 3},
        "판정": [{"metric": _CLAIM, "rule": "비율 < 0.30", "역할": "주장"}],
        "상태": "open",
    }
    base.update(overrides)
    return base


def _day(day, enter=None, no_strike=None, present=True):
    """하루치 지표 json — `present=False`면 그 절 자체가 없다(그 지표가 아직 없던 날)."""
    if not present:
        return (day, {"date": day.isoformat(), "db": {}})
    selected = {}
    if enter is not None:
        selected["enter_minutes"] = enter
    if no_strike is not None:
        selected["reason"] = {"no_strike_match": no_strike}
    return (day, {"date": day.isoformat(), "db": {"decisions": {"selected_instruments": selected}}})


# --- 표본이 찰 때까지 판정하지 않는다 -----------------------------------------------------


def test_below_the_threshold_the_verdict_is_insufficient_not_fail():
    """08-18 실측 비율 0.805는 기준 0.30을 크게 넘지만, 표본이 안 찼으면 **불합격이 아니다.**"""
    days = [_day(date(2026, 8, 18), enter=205, no_strike=165)]
    sample = campaign.accumulate(_channel(), days)
    verdict = campaign.judge(_channel(), sample)

    assert verdict["verdict"] == campaign.VERDICT_INSUFFICIENT
    assert verdict["progress"] == "205/100 (1일)"  # 건수는 찼지만 일수가 안 찼다


def test_min_days_alone_can_hold_the_verdict():
    """단일일 쏠림 방지 — 하루에 표본이 다 차도 일수 하한을 못 채우면 판정하지 않는다."""
    days = [_day(date(2026, 8, 18), enter=5000, no_strike=100)]
    sample = campaign.accumulate(_channel(), days)

    assert campaign.judge(_channel(), sample)["verdict"] == campaign.VERDICT_INSUFFICIENT


def test_once_the_sample_is_full_the_rule_actually_judges():
    days = [
        _day(date(2026, 8, 18), enter=100, no_strike=10),
        _day(date(2026, 8, 19), enter=100, no_strike=10),
        _day(date(2026, 8, 20), enter=100, no_strike=10),
    ]
    sample = campaign.accumulate(_channel(), days)
    verdict = campaign.judge(_channel(), sample)

    assert verdict["verdict"] == campaign.VERDICT_PASS  # 30/300 = 0.10 < 0.30


def test_a_full_sample_that_misses_the_bar_is_a_real_fail():
    days = [_day(date(2026, 8, 18 + i), enter=100, no_strike=81) for i in range(3)]
    sample = campaign.accumulate(_channel(), days)
    verdict = campaign.judge(_channel(), sample)

    assert verdict["verdict"] == campaign.VERDICT_FAIL
    assert "0.810" in verdict["detail"]


# --- 규약 C — 없는 날은 0이 아니다 ---------------------------------------------------------


def test_days_without_the_metric_are_skipped_not_counted_as_zero():
    """0으로 채우면 「못 쟀다」와 「쟀는데 0이다」가 뭉개진다 — 채널보다 늦게 생긴 지표를
    안전하게 쓸 수 있는 근거이기도 하다."""
    days = [
        _day(date(2026, 8, 18), present=False),   # 그 절이 아직 없던 날
        _day(date(2026, 8, 19), enter=100, no_strike=10),
    ]
    sample = campaign.accumulate(_channel(), days)

    assert sample["samples"] == 100  # 없던 날이 0으로 안 섞였다
    assert sample["days"] == 1
    assert sample["days_in_range"] == 2


def test_days_before_the_start_date_never_count():
    days = [
        _day(date(2026, 8, 17), enter=999, no_strike=999),  # 개시 전
        _day(date(2026, 8, 18), enter=100, no_strike=10),
    ]
    sample = campaign.accumulate(_channel(), days)

    assert sample["samples"] == 100
    assert sample["days_in_range"] == 1


# --- 「아직 개시 전」과 「경로 오타」는 다른 사건이다 ------------------------------------------


def test_a_channel_that_has_not_started_is_insufficient_not_a_dead_path():
    """새로 등재한 채널이 「경로 없음」으로 찍히면 등재할 때마다 거짓 경보가 뜬다."""
    sample = campaign.accumulate(_channel(), [_day(date(2026, 8, 17), enter=100)])
    verdict = campaign.judge(_channel(), sample)

    assert verdict["verdict"] == campaign.VERDICT_INSUFFICIENT
    assert verdict["detail"] == "개시 전"


def test_a_typo_in_the_sample_path_is_reported_as_a_dead_path():
    channel = _channel(표본={"metric": "db.decisions.오타.x", "min_samples": 10, "min_days": 1})
    sample = campaign.accumulate(channel, [_day(date(2026, 8, 18), enter=100, no_strike=10)])
    verdict = campaign.judge(channel, sample)

    assert verdict["verdict"] == campaign.VERDICT_PATH_DEAD


# --- 규약 F/G의 캠페인판 --------------------------------------------------------------------


def test_a_claim_may_not_put_a_threshold_on_an_accumulated_count():
    """누적 건수에 임계를 걸면 그 지표는 «며칠 지났는가»를 재게 된다."""
    assert campaign.violates_ratio_claim_rule("주장", "< 100") is True
    assert campaign.violates_ratio_claim_rule("주장", "비율 < 0.3") is False
    assert campaign.violates_ratio_claim_rule("주장", "관측") is False
    # 참고·대가 역할은 막지 않는다.
    assert campaign.violates_ratio_claim_rule("참고", "< 100") is False


def test_a_market_state_dependent_metric_can_only_be_observed():
    """누적해도 «오늘 시장이 어땠는가»의 함수라는 성질은 사라지지 않는다(규약 G)."""
    from mahdi.ops.hypotheses import is_market_state_dependent

    metric = "db.decisions.member_count.dead_axis_mean"
    assert is_market_state_dependent(metric), "규약 G 목록이 바뀌었다 — 이 테스트의 전제부터 확인할 것"

    assert campaign.violates_market_state_rule("주장", metric, "비율 > 0.5") is True
    assert campaign.violates_market_state_rule("주장", metric, "비율 < 0.5") is True
    assert campaign.violates_market_state_rule("주장", metric, "관측") is False
    # 참고 역할은 막지 않는다 — 규약 G는 «주장»에만 건다.
    assert campaign.violates_market_state_rule("참고", metric, "비율 > 0.5") is False
    # 시장 상태와 무관한 지표는 임계를 걸 수 있다.
    assert campaign.violates_market_state_rule("주장", _CLAIM, "비율 < 0.3") is False


def test_schema_problems_are_collected_not_raised():
    problems = campaign.validate({"id": "", "상태": "이상한값", "표본": {}, "판정": []})

    assert any("id 없음" in p for p in problems)
    assert any("min_samples" in p for p in problems)
    assert any("판정 규칙 없음" in p for p in problems)


def test_a_channel_with_schema_problems_is_not_judged():
    broken = _channel(판정=[{"metric": _CLAIM, "rule": "< 100", "역할": "주장"}])
    (row,) = campaign.evaluate({"channels": [broken], "decisions": []}, [])

    assert row["verdict"] == "스키마 오류"
    assert row["problems"]


# --- 관측 전용 채널 -------------------------------------------------------------------------


def test_an_observe_only_channel_never_passes_or_fails():
    """임계를 일부러 안 건 채널을 합격/불합격으로 찍으면 규약 G를 어긴 것과 같아진다."""
    channel = _channel(판정=[{"metric": _CLAIM, "rule": "관측", "역할": "참고"}])
    days = [_day(date(2026, 8, 18 + i), enter=100, no_strike=99) for i in range(3)]
    sample = campaign.accumulate(channel, days)

    assert campaign.judge(channel, sample)["verdict"] == campaign.VERDICT_OBSERVE


# --- 선행 채널 게이트 -----------------------------------------------------------------------


def test_a_channel_waits_for_its_prerequisite():
    channel = _channel(선행="다른채널")
    days = [_day(date(2026, 8, 18 + i), enter=100, no_strike=10) for i in range(3)]
    sample = campaign.accumulate(channel, days)

    assert campaign.judge(channel, sample)["verdict"] == campaign.VERDICT_BLOCKED
    assert campaign.judge(channel, sample, frozenset({"다른채널"}))["verdict"] == campaign.VERDICT_PASS


# --- 판정과 결정은 별개다 -------------------------------------------------------------------


def test_a_confirmed_decision_rides_along_with_whatever_the_verdict_is():
    """판정은 매일 재계산되지만 결정은 사람이 확정한 것이라 남는다 — 이 분리가 제도의 절반이다."""
    campaign_cfg = {
        "channels": [_channel()],
        "decisions": [{"id": "d1", "channel": "c1", "date": "2026-09-01", "decision": "밴드 유지"}],
    }
    (row,) = campaign.evaluate(campaign_cfg, [_day(date(2026, 8, 18), enter=10, no_strike=9)])

    assert row["decision"] == "밴드 유지"
    assert row["verdict"] == campaign.VERDICT_INSUFFICIENT  # 결정이 있어도 판정은 판정이다


def test_closed_channels_drop_out_of_the_report():
    (rows) = campaign.evaluate({"channels": [_channel(상태="closed")], "decisions": []}, [])
    assert rows == []


# --- 저장소의 실제 파일 ---------------------------------------------------------------------


def test_the_repository_campaign_file_parses_and_every_channel_validates():
    cfg = campaign.load(_CAMPAIGN_PATH)

    assert cfg["channels"], "채널이 하나도 없다"
    for channel in cfg["channels"]:
        assert campaign.validate(channel) == [], f"{channel.get('id')}: 스키마 위반"


def test_phase_1_keeps_the_channel_count_small():
    """미륵이는 67개를 운영하는데 다수가 몇 달째 표본 미달이다 — 표본 기아 경보 전에는 5개 이하."""
    cfg = campaign.load(_CAMPAIGN_PATH)
    open_channels = [c for c in cfg["channels"] if c.get("상태") != campaign.STATUS_CLOSED]

    assert len(open_channels) <= 5


def test_every_repository_metric_path_starts_at_a_real_report_section():
    """지표 경로의 **절 이름**이 자동 집계에 실제로 있는지 본다 — 없으면 그 채널은 영원히
    검정 불가가 된다(`test_ops_hypotheses`가 가설에 대해 하는 검사와 같은 것)."""
    from tests.test_ops_hypotheses import _DB_METRIC_ROOTS, _METRIC_ROOTS

    wrong = []
    for channel in campaign.load(_CAMPAIGN_PATH)["channels"]:
        paths = [str((channel.get("표본") or {}).get("metric") or "")]
        paths += [str(j.get("metric") or "") for j in (channel.get("판정") or [])]
        for metric in paths:
            if metric.startswith("db."):
                root, roots = metric[3:].split(".")[0], _DB_METRIC_ROOTS
            else:
                root, roots = metric.split(".")[0], _METRIC_ROOTS
            if root not in roots:
                wrong.append((channel.get("id"), metric))
    assert not wrong, f"자동 집계에 없는 절에서 시작하는 지표 경로: {wrong}"


@pytest.mark.parametrize("rule,kind", [
    ("관측", "관측"), ("비율 < 0.3", "비율"), ("비율 >= 0.9", "비율"),
    ("< 100", ""), ("", ""), (None, ""),
])
def test_the_rule_language_is_exactly_two_forms_in_phase_1(rule, kind):
    assert campaign.rule_kind(rule) == kind
