"""Signal Fusion — Conflict Resolution Layer (v6 §11.1).

앙상블 가중 평균(ensemble.py)은 하나의 합의 방향을 만들지만, 그 합의가 "다수의 동조"
때문인지 "몇 개 안 되는 신호가 우연히 맞아떨어진 것"인지는 구분하지 못한다. 이 레이어는
멤버별 부호를 ensemble의 합의 방향과 대조해 동조/반대 개수를 세고, 반대가 동조 이상이면
(뚜렷한 다수 합의가 없다는 뜻) conviction을 깎을 근거를 meta_label에 넘긴다.
"""

from __future__ import annotations

from dataclasses import dataclass

from mahdi.fusion.ensemble import EnsembleResult
from mahdi.fusion.signal_layer import MEMBER_FIELDS, MemberScores


def _sign(value: float) -> float:
    if value > 0:
        return 1.0
    if value < 0:
        return -1.0
    return 0.0


@dataclass(frozen=True, slots=True)
class ConflictResolution:
    agreement_count: int  # 합의 방향과 부호가 같은 멤버 수
    disagreement_count: int  # 합의 방향과 부호가 반대인 멤버 수
    available_member_count: int
    has_clear_consensus: bool  # 반대가 동조보다 적을 때만 True
    # 2026-08-06 고도화#2 — 비영 점수를 낸 멤버 수. 상세 근거는 `EnsembleResult`.
    # 그대로 실어 나르기만 한다(여기서 다시 세면 같은 사실이 두 곳에 적힌다).
    effective_member_count: int = 0


def resolve_conflicts(scores: MemberScores, ensemble_result: EnsembleResult) -> ConflictResolution:
    """
    입력: MemberScores(멤버별 점수), EnsembleResult(가중 평균 합의 방향).
    계산: 합의 방향의 부호를 기준으로, 값이 있는 멤버 각각이 같은 부호면 동조, 반대 부호면
         반대로 센다(0점은 중립이라 어느 쪽에도 안 셈).
    해석: has_clear_consensus=False(반대 >= 동조)면 상위 표에서 "신호 충돌"로 간주해
         NO_TRADE 방향으로 conviction을 깎아야 한다(v6 §11.1 "신호 충돌 시 상위 우선순위
         규칙" — 규칙 자체는 "충돌 시 거래하지 않는다"는 보수적 원칙으로 구현).
    실패 조건: 합의 방향이 0.0(완전 중립/신호 없음)이면 동조/반대 모두 0, has_clear_consensus=False.
    """
    consensus_sign = _sign(ensemble_result.direction)

    agreement = 0
    disagreement = 0
    if consensus_sign != 0.0:
        for field_name in MEMBER_FIELDS:
            value = getattr(scores, field_name)
            if value is None:
                continue
            member_sign = _sign(value)
            if member_sign == 0.0:
                continue
            if member_sign == consensus_sign:
                agreement += 1
            else:
                disagreement += 1

    return ConflictResolution(
        agreement_count=agreement,
        disagreement_count=disagreement,
        available_member_count=ensemble_result.available_member_count,
        has_clear_consensus=consensus_sign != 0.0 and agreement > disagreement,
        effective_member_count=ensemble_result.effective_member_count,
    )
