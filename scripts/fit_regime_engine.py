"""오프라인 배치 — feature_store에 축적된 §7.3 피처 이력으로 RegimeEngine을 캘리브레이션한다.

실시간 프로세스(mahdi/main.py)는 이 스크립트가 만든 data/models/regime_engine.pkl의 존재
여부만 보고 warmup_fallback() 대신 predict()를 쓸지 자동으로 판단한다(RegimeStateMachine).
수십 세션 분량이 쌓인 뒤 사람이 수동으로(또는 주기적 스케줄로) 실행하는 배치다 — main.py가
매번 refit하지 않는다.

실행: python scripts/fit_regime_engine.py [--underlying KOSPI200] [--min-samples 5000]
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mahdi.data import db
from mahdi.engines.regime import FEATURE_NAMES, RegimeEngine
from mahdi.engines.regime_pipeline import DEFAULT_MODEL_PATH, FEATURE_VERSION

logger = logging.getLogger("mahdi.fit_regime_engine")

# 스펙(regime.py fit() 주석)이 권고하는 "최소 수십 세션" 근사치 — 정규장 1세션이 대략
# 09:00~15:45(405분)이므로, 20세션 ≈ 8,100행. 미달이어도 강제 차단은 안 하고 경고만 한다
# (사용자가 판단할 문제 — 데이터가 적을수록 fit() 결과 신뢰도가 낮아질 뿐).
DEFAULT_MIN_SAMPLES = 8000


def build_feature_matrix(history: list[tuple[datetime, dict]]) -> np.ndarray:
    """
    입력: db.get_feature_history()가 반환한 (timestamp, features dict) 목록.
    계산: FEATURE_NAMES 순서로 정렬한 (n, 6) ndarray를 만든다. 값이 하나라도 없는 행은 제외.
    """
    rows = []
    for _timestamp, features in history:
        try:
            rows.append([features[name] for name in FEATURE_NAMES])
        except KeyError:
            continue
    return np.array(rows, dtype=float)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--underlying", default="KOSPI200")
    parser.add_argument("--feature-version", default=FEATURE_VERSION)
    parser.add_argument("--min-samples", type=int, default=DEFAULT_MIN_SAMPLES)
    parser.add_argument("--model-path", default=str(DEFAULT_MODEL_PATH))
    args = parser.parse_args()

    with db.get_connection() as conn:
        history = db.get_feature_history(conn, args.underlying, args.feature_version)

    features = build_feature_matrix(history)
    if features.size == 0:
        logger.error("feature_store에 축적된 피처가 없습니다 — 실시간 파이프라인이 먼저 돌아야 합니다")
        return

    if len(features) < args.min_samples:
        logger.warning(
            "샘플 수(%d)가 권장 최소치(%d) 미만입니다 — fit()은 진행하지만 결과 신뢰도가 낮을 수 있습니다",
            len(features), args.min_samples,
        )

    engine = RegimeEngine()
    engine.fit(features)
    engine.save(args.model_path)
    logger.info("RegimeEngine 캘리브레이션 완료(%d개 샘플) — 저장 경로: %s", len(features), args.model_path)


if __name__ == "__main__":
    main()
