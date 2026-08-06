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
    # 2026-08-06 고도화#2 — **비영 점수를 낸 멤버 수.**
    #
    # 08-06 §14-3(그날 처음 나온 표)이 `regime_hmm`이 399분 **전량 중립**(평균 +0.0000,
    # 강세 0 / 약세 0 / 중립 399)임을 드러냈다. 그 멤버는 가용 판정을 받고 앙상블에 들어가지만
    # 가중 평균에 아무것도 기여하지 않는다 — `available_member_count = 4`가 "판단이 네 개 축을
    # 본다"는 뜻이 아니었다. 배경은 레짐이 **23영업일 연속 상태 2 하나**라는 것이다.
    #
    # `available_member_count`를 **고치지 않고 옆에 세우는** 이유:
    #   1. 그 값은 `meta_label`의 확신도 분모이고(v6 §11.3), 정의를 조용히 바꾸면 그날부터
    #      확신도 시계열이 과거와 비교 불가능해진다.
    #   2. **둘의 차이 자체가 죽은 축의 수**이고, 그것이 이 지표가 재려는 것이다.
    #
    # 이 값으로 확신도를 깎지 않는다 — 정상 범위를 모르는 채 임계를 먼저 정하면 그 임계가 곧
    # 결론이 된다(§16 괴리율에서 배운 것). 며칠 재고 사람이 정한다.
    effective_member_count: int = 0


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
    effective_count = 0

    for field_name in MEMBER_FIELDS:
        value = getattr(scores, field_name)
        if value is None:
            continue
        weight = ensemble_cfg.get(field_name, {}).get("base_w", 0.0)
        weighted_sum += value * weight
        weight_total += weight
        available_count += 1
        # 2026-08-06 고도화#2 — 0은 중립이지 의견이 아니다. 상세 근거는 `EnsembleResult`.
        if value != 0.0:
            effective_count += 1

    direction = weighted_sum / weight_total if weight_total > 0 else 0.0
    return EnsembleResult(
        direction=direction,
        available_member_count=available_count,
        total_member_count=len(MEMBER_FIELDS),
        effective_member_count=effective_count,
    )
