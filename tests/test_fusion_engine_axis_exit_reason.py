"""「판단 축 이탈」 줄이 **왜 빠졌는지**를 말한다 — 2026-09-11 제4부 P1-3 (09-11 §3-1).

## 이 파일이 존재하는 이유

09-11 15:24:54에 `options_flow`가, 15:36:54에 `orderflow_ofi_vpin`이 가용 목록에서 빠졌다.
08-23 Fix#3이 만든 이탈 줄은 **언제·몇 개·얼마 만에**를 정확히 말했지만 **왜**는 말하지
않았고, 장후 회차는 *"입력 데이터 고갈인지 예외 처리 경로인지 로그만으로는 안 갈린다"*고
적은 채 원인 규명을 다음으로 넘겼다. 값은 이미 그 자리에 있었다 — 그 사이클의 `SignalInputs`.

## ⚠ 리포트가 제안한 어휘를 그대로 쓰지 않았다

제4부 P1-3은 `input_missing` / `exception` / `timeout` 셋을 제안했는데, `build_member_scores()`
docstring이 *"실패 조건: 없음 — 원재료 부재는 개별 멤버의 None으로 표현된다"* 라고 못박은 대로
**이 레이어에는 예외를 삼키는 자리도 타임아웃도 없다.** 뒤 둘은 영원히 안 나오는 값이다.
대신 실제로 갈리는 두 갈래를 쓴다 — `원재료없음[...]`과 `부호0[...]`. 뒤엣것은 **입력이
멀쩡했는데 방향이 0이라 기각된 것**이고, 리포트의 세 값 어디에도 자리가 없던 갈래다.

## 무엇을 지키는가

① 사유가 실제로 갈리는가 · ② 사유가 **점수 함수와 어긋나지 않는가**(규칙이 두 곳에 적히면
하나가 옛말을 한다) · ③ **규약 C** — 빈칸 대신 `미상`이 찍히는가 · ④ **판정 무변경** —
꼬리표가 붙은 뒤에도 판단 출력이 1비트도 안 바뀌고 파서가 같은 값을 계속 읽는가.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

import pytest

from mahdi.engines.regime import RegimeLabel, RegimeState
from mahdi.fusion.engine import MetaLabelContext, SignalFusionEngine
from mahdi.fusion.signal_layer import (
    MEMBER_FIELDS,
    SignalInputs,
    build_member_scores,
    member_absence_reason,
)
from mahdi.ops import log_metrics

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COLLECTOR = PROJECT_ROOT / "docs" / "동작점검" / "tools" / "collect_evidence.py"


@pytest.fixture(scope="module")
def collector():
    spec = importlib.util.spec_from_file_location("collect_evidence_axis_reason", COLLECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _regime_state() -> RegimeState:
    prob_vector = [0.0] * 8
    prob_vector[RegimeLabel.TREND_UP_STRONG] = 1.0
    return RegimeState(
        regime=RegimeLabel.TREND_UP_STRONG, prob_vector=tuple(prob_vector), stability_flag=True,
    )


def _full() -> SignalInputs:
    """네 축이 전부 점수를 내는 사이클 — 09-11 오전의 정상 형태다."""
    return SignalInputs(
        regime_state=_regime_state(), gex=-1000.0, gamma_flip=100.0, spot=105.0,
        ofi=5.0, queue_imbalance=0.3, foreign_net_flow=500.0,
    )


def _exit_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "판단 축 이탈" in r.getMessage()]


def _run(caplog, second: SignalInputs) -> list[str]:
    """정상 사이클 한 번 → 무언가 끊긴 사이클 한 번. 이탈 줄은 두 번째에만 난다."""
    engine = SignalFusionEngine()
    with caplog.at_level(logging.INFO, logger="mahdi.fusion.engine"):
        engine.evaluate(_full(), MetaLabelContext())
        caplog.clear()
        engine.evaluate(second, MetaLabelContext())
    return _exit_lines(caplog)


# ===== ① 사유가 갈린다 =====


def test_exit_line_names_the_missing_raw_input(caplog):
    """원재료가 끊긴 경우 — **어느 필드**인지까지 말한다."""
    gone = SignalInputs(
        regime_state=_regime_state(), gex=-1000.0, gamma_flip=100.0, spot=105.0,
        ofi=5.0, queue_imbalance=0.3,  # foreign_net_flow가 빠졌다
    )
    lines = _run(caplog, gone)

    assert len(lines) == 1
    assert "flow_position" in lines[0]
    assert "사유 원재료없음[foreign_net_flow]" in lines[0]


def test_exit_line_distinguishes_a_zero_sign_from_a_missing_input(caplog):
    """⛔ 이 파일의 핵심. `options_flow`는 **입력이 다 있어도** 스팟이 기준선과 정확히 같으면
    None이 된다 — 「고갈」과 전혀 다른 사건이고, 리포트의 세 값 어디에도 자리가 없었다."""
    flat = SignalInputs(
        regime_state=_regime_state(), gex=-1000.0, gamma_flip=100.0, spot=100.0,
        ofi=5.0, queue_imbalance=0.3, foreign_net_flow=500.0,
    )
    lines = _run(caplog, flat)

    assert len(lines) == 1
    assert "options_flow" in lines[0]
    assert "부호0[스팟−기준선(flip)]" in lines[0]
    assert "원재료없음" not in lines[0]


def test_orderflow_needs_both_inputs_gone(caplog):
    """`_orderflow_ofi_vpin_score`는 값이 있으면 부호가 0이어도 성분에 담는다 —
    그래서 이 축이 None이 되는 길은 **둘 다 없을 때** 하나뿐이다."""
    gone = SignalInputs(
        regime_state=_regime_state(), gex=-1000.0, gamma_flip=100.0, spot=105.0,
        foreign_net_flow=500.0,
    )
    lines = [line for line in _run(caplog, gone) if "orderflow_ofi_vpin" in line]

    assert len(lines) == 1
    assert "사유 원재료없음[ofi·queue_imbalance]" in lines[0]


def test_untrained_members_say_so():
    """Phase 3 전까지 항상 None인 두 축은 「끊겼다」가 아니라 「아직 없다」다."""
    assert member_absence_reason("xgboost_tabular", _full()) == "미학습"
    assert member_absence_reason("lstm_temporal", _full()) == "미학습"


def test_charm_is_not_counted_as_missing_before_1400():
    """Charm 성분은 14:00 이후에만 대상이다(v6 §13.2). 비활성 시간대의 부재를 결손으로 세면
    오전 내내 있지도 않은 결손이 로그에 남는다."""
    morning = SignalInputs(gex=-1000.0, gamma_flip=100.0, spot=100.0, charm_active=False)
    assert "total_charm" not in member_absence_reason("options_flow", morning)

    afternoon = SignalInputs(gex=-1000.0, gamma_flip=100.0, spot=100.0, charm_active=True)
    assert "total_charm" in member_absence_reason("options_flow", afternoon)


# ===== ② 사유는 점수 함수와 어긋날 수 없다 =====


@pytest.mark.parametrize(
    "inputs",
    [
        SignalInputs(),
        _full(),
        SignalInputs(regime_state=_regime_state()),
        SignalInputs(gex=-1.0, gamma_flip=100.0, spot=100.0),
        SignalInputs(gex=-1.0, gamma_wall=100.0, spot=105.0, charm_active=True, total_charm=0.0),
        SignalInputs(ofi=0.0),
        SignalInputs(queue_imbalance=-0.5, foreign_net_flow=0.0),
    ],
)
def test_reason_never_claims_a_cause_for_a_member_that_scored(inputs):
    """점수가 나온 축에는 사유가 붙을 일이 없다 — 붙으면 규칙이 두 곳에서 갈린 것이다.
    (이탈 줄은 점수가 None인 축에만 난다. 이 시험은 그 전제를 함수 층에서 못박는다.)"""
    scores = build_member_scores(inputs)
    for field in MEMBER_FIELDS:
        if getattr(scores, field) is not None:
            assert member_absence_reason(field, inputs) == "미상"


@pytest.mark.parametrize(
    "inputs",
    [
        SignalInputs(),
        SignalInputs(regime_state=_regime_state()),
        SignalInputs(gex=-1.0, gamma_flip=100.0, spot=100.0),
        SignalInputs(ofi=0.0),
    ],
)
def test_every_absent_member_gets_a_concrete_reason(inputs):
    """규약 C — 점수가 None인 축은 **반드시** 빈칸이 아닌 사유를 받는다."""
    scores = build_member_scores(inputs)
    for field in MEMBER_FIELDS:
        if getattr(scores, field) is None:
            reason = member_absence_reason(field, inputs)
            assert reason
            assert reason != "미상" or field in ("xgboost_tabular", "lstm_temporal")


# ===== ③ 판정 무변경 =====


def test_decision_is_bit_for_bit_unchanged(caplog):
    """⛔ 이 항목이 지키는 진짜 선. 사유는 **이미 계산된 사실을 문자열로 옮기는 것**이 전부다 —
    점수·가용·실질 멤버 수·허가·전략 어느 것도 움직이면 안 된다."""
    engine = SignalFusionEngine()
    with caplog.at_level(logging.INFO, logger="mahdi.fusion.engine"):
        first = engine.evaluate(_full(), MetaLabelContext())
        gone = SignalInputs(
            regime_state=_regime_state(), gex=-1000.0, gamma_flip=100.0, spot=105.0,
            ofi=5.0, queue_imbalance=0.3,
        )
        second = engine.evaluate(gone, MetaLabelContext())

    assert first.available_member_count == 4
    assert second.available_member_count == 3
    assert second.effective_member_count == first.effective_member_count - 1
    assert second.member_scores.flow_position is None
    # 남은 축의 점수는 한 글자도 안 바뀐다 — 사유 계산은 아무것도 되돌려 쓰지 않는다.
    for field in ("regime_hmm", "options_flow", "orderflow_ofi_vpin"):
        assert getattr(second.member_scores, field) == getattr(first.member_scores, field)


def test_return_line_carries_no_reason(caplog):
    """⛔ 복귀 줄에는 안 붙인다 — 복귀 시점의 원재료는 「지금 있다」라서 사유가 늘 `미상`이 된다."""
    engine = SignalFusionEngine()
    gone = SignalInputs(
        regime_state=_regime_state(), gex=-1000.0, gamma_flip=100.0, spot=105.0,
        ofi=5.0, queue_imbalance=0.3,
    )
    with caplog.at_level(logging.INFO, logger="mahdi.fusion.engine"):
        engine.evaluate(_full(), MetaLabelContext())
        engine.evaluate(gone, MetaLabelContext())
        caplog.clear()
        engine.evaluate(_full(), MetaLabelContext())

    returns = [r.getMessage() for r in caplog.records if "판단 축 복귀" in r.getMessage()]
    assert len(returns) == 1
    assert "사유" not in returns[0]


def test_parsers_still_count_the_line(caplog, collector):
    """08-04에 문구가 움직여 362건이 0건으로 보고된 자리다. 꼬리표는 **줄 끝에만** 붙으므로
    부분문자열로 세는 파서도, 앞머리를 읽는 수집기도 종전과 같은 값을 낸다."""
    gone = SignalInputs(
        regime_state=_regime_state(), gex=-1000.0, gamma_flip=100.0, spot=105.0,
        ofi=5.0, queue_imbalance=0.3,
    )
    line = _run(caplog, gone)[0]

    assert line.startswith("판단 축 이탈: ")
    assert log_metrics._QUALITATIVE_MARKERS["member_axis_exit"] in line
    # 괄호 안의 자리(가용·비영 전이)도 종전 그대로다.
    assert "(가용 4→3, 비영 4→3)" in line
    assert hasattr(collector, "MEMBER_TOKEN")
