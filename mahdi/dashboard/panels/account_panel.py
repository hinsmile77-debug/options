"""계좌 현황 패널 (2026-07-29 신규) — 계좌 잔고/손익률을 COCKPIT 최상단에서 "3초 룰"로 보여준다.

값은 전부 `mahdi.dashboard.data_source.get_account_status_view()`가 `mahdi.execution.
account_tracker.build_account_state()`로 이미 계산해온 것을 그대로 표시만 한다(손익률 계산을
이 패널에서 다시 하지 않음 — RiskEngine이 쓰는 것과 동일한 계산 결과).
"""

from __future__ import annotations


def _pct_status(value: float) -> str:
    if value > 0:
        return "ok"
    if value < 0:
        return "warning"
    return "neutral"


def build_account_summary_cards(status: dict | None) -> list[dict]:
    """
    입력: `data_source.get_account_status_view()` — 계좌 잔고 폴러가 아직 스냅샷을 못 쌓았으면
         None(그 경우 카드 없이 "아직 없음" 1장만 반환 — 손익 0으로 지어내지 않는다).
    계산: 카드 7장 — 추정예탁자산/현금(주문가능)/평가손익/실현손익/일간 수익률/주간 수익률/
         최대낙폭. `decision_panel.build_decision_summary_cards()`와 동일한 label/value/status/
         help dict 리스트 패턴이라, 향후 지표(예: 실현손익 추이) 추가는 리스트에 dict 하나만
         더 붙이면 된다.
    """
    if status is None:
        return [{"label": "계좌 현황", "value": "아직 없음", "status": "neutral", "help": None}]

    return [
        {
            "label": "추정예탁자산",
            "value": f"{status['prsm_dpast']:,.0f}원",
            "status": "neutral",
            "help": None,
        },
        {
            "label": "현금(주문가능)",
            "value": f"{status['ord_psbl_cash']:,.0f}원",
            "status": "neutral",
            "help": None,
        },
        {
            "label": "평가손익",
            "value": f"{status['evlu_pfls_amt_smtl']:+,.0f}원",
            "status": _pct_status(status["evlu_pfls_amt_smtl"]),
            "help": None,
        },
        {
            "label": "실현손익",
            "value": f"{status['trad_pfls_amt_smtl']:+,.0f}원",
            "status": _pct_status(status["trad_pfls_amt_smtl"]),
            "help": None,
        },
        {
            "label": "일간 수익률",
            "value": f"{status['daily_pnl_pct'] * 100:+.2f}%",
            "status": _pct_status(status["daily_pnl_pct"]),
            "help": None,
        },
        {
            "label": "주간 수익률",
            "value": f"{status['weekly_pnl_pct'] * 100:+.2f}%",
            "status": _pct_status(status["weekly_pnl_pct"]),
            "help": None,
        },
        {
            "label": "최대낙폭",
            "value": f"{status['drawdown_pct'] * 100:+.2f}%",
            "status": "warning" if status["drawdown_pct"] < 0 else "neutral",
            "help": None,
        },
    ]
