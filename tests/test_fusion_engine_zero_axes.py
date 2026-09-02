"""판단 형태 전이 줄이 **어느 축이 0점을 냈는지 이름을 말한다** (2026-09-02 제4부 P1-3).

09-02 §1-8 — 14:06부터 비영 멤버가 3/6 → 2/6으로 내려앉아 그 시간대 판단의 72%가 2/6이었다.
그런데 로그에는 「비영 2」만 있어 **어느 축이 빠졌는지는 DB를 열어야만** 알 수 있었다.
값은 이미 그 자리에 있었다(`member_scores`) — 로그에만 없었다.

⚠ **이 파일이 지키는 진짜 선은 「판정 무변경」이다.** 이 항목은 B등급(판정에 닿는 자리의
계측)이라, 꼬리표가 붙은 뒤에도 ① 판단 출력이 1비트도 안 바뀌고 ② `collect_evidence.MEMBER_RE`가
종전과 **같은 값**을 계속 읽어야 한다. 08-04에 문구가 움직여 362건이 0건으로 보고된 자리다.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

import pytest

from mahdi.engines.regime import RegimeLabel, RegimeState
from mahdi.fusion.engine import MetaLabelContext, SignalFusionEngine
from mahdi.fusion.signal_layer import SignalInputs

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COLLECTOR = PROJECT_ROOT / "docs" / "동작점검" / "tools" / "collect_evidence.py"


@pytest.fixture(scope="module")
def collector():
    spec = importlib.util.spec_from_file_location("collect_evidence_p13", COLLECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _regime_state(regime: RegimeLabel, prob: float = 1.0) -> RegimeState:
    prob_vector = [0.0] * 8
    prob_vector[regime] = prob
    return RegimeState(regime=regime, prob_vector=tuple(prob_vector), stability_flag=True)


def _dead_axis_inputs() -> SignalInputs:
    """`regime_hmm`이 중립(0점)인 상태 — 08-07 실측 212분 전량이 이랬다."""
    return SignalInputs(
        regime_state=_regime_state(RegimeLabel.RANGE_BALANCED),
        gex=-1000.0, gamma_flip=100.0, spot=105.0,
        ofi=5.0, queue_imbalance=0.3, foreign_net_flow=500.0,
    )


def _shape_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "판단 형태 전이" in r.getMessage()]


def test_zero_scoring_axis_is_named_not_just_counted(caplog):
    """「비영 N」은 몇 개인지만 말한다 — 이 줄은 **누구인지**를 말한다."""
    engine = SignalFusionEngine()
    with caplog.at_level(logging.INFO, logger="mahdi.fusion.engine"):
        engine.evaluate(_dead_axis_inputs(), MetaLabelContext())

    lines = _shape_lines(caplog)
    assert len(lines) == 1
    assert "0점축=" in lines[0]
    assert "regime_hmm" in lines[0].split("0점축=")[1], (
        "0점을 낸 축의 이름이 꼬리표에 실려야 한다"
    )


def test_empty_zero_axis_list_is_still_printed(caplog):
    """규약 C — `0점축=[]`가 없으면 「0점 축이 없었다」와 「이 줄이 아직 안 실렸다」가
    같은 글자가 된다."""
    engine = SignalFusionEngine()
    inputs = SignalInputs(foreign_net_flow=500.0)  # 가용 축이 전부 비영
    with caplog.at_level(logging.INFO, logger="mahdi.fusion.engine"):
        decision = engine.evaluate(inputs, MetaLabelContext())

    assert decision.available_member_count == decision.effective_member_count, (
        "0점 축이 있으면 이 시나리오가 아니다"
    )
    lines = _shape_lines(caplog)
    assert len(lines) == 1
    assert "0점축=[]" in lines[0]


def test_tag_does_not_change_the_decision(caplog):
    """**판정 무변경** — 꼬리표는 로그일 뿐이다. 로깅을 끄든 켜든 판단은 같아야 한다."""
    inputs = _dead_axis_inputs()
    with caplog.at_level(logging.INFO, logger="mahdi.fusion.engine"):
        logged = SignalFusionEngine().evaluate(inputs, MetaLabelContext())
    with caplog.at_level(logging.CRITICAL, logger="mahdi.fusion.engine"):
        silent = SignalFusionEngine().evaluate(inputs, MetaLabelContext())

    assert logged.trade_permission == silent.trade_permission
    assert logged.conviction_score == silent.conviction_score
    assert logged.available_member_count == silent.available_member_count
    assert logged.effective_member_count == silent.effective_member_count
    assert tuple(logged.reject_reasons) == tuple(silent.reject_reasons)
    assert tuple(logged.allowed_strategies) == tuple(silent.allowed_strategies)


def test_member_parser_still_reads_the_same_numbers(caplog, collector):
    """대가 축 — `MEMBER_RE`가 꼬리표 뒤에도 **같은 값**을 읽는가.

    08-04에 문구가 움직여 362건이 0건으로 보고됐다. 꼬리표를 **줄 끝에만** 붙인 이유가 이것이다.
    """
    engine = SignalFusionEngine()
    with caplog.at_level(logging.INFO, logger="mahdi.fusion.engine"):
        decision = engine.evaluate(_dead_axis_inputs(), MetaLabelContext())

    line = _shape_lines(caplog)[0]
    match = collector.MEMBER_RE.search(line)
    assert match is not None, "꼬리표를 붙인 뒤 파서가 눈이 멀었다"
    assert int(match.group(2)) == decision.available_member_count
    assert int(match.group(3)) == 6
    assert int(match.group(4)) == decision.effective_member_count, (
        "「비영 N」의 자리가 움직이면 안 된다"
    )


def test_tag_sits_at_the_end_after_the_parsed_head(caplog):
    """꼬리표는 **줄 끝**이다 — 앞머리(`가용멤버 …` · `사유` · `전략`)보다 뒤에 온다."""
    engine = SignalFusionEngine()
    with caplog.at_level(logging.INFO, logger="mahdi.fusion.engine"):
        engine.evaluate(_dead_axis_inputs(), MetaLabelContext())

    line = _shape_lines(caplog)[0]
    assert line.index("0점축=") > line.index("전략 "), "꼬리표가 앞머리를 밀면 파서가 깨진다"
