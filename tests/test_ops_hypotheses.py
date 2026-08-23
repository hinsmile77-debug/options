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


def test_missing_leaf_reports_no_data_instead_of_failing():
    """**부모 절은 있는데 그날 그 잎이 없는** 경우 — 정상일 수 있다. 내일 다시 본다."""
    [r] = hypotheses.evaluate(
        [_entry(예측=[{"metric": "overrun.없는키", "expect": "<= 1", "역할": hypotheses.ROLE_CLAIM}])],
        date(2026, 8, 3), _METRICS,
    )
    assert r["actual"] is None
    assert r["verdict"] == hypotheses.VERDICT_NO_DATA
    assert r["path_dead"] is False


# ===== 2026-08-06 §3-1 / Fix#3 — 「경로 없음」과 「실측 없음」을 가른다 =====
#
# 08-05 예측 13건 중 6건이 존재하지 않는 경로를 지목했고, 전부 「실측 없음」으로 표시돼
# 28행짜리 표의 조용한 한 줄이 됐다. 그래서 `p1`의 **대가 지표 12배 초과**(ENTER 예측 ≤5에
# 실측 62건)를 아무도 자동으로 알아채지 못했다. 둘은 조치가 다르다:
#   실측 없음  그날 값이 안 나왔다 → 내일 다시 본다
#   경로 없음  **영원히** 안 나온다 → yaml을 고쳐야 한다


def test_a_nonexistent_section_is_reported_as_a_dead_path():
    [r] = hypotheses.evaluate(
        [_entry(예측=[{"metric": "없는절.어떤키", "expect": "<= 1", "역할": hypotheses.ROLE_CLAIM}])],
        date(2026, 8, 3), _METRICS,
    )
    assert r["verdict"] == hypotheses.VERDICT_PATH_DEAD
    assert r["path_dead"] is True


def test_the_2026_08_05_paths_that_died_silently_are_now_flagged():
    """08-05가 실제로 적었던 경로들 — 그날 전부 「실측 없음」이었다."""
    dead_for_real = [
        "db.decisions.decision.ENTER",                              # `db.decisions` 절이 없던 시절
        "db.member_availability.orderflow_ofi_vpin.available_pct",  # 멤버가 키가 아니라 리스트 원소
        "db.tables.underlying_spot_1m.rows",                        # `db.tables`가 리스트
    ]
    for metric in dead_for_real:
        [r] = hypotheses.evaluate(
            [_entry(예측=[{"metric": metric, "expect": ">= 0", "역할": hypotheses.ROLE_CLAIM}])],
            date(2026, 8, 3), _METRICS, {"monthly_coverage": {"coverage_pct": 96.4}},
        )
        assert r["verdict"] == hypotheses.VERDICT_PATH_DEAD, metric


def test_those_same_paths_resolve_once_the_sections_exist():
    """Fix#3의 나머지 절반 — **경로를 사람에게 맞춘다.** 세 경로 모두 이제 값을 낸다."""
    db = {
        "decisions": {"decision": {"ENTER": 62}},
        "member_availability": {
            "members": [{"member": "orderflow_ofi_vpin", "available_pct": 82.3}],
            "orderflow_ofi_vpin": {"member": "orderflow_ofi_vpin", "available_pct": 82.3},
        },
        "tables": [{"table": "underlying_spot_1m", "rows": 384}],
    }
    expected = {
        "db.decisions.decision.ENTER": 62,
        "db.member_availability.orderflow_ofi_vpin.available_pct": 82.3,
        "db.tables.underlying_spot_1m.rows": 384,
    }
    for metric, value in expected.items():
        [r] = hypotheses.evaluate(
            [_entry(예측=[{"metric": metric, "expect": ">= 0", "역할": hypotheses.ROLE_CLAIM}])],
            date(2026, 8, 3), _METRICS, db,
        )
        assert r["actual"] == value, metric
        assert r["path_dead"] is False


def test_a_day_with_no_metrics_at_all_does_not_flag_every_path_as_dead():
    """DB가 통째로 죽은 날 전 경로가 「경로 없음」으로 뜨면 진짜 오타가 그 소음에 묻힌다 —
    이 fix가 고치려던 바로 그 실패 형태다."""
    [r] = hypotheses.evaluate(
        [_entry(예측=[{"metric": "db.decisions.decision.ENTER", "expect": ">= 0", "역할": hypotheses.ROLE_CLAIM}])],
        date(2026, 8, 3), None, None,
    )
    assert r["verdict"] == hypotheses.VERDICT_NO_DATA
    assert r["path_dead"] is False


def test_path_exists_uses_the_parent_not_the_leaf():
    """잎은 데이터 의존, 부모는 구조 — 이 구분이 판별의 전부다."""
    db = {"decisions": {"reject_reason": {}}}
    assert hypotheses.path_exists(None, db, "db.decisions.reject_reason.wait_only") is True
    assert hypotheses.path_exists(None, db, "db.decisions.없는축.wait_only") is False
    # 마디가 하나뿐인 경로는 그 자체가 절 이름이다.
    assert hypotheses.path_exists(None, db, "db.decisions") is True
    assert hypotheses.path_exists(None, db, "db.없는절") is False


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
        # 2026-08-14 고도화 4 — 어휘의 출처는 `hypotheses.STATUSES` **하나**다.
        # 여기에 집합을 다시 적으면 새 상태가 추가된 날 이 테스트가 그것을 「오타」로 잡는다.
        assert str(entry.get("상태")).lower() in hypotheses.STATUSES
        for prediction in entry["예측"]:
            assert "metric" in prediction and "expect" in prediction


# 자동 리포트가 실제로 내는 최상위 절 이름. 여기 없는 접두사를 쓰면 그 예측은 **영원히
# "실측 없음"** 이 되어 검정이 무력화된다 — 위 테스트의 주석이 바로 그 위험을 적어두고도
# 정작 리포지터리 파일의 경로는 검사하지 않고 있었다.
#
# 2026-08-06(운영점검 장전편) — 그 빈틈이 실제로 뚫려 있었다. `2026-08-05-p2`의 **주장 지표
# 두 개**가 `log.failures.…`로 적혀 있었는데 리포트의 절 이름은 `failures`다(`log_` 접두사는
# 모듈 이름이지 지표 경로가 아니다). 08-06 07:52 리포트가 그 둘을 "실측 없음"으로 냈고,
# 같은 날 로그에는 해당 실패가 3건 실재했다. **주장 지표가 죽은 채로 가설이 하루를 갔다.**
_METRIC_ROOTS = {
    "cycles", "rest", "backoff", "bursts", "stalls", "slow_calls", "rest_latency",
    "atm_rolls", "budget_exceeded", "catchups", "poller_phase", "log_volume",
    "qualitative", "parser_audit", "failures", "overrun",
    # 2026-08-06 Fix#4/#5/#6 — 억제된 예외, 프로세스 기동 시각, 실패의 원인 축.
    "qualitative_suppressed", "process_starts", "failures_by_cause",
    # 2026-08-06 고도화#1 — 먼슬리 레그 재시도.
    "priority_retry",
    # 2026-08-11 Fix#1 / 고도화 A — 사이클 조기 종료의 **두 원인**. 예산 초과(우리가 느렸다)와
    # 원인이 다르므로 지표도 갈렸다: 연속 타임아웃(KIS가 4초 천장에 닿았다) / 누적 실패
    # (성공·실패가 섞여 절반이 죽었다). 08-11에는 셋이 한 줄이었다.
    "timeout_abort", "failure_budget_abort",
    # 2026-08-11 Fix#7 — 폴러별 밀림. `overrun`은 옵션체인 전용이라 다른 폴러가 안 잡혔다.
    "overrun_by_poller",
    # 2026-08-12 Fix#6/#8 — 로그 축이 아니라 **그날의 코드 상태**(레버)와 **감시자 자신의 로그**.
    # 둘 다 `daily_ops_report.build`가 `metrics`에 실어 넣는다. 사이드카에만 넣으면
    # `hypotheses._lookup`이 못 찾아 그 가설이 영원히 「경로 없음」이 된다(08-06 §3-1의 사고).
    "levers", "watchdog",
    # 2026-08-12 고도화 1 — WS 단절을 **비용**으로 재는 축(시각·시간대별 분포).
    "ws_disconnect",
    # 2026-08-19 Fix#3 — §1 델타의 **기준일이 무엇이었는가**. `levers`/`watchdog`과 같이
    # `daily_ops_report.build`가 `metrics`에 실어 넣는다(사이드카에만 넣으면 검정 불가).
    "delta_baseline",
    # 2026-08-19 — 관측 루프가 **왜** 죽었는가(`observation_loop_crash.log`).
    # 예외가 로깅을 거치지 않고 프로세스를 끝내므로 `log_metrics`가 읽는 파일에는 안 남는다.
    "crash",
}


# DB 지표 쪽 최상위 절 — `mahdi.ops.db_metrics.collect()`가 만드는 키들.
#
# 2026-08-06(§3-1 / Fix#3) — **아래 `db.` 면제가 뚫려 있던 구멍이다.** 08-06 아침 커밋이 위
# 테스트를 만들면서 `if not metric.startswith("db.")`로 DB 경로를 통째로 비켜갔고, 그날 죽어
# 있던 경로 12개는 **전부 `db.`로 시작했다**. 예측 13건 중 6건이 주장 지표를 하나도 못 받았다.
_DB_METRIC_ROOTS = {
    "monthly_coverage", "tables", "chain_minute_coverage", "monthly_leg_completeness",
    "spot_source_divergence", "book_coverage", "book_gamma_map", "wide_oi_landscape",
    "member_availability", "member_score_quality", "strike_window_quality",
    "signal_decisions", "decisions", "decision_outcomes", "signal_reach", "risk_gate_distinct", "regime",
    "feature_store", "macro", "market_halt", "ws_status", "remaining_processes", "rate_limiter",
    # 2026-08-12 고도화 1 — 선물봉 vs 레짐 분 수(Fix#3의 직접 지표).
    "regime_vs_futures_bars",
    # 2026-08-16 (Block B) — 보유 포지션(마이그레이션 030). 방향 판정 실패분을 여기서 본다.
    "positions",
    # 2026-08-16 (Block D) — 경보 토글(DB 상태). 「조건 없음」과 「토글 꺼짐」을 가른다.
    "slack_alerts",
    # 2026-08-23 (실행 배선 ①) — 포지션 원장(마이그레이션 034). `positions`(030)와 겹치지
    # 않는다 — 저쪽은 브로커 미러, 이쪽은 우리 원장이고 **둘이 갈리면 그 자체가 사건이다.**
    "ledger",
    # 2026-08-23 (실행 배선 ②) — 체결통보(마이그레이션 035). 첫 통보가 `_NOTICE_FIELDS`를
    # 실측으로 확정한다 — `field_count_distribution`이 그 판정이다.
    "order_notices",
    # 2026-08-23 (실행 배선 ③) — 실제로 나간 주문(execution_logs).
    "orders",
}


def test_db_metric_roots_match_what_collect_actually_produces():
    """위 집합이 `db_metrics.collect()`의 실제 키와 갈라지면 이 테스트가 무의미해진다.

    `collect()`는 DB가 필요해 여기서 못 돌리므로 **소스에 적힌 키 목록**을 읽어 대조한다 —
    키가 추가/삭제될 때 이 테스트가 먼저 깨지는 것이 요점이다.
    """
    import inspect
    import re

    from mahdi.ops import db_metrics

    source = inspect.getsource(db_metrics.collect)
    declared = set(re.findall(r'\(\s*"([a-z_]+)"\s*,\s*[\w.]+\s*\)', source))
    declared.add("monthly_coverage")  # elapsed_minutes가 있을 때만 붙는 예외 경로
    assert declared == _DB_METRIC_ROOTS, (
        f"db_metrics.collect()의 절 목록이 바뀌었다: 추가 {declared - _DB_METRIC_ROOTS} / "
        f"삭제 {_DB_METRIC_ROOTS - declared}"
    )


def test_every_repository_metric_path_starts_at_a_real_report_section():
    """지표 경로의 **첫 마디**가 자동 집계의 실제 절 이름인지 본다 — **`db.`도 포함해서.**

    전체 경로를 검사하지 않는 이유는 하위 키가 그날 데이터에 따라 없을 수 있기 때문이다 —
    없는 것과 **틀린 것**은 다르고, 이 테스트가 잡아야 하는 것은 후자다. 그 판별(부모 컨테이너가
    해석되는가)은 런타임에 `hypotheses.path_exists()`가 하고 리포트 §0이 「경로 없음」으로 낸다.
    """
    entries = hypotheses.load(PROJECT_ROOT / "docs" / "동작점검" / "hypotheses.yaml")
    wrong = []
    for entry in entries:
        for prediction in entry["예측"]:
            metric = str(prediction["metric"])
            if metric.startswith("db."):
                root, roots = metric[3:].split(".")[0], _DB_METRIC_ROOTS
            else:
                root, roots = metric.split(".")[0], _METRIC_ROOTS
            if root not in roots:
                wrong.append((entry["id"], metric))
    assert not wrong, (
        f"자동 집계에 없는 절에서 시작하는 지표 경로: {wrong} — "
        "이런 경로는 그 가설을 영원히 검정 불가로 만든다"
    )


# ===== 2026-08-19 장후 — 잎 린트 (`leaf_absent`) =====
#
# 위 `test_every_repository_metric_path_starts_at_a_real_report_section`은 **첫 마디**만 본다.
# 그 규칙을 통과하면서 영원히 값을 못 내는 경로가 08-19에 저장소 안에서 **셋** 발견됐다.
# 아래가 그 셋을 회귀로 못 박고, 마지막 테스트가 **다음 셋이 생기는 것을 막는다.**

_LEAF_LINT_SAMPLE = {
    "decisions": {"total": 485, "entry_strategies": {"straddle_accumulate": 169}},
    "regime": {"visits": [{"regime": "3", "today": 109}], "today_total": 402},
    "tables": [{"table": "execution_logs", "rows": 0}],
    "member_availability": {"minutes": 485, "members": [{"member": "options_flow", "available_minutes": 369}]},
}


@pytest.mark.parametrize(
    "path",
    [
        "db.decisions.strategies",                 # 실재 키는 entry_strategies (레버 F의 **대가** 지표였다)
        "db.regime.warmup_minutes",                # 그 잎은 미구현이었다 (같은 날 신설)
        "db.member_availability.headcount",        # 구조 dict의 잎 오타
    ],
)
def test_a_leaf_that_never_appears_is_caught_even_though_its_parent_exists(path):
    """이 셋은 `path_exists()`를 **통과한다** — 부모가 실재하기 때문이다. 그것이 그 구멍이었다."""
    assert hypotheses.path_exists(None, _LEAF_LINT_SAMPLE, path) is True
    assert hypotheses.leaf_absent(None, _LEAF_LINT_SAMPLE, path) is True


@pytest.mark.parametrize(
    "path",
    [
        "db.decisions.entry_strategies",                          # 실재하는 잎
        "db.decisions.entry_strategies.limited_premium_sell",     # **카운터 dict** — 그날 안 난 전략
        "db.tables.underlying_spot_1m",                           # 부모가 list(자연 키 색인)
        "db.decisions.total.whatever",                            # 부모가 스칼라
        "db.member_availability.members.options_flow.available_minutes",
        "db.decisions",                                           # 마디 하나 — path_exists의 관할
    ],
)
def test_the_leaf_lint_stays_silent_where_absence_is_normal(path):
    """**오탐보다 미탐이 낫다.** 거짓 경보가 몇 번 나면 이 열 전체가 무시된다."""
    assert hypotheses.leaf_absent(None, _LEAF_LINT_SAMPLE, path) is False


def test_a_dead_leaf_is_reported_as_a_dead_path_not_as_missing_data():
    """조치가 다르다 — 「실측 없음」은 내일 다시 보는 것이고 「경로 없음」은 오늘 yaml을 고치는 것이다."""
    entry = _entry(예측=[{"metric": "db.decisions.strategies", "expect": ">= 1", "역할": hypotheses.ROLE_CLAIM}])
    [r] = hypotheses.evaluate([entry], date(2026, 8, 3), None, _LEAF_LINT_SAMPLE)
    assert r["path_dead"] is True
    assert r["verdict"] == hypotheses.VERDICT_PATH_DEAD


def test_a_fix_landed_on_or_after_the_metrics_date_is_exempt_from_the_leaf_lint():
    """fix를 낸 **당일·다음 날**에 거짓 경보가 뜨면 이 린트는 하루 만에 무시된다.

    같은 날도 면제하는 이유: 사이드카는 15:45에 확정되는데 커밋은 그 뒤에도 들어온다
    (08-19 17:00 커밋의 `slow_calls.censored.phase_ratio`가 실제로 그 형태였다).
    """
    pred = [{"metric": "db.decisions.strategies", "expect": ">= 1", "역할": hypotheses.ROLE_CLAIM}]
    same_day = _entry(구현일=date(2026, 8, 3), 예측=pred)
    later = _entry(구현일=date(2026, 8, 4), 예측=pred)
    earlier = _entry(구현일=date(2026, 8, 2), 예측=pred)
    assert hypotheses.evaluate([same_day], date(2026, 8, 3), None, _LEAF_LINT_SAMPLE)[0]["path_dead"] is False
    assert hypotheses.evaluate([later], date(2026, 8, 3), None, _LEAF_LINT_SAMPLE)[0]["path_dead"] is False
    assert hypotheses.evaluate([earlier], date(2026, 8, 3), None, _LEAF_LINT_SAMPLE)[0]["path_dead"] is True


def test_an_entry_without_an_implementation_date_is_still_linted():
    """모르는 것을 면제로 바꾸면 「구현일을 안 적으면 통과」로 이 린트가 우회된다."""
    entry = _entry(예측=[{"metric": "db.decisions.strategies", "expect": ">= 1", "역할": hypotheses.ROLE_CLAIM}])
    entry.pop("구현일")
    assert hypotheses.evaluate([entry], date(2026, 8, 3), None, _LEAF_LINT_SAMPLE)[0]["path_dead"] is True


def test_the_repository_hypotheses_have_no_dead_leaves_against_the_latest_sidecar():
    """저장소의 pending 가설 전건을 **가장 최근 사이드카**에 대고 돌린다.

    ## 등재 시점 검사만으로는 부족하다

    현행 검사(`test_every_repository_metric_path_starts_at_a_real_report_section`)는 첫 마디만
    본다. 그리고 등재 시점에 **살아 있던** 경로가 나중에 옮겨지면 어떤 검사도 그것을 못 잡는다 —
    `c1`의 `db.tables.execution_logs`가 그 형태였다. 그래서 **매일 실측에 대고** 돌린다.

    ## 휴장일·다른 PC에서 죽지 않게

    사이드카가 하나도 없으면 `skip`한다(이식성 규약 — 이 저장소는 여러 PC에서 돌아간다).
    """
    import json

    auto = PROJECT_ROOT / "docs" / "동작점검" / "auto"
    sidecars = sorted(auto.glob("*_지표.json")) if auto.exists() else []
    if not sidecars:
        pytest.skip("사이드카가 없다 — 이 검사는 실측이 있어야 뜻이 있다")
    latest = sidecars[-1]
    metrics = json.loads(latest.read_text(encoding="utf-8"))
    target = date.fromisoformat(latest.name[:10])
    db_metrics = metrics.get("db") or {}

    dead = []
    for entry in hypotheses.load(PROJECT_ROOT / "docs" / "동작점검" / "hypotheses.yaml"):
        if str(entry.get("상태", "pending")).lower() != "pending":
            continue
        if not hypotheses.measurable_on(entry, target):
            continue
        for prediction in entry.get("예측") or []:
            metric = str(prediction["metric"])
            if hypotheses.leaf_absent(metrics, db_metrics, metric):
                dead.append((entry.get("id"), metric, prediction.get("역할", "참고")))
    assert not dead, (
        f"{latest.name}에 잎이 없는 지표 경로: {dead} — 부모가 실재해서 「실측 없음」으로 "
        "분류되지만 그 값은 **내일도 안 나온다**. yaml의 경로를 고치거나 지표를 신설할 것"
    )


def test_the_2026_08_05_dead_paths_would_now_be_caught():
    """회귀 방지 — 08-05에 실제로 죽어 있던 경로들이 지금 규칙에 걸리는지 본다.

    이 테스트가 통과하는 것만으로는 부족하다(그 경로들은 이미 고쳐졌다). 요점은 **규칙이
    그것을 잡을 수 있는가**이고, 그래서 문자열을 직접 넣어 본다.
    """
    dead = [
        "db.decisions_typo.reject_reason",       # 절 이름 오타
        "db.tables_by_name.underlying_spot_1m",  # 존재하지 않는 절
        "log.failures.만기 유동성 폴링 실패",      # 08-05 p2의 실제 오타(모듈명을 절로 착각)
    ]
    for metric in dead:
        root = metric[3:].split(".")[0] if metric.startswith("db.") else metric.split(".")[0]
        assert root not in _METRIC_ROOTS and root not in _DB_METRIC_ROOTS, metric


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
    section = out.split("## 0-1. 가설 검정", 1)[1].split("\n## ", 1)[0]
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


# ==========================================================================================
# 2026-08-07(§4 / Fix#5) — 규약 F: 주장 지표는 절대 건수로 세우지 않는다
# ==========================================================================================


def test_count_shaped_metrics_are_recognised():
    """08-07에 세 번 틀린 그 형태들이 전부 잡혀야 한다."""
    assert hypotheses.is_count_shaped("db.tables.expiry_liquidity_1m.rows")
    assert hypotheses.is_count_shaped("db.signal_reach.chain_leg_over_design_minutes")
    assert hypotheses.is_count_shaped("rest.by_group.옵션체인_calls")
    # 정규화된 것은 아니다 — 구조 변수가 분모에서 약분된다.
    assert not hypotheses.is_count_shaped("db.monthly_coverage.coverage_pct")
    assert not hypotheses.is_count_shaped("db.signal_reach.chain_age_seconds_median")
    assert not hypotheses.is_count_shaped("rest.calls_per_second")
    assert not hypotheses.is_count_shaped("db.decisions.member_count.dead_axis_mean")


def test_normalized_claim_rule_flags_the_three_08_07_mistakes():
    """08-07 하루에 같은 형태로 세 번 틀렸다 — fix는 맞았는데 예측이 반증으로 찍혔다."""
    assert hypotheses.violates_normalized_claim_rule("주장", "db.tables.expiry_liquidity_1m.rows", ">= 100")
    assert hypotheses.violates_normalized_claim_rule("주장", "db.ws_status.atm_roll_dropped_subs_today", "<= 4")
    assert hypotheses.violates_normalized_claim_rule("주장", "db.signal_reach.chain_leg_count", "0 ~ 10")


def test_normalized_claim_rule_allows_invariants_and_non_claim_roles():
    """`== 0`(불변식)과 대가·참고는 막지 않는다.

    0은 구조 변수에 비례하지 않는 유일한 건수다 — 08-07 Fix#1의
    `chain_leg_over_design_minutes == 0`이 정확히 그 형태이고, 그건 옳은 예측이다.
    대가는 "얼마나 늘었나"가 본질이라 건수가 맞는 경우가 많다.
    """
    assert not hypotheses.violates_normalized_claim_rule(
        "주장", "db.signal_reach.chain_leg_over_design_minutes", "== 0"
    )
    assert not hypotheses.violates_normalized_claim_rule("대가", "rest.by_group.옵션체인", "<= 9500")
    assert not hypotheses.violates_normalized_claim_rule("참고", "db.tables.feature_store.rows", ">= 8000")
    # 수기 판정으로 내리면 통과한다 — 사람이 그날의 구조 변수를 보고 읽겠다는 선언이다.
    assert not hypotheses.violates_normalized_claim_rule(
        "주장", "db.tables.expiry_liquidity_1m.rows", "수기 판정 — 북 수로 나눠 읽을 것"
    )


# ===== 규약 G (2026-08-11 Fix#6) — 시장 상태 의존 지표에 무조건부 하한 금지 =====


def test_market_state_rule_flags_the_08_11_regime_mistake():
    """08-11 실사고 — 레짐 엔진이 완벽히 돌았는데 검증 기준 둘이 반증을 찍었다.

    원인은 엔진이 아니라 그날 시장이 추세가 아니었던 것이다(방문 셋 전부 방향 없는 레짐).
    """
    assert hypotheses.violates_market_state_rule("주장", "db.decisions.member_count.dead_axis_mean", "< 1.02") is False
    # 하한이 문제다 — "죽은 축이 이만큼은 있어야 한다"는 조용한 날에 성립하지 않는다.
    assert hypotheses.violates_market_state_rule("주장", "db.decisions.member_count.effective_mean", ">= 3.0")
    assert hypotheses.violates_market_state_rule("주장", "db.regime.trend_minutes", "> 0")
    assert hypotheses.violates_market_state_rule("주장", "db.signal_reach.gamma_flip_pct", ">= 80")


def test_market_state_rule_allows_upper_bounds_and_invariants():
    """상한은 막지 않는다 — "이보다 많으면 이상"은 시장이 조용해도 성립한다."""
    # 채터링 감시가 그 형태다.
    assert not hypotheses.violates_market_state_rule("주장", "db.regime.trend_minutes", "<= 20")
    # `>= 0`은 경로 생존 확인이라 시장과 무관하다.
    assert not hypotheses.violates_market_state_rule("주장", "db.tables.market_raw_1m.rows", ">= 0")
    # 대가·참고는 대상이 아니다.
    assert not hypotheses.violates_market_state_rule("대가", "db.tables.market_raw_1m.rows", ">= 900")
    # 수기 판정은 사람이 그날 시장을 보고 읽겠다는 선언이다.
    assert not hypotheses.violates_market_state_rule(
        "주장", "db.regime.trend_minutes", "수기 판정 — 추세 방문이 0분이면 판정 불가"
    )


def test_repo_pending_hypotheses_obey_the_market_state_rule():
    """규약 G를 **실제 파일에** 강제한다 — 규약 F와 같은 방식이다."""
    entries = hypotheses.load(PROJECT_ROOT / "docs" / "동작점검" / "hypotheses.yaml")
    offenders = [
        f"{e['id']}: {p['metric']} ({p.get('expect')})"
        for e in entries if str(e.get("상태", "")).lower() == "pending"
        for p in e.get("예측") or []
        if hypotheses.violates_market_state_rule(
            p.get("역할", hypotheses.ROLE_REFERENCE), p["metric"], p.get("expect", "")
        )
    ]
    assert not offenders, (
        "시장 상태에 의존하는 지표에 무조건부 하한을 걸었다 — 시장이 조용한 날마다 멀쩡한 "
        f"구현이 반증으로 찍힌다. 조건을 지표로 만들어 함께 걸 것: {offenders}"
    )


def test_directional_regimes_match_the_signal_layer():
    """`db_metrics.DIRECTIONAL_REGIMES`는 `signal_layer._TREND_DIRECTION`의 복제다.

    갈라지면 `trend_minutes`가 조용히 틀린 전제를 세우고, 그 위에서 `regime_hmm` 판정이
    다시 08-11과 같은 오독을 한다. `log_metrics.PRIORITY_SERIES_LABEL`과 같은 계약이다.
    """
    from mahdi.fusion import signal_layer
    from mahdi.ops import db_metrics

    assert set(db_metrics.DIRECTIONAL_REGIMES) == {int(r) for r in signal_layer._TREND_DIRECTION}


def test_repo_pending_hypotheses_obey_the_normalized_claim_rule():
    """규약 F를 **실제 파일에** 강제한다 — 08-07에 세 번 겪은 실수를 파일이 막는다."""
    entries = hypotheses.load(PROJECT_ROOT / "docs" / "동작점검" / "hypotheses.yaml")
    offenders = [
        f"{e['id']}: {p['metric']} ({p.get('expect')})"
        for e in entries if str(e.get("상태", "")).lower() == "pending"
        for p in e.get("예측") or []
        if hypotheses.violates_normalized_claim_rule(
            p.get("역할", hypotheses.ROLE_REFERENCE), p["metric"], p.get("expect", "")
        )
    ]
    assert not offenders, (
        "주장 지표를 절대 건수 부등식으로 걸었다 — 그 건수를 만드는 구조 변수(관측 길이·북 수·"
        f"스팟 이동거리)가 바뀌면 멀쩡한 fix가 반증으로 찍힌다: {offenders}"
    )


# ===== 무조건발동일 (2026-08-13 고도화 4 + 2026-08-14 고도화 5) =====
#
# 레버 F는 세 번 「오늘 켤 것」으로 지정되고 세 번 다 안 켜졌다. 레버 E는 일곱 번 미뤄졌고
# 열 번 중 한 번도 「안 켜기로 했다」고 적힌 적이 없다 — 전부 아침에 잊은 것이다.
# 경고만으로는 세 번 실패했으므로, **강제력은 이 테스트가 갖는다.**


def _levers(**state):
    return {"levers": [
        {"key": k, "위치": "테스트", "value": v, "default": False, "on": v}
        for k, v in state.items()
    ]}


def test_a_passed_deadline_with_the_lever_still_off_is_a_breach():
    entries = [{"id": "eE", "전제레버": "OPTION_CHAIN_SLOW_SERIES_CONGESTED_HOURS",
                "무조건발동일": "2026-08-18", "유예횟수": 7}]

    breaches = hypotheses.lever_deadline_breaches(
        entries, date(2026, 8, 19), _levers(OPTION_CHAIN_SLOW_SERIES_CONGESTED_HOURS=False)
    )

    assert len(breaches) == 1
    assert breaches[0]["지난일수"] == 1
    assert breaches[0]["유예횟수"] == 7


def test_the_deadline_is_silent_before_it_arrives_and_after_the_lever_is_on():
    entries = [{"id": "eE", "전제레버": "L", "무조건발동일": "2026-08-18"}]
    assert hypotheses.lever_deadline_breaches(entries, date(2026, 8, 17), _levers(L=False)) == []
    assert hypotheses.lever_deadline_breaches(entries, date(2026, 8, 19), _levers(L=True)) == []


def test_an_unreadable_lever_is_not_counted_as_a_breach():
    """「꺼져 있었다」와 「못 읽었다」의 조치가 다르다 — 후자로 테스트를 깨면 그 실패는 곧 무시된다."""
    entries = [{"id": "eE", "전제레버": "이름이_틀린_레버", "무조건발동일": "2026-08-18"}]
    assert hypotheses.lever_deadline_breaches(entries, date(2026, 8, 19), _levers(L=False)) == []


def test_levers_due_today_fires_only_on_the_named_day():
    entries = [{"id": "eF", "전제레버": "L", "발동일": "2026-08-17"}]
    assert hypotheses.levers_due_today(entries, date(2026, 8, 17), _levers(L=False))
    assert hypotheses.levers_due_today(entries, date(2026, 8, 18), _levers(L=False)) == []
    assert hypotheses.levers_due_today(entries, date(2026, 8, 17), _levers(L=True)) == []


def test_repo_levers_have_not_blown_their_unconditional_deadline():
    """**이 테스트가 이 장치의 전부다.**

    `무조건발동일`이 지났는데 레버가 꺼져 있으면 여기서 빨간불이 뜬다. 고치는 길은 둘뿐이고
    **둘 다 사람의 한 줄이면 끝난다**:

        (a) 레버를 켠다 — 그날 아침 기동 전에.
        (b) 날짜를 옮기고 **유예 사유를 문자로 적는다** — 그것이 규약이 요구한 전부다.

    자동으로 켜지 않는 이유는 `scripts/check_lever_due.py` docstring 참고(결정 7).
    """
    from mahdi.ops import levers as levers_module

    entries = hypotheses.load(PROJECT_ROOT / "docs" / "동작점검" / "hypotheses.yaml")
    breaches = hypotheses.lever_deadline_breaches(
        entries, date.today(), levers_module.collect(PROJECT_ROOT)
    )

    assert not breaches, (
        "무조건발동일이 지났는데 레버가 꺼져 있다: "
        + "; ".join(
            f"{b['id']}(기한 {b['무조건발동일']}, {b['지난일수']}일 지남, 꺼진 레버 {b['off']})"
            for b in breaches
        )
        + " — 켜거나, 날짜를 옮기고 유예 사유를 yaml에 적을 것"
    )


# ===== mechanism_differed (2026-08-14 §5 / 고도화 4) =====


def test_mechanism_differed_is_a_closed_state_not_a_pending_one():
    """08-14 장중 외삽이 이 형태였다 — 방향은 맞고 기제가 달랐다.

    `확인`으로 닫으면 틀린 기제가 살아남고 `반증`으로 닫으면 맞은 방향이 죽는다.
    닫힌 상태이므로 평가기가 더 이상 매일 인쇄하지 않아야 한다.
    """
    entry = _entry(상태=hypotheses.STATUS_MECHANISM_DIFFERED)
    assert hypotheses.evaluate([entry], date(2026, 8, 20), _METRICS) == []


def test_the_status_vocabulary_has_a_single_source():
    """어휘를 두 곳에 적으면 한쪽이 뒤처지고, 그때 새 상태는 「오타」로 판정된다."""
    assert hypotheses.STATUS_MECHANISM_DIFFERED in hypotheses.STATUSES
    assert {"pending", "confirmed", "refuted", "inconclusive", "untested"} <= hypotheses.STATUSES
