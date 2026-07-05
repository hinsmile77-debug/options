"""E1 Regime Intelligence — HMM(GaussianHMM, 8-state) + 매크로 나침반 워밍업 폴백 (v6 PART 7).

레짐은 모든 하위 엔진의 가중치 스위치다. §7.3 입력 목록에는 방향(상승/하락) 판별용 피처가
없으므로, v1은 hurst(추세성) 순위로 TREND/RANGE 계열을 구분하고 실제 방향은 상위 레이어
(Fusion)가 가격 모멘텀 등으로 별도 보정하는 것을 전제로 한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

import numpy as np
from hmmlearn.hmm import GaussianHMM


class RegimeLabel(IntEnum):
    """v6 §7.2 8-State 레짐 공간."""

    TREND_UP_STRONG = 0
    TREND_DOWN_STRONG = 1
    RANGE_BALANCED = 2
    RANGE_BREAK_PREP = 3
    VOL_EXPANSION = 4
    VOL_COMPRESSION = 5
    LIQUIDITY_THIN = 6
    CRISIS_DEFENSE = 7


@dataclass(frozen=True, slots=True)
class RegimeState:
    regime: RegimeLabel
    prob_vector: tuple[float, ...]          # 8차원, RegimeLabel 순서
    higher_tf_regime: RegimeLabel | None = None
    stability_flag: bool = True             # False → REGIME_UNSTABLE
    is_warmup: bool = False


# §7.3 입력 피처 순서: Hurst, ADX, RV5d/RV20d, ATM IV 변화율, Cross-asset stress, 호가 잔량 급감
FEATURE_NAMES = ("hurst", "adx", "rv_ratio", "iv_chg", "cross_asset_stress", "book_thinning")

_UNSTABLE_PROB_THRESHOLD = 0.40  # 최고 확률 상태가 이 값 미만이면 REGIME_UNSTABLE


class RegimeEngine:
    """GaussianHMM 기반 레짐 판별기. fit()으로 잠재상태<->RegimeLabel 매핑을 캘리브레이션한 뒤 predict() 사용."""

    def __init__(self, random_state: int = 42, n_restarts: int = 10, n_iter: int = 200) -> None:
        self.random_state = random_state
        self.n_restarts = n_restarts
        self.n_iter = n_iter
        self.model: GaussianHMM | None = None
        self._state_to_label: dict[int, RegimeLabel] | None = None
        self._fitted = False

    def fit(self, features: np.ndarray) -> None:
        """
        입력: (n_samples, 6) 배열, FEATURE_NAMES 순서. 최소 수십 세션 분량 권장.
        계산: EM은 초기화에 따라 지역해(일부 잠재상태 미사용)에 빠질 수 있어, 서로 다른
             random_state로 n_restarts회 학습 후 로그우도(score)가 가장 높은 모델을 채택한다.
             이후 잠재상태별 평균 피처값으로 8개 RegimeLabel에 결정론적으로 매핑한다
             (rv_ratio 최댓값→VOL_EXPANSION, 최솟값→VOL_COMPRESSION, 잔여 중 cross_asset_stress
             최댓값→CRISIS_DEFENSE, book_thinning 최댓값→LIQUIDITY_THIN, 나머지는 hurst 내림차순
             으로 TREND_UP/TREND_DOWN/RANGE_BALANCED/RANGE_BREAK_PREP에 배정).
        실패 조건: features가 비어있으면 ValueError.
        """
        if features.size == 0:
            raise ValueError("fit() requires non-empty features")

        best_model = None
        best_score = -np.inf
        for i in range(self.n_restarts):
            candidate = GaussianHMM(
                n_components=len(RegimeLabel),
                covariance_type="diag",
                random_state=self.random_state + i,
                n_iter=self.n_iter,
            )
            try:
                candidate.fit(features)
                score = candidate.score(features)
            except ValueError:
                # 불운한 초기화로 EM이 발산(상태 소실 → 공분산 0 → NaN)한 경우 해당 후보를 버린다.
                continue
            if np.isfinite(score) and score > best_score:
                best_score = score
                best_model = candidate

        if best_model is None:
            raise RuntimeError("모든 HMM 초기화가 발산했습니다 — n_restarts를 늘리거나 피처를 재점검하세요")
        self.model = best_model
        self._state_to_label = self._calibrate_labels(features)
        self._fitted = True

    def _calibrate_labels(self, features: np.ndarray) -> dict[int, RegimeLabel]:
        state_seq = self.model.predict(features)
        means: dict[int, np.ndarray] = {}
        for state in range(self.model.n_components):
            mask = state_seq == state
            means[state] = features[mask].mean(axis=0) if mask.any() else self.model.means_[state]

        hurst_idx = FEATURE_NAMES.index("hurst")
        rv_idx = FEATURE_NAMES.index("rv_ratio")
        stress_idx = FEATURE_NAMES.index("cross_asset_stress")
        thin_idx = FEATURE_NAMES.index("book_thinning")

        states_by_rv = sorted(means, key=lambda s: means[s][rv_idx])
        vol_compression_state = states_by_rv[0]
        vol_expansion_state = states_by_rv[-1]

        labels: dict[int, RegimeLabel] = {
            vol_expansion_state: RegimeLabel.VOL_EXPANSION,
            vol_compression_state: RegimeLabel.VOL_COMPRESSION,
        }

        remaining = [s for s in means if s not in labels]
        if remaining:
            crisis_state = max(remaining, key=lambda s: means[s][stress_idx])
            labels[crisis_state] = RegimeLabel.CRISIS_DEFENSE
            remaining.remove(crisis_state)

        if remaining:
            thin_state = max(remaining, key=lambda s: means[s][thin_idx])
            labels[thin_state] = RegimeLabel.LIQUIDITY_THIN
            remaining.remove(thin_state)

        trend_range_labels = [
            RegimeLabel.TREND_UP_STRONG,
            RegimeLabel.TREND_DOWN_STRONG,
            RegimeLabel.RANGE_BALANCED,
            RegimeLabel.RANGE_BREAK_PREP,
        ]
        for state, label in zip(sorted(remaining, key=lambda s: means[s][hurst_idx], reverse=True), trend_range_labels):
            labels[state] = label

        return labels

    def predict(self, features_1m: np.ndarray, features_15m: np.ndarray | None = None) -> RegimeState:
        """
        §7.3 detect_regime 구현.

        입력: 최근 윈도우 (n,6) 1분 피처, 선택적 15분 상위 피처.
        계산: HMM 베이지안 확률(predict_proba) 최신 행을 prob_vector로 사용, argmax를 regime으로.
             1분 레짐과 15분 상위 레짐 충돌 시 상위 레짐 우선.
        해석: stability_flag=False(REGIME_UNSTABLE) → 사이즈 자동 축소 신호.
        실패 조건: fit() 이전 호출 시 RuntimeError — 데이터 부족 구간에는 warmup_fallback() 사용.
        """
        if not self._fitted or self._state_to_label is None:
            raise RuntimeError("predict() 이전에 fit()으로 캘리브레이션이 필요합니다 — 미가용 시 warmup_fallback() 사용")

        prob_vector = self._prob_vector(features_1m)
        regime = RegimeLabel(int(np.argmax(prob_vector)))
        stability_flag = max(prob_vector) >= _UNSTABLE_PROB_THRESHOLD

        higher_tf_regime = None
        if features_15m is not None and features_15m.size:
            prob_vector_15m = self._prob_vector(features_15m)
            higher_tf_regime = RegimeLabel(int(np.argmax(prob_vector_15m)))
            if higher_tf_regime != regime:
                regime = higher_tf_regime  # 상위 레짐 우선

        return RegimeState(
            regime=regime,
            prob_vector=tuple(prob_vector),
            higher_tf_regime=higher_tf_regime,
            stability_flag=stability_flag,
        )

    def _prob_vector(self, features: np.ndarray) -> list[float]:
        assert self._state_to_label is not None
        proba = self.model.predict_proba(features)[-1]
        prob_vector = [0.0] * len(RegimeLabel)
        for state, p in enumerate(proba):
            prob_vector[self._state_to_label[state]] = float(p)
        return prob_vector


_GAP_ZSCORE_THRESHOLD = 2.0
_CRISIS_GAP_ZSCORE_THRESHOLD = 3.0


def warmup_fallback(prior_close_regime: RegimeLabel, macro_score: float, gap_zscore: float) -> RegimeState:
    """
    §7.4 / §16.1 WARMUP (4) — 장 초반 데이터 부족 구간의 레짐 대체.

    입력: 전일 마감 레짐, 장전 매크로 스코어(양수=위험선호, 음수=위험회피), 갭 z-score.
    계산: |gap_zscore|가 임계값 이상이면 갭 방향과 매크로 스코어로 레짐을 override,
         아니면 전일 마감 레짐을 그대로 사용.
    해석: 연속 세션 데이터가 쌓이면 HMM 기반 predict()로 전환한다.
    실패 조건: 없음 — 항상 결정론적 폴백 값을 반환하되 stability_flag=False로 신뢰도를 낮춘다.
    """
    if abs(gap_zscore) >= _GAP_ZSCORE_THRESHOLD:
        if macro_score < 0:
            regime = (
                RegimeLabel.CRISIS_DEFENSE
                if abs(gap_zscore) >= _CRISIS_GAP_ZSCORE_THRESHOLD
                else RegimeLabel.VOL_EXPANSION
            )
        else:
            regime = RegimeLabel.TREND_UP_STRONG if gap_zscore > 0 else RegimeLabel.TREND_DOWN_STRONG
    else:
        regime = prior_close_regime

    prob_vector = [0.0] * len(RegimeLabel)
    prob_vector[regime] = 1.0
    return RegimeState(regime=regime, prob_vector=tuple(prob_vector), stability_flag=False, is_warmup=True)
