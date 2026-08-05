"""지표끼리 맞춰보는 규칙 — 2026-08-05 고도화#3.

## 이 파일이 존재하는 이유

08-05 리포트는 같은 문서 안에서 논리적으로 모순되는 두 답을 냈다:

  §14  감마플립 산출률 4.5% (22/494분)
  §15  광폭 감마플립 — 세 북 모두 **없음**

광폭으로 훑어도 부호 전환이 없는데 좁은 창을 쓰는 라이브 판단이 22번 flip을 냈다면 그것은
시장 구조가 아니라 계산 결함이다. 전수 대조 결과 21건이 레그 범위 밖 외삽이었다(§2-5) —
**그 대조를 사람이 손으로 하기 전까지 아무도 몰랐다.** 리포트는 두 값을 나란히 인쇄해 놓고
서로 비교하지는 않았다.
"""

from __future__ import annotations

from mahdi.ops import crosscheck


def _ids(findings) -> set[str]:
    return {f.id for f in findings}


# ===== §14 ↔ §15 감마플립 =====


def _flip_contradiction_db(flip_count: int = 22, wide_flip=None) -> dict:
    return {
        "signal_reach": {
            "gamma_flip_count": flip_count, "gamma_flip_pct": 4.5,
            "gamma_flip_out_of_range_count": 21,
        },
        "wide_oi_landscape": [
            {"expiry": "2026-08-06", "wide_gamma_flip": wide_flip},
            {"expiry": "2026-08-13", "wide_gamma_flip": wide_flip},
        ],
    }


def test_live_flip_without_a_wide_flip_is_a_contradiction():
    """**08-05을 그대로 재현하는 테스트다.** 이 한 줄이 있었으면 §2-5는 손 대조 전에 잡혔다."""
    findings = crosscheck.evaluate({}, _flip_contradiction_db())

    assert "gamma-flip-vs-wide-landscape" in _ids(findings)
    [f] = [x for x in findings if x.id == "gamma-flip-vs-wide-landscape"]
    assert f.sections == ("14", "15")
    assert "22분" in f.summary
    assert "21건" in f.detail, "범위 밖 건수를 증거로 인용해야 사람이 바로 확인할 수 있다"


def test_no_contradiction_when_the_wide_search_also_finds_a_flip():
    """광폭에도 flip이 있으면 라이브 flip은 정상이다 — 오탐을 내지 않는다."""
    findings = crosscheck.evaluate({}, _flip_contradiction_db(wide_flip=1002.5))
    assert "gamma-flip-vs-wide-landscape" not in _ids(findings)


def test_no_contradiction_when_the_live_flip_count_is_zero():
    """08-04처럼 라이브 flip이 0이고 광폭도 없음이면 두 지표는 **일치**한다."""
    findings = crosscheck.evaluate({}, _flip_contradiction_db(flip_count=0))
    assert "gamma-flip-vs-wide-landscape" not in _ids(findings)


# ===== §12 커버리지 ↔ 레그 두께 =====


def test_high_coverage_with_thin_legs_is_a_contradiction():
    """커버리지 98.8%인데 레그 10개 미만이 38.2% — "있다"와 "충분하다"는 다르다(08-05 실측)."""
    db = {
        "monthly_coverage": {"coverage_pct": 98.8},
        "monthly_leg_completeness": {
            "below_design_pct": 38.2, "design_legs": 10, "below_flip_minimum_count": 18,
        },
    }
    findings = crosscheck.evaluate({}, db)

    assert "coverage-vs-leg-thickness" in _ids(findings)


def test_high_coverage_with_full_legs_is_not_flagged():
    db = {
        "monthly_coverage": {"coverage_pct": 98.8},
        "monthly_leg_completeness": {
            "below_design_pct": 4.0, "design_legs": 10, "below_flip_minimum_count": 0,
        },
    }
    assert "coverage-vs-leg-thickness" not in _ids(crosscheck.evaluate({}, db))


# ===== §6 백오프 ↔ §9-1 KIS 지연 =====


def test_low_backoff_with_kis_latency_warnings_attributes_to_kis():
    """08-04 §2-6이 미리 적어둔 판정표의 자동화 — 페이서가 한가한데 타임아웃이 나면 KIS 귀속."""
    metrics = {
        "backoff": {"max_multiplier": 2.25},
        "rest_latency": {"warnings": ["9시 inquire-price 2.95초"] * 6},
        "qualitative": {"read_timeout": 210},
    }
    findings = crosscheck.evaluate(metrics, {})

    assert "backoff-vs-kis-latency" in _ids(findings)


def test_high_backoff_is_our_own_congestion_not_kis():
    """백오프가 높으면 우리 쪽 큐 경합이 섞여 있으므로 KIS 귀속으로 단정하지 않는다."""
    metrics = {
        "backoff": {"max_multiplier": 4.0},
        "rest_latency": {"warnings": ["x"]},
        "qualitative": {"read_timeout": 210},
    }
    assert "backoff-vs-kis-latency" not in _ids(crosscheck.evaluate(metrics, {}))


# ===== 계측 전 / 빈 입력 =====


def test_missing_inputs_produce_no_findings_instead_of_false_alarms():
    """계측이 아직 안 붙은 날(키 자체가 없음)에 거짓 모순을 만들지 않는다."""
    assert crosscheck.evaluate({}, {}) == []
    assert crosscheck.evaluate(None, None) == []


def test_evaluate_is_pure_and_does_not_mutate_its_inputs():
    metrics = {"backoff": {"max_multiplier": 2.25}}
    db = _flip_contradiction_db()
    before = (repr(metrics), repr(db))
    crosscheck.evaluate(metrics, db)
    assert (repr(metrics), repr(db)) == before
