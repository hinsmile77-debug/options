"""오프라인 배치 — `trade_history`에 축적된 거래 결과로 Signal Fusion의 `xgboost_tabular`
앙상블 멤버를 학습한다.

**2026-07-28 2차 시점: `trade_history`가 0건이라 오늘 실행하면 "데이터 없음" 에러만 남기고
종료한다 — 이게 기대된 동작이다.** 아직 main.py가 실주문을 내지 않아(Signal Fusion 라이브
배선은 ADVISORY 전용, [[DECISION_LOG]] 참고) `trade_history`에 쓰는 곳 자체가 없다. 실주문
실행(다음 증분)이 생겨 거래가 쌓인 뒤 사람이 수동으로(또는 주기적 스케줄로) 실행하는 배치다.
`scripts/fit_regime_engine.py`와 완전히 같은 구조 — 데이터가 있으면 최소 표본 수 미달이어도
경고만 남기고 학습은 진행한다(강제 차단하지 않음, 판단은 사용자/Champion-Challenger 승격
절차의 몫).

실행: python scripts/fit_signal_fusion_meta_label.py [--strategy-id vrp_harvest] [--min-samples 200]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mahdi.data import db
from mahdi.fusion import trainer

logger = logging.getLogger("mahdi.fit_signal_fusion_meta_label")

# scikit-learn GradientBoostingClassifier가 과적합 없이 최소한의 일반화를 보이려면 대략 이
# 정도(전략당 수백 건 ~ 며칠~몇 주 운영)는 필요하다는 보수적 근사치 — regime_engine의
# 8,000행/20영업일 같은 엄밀한 근거는 아니고, 실제 데이터가 쌓이며 재조정할 시작값이다.
DEFAULT_MIN_SAMPLES = 200
DEFAULT_MODEL_PATH = Path("data/models/signal_fusion_tabular.pkl")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy-id", default=None)
    parser.add_argument("--min-samples", type=int, default=DEFAULT_MIN_SAMPLES)
    parser.add_argument("--model-path", default=str(DEFAULT_MODEL_PATH))
    args = parser.parse_args()

    with db.get_connection() as conn:
        trade_rows = db.get_trade_history(conn, strategy_id=args.strategy_id)

    if not trade_rows:
        logger.error("trade_history에 축적된 거래가 없습니다 — 실주문 실행이 먼저 쌓여야 합니다")
        return

    if len(trade_rows) < args.min_samples:
        logger.warning(
            "샘플 수(%d)가 권장 최소치(%d) 미만입니다 — 학습은 진행하지만 결과 신뢰도가 낮을 수 있습니다",
            len(trade_rows), args.min_samples,
        )

    X, y = trainer.build_training_matrix(trade_rows)
    model = trainer.train_tabular_classifier(X, y)
    trainer.save(model, args.model_path)
    logger.info("xgboost_tabular 멤버 학습 완료(%d개 샘플) — 저장 경로: %s", len(trade_rows), args.model_path)


if __name__ == "__main__":
    main()
