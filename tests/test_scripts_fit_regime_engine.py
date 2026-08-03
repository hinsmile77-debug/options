"""`scripts/fit_regime_engine.py`의 학습 입력 위생 — 2026-08-03 §4 우선순위 6.

08-10 임계 도달 전 드라이런에서 발견한 것: `fit()`이 **예외 없이 끝나면서도** 6회 재시작 전부
비수렴(로그우도 -2.2e22)이었다. 원인은 `feature_store`에 남아 있던 z-score 폭발 이력이었고,
`_series_zscore()` 쪽은 고쳤지만 **이미 쌓인 과거 행은 그대로 남는다**(DB를 고쳐 쓰지 않는다 —
그날 실제로 계산된 값이 무엇이었는지는 기록으로 남아야 한다). 그래서 학습 입력에서 거른다.
"""

from datetime import datetime

import numpy as np

from scripts.fit_regime_engine import build_feature_matrix, describe_feature_matrix


def _features(**overrides) -> dict:
    base = {
        "hurst": 0.5, "adx": 25.0, "rv_ratio": 1.1,
        "iv_chg": 0.02, "cross_asset_stress": 0.8, "book_thinning": 1.2,
    }
    base.update(overrides)
    return base


def _history(*feature_dicts) -> list[tuple[datetime, dict]]:
    return [(datetime(2026, 8, 3, 9, i), f) for i, f in enumerate(feature_dicts)]


def test_build_feature_matrix_keeps_ordinary_rows():
    matrix = build_feature_matrix(_history(_features(), _features(adx=30.0)))
    assert matrix.shape == (2, 6)


def test_build_feature_matrix_drops_pre_clamp_zscore_outliers():
    # 08-03 실측 값 — cross_asset_stress 1.05e12 (클램프 도입 이전에 계산된 행).
    matrix = build_feature_matrix(_history(_features(), _features(cross_asset_stress=1.05e12)))
    assert matrix.shape == (1, 6)


def test_build_feature_matrix_drops_non_finite_rows():
    matrix = build_feature_matrix(_history(_features(), _features(book_thinning=float("nan"))))
    assert matrix.shape == (1, 6)


def test_build_feature_matrix_drops_rows_missing_a_feature():
    incomplete = _features()
    del incomplete["hurst"]
    assert build_feature_matrix(_history(_features(), incomplete)).shape == (1, 6)


def test_build_feature_matrix_admits_the_widest_legitimate_feature_value():
    # 임계(100)의 근거는 "현행 정의가 만들 수 있는 가장 넓은 범위 = adx(0~100)" 이다 —
    # 정상적으로 계산된 값은 하나도 거르지 않아야 한다.
    assert build_feature_matrix(_history(_features(adx=100.0))).shape == (1, 6)


def test_describe_reports_dropped_rows_so_they_do_not_vanish_silently():
    history = _history(_features(), _features(cross_asset_stress=1.05e12))
    lines = describe_feature_matrix(history, build_feature_matrix(history))
    assert "제외 1행" in lines[0]


def test_describe_flags_zero_variance_features():
    # rv_ratio가 정확히 이 상태였다(410건 전부 1.0) — 분산 0이면 HMM 공분산 추정이 무너진다.
    history = _history(_features(adx=10.0), _features(adx=20.0), _features(adx=30.0))
    lines = describe_feature_matrix(history, build_feature_matrix(history))
    rv_line = next(line for line in lines if "rv_ratio" in line)
    assert "고유값 1개" in rv_line
    adx_line = next(line for line in lines if "adx" in line)
    assert "고유값 1개" not in adx_line


def test_describe_handles_empty_matrix():
    assert describe_feature_matrix([], np.array([])) == [
        "이력 0행 → 행렬 0행 (누락·비유한값·범위초과로 제외 0행)"
    ]
