"""Execution Engine — 포지션 생애주기 원장 + 브로커 대사(Reconciler).

진입 체결 → 보유 → 청산 완결을 잇는 층이다. `execution/*` 전체가 라이브에 배선될 수 없었던
**단일 최상위 블로커**가 이것이었다(2026-08-06 `docs/동작흐름과상태` §2 "배선 선행 조건",
`CURRENT_STATE.md` "재진입 방지 로직 부재 — 배선 전 선행 해결 필요").

## 무엇이 없어서 무엇이 막혔나

포지션이 언제 생겨 언제 사라졌는지 아는 주체가 없었다. 그 빈칸의 대가가 셋이다:

  1. `exit_stack.PositionState`(진입가·**진입시각**·레짐)를 만들 곳이 없다 → **청산 평가 자체가
     불가능**하다. 백테스트는 `symbol="BACKTEST"` 자리표시자로 메우고 있었다.
  2. `entry.forbid_averaging_down(has_open_position_same_direction, ...)`이 채울 값이 없어
     항상 `False`를 받는다 → **물타기 금지가 무력**하다.
  3. `trade_history`가 영원히 0행이다 → 메타라벨 학습(§11.2 Triple Barrier)도, 자기강화
     학습(§14)도, Champion-Challenger(§14.4)도 재료가 없다.

## 이 모듈이 진실원천이 **아닌** 이유 — L12/R12

포지션의 권위는 **브로커**다(*"프로세스는 무상태. 재시작 복원은 브로커 API 재조회 →
Reconciler 대사 → 구독 시작 순"*). 마이그레이션 030이 `position_snapshots`에 대해 같은 말을
이미 적어 뒀고, 이 모듈은 그 규율을 깨지 않는다.

그래서 원장은 **「브로커가 아는 것」을 복제하지 않는다.** 브로커는 이미 안다 — 종목·방향·수량·
평균단가·현재가·청산가능수량. 브로커가 **모르는 것만** 이 원장이 든다:

    언제 들어갔는가 · 어느 전략으로 · 그때 레짐은 무엇이었나 · 확신도는 얼마였나

즉 원장은 **우리 쪽 절반**이고, `reconcile()`이 두 절반을 잇는다. 이렇게 두면 프로세스가
죽었다 살아나도(08-21에 장중 재기동이 실제로 있었다) 「무엇을 들고 있나」는 브로커에게 다시
물어서 알고, 「왜 들고 있나」는 DB에 남아 있다.

## 지어내지 않는다 — 고아 포지션의 진입 시각

브로커에는 있는데 원장에 없는 포지션이 생길 수 있다: 사람이 HTS로 직접 냈거나, 원장 기록이
실패했거나, 원장이 생기기 **전에** 열린 포지션이거나. 그때 진입 시각을 모른다는 것이 사실이고,
**모르는 것을 지어내면 그 순간부터 타임스톱은 허구가 된다.**

그렇다고 「모른다」로 두면 타임스톱이 조용히 사라진다 — `resolve_exit_params()`가 미정의 레짐에
대해 겪은 바로 그 실패다. 답은 **하한**이다:

    직전 잔고 조회에 그 종목이 없었다면, 진입은 그 조회 **이후**다.
    직전 조회가 없으면(기동 첫 사이클) 세션 시작(09:00) 이후다.

하한을 쓰면 보유 시간이 **과대평가**되고 타임스톱은 **더 일찍** 걸린다 — 안전한 쪽으로만
틀린다. 그리고 그 값이 추정이라는 사실을 `opened_at_exact=False`가 들고 다닌다(계명 12:
조용한 폴백 금지). 리포트는 그 플래그를 세어 인쇄한다.

**고아를 강제청산하지 않는 이유**: 사람이 손으로 낸 포지션일 수 있고, 그것을 우리가 「모르는
포지션이니 닫는다」로 처리하면 사람의 거래를 시스템이 지우는 것이 된다. 방어(하드스톱·15:10)는
그대로 걸되, 그 사실을 기록하고 사람이 보게 한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime

from mahdi.data import db

logger = logging.getLogger("mahdi.execution.position_ledger")

# 원장 행이 어떻게 생겼는지 — 이 값이 「진입 시각을 믿을 수 있는가」를 가른다.
ORIGIN_ORDER = "order"  # 우리 주문의 체결로 열렸다(③ 배선 후의 정상 경로).
ORIGIN_ORPHAN = "orphan"  # 브로커에만 있었다 — 사람의 수동 거래이거나 원장 유실.

# ===== 로그 문구는 emit 측 모듈 상수로 둔다 (규약 A) =====
#
# `test_ops_log_metrics_contract.py`가 파서와 emit 측을 왕복 검증하므로, 문구를 파서에 복사해
# 적으면 08-04의 「362건 → 0건」(문구가 바뀌었는데 파서가 눈이 멀었다)이 재현된다.
LOG_POSITION_OPENED = (
    "포지션 개시: %s %s %.0f계약 @%.2f · 출처=%s · 진입시각 %s%s"
)
LOG_POSITION_CLOSED = (
    "포지션 종료: %s %s %.0f계약 · 진입 %s @%.2f → 청산 %s @%.2f · %.0f분 보유 · 사유=%s"
)
LOG_POSITION_QTY_CHANGED = "포지션 수량 변경: %s %s %.0f → %.0f계약 (%s)"
LOG_ORPHAN_POSITION = (
    "원장에 없는 포지션 발견: %s %s %.0f계약 @%.2f — 진입 시각을 모른다(하한 %s로 둔다). "
    "사람이 직접 냈거나 원장 기록이 실패한 것이다. 방어(하드스톱·15:10)는 그대로 걸린다"
)


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """열려 있는 포지션 하나에 대한 **우리 쪽 절반**.

    수량·평균단가는 브로커 값을 그대로 미러링한다(대사 때마다 갱신된다). 원장이 소유하는 것은
    `opened_at` 아래의 넷 — 브로커가 모르는 것들이다.
    """

    symbol: str
    side: str  # BUY / SELL (진입 방향)
    qty: float
    entry_price: float
    opened_at: datetime
    # False면 `opened_at`은 실제 진입 시각이 아니라 **하한**이다(모듈 docstring 참고).
    opened_at_exact: bool = True
    origin: str = ORIGIN_ORDER
    strategy_id: str | None = None
    entry_order_id: str | None = None
    regime_entry: int | None = None
    exit_rules_key: str | None = None
    confidence_entry: float | None = None
    # 대사에서 마지막으로 「아직 있다」를 확인한 시각. 종료 시각의 하한이기도 하다.
    last_seen_at: datetime | None = None

    def held_minutes(self, now: datetime) -> float:
        """
        계산: `opened_at`부터 `now`까지의 분. 음수는 0으로 막는다.
        해석: `opened_at_exact=False`면 이 값은 **상한**이다 — 실제보다 길게 나온다.
             타임스톱이 더 일찍 걸리는 쪽이므로 안전 방향이다(모듈 docstring).
        실패 조건: 없다.
        """
        return max((now - self.opened_at).total_seconds() / 60.0, 0.0)


@dataclass(frozen=True, slots=True)
class ClosedPosition:
    """대사에서 「브로커에 더는 없다」로 판정된 포지션 — `trade_history` 한 행의 재료."""

    entry: LedgerEntry
    closed_at: datetime
    exit_price: float
    exit_reason: str
    # 청산 시각도 하한일 수 있다: 폴링 사이 어느 시점엔가 닫혔고 우리는 그 사실을 폴에서 안다.
    # 즉 `closed_at`은 「이 시각까지는 확실히 닫혔다」이고 실제 체결은 그 이전이다.
    closed_at_exact: bool = False

    @property
    def held_minutes(self) -> float:
        return self.entry.held_minutes(self.closed_at)

    def gross_pnl(self) -> float:
        """
        계산: (청산가 − 진입가) × 수량, 매도 진입이면 부호 반전.
        해석: **수수료·슬리피지를 뺀 값이 아니다**(`net_pnl`은 호출측이 실비용을 알 때 채운다).
             지금 이 값을 net으로 쓰면 백테스트의 비용 가정과 라이브 실적이 섞인다.
        실패 조건: 없다.
        """
        diff = (self.exit_price - self.entry.entry_price) * self.entry.qty
        return diff if self.entry.side.upper() == "BUY" else -diff


@dataclass(frozen=True, slots=True)
class QtyChange:
    """같은 종목·같은 방향인데 수량이 달라진 경우 — 추가 진입 또는 부분 청산."""

    entry: LedgerEntry
    previous_qty: float
    new_qty: float
    at: datetime

    @property
    def increased(self) -> bool:
        return self.new_qty > self.previous_qty


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """한 번의 대사 결과. **네 목록이 서로 겹치지 않는다.**"""

    opened: tuple[LedgerEntry, ...] = ()
    held: tuple[LedgerEntry, ...] = ()
    closed: tuple[ClosedPosition, ...] = ()
    qty_changed: tuple[QtyChange, ...] = ()
    # 이번에 열린 것 중 진입 시각을 모르는 것들(= `origin == ORIGIN_ORPHAN`).
    orphans: tuple[LedgerEntry, ...] = ()

    @property
    def open_entries(self) -> tuple[LedgerEntry, ...]:
        """대사 이후 열려 있는 전부 — 청산 평가 루프(④)가 도는 대상이다."""
        return self.opened + self.held + tuple(c.entry for c in self.qty_changed)


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    """대사에 필요한 만큼만 추린 브로커 잔고 한 행.

    `account_tracker.PositionRecord`를 직접 받지 않는 이유: 이 모듈은 KIS 응답 형식을 몰라야
    하고(`order_manager`가 `BrokerClient` 프로토콜로 같은 규율을 지킨다), 그래야 테스트가
    KIS 픽스처 없이 대사 로직만 검사할 수 있다. 변환은 `from_position_record()`가 한다.
    """

    symbol: str
    side: str
    qty: float
    avg_price: float
    current_price: float


def from_position_record(record) -> BrokerPosition:
    """
    입력: `account_tracker.PositionRecord`(덕 타이핑 — 같은 속성을 가진 무엇이든).
    계산: 대사에 쓰는 다섯 필드만 옮긴다.
    해석: 경계를 한 함수에 몰아 둔다 — KIS 필드명이 실측으로 바뀌어도(R8, 아직 미실측이다)
         고칠 곳이 여기 하나다.
    실패 조건: 없다.
    """
    return BrokerPosition(
        symbol=record.symbol,
        side=record.side,
        qty=float(record.qty),
        avg_price=float(record.avg_price),
        current_price=float(record.current_price),
    )


def reconcile(
    ledger: list[LedgerEntry],
    broker_positions: list[BrokerPosition],
    now: datetime,
    *,
    opened_at_floor: datetime,
    pending_entries: dict[str, LedgerEntry] | None = None,
    default_exit_reason: str = "RECONCILED_FLAT",
) -> ReconcileResult:
    """
    입력: 현재 열려 있는 원장 행들, 브로커 잔고의 포지션들, 대사 시각,
         `opened_at_floor`(진입 시각을 모를 때 쓸 **하한** — 보통 직전 잔고 조회 시각,
         없으면 세션 시작), `pending_entries`(③ 배선 후: 우리가 방금 낸 주문의 진입 맥락을
         종목코드로 찾을 수 있게 넘긴다), 원장에서 사라진 포지션에 붙일 기본 청산 사유.
    계산: 종목코드를 키로 양쪽을 맞춘다.
         · 브로커에만 있다 → **개시**. `pending_entries`에 맥락이 있으면 그것을 쓰고(진입 시각
           정확), 없으면 **고아**로 열되 `opened_at`을 `opened_at_floor`로, `opened_at_exact`를
           False로 둔다.
         · 양쪽에 있고 수량이 같다 → **유지**. 브로커의 평균단가·현재가를 미러링한다.
         · 양쪽에 있는데 수량이 다르다 → **수량 변경**(추가 진입 또는 부분 청산).
         · 원장에만 있다 → **종료**. 청산가는 원장이 마지막으로 본 현재가를 쓴다.
    해석: **이 함수는 순수하다** — DB도 시계도 안 건드린다. 그래야 「기동 첫 사이클의 고아」
         「폴링 사이의 부분 청산」 같은 시퀀스를 테스트가 시각 목록만으로 재현할 수 있다.
         적재와 로깅은 `apply_reconcile()`/`log_reconcile()`의 몫이다.
    실패 조건: 없다. 방향이 뒤집힌 채 같은 종목이 남아 있으면(매수 청산 후 즉시 매도 진입이
              한 사이클 안에서 일어난 경우) **종료 + 개시 두 사건으로 낸다** — 한 행의 방향을
              갈아끼우면 그 트레이드의 손익이 둘 다 사라진다.
    """
    pending = dict(pending_entries or {})
    by_symbol = {entry.symbol: entry for entry in ledger}
    broker_by_symbol = {p.symbol: p for p in broker_positions}

    opened: list[LedgerEntry] = []
    held: list[LedgerEntry] = []
    closed: list[ClosedPosition] = []
    qty_changed: list[QtyChange] = []
    orphans: list[LedgerEntry] = []

    for symbol, position in broker_by_symbol.items():
        existing = by_symbol.get(symbol)
        if existing is not None and existing.side.upper() == position.side.upper():
            mirrored = replace(
                existing,
                qty=position.qty,
                entry_price=position.avg_price or existing.entry_price,
                last_seen_at=now,
            )
            if position.qty != existing.qty:
                qty_changed.append(
                    QtyChange(
                        entry=mirrored, previous_qty=existing.qty, new_qty=position.qty, at=now
                    )
                )
            else:
                held.append(mirrored)
            continue

        # 방향이 뒤집혔으면 옛 행을 먼저 닫는다 — 아래 "원장에만 있다" 루프는 브로커에 종목이
        # 남아 있어서 이 행을 못 잡는다. 여기서 안 닫으면 그 트레이드가 원장에서 증발한다.
        if existing is not None:
            closed.append(
                ClosedPosition(
                    entry=existing,
                    closed_at=now,
                    exit_price=position.current_price or existing.entry_price,
                    exit_reason="SIDE_FLIPPED",
                )
            )

        context = pending.pop(symbol, None)
        if context is not None:
            entry = replace(
                context,
                symbol=symbol,
                side=position.side,
                qty=position.qty,
                entry_price=position.avg_price or context.entry_price,
                last_seen_at=now,
            )
        else:
            entry = LedgerEntry(
                symbol=symbol,
                side=position.side,
                qty=position.qty,
                entry_price=position.avg_price,
                # 하한이다 — 실제 진입은 이 시각 **이후**다. 모듈 docstring 참고.
                opened_at=opened_at_floor,
                opened_at_exact=False,
                origin=ORIGIN_ORPHAN,
                last_seen_at=now,
            )
            orphans.append(entry)
        opened.append(entry)

    for symbol, entry in by_symbol.items():
        if symbol in broker_by_symbol:
            continue  # 위 루프가 유지/변경/방향전환으로 이미 처분했다.
        closed.append(
            ClosedPosition(
                entry=entry,
                closed_at=now,
                # 마지막으로 본 값이다. 실제 체결가는 폴링 사이에 있었고 우리는 그것을 모른다 —
                # `closed_at_exact=False`가 그 사실을 들고 다닌다. ③ 배선 후 우리 주문으로 닫은
                # 경우에는 호출측이 체결가로 덮어쓴다.
                exit_price=entry.entry_price,
                exit_reason=default_exit_reason,
            )
        )

    return ReconcileResult(
        opened=tuple(opened),
        held=tuple(held),
        closed=tuple(closed),
        qty_changed=tuple(qty_changed),
        orphans=tuple(orphans),
    )


def log_reconcile(result: ReconcileResult, now: datetime) -> None:
    """
    입력: 대사 결과, 대사 시각.
    계산: 사건이 있는 경우에만 줄을 낸다 — 개시·종료·수량변경·고아.
    해석: **유지는 안 찍는다.** 매 사이클 「아직 들고 있다」를 찍으면 08-15 `ALERT_ONLY`
         94줄의 재현이고, 그 줄들이 정작 사건을 덮는다(08-21 §1-11이 DEGRADED 14줄로 겪은
         바로 그 형태다). 상태는 `position_snapshots`가 이미 매 사이클 남긴다.
    실패 조건: 없다 — 로깅 실패가 대사를 막지 않는다.
    """
    for entry in result.orphans:
        logger.warning(
            LOG_ORPHAN_POSITION,
            entry.symbol, entry.side, entry.qty, entry.entry_price,
            f"{entry.opened_at:%H:%M:%S}",
        )
    for entry in result.opened:
        if entry.origin == ORIGIN_ORPHAN:
            continue  # 바로 위에서 더 자세한 줄을 이미 냈다.
        logger.info(
            LOG_POSITION_OPENED,
            entry.symbol, entry.side, entry.qty, entry.entry_price, entry.origin,
            f"{entry.opened_at:%H:%M:%S}",
            "" if entry.opened_at_exact else " (하한 — 실제 진입은 그 이후다)",
        )
    for change in result.qty_changed:
        logger.info(
            LOG_POSITION_QTY_CHANGED,
            change.entry.symbol, change.entry.side, change.previous_qty, change.new_qty,
            "추가 진입" if change.increased else "부분 청산",
        )
    for item in result.closed:
        logger.info(
            LOG_POSITION_CLOSED,
            item.entry.symbol, item.entry.side, item.entry.qty,
            f"{item.entry.opened_at:%H:%M:%S}", item.entry.entry_price,
            f"{item.closed_at:%H:%M:%S}", item.exit_price,
            item.held_minutes, item.exit_reason,
        )


def trade_history_row(closed: ClosedPosition) -> dict:
    """
    입력: 종료 판정된 포지션.
    계산: `db.insert_trade_history()`에 바로 넘길 dict. 모르는 값은 **None으로 둔다.**
    해석: `commission`/`slippage`/`net_pnl`을 **0.0으로 채우지 않는 것이 요점이다.** 0은
         「비용이 없었다」이고 None은 「아직 모른다」인데, 이 둘을 섞으면 나중에 이 표로 학습한
         모델이 무비용 거래를 학습한다. 실비용은 체결통보(②)와 수수료 산식이 붙는 날 채운다.
         `gross_pnl`은 지금 계산할 수 있으므로 채운다.

    ## 그 결과 — **이 행은 학습에 안 잡힌다. 의도한 것이다** (2026-08-23 라이브 왕복 확인)

    `db.get_trade_history()`는 `net_pnl IS NULL`인 행을 거른다(ML 학습 매트릭스용이라 그렇다).
    즉 비용을 모르는 동안 `trade_history`에는 행이 쌓이는데 **메타라벨 학습은 여전히 0행을
    본다.** 둘이 어긋나 보이지만 옳은 쪽이다 — 순손익을 모르는 트레이드로 모델을 학습시키면
    그 모델은 비용이 없는 세계를 배운다.

    한도 체계는 다르게 동작한다: `db.daily_trade_counts_by_strategy()`는 `net_pnl`을 안 보므로
    **이 행부터 즉시 세어진다.** 넉 달간 죽어 있던 `max_daily_trades_per_strategy` 한도가
    이 함수의 첫 행과 함께 살아난다.
    실패 조건: 없다.
    """
    entry = closed.entry
    return {
        "strategy_id": entry.strategy_id,
        "symbol": entry.symbol,
        "entry_time": entry.opened_at,
        "exit_time": closed.closed_at,
        "entry_price": entry.entry_price,
        "exit_price": closed.exit_price,
        "qty": int(entry.qty),
        "gross_pnl": closed.gross_pnl(),
        "commission": None,
        "slippage": None,
        "net_pnl": None,
        "regime_entry": entry.regime_entry,
        "confidence_entry": entry.confidence_entry,
        "exit_reason": closed.exit_reason,
        "setup_fingerprint": None,
    }


def ledger_row(entry: LedgerEntry) -> dict:
    """`db.upsert_position_ledger()`에 넘길 dict — 열려 있는 행 하나."""
    return {
        "symbol": entry.symbol,
        "opened_at": entry.opened_at,
        "side": entry.side,
        "qty": entry.qty,
        "entry_price": entry.entry_price,
        "opened_at_exact": entry.opened_at_exact,
        "origin": entry.origin,
        "strategy_id": entry.strategy_id,
        "entry_order_id": entry.entry_order_id,
        "regime_entry": entry.regime_entry,
        "exit_rules_key": entry.exit_rules_key,
        "confidence_entry": entry.confidence_entry,
        "last_seen_at": entry.last_seen_at,
    }


def entry_from_row(row: dict) -> LedgerEntry:
    """DB 행 → `LedgerEntry`. `db.open_position_ledger()`가 돌려준 dict를 그대로 받는다."""
    return LedgerEntry(
        symbol=row["symbol"],
        side=row["side"],
        qty=float(row["qty"] or 0.0),
        entry_price=float(row["entry_price"] or 0.0),
        opened_at=row["opened_at"],
        opened_at_exact=bool(row.get("opened_at_exact", True)),
        origin=row.get("origin") or ORIGIN_ORDER,
        strategy_id=row.get("strategy_id"),
        entry_order_id=row.get("entry_order_id"),
        regime_entry=row.get("regime_entry"),
        exit_rules_key=row.get("exit_rules_key"),
        confidence_entry=(
            None if row.get("confidence_entry") is None else float(row["confidence_entry"])
        ),
        last_seen_at=row.get("last_seen_at"),
    )


@dataclass(frozen=True, slots=True)
class LedgerSummary:
    """리포트·COCKPIT이 읽는 한 줄 요약. **셋을 가른다**(규약 C).

    `unknown_entry_time`이 0이 아니면 그만큼의 포지션은 타임스톱이 하한 위에서 돈다 —
    「걸렸다」와 「걸릴 수 있었는데 시각을 몰랐다」는 다른 사건이다.
    """

    open_count: int = 0
    orphan_count: int = 0
    unknown_entry_time: int = 0
    opened_today: int = 0
    closed_today: int = 0

    @classmethod
    def from_entries(cls, entries: tuple[LedgerEntry, ...], result: ReconcileResult) -> LedgerSummary:
        return cls(
            open_count=len(entries),
            orphan_count=sum(1 for e in entries if e.origin == ORIGIN_ORPHAN),
            unknown_entry_time=sum(1 for e in entries if not e.opened_at_exact),
            opened_today=len(result.opened),
            closed_today=len(result.closed),
        )


def apply_reconcile(conn, result: ReconcileResult) -> dict:
    """
    입력: DB 커넥션, `reconcile()` 결과.
    계산: 열린 행(개시·유지·수량변경)을 전부 upsert하고, 종료된 행은 닫은 **뒤에만**
         `trade_history`에 적재한다.
    반환: `{"upserted": int, "closed": int, "trades": int, "already_closed": int}`.
    해석: **이 함수의 존재 이유는 순서 하나다.** `db.close_position_ledger()`가 True를 돌려준
         경우에만 `insert_trade_history()`를 부른다. 대사가 같은 종료를 두 번 보고하면(폴링
         재시도, 프로세스 재기동 직후의 중복 사이클) 두 번째는 이미 닫힌 행이라 False가 되고
         손익이 두 번 세어지지 않는다. 두 호출을 떼어 놓으면 그 짝이 깨지기 쉬워, 원장의
         불변식과 같은 자리에 둔다.
    실패 조건: 예외를 삼키지 않는다 — 손익 기록의 실패는 그날의 성과를 영구히 틀리게 만들고,
              호출측(폴러의 격리 블록)이 그것을 로그로 남겨야 한다(R7).
    """
    upserted = 0
    for entry in result.open_entries:
        db.upsert_position_ledger(conn, ledger_row(entry))
        upserted += 1

    closed = 0
    trades = 0
    already_closed = 0
    for item in result.closed:
        did_close = db.close_position_ledger(
            conn,
            symbol=item.entry.symbol,
            opened_at=item.entry.opened_at,
            closed_at=item.closed_at,
            exit_price=item.exit_price,
            exit_reason=item.exit_reason,
        )
        if not did_close:
            already_closed += 1
            logger.warning(
                "포지션 종료를 두 번 보고했다: %s (개시 %s) — 이미 닫힌 행이라 "
                "trade_history에 다시 적재하지 않는다",
                item.entry.symbol, item.entry.opened_at,
            )
            continue
        closed += 1
        db.insert_trade_history(conn, trade_history_row(item))
        trades += 1

    return {
        "upserted": upserted,
        "closed": closed,
        "trades": trades,
        "already_closed": already_closed,
    }


def load_open_entries(conn) -> list[LedgerEntry]:
    """
    입력: DB 커넥션.
    계산: `db.open_position_ledger()`의 행들을 `LedgerEntry`로 되돌린다.
    해석: 재시작 복원의 **우리 쪽 절반**이다(L12/R12). 이것이 없으면 재기동 한 번에 모든
         포지션이 고아가 되고 진입 시각이 전부 하한으로 떨어진다 — 08-21에 장중 재기동이
         실제로 있었고, 실거래였다면 그날 열린 포지션의 타임스톱이 그 순간 리셋됐을 것이다.
    실패 조건: 없다 — 열린 포지션이 없으면 빈 목록.
    """
    return [entry_from_row(row) for row in db.open_position_ledger(conn)]
