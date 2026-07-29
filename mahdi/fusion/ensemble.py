"""Signal Fusion — 앙상블 가중 평균 (v6 §11.3).

`strategy_params.yaml`의 `ensemble.*.base_w`를 단일 소스로 쓰되, 이번 증분에는 학습된
모델이 없는 `xgboost_tabular`/`lstm_temporal`은 항상 None이라 자동으로 분모에서
빠진다 — `regime_pipeline.compute_macro_score_proxy()`가 쓰는 "존재하는 신호만
평균"과 같은 원칙(base_w 비율은 유지한 채, 존재하는 멤버끼리만 재정규화).
고정 가중치 자체는 v6 §11.3 주석대로 Phase 3(Thompson Sampling)에서 성과 기반으로
재배분될 예정이며 이 모듈은 그 전까지의 정적 가중치만 다룬다.
"""

from __future__ import annotations

from dataclasses import dataclass

from mahdi.fusion.signal_layer import MEMBER_FIELDS, MemberScores


@dataclass(frozen=True, slots=True)
class EnsembleResult:
    direction: float  # -1~+1 가중 평균, 사용 가능한 멤버가 하나도 없으면 0.0(완전 중립)
    available_member_count: int
    total_member_count: int = len(MEMBER_FIELDS)


def weighted_consensus(scores: MemberScores, ensemble_cfg: dict) -> EnsembleResult:
    """
    입력: MemberScores(멤버별 -1~+1 또는 None), ensemble_cfg(`strategy_params.yaml`의
         `ensemble` 섹션, 각 멤버 키에 `{"base_w": float}`).
    계산: 값이 None이 아닌 멤버들만 모아 base_w로 가중 평균(가중치 합으로 나눠 재정규화).
    해석: available_member_count는 conflict_resolution/meta_label이 "신호 동조 개수"의
         분모로 재사용한다.
    실패 조건: 사용 가능한 멤버가 하나도 없으면 direction=0.0(완전 중립), 가중치 합이
              0이면(설정 오류) 마찬가지로 0.0.
    """
    weighted_sum = 0.0
    weight_total = 0.0
    available_count = 0

    for field_name in MEMBER_FIELDS:
        value = getattr(scores, field_name)
        if value is None:
            continue
        weight = ensemble_cfg.get(field_name, {}).get("base_w", 0.0)
        weighted_sum += value * weight
        weight_total += weight
        available_count += 1

    direction = weighted_sum / weight_total if weight_total > 0 else 0.0
    return EnsembleResult(
        direction=direction,
        available_member_count=available_count,
        total_member_count=len(MEMBER_FIELDS),
    )
