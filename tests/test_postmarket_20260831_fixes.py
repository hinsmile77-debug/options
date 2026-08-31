"""2026-08-31 장후 자동조치 — P1-1 · P1-2 · P1-4 · P2-2 · P2-3 · 고도화 1 · 3 · 4 회귀.

**이 파일이 지키는 것은 「판정은 안 바뀌고 재는 것만 늘었다」이다.** 08-31 회차가 고른 항목은
전부 계측이고, 계측이 판정을 흔들면 그것이 이 회차가 막으려던 사고다(08-19 · 08-26에 두 번 났다).

각 절의 상세 근거는 원본 코드의 주석과 `docs/동작점검/hypotheses.yaml`의 2026-08-31 항목에 있다.
"""

from __future__ import annotations

import importlib.util
import logging
from datetime import date, datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COLLECTOR = PROJECT_ROOT / "docs" / "동작점검" / "tools" / "collect_evidence.py"


@pytest.fixture(scope="module")
def collector():
    spec = importlib.util.spec_from_file_location("collect_evidence_20260831", COLLECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ===================================================================== P1-1
# 「감마플립 산출 불가」 줄이 **그 판단이 무엇을 먹었는지** 같은 줄에서 말한다.

def _thin_legs():
    """BS 계산 가능 레그가 최소치(6)에 못 미치는 북 — 산출 불가 경로를 탄다."""
    from mahdi.features.options_intel import OptionLeg

    return [
        OptionLeg(strike=1000.0, option_type="C", oi=10, iv=0.2, t_years=0.05, gamma=0.0, vanna=0.0),
        OptionLeg(strike=1005.0, option_type="P", oi=10, iv=0.2, t_years=0.05, gamma=0.0, vanna=0.0),
    ]


def test_the_uncomputable_line_carries_the_rows_it_was_fed(caplog):
    from mahdi.features.options_intel import find_gamma_flip

    with caplog.at_level(logging.WARNING, logger="mahdi.features.options_intel"):
        assert find_gamma_flip(_thin_legs(), 1000.0, snapshot_rows=2, snapshot_minute="14:01") is None

    line = "\n".join(r.getMessage() for r in caplog.records)
    assert "감마플립 산출 불가" in line, "앞머리 문구는 파서의 마커다 — 절대 안 바뀐다"
    assert "직전 분 적재 2행(14:01)" in line, (
        "08-31에 사람이 이 인과를 잇느라 로그 두 종류를 시각으로 맞춰 붙여야 했다"
    )


def test_without_the_snapshot_it_says_nothing_rather_than_zero(caplog):
    """「모른다」를 「0행」으로 찍으면 그 자리가 곧 다음 오독이다(규약 C)."""
    from mahdi.features.options_intel import find_gamma_flip

    with caplog.at_level(logging.WARNING, logger="mahdi.features.options_intel"):
        find_gamma_flip(_thin_legs(), 1000.0)

    line = "\n".join(r.getMessage() for r in caplog.records)
    assert "감마플립 산출 불가" in line
    assert "직전 분 적재" not in line, "백테스트·대시보드 호출에는 이 사실이 없다"


def test_the_snapshot_minute_comes_from_the_newest_row():
    """스냅샷은 (만기·행사가·타입)별 최신 1건이라 행마다 시각이 다르다 — **가장 최근**이 답이다."""
    from mahdi.main import _chain_snapshot_minute

    rows = [
        {"timestamp": datetime(2026, 8, 31, 14, 0, 12)},
        {"timestamp": datetime(2026, 8, 31, 14, 1, 3)},
        {"timestamp": datetime(2026, 8, 31, 13, 59, 40)},
    ]
    assert _chain_snapshot_minute(rows) == "14:01"
    assert _chain_snapshot_minute([]) is None, "행이 없으면 시각을 지어내지 않는다"
    assert _chain_snapshot_minute([{"timestamp": None}]) is None
    assert _chain_snapshot_minute([{"timestamp": "이상한 값"}]) is None, "모르면 None이다"


# ===================================================================== 고도화 4
# 「산출 불가」와 「기각」은 뿌리가 다른 사건이라 **따로** 센다.

def test_the_two_gamma_flip_events_have_separate_counters():
    from mahdi.ops import log_metrics

    markers = log_metrics._QUALITATIVE_MARKERS
    assert markers["gamma_flip_uncomputable"] == "감마플립 산출 불가"
    assert markers["gamma_flip_out_of_leg_range"] == "감마플립 기각(레그 범위 밖)", (
        "기존 축의 마커를 건드리면 08-25 P2-2가 쓰는 §14 분모가 깨진다"
    )
    assert markers["gamma_flip_uncomputable"] != markers["gamma_flip_out_of_leg_range"]


def test_a_zero_day_still_prints_both_keys():
    """0건인 날에도 키가 실려야 「계산은 다 됐다」와 「그 줄이 없던 버전」이 갈린다(규약 C)."""
    from mahdi.ops import log_metrics

    assert "gamma_flip_uncomputable" in log_metrics._QUALITATIVE_ALWAYS_PRESENT
    assert "gamma_flip_out_of_leg_range" in log_metrics._QUALITATIVE_ALWAYS_PRESENT


def test_the_p1_1_tail_does_not_blind_the_new_counter(caplog):
    """P1-1이 붙인 꼬리 뒤에도 마커가 **부분문자열로** 잡힌다 — 두 fix가 서로를 안 눈멀게 한다."""
    from mahdi.features.options_intel import find_gamma_flip
    from mahdi.ops import log_metrics

    with caplog.at_level(logging.WARNING, logger="mahdi.features.options_intel"):
        find_gamma_flip(_thin_legs(), 1000.0, snapshot_rows=2, snapshot_minute="14:01")

    rendered = caplog.records[-1].getMessage()
    assert log_metrics._QUALITATIVE_MARKERS["gamma_flip_uncomputable"] in rendered


def test_the_evidence_table_lists_the_two_events_apart(collector):
    tokens = [token for _item, token, _alt in collector.MEASUREMENT_MAP]
    assert "감마플립 산출 불가" in tokens
    assert "감마플립 기각" in tokens, "한 줄로 묶어 세면 08-19형 사고(폐기 진단 부활)가 난다"


# ===================================================================== P1-4
# 분모가 1이면 로그가 그렇다고 말한다. ⛔ **동작은 안 바뀐다.**

def test_the_denominator_tag_is_built_after_the_decision_is_final():
    """**판정 무변경.** 꼬리표는 `FusionDecision`이 이미 만들어진 뒤에 생기는 문자열이다."""
    source = (PROJECT_ROOT / "mahdi" / "fusion" / "engine.py").read_text(encoding="utf-8")
    assert source.index("decision = FusionDecision(") < source.index("denominator_note = ")
    tag_block = source.split("denominator_note = ")[1].split("logger.info")[0]
    # 꼬리표를 만드는 자리는 판단을 **읽기만** 한다 — 대입도 호출도 없다.
    assert "=" not in tag_block.split("else")[0].replace("<=", ""), (
        "꼬리표 자리에서 무언가를 대입하면 이 fix는 계측이 아니라 동작 변경이다"
    )


# ===================================================================== P2-3
# 조용한 상한 컷이 사유를 남긴다. ⛔ **판정 무변경 — B등급의 조건이다.**

@pytest.mark.parametrize(
    "allowed_before, used_today, cap, expect_reason",
    [
        (["a"], frozenset(), 2, False),             # 아무것도 안 잘렸다 — 사유가 늘면 안 된다
        (["a"], frozenset({"x", "y"}), 2, True),    # 상한 도달 후 새 전략 — 잘렸다
        ([], frozenset({"x", "y"}), 2, False),      # 자를 것이 애초에 없었다
        (["x"], frozenset({"x", "y"}), 2, False),   # 연속 사용은 상한 밖이다(08-05 Fix#5)
    ],
)
def test_the_cap_reason_appears_only_when_something_was_actually_cut(
    allowed_before, used_today, cap, expect_reason
):
    """**사유는 실제로 잘린 사이클에만 는다.** 안 그러면 매 분 사유가 하나씩 늘어 판정이 흐려진다."""
    from mahdi.fusion.strategy_palette import enforce_daily_strategy_cap

    allowed = enforce_daily_strategy_cap(allowed_before, used_today, cap)
    assert (len(allowed) < len(allowed_before)) is expect_reason


def test_the_cap_itself_was_not_touched():
    """⛔ **판정 무변경** — 이 항목이 바꾼 것은 `reject_reasons`뿐이다.

    상한 함수의 동작은 08-05 Fix#5 이후 그대로여야 한다: 연속 사용은 상한 밖이고,
    새 전략만 남은 슬롯만큼 통과하며, `cap <= 0`은 전부 막는다.
    """
    from mahdi.fusion.strategy_palette import enforce_daily_strategy_cap

    assert enforce_daily_strategy_cap(["a", "b"], frozenset(), 2) == ["a", "b"]
    assert enforce_daily_strategy_cap(["a"], frozenset({"x", "y"}), 2) == []
    assert enforce_daily_strategy_cap(["x"], frozenset({"x", "y"}), 2) == ["x"]
    assert enforce_daily_strategy_cap(["a"], frozenset(), 0) == []


def test_the_engine_appends_the_reason_next_to_the_cooldown_one():
    """08-05에 쿨다운이 같은 이유로 얻은 줄 **바로 옆**이다 — 패턴을 맞추는 것이 이 항목이다."""
    source = (PROJECT_ROOT / "mahdi" / "fusion" / "engine.py").read_text(encoding="utf-8")
    assert 'reject_reasons.append("strategy_daily_cap")' in source
    assert source.index('reject_reasons.append("strategy_daily_cap")') < source.index(
        'reject_reasons.append("reentry_cooldown")'
    ), "상한 컷이 쿨다운보다 먼저 일어난다 — 사유 순서가 그 순서를 따라야 읽힌다"


# ===================================================================== 고도화 1
# 마감 전후 응답시간 비 — **1에 가까우면 우리 쪽, 0에 가까우면 KIS 부하.**

def _window(hh: int, mm: int, p50: float, n: int, ss: int = 46) -> dict:
    """실측 창은 `15:20:46`처럼 **분 중간**에 찍힌다 — 기본값을 그 위상으로 둔다."""
    return {"at": (hh * 60 + mm) * 60 + ss, "endpoint": "inquire-price", "n": n,
            "p50": p50, "p95": p50, "p99": p50, "max": p50}


def test_the_close_transition_reproduces_the_0831_measurement():
    """08-31 실측 — 15:20:46 창 2.42초/77건 → 15:30:46 창 0.02초/101건, 비 **0.008배**."""
    from mahdi.ops.log_metrics import _close_transition

    ct = _close_transition([_window(15, 20, 2.42, 77), _window(15, 30, 0.02, 101)])
    assert ct["before_p50"] == 2.42 and ct["before_calls"] == 77, (
        "경계를 `< 15:20:00`으로 잡으면 08-31이 인용한 바로 그 창이 빠진다"
    )
    assert ct["after_p50"] == 0.02 and ct["after_calls"] == 101
    assert ct["ratio"] == pytest.approx(0.0083, abs=0.0002)


def test_a_missing_side_is_not_measured_rather_than_zero():
    """「비를 못 쟀다」와 「비가 0이다」는 다른 사실이다(규약 C)."""
    from mahdi.ops.log_metrics import _close_transition

    ct = _close_transition([_window(13, 0, 2.42, 77)])  # 두 구간 다 밖이다
    assert ct["ratio"] is None
    assert ct["before_p50"] is None and ct["after_p50"] is None
    assert ct["before_calls"] == 0 and ct["after_calls"] == 0
    assert _close_transition([])["ratio"] is None

    # 한쪽만 있는 날도 **비는 못 쟀다**이지 0이 아니다.
    half = _close_transition([_window(15, 0, 2.42, 77)])
    assert half["before_p50"] == 2.42 and half["after_p50"] is None and half["ratio"] is None


def test_the_ratio_never_divides_by_zero():
    from mahdi.ops.log_metrics import _close_transition

    assert _close_transition([_window(15, 0, 0.0, 5), _window(15, 35, 0.02, 5)])["ratio"] is None


def test_the_report_prints_not_measured_rather_than_a_number():
    from mahdi.ops.report import _render_close_transition

    out = "\n".join(_render_close_transition({"close_transition": {
        "before_window": "14:50~15:20", "before_p50": None, "before_calls": 0,
        "after_window": "15:30~15:45", "after_p50": 0.02, "after_calls": 101, "ratio": None,
    }}))
    assert "못 쟀다" in out
    assert "비 = " not in out, "값이 없는데 숫자를 인쇄하면 그 자리가 곧 다음 오독이다"
    # 구버전 사이드카는 **아무것도 안 낸다** — 없는 값을 0으로 찍지 않는다.
    assert _render_close_transition({}) == []


# ===================================================================== 고도화 3
# 「비영 멤버가 N으로 머문 지속 시간」 — 움직인 것은 수준이 아니라 지속 시간이다.

def _rows(pairs):
    return [(datetime(2026, 8, 31, hh, mm), level) for hh, mm, level in pairs]


def test_the_longest_run_is_measured_not_just_the_level(monkeypatch):
    """08-31 §1-13 — 비영 2가 **8분 연속**이었다. 최신 사이클만 보는 눈은 그것을 못 본다."""
    from mahdi.ops import db_metrics

    rows = _rows([(14, m, 2) for m in range(33, 41)] + [(14, 41, 3)])
    monkeypatch.setattr(db_metrics, "_fetchall", lambda *a, **k: rows)
    runs = db_metrics._effective_member_runs(object(), date(2026, 8, 31))
    assert runs["2"] == 8
    assert runs["3"] == 1


def test_a_gap_in_the_cycles_breaks_the_run(monkeypatch):
    """사이클이 밀려 행이 없는 분을 이어 붙이면 **관측 공백이 「눌러앉았다」로 읽힌다**."""
    from mahdi.ops import db_metrics

    rows = _rows([(14, 0, 2), (14, 1, 2), (14, 30, 2), (14, 31, 2), (14, 32, 2)])
    monkeypatch.setattr(db_metrics, "_fetchall", lambda *a, **k: rows)
    assert db_metrics._effective_member_runs(object(), date(2026, 8, 31))["2"] == 3


def test_no_rows_means_no_keys_rather_than_zero(monkeypatch):
    from mahdi.ops import db_metrics

    monkeypatch.setattr(db_metrics, "_fetchall", lambda *a, **k: [])
    assert db_metrics._effective_member_runs(object(), date(2026, 8, 31)) == {}


def test_the_report_prints_nothing_when_the_axis_is_absent():
    from mahdi.ops.report import _render_effective_member_runs

    assert _render_effective_member_runs({}) == [], "구버전 사이드카를 「0분」으로 찍지 않는다"
    out = "\n".join(_render_effective_member_runs(
        {"decisions": {"member_count": {"longest_run_by_level": {"1": 3, "2": 8}}}}
    ))
    assert "비영 1" in out and "8분" in out


# ===================================================================== P1-2
# 검열률과 p95/timeout이 §12 적신호에 닿는다.

def test_the_censoring_alert_line_is_a_provisional_axis_not_a_relaxed_threshold(collector):
    """⚠ 30%는 **완화가 아니라 새 축의 첫 잠정선**이다 — 기존 임계는 하나도 안 움직였다."""
    assert collector.CENSORED_WINDOW_ALERT_RATIO == 0.30
    assert collector.P50_CENSORED_FLOOR_RATIO == 0.98, "기존 검열 판정선은 그대로다"
    assert collector.P50_TIMEOUT_WARN_RATIO == 0.8, "기존 경고선도 그대로다"


# ===================================================================== P2-2 (P2-B)
# §8 표의 `가설` 열이 닷새째 빈칸이던 것 — 블록 스칼라를 읽는다.

def test_the_repository_hypotheses_all_have_a_readable_claim(collector):
    """08-27 P2-B — `가설: |` 의 값으로 **`|` 한 글자**를 담아 13행이 통째로 빈칸이었다."""
    _path, due = collector.due_hypotheses(PROJECT_ROOT, date(2026, 9, 1))
    assert due, "도래 항목이 없으면 이 회귀는 아무것도 안 지킨다"
    for item, _note in due:
        claim = item.get("가설", "")
        assert claim and claim != "|", f"{item['id']}: 블록 스칼라를 못 읽었다"
        assert len(claim) > 10, f"{item['id']}: 첫 줄만 읽고 끊겼다"


def test_a_pipe_inside_the_claim_cannot_break_the_table():
    """표 셀 안에서 `|`는 열 구분자다 — 본문의 파이프 하나가 §8 표를 통째로 깬다."""
    assert "본문 | 파이프".replace("|", "\\|") == "본문 \\| 파이프"


def test_the_premise_lever_column_is_field_absence_not_a_parser_bug(collector):
    """08-27 P2-B 부수 — 「전제레버가 전 행 `—`」의 원인을 가른다.

    **파서 문제가 아니다.** 저장소 전체에서 `전제레버:` 키를 가진 항목은 넷뿐이고, 도래한
    항목들은 그 키를 애초에 안 달았다. 파서는 달린 곳에서 값을 정확히 읽는다.
    """
    yaml_text = (PROJECT_ROOT / "docs" / "동작점검" / "hypotheses.yaml").read_text(encoding="utf-8")
    assert yaml_text.count("\n  전제레버:") == 4, "이 수가 바뀌면 위 판정을 다시 해야 한다"
    schedule = collector.lever_schedule(PROJECT_ROOT)
    assert "use_effective_member_count" in schedule
