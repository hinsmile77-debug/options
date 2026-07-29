"""Backtest — WFO(Walk-Forward)/Monte Carlo/DSR 검증 스택 (v6 PART 21 "백테스트 엔진").

이 모듈의 함수는 전부 순수 통계 계산이라 데이터 양과 무관하게 지금 바로 정확하게 동작한다
(`mahdi/backtest/engine.py`와 달리 실거래 이력 축적을 기다릴 필요가 없다). 다만 "몇 건 안 되는
거래로 계산한 결과가 통계적으로 의미 있는가"는 별개 문제이니, 표본이 적을 때는 해석에 주의할 것.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from statistics import NormalDist

_NORMAL = NormalDist()
_EULER_MASCHERONI = 0.5772156649015329


@dataclass(frozen=True, slots=True)
class WalkForwardSplit:
    train_indices: range
    test_indices: range


def walk_forward_splits(n_samples: int, n_folds: int, embargo: int = 0) -> list[WalkForwardSplit]:
    """
    입력: 전체 샘플 수(시간순), 분할 개수, 임베고(각 테스트 구간 직전에서 제외할 샘플 수 —
         v6 §11.2 "Purged K-Fold + Embargo"의 누수 차단 원칙을 백테스트 구간 분할에도 적용).
    계산: 시간순 인덱스를 n_folds개의 연속 테스트 구간으로 나누고, 각 구간의 훈련 구간은
         "그 이전까지의 전체 구간"에서 embargo만큼 제외한 나머지(앵커드 walk-forward)로 정한다.
    해석: 첫 fold는 훈련 구간이 비어있을 수 있다(테스트 전용) — 시간순 데이터라 미래로 과거를
         훈련할 수 없기 때문. 호출측이 최소 훈련 표본 크기를 확인해야 한다.
    실패 조건: n_samples<=0이거나 n_folds<=0이면 빈 목록.
    """
    if n_samples <= 0 or n_folds <= 0:
        return []
    fold_size = max(1, n_samples // n_folds)
    splits: list[WalkForwardSplit] = []
    for i in range(n_folds):
        test_start = i * fold_size
        if test_start >= n_samples:
            break
        test_end = n_samples if i == n_folds - 1 else min(n_samples, test_start + fold_size)
        train_end = max(0, test_start - embargo)
        splits.append(WalkForwardSplit(train_indices=range(0, train_end), test_indices=range(test_start, test_end)))
    return splits


@dataclass(frozen=True, slots=True)
class MonteCarloResult:
    mean_final_pnl: float
    worst_final_pnl: float
    best_final_pnl: float
    mean_max_dd: float
    worst_max_dd: float


def monte_carlo_resample(trade_pnls: list[float], n_simulations: int, seed: int | None = None) -> MonteCarloResult:
    """
    입력: 거래별 net_pnl 시퀀스(순서 무관 — 복원추출로 재배열), 시뮬레이션 횟수, 재현용 시드.
    계산: 매 시뮬레이션마다 같은 개수만큼 복원추출(bootstrap)한 순서로 누적손익 경로를 구성해
         최종 누적손익과 최대낙폭을 기록한다.
    해석: worst_*는 n_simulations회 중 최악 시나리오 — 실제 거래 순서가 다르게 나왔다면
         일어날 수 있었던 결과 범위를 보여준다(거래 순서 의존성 검증).
    실패 조건: trade_pnls가 비어있으면 전부 0.0. 같은 seed로 다시 호출하면 항상 같은 결과.
    """
    if not trade_pnls:
        return MonteCarloResult(0.0, 0.0, 0.0, 0.0, 0.0)

    rng = random.Random(seed)
    n = len(trade_pnls)
    finals: list[float] = []
    max_dds: list[float] = []

    for _ in range(n_simulations):
        resampled = [trade_pnls[rng.randrange(n)] for _ in range(n)]
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for pnl in resampled:
            cumulative += pnl
            peak = max(peak, cumulative)
            max_dd = min(max_dd, cumulative - peak)
        finals.append(cumulative)
        max_dds.append(max_dd)

    return MonteCarloResult(
        mean_final_pnl=sum(finals) / len(finals),
        worst_final_pnl=min(finals),
        best_final_pnl=max(finals),
        mean_max_dd=sum(max_dds) / len(max_dds),
        worst_max_dd=min(max_dds),
    )


def _expected_max_sharpe(trial_sharpe_ratios: list[float]) -> float:
    """
    계산: Bailey & de Prado(2014) 근사식 — E[max SR_N] ~= std(trials) x
         [(1-gamma)*Phi^-1(1-1/N) + gamma*Phi^-1(1-1/(N*e))], gamma=오일러-마스케로니 상수.
         다중 시행(trial_sharpe_ratios) 사이의 표본표준편차를 "단일 시행 추정치의 변동성"
         근사치로 쓴다.
    실패 조건: 시행이 1개 이하면 분산 정의 불가 — 그 시행의 값을 그대로 반환(팽창 없음).
              분산이 0이면(전부 동일값) 평균을 그대로 반환.
    """
    n = len(trial_sharpe_ratios)
    if n <= 1:
        return trial_sharpe_ratios[0] if trial_sharpe_ratios else 0.0
    mean = sum(trial_sharpe_ratios) / n
    variance = sum((s - mean) ** 2 for s in trial_sharpe_ratios) / (n - 1)
    std = math.sqrt(variance)
    if std == 0:
        return mean
    term1 = (1 - _EULER_MASCHERONI) * _NORMAL.inv_cdf(1 - 1 / n)
    term2 = _EULER_MASCHERONI * _NORMAL.inv_cdf(1 - 1 / (n * math.e))
    return std * (term1 + term2)


def deflated_sharpe_ratio(
    trial_sharpe_ratios: list[float],
    observed_sharpe: float,
    n_observations: int,
    skewness: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """
    입력: 지금까지 시도한 모든 전략/파라미터 조합의 Sharpe 목록(선택 편향 반영용), 최종 채택안의
         관측 Sharpe, 그 관측에 쓰인 수익률 관측치 개수, 수익률 분포의 왜도/첨도(정규분포=0/3).
    계산: (1) trial_sharpe_ratios로 "N회 시행 중 기대 최대 Sharpe"(SR0, 다중검정 보정 기준선)를
         구한다. (2) PSR(Probabilistic Sharpe Ratio) 공식으로 observed_sharpe가 SR0를 넘길
         확률을 반환: Phi((observed_sharpe - SR0) x sqrt(n_observations-1) /
         sqrt(1 - skewness*observed_sharpe + (kurtosis-1)/4 * observed_sharpe^2)).
    해석: 반환값이 낮을수록 "이 전략의 성과가 다중 시행 중 우연히 나온 최댓값일 가능성"이 높다는
         뜻 — Champion 승격 전 확인해야 할 통계적 안전장치(v6 PART 14 cc_scorecard와 함께 사용).
         trial_sharpe_ratios가 비어 있으면 SR0=0(다중검정 보정 없음 — 단일 전략 평가로 취급).
    실패 조건: n_observations < 2면 분산 항이 정의 안 돼 ValueError.
    """
    if n_observations < 2:
        raise ValueError("n_observations는 2 이상이어야 합니다(분산 추정 불가)")

    sr0 = _expected_max_sharpe(trial_sharpe_ratios) if trial_sharpe_ratios else 0.0
    denominator = math.sqrt(
        max(1e-12, 1 - skewness * observed_sharpe + (kurtosis - 1) / 4 * observed_sharpe**2)
    )
    z = (observed_sharpe - sr0) * math.sqrt(n_observations - 1) / denominator
    return _NORMAL.cdf(z)
