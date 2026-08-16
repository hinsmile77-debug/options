"""Risk Engine — 파사드 (v6 §12, "독립 거부권").

Signal Fusion이 만든 진입 후보를 sizing/limits/circuit_breaker 세 모듈에 통과
시켜 최종 승인 여부와 사이즈를 결정한다. 이 클래스 자체는 신호를 생성하지
않는다 — 오직 거부하거나 사이즈를 깎을 뿐이다(v6 §12 서두: "리스크 엔진은
신호 엔진의 하위 모듈이 아니다. 항공기의 실속 경보처럼 독립 회로로 작동하며,
어떤 신호도 거부할 수 있다").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as dtime

from mahdi import session
from mahdi.config.settings import get_risk_limits
from mahdi.risk.circuit_breaker import CircuitBreaker, CircuitBreakerDecision, CircuitBreakerState, MarketConditions
from mahdi.risk.limits import AccountState, check_limits
from mahdi.risk.sizing import PositionSizingInput, compute_position_size, contracts_from_size


@dataclass
class RiskDecision:
    approved: bool
    approved_size: float
    reject_reasons: list[str] = field(default_factory=list)
    unconfigured_checks: list[str] = field(default_factory=list)
    # 2026-08-16 (Block D) — **실제로 주문할 정수 계약수.**
    #
    # `approved_size`를 고치지 않고 옆에 세우는 이유는 08-06 고도화#2가
    # `available/effective_member_count`를 그렇게 둔 것과 같다: 그 값은 사이징 공식의 출력이고,
    # 정의를 바꾸면 그날부터 시계열이 과거와 비교 불가가 된다. **둘의 차이가 곧 버림분**이고
    # 그것이 「사이징이 계산한 규모와 실제로 낸 규모」의 간격이다.
    #
    # `approved=False`면 항상 0이다(거부는 전량 거부 — 애매한 상태를 만들지 않는다).
    approved_contracts: int = 0


class RiskEngine:
    def __init__(self, risk_limits: dict | None = None) -> None:
        self._risk_limits = risk_limits if risk_limits is not None else get_risk_limits()
        self._circuit_breaker = CircuitBreaker(risk_limits=self._risk_limits)

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self._circuit_breaker

    def evaluate_entry(
        self,
        sizing_input: PositionSizingInput,
        account_state: AccountState,
        strategy_id: str,
        market_conditions: MarketConditions,
        portfolio_greeks_limits: dict | None = None,
        current_portfolio_greeks: dict | None = None,
        market_halted: bool = False,
        now: datetime | dtime | None = None,
    ) -> RiskDecision:
        """
        입력: 사이징 입력, 계좌 상태, 전략 ID, 시장 상태, market_halted(2026-07-29 신규 — KRX
              서킷브레이커/거래정지가 실시간으로 발동 중인지, mahdi.risk.market_halt.
              MarketHaltMonitor 기준. 이 모듈 내부의 CircuitBreaker와는 별개 개념이라 별도
              파라미터로 받는다 — market_conditions에 섞지 않는 이유는 아래 참고),
              now(2026-08-06 §2-2 / Fix#1 신규 — 판단 시각. v6 §4.2 「14:50 신규 진입 컷오프」를
              평가하기 위해 받는다. `None`이면 시각 게이트를 건너뛴다 — 시각과 무관한 한도만
              보고 싶은 기존 호출측/테스트를 깨지 않기 위함이고, **라이브 경로는 반드시 넘긴다**
              (`tests/test_main.py`가 그것을 강제한다)).
        계산: (0a) `now`가 신규 진입 컷오프(14:50) 이후면 다른 무엇도 보지 않고 즉시 전량 거부.
              **`market_halted`보다도 앞에 둔다** — 거래소가 열려 있든 닫혀 있든 이 시각 이후의
              신규 진입은 v6 §4.2가 금지한 것이고, 뒤에 두면 halt 상태에 따라 거부 사유가
              바뀌어 `signal_decisions.reject_reason` 시계열이 흔들린다.
              (0b) market_halted가 True면 다른 무엇도 보지 않고 즉시 전량 거부한다 — 거래소
              자체가 정지된 상태에서는 사이징/한도 계산이 무의미하다.
              (1) Circuit Breaker 평가 — HALTED면 즉시 전량 거부 사유에 추가.
              (2) 한도 체계(§12.2) 점검 — 위반이 있으면 그 사유도 전부 추가.
              (3) 위반이 하나도 없을 때만 사이징 공식(§12.1)으로 최종 사이즈
                  계산.
        해석: approved=False인 경우 approved_size는 항상 0.0 — "거부됐지만
              일부 사이즈는 허용" 같은 애매한 상태를 만들지 않는다(§12
              "독립 거부권" 취지 — 거부는 전량 거부).
              market_halted를 CircuitBreaker(내부 리스크 킬스위치, daily_loss/drawdown 등)에
              합치지 않는 이유: CircuitBreaker는 한 번 HALTED면 reset_daily() 전까지 그날 안엔
              자동 복귀하지 않는 래치 설계인데, 거래소 CB는 KRX가 해제 이벤트를 보내는 즉시
              실시간으로 풀려야 한다 — 같은 래치를 적용하면 "CB 해제됐는데도 그날 내내 거래
              차단"이라는 잘못된 동작이 된다(mahdi/risk/market_halt.py 모듈 docstring 참고).
        실패 조건: Circuit Breaker HALTED와 한도 위반이 동시에 있어도 둘 다
              reject_reasons에 남겨 signal_decisions.reject_reason 로그가
              원인을 온전히 담도록 한다(§18.2).
              단 컷오프/halt는 예외다 — 둘 다 "그 무엇도 볼 필요가 없다"는 뜻이므로 단독으로 낸다.
        """
        # 2026-08-06 Fix#1 — v6 §4.2 신규 진입 컷오프. 08-06에 이 게이트가 없어 14:50 이후
        # ENTER 21건(그중 15:10 강제 평탄화 이후 18건)이 나왔다. 상세 근거는 `mahdi.session`.
        if now is not None and session.is_after_entry_cutoff(now):
            return RiskDecision(approved=False, approved_size=0.0, reject_reasons=["entry_cutoff"])

        if market_halted:
            return RiskDecision(approved=False, approved_size=0.0, reject_reasons=["market_halt"])

        cb_decision = self._circuit_breaker.evaluate(account_state, market_conditions)

        reject_reasons: list[str] = []
        if cb_decision.state == CircuitBreakerState.HALTED:
            reject_reasons.extend(f"circuit_breaker:{c}" for c in cb_decision.triggered_conditions)

        limit_result = check_limits(
            account_state,
            strategy_id,
            risk_limits=self._risk_limits,
            portfolio_greeks_limits=portfolio_greeks_limits,
            current_portfolio_greeks=current_portfolio_greeks,
        )
        reject_reasons.extend(f"{v.limit_name}: {v.reason}" for v in limit_result.violations)

        if reject_reasons:
            return RiskDecision(
                approved=False,
                approved_size=0.0,
                reject_reasons=reject_reasons,
                unconfigured_checks=limit_result.unconfigured_checks,
            )

        sizing_result = compute_position_size(sizing_input, risk_limits=self._risk_limits)
        # 2026-08-16 (Block D) — 정수 계약수를 여기서 확정한다. `contracts_from_size()`가
        # `size == 0`을 고정 계약수로 덮어쓰지 않는다(DD 한도가 낸 0을 설정으로 우회하면
        # 리스크 한도가 무의미해진다 — 그 함수 주석 참고).
        return RiskDecision(
            approved=True,
            approved_size=sizing_result.size,
            approved_contracts=contracts_from_size(sizing_result.size, self._risk_limits),
            unconfigured_checks=limit_result.unconfigured_checks,
        )

    def evaluate_ongoing(
        self,
        account_state: AccountState,
        market_conditions: MarketConditions,
    ) -> CircuitBreakerDecision:
        """보유 포지션 재평가용(v6 §13.4 매분 재평가 루프가 호출할 대상) —
        Circuit Breaker만 재확인한다(사이징은 신규 진입에만 의미가 있음)."""
        return self._circuit_breaker.evaluate(account_state, market_conditions)
