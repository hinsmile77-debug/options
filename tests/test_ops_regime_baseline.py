"""HMM 재학습 기준선 박제 (2026-08-06 고도화#3).

박제의 가치는 **덮어쓰이지 않는 것**에 있다 — 재학습 후 숫자를 보고 기준선을 고치면
`hypotheses.yaml`의 예측을 실측 뒤에 고치는 것과 같은 자기기만이 된다.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from mahdi.ops import regime_baseline


# ===== 도달 예정일 =====


def test_eta_rounds_up_because_a_partial_day_does_not_reach_the_threshold():
    """2026-08-06 Fix#6과 같은 규약 — 일정은 낙관적으로 반올림하지 않는다.

    08-06 실측: (8000-7032)/391 = 2.48 → **3영업일**(08-10 월). 반올림하면 2가 나와
    08-05 보고서가 확정한 날짜와 어긋난다.
    """
    assert regime_baseline.eta_business_days(7032, 18, target_rows=8000) == 3


def test_eta_is_zero_once_the_threshold_is_reached():
    assert regime_baseline.eta_business_days(8000, 20, target_rows=8000) == 0
    assert regime_baseline.eta_business_days(9999, 25, target_rows=8000) == 0


def test_eta_does_not_divide_by_zero_on_an_empty_history():
    assert regime_baseline.eta_business_days(0, 0, target_rows=8000) == 0


def test_eta_matches_the_cockpit_badge_calculation():
    """규약 B — 배지와 박제가 다른 날짜를 내면 어느 쪽을 믿을지 알 수 없다."""
    import math

    from mahdi.dashboard.data_source import _REGIME_FIT_TARGET_ROWS

    total, days = 7422, 19
    badge_eta = math.ceil((_REGIME_FIT_TARGET_ROWS - total) / (total / days))
    assert regime_baseline.eta_business_days(total, days, _REGIME_FIT_TARGET_ROWS) == badge_eta


# ===== 렌더링 =====


def _baseline(**overrides) -> dict:
    base = {
        "captured_on": "2026-08-06",
        "underlying": "KOSPI200",
        "feature_version": "v1",
        "hmm_threshold": 8000,
        "feature_store": {
            "available": True, "total_rows": 7422, "distinct_days": 19,
            "rows_per_day": 390.6, "progress_pct": 92.8, "eta_business_days": 2,
        },
        "regime_states": {
            "available": True, "distinct_states": 1,
            "states": [{"regime": 2, "rows": 9000, "days": 23}],
        },
        "regime_hmm_scores": {
            "available": True, "scored_minutes": 399, "mean": 0.0,
            "bullish_minutes": 0, "bearish_minutes": 0, "neutral_minutes": 399,
            "neutral_pct": 100.0,
        },
    }
    base.update(overrides)
    return base


def test_render_carries_the_three_axes_that_decide_success():
    text = regime_baseline.render(_baseline())
    assert "7,422행" in text and "92.8%" in text          # 언제 돌릴 수 있는가
    assert "방문 상태 **1종**" in text                     # 갈렸는가
    assert "중립 **399**(100.0%)" in text                  # 판단이 달라졌는가


def test_render_survives_a_missing_score_axis():
    text = regime_baseline.render(
        _baseline(regime_hmm_scores={"available": False, "reason": "그날 기록 없음"})
    )
    assert "집계 불가" in text and "그날 기록 없음" in text


# ===== 덮어쓰기 금지 =====


class _StubConn:
    """`capture()`를 통째로 스텁하므로 커넥션은 쓰이지 않는다."""

    def rollback(self):
        pass


def test_capture_to_file_writes_json_and_markdown(tmp_path, monkeypatch):
    monkeypatch.setattr(regime_baseline, "capture", lambda *a, **k: _baseline())
    written = regime_baseline.capture_to_file(
        _StubConn(), date(2026, 8, 6), tmp_path / "regime_baseline_2026-08-06"
    )
    assert written.suffix == ".md"
    payload = json.loads((tmp_path / "regime_baseline_2026-08-06.json").read_text(encoding="utf-8"))
    assert payload["regime_hmm_scores"]["neutral_pct"] == 100.0


def test_capture_to_file_refuses_to_overwrite(tmp_path, monkeypatch):
    """**박제는 덮어쓰지 않는다.** 재학습 후 기준선을 고치면 비교가 성립하지 않는다."""
    monkeypatch.setattr(regime_baseline, "capture", lambda *a, **k: _baseline())
    path = tmp_path / "regime_baseline_2026-08-06"
    regime_baseline.capture_to_file(_StubConn(), date(2026, 8, 6), path)
    with pytest.raises(FileExistsError, match="덮어쓰지 않는다"):
        regime_baseline.capture_to_file(_StubConn(), date(2026, 8, 6), path)


def test_the_repository_baseline_exists_and_records_the_dead_axis():
    """리포지터리에 박제된 08-06 기준선이 §14-3 실측과 일치하는지 못박는다.

    이 파일이 사라지면 08-10 재학습 뒤에 "좋아졌다"를 말할 근거가 없어진다.
    """
    from mahdi.config.settings import PROJECT_ROOT

    path = PROJECT_ROOT / "docs" / "동작점검" / "regime_baseline_2026-08-06.json"
    assert path.exists(), "08-06 기준선 박제가 없다 — 재학습 전에 반드시 찍어야 한다"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["regime_states"]["distinct_states"] == 1
    assert payload["regime_hmm_scores"]["neutral_pct"] == 100.0
