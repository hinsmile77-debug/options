"""E1 Regime Intelligence — HMM(GaussianHMM, 8-state) + 매크로 나침반 워밍업 폴백 (v6 PART 7).

레짐은 모든 하위 엔진의 가중치 스위치다. §7.3 입력 목록에는 방향(상승/하락) 판별용 피처가
없으므로, v1은 hurst(추세성) 순위로 TREND/RANGE 계열을 구분하고 실제 방향은 상위 레이어
(Fusion)가 가격 모멘텀 등으로 별도 보정하는 것을 전제로 한다.
"""

from __future__ import annotations

import logging
import pickle
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

import numpy as np
from hmmlearn.hmm import GaussianHMM

logger = logging.getLogger("mahdi.engines.regime")


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


def convergence_report(model) -> tuple[bool, str]:
    """
    입력: 학습된 GaussianHMM.
    계산: EM이 **정말로 수렴했는지**를 `monitor_.history`에서 직접 판정한다 —
         (a) 반복을 다 쓰지 않았고(`iter < n_iter`) (b) 마지막 로그우도 증분이 `0 <= delta < tol`.
    해석: 2026-08-10 — hmmlearn(0.3.3)의 `monitor_.converged`는 **수렴을 재지 않는다**:

             return (self.iter == self.n_iter or
                     (len(self.history) >= 2 and self.history[-1] - self.history[-2] < self.tol))

         `iter == n_iter`는 반복을 다 쓰고도 못 멈춘 것(비수렴)인데 True이고, `delta < tol`은
         **음수 delta**(로그우도가 줄어드는 발산)도 True다. hmmlearn 소스에도
         `XXX we might want to check that log_prob is non-decreasing` 주석이 달려 있다.

         **여기(엔진)에 있는 이유**: `fit()`의 재시작 루프가 **후보를 고를 때** 이 판정을 쓰고,
         `scripts/fit_regime_engine.py`의 저장 게이트가 **같은 함수**를 쓴다. 두 곳이 다른 기준을
         쓰면 "학습은 통과시킨 모델을 저장이 거부"하거나 그 반대가 되어, 어느 쪽이 옳은지 알 수
         없게 된다(`replay_live_predictions`를 라이브 모듈에 둔 것과 같은 원칙).
    실패 조건: 없음 — (통과 여부, 사람이 읽을 사유)를 돌려주고 판단은 호출측이 한다.
    """
    monitor = getattr(model, "monitor_", None)
    history = list(getattr(monitor, "history", []) or [])
    if monitor is None or len(history) < 2:
        return False, "수렴 이력을 읽지 못했다(hmmlearn 버전 차이?) — 저장 전 사람이 확인할 것"
    delta = history[-1] - history[-2]
    n_iter = getattr(monitor, "n_iter", None)
    tol = getattr(monitor, "tol", None)
    if n_iter is not None and getattr(monitor, "iter", None) == n_iter:
        return False, f"반복 {n_iter}회를 다 쓰고 멈췄다(비수렴) — n_iter를 늘리거나 피처 분산을 볼 것"
    if delta < 0:
        return False, f"로그우도가 감소했다(delta={delta:+.6g}) — EM 발산. 이 후보는 저장하면 안 된다"
    if tol is not None and delta >= tol:
        return False, f"증분이 아직 크다(delta={delta:.6g} >= tol={tol}) — 수렴 전에 멈춘 것"
    return True, f"수렴(iter={getattr(monitor, 'iter', '?')}/{n_iter}, delta={delta:+.6g} < tol={tol})"


class RegimeEngine:
    """GaussianHMM 기반 레짐 판별기. fit()으로 잠재상태<->RegimeLabel 매핑을 캘리브레이션한 뒤 predict() 사용."""

    def __init__(self, random_state: int = 42, n_restarts: int = 10, n_iter: int = 200) -> None:
        self.random_state = random_state
        self.n_restarts = n_restarts
        self.n_iter = n_iter
        self.model: GaussianHMM | None = None
        self._state_to_label: dict[int, RegimeLabel] | None = None
        self._fitted = False
        # predict()의 길이 1 경고는 인스턴스당 1회만 — 매분 호출되는 경로라 로그가 잠긴다.
        self._warned_single_row = False
        # 학습 출처(2026-08-10) — `load()`가 채운다. 자세한 이유는 `save()` docstring.
        self.metadata: dict = {}

    def fit(self, features: np.ndarray, lengths: list[int] | None = None) -> None:
        """
        입력: (n_samples, 6) 배열(FEATURE_NAMES 순서)과 **세션별 행 수** `lengths`.
             최소 수십 세션 분량 권장. `sum(lengths) == len(features)`여야 한다.
        계산: EM은 초기화에 따라 지역해(일부 잠재상태 미사용)에 빠질 수 있어, 서로 다른
             random_state로 n_restarts회 학습 후 로그우도(score)가 가장 높은 모델을 채택한다.
             이후 잠재상태별 평균 피처값으로 8개 RegimeLabel에 결정론적으로 매핑한다
             (rv_ratio 최댓값→VOL_EXPANSION, 최솟값→VOL_COMPRESSION, 잔여 중 cross_asset_stress
             최댓값→CRISIS_DEFENSE, book_thinning 최댓값→LIQUIDITY_THIN, 나머지는 hurst 내림차순
             으로 TREND_UP/TREND_DOWN/RANGE_BALANCED/RANGE_BREAK_PREP에 배정).

        **`lengths`를 반드시 넘겨야 하는 이유(2026-08-10)**: hmmlearn의 EM은 `startprob_`를
        **시퀀스 시작점들의 사후확률 평균**으로 재추정한다. `lengths=None`이면 전체가 시퀀스
        하나로 취급돼 표본이 **관측된 시작점 1개**뿐이고, EM 반복이 그 한 점을 뾰족하게 만들어
        `startprob_`가 one-hot으로 붕괴한다. 08-10 재학습이 정확히 그랬다 —
        `startprob_ = [0,0,0,0,0,1,0,0]`(7개가 부동소수 정확히 0.0)이 나왔고, 그 모델은
        `predict()`에 길이 1 시퀀스가 들어오는 라이브 경로에서 **전 이력 8,241분을 단일 상태로**
        판정했다. 같은 데이터에 `lengths`(21세션)만 넘기면 비영 startprob이 5개로 살아난다.

        `lengths=None`은 **테스트·단발 실험용 하위호환**으로만 남긴다(경고 1줄).
        실패 조건: features가 비어있으면 ValueError. `sum(lengths) != len(features)`면 ValueError.
        """
        if features.size == 0:
            raise ValueError("fit() requires non-empty features")
        if lengths is None:
            logger.warning(
                "fit(lengths=None) — 전체를 시퀀스 1개로 학습한다. startprob_가 one-hot으로 "
                "붕괴할 수 있다(2026-08-10 실측). 운영 학습은 반드시 세션별 lengths를 넘길 것."
            )
        elif sum(lengths) != len(features):
            raise ValueError(
                f"lengths 합({sum(lengths)})이 features 행 수({len(features)})와 다릅니다 — "
                "필터로 제외된 행이 있으면 lengths도 그 뒤에 다시 세야 합니다"
            )

        # 2026-08-10 — **수렴한 후보 중에서** 로그우도 최댓값을 고른다. 종전에는 점수만 봤는데,
        # 그러면 발산한 후보(로그우도가 줄어드는 중이라 마지막 값이 우연히 높을 수 있다)가
        # 승자가 된다. 실제로 그날 재계산 피처로 학습했을 때 승자가 delta=−0.32로 발산한 후보였고,
        # 저장 게이트가 **런 전체를 거부**했다 — 멀쩡한 후보가 9개 있었는데도.
        # 수렴 후보가 하나도 없을 때만 비수렴 후보로 물러서고, 그때는 저장 게이트가 거부한다
        # (여기서 예외를 던지면 `--allow-nonconverged`로 진단할 길이 사라진다).
        best_model = best_fallback = None
        best_score = fallback_score = -np.inf
        for i in range(self.n_restarts):
            candidate = GaussianHMM(
                n_components=len(RegimeLabel),
                covariance_type="diag",
                random_state=self.random_state + i,
                n_iter=self.n_iter,
            )
            try:
                candidate.fit(features, lengths)
                score = candidate.score(features, lengths)
            except ValueError:
                # 불운한 초기화로 EM이 발산(상태 소실 → 공분산 0 → NaN)한 경우 해당 후보를 버린다.
                continue
            if not np.isfinite(score):
                continue
            converged, _reason = convergence_report(candidate)
            if converged:
                if score > best_score:
                    best_score, best_model = score, candidate
            elif score > fallback_score:
                fallback_score, best_fallback = score, candidate

        if best_model is None and best_fallback is not None:
            logger.warning(
                "재시작 %d회 중 수렴한 후보가 없다 — 최고점 비수렴 후보로 물러선다. "
                "저장 게이트가 이것을 거부할 것이다(--allow-nonconverged로만 통과).",
                self.n_restarts,
            )
            best_model = best_fallback

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
             **n은 1보다 커야 의미가 있다** — 아래 참조.
        계산: HMM 베이지안 확률(predict_proba) 최신 행을 prob_vector로 사용, argmax를 regime으로.
             1분 레짐과 15분 상위 레짐 충돌 시 상위 레짐 우선.
        해석: stability_flag=False(REGIME_UNSTABLE) → 사이즈 자동 축소 신호.

        **길이 1 시퀀스를 넘기면 이 함수는 HMM이 아니다(2026-08-10)**: `predict_proba`의 사후확률은
        길이 1에서 `normalize(startprob ⊙ emission(x))`로 축약돼 **전이행렬이 통째로 안 쓰인다.**
        `startprob_`가 뾰족하면(학습 때 `lengths`를 안 넘긴 경우) emission이 그것을 뒤집지 못해
        출력이 입력과 무관한 상수가 된다 — 08-10에 전 이력 8,241분이 단일 상태로 나온 사고가
        그것이다. 라이브 호출부(`RegimeStateMachine.step`)는 세션 누적 창을 넘긴다.
        여기서 예외를 던지지 않는 이유: 테스트·도구의 정당한 단발 호출이 있고, 라이브는
        `_MIN_WARMUP_BARS`(30) 이후에만 부르므로 **이 경고가 뜨는 것 자체가 회귀 신호**다.
        실패 조건: fit() 이전 호출 시 RuntimeError — 데이터 부족 구간에는 warmup_fallback() 사용.
        """
        if not self._fitted or self._state_to_label is None:
            raise RuntimeError("predict() 이전에 fit()으로 캘리브레이션이 필요합니다 — 미가용 시 warmup_fallback() 사용")

        if len(features_1m) == 1 and not self._warned_single_row:
            self._warned_single_row = True
            logger.warning(
                "predict()에 길이 1 시퀀스가 들어왔다 — 전이행렬이 사용되지 않아 startprob_가 "
                "답을 지배한다(2026-08-10 상수 출력 사고). 호출부가 창을 넘기는지 확인할 것."
            )

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

    def save(self, path: str | Path, metadata: dict | None = None) -> None:
        """
        입력: 저장 경로, (선택) 학습 출처 메타데이터.
        계산: fit()으로 캘리브레이션된 model/state_to_label을 pickle로 직렬화한다 — 오프라인
             배치(scripts/fit_regime_engine.py)가 만든 결과를 실시간 프로세스가 재학습 없이 로드.
        해석: 2026-08-10 — `metadata`를 함께 싣는 이유. 이날부터 학습은 `iv_chg`를 **DB가 기록한
             값이 아니라 재계산한 값**으로 쓴다(`feature_store`는 그대로 두고 학습 시점에만
             대체 — `fit_regime_engine.reconstruct_iv_chg()`). 그 사실이 모델 **안에** 없으면,
             나중에 이 파일을 열어 본 사람은 학습 입력이 DB와 같다고 **잘못 가정**한다. 로그는
             지워지고 사람의 기억은 흐려지지만 pickle은 남는다. 운영 리포트와 COCKPIT이
             이 값을 읽어 화면에 낸다.
        실패 조건: fit() 이전이면 RuntimeError(미캘리브레이션 상태 저장 방지).
        """
        if not self._fitted:
            raise RuntimeError("fit() 이전에는 save()할 수 없습니다")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(
                {
                    "model": self.model,
                    "state_to_label": self._state_to_label,
                    "metadata": metadata or {},
                },
                f,
            )

    @classmethod
    def load(cls, path: str | Path) -> "RegimeEngine":
        """
        입력: save()가 만든 pickle 경로.
        계산: model/state_to_label/metadata를 복원한 fitted RegimeEngine을 반환한다.
             `metadata`는 2026-08-10에 생겼으므로 **그 이전 파일에는 없다** — 없으면 빈 dict다
             (「메타데이터가 없다」와 「대체가 없었다」는 다른 사실이라 지어내지 않는다).
        실패 조건: 파일이 없으면 FileNotFoundError(호출측이 잡아서 warmup_fallback으로 폴백해야 함).
        """
        with open(path, "rb") as f:
            payload = pickle.load(f)
        engine = cls()
        engine.model = payload["model"]
        engine._state_to_label = payload["state_to_label"]
        engine.metadata = payload.get("metadata", {})
        engine._fitted = True
        return engine


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
