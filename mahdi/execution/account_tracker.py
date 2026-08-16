"""Execution Engine — 계좌 손익/포지션 상태 추적기.

`RiskEngine.evaluate_entry()`/`evaluate_ongoing()`이 필요로 하는 `AccountState`를
`get_balance()`(선물옵션 잔고현황, CTFO6118R/VTFO6118R) 응답으로부터 만든다. 필드 매핑은
2026-07-28 7차 세션에서 라이브 모의계좌로 직접 호출해 확인한 실측 결과 그대로다
([[DECISION_LOG]] 참고) — 추측으로 채운 필드가 없다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from mahdi.risk.limits import AccountState

logger = logging.getLogger(__name__)

# ===== 2026-08-16 (Block B) — 매도/매수 구분값을 하나로 못박지 않는다 =====
#
# ## 왜 이 상수가 집합인가
#
# 종전 구현은 `sll_buy_dvsn_name == "BUY"` / `== "SLL"` **두 리터럴**만 봤다. 그런데
# 공식 문서(`docs/efriend` xlsx, "선물옵션 잔고현황" 시트 `CTFO6118R`/`VTFO6118R`)의
# `sll_buy_dvsn_name` 설명은 이렇게 적혀 있다:
#
#     매수잔고인 경우, "매수" 혹은 "BUY"로 출력
#     매도잔고인 경우, "매도" 혹은 ...
#
# **즉 KIS가 한글로 줄 수 있다.** 그리고 이 값은 이 저장소에서 **한 번도 실측된 적이 없다** —
# 주문이 나간 적이 없어(`execution_logs` 0행) `output1`에 보유 종목이 담긴 응답을 받아본
# 적이 없기 때문이다. `tests/`의 "BUY"/"SLL" 픽스처는 **문서에서 옮겨 적은 값**이다.
#
# ## 못 알아보면 무슨 일이 나는가 — 두 안전장치가 동시에 무력화된다
#
# 못 알아본 값은 buy도 sell도 아니게 되어 카운트가 **조용히 0**이 된다. 그러면:
#   1. `risk/limits.py`의 `same_direction_positions >= max_same_direction_positions`(3)이
#      **영원히 성립하지 않는다** — 동일방향 한도가 사라진다.
#   2. `execution/entry.py`의 `forbid_averaging_down(has_open_position_same_direction, ...)`이
#      항상 False를 받는다 — **물타기 금지가 사라진다.**
# 둘 다 「없는데 있는 척」이 아니라 **「있는데 없는 척」**이라, 로그에도 아무 흔적이 없다.
# 계명 12(조용한 폴백 금지)가 정확히 이 형태를 겨눈다.
#
# ## 그래서 이렇게 한다
#
# (a) 문서가 말한 변형을 전부 받는다. (b) `sll_buy_dvsn_cd`(매도매수구분코드, 같은 문서에
# Required로 있다)를 **2차 근거**로 함께 본다. (c) 그래도 못 알아본 값은 버리지 않고
# `unknown_side_count`로 세고 **경고를 남긴다** — 8/18에 첫 포지션이 생기는 날 그 로그가
# 실측값을 알려준다(그때 이 상수를 실측 기준으로 좁힌다).
#
# 코드값 매핑 근거: KIS 국내주식/선물옵션 공통 관례로 01=매도, 02=매수다. **이것도 미실측이라**
# 이름값(`_name`)을 먼저 보고 코드값은 보조로만 쓴다 — 둘이 어긋나면 이름값이 이긴다.
_BUY_SIDE_TOKENS = frozenset({"BUY", "매수", "02"})
_SELL_SIDE_TOKENS = frozenset({"SLL", "SELL", "매도", "01"})

SIDE_BUY = "BUY"
SIDE_SELL = "SELL"
SIDE_UNKNOWN = "UNKNOWN"


def classify_side(name: object, code: object = None) -> str:
    """
    입력: `output1[].sll_buy_dvsn_name`, (선택) `output1[].sll_buy_dvsn_cd`.
    계산: 이름값을 먼저 보고, 판정이 안 되면 코드값으로 보조 판정한다.
    반환: `SIDE_BUY` / `SIDE_SELL` / `SIDE_UNKNOWN`.
    해석: 빈칸("당일 잔고를 청산하여 잔고가 없는 경우 빈칸으로 출력")은 **UNKNOWN이 아니라
         포지션 없음**이다 — 호출측이 그 행을 버린다. UNKNOWN은 「값이 있는데 우리가 모르는
         값」만을 뜻한다(둘을 섞으면 매일 오경보가 난다).
    실패 조건: 없음 — 모르는 것은 UNKNOWN으로 드러낸다(예외를 던지지 않는다: 잔고 조회 한 번의
              실패가 관측 루프를 세우면 안 된다).
    """
    for token in (name, code):
        if token is None:
            continue
        text = str(token).strip().upper()
        if not text:
            continue
        if text in _BUY_SIDE_TOKENS:
            return SIDE_BUY
        if text in _SELL_SIDE_TOKENS:
            return SIDE_SELL
    return SIDE_UNKNOWN


@dataclass(frozen=True, slots=True)
class PositionRecord:
    """브로커 잔고 `output1` 한 행 = 보유 종목 하나.

    **필드는 공식 문서에서 Required로 확인된 것만 담는다**(R8 / 계명 11 — 필드 실측 없는
    스키마 금지). 문서에 없는 것을 추측해 넣지 않고, 대신 `raw`에 원본 dict를 그대로 들고
    있어 8/18 첫 포지션 날에 실제 값을 그 자리에서 읽을 수 있게 한다(R7 — 원본 페이로드 보존).

    수치는 문서상 전부 string이라 `_to_float`을 통과시킨다(빈칸 = 0.0).
    """

    symbol: str  # shtn_pdno — 단축상품번호 (예: 101P09)
    side: str  # classify_side() 결과
    qty: float  # cblc_qty — 잔고수량
    avg_price: float  # ccld_avg_unpr1 — 체결평균단가1
    current_price: float  # idx_clpr — 지수종가
    eval_pnl: float  # evlu_pfls_amt — 평가손익금액
    liquidatable_qty: float  # lqd_psbl_qty — 청산가능수량
    raw: dict = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class BalanceSnapshot:
    timestamp: datetime
    prsm_dpast: float  # 추정예탁자산 — 일간/주간 손익, 드로우다운의 기준값
    evlu_pfls_amt_smtl: float  # 평가손익금액합계
    trad_pfls_amt_smtl: float  # 매매손익금액합계
    dnca_cash: float
    ord_psbl_cash: float
    mgna_tota: float
    same_direction_buy_count: int
    same_direction_sell_count: int
    # 2026-08-16 (Block B) — 위 두 카운트로 설명되지 않는 보유 종목 수.
    # **0이 아니면 위 두 카운트를 신뢰할 수 없다**(그만큼이 어느 쪽에도 안 세어졌다).
    unknown_side_count: int = 0
    positions: tuple[PositionRecord, ...] = ()


def _to_float(value) -> float:
    return float(value) if value not in (None, "") else 0.0


def parse_balance_response(response: dict, timestamp: datetime) -> BalanceSnapshot:
    """
    입력: `KISRestClient.get_balance()` 원본 응답(`output1` 배열 + `output2` dict), 조회 시각.
    계산: `output2`의 문자열 숫자 필드를 float으로 변환하고, `output1`의 각 행을
         `PositionRecord`로 만들면서 `classify_side()`로 방향을 판정해 방향별 포지션 수를 센다.
    해석: **빈칸은 포지션 없음이다** — 문서가 "당일 잔고를 청산하여 잔고가 없는 경우 빈칸으로
         출력"이라고 적었고, 잔고수량까지 0이면 그 행은 이미 청산된 종목이라 `positions`에도
         담지 않는다. 반면 **값이 있는데 우리가 모르는 값**은 `unknown_side_count`로 세고
         경고를 남긴다 — 상세 근거는 `_BUY_SIDE_TOKENS` 위 주석.
    실패 조건: `output2`가 없으면(비정상 응답 — 예: 요청 파라미터 오류로 인한 실패 응답)
              ValueError. `output1`이 없으면 빈 배열로 취급(포지션 없음).
    """
    output2 = response.get("output2")
    if output2 is None:
        raise ValueError("get_balance() 응답에 output2가 없습니다")

    rows = response.get("output1") or []
    records: list[PositionRecord] = []
    buy_count = sell_count = unknown_count = 0

    for row in rows:
        raw_name = row.get("sll_buy_dvsn_name")
        qty = _to_float(row.get("cblc_qty"))
        # 청산돼 흔적만 남은 행 — 방향도 수량도 없다. 이것을 UNKNOWN으로 세면 매일 오경보가 난다.
        if not str(raw_name or "").strip() and qty == 0.0:
            continue

        side = classify_side(raw_name, row.get("sll_buy_dvsn_cd"))
        if side == SIDE_BUY:
            buy_count += 1
        elif side == SIDE_SELL:
            sell_count += 1
        else:
            unknown_count += 1
            # **원본 값을 그대로 찍는다.** 이 한 줄이 8/18에 실측값을 알려준다 —
            # 지금 이 저장소는 KIS가 무엇을 보내는지 모른다(위 주석).
            logger.warning(
                "잔고 방향 판정 실패 — 동일방향 한도와 물타기 금지가 이 종목을 못 센다: "
                "symbol=%s sll_buy_dvsn_name=%r sll_buy_dvsn_cd=%r cblc_qty=%r",
                row.get("shtn_pdno"), raw_name, row.get("sll_buy_dvsn_cd"), row.get("cblc_qty"),
            )

        records.append(
            PositionRecord(
                symbol=str(row.get("shtn_pdno") or ""),
                side=side,
                qty=qty,
                avg_price=_to_float(row.get("ccld_avg_unpr1")),
                current_price=_to_float(row.get("idx_clpr")),
                eval_pnl=_to_float(row.get("evlu_pfls_amt")),
                liquidatable_qty=_to_float(row.get("lqd_psbl_qty")),
                raw=dict(row),
            )
        )

    return BalanceSnapshot(
        timestamp=timestamp,
        prsm_dpast=_to_float(output2.get("prsm_dpast")),
        evlu_pfls_amt_smtl=_to_float(output2.get("evlu_pfls_amt_smtl")),
        trad_pfls_amt_smtl=_to_float(output2.get("trad_pfls_amt_smtl")),
        dnca_cash=_to_float(output2.get("dnca_cash")),
        ord_psbl_cash=_to_float(output2.get("ord_psbl_cash")),
        mgna_tota=_to_float(output2.get("mgna_tota")),
        same_direction_buy_count=buy_count,
        same_direction_sell_count=sell_count,
        unknown_side_count=unknown_count,
        positions=tuple(records),
    )


def snapshot_to_row(snapshot: BalanceSnapshot) -> dict:
    """`db.insert_account_balance_snapshot()`에 바로 넘길 수 있는 dict로 펼친다."""
    return {
        "timestamp": snapshot.timestamp,
        "prsm_dpast": snapshot.prsm_dpast,
        "evlu_pfls_amt_smtl": snapshot.evlu_pfls_amt_smtl,
        "trad_pfls_amt_smtl": snapshot.trad_pfls_amt_smtl,
        "dnca_cash": snapshot.dnca_cash,
        "ord_psbl_cash": snapshot.ord_psbl_cash,
        "mgna_tota": snapshot.mgna_tota,
        "same_direction_buy_count": snapshot.same_direction_buy_count,
        "same_direction_sell_count": snapshot.same_direction_sell_count,
        # 마이그레이션 030. **0이 아닌 날은 위 두 카운트를 신뢰하지 않는다** — 그날치
        # 동일방향 한도 판정은 보수적으로 부풀려져 있다(`same_direction_positions()` 주석).
        "unknown_side_count": snapshot.unknown_side_count,
    }


def position_rows(snapshot: BalanceSnapshot) -> list[dict]:
    """
    입력: 잔고 스냅샷.
    계산: `db.insert_position_snapshots()`에 바로 넘길 수 있는 dict 목록으로 펼친다.
    해석: **브로커가 진실원천이고 이 테이블은 미러다**(L12/R12 — 재시작 복원은 브로커 API
         재조회 → Reconciler 대사 순). 그래서 「현재 포지션」을 이 테이블에서 읽어 판단하지
         않는다. 쌓는 이유는 둘이다: (a) 사후에 「그때 무엇을 들고 있었나」를 답할 수 있어야
         하고, (b) `raw`가 **KIS가 실제로 보낸 필드와 값**을 보존하므로 8/18 첫 포지션 날의
         이 행 하나가 곧 실측 범위표의 원재료가 된다(R8은 실측 후 스키마 확정을 요구한다 —
         이 컬럼들은 문서 확인분이고, 확정은 실측 뒤다).
    실패 조건: 없음 — 포지션이 없으면 빈 목록.
    """
    return [
        {
            "timestamp": snapshot.timestamp,
            "symbol": p.symbol,
            "side": p.side,
            "qty": p.qty,
            "avg_price": p.avg_price,
            "current_price": p.current_price,
            "eval_pnl": p.eval_pnl,
            "liquidatable_qty": p.liquidatable_qty,
            "raw": p.raw,
        }
        for p in snapshot.positions
    ]


def same_direction_positions(snapshot: BalanceSnapshot, candidate_side: str) -> int:
    """
    입력: 잔고 스냅샷, 진입하려는 방향("BUY"/"SELL").
    계산: 후보와 **같은 방향**의 기존 포지션 수 + `unknown_side_count`.
    해석: **모르는 것은 후보 방향으로 센다 — 모르면 안전한 쪽**(진입을 막는 쪽)이다.
         v6 §12.2의 동일방향 한도는 후보와 같은 방향의 포지션만 세는데, 방향을 못 읽은 종목은
         「같은 방향일 수도 있는 종목」이므로 한도 쪽에 붙이는 것이 보수적이다. 반대로 처리하면
         KIS가 한글을 보내기 시작한 날 한도가 통째로 사라진다(`_BUY_SIDE_TOKENS` 위 주석).
         정상 운영에서 `unknown_side_count`는 0이므로 **평시 동작은 종전과 완전히 같다.**
    실패 조건: 없음.
    """
    matched = (
        snapshot.same_direction_buy_count
        if candidate_side.upper() == SIDE_BUY
        else snapshot.same_direction_sell_count
    )
    return matched + snapshot.unknown_side_count


def has_open_position_same_direction(snapshot: BalanceSnapshot, candidate_side: str) -> bool:
    """
    입력: 잔고 스냅샷, 진입하려는 방향("BUY"/"SELL").
    계산: `same_direction_positions() > 0`.
    해석: `ExecutionEngine.EntryRequest.has_open_position_same_direction`에 그대로 넘긴다 —
         그 값이 `entry.forbid_averaging_down()`의 첫 인자이고, 거기서 「새 신호 없는 추가
         진입」을 막는다(v6 §13.2 물타기 기본 금지).

         **이 함수가 없던 동안 그 자리는 기본값 False였다.** 즉 물타기 금지는 구현돼 있었지만
         입력이 언제나 「같은 방향 포지션 없음」이라 발동할 수 없었다 — 브로커가 답을 갖고
         있는데 아무도 묻지 않은 형태이고, 08-04에 `ofi=None` 하드코딩이 앙상블 멤버를 죽였던
         것과 같은 종류다(그때도 `market_raw_1m.ofi`는 이미 채워져 있었다).
    실패 조건: 없음.
    """
    return same_direction_positions(snapshot, candidate_side) > 0


def _pct_change(current: float, baseline: float) -> float:
    if baseline == 0:
        return 0.0
    return (current - baseline) / baseline


def build_account_state(
    latest: BalanceSnapshot,
    start_of_day: BalanceSnapshot | None,
    start_of_week: BalanceSnapshot | None,
    peak_prsm_dpast: float | None,
    candidate_side: str,
    daily_trades_by_strategy: dict[str, int],
    pending_trade_loss_pct: float = 0.0,
) -> AccountState:
    """
    입력: 최신 스냅샷, 오늘/이번주 시작 시점 기준 스냅샷(`db.account_balance_snapshot_before()`
         — 없으면 baseline 미확정), 역대 최고 `prsm_dpast`(`db.max_account_balance_ever()` —
         없으면 드로우다운 미확정), 진입하려는 방향("BUY"/"SELL"), 전략별 오늘 거래 횟수,
         진입하려는 트레이드의 예상 최대손실률.
    계산: `daily_pnl_pct`/`weekly_pnl_pct` = (latest.prsm_dpast − baseline.prsm_dpast) /
         baseline.prsm_dpast, `drawdown_pct` = (latest.prsm_dpast − peak) / peak,
         `same_direction_positions`는 `candidate_side`에 맞는 buy/sell 카운트를 그대로 쓴다
         (매수 후보면 기존 매수 포지션 수, 매도 후보면 기존 매도 포지션 수 — v6 §12.2
         "동일 방향 동시 포지션" 한도가 후보와 같은 방향의 기존 포지션만 센다는 의미와 일치).
    해석: baseline/peak이 없으면(운영 첫 날 등) 0.0으로 폴백 — "손익 없음"이 아니라 "아직 비교할
         과거가 없다"는 뜻이지만, 두 상태를 리스크 게이트 관점에서 구분할 필요는 없다(어느
         쪽이든 한도 위반은 아니므로).
    실패 조건: 없음 — 전부 안전한 기본값으로 흡수.
    """
    daily_baseline = start_of_day.prsm_dpast if start_of_day is not None else 0.0
    weekly_baseline = start_of_week.prsm_dpast if start_of_week is not None else 0.0
    peak = peak_prsm_dpast if peak_prsm_dpast is not None else 0.0

    # 2026-08-16 (Block B) — 방향 판정 실패분을 후보 방향에 포함한다(모르면 안전한 쪽).
    # 종전 인라인 계산을 `same_direction_positions()`로 옮겨 COCKPIT·리스크 게이트가 **같은
    # 함수**를 쓰게 한다(리포트와 배지가 다른 답을 내면 어느 쪽을 믿을지 알 수 없다 — 규약).
    same_direction = same_direction_positions(latest, candidate_side)

    return AccountState(
        daily_pnl_pct=_pct_change(latest.prsm_dpast, daily_baseline) if start_of_day is not None else 0.0,
        weekly_pnl_pct=_pct_change(latest.prsm_dpast, weekly_baseline) if start_of_week is not None else 0.0,
        drawdown_pct=_pct_change(latest.prsm_dpast, peak) if peak_prsm_dpast is not None else 0.0,
        same_direction_positions=same_direction,
        daily_trades_by_strategy=daily_trades_by_strategy,
        pending_trade_loss_pct=pending_trade_loss_pct,
    )
