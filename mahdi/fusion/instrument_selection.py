"""Signal Fusion — Instrument Selection (v6 §11.5).

§11.4까지의 출력은 **전략 이름**(`atm_long`, `debit_spread`, …)이고 Execution의 입력은
**단축상품번호**(`EntryContext.symbol`)다. 이 모듈이 그 사이를 잇는다 — 2026-08-17에
스펙(§11.5)이 생기기 전까지 이 계층은 코드에도 스펙에도 없었고, 유일한 호출부인 백테스트가
`symbol="BACKTEST"` 자리표시자로 그 빈칸을 메우고 있었다.

## 이 모듈이 지키는 네 가지

1. **순수 함수다.** DB도 종목 마스터도 직접 읽지 않는다 — 체인 레그 목록과 기준가를 받고,
   단축코드 변환은 `symbol_resolver` 콜백으로 주입받는다. 그래야 라이브·백테스트·테스트가
   같은 선택 로직을 쓴다.
2. **빈 후보 목록은 실패가 아니라 판정이다.** 전 후보가 유동성 필터에 걸리면
   `REASON_NO_LIQUID_INSTRUMENT`를 남기고 진입하지 않는다(`defensive_regime_no_new_entries`와
   같은 규약). 「후보가 없었다」와 「선택기가 안 돌았다」가 구분되지 않으면 이 기록은 쓸모없다.
3. **근거를 버리지 않는다.** 무엇이 이 행사가를 골랐는지(델타·OI·거래량·스프레드·규칙 이름)를
   후보에 함께 싣는다 — 계산해 놓고 버리면 나중에 답할 수 없다(`member_scores`의 교훈).
4. **모르는 것을 지어내지 않는다.** §11.5 표에 규칙이 없는 전략(`long_gamma`,
   프리미엄 매도 계열의 행사가)은 후보를 만들지 않고 `REASON_NO_SELECTION_RULE`을 남긴다.
   유동성 임계도 설정이 비어 있으면 그 필터를 **걸지 않는다** — 모르는 채 임계를 정하면
   그 임계가 곧 결론이 된다(08-05 스팟 괴리율에서 한 실수).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date

# ===== 사유 코드 =====
# 후보가 비었을 때 **왜 비었는지**를 가른다. 전부 `signal_decisions`에 그대로 실린다.
REASON_NO_CHAIN = "no_chain_snapshot"
"""체인 스냅샷 자체가 비었다 — 수집이 죽었거나 아직 안 돌았다(선택기 문제가 아니다)."""
REASON_NO_ELIGIBLE_BOOK = "no_eligible_book"
"""관측된 만기가 전부 당일 만기이거나 하나도 없다 — 0DTE 전용 북만 남은 상태."""
REASON_NO_LIQUID_INSTRUMENT = "no_liquid_instrument"
"""후보는 만들었으나 전부 유동성 하한에 걸렸다 — §11.5가 이름 붙인 그 판정."""
REASON_NO_SELECTION_RULE = "no_selection_rule"
"""§11.5 표에 그 전략의 행사가 규칙이 없다 — 지어내지 않고 비워 둔다."""
REASON_SPOT_UNAVAILABLE = "spot_unavailable"
"""ATM 기준가(선물 체결가)가 없다 — 행사가를 고를 기준점이 없다."""
REASON_NO_STRIKE_MATCH = "no_strike_match"
"""규칙이 요구한 행사가/델타 밴드에 해당하는 레그가 체인에 없다."""

_CALL, _PUT = "C", "P"


@dataclass(frozen=True, slots=True)
class ChainLeg:
    """선택기가 보는 체인 한 레그 — `db._chain_snapshot()` 행에서 필요한 것만 추린 형태.

    `delta`/`volume`/`spread_state`가 `None`인 것과 `0`인 것은 **다른 사실이다**:
    None은 「모른다」, 0은 「쟀는데 0이다」. 유동성 필터는 뒤쪽만 걸러야 한다.
    """

    expiry: date
    strike: float
    option_type: str  # "C" | "P"
    delta: float | None = None
    oi: int | None = None
    volume: int | None = None
    spread_state: int | None = None


def legs_from_chain_snapshot(rows: Iterable[dict]) -> list[ChainLeg]:
    """
    입력: `db.latest_option_chain()` / `db.option_chain_as_of()`가 돌려준 dict 목록.
    계산: 선택에 쓰는 필드만 뽑아 `ChainLeg`로 옮긴다. `option_type`은 대문자로 정규화한다
         (DB는 CHAR(1)에 "C"/"P"를 넣지만 `OptionLeg`는 소문자를 쓴다 — 두 관례가 한 프로세스
         안에 있으므로 경계에서 한 번 고정한다).
    해석: 만기/행사가가 없는 행은 선택 대상이 될 수 없으므로 건너뛴다.
    실패 조건: 없음 — 못 쓰는 행은 조용히 빠진다(빈 목록은 호출측이 사유로 다룬다).
    """
    legs: list[ChainLeg] = []
    for row in rows:
        expiry, strike = row.get("expiry"), row.get("strike")
        option_type = str(row.get("option_type") or "").upper()
        if expiry is None or strike is None or option_type not in (_CALL, _PUT):
            continue
        legs.append(
            ChainLeg(
                expiry=expiry,
                strike=float(strike),
                option_type=option_type,
                delta=None if row.get("delta") is None else float(row["delta"]),
                oi=None if row.get("oi") is None else int(row["oi"]),
                volume=None if row.get("volume") is None else int(row["volume"]),
                spread_state=None if row.get("spread_state") is None else int(row["spread_state"]),
            )
        )
    return legs


@dataclass(frozen=True, slots=True)
class LiquidityThresholds:
    """§11.5 유동성 하한 — **전 필드가 None/False면 아무것도 안 거른다**(레버 OFF와 같은 상태).

    초기값을 코드에 박지 않는다. 모의투자 실측 분포를 본 뒤 `strategy_params.yaml`에 넣는다.
    """

    min_oi: int | None = None
    max_spread_state: int | None = None
    require_traded_today: bool = False

    @property
    def any_enabled(self) -> bool:
        return self.min_oi is not None or self.max_spread_state is not None or self.require_traded_today

    @classmethod
    def from_params(cls, params: dict | None) -> "LiquidityThresholds":
        """
        입력: `strategy_params.yaml`의 `instrument_selection.liquidity` 하위 dict(없으면 None).
        계산: 키가 없거나 값이 null이면 그 필터는 꺼진 채로 둔다.
        실패 조건: 없음 — 잘못된 타입은 그 필터만 꺼진다(설정 오타로 진입이 막히면 안 된다).
        """
        cfg = params or {}

        def _int_or_none(key: str) -> int | None:
            value = cfg.get(key)
            try:
                return None if value is None else int(value)
            except (TypeError, ValueError):
                return None

        return cls(
            min_oi=_int_or_none("min_oi"),
            max_spread_state=_int_or_none("max_spread_state"),
            require_traded_today=bool(cfg.get("require_traded_today", False)),
        )


def liquidity_reject_reason(leg: ChainLeg, thresholds: LiquidityThresholds) -> str | None:
    """
    입력: 체인 레그, 유동성 하한.
    계산: 걸리는 첫 조건의 이름을 돌려준다(`oi_below_min` / `spread_above_max` / `not_traded_today`).
    해석: `None`이면 통과. **값이 `None`인 필드는 통과시킨다** — 「모른다」를 「나쁘다」로 읽으면
         체인 파싱이 한 컬럼 실패한 날 전 후보가 사라진다(그것은 유동성 판정이 아니라 결손이다).
    실패 조건: 없음.
    """
    if thresholds.min_oi is not None and leg.oi is not None and leg.oi < thresholds.min_oi:
        return "oi_below_min"
    if (
        thresholds.max_spread_state is not None
        and leg.spread_state is not None
        and leg.spread_state > thresholds.max_spread_state
    ):
        return "spread_above_max"
    if thresholds.require_traded_today and leg.volume is not None and leg.volume <= 0:
        return "not_traded_today"
    return None


def observed_expiries(legs: Sequence[ChainLeg]) -> list[date]:
    """계산: 체인에 실제로 관측된 만기를 오름차순 중복 없이. 실패 조건: 없음(빈 입력 → 빈 목록)."""
    return sorted({leg.expiry for leg in legs})


def is_expiry_day(legs: Sequence[ChainLeg], today: date) -> bool:
    """
    입력: 체인 레그 목록, 오늘 날짜.
    계산: 관측된 만기 중 오늘이 있는가.
    해석: **이 값이 만기 여부의 단일 출처다.** `exit_stack.exit_rules_key(is_expiry_day=...)`에
         같은 값을 먹여야 한다 — 두 곳이 만기 여부를 따로 판정하면 어느 쪽이 옳은지 알 수 없게
         되고, 한쪽은 0DTE 파라미터로 청산하면서 다른 쪽은 일반 후보를 만드는 상태가 된다.
         요일 규칙으로 만들지 않는 이유는 `db.observed_future_expiries()` 주석에 있다 —
         2026-08-18 만기는 **화요일**이다(08-17 대체공휴일 이월).
    실패 조건: 없음.
    """
    return any(leg.expiry == today for leg in legs)


def select_book(legs: Sequence[ChainLeg], today: date) -> tuple[date | None, str | None]:
    """
    입력: 체인 레그 목록, 오늘 날짜.
    계산: 관측된 만기 중 **오늘을 제외한** 가장 가까운 만기를 고른다.
    해석: 만기 당일 북은 0DTE 플레이북 전용이라 일반 전략의 후보에서 제외한다(§11.4/§11.5).
         잔존만기 0일에는 감마플립이 정의되지 않고 핀 리스크만 의미를 갖는다.
    실패 조건: 쓸 만기가 없으면 `(None, REASON_NO_ELIGIBLE_BOOK)`. 체인 자체가 비었으면
              `(None, REASON_NO_CHAIN)` — 둘은 다른 사건이라 사유를 가른다.
    """
    if not legs:
        return None, REASON_NO_CHAIN
    future = [expiry for expiry in observed_expiries(legs) if expiry > today]
    if not future:
        return None, REASON_NO_ELIGIBLE_BOOK
    return future[0], None


@dataclass(frozen=True, slots=True)
class SelectedLeg:
    """고른 한 다리 + **왜 이것인가**."""

    expiry: date
    strike: float
    option_type: str
    side: str  # BUY | SELL
    rule: str  # 이 다리를 고른 규칙 이름 — 사후에 "무엇이 이 행사가를 골랐나"에 답하는 자리
    symbol: str | None = None  # 단축상품번호. None이면 마스터에서 못 찾았다는 뜻
    delta: float | None = None
    oi: int | None = None
    volume: int | None = None
    spread_state: int | None = None

    def to_record(self) -> dict:
        """JSONB 적재용 — 날짜는 ISO 문자열로 내린다(psycopg가 dict 안의 date를 모른다)."""
        return {
            "symbol": self.symbol,
            "expiry": self.expiry.isoformat(),
            "strike": self.strike,
            "option_type": self.option_type,
            "side": self.side,
            "rule": self.rule,
            "delta": self.delta,
            "oi": self.oi,
            "volume": self.volume,
            "spread_state": self.spread_state,
        }


@dataclass(frozen=True, slots=True)
class InstrumentCandidate:
    strategy: str
    legs: tuple[SelectedLeg, ...]

    def to_record(self) -> dict:
        return {"strategy": self.strategy, "legs": [leg.to_record() for leg in self.legs]}


@dataclass(frozen=True, slots=True)
class SelectionResult:
    """선택기 1회 실행 결과.

    `candidates`가 비어 있어도 `reason`과 `rejected`가 **왜**를 담는다 — 그것이 이 기록의 값이다.
    """

    candidates: tuple[InstrumentCandidate, ...] = ()
    book_expiry: date | None = None
    reason: str | None = None
    rejected: tuple[dict, ...] = ()

    def to_record(self) -> dict:
        return {
            "candidates": [c.to_record() for c in self.candidates],
            "book_expiry": self.book_expiry.isoformat() if self.book_expiry else None,
            "reason": self.reason,
            "rejected": list(self.rejected),
        }


# ===== §11.5 전략별 선택 규칙 =====
#
# 표에 **없는 것은 만들지 않는다.** 규칙이 없는 전략은 후보를 만들지 않고 사유를 남긴다:
#
#   long_gamma                     §11.5 표에 행이 없다(VOL_EXPANSION×fair 셀).
#   limited_premium_sell 계열      표가 "게이트 통과 시에만 후보 생성"이라고만 적었고
#                                  **행사가 규칙을 주지 않았다.** 매도 다리의 행사가를 여기서
#                                  지어내면 그 숫자가 곧 프리미엄 매도의 위험 프로파일이 된다.
#
# 이 둘이 `no_selection_rule`로 남는 것은 결함이 아니라 스펙의 공백을 그대로 드러낸 것이다.
_ATM = "atm"
_DELTA_BAND = "delta_band"
_STRIKE_STEPS_OUT = "strike_steps_out"


@dataclass(frozen=True, slots=True)
class _LegRule:
    kind: str
    side: str
    # option_type이 None이면 방향(direction)에서 정한다(콜=상승/풋=하락).
    option_type: str | None = None
    delta_low: float | None = None
    delta_high: float | None = None
    steps_out: tuple[int, ...] = ()


# §11.5 "전략별 선택 파라미터" 표를 그대로 옮긴 것.
_STRATEGY_RULES: dict[str, tuple[_LegRule, ...]] = {
    # ATM 최근접 행사가 (선물 체결가 기준), 콜 또는 풋 1다리.
    "atm_long": (_LegRule(kind=_ATM, side="BUY"),),
    # |델타| 0.60~0.70.
    "itm_debit": (_LegRule(kind=_DELTA_BAND, side="BUY", delta_low=0.60, delta_high=0.70),),
    # 매수 ATM ±1 / 매도 2~3행사가 밖 (양다리 동일 만기).
    #
    # "매수 ATM ±1"의 ±1은 **오프셋이 아니라 허용 오차**로 읽는다 — ATM 최근접을 사고, 그 격자가
    # 한 칸 어긋나 있어도 같은 다리로 본다는 뜻이다. 오프셋으로 읽으면 어느 쪽으로 1칸인지를
    # 표가 정해주지 않아 우리가 정하게 된다.
    # 매도 다리는 2칸 밖을 우선하고 없으면 3칸 밖 — 표가 준 범위 안에서만 움직인다.
    "debit_spread": (
        _LegRule(kind=_ATM, side="BUY"),
        _LegRule(kind=_STRIKE_STEPS_OUT, side="SELL", steps_out=(2, 3)),
    ),
    # 동일 ATM 행사가 콜+풋.
    "atm_straddle": (
        _LegRule(kind=_ATM, side="BUY", option_type=_CALL),
        _LegRule(kind=_ATM, side="BUY", option_type=_PUT),
    ),
    # 동일 ATM, 분할 진입(회당 상한은 Risk가 결정하므로 여기서는 스트래들과 같은 다리를 낸다).
    "straddle_accumulate": (
        _LegRule(kind=_ATM, side="BUY", option_type=_CALL),
        _LegRule(kind=_ATM, side="BUY", option_type=_PUT),
    ),
    # 양쪽 |델타| ≈ 0.25 — 표의 "≈"를 밴드로 옮긴다(0.20~0.30).
    "small_strangle_buy": (
        _LegRule(kind=_DELTA_BAND, side="BUY", option_type=_CALL, delta_low=0.20, delta_high=0.30),
        _LegRule(kind=_DELTA_BAND, side="BUY", option_type=_PUT, delta_low=0.20, delta_high=0.30),
    ),
}


def _atm_strike(strikes: Sequence[float], spot: float) -> float | None:
    """계산: 기준가에 가장 가까운 상장 행사가. 동률이면 낮은 쪽(결정론적이면 충분하다)."""
    if not strikes:
        return None
    return min(strikes, key=lambda s: (abs(s - spot), s))


def _strikes_for(legs: Sequence[ChainLeg], expiry: date, option_type: str) -> list[float]:
    return sorted({leg.strike for leg in legs if leg.expiry == expiry and leg.option_type == option_type})


def _find_leg(legs: Sequence[ChainLeg], expiry: date, option_type: str, strike: float) -> ChainLeg | None:
    for leg in legs:
        if leg.expiry == expiry and leg.option_type == option_type and leg.strike == strike:
            return leg
    return None


def _resolve_leg_rule(
    rule: _LegRule,
    legs: Sequence[ChainLeg],
    expiry: date,
    spot: float,
    direction: int,
) -> tuple[ChainLeg | None, str]:
    """
    입력: 다리 규칙, 체인, 북 만기, ATM 기준가, 방향(+1 상승 / -1 하락).
    계산: 규칙 종류별로 레그 하나를 고른다.
         - `atm`: 기준가 최근접 행사가.
         - `delta_band`: |델타|가 밴드 안인 레그 중 밴드 중앙에 가장 가까운 것.
         - `strike_steps_out`: ATM에서 외가 방향으로 n칸(표가 준 후보 순서대로 첫 성공).
    해석: 반환 2번째 값은 이 다리를 고른 규칙 이름 — 근거 기록에 그대로 실린다.
    실패 조건: 해당 레그가 체인에 없으면 `(None, ...)` — 호출측이 `no_strike_match`로 다룬다.
    """
    option_type = rule.option_type or (_CALL if direction >= 0 else _PUT)
    strikes = _strikes_for(legs, expiry, option_type)
    if not strikes:
        return None, rule.kind

    if rule.kind == _ATM:
        atm = _atm_strike(strikes, spot)
        if atm is None:
            return None, _ATM
        return _find_leg(legs, expiry, option_type, atm), f"atm@{atm:g}"

    if rule.kind == _DELTA_BAND:
        low, high = rule.delta_low or 0.0, rule.delta_high or 1.0
        center = (low + high) / 2
        # 델타가 None인 레그는 **후보에서 뺀다** — 밴드 판정을 못 하는 레그를 통과시키면
        # "규칙대로 골랐다"는 기록이 거짓이 된다.
        banded = [
            leg
            for leg in legs
            if leg.expiry == expiry and leg.option_type == option_type and leg.delta is not None
            and low <= abs(leg.delta) <= high
        ]
        if not banded:
            return None, f"delta_band[{low:g},{high:g}]"
        best = min(banded, key=lambda leg: (abs(abs(leg.delta) - center), leg.strike))
        return best, f"delta_band[{low:g},{high:g}]"

    if rule.kind == _STRIKE_STEPS_OUT:
        atm = _atm_strike(strikes, spot)
        if atm is None:
            return None, _STRIKE_STEPS_OUT
        index = strikes.index(atm)
        # 외가 방향: 콜은 행사가가 높은 쪽, 풋은 낮은 쪽.
        step_sign = 1 if option_type == _CALL else -1
        for steps in rule.steps_out:
            target_index = index + step_sign * steps
            if 0 <= target_index < len(strikes):
                leg = _find_leg(legs, expiry, option_type, strikes[target_index])
                if leg is not None:
                    return leg, f"{steps}_strikes_out@{strikes[target_index]:g}"
        return None, _STRIKE_STEPS_OUT

    return None, rule.kind  # pragma: no cover - 규칙 종류는 위 셋뿐


def select_instruments(
    entry_strategies: Sequence[str],
    legs: Sequence[ChainLeg],
    spot: float | None,
    today: date,
    *,
    direction: int = 1,
    thresholds: LiquidityThresholds | None = None,
    symbol_resolver: Callable[[str, float, date], str | None] | None = None,
) -> SelectionResult:
    """
    입력: §11.4가 남긴 **진입** 전략 목록(`entry_strategies()`로 관망 계열이 이미 걸러진 것),
         체인 레그, ATM 기준가(선물 체결가), 오늘 날짜, 방향(FusionDecision.direction 부호),
         유동성 하한, 단축코드 변환 콜백(`(option_type, strike, expiry) -> symbol | None`).
    계산: (1) 만기 당일을 제외한 최근접 북을 고른다. (2) 전략마다 §11.5 표의 다리 규칙을 적용해
         레그를 고른다. (3) 고른 레그 전부가 유동성 하한을 통과해야 그 전략이 후보가 된다 —
         **한 다리라도 걸리면 그 전략 전체를 뺀다**(스프레드의 한 다리만 유동성이 없으면 그
         전략은 실행 불가다). (4) 남은 후보가 하나도 없으면 사유를 정한다.
    해석: 반환값의 `reason`은 후보가 **비었을 때만** 채워진다. 전략별 탈락 사유는 `rejected`에
         전부 남는다 — 「후보가 없었다」와 「선택기가 안 돌았다」를 가르는 것이 이 기록의 목적이고,
         그 둘이 구분되지 않으면 기록 자체가 쓸모없다(규약 C).
         `symbol_resolver`가 None을 돌려줘도 후보는 **살린다** — 종목코드를 못 찾은 것은 선택의
         실패가 아니라 마스터 조회의 실패이고, 그 사실은 `symbol=None`으로 드러난다. 여기서
         후보를 버리면 두 원인이 한 사유로 뭉개진다.
    실패 조건: 없음 — 모든 경로가 사유 코드로 드러난다.
    """
    thresholds = thresholds or LiquidityThresholds()
    if not legs:
        return SelectionResult(reason=REASON_NO_CHAIN)
    if spot is None:
        return SelectionResult(reason=REASON_SPOT_UNAVAILABLE)

    book_expiry, book_reason = select_book(legs, today)
    if book_expiry is None:
        return SelectionResult(reason=book_reason)

    candidates: list[InstrumentCandidate] = []
    rejected: list[dict] = []
    saw_liquidity_reject = False

    for strategy in entry_strategies:
        rules = _STRATEGY_RULES.get(strategy)
        if rules is None:
            rejected.append({"strategy": strategy, "reason": REASON_NO_SELECTION_RULE})
            continue

        selected: list[SelectedLeg] = []
        failure: dict | None = None
        for rule in rules:
            leg, rule_name = _resolve_leg_rule(rule, legs, book_expiry, spot, direction)
            if leg is None:
                failure = {"strategy": strategy, "reason": REASON_NO_STRIKE_MATCH, "rule": rule_name}
                break
            liquidity_reason = liquidity_reject_reason(leg, thresholds)
            if liquidity_reason is not None:
                saw_liquidity_reject = True
                failure = {
                    "strategy": strategy,
                    "reason": REASON_NO_LIQUID_INSTRUMENT,
                    "rule": rule_name,
                    "detail": liquidity_reason,
                    "strike": leg.strike,
                    "option_type": leg.option_type,
                }
                break
            symbol = None
            if symbol_resolver is not None:
                symbol = symbol_resolver(leg.option_type, leg.strike, leg.expiry)
            selected.append(
                SelectedLeg(
                    expiry=leg.expiry,
                    strike=leg.strike,
                    option_type=leg.option_type,
                    side=rule.side,
                    rule=rule_name,
                    symbol=symbol,
                    delta=leg.delta,
                    oi=leg.oi,
                    volume=leg.volume,
                    spread_state=leg.spread_state,
                )
            )

        if failure is not None:
            rejected.append(failure)
            continue
        candidates.append(InstrumentCandidate(strategy=strategy, legs=tuple(selected)))

    if candidates:
        return SelectionResult(
            candidates=tuple(candidates), book_expiry=book_expiry, rejected=tuple(rejected)
        )

    # 비었을 때의 사유 — **유동성으로 걸린 것이 하나라도 있으면 그것이 사유다**(§11.5가 이름
    # 붙인 판정). 그 외에는 마지막 탈락 사유를 그대로 쓰고, 전략 자체가 없었으면 사유가 없다
    # (그 경우 호출측이 애초에 이 함수를 부르지 않는다 — 부르면 `entry_strategies`가 빈 것이다).
    if saw_liquidity_reject:
        reason = REASON_NO_LIQUID_INSTRUMENT
    elif rejected:
        reason = str(rejected[-1]["reason"])
    else:
        reason = None
    return SelectionResult(book_expiry=book_expiry, reason=reason, rejected=tuple(rejected))
