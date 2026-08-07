"""오프라인 배치 — feature_store에 축적된 §7.3 피처 이력으로 RegimeEngine을 캘리브레이션한다.

실시간 프로세스(mahdi/main.py)는 이 스크립트가 만든 data/models/regime_engine.pkl의 존재
여부만 보고 warmup_fallback() 대신 predict()를 쓸지 자동으로 판단한다(RegimeStateMachine).
수십 세션 분량이 쌓인 뒤 사람이 수동으로(또는 주기적 스케줄로) 실행하는 배치다 — main.py가
매번 refit하지 않는다.

실행: python scripts/fit_regime_engine.py [--underlying KOSPI200] [--min-samples 5000] [--dry-run]
      python scripts/fit_regime_engine.py --baseline   # 재학습 **전** 상태를 박제만 하고 끝낸다

`--dry-run`은 **모델을 저장하지 않고** 데이터 적재 → 행렬 구성 → fit()까지만 돌려본다
(2026-08-03 §4 우선순위 6, NEXT_TODO 이월 항목). 임계 도달일(08-10경)에 처음 실행해서
스키마 불일치·피처 순서·NaN 처리 같은 기계적 오류를 만나면 하루를 잃으므로, 그 전에 미리
같은 경로를 밟아 본다. 샘플 부족 경고는 드라이런에서 정상이며 실패로 치지 않는다.

`--baseline`은 **아무것도 학습하지 않는다** — 재학습 전 상태(피처 누적 / 레짐 상태 분포 /
`regime_hmm` 멤버 점수)를 `docs/동작점검/regime_baseline_YYYY-MM-DD.{json,md}`로 박제한다
(2026-08-06 고도화#3). 이것을 안 찍고 재학습하면 뒤에 "좋아졌다"를 말할 비교 대상이 없다.
상세 근거는 `mahdi/ops/regime_baseline.py`.
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


# 2026-08-03(§4 우선순위 6) — 학습 행렬이 받아들이는 피처 절대값 상한.
#
# `_series_zscore()`의 클램프(±10)는 **오늘 이후 새로 계산되는 값**에만 적용된다. 이미
# `feature_store`에 쌓인 이력에는 클램프 이전에 계산된 이상치가 남아 있다(08-03 실측: 6,213행 중
# `cross_asset_stress` 1e11~1e12가 13행, |book_thinning|>10이 9행). 대각 공분산 가우시안 HMM은
# 이런 값 하나로 EM이 발산하므로(드라이런에서 6회 재시작 전부 비수렴, 로그우도 -2.2e22) 학습
# 입력에서 제외한다. **DB의 과거 행을 고쳐 쓰지는 않는다** — 그날 실제로 계산된 값이 무엇이었는지는
# 기록으로 남아야 한다.
#
# 임계 100의 근거: 현행 피처 정의가 만들어낼 수 있는 **가장 넓은 범위가 adx(0~100)** 이다.
# z-score 계열(book_thinning, cross_asset_stress)은 이제 `_series_zscore()`가 ±10으로 자르고,
# hurst는 0~1, rv_ratio는 양수 비율, iv_chg는 변화율이다. 즉 이 임계는 **정상적으로 계산된 값은
# 하나도 거르지 않고** 클램프 도입 이전의 산물만 걸러낸다(08-03 실측: 6,213행 중 9행).
_MAX_ABS_FEATURE_VALUE = 100.0


def build_feature_matrix(history: list[tuple[datetime, dict]]) -> np.ndarray:
    """
    입력: db.get_feature_history()가 반환한 (timestamp, features dict) 목록.
    계산: FEATURE_NAMES 순서로 정렬한 (n, 6) ndarray를 만든다. 값이 하나라도 없는 행, 그리고
         비유한값(NaN/inf)이나 `_MAX_ABS_FEATURE_VALUE`를 넘는 값이 섞인 행은 제외한다
         (제외 건수는 `describe_feature_matrix()`가 보고하므로 조용히 사라지지 않는다).
    """
    rows = []
    for _timestamp, features in history:
        try:
            row = [float(features[name]) for name in FEATURE_NAMES]
        except (KeyError, TypeError, ValueError):
            continue
        if all(np.isfinite(v) and abs(v) <= _MAX_ABS_FEATURE_VALUE for v in row):
            rows.append(row)
    return np.array(rows, dtype=float)


def describe_feature_matrix(history: list[tuple[datetime, dict]], features: np.ndarray) -> list[str]:
    """
    입력: 원본 이력과 `build_feature_matrix()` 결과.
    계산: fit() 전에 사람이 봐야 할 진단을 문자열 목록으로 만든다 — 누락으로 버려진 행 수,
         피처별 min/max/고유값 수, NaN/inf 개수.
    해석: 2026-08-03 §4 우선순위 6. `build_feature_matrix()`가 KeyError 행을 **조용히 버리므로**,
         피처 이름이 하나 바뀌면 행렬이 빈 채로 "0개 샘플"만 남고 원인을 알 수 없다. 그리고
         고유값이 1개인 피처는 분산이 0이라 HMM 공분산 추정을 망가뜨린다 — 08-03 실측에서
         `rv_ratio`가 정확히 그 상태였다(410건 전부 1.0, 유효 종가 20일 < 임계 21일).
    실패 조건: 없음 — 순수 진단 함수.
    """
    lines = [
        f"이력 {len(history)}행 → 행렬 {len(features)}행 "
        f"(누락·비유한값·범위초과로 제외 {len(history) - len(features)}행)"
    ]
    if features.size == 0:
        return lines
    for i, name in enumerate(FEATURE_NAMES):
        column = features[:, i]
        finite = column[np.isfinite(column)]
        unique = len(np.unique(finite))
        flag = "  ⚠ 고유값 1개 — 분산 0, HMM 공분산 추정 불가" if unique <= 1 else ""
        lines.append(
            f"  {name:<20} min={finite.min():.6g} max={finite.max():.6g} "
            f"고유값={unique} 비유한값={len(column) - len(finite)}{flag}"
        )
    return lines


def _capture_baseline(args) -> None:
    """재학습 **전** 상태를 박제한다 — 이 스크립트는 얇게, 로직은 `mahdi/ops/regime_baseline.py`."""
    from mahdi.config.settings import PROJECT_ROOT
    from mahdi.ops import regime_baseline

    today = db.local_now().date()
    path = Path(args.baseline_path) if args.baseline_path else (
        PROJECT_ROOT / "docs" / "동작점검" / f"regime_baseline_{today.isoformat()}"
    )
    with db.get_connection() as conn:
        try:
            written = regime_baseline.capture_to_file(conn, today, path, args.underlying)
        except FileExistsError as exc:
            logger.error("%s", exc)
            return
    logger.info("HMM 재학습 기준선 박제 완료: %s (그리고 같은 이름의 .json)", written)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--underlying", default="KOSPI200")
    parser.add_argument("--feature-version", default=FEATURE_VERSION)
    parser.add_argument("--min-samples", type=int, default=DEFAULT_MIN_SAMPLES)
    parser.add_argument("--model-path", default=str(DEFAULT_MODEL_PATH))
    parser.add_argument(
        "--baseline", action="store_true",
        help="재학습 전 상태만 박제하고 끝낸다(2026-08-06 고도화#3, 학습은 하지 않는다)",
    )
    parser.add_argument(
        "--baseline-path", default=None,
        help="박제 경로(확장자 없이). 기본값은 docs/동작점검/regime_baseline_<오늘>",
    )
    parser.add_argument(
        "--allow-nonconverged", action="store_true",
        help=(
            "EM이 수렴하지 않아도 저장한다(2026-08-07 고도화#4). 기본값은 저장 거부 — "
            "수렴 안 한 모델을 저장하면 그날부터 모든 레짐 판단이 그것을 믿는다"
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="모델을 저장하지 않고 적재→행렬→fit()까지만 돌려본다(2026-08-03 §4 우선순위 6)",
    )
    args = parser.parse_args()

    if args.baseline:
        _capture_baseline(args)
        return

    with db.get_connection() as conn:
        history = db.get_feature_history(conn, args.underlying, args.feature_version)

    features = build_feature_matrix(history)
    for line in describe_feature_matrix(history, features):
        logger.info("%s", line)
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

    # 2026-08-03 §4 우선순위 6 — **"fit()이 안 죽었다"는 성공 기준이 아니다.** 08-10의 실제 성공
    # 기준은 `regime_state`에 RANGE_BALANCED 외의 값이 나오는 것이고, 그러려면 잠재상태가 실제로
    # 여러 개 방문돼야 한다. 드라이런 시점에는 방문 상태가 적은 것이 정상이지만(rv_ratio 분산 0 +
    # 이력 전체가 단일 레짐), 임계 도달일에도 1~2개면 데이터가 아니라 모델 구성을 의심해야 한다.
    visited = np.unique(engine.model.predict(features))
    logger.info(
        "잠재상태 방문: %d/%d개 %s", len(visited), engine.model.n_components, visited.tolist(),
    )
    if len(visited) <= 2:
        logger.warning(
            "방문 잠재상태가 %d개뿐이다 — 이대로 저장하면 레짐이 사실상 고정 출력이 된다"
            "(현재는 rv_ratio 분산 0 + 단일 레짐 이력 때문으로 보이며, 임계 도달일에 재확인할 것).",
            len(visited),
        )

    # 2026-08-07 고도화#4 — **드라이런이 찾아낸 것.** 08-07 실측(7,680샘플)에서 hmmlearn이
    # `Model is not converging` 4건을 냈다(로그우도 델타가 음수). `fit()`은 예외를 안 던지므로
    # 이대로 08-10에 저장했으면 **수렴하지 않은 모델이 조용히 라이브에 올라갔을 것**이다 —
    # 그리고 그 뒤의 모든 레짐 판단이 그 모델에서 나온다.
    #
    # 저장을 막는 쪽을 기본값으로 둔다: 안 저장하면 오늘 하루 WARMUP 폴백이 이어질 뿐이지만
    # (08-06까지 24영업일 그랬다), 나쁜 모델을 저장하면 그날부터 판단이 그것을 믿는다.
    # 억지로 저장해야 할 근거가 생기면 `--allow-nonconverged`로 **명시적으로** 넘긴다.
    converged = getattr(getattr(engine.model, "monitor_", None), "converged", None)
    if converged is False:
        logger.warning(
            "EM이 수렴하지 않았다(hmmlearn monitor_.converged=False) — n_iter(%d)를 늘리거나 "
            "피처 분산을 재점검할 것. 08-07 드라이런에서도 같은 경고가 났다.",
            engine.model.n_iter,
        )
    elif converged is None:
        logger.warning("수렴 여부를 읽지 못했다(hmmlearn 버전 차이?) — 저장 전 사람이 확인할 것")

    if args.dry_run:
        # 저장하지 않는다 — 드라이런의 목적은 "기계적 오류가 없는지"이지 모델을 만드는 게 아니다.
        # 지금 저장하면 RegimeStateMachine이 그 파일을 보고 곧바로 predict()로 전환해버린다.
        logger.info(
            "드라이런 성공(%d개 샘플) — fit()이 예외 없이 끝났다. 모델은 저장하지 않았다(%s).",
            len(features), args.model_path,
        )
        return
    if converged is not True and not args.allow_nonconverged:
        logger.error(
            "수렴하지 않은 모델은 저장하지 않는다 — 저장하면 그날부터 모든 레짐 판단이 이 모델에서 "
            "나온다. 안 저장하면 WARMUP 폴백이 하루 더 이어질 뿐이다(v6 §16.1). "
            "그래도 저장하려면 --allow-nonconverged."
        )
        return
    engine.save(args.model_path)
    logger.info("RegimeEngine 캘리브레이션 완료(%d개 샘플) — 저장 경로: %s", len(features), args.model_path)


if __name__ == "__main__":
    main()
