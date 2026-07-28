"""Risk Engine — 한도 체계 (v6 §12.2).

단일 트레이드 손실/일일 손실/주간 손실/최대 드로우다운/동일방향 포지션수/
전략별 일일 거래횟수를 점검한다. 하나라도 위반하면 그 사유를 전부 모아 반환
한다 — §18.2 "거절된 신호도 기록한다. 거절의 품질이 시스템의 품질이다" 원칙과
맞추기 위해 첫 위반에서 멈추지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mahdi.config.settings import get_risk_limits


@dataclass
class AccountState:
    daily_pnl_pct: float
    weekly_pnl_pct: float
    drawdown_pct: float
    same_direction_positions: int
    daily_trades_by_strategy: dict[str, int] = field(default_factory=dict)
    pending_trade_loss_pct: float = 0.0  # 진입하려는 트레이드의 예상 최대손실률(음수)


@dataclass
class LimitViolation:
    limit_name: str
    reason: str


@dataclass
class LimitCheckResult:
    violations: list[LimitViolation] = field(default_factory=list)
    unconfigured_checks: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.violations


def check_limits(
    account_state: AccountState,
    strategy_id: str,
    risk_limits: dict | None = None,
    portfolio_greeks_limits: dict | None = None,
    current_portfolio_greeks: dict | None = None,
) -> LimitCheckResult:
    """
    입력: 계좌/포지션 현재 상태, 진입하려는 전략 ID.
    계산: risk_limits.yaml의 limits 섹션 각 항목과 순서대로 대조한다(단일
          트레이드→일일→주간→최대DD→동일방향 포지션수→전략별 일일거래횟수→
          포트폴리오 Greeks).
    해석: violations가 비어 있으면 진입 허용, 하나라도 있으면 거부(v6 §12.2
          표의 "위반 시" 개별 조치 — 신규 거래 중단/Review 모드/자동 거래
          정지 등 — 는 상위 RiskEngine/Execution Engine이 상태 전이로 처리
          하고, 이 함수는 위반 여부만 순수 판정한다).
    실패 조건: 포트폴리오 Greeks 한도는 risk_limits.yaml에 아직 수치가 없다
          (v6 §12.2 표에만 "상한"으로 존재, 실제 값은 미정). 억지로 숫자를
          지어내지 않고 portfolio_greeks_limits가 None이면 그 체크만
          건너뛰되 unconfigured_checks에 "portfolio_greeks"를 남겨 조용히
          무시되지 않도록 한다.
    """
    if risk_limits is None:
        risk_limits = get_risk_limits()
    limits_cfg = risk_limits.get("limits", {})

    violations: list[LimitViolation] = []
    unconfigured: list[str] = []

    per_trade_limit = limits_cfg.get("per_trade_loss_pct")
    if per_trade_limit is not None and account_state.pending_trade_loss_pct < per_trade_limit:
        violations.append(LimitViolation(
            "per_trade_loss_pct",
            f"예상 손실률 {account_state.pending_trade_loss_pct:.4f} < 한도 {per_trade_limit:.4f}",
        ))

    daily_limit = limits_cfg.get("daily_loss_pct")
    if daily_limit is not None and account_state.daily_pnl_pct < daily_limit:
        violations.append(LimitViolation(
            "daily_loss_pct",
            f"일일 손익률 {account_state.daily_pnl_pct:.4f} < 한도 {daily_limit:.4f}",
        ))

    weekly_limit = limits_cfg.get("weekly_loss_pct")
    if weekly_limit is not None and account_state.weekly_pnl_pct < weekly_limit:
        violations.append(LimitViolation(
            "weekly_loss_pct",
            f"주간 손익률 {account_state.weekly_pnl_pct:.4f} < 한도 {weekly_limit:.4f}",
        ))

    max_dd_limit = limits_cfg.get("max_drawdown_pct")
    if max_dd_limit is not None and account_state.drawdown_pct < max_dd_limit:
        violations.append(LimitViolation(
            "max_drawdown_pct",
            f"드로우다운 {account_state.drawdown_pct:.4f} < 한도 {max_dd_limit:.4f}",
        ))

    max_same_dir = limits_cfg.get("max_same_direction_positions")
    if max_same_dir is not None and account_state.same_direction_positions >= max_same_dir:
        violations.append(LimitViolation(
            "max_same_direction_positions",
            f"동일방향 포지션 {account_state.same_direction_positions}개 >= 한도 {max_same_dir}개",
        ))

    max_daily_trades = limits_cfg.get("max_daily_trades_per_strategy")
    if max_daily_trades is not None:
        current_count = account_state.daily_trades_by_strategy.get(strategy_id, 0)
        if current_count >= max_daily_trades:
            violations.append(LimitViolation(
                "max_daily_trades_per_strategy",
                f"'{strategy_id}' 오늘 거래 {current_count}회 >= 한도 {max_daily_trades}회",
            ))

    if portfolio_greeks_limits is None:
        unconfigured.append("portfolio_greeks")
    else:
        greeks = current_portfolio_greeks or {}
        for greek_name, cap in portfolio_greeks_limits.items():
            value = greeks.get(greek_name)
            if value is not None and abs(value) > cap:
                violations.append(LimitViolation(
                    f"portfolio_{greek_name}",
                    f"포트폴리오 {greek_name} |{value:.4f}| > 한도 {cap:.4f}",
                ))

    return LimitCheckResult(violations=violations, unconfigured_checks=unconfigured)
