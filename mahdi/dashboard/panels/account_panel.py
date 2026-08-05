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


# 2026-08-05(COCKPIT 육안 점검 P1-5) — "기준이 없다"를 "변동이 없다"로 표시하지 않는다.
_NO_BASELINE_HELP = {
    "일간 수익률": "오늘 자정 이전 잔고 스냅샷이 없어 비교 기준이 없습니다 — 0%가 아니라 '모름'입니다.",
    "주간 수익률": "이번 주 시작 이전 잔고 스냅샷이 없어 비교 기준이 없습니다 — 0%가 아니라 '모름'입니다.",
    "최대낙폭": "역대 최고 예탁자산 기록이 없어 낙폭을 잴 기준이 없습니다 — 0%가 아니라 '모름'입니다.",
}


def _pct_card(label: str, value: float, *, has_baseline: bool, drawdown: bool = False) -> dict:
    """
    입력: 카드 라벨, `build_account_state()`가 낸 비율, 그 비율의 비교 기준 존재 여부.
    계산: 기준이 없으면 값 대신 "기준 없음"을 쓴다. 있으면 퍼센트로 표시하되, **낙폭은 부호를
         붙이지 않는다** — 드로우다운은 정의상 0 이하라 "+0.00%"는 있을 수 없는 표기다
         (`account_tracker.build_account_state`: drawdown_pct = (latest − peak) / peak).
    해석: 2026-08-05 P1-5. 08-05 화면의 일간/주간 +0.00%와 최대낙폭 +0.00%는 "변동 없음"이
         아니라 "비교할 과거가 없음"이었는데 화면에서 둘이 구분되지 않았다. RiskEngine은 두
         경우를 구분할 필요가 없지만(어느 쪽이든 한도 위반이 아니다) 사람은 구분해야 한다.
    """
    if not has_baseline:
        return {"label": label, "value": "기준 없음", "status": "neutral", "help": _NO_BASELINE_HELP[label]}
    text = f"{value * 100:.2f}%" if drawdown else f"{value * 100:+.2f}%"
    status = "warning" if (drawdown and value < 0) else _pct_status(value)
    return {"label": label, "value": text, "status": status, "help": None}


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
        _pct_card("일간 수익률", status["daily_pnl_pct"], has_baseline=status.get("has_daily_baseline", True)),
        _pct_card("주간 수익률", status["weekly_pnl_pct"], has_baseline=status.get("has_weekly_baseline", True)),
        _pct_card("최대낙폭", status["drawdown_pct"], has_baseline=status.get("has_peak", True), drawdown=True),
    ]
