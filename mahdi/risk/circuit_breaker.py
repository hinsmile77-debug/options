"""Risk Engine — Circuit Breaker & Kill Switch (v6 §12.3).

HALT_CONDITIONS 7종(daily_loss_pct/drawdown_pct/vpin_crisis/vix_spike/
usdkrw_daily_change/data_quality_fail/model_drift)을 감시한다. 실제 청산
실행(gradual_delever()/emergency_flatten())은 이 모듈의 책임이 아니다 —
상태와 필요 조치 플래그만 반환하고, 실행은 Execution Engine(다음 증분)이
담당한다. Forced Flat "종료 시퀀스 자기검증" 요구사항([[DECISION_LOG]]
2026-07-21)과 관심사가 섞이지 않도록 의도적으로 분리했다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from mahdi.config.settings import get_risk_limits
from mahdi.risk.limits import AccountState


class CircuitBreakerState(str, Enum):
    NORMAL = "NORMAL"
    HALTED = "HALTED"


@dataclass
class MarketConditions:
    vpin: float = 0.0
    vix: float = 0.0
    usdkrw_daily_change_pct: float = 0.0
    data_quality_ok: bool = True
    model_drift_detected: bool = False


@dataclass
class CircuitBreakerDecision:
    state: CircuitBreakerState
    triggered_conditions: list[str] = field(default_factory=list)
    requires_gradual_delever: bool = False
    requires_emergency_flatten: bool = False


class CircuitBreaker:
    """일간 상태를 갖는 Circuit Breaker. 한 번 HALTED면 reset_daily() 전까지 유지."""

    def __init__(self, risk_limits: dict | None = None) -> None:
        self._risk_limits = risk_limits
        self._state = CircuitBreakerState.NORMAL
        self._halt_reasons: list[str] = []

    @property
    def state(self) -> CircuitBreakerState:
        return self._state

    def reset_daily(self) -> None:
        """장 시작 시 호출 — 일간 HALT 상태를 초기화한다."""
        self._state = CircuitBreakerState.NORMAL
        self._halt_reasons = []

    def evaluate(
        self,
        account_state: AccountState,
        market_conditions: MarketConditions,
    ) -> CircuitBreakerDecision:
        """
        입력: 계좌 상태 + 시장 상태.
        계산: risk_limits.yaml의 circuit_breaker 섹션(v6 §12.3 HALT_CONDITIONS)과
              순서대로 대조한다. 손실/DD 계열은 account_state, 나머지
              (vpin/vix/usdkrw/데이터품질/모델드리프트)는 market_conditions에서
              읽는다.
        해석: 하나라도 발동하면 신규 진입 전면 차단(requires_gradual_delever)
              — 손실 계열(daily_loss_pct/drawdown_pct) 또는 데이터품질 실패는
              즉시 청산까지 요구한다(requires_emergency_flatten, v6 §12.3
              "발동 시: 신규 차단 → gradual_delever() → 필요 시
              emergency_flatten()").
        실패 조건: 한 번 HALTED가 되면 reset_daily() 전까지 NORMAL로 자동
              복귀하지 않는다 — 같은 거래일 안에서 일시적으로 조건이 해소됐다고
              바로 재진입을 허용하지 않기 위한 의도적 설계(requires_gradual_
              delever는 계속 True). 반면 requires_emergency_flatten은 오직 그
              호출 시점에 새로 발동한 조건만 반영한다(과거 HALT 이력 때문에
              영구히 True로 고착되지 않음) — emergency_flatten은 멱등 동작
              (포지션이 이미 없으면 no-op)이므로 조건이 실제로 계속되는 동안만
              반복 신호를 보내는 것이 안전하며, 조건이 해소된 뒤에도 계속
              True를 반환하면 호출측이 "지금도 위급 상황"과 "예전에 위급했던
              적 있음"을 구분할 수 없게 된다.
        """
        risk_limits = self._risk_limits if self._risk_limits is not None else get_risk_limits()
        cb_cfg = risk_limits.get("circuit_breaker", {})

        triggered_now: list[str] = []
        emergency = False

        daily_loss_limit = cb_cfg.get("daily_loss_pct")
        if daily_loss_limit is not None and account_state.daily_pnl_pct < daily_loss_limit:
            triggered_now.append("daily_loss_pct")
            emergency = True

        dd_limit = cb_cfg.get("drawdown_pct")
        if dd_limit is not None and account_state.drawdown_pct < dd_limit:
            triggered_now.append("drawdown_pct")
            emergency = True

        vpin_limit = cb_cfg.get("vpin_crisis")
        if vpin_limit is not None and market_conditions.vpin >= vpin_limit:
            triggered_now.append("vpin_crisis")

        vix_limit = cb_cfg.get("vix_spike")
        if vix_limit is not None and market_conditions.vix >= vix_limit:
            triggered_now.append("vix_spike")

        usdkrw_limit = cb_cfg.get("usdkrw_daily_change")
        if usdkrw_limit is not None and abs(market_conditions.usdkrw_daily_change_pct) >= usdkrw_limit:
            triggered_now.append("usdkrw_daily_change")

        if cb_cfg.get("data_quality_fail") and not market_conditions.data_quality_ok:
            triggered_now.append("data_quality_fail")
            emergency = True

        if cb_cfg.get("model_drift") and market_conditions.model_drift_detected:
            triggered_now.append("model_drift")

        if triggered_now:
            self._state = CircuitBreakerState.HALTED
            for reason in triggered_now:
                if reason not in self._halt_reasons:
                    self._halt_reasons.append(reason)

        halted = self._state == CircuitBreakerState.HALTED
        return CircuitBreakerDecision(
            state=self._state,
            triggered_conditions=list(self._halt_reasons) if halted else [],
            requires_gradual_delever=halted,
            requires_emergency_flatten=emergency,
        )
