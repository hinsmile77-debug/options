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
import collections
import logging
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mahdi.data import db
from mahdi.engines.regime import FEATURE_NAMES, RegimeEngine, convergence_report
from mahdi.engines.regime_pipeline import (
    DEFAULT_MODEL_PATH,
    FEATURE_VERSION,
    _IV_WINDOW_MINUTES,
)

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


def reconstruct_iv_chg(conn, underlying: str) -> dict[datetime, float]:
    """
    입력: DB 커넥션, underlying 라벨.
    계산: `option_analysis_1m` 원본 체인에서 분마다 **먼슬리 단독 ATM IV**를 구하고
         (`options_intel.monthly_atm_iv()` — 라이브 `main._update_atm_iv()`와 **같은 함수**),
         라이브와 같은 30분 롤링 창으로 `iv_change_rate()`를 다시 계산해 {시각: iv_chg}로 돌려준다.
    해석: 2026-08-10 — `feature_store.iv_chg`가 북 혼합으로 오염돼 있다(짝수분 0.5285 /
         홀수분 0.7387, 차분 자기상관 −0.900). 그대로 학습하면 HMM이 **폴링 격자를 레짐으로**
         배운다(실측: 짝수/홀수분 94% 일치, 재생 시 분당 99.2% 레짐 전이).

         **DB는 고치지 않는다**(사용자 결정, 2026-08-10). 그날 실제로 계산된 값이 무엇이었는지는
         기록으로 남아야 하므로 `feature_store`는 그대로 두고 **학습 시점에만** 이 값으로 대체한다.
         대체 사실은 로그·모델 메타데이터·운영 리포트 세 곳에 남는다 — 학습이 쓴 값과 DB가 기록한
         값이 다르다는 것은 **조용히 넘어가면 안 되는 사실**이다.

         **세션 경계에서 창을 끊는 이유**: 라이브의 IV 창은 `RegimeFeatureBuilder`의 deque이고
         프로세스가 매일 재기동되므로 세션에서 끊긴다. 여기서 안 끊으면 전날 마지막 IV가 오늘 첫
         `iv_chg`에 들어가 **라이브에 없는 값**이 학습에 섞인다.

         **스팟 소스가 라이브와 다르다**: 라이브는 체인 폴링이 돌려준 `latest_spot`을 쓰고 여기서는
         `underlying_spot_1m`을 쓴다(그 분의 폴링 응답은 보존돼 있지 않다). ATM은 행사가 격자
         2.5p 단위라 이 차이가 ATM 선택을 바꾸는 경우는 드물지만, **0은 아니다.**
    실패 조건: 없음 — ATM IV를 못 구한 분은 결과에서 빠지고, 호출측이 그 분만 DB 값으로 남긴다.
    """
    from mahdi.features.options_intel import monthly_atm_iv
    from mahdi.features.regime_features import iv_change_rate

    out: dict[datetime, float] = {}
    window: collections.deque[float] = collections.deque(maxlen=_IV_WINDOW_MINUTES)
    session = None
    for timestamp, chain_rows, spot in db.get_chain_minutes_for_regime_fit(conn, underlying):
        if timestamp.date() != session:
            session = timestamp.date()
            window.clear()
        atm_iv = monthly_atm_iv(chain_rows, spot)
        if atm_iv is None:
            continue
        window.append(atm_iv)
        out[timestamp] = iv_change_rate(list(window))
    return out


def build_feature_matrix(
    history: list[tuple[datetime, dict]], iv_chg_override: dict[datetime, float] | None = None
) -> tuple[np.ndarray, list[int]]:
    """
    입력: db.get_feature_history()가 반환한 (timestamp, features dict) 목록.
    계산: FEATURE_NAMES 순서로 정렬한 (n, 6) ndarray와 **세션(거래일)별 행 수** `lengths`를
         만든다. 값이 하나라도 없는 행, 그리고 비유한값(NaN/inf)이나 `_MAX_ABS_FEATURE_VALUE`를
         넘는 값이 섞인 행은 제외한다(제외 건수는 `describe_feature_matrix()`가 보고하므로
         조용히 사라지지 않는다).
    해석: 2026-08-10 — `lengths`가 이 함수에서 나와야 하는 이유는 **필터가 여기 있기 때문**이다.
         원본 이력의 날짜로 세션을 세면 필터로 빠진 행(08-10 실측 19행)만큼 합이 어긋나
         `RegimeEngine.fit()`이 ValueError를 낸다. **살아남은 행만** 날짜로 묶어야 한다.
         `lengths`가 왜 필요한지는 `RegimeEngine.fit()` docstring 참고(startprob one-hot 붕괴).
    """
    iv_index = FEATURE_NAMES.index("iv_chg")
    rows: list[list[float]] = []
    lengths: list[int] = []
    current_date = None
    for timestamp, features in history:
        try:
            row = [float(features[name]) for name in FEATURE_NAMES]
        except (KeyError, TypeError, ValueError):
            continue
        if iv_chg_override is not None and timestamp in iv_chg_override:
            # 2026-08-10 — DB가 기록한 값을 **읽기만 하고** 학습 입력에서만 갈아 끼운다.
            # 대체 못 한 분은 DB 값 그대로 남는다(조용한 결측보다 낫다 — 대체율을 호출측이 센다).
            row[iv_index] = iv_chg_override[timestamp]
        if not all(np.isfinite(v) and abs(v) <= _MAX_ABS_FEATURE_VALUE for v in row):
            continue
        if timestamp.date() != current_date:
            current_date = timestamp.date()
            lengths.append(0)
        lengths[-1] += 1
        rows.append(row)
    return np.array(rows, dtype=float), lengths


def split_sessions(features: np.ndarray, lengths: list[int]) -> list[np.ndarray]:
    """
    입력: `build_feature_matrix()`가 낸 행렬과 세션별 행 수.
    계산: 행렬을 세션 경계로 잘라 (n_i, 6) 배열 목록으로 만든다.
    해석: 저장 게이트의 라이브 리플레이(`regime_pipeline.replay_live_predictions`)가 세션 단위로
         창을 만들기 때문에 필요하다 — 세션을 안 나누면 전날 마지막 봉이 오늘 첫 봉의 문맥이 되어
         라이브에 없는 정보가 들어간다(프로세스가 매일 재기동하므로 창은 세션에서 끊긴다).
    실패 조건: 없음 — `np.split`의 경계가 배열 밖이면 빈 조각이 나올 뿐이다.
    """
    bounds = np.cumsum(lengths)[:-1]
    return [chunk for chunk in np.split(features, bounds) if len(chunk)]


def describe_feature_matrix(
    history: list[tuple[datetime, dict]], features: np.ndarray, lengths: list[int] | None = None
) -> list[str]:
    """
    입력: 원본 이력과 `build_feature_matrix()` 결과(행렬, 그리고 선택적으로 세션별 행 수).
    계산: fit() 전에 사람이 봐야 할 진단을 문자열 목록으로 만든다 — 누락으로 버려진 행 수,
         **세션 수와 최소/최대 세션 길이**, 피처별 min/max/고유값 수, NaN/inf 개수.
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
    if lengths:
        # 2026-08-10 — 세션 수는 `startprob_` 추정의 **표본 수**다. 이 값이 1이면 그 지점에서
        # 이미 one-hot 붕괴가 예정돼 있다. 그리고 25영업일을 돌았는데 세션이 21개면 4일치가
        # 통째로 필터에 걸린 것이라, 그 사실이 여기 안 보이면 아무도 모른다(08-10 실측).
        lines.append(
            f"  세션(거래일) {len(lengths)}개 — 길이 min={min(lengths)} / max={max(lengths)} "
            f"/ 중앙={int(np.median(lengths))}"
        )
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


def check_live_diversity(engine, sessions: list[np.ndarray]) -> tuple[bool, str]:
    """
    입력: 캘리브레이션된 RegimeEngine, 세션별 피처 배열.
    계산: `regime_pipeline.replay_live_predictions()`로 **라이브와 같은 호출 방식**으로 학습
         데이터를 리플레이해, 방문 레짐이 2종 이상인지 본다.
    해석: 2026-08-10 — 종전 게이트는 전 이력을 한 시퀀스로 배치 Viterbi해 "잠재상태 8/8 방문"을
         확인했는데, 같은 모델을 라이브 축(길이 1 호출)으로 재면 **1/8**이었다. 재는 축이
         주장하는 축과 달라 통과시킨 것이다 — 운영점검 규약 F가 지표에 대해 말하는 것을 코드
         게이트에 적용한 것이 이 함수다.

         **이 게이트가 지키는 것은 「학습 결함」이 아니라 「호출 경로」다.** 음성 대조로 확인했다:
         08-10에 저장됐던 붕괴 모델을 넣어도 `window=120`(현행 라이브)에서는 8종이 나와 **통과**하고,
         `window=1`(R3 회귀 = 그날의 실제 라이브)에서는 1종이 나와 **거부**한다. 즉
           - `check_startprob()` → 학습 쪽 결함(`lengths` 누락)을 잡는다. 08-10 모델을 여기서 잡았다.
           - 이 함수      → **라이브 호출부가 창을 잃어버리는 회귀**를 잡는다.
         둘은 서로 다른 실패 모드를 덮으므로 **둘 다 있어야 한다.** 어느 하나로 다른 쪽을
         대신하려 들면 08-10처럼 "한 축만 재고 통과"가 다시 생긴다.
         리플레이 구현이 `regime_pipeline`에 있는 이유는 그쪽 docstring 참고(축 동기화).
    실패 조건: 없음 — (통과 여부, 사유)를 돌려준다. 리플레이할 분이 없으면 실패로 본다.
    """
    from mahdi.engines.regime_pipeline import replay_live_predictions

    labels = replay_live_predictions(engine, sessions)
    if not labels:
        return False, "리플레이할 분이 없다 — 모든 세션이 워밍업 길이 이하다"
    counts = collections.Counter(label.name for label in labels)
    summary = ", ".join(f"{name} {n}분({n / len(labels) * 100:.1f}%)" for name, n in counts.most_common())
    if len(counts) < 2:
        return False, f"라이브 호출 방식에서 레짐이 {len(counts)}종뿐이다 — 사실상 고정 출력이다. {summary}"
    return True, f"라이브 리플레이 {len(labels)}분 / 방문 {len(counts)}종 — {summary}"


def check_startprob(model) -> tuple[bool, str]:
    """
    입력: 학습된 GaussianHMM.
    계산: `startprob_`의 비영(>1e-12) 성분이 2개 이상인지.
    해석: 2026-08-10 — `check_live_diversity()`가 이 결함을 포섭하지만(붕괴하면 리플레이도 1종),
         **실패 원인을 즉답하기 위해** 따로 둔다. 리플레이 실패만 보면 "모델이 나쁜가 데이터가
         나쁜가"를 다시 조사해야 하는데, 이 줄이 있으면 `lengths`를 안 넘겼다는 것이 바로 보인다.
    실패 조건: 없음 — (통과 여부, 사유)를 돌려준다.
    """
    startprob = getattr(model, "startprob_", None)
    if startprob is None:
        return False, "startprob_를 읽지 못했다"
    nonzero = int((np.asarray(startprob) > 1e-12).sum())
    if nonzero < 2:
        return False, (
            f"startprob_ 비영 성분이 {nonzero}개다 — 세션 경계(lengths) 없이 학습하면 생기는 "
            "one-hot 붕괴다. 길이 1 호출에서 이 모델은 상수를 낸다(2026-08-10)"
        )
    return True, f"startprob_ 비영 {nonzero}/{len(startprob)}개"


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
            "저장 게이트(수렴/startprob/라이브 리플레이)가 실패해도 저장한다"
            "(2026-08-07 고도화#4, 2026-08-10 재구축). 기본값은 저장 거부 — "
            "나쁜 모델을 저장하면 그날부터 모든 레짐 판단이 그것을 믿는다"
        ),
    )
    parser.add_argument(
        "--raw-iv-chg", action="store_true",
        help=(
            "iv_chg를 재계산하지 않고 feature_store 값을 그대로 쓴다(2026-08-10 이전 동작 재현용). "
            "그 값은 북 혼합으로 분 단위 구형파라 HMM이 폴링 격자를 레짐으로 배운다 — "
            "재현·비교 목적이 아니면 쓰지 말 것"
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
        # 2026-08-10 — `iv_chg`를 먼슬리 단독으로 재계산해 **학습 입력에서만** 갈아 끼운다.
        # `feature_store`(DB)는 손대지 않는다 — 사유는 `reconstruct_iv_chg()` docstring.
        iv_chg_override = None if args.raw_iv_chg else reconstruct_iv_chg(conn, args.underlying)

    features, lengths = build_feature_matrix(history, iv_chg_override)
    for line in describe_feature_matrix(history, features, lengths):
        logger.info("%s", line)

    # 대체 사실을 **로그에 남긴다**(리포트·모델 메타데이터와 함께 세 곳 중 하나).
    if iv_chg_override is None:
        iv_chg_source = "feature_store 원본(--raw-iv-chg)"
        logger.warning(
            "iv_chg를 DB 원본 그대로 쓴다 — 그 값은 북 혼합으로 분 단위 구형파다"
            "(2026-08-10). HMM이 폴링 격자를 레짐으로 배울 수 있다."
        )
    else:
        covered = sum(1 for timestamp, _ in history if timestamp in iv_chg_override)
        pct = covered / len(history) * 100 if history else 0.0
        iv_chg_source = f"먼슬리 단독 재계산(이력 {covered}/{len(history)}행, {pct:.1f}%)"
        logger.info(
            "iv_chg 재계산: 이력 %d행 중 %d행(%.1f%%)을 먼슬리 단독 ATM IV로 대체했다 — "
            "**DB(feature_store)는 고치지 않았다.** 나머지 %d행은 DB 값 그대로다(그 분에 체인/스팟이 없다)",
            len(history), covered, pct, len(history) - covered,
        )
    if features.size == 0:
        logger.error("feature_store에 축적된 피처가 없습니다 — 실시간 파이프라인이 먼저 돌아야 합니다")
        return

    if len(features) < args.min_samples:
        logger.warning(
            "샘플 수(%d)가 권장 최소치(%d) 미만입니다 — fit()은 진행하지만 결과 신뢰도가 낮을 수 있습니다",
            len(features), args.min_samples,
        )

    engine = RegimeEngine()
    engine.fit(features, lengths)
    sessions = split_sessions(features, lengths)

    # 2026-08-03 §4 우선순위 6 — **"fit()이 안 죽었다"는 성공 기준이 아니다.**
    #
    # 2026-08-10 정정: 이 줄(배치 Viterbi 방문 상태)은 **참고로만 남긴다.** 08-10에 이 값이
    # "8/8 방문"을 냈는데 같은 모델을 라이브 축으로 재면 1/8이었다 — 판정을 여기에 걸면 안 된다.
    # 실제 게이트는 아래 `check_live_diversity()`다.
    visited = np.unique(engine.model.predict(features, lengths))
    logger.info(
        "잠재상태 방문(배치 Viterbi, 참고용): %d/%d개 %s",
        len(visited), engine.model.n_components, visited.tolist(),
    )

    # 2026-08-07 고도화#4 → 2026-08-10 재구축. 저장 게이트 3겹. 각 함수의 docstring에 근거가 있다.
    #
    # 저장을 막는 쪽을 기본값으로 둔다: 안 저장하면 WARMUP 폴백이 하루 더 이어질 뿐이지만
    # (08-06까지 24영업일 그랬다), 나쁜 모델을 저장하면 그날부터 판단이 그것을 믿는다.
    # 억지로 저장해야 할 근거가 생기면 `--allow-nonconverged`로 **명시적으로** 넘긴다.
    gates = [
        # 수렴 판정은 `regime.convergence_report()` — `RegimeEngine.fit()`의 후보 선택과
        # **같은 함수**다. 두 곳이 다른 기준을 쓰면 어느 쪽이 옳은지 알 수 없다.
        ("수렴", *convergence_report(engine.model)),
        ("startprob", *check_startprob(engine.model)),
        ("라이브 리플레이", *check_live_diversity(engine, sessions)),
    ]
    for name, ok, reason in gates:
        (logger.info if ok else logger.warning)("게이트[%s] %s — %s", name, "통과" if ok else "실패", reason)
    failed = [name for name, ok, _ in gates if not ok]

    if args.dry_run:
        # 저장하지 않는다 — 드라이런의 목적은 "기계적 오류가 없는지"이지 모델을 만드는 게 아니다.
        # 지금 저장하면 RegimeStateMachine이 그 파일을 보고 다음 기동부터 predict()로 전환한다.
        logger.info(
            "드라이런 완료(%d개 샘플, 세션 %d개) — 게이트 실패 %s. 모델은 저장하지 않았다(%s).",
            len(features), len(sessions), failed or "없음", args.model_path,
        )
        return
    if failed and not args.allow_nonconverged:
        logger.error(
            "저장 게이트 실패(%s) — 저장하지 않는다. 저장하면 그날부터 모든 레짐 판단이 이 모델에서 "
            "나온다(v6 §16.1). 근거를 적고 그래도 저장하려면 --allow-nonconverged.",
            ", ".join(failed),
        )
        return
    # 학습 출처를 모델 **안에** 싣는다 — 로그는 지워지지만 pickle은 남는다(`save()` docstring).
    engine.save(args.model_path, metadata={
        "trained_at": db.local_now().isoformat(timespec="seconds"),
        "samples": len(features),
        "sessions": len(sessions),
        "feature_version": args.feature_version,
        "iv_chg_source": iv_chg_source,
        "db_rows_modified": False,  # feature_store는 절대 고치지 않는다(2026-08-10 사용자 결정)
    })
    logger.info(
        "RegimeEngine 캘리브레이션 완료(%d개 샘플, 세션 %d개, iv_chg=%s) — 저장 경로: %s",
        len(features), len(sessions), iv_chg_source, args.model_path,
    )


if __name__ == "__main__":
    main()
