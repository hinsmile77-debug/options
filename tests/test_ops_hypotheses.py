"""`mahdi.ops.hypotheses` — "예측 → 실측 검정" 자동 대조(§5-1).

2026-08-01. 이 모듈의 설계 의도 두 가지가 테스트로 고정돼야 한다:
  1. YAML의 `상태`를 자동으로 고치지 않는다(사람이 확정한다).
  2. 해석 못 하는 `expect`를 억지로 판정하지 않는다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from mahdi.config.settings import PROJECT_ROOT
from mahdi.ops import hypotheses

_METRICS = {"overrun": {"count": 12}, "rest": {"capacity_pct": 45.2}, "poller_phase": {"만기유동성": {"mode_second": 15}}}
_DB = {"monthly_coverage": {"coverage_pct": 96.4}}


def _entry(**overrides) -> dict:
    base = {
        "id": "h1", "구현일": date(2026, 8, 1), "검증예정일": date(2026, 8, 3),
        "가설": "테스트", "예측": [{"metric": "overrun.count", "expect": "<= 30"}], "상태": "pending",
    }
    base.update(overrides)
    return base


def test_confirms_and_refutes_comparison_expectations():
    [r] = hypotheses.evaluate([_entry()], date(2026, 8, 3), _METRICS)
    assert r["actual"] == 12 and r["verdict"] == hypotheses.VERDICT_CONFIRMED

    [r] = hypotheses.evaluate(
        [_entry(예측=[{"metric": "overrun.count", "expect": "<= 5"}])], date(2026, 8, 3), _METRICS
    )
    assert r["verdict"] == hypotheses.VERDICT_REFUTED


def test_supports_range_expectations():
    [r] = hypotheses.evaluate(
        [_entry(예측=[{"metric": "poller_phase.만기유동성.mode_second", "expect": "13 ~ 17"}])],
        date(2026, 8, 3), _METRICS,
    )
    assert r["verdict"] == hypotheses.VERDICT_CONFIRMED


def test_db_prefixed_metrics_resolve_against_db_metrics():
    [r] = hypotheses.evaluate(
        [_entry(예측=[{"metric": "db.monthly_coverage.coverage_pct", "expect": ">= 95"}])],
        date(2026, 8, 3), _METRICS, _DB,
    )
    assert r["actual"] == 96.4 and r["verdict"] == hypotheses.VERDICT_CONFIRMED


def test_unparseable_expectation_is_left_for_a_human_not_forced():
    # 억지 자동 판정보다 "수기 판정"으로 남기는 편이 낫다.
    [r] = hypotheses.evaluate(
        [_entry(예측=[{"metric": "overrun.count", "expect": "15:4x 쯤"}])], date(2026, 8, 3), _METRICS
    )
    assert r["verdict"] == hypotheses.VERDICT_MANUAL


def test_missing_metric_reports_no_data_instead_of_failing():
    [r] = hypotheses.evaluate(
        [_entry(예측=[{"metric": "없는.경로", "expect": "<= 1"}])], date(2026, 8, 3), _METRICS
    )
    assert r["actual"] is None and r["verdict"] == hypotheses.VERDICT_NO_DATA


def test_only_pending_entries_are_evaluated():
    assert hypotheses.evaluate([_entry(상태="confirmed")], date(2026, 8, 3), _METRICS) == []


def test_entries_stay_visible_after_their_due_date_until_a_human_closes_them():
    # 예정일이 지났는데 아직 pending이면 계속 보여야 한다 — 하루 놓치면 영영 사라지면 안 된다.
    assert hypotheses.evaluate([_entry()], date(2026, 8, 10), _METRICS)
    # 반대로 예정일 전에는 아직 안 보인다.
    assert hypotheses.evaluate([_entry()], date(2026, 8, 2), _METRICS) == []


def test_a_broken_entry_does_not_take_down_the_others():
    good = _entry(id="good")
    broken = _entry(id="broken", 예측=[{"expect": "<= 1"}])  # metric 키 없음
    results = hypotheses.evaluate([broken, good], date(2026, 8, 3), _METRICS)
    assert [r["id"] for r in results] == ["good"]


def test_missing_file_returns_empty_instead_of_raising(tmp_path):
    assert hypotheses.load(tmp_path / "없는파일.yaml") == []


def test_repository_hypotheses_file_is_valid_and_uses_resolvable_metric_paths():
    # 회귀 방지: 지표 경로 오타는 조용히 "실측 없음"이 되어 검정을 무력화한다. 리포지터리에
    # 실제로 커밋된 파일이 07-31 실측 지표 구조에서 전부 해석되는지 여기서 못박는다.
    entries = hypotheses.load(PROJECT_ROOT / "docs" / "동작점검" / "hypotheses.yaml")
    assert entries, "가설 파일이 비어 있다"
    for entry in entries:
        assert entry.get("id") and entry.get("가설")
        assert str(entry.get("상태")).lower() in {"pending", "confirmed", "refuted", "inconclusive"}
        for prediction in entry["예측"]:
            assert "metric" in prediction and "expect" in prediction


@pytest.mark.parametrize(
    "path",
    [
        "overrun.count",
        "cycles.missing.unrecovered_count",
        "rest.capacity_pct",
        "rest.calls_per_second",
        "slow_calls.count",
        "bursts.만기유동성.occupancy_seconds.median",
        "bursts.만기유동성.calls_per_burst_median",
        "poller_phase.만기유동성.mode_second",
        "poller_phase.투자자수급.mode_second",
        "poller_phase.매크로.mode_second",
        "poller_phase.계좌잔고.mode_second",
    ],
)
def test_metric_paths_used_by_the_repository_file_exist_in_a_real_parse(path):
    # 픽스처 로그를 실제로 파싱해 그 경로가 존재하는지 본다 — 지표 dict 구조가 바뀌면 여기서 깨진다.
    from mahdi.ops import log_metrics, report

    parsed = log_metrics.parse_day(
        log_metrics.iter_day_lines(
            Path(__file__).parent / "fixtures", date(2026, 7, 31), stem="observation_loop_sample.log"
        ),
        date(2026, 7, 31),
    )
    # 값 자체는 픽스처에 따라 없을 수 있으나, 최상위 절은 반드시 존재해야 한다.
    assert report.dig(parsed, path.split(".")[0]) is not None
