"""Risk Engine — 파사드 (v6 §12, "독립 거부권").

Signal Fusion이 만든 진입 후보를 sizing/limits/circuit_breaker 세 모듈에 통과
시켜 최종 승인 여부와 사이즈를 결정한다. 이 클래스 자체는 신호를 생성하지
않는다 — 오직 거부하거나 사이즈를 깎을 뿐이다(v6 §12 서두: "리스크 엔진은
신호 엔진의 하위 모듈이 아니다. 항공기의 실속 경보처럼 독립 회로로 작동하며,
어떤 신호도 거부할 수 있다").
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mahdi.config.settings import get_risk_limits
from mahdi.risk.circuit_breaker import CircuitBreaker, CircuitBreakerDecision, CircuitBreakerState, MarketConditions
from mahdi.risk.limits import AccountState, check_limits
from mahdi.risk.sizing import PositionSizingInput, compute_position_size


@dataclass
class RiskDecision:
    approved: bool
    approved_size: float
    reject_reasons: list[str] = field(default_factory=list)
    unconfigured_checks: list[str] = field(default_factory=list)


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
    ) -> RiskDecision:
        """
        입력: 사이징 입력, 계좌 상태, 전략 ID, 시장 상태.
        계산: (1) Circuit Breaker 평가 — HALTED면 즉시 전량 거부 사유에 추가.
              (2) 한도 체계(§12.2) 점검 — 위반이 있으면 그 사유도 전부 추가.
              (3) 위반이 하나도 없을 때만 사이징 공식(§12.1)으로 최종 사이즈
                  계산.
        해석: approved=False인 경우 approved_size는 항상 0.0 — "거부됐지만
              일부 사이즈는 허용" 같은 애매한 상태를 만들지 않는다(§12
              "독립 거부권" 취지 — 거부는 전량 거부).
        실패 조건: Circuit Breaker HALTED와 한도 위반이 동시에 있어도 둘 다
              reject_reasons에 남겨 signal_decisions.reject_reason 로그가
              원인을 온전히 담도록 한다(§18.2).
        """
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
        return RiskDecision(
            approved=True,
            approved_size=sizing_result.size,
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
