"""Signal Fusion — `xgboost_tabular` 멤버 학습 스캐폴딩 (v6 §11.3).

`trade_history`가 0건이라 오늘은 실제로 학습시킬 수 없다 — 이 모듈은
`engines/regime.py`의 `RegimeEngine.save()`/`load()`와 같은 패턴으로, 데이터가 실제로
쌓였을 때 `scripts/fit_signal_fusion_meta_label.py`가 바로 쓸 수 있는 학습/저장/로드
파이프라인만 미리 갖춰둔다. `trade_history` 스키마에 이미 있는 컬럼(`regime_entry`,
`confidence_entry`, `net_pnl`)만 피처/레이블로 쓴다 — v6 §11.2가 실제로 요구하는 메타
모델 입력(신호 동조 개수/슬리피지/감마 레짐/외국인 플로우 정합성/이벤트 근접도)까지 담을
피처 저장소(예: `prediction_logs.signal_features`) 설계는 Phase 3(자기강화 루프) 범위로
미룬다 — 이 모듈은 그 전까지 쓸 수 있는 최소 스캐폴딩이다.

`xgboost_tabular`라는 앙상블 멤버 이름과 달리 실제 구현은 XGBoost가 아니라 이미 설치된
scikit-learn `GradientBoostingClassifier`를 쓴다(신규 의존성 도입 없음, [[DECISION_LOG]]
2026-07-28 2차 항목 참고) — 이름은 v6 §11.3 설정 키를 그대로 유지한다.
"""

from __future__ import annotations

import pickle
from pathlib import Path

import numpy as np
from sklearn.ensemble import GradientBoostingClassifier

from mahdi.engines.regime import RegimeLabel

N_REGIME_LABELS = len(RegimeLabel)


def build_training_matrix(trade_rows: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    """
    입력: `db.get_trade_history()`가 반환한 dict 목록(regime_entry/confidence_entry/net_pnl).
    계산: 피처=[regime_entry 원-핫(8차원), confidence_entry] 총 9차원, 레이블=net_pnl>0이면 1,
         아니면 0.
    실패 조건: trade_rows가 비어있으면 빈 배열 쌍(0 x 9, 0).
    """
    if not trade_rows:
        return np.empty((0, N_REGIME_LABELS + 1)), np.empty((0,))

    features = []
    labels = []
    for row in trade_rows:
        one_hot = [0.0] * N_REGIME_LABELS
        regime_entry = row["regime_entry"]
        if 0 <= regime_entry < N_REGIME_LABELS:
            one_hot[regime_entry] = 1.0
        features.append(one_hot + [row["confidence_entry"]])
        labels.append(1.0 if row["net_pnl"] > 0 else 0.0)

    return np.array(features, dtype=float), np.array(labels, dtype=float)


def train_tabular_classifier(X: np.ndarray, y: np.ndarray) -> GradientBoostingClassifier:
    """
    계산: scikit-learn `GradientBoostingClassifier`를 기본 하이퍼파라미터로 학습한다 —
         하이퍼파라미터 튜닝은 실제 데이터로 검증할 수 있게 되는 시점(Phase 3)에 다룬다.
    실패 조건: y에 클래스가 1종류뿐이면(전부 승 또는 전부 패) scikit-learn이 자체적으로
              ValueError를 던질 수 있다 — 호출측(스크립트)이 데이터 품질 문제로 처리해야 한다.
    """
    model = GradientBoostingClassifier(random_state=42)
    model.fit(X, y)
    return model


def save(model: GradientBoostingClassifier, path: str | Path) -> None:
    """`RegimeEngine.save()`와 동일한 pickle 패턴."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(model, f)


def load(path: str | Path) -> GradientBoostingClassifier:
    """실패 조건: 파일이 없으면 FileNotFoundError(호출측이 폴백 처리)."""
    with open(path, "rb") as f:
        return pickle.load(f)
