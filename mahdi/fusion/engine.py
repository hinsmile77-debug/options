"""Signal Fusion — 파사드 (v6 §11 전체 파이프라인).

Primary Signal Layer -> Ensemble -> Conflict Resolution -> Meta-Label -> Strategy
Palette를 하나의 evaluate() 호출로 통과시킨다. `RiskEngine`과 같은 파사드 패턴 —
이 클래스 자체는 최종 주문 승인권이 없다(그건 RiskEngine의 몫, v6 §12 "독립
거부권"). `FusionDecision`은 Execution Engine이 RiskEngine과 함께 검토할 "진입
후보"일 뿐이다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from mahdi.config.settings import get_strategy_params
from mahdi.engines.regime import RegimeLabel
from mahdi.fusion.conflict_resolution import resolve_conflicts
from mahdi.fusion.ensemble import weighted_consensus
from mahdi.fusion.meta_label import MetaLabelInputs, TradePermission, classify
from mahdi.fusion.signal_layer import MEMBER_FIELDS, MemberScores, SignalInputs, build_member_scores
from mahdi.fusion.strategy_palette import (
    enforce_daily_strategy_cap,
    enforce_reentry_cooldown,
    select_strategies,
)

# 2026-08-03(운영점검보고서 §5-2) — 이 모듈에는 로그가 하나도 없었다.
#
# 08-03 하루 전체에서 로그를 낸 것은 httpx / mahdi.main / mahdi.broker.rest_client 셋뿐이고,
# fusion·engines·risk·features·execution·learning은 전부 무음이었다. §2-1의 버그(감마플립이
# 전 이력에서 한 번도 산출된 적 없음 → options_flow 멤버 영구 미가용)가 넉 달간 안 보인 이유가
# 정확히 이것이다 — **판단 축에는 관측이 없었다.**
#
# 다만 볼륨을 다시 늘리면 07-31에 어렵게 되찾은 가독성을 또 잃는다(08-03 §2-8: 사람이 읽는 줄이
# 4,629줄로 두 배가 됐다). 그래서 **전이(transition)에만 반응한다** — 매 분 찍지 않고, 멤버
# 가용 조합이나 판정 형태가 **바뀔 때만** 한 줄. 정상적인 하루라면 수 건에 그친다.
# (2026-07-19 §5-5의 WarningThrottle, 07-30 Fix#4의 "상태 전이가 있을 때만 기록"과 같은 계열.)
logger = logging.getLogger("mahdi.fusion.engine")


def _sign(value: float) -> float:
    if value > 0:
        return 1.0
    if value < 0:
        return -1.0
    return 0.0


@dataclass(frozen=True, slots=True)
class MetaLabelContext:
    """SignalInputs/앙상블만으로는 알 수 없는, 호출측이 직접 넘겨야 하는 메타 라벨 입력."""

    recent_slippage_elevated: bool = False
    gamma_regime_stable: bool = True
    event_proximity_minutes: float | None = None
    recent_same_setup_win_rate: float | None = None


@dataclass(frozen=True, slots=True)
class FusionDecision:
    direction: float
    conviction_score: float
    trade_permission: TradePermission
    allowed_strategies: list[str] = field(default_factory=list)
    signal_agreement_count: int = 0
    available_member_count: int = 0
    # 2026-08-06 고도화#2 — **비영 점수를 낸 멤버 수.** `available_member_count`와 나란히 둔다.
    # 08-06 §14-3: `regime_hmm`이 399분 전량 중립이었고, 그래서 가용 4는 실질 3이었다.
    # **둘의 차이가 곧 죽은 축의 수**다. 상세 근거는 `fusion.ensemble.EnsembleResult`.
    effective_member_count: int = 0
    reject_reasons: list[str] = field(default_factory=list)
    # 2026-08-05(고도화#4) — **앙상블에 들어간 멤버별 점수 그 자체.**
    #
    # 여기까지 계산해 놓고 버리고 있었다. 그래서 08-05에 판단이 살아났을 때
    # (가용 멤버 2 → 4, 확신도 4종, 전이 83회) **그 방향 ±0.692가 어느 멤버에서 왔는지를
    # DB로 역산할 수 없었다.** `available_member_count` 숫자 하나로는 "몇 개가 살아 있었나"만
    # 알 뿐 "무엇이 판단을 밀었나"에 답하지 못한다.
    #
    # 구체적으로 이 값이 없어서 못 답하는 질문: `OPTIONS_FLOW_GAMMA_WALL_FALLBACK`을 유지할
    # 것인가(08-04 §7 #1, 사용자 결정 대기). 감마월 폴백이 만든 점수가 방향을 뒤집었는지
    # 아니면 다른 멤버에 묻혔는지가 갈리는데, 점수가 안 남아 데이터로 답할 수 없다.
    member_scores: MemberScores | None = None


class SignalFusionEngine:
    def __init__(self, strategy_params: dict | None = None) -> None:
        self._params = strategy_params if strategy_params is not None else get_strategy_params()
        # 직전 사이클의 "판단 형태" — 바뀔 때만 로그를 낸다(위 logger 주석 참고).
        self._last_shape: tuple | None = None
        # 2026-08-23 Fix#3 — 축 하나하나의 **최근 상태 전환 시각**. 상세 근거는
        # `LOG_MEMBER_AXIS_EXIT` 위 주석. 값이 없는 멤버는 아직 한 번도 안 바뀐 것이다.
        self._member_since: dict[str, datetime] = {}
        self._last_available: tuple[str, ...] | None = None
        self._last_effective: int = 0

    def evaluate(
        self,
        signal_inputs: SignalInputs,
        meta_context: MetaLabelContext,
        vrp: float = 0.0,
        already_used_strategies_today: frozenset[str] = frozenset(),
        # 2026-08-11 고도화 D — 전략별 **직전 진입 이후 경과 분**. 기본값 None은 "기록 없음"이라
        # 쿨다운이 켜져 있어도 아무것도 안 막는다(레버 OFF와 같은 결과) — 백테스트·테스트 경로 보호.
        last_entry_minutes_ago: dict[str, float] | None = None,
        # 2026-08-23 Fix#3 — 사건 줄에 찍을 **이번 사이클의 시각**. `None`이면 벽시계를 쓴다
        # (백테스트·단위 테스트 경로 보호 — 로그 문구에만 쓰이고 판단에는 안 들어간다).
        now: datetime | None = None,
    ) -> FusionDecision:
        """
        입력: SignalInputs(원재료), MetaLabelContext(슬리피지/감마레짐/이벤트근접/자기강화
             이력 — 신호 계산기만으로는 알 수 없는 값), VRP, 오늘 이미 쓴 전략 집합.
        계산: (1) 멤버별 점수 -> (2) 가중 합의 방향 -> (3) 충돌 해소(동조/반대 개수) ->
             (4) 메타 라벨(conviction_score, TradePermission) -> (5) 합의가 뚜렷하고
             NO_TRADE가 아닐 때만 Strategy Palette 조회 + 하루 상한 적용.
        해석: reject_reasons가 비어 있어야만 allowed_strategies가 실제로 채워진다 —
             NO_TRADE/충돌/팔레트 게이트 미충족 중 하나라도 있으면 빈 목록.
        실패 조건: 없음 — 원재료 부재는 전부 개별 레이어의 None/중립값으로 흡수된다.
        """
        member_scores = build_member_scores(signal_inputs)
        ensemble_result = weighted_consensus(member_scores, self._params.get("ensemble", {}))
        conflict = resolve_conflicts(member_scores, ensemble_result)

        regime_state = signal_inputs.regime_state
        regime_confidence = max(regime_state.prob_vector) if regime_state is not None else 0.0
        regime = regime_state.regime if regime_state is not None else RegimeLabel.RANGE_BALANCED

        foreign_flow_aligned = True
        consensus_sign = _sign(ensemble_result.direction)
        if signal_inputs.foreign_net_flow is not None and consensus_sign != 0.0:
            foreign_sign = _sign(signal_inputs.foreign_net_flow)
            foreign_flow_aligned = foreign_sign == 0.0 or foreign_sign == consensus_sign

        meta_inputs = MetaLabelInputs(
            regime_confidence=regime_confidence,
            signal_agreement_count=conflict.agreement_count,
            # 2026-08-06 고도화#2 — **여기에는 `effective_member_count`를 넣지 않는다.**
            # 이 값은 확신도의 분모이고(v6 §11.3), 정의를 바꾸면 그날부터 확신도 시계열이
            # 과거와 비교 불가능해진다. 이 fix는 **재기만 한다** — 며칠 쌓고 사람이 정한다.
            #
            # 2026-08-07 고도화#3 — **레버를 만들되 내려 둔다**(08-06 고도화#4와 같은 방식).
            # 08-07 실측: 가용 평균 3.16 vs 실질 2.15(죽은 축 1.02) — 확신도의 분모가 매일
            # 47% 과대평가돼 있다. `regime_hmm`이 212분 전량 중립이었기 때문이고, 그 원인은
            # 레짐이 24영업일 연속 한 상태라는 것이다(학습 미완).
            #
            # **지금 켜지 않는 이유**: (a) 정의를 바꾸면 그날부터 확신도 시계열이 과거와 비교
            # 불가가 된다 (b) 08-10 HMM 재학습으로 `regime_hmm`이 살아나면 격차가 저절로 줄어,
            # 그때 켜는 것이 전환 비용이 가장 작다 (c) 재학습과 분모 변경을 같은 날 하면
            # 확신도 변화를 둘 중 무엇이 만들었는지 영영 못 가른다.
            #
            # **켜는 방법**: `strategy_params`의 `use_effective_member_count: true`.
            # **켜는 시점**: 08-10 재학습 후 `regime_hmm`이 비영 점수를 내는 것을 확인한 **다음
            # 영업일**(재학습 효과와 분리하기 위해). 그때 `hypotheses.yaml`에 예측을 먼저 적는다.
            available_member_count=(
                conflict.effective_member_count
                if self._params.get("use_effective_member_count", False)
                else conflict.available_member_count
            ),
            recent_slippage_elevated=meta_context.recent_slippage_elevated,
            gamma_regime_stable=meta_context.gamma_regime_stable,
            foreign_flow_aligned=foreign_flow_aligned,
            event_proximity_minutes=meta_context.event_proximity_minutes,
            recent_same_setup_win_rate=meta_context.recent_same_setup_win_rate,
        )
        meta_result = classify(meta_inputs, self._params.get("meta_label_thresholds", {}))

        reject_reasons: list[str] = []
        allowed: list[str] = []
        if meta_result.trade_permission == TradePermission.NO_TRADE:
            reject_reasons.append("meta_label:no_trade")
        elif not conflict.has_clear_consensus:
            reject_reasons.append("conflict_resolution:no_clear_consensus")
        else:
            palette = select_strategies(
                regime,
                vrp,
                highest_confidence=meta_result.trade_permission == TradePermission.HIGH_CONVICTION,
                positive_gex=signal_inputs.gex is not None and signal_inputs.gex >= 0,
                stable_regime=regime_state.stability_flag if regime_state is not None else False,
            )
            if palette.reason:
                reject_reasons.append(f"strategy_palette:{palette.reason}")
            strategy_gates = self._params.get("strategy_gates", {})
            cap = strategy_gates.get("max_priority_strategies_per_regime_day", 2)
            before_cap = palette.allowed_strategies
            allowed = enforce_daily_strategy_cap(before_cap, already_used_strategies_today, cap)
            # ===== 2026-08-31 (08-31 제4부 P2-3) — **조용한 컷은 사유를 남긴다** =====
            #
            # 종전에는 상한을 넘은 전략을 **아무 흔적 없이** 잘라냈다. 그래서 08-31에
            # 「레짐일당 상한 2」를 지켰는지 **로그로도 DB로도 확인할 수 없었고**, 그날
            # 절대원칙 판정표에 「판정 불가」로 남았다.
            #
            # 바로 아래 `enforce_reentry_cooldown`이 **이미 그렇게 하고 있다**(08-05에 같은
            # 이유로 추가된 줄). 같은 패턴을 옆 줄에 맞추는 것이다.
            #
            # ⛔ **판정은 한 글자도 안 바뀐다** — `allowed`의 내용도 진입 여부도 확신도도
            # 그대로고, **실제로 잘린 사이클에만** 사유 한 항목이 는다.
            # ⚠ 상한 값(`max_priority_strategies_per_regime_day`)은 손대지 않는다 —
            # 이 항목이 바꾸는 것은 「무엇을 재는가」이지 임계가 아니다.
            if len(allowed) < len(before_cap):
                reject_reasons.append("strategy_daily_cap")
            # 2026-08-11 고도화 D — 동일 전략 재진입 쿨다운. **기본 0(OFF)** 이라 종전과 같다.
            # 상한(가짓수)과 쿨다운(빈도)은 다른 것을 막는다 — 근거는
            # `strategy_palette.enforce_reentry_cooldown` 위 주석.
            cooldown = strategy_gates.get("reentry_cooldown_minutes", 0)
            before_cooldown = allowed
            allowed = enforce_reentry_cooldown(allowed, last_entry_minutes_ago or {}, cooldown)
            if before_cooldown and not allowed:
                # **왜 진입이 안 났는지**가 사유로 남아야 한다. 이 줄이 없으면 쿨다운이 켜진 날
                # ENTER 급감이 "신호가 약해서"로 오독된다(08-05에 팔레트가 정확히 그랬다).
                reject_reasons.append("reentry_cooldown")

        decision = FusionDecision(
            direction=ensemble_result.direction,
            conviction_score=meta_result.conviction_score,
            trade_permission=meta_result.trade_permission,
            allowed_strategies=allowed,
            signal_agreement_count=conflict.agreement_count,
            available_member_count=conflict.available_member_count,
            effective_member_count=conflict.effective_member_count,
            reject_reasons=reject_reasons,
            member_scores=member_scores,
        )
        self._log_member_transitions(decision, member_scores, now)
        self._log_shape_transition(decision, member_scores)
        return decision

    def _log_shape_transition(self, decision: FusionDecision, member_scores: MemberScores) -> None:
        """
        입력: 이번 사이클의 판단과 멤버 점수.
        계산: "어떤 멤버가 살아 있고 / 어떤 허가와 사유가 나왔는가"를 형태(shape)로 압축해,
             직전 사이클과 다를 때만 INFO 한 줄을 남긴다.
        해석: 2026-08-03 §5-2. 매 분 찍으면 하루 495줄이 되어 가독성을 잃고, 아무것도 안 찍으면
             **멤버 하나가 조용히 죽어도 로그만으로는 영영 모른다**(08-03에 실제로 그랬다).
             전이만 남기면 정상일에는 수 건, 무언가 바뀐 날에는 그 시각이 정확히 남는다.
        실패 조건: 없음 — 로깅 실패는 판단에 영향을 주지 않는다.
        """
        available = tuple(
            name for name in MEMBER_FIELDS if getattr(member_scores, name) is not None
        )
        shape = (available, decision.trade_permission, tuple(decision.reject_reasons),
                 tuple(decision.allowed_strategies))
        if shape == self._last_shape:
            return
        previous, self._last_shape = self._last_shape, shape
        # ===== 2026-08-19 §3-5 / Fix#6 — **가용 4/6이 실질 2.36이었다** =====
        #
        # 그날 장중 회차가 이 줄의 `4/6`을 보고 축 가용성을 ✅로 읽었고, 장후 DB 축에서만
        # 실질 2.36이 보였다(죽은 축 1.07 — `regime_hmm` 410분 전량 중립). **0점은 중립이지
        # 의견이 아닌데**(`ensemble.EnsembleResult` 주석) 0점 멤버도 「가용멤버」에는 남는다.
        #
        # 값은 이미 여기 있었다 — `decision.effective_member_count`. 로그에만 없었다.
        # 파서는 옛 문구와 새 문구를 **둘 다** 읽는다(`collect_evidence.MEMBER_RE`) —
        # 08-04에 문구가 바뀌며 정규식이 눈이 멀어 362건을 0건으로 보고한 전례가 있다.
        # ===== 2026-08-31 (08-31 §1-15 / 제4부 P1-4) — **분모가 1이면 로그가 그렇다고 말한다** =====
        #
        # 레버 F(`use_effective_member_count`)가 켜져 있으면 확신도의 분모가 **비영 멤버 수**다.
        # 그 값이 1이면 합의비율이 **구조적으로 1.00**(최댓값)이 되어, 「의견을 낸 축이 하나뿐」인
        # 상태가 「만장일치」로 읽힌다. 08-31에 그 상황에서 `HIGH_CONVICTION`이 3건 났다
        # (14:51 · 14:54 · 15:36 — 전부 14:50 컷오프 뒤였다).
        #
        # ⛔ **동작은 바꾸지 않는다. 임계도 걸지 않는다 — 계측이 먼저다**(08-21 사용자 조치 7).
        # 레버 F는 08-23에 다섯 번 만에 켠 것이고 `strategy_params.yaml:72-77`이 「확신도는
        # 오른다」를 **예측으로 적어 뒀다.** 그 예측이 맞는 중인데 지금 되돌리면 무엇이 무엇을
        # 만들었는지 영영 못 가른다. 며칠 쌓아 「분모 1이 하루에 몇 번, 몇 시에 나는가」를
        # 안 뒤에 사람이 정한다.
        #
        # ⚠ **꼬리표는 줄 끝에만 붙인다** — 앞머리와 `비영 %d`의 자리를 건드리면
        # `collect_evidence.MEMBER_RE`가 눈이 먼다(08-04에 문구가 움직여 362건이 0건이 됐다).
        # ⚠ 억제는 이 함수의 구조가 이미 한다 — 이 줄은 **형태가 바뀔 때만** 찍히므로
        # 분모 1이 여러 분 이어져도 줄은 전이 시점에 하나다(08-31 실측 3건).
        denominator_note = (
            " · ⚠ 분모 1 — 합의비율 구조적 1.00"
            if self._params.get("use_effective_member_count", False)
            and decision.effective_member_count <= 1
            else ""
        )
        # ===== 2026-09-02 (09-02 §1-8 / 제4부 P1-3) — **비영 N이 「누가 빠졌는지」는 안 말한다** =====
        #
        # 09-02 14:06부터 비영이 3/6 → 2/6으로 내려앉아 그 시간대 판단의 72%가 2/6이었는데,
        # 로그에는 「비영 2」만 있었다. **어느 축이 0점을 냈는지는 DB를 열어야만** 알 수 있었다
        # (`db.decisions.member_count.dead_axis_by_member`). 08-19 Fix#6이 「가용 4/6이 실질
        # 2.36이었다」를 고친 것과 같은 형태 — **값은 이미 여기 있고 로그에만 없다.**
        #
        # ⛔ **0은 중립이지 의견이 아니다**(`ensemble.EnsembleResult`). `available`에는 남아 있고
        # `effective_member_count`에서만 빠지는 그 축들의 **이름**이 이 목록이다.
        # ⚠ **꼬리표는 줄 끝에만 붙인다** — 앞머리와 `비영 %d`의 자리를 건드리면
        # `collect_evidence.MEMBER_RE`가 눈이 먼다(08-04에 문구가 움직여 362건이 0건이 됐다).
        # ⚠ **빈 목록도 찍는다**(규약 C) — `0점축=[]`가 없으면 「0점 축이 없었다」와
        # 「이 줄이 아직 안 실렸다」가 같은 글자가 된다.
        # ⚠ 줄 수는 안 는다 — 이 함수는 **형태가 바뀔 때만** 찍는다.
        zero_axes = [name for name in available if getattr(member_scores, name) == 0.0]
        logger.info(
            "판단 형태 전이: 가용멤버 %s(%d/%d, 비영 %d) · %s · 사유 %s · 전략 %s%s%s%s",
            list(available), len(available), len(MEMBER_FIELDS), decision.effective_member_count,
            decision.trade_permission.value,
            list(decision.reject_reasons) or "없음",
            list(decision.allowed_strategies) or "없음",
            "" if previous is None else f" (직전 가용멤버 {list(previous[0])})",
            f" · 0점축={zero_axes}",
            denominator_note,
        )

    def _log_member_transitions(
        self, decision: FusionDecision, member_scores: MemberScores, now: datetime | None,
    ) -> None:
        """
        입력: 이번 사이클의 판단, 멤버 점수, (선택) 이번 사이클 시각.
        계산: 가용 멤버 **집합**이 직전 사이클과 달라진 축마다 사건 줄 하나. 그 축이 직전
             상태를 얼마나 유지했는지도 함께 적는다.
        해석: 상세 근거는 `LOG_MEMBER_AXIS_EXIT` 위 주석. 첫 사이클은 **아무것도 안 찍는다** —
             기동 직후의 「전부 새로 편입」은 사건이 아니라 시작이고, 그것을 사건으로 세면
             매일 아침 여섯 줄이 상한을 갉아먹는다.
        실패 조건: 없다 — 로깅 실패는 판단에 영향을 주지 않는다.
        """
        moment = now if now is not None else datetime.now()
        available = tuple(
            name for name in MEMBER_FIELDS if getattr(member_scores, name) is not None
        )
        previous, self._last_available = self._last_available, available
        previous_effective, self._last_effective = self._last_effective, decision.effective_member_count
        if previous is None:
            # 기동 직후 — 지금 살아 있는 축의 「편입 시각」만 세워 두고 조용히 넘어간다.
            for name in available:
                self._member_since[name] = moment
            return
        if previous == available:
            return

        for name in previous:
            if name not in available:
                self._emit_axis_event(
                    LOG_MEMBER_AXIS_EXIT, name, previous, available,
                    previous_effective, decision.effective_member_count, moment, "편입", "유지",
                )
        for name in available:
            if name not in previous:
                self._emit_axis_event(
                    LOG_MEMBER_AXIS_RETURN, name, previous, available,
                    previous_effective, decision.effective_member_count, moment, "이탈", "부재",
                )

    def _emit_axis_event(
        self, template: str, name: str, previous: tuple[str, ...], available: tuple[str, ...],
        previous_effective: int, effective: int, moment: datetime, since_label: str, span_label: str,
    ) -> None:
        """한 축의 전환 한 줄. **직전 상태를 언제부터 유지했는지**가 이 줄의 절반이다.

        「빠졌다」만으로는 그것이 42분 만의 첫 이탈인지 1분 만의 되돌이인지 알 수 없고,
        그 둘은 조치가 다르다(전자는 사건, 후자는 채터링이다 — 08-04 ATM 롤링 왕복 70회가
        같은 형태였고 그때도 「몇 번」이 아니라 「얼마 만에」가 답이었다).
        """
        began = self._member_since.get(name)
        self._member_since[name] = moment
        if began is None:
            span = f"직전 {since_label} 시각 모름"
        else:
            minutes = max((moment - began).total_seconds() / 60.0, 0.0)
            span = f"직전 {since_label} {began:%H:%M:%S} · {minutes:.0f}분 {span_label}"
        logger.info(
            template, name, len(previous), len(available), previous_effective, effective, span,
        )


# ===== 2026-08-23 (08-21 §1-10 / §4 Fix#3) — 축이 **빠진 그 분**에 한 줄 =====
#
# ## 사흘째 이월된 fix이고, 08-21에 여섯 번 눈으로 찾았다
#
# 그날 `options_flow`가 여섯 번 빠졌다(10:47 · 12:04 · 13:05 · 13:34 · 13:49 · 15:09) —
# **어제 2회에서 세 배**로 늘었는데 여섯 번 다 사람이 로그를 겹쳐 세어 찾았다. 사흘 연속이다.
#
# ## 값은 이미 있었다. 없던 것은 **차이를 말하는 문장**이다
#
# 위 `_log_shape_transition`은 「지금 형태가 무엇인가」를 찍는다 — `가용멤버 [...](4/6, 비영 3)`.
# 축이 하나 빠진 것을 알려면 **직전 줄과 나란히 놓고 목록을 비교해야** 한다. 그 비교를 사람이
# 하는 한, 축은 조용히 죽고 그 사실은 장후에나 드러난다(08-19 §3-5가 정확히 그 하루였다).
#
# ## 두 줄은 서로를 대체하지 않는다
#
# 형태 전이 줄은 **상태**이고 이 줄은 **사건**이다. 전자를 없애면 「지금 무엇이 살아 있나」를
# 못 묻고, 후자가 없으면 「언제 무엇이 죽었나」를 못 묻는다. 08-19 Fix#6이 형태 줄에 「비영」을
# 실은 것과 같은 계열의 보강이고, 그때와 같이 **파서는 두 문구를 각자 센다**
# (`log_metrics._QUALITATIVE_MARKERS`).
#
# ## 볼륨
#
# 전이가 있는 분에만, **바뀐 축마다 한 줄**이다. 08-21 실측이면 이탈 6 + 복귀 5 = **11줄**이고,
# 가설이 상한을 하루 20건으로 못박아 뒀다(`2026-08-21-fix7-member-exit-is-an-event`).
# 매분 찍으면 08-15 `ALERT_ONLY` 94줄의 재현이다 — 그래서 전이에만 반응한다.
LOG_MEMBER_AXIS_EXIT = "판단 축 이탈: %s (가용 %d→%d, 비영 %d→%d) · %s"
LOG_MEMBER_AXIS_RETURN = "판단 축 복귀: %s (가용 %d→%d, 비영 %d→%d) · %s"
