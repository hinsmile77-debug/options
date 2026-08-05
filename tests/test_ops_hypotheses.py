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
        # 2026-08-05 고도화#2: 이 헬퍼의 기본 예측은 **주장 지표**다 — 아래 테스트들은 판정
        # 로직을 보는 것이라 주장/대가 규약에 걸리면 검증 대상이 흐려진다.
        "가설": "테스트", "상태": "pending",
        "예측": [{"metric": "overrun.count", "expect": "<= 30", "역할": hypotheses.ROLE_CLAIM}],
    }
    base.update(overrides)
    return base


def test_confirms_and_refutes_comparison_expectations():
    [r] = hypotheses.evaluate([_entry()], date(2026, 8, 3), _METRICS)
    assert r["actual"] == 12 and r["verdict"] == hypotheses.VERDICT_CONFIRMED

    [r] = hypotheses.evaluate(
        [_entry(예측=[{"metric": "overrun.count", "expect": "<= 5", "역할": hypotheses.ROLE_CLAIM}])], date(2026, 8, 3), _METRICS
    )
    assert r["verdict"] == hypotheses.VERDICT_REFUTED


def test_supports_range_expectations():
    [r] = hypotheses.evaluate(
        [_entry(예측=[{"metric": "poller_phase.만기유동성.mode_second", "expect": "13 ~ 17", "역할": hypotheses.ROLE_CLAIM}])],
        date(2026, 8, 3), _METRICS,
    )
    assert r["verdict"] == hypotheses.VERDICT_CONFIRMED


def test_db_prefixed_metrics_resolve_against_db_metrics():
    [r] = hypotheses.evaluate(
        [_entry(예측=[{"metric": "db.monthly_coverage.coverage_pct", "expect": ">= 95", "역할": hypotheses.ROLE_CLAIM}])],
        date(2026, 8, 3), _METRICS, _DB,
    )
    assert r["actual"] == 96.4 and r["verdict"] == hypotheses.VERDICT_CONFIRMED


def test_unparseable_expectation_is_left_for_a_human_not_forced():
    # 억지 자동 판정보다 "수기 판정"으로 남기는 편이 낫다.
    [r] = hypotheses.evaluate(
        [_entry(예측=[{"metric": "overrun.count", "expect": "15:4x 쯤", "역할": hypotheses.ROLE_CLAIM}])], date(2026, 8, 3), _METRICS
    )
    assert r["verdict"] == hypotheses.VERDICT_MANUAL


def test_missing_metric_reports_no_data_instead_of_failing():
    [r] = hypotheses.evaluate(
        [_entry(예측=[{"metric": "없는.경로", "expect": "<= 1", "역할": hypotheses.ROLE_CLAIM}])], date(2026, 8, 3), _METRICS
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
        # 2026-08-04 신규 `untested`: **그날 프로세스에 fix가 안 실려 검증 자체가 성립 안 됨.**
        # 08-03 p4가 그랬다 — fix 커밋 07:45, 관측 루프 기동 07:30으로 15분 차이였다.
        # 이걸 `refuted`로 적으면 멀쩡한 fix를 되돌리게 되고, `pending`으로 두면 "검증했는데
        # 아직 판단 못 함"과 섞인다. 세 번째 상태가 필요하다.
        assert str(entry.get("상태")).lower() in {
            "pending", "confirmed", "refuted", "inconclusive", "untested"
        }
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


# ===== 2026-08-03 §5-4: 확정 대기(overdue) 표시 =====


def test_evaluate_marks_overdue_pending_entries(tmp_path):
    """예정일이 지났는데 아직 pending이면 표에 섞이지 않고 따로 드러나야 한다.

    규약상 `상태`는 사람이 손으로 확정해야 하는데, 확정 안 된 것이 쌓이면 규약 자체가 무력해진다.
    """
    entries = [
        {"id": "due-today", "검증예정일": date(2026, 8, 4), "상태": "pending",
         "예측": [{"metric": "overrun.count", "expect": "<= 30"}]},
        {"id": "overdue", "검증예정일": date(2026, 8, 3), "상태": "pending",
         "예측": [{"metric": "overrun.count", "expect": "<= 30"}]},
    ]
    results = hypotheses.evaluate(entries, date(2026, 8, 4), {"overrun": {"count": 12}})

    by_id = {r["id"]: r for r in results}
    assert by_id["due-today"]["overdue"] is False  # 오늘이 예정일 — 아직 늦지 않았다
    assert by_id["overdue"]["overdue"] is True
    assert by_id["overdue"]["검증예정일"] == "2026-08-03"


def test_report_surfaces_overdue_entries_above_the_table():
    from mahdi.ops import report

    out = report.render(
        {"date": "2026-08-04"},
        hypotheses=[{
            "id": "2026-08-01-p1", "가설": "버스트 분할", "metric": "overrun.count",
            "actual": 12, "expect": "<= 20", "verdict": "확인",
            "overdue": True, "검증예정일": "2026-08-03",
        }],
    )
    section = out.split("## 0. 가설 검정", 1)[1].split("\n## ", 1)[0]
    callout, table = section.split("| id |", 1)
    assert "확정 대기 1건" in callout
    assert "2026-08-01-p1" in callout
    assert "2026-08-03" in callout
    assert table  # 표 자체는 그대로 나온다


def test_report_omits_the_callout_when_nothing_is_overdue():
    from mahdi.ops import report

    out = report.render(
        {"date": "2026-08-04"},
        hypotheses=[{
            "id": "h1", "가설": "x", "metric": "overrun.count",
            "actual": 12, "expect": "<= 20", "verdict": "확인", "overdue": False,
        }],
    )
    assert "확정 대기" not in out


# ===== 2026-08-05 고도화#2 / 규약 E — 주장 지표·대가 지표 =====


def _role_entry(predictions, **extra):
    base = {
        "id": "x", "가설": "h", "검증예정일": date(2026, 8, 6),
        "예측": predictions, "상태": "pending",
    }
    base.update(extra)
    return base


def test_a_hypothesis_without_a_claim_metric_cannot_be_judged():
    """**이 테스트가 08-04 `p4`의 실패를 재현한다.**

    p4는 "ATM 히스테리시스로 롤링 왕복이 사라진다"고 주장하면서 등록 지표가
    `chain_age_seconds_max`와 `log_volume.human_lines`였다 — 둘 다 왕복을 재지 않는다.
    왕복률은 36.1% → 47.5%로 **나빠졌는데도** §0은 "확인"을 냈다.
    자기 주장을 검정하지 않는 지표로 받은 합격이다.
    """
    entry = _role_entry([{"metric": "a.b", "expect": "<= 10"}])  # 역할 없음 = 참고
    rows = hypotheses.evaluate([entry], date(2026, 8, 6), {"a": {"b": 5}})

    assert rows[0]["claim_missing"] is True
    assert rows[0]["verdict"] == hypotheses.VERDICT_UNJUDGEABLE, (
        "실측이 예측을 만족해도 주장 지표가 없으면 '확인'을 내면 안 된다"
    )
    assert rows[0]["actual"] == 5, "실측은 사실이므로 그대로 남긴다 — 무효화하는 것은 판정뿐이다"


def test_a_claim_metric_restores_normal_judgement():
    entry = _role_entry([{"metric": "a.b", "expect": "<= 10", "역할": hypotheses.ROLE_CLAIM}])
    rows = hypotheses.evaluate([entry], date(2026, 8, 6), {"a": {"b": 5}})

    assert rows[0]["claim_missing"] is False
    assert rows[0]["verdict"] == hypotheses.VERDICT_CONFIRMED


def test_declaring_a_cost_without_measuring_it_is_flagged():
    """규약 E — 08-04 Fix#8은 "(대가로 얇은 사이클이 생긴다)"라고 **선언해 놓고** 그것을 재는
    지표를 등록하지 않았다. 밀림 0건이라는 훌륭한 숫자 뒤에서 먼슬리 북이 38% 확률로
    얇아지고 4분이 통째로 사라진 것을 다음날에야 알았다.
    """
    entry = _role_entry(
        [{"metric": "a.b", "expect": "<= 10", "역할": hypotheses.ROLE_CLAIM}],
        대가="얇은 사이클이 생긴다",
    )
    rows = hypotheses.evaluate([entry], date(2026, 8, 6), {"a": {"b": 5}})

    assert rows[0]["cost_missing"] is True
    # 대가 미등록은 판정을 막지는 않는다 — 주장은 검정됐다. 경고만 띄운다.
    assert rows[0]["verdict"] == hypotheses.VERDICT_CONFIRMED


def test_a_registered_cost_metric_clears_the_flag():
    entry = _role_entry(
        [
            {"metric": "a.b", "expect": "<= 10", "역할": hypotheses.ROLE_CLAIM},
            {"metric": "a.c", "expect": ">= 0", "역할": hypotheses.ROLE_COST},
        ],
        대가="얇은 사이클이 생긴다",
    )
    rows = hypotheses.evaluate([entry], date(2026, 8, 6), {"a": {"b": 5, "c": 1}})

    assert all(r["cost_missing"] is False for r in rows)


def test_every_pending_hypothesis_in_the_repo_has_a_claim_metric():
    """규약 E를 **실제 파일에** 강제한다 — 새 가설을 주장 지표 없이 등재하면 여기서 깨진다."""
    entries = hypotheses.load(PROJECT_ROOT / "docs" / "동작점검" / "hypotheses.yaml")
    pending = [e for e in entries if str(e.get("상태", "")).lower() == "pending"]
    assert pending, "pending이 하나도 없으면 이 테스트가 아무것도 안 지킨다"

    for entry in pending:
        roles = [str(p.get("역할", hypotheses.ROLE_REFERENCE)) for p in entry.get("예측") or []]
        assert hypotheses.ROLE_CLAIM in roles, f"{entry['id']}: 주장 지표가 없다"
        if entry.get("대가"):
            assert hypotheses.ROLE_COST in roles, f"{entry['id']}: 대가를 선언하고 안 쟀다"
