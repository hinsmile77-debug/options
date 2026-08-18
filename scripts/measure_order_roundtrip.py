"""주문 API 왕복 실측 — 제출 → 조회 → 취소를 **한 번만** 실행하고 전부 기록한다.

2026-08-16 (Block C). 2026-08-18 장중에 사람이 손으로 한 번 돌린다.

## 왜 이 스크립트가 필요한가

`submit_order`/`cancel_order`/`inquire_ccnl` 셋은 오늘까지 **한 번도 호출된 적이 없다.**
Capability Matrix 규율(L9·L19·L26)은 실측 검증 안 된 기능의 사용을 금지하고, 이 저장소는
같은 이유로 여러 번 다쳤다 — 잔고 조회는 필수 파라미터 4개가 빠진 채 **항상 실패**하고 있었고
(2026-07-28에 발견), `output1.gama`는 이름이 문서와 달랐다(2026-07-06).

주문은 그중 가장 되돌리기 어려운 경로다. 그래서 **실주문 왕복을 격리된 스크립트로 딱 한 번**
실행해 (1) 요청 필드가 맞는지 (2) 응답 필드명이 문서와 같은지 (3) 취소가 실제로 되는지를
확인하고, 그 결과를 `docs/dev_memory/KIS_RAW_FIELD_RANGES.md`에 적을 원재료로 남긴다.

## 안전 설계 — 체결되지 않게 만드는 것이 핵심이다

  * **모의계좌 전용.** `KIS_ENV=prod`면 즉시 거부한다(`--i-know-this-is-real`로도 못 넘는다 —
    그런 플래그를 두지 않았다).
  * **1계약 지정가.** 수량은 상수로 1에 고정돼 있고 인자로 바꿀 수 없다.
  * **원거리 지정가.** 매수는 현재가보다 크게 **낮은** 가격, 매도는 크게 **높은** 가격에 낸다
    (`_PRICE_AWAY_RATIO`). 체결되면 실측의 목적(취소 확인)이 사라지고 포지션이 생긴다.
  * **호가단위 격자에 스냅한다 — 항상 «더 멀어지는» 쪽으로만**(매수 내림/매도 올림).
    가장 가까운 격자로 반올림하면 가격이 현재가 쪽으로 움직일 수 있고, 그것은 이 스크립트가
    유일하게 피해야 하는 것에 한 걸음 다가가는 일이다. 근거와 실측은 `_away_price()` 참고.
  * **`--confirm` 없이는 아무것도 보내지 않는다.** 기본 실행은 계획만 인쇄하는 예행연습이다.
  * **취소는 finally에서 반드시 시도한다.** 중간에 무엇이 실패하든 접수된 주문을 남기지 않는다.
  * 장 시간 확인. 정규장이 아니면 경고하고 `--confirm`이라도 멈춘다(`--force-outside-hours`로만 통과).

## 실행

    uv run python scripts/measure_order_roundtrip.py                 # 예행연습(주문 없음)
    uv run python scripts/measure_order_roundtrip.py --confirm       # 실제 왕복 1회

결과는 stdout과 `logs/order_roundtrip_YYYYMMDD_HHMMSS.json`에 남는다 — **응답 원문을 통째로**
저장하므로 필드명을 나중에 다시 읽을 수 있다(R8).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mahdi import session
from mahdi.broker import tr_codes  # noqa: F401 — 아래 market_div_for()가 상수를 쓴다
from mahdi.broker.rest_client import (
    KISRestClient, format_order_price, normalize_order_no, parse_fill_status,
)
from mahdi.broker.token_daemon import TokenDaemon
from mahdi.config.settings import PROJECT_ROOT, get_kis_settings
from mahdi.data import db
from mahdi.execution.entry import DEFAULT_TICK_SIZE

# 바꿀 수 없다 — 인자로 노출하지 않는 것이 이 스크립트의 안전 설계다.
_QTY = 1
# 현재가에서 이 비율만큼 불리한 쪽으로 지정가를 낸다(체결 방지).
# 옵션 프리미엄은 변동이 크므로 30%로 넉넉히 잡는다 — 목적은 「접수되고 체결되지 않는 것」이다.
_PRICE_AWAY_RATIO = 0.30
# 격자 판정에서 부동소수 먼지를 흡수하는 여유. 호가단위(0.01)보다 일곱 자리 작아 실제 가격을
# 한 틱도 옮기지 못한다.
_TICK_EPSILON = 1e-9


def _log_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return PROJECT_ROOT / "logs" / f"order_roundtrip_{stamp}.json"


def _acquire_token(daemon: TokenDaemon, attempts: int = 3, wait_seconds: int = 65) -> bool:
    """
    계산: 토큰을 미리 발급받는다. 403(분당 발급 제한)이면 기다렸다 다시 시도한다.
    해석: **`TokenDaemon`의 토큰 캐시는 프로세스 메모리 전용이다** — 디스크 공유가 없으므로
         이 스크립트는 관측 루프와 별개로 자기 토큰을 발급받는다. KIS는 토큰 발급을 분당 1회로
         제한하고(NEXT_TODO에 관찰 항목으로 남아 있다), 실제로 이 스크립트 개발 중 403을 받았다
         (같은 앱키로 직전에 발급한 직후였다. 1분 뒤 같은 호출은 200이었다).

         재시도를 여기 두고 `TokenDaemon`에 넣지 않는 이유: 토큰 발급은 관측 루프의 인증
         경로다. 실측 하루 전에 그 경로를 건드리면 그날 아침 기동이 새 코드로 처음 시험된다 —
         바꿔야 할 이유가 이 스크립트의 편의뿐이라면 스크립트 쪽에 두는 것이 맞다.
    반환: 성공하면 True. 전부 실패하면 False(호출측이 종료 코드로 옮긴다).
    """
    import time as _time

    import httpx as _httpx

    for attempt in range(1, attempts + 1):
        try:
            daemon.get_token()
            return True
        except _httpx.HTTPStatusError as exc:
            if exc.response.status_code != 403 or attempt == attempts:
                print(f"토큰 발급 실패: {exc}", file=sys.stderr)
                return False
            print(
                f"토큰 발급 403(분당 제한) — {wait_seconds}초 뒤 재시도 ({attempt}/{attempts - 1}). "
                "관측 루프가 방금 발급했을 수 있다.",
                file=sys.stderr,
            )
            _time.sleep(wait_seconds)
    return False


# ===== 2026-08-18 — 선물을 옵션으로 조회하고 있었다 =====
#
# `KISRestClient.get_quote()`의 `market_div_code` **기본값이 옵션('O')** 인데 이 스크립트는 그
# 인자를 넘기지 않았다. 그래서 선물 코드(A01609)를 'O'로 물었고 KIS는 **HTTP 200에 전 필드 0**
# 으로 답했다(`futs_prpr="0.00"`, `futs_last_tr_date="0"`, `dprt=9999.99` 센티널).
#
# 실측 대조(2026-08-18 13:5x, 같은 심볼):
#     F(지수선물) -> futs_prpr='1096.80'  최종거래일='20260910'  잔존일='24'
#     O(지수옵션) -> futs_prpr='0.00'     최종거래일='0'         잔존일='0'
#
# **에러가 아니라 0으로 답한다는 것이 이 결함의 성질이다** — 4xx였다면 즉시 드러났을 것이다.
# 다행히 뒤이은 `reference <= 0` 검사가 주문 전에 멈춰 세웠다(예행연습 종료코드 3). 즉 실주문
# 위험은 없었고, **예행연습이 정확히 제 일을 했다.**
_MARKET_DIV_BY_LENGTH = {6: tr_codes.FID_MRKT_DIV_INDEX_FUTURES, 9: tr_codes.FID_MRKT_DIV_INDEX_OPTION}


def _optional_limit(raw) -> float | None:
    """계산: 상/하한가 필드를 float으로. 못 읽거나 0이면 None(밴드를 모르면 자르지 않는다)."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def market_div_for(symbol: str) -> str:
    """
    입력: 단축상품번호.
    계산: 길이로 시장구분을 가른다 — **선물 6자리 / 옵션 9자리**(저장소 전역 규약,
         `db/migrations/030_position_snapshots.sql`의 `shtn_pdno` 주석과 같은 근거).
    해석: 길이를 모르는 값이면 옵션으로 둔다 — `get_quote()`의 기존 기본값과 같아서
         이 함수가 생기기 전 동작을 바꾸지 않는다(옵션 경로는 원래 맞게 돌고 있었다).
    실패 조건: 없음.
    """
    return _MARKET_DIV_BY_LENGTH.get(len(str(symbol).strip()), tr_codes.FID_MRKT_DIV_INDEX_OPTION)


def _away_price(
    reference: float,
    side: str,
    tick: float = DEFAULT_TICK_SIZE,
    low_limit: float | None = None,
    high_limit: float | None = None,
) -> float:
    """
    입력: 현재가, BUY/SELL, 호가단위(기본 `DEFAULT_TICK_SIZE` — 선물·옵션 양쪽에서 유효한 격자).
    계산: 현재가에서 `_PRICE_AWAY_RATIO`만큼 불리한 쪽으로 옮긴 뒤, **더 불리한 쪽으로만**
         격자에 스냅한다 — 매수는 내림, 매도는 올림.
    해석: **스냅 방향이 안전장치다.** 가장 가까운 격자로 반올림하면 가격이 현재가 쪽으로
         움직일 수 있고, 그것은 이 스크립트가 유일하게 피해야 하는 것(체결)에 한 걸음
         다가가는 일이다. 항상 멀어지는 쪽으로만 스냅하면 체결 위험은 결코 늘지 않는다.

         2026-08-17까지 이 함수는 `round(x, 2)`였다 — 주석은 "옵션 최소 호가단위(0.01)"라고
         적었지만 첫 실측 대상은 **선물(A01609)** 이었고, 선물 격자는 0.05다. 현재가 2,000개
         실측에서 **1,600개(80%)** 가 격자를 위반하는 지정가를 만들었다. 거래소가 거부하면
         포지션은 안 생기지만, 한 번뿐인 실측이 날아가고 «주문 API가 안 된다»로 오귀속되기 쉽다.
         `low_limit`/`high_limit`(응답의 `futs_llam`/`futs_mxpr`)을 주면 그 밴드 안으로 자른다 —
         거래소는 제한폭 밖 지정가를 접수하지 않는다(2026-08-18 실측: `rt_cd=1` 상/하한가 오류).
    실패 조건: 스냅 결과가 0 이하이거나 현재가보다 불리하지 않으면 `ValueError`.
         저가 옵션(프리미엄 0.05 미만)에서 0.05 격자로는 「현재가보다 낮은 양수」를 만들 수 없다 —
         그 경우 조용히 0이나 현재가 위쪽 값을 내보내지 않고 **멈춰서 사람에게 `--tick`을
         묻는다**(체결될 수 있는 가격을 지어내는 것보다 실측을 미루는 편이 낫다).
    """
    if tick <= 0:
        raise ValueError(f"호가단위는 양수여야 한다: {tick}")

    is_buy = side.upper() == "BUY"
    raw = reference * (1 - _PRICE_AWAY_RATIO) if is_buy else reference * (1 + _PRICE_AWAY_RATIO)

    def _floor(value: float) -> float:
        return round(math.floor(value / tick + _TICK_EPSILON) * tick, 4)

    def _ceil(value: float) -> float:
        return round(math.ceil(value / tick - _TICK_EPSILON) * tick, 4)

    # 부동소수 먼지가 격자 판정을 뒤집지 않도록 여유를 준다(예: 13651.999999997 -> 13652).
    price = _floor(raw) if is_buy else _ceil(raw)

    # ===== 2026-08-18 — 30%는 일일 가격제한폭 밖이었다 =====
    #
    # 첫 실주문이 `rt_cd=1 "모의투자 상/하한가 오류"`(msg_cd 40270000)로 거부됐다. 실측:
    #     현재가 1099.65 · 기준가 1098.90 · 제한폭 1011.00 ~ 1186.80 (기준가 ±8.0%)
    #     시도한 지정가 769.75 -> 하한보다 241.25 낮다
    # `_PRICE_AWAY_RATIO=0.30`의 주석은 *"옵션 프리미엄은 변동이 크므로 30%"* 인데, 그 가정을
    # 선물에 그대로 쓴 것이다(시장구분 결함과 **같은 뿌리** — 옵션용 전제를 선물에 적용).
    #
    # 30%를 낮추지 않고 **밴드로 자른다**: 원하는 거리는 그대로 두고 거래소가 허용하는 범위에서
    # 가장 먼 값을 쓴다. 옵션처럼 밴드가 넓은 상품에서는 30%가 그대로 살아 있다.
    #
    # 잘릴 때 격자 스냅 방향이 **뒤집힌다**: 평소에는 «더 멀어지는 쪽»으로 내리지만, 하한에
    # 붙였을 때 또 내리면 밴드 **밖으로** 나가 다시 거부당한다. 그래서 그때만 올림한다 —
    # 한 틱 가까워지지만 그 지점은 이미 기준가에서 8% 떨어진 곳이라 체결 위험은 없다.
    clamped = False
    if is_buy and low_limit is not None and price < low_limit:
        price, clamped = _ceil(low_limit), True
    elif not is_buy and high_limit is not None and price > high_limit:
        price, clamped = _floor(high_limit), True
    if clamped and ((is_buy and price < low_limit) or (not is_buy and price > high_limit)):
        raise ValueError(f"제한폭 경계를 격자로 표현할 수 없다(가격 {price}, 호가단위 {tick})")

    if price <= 0:
        raise ValueError(
            f"호가단위 {tick}로는 현재가 {reference}에서 체결 방지 지정가를 만들 수 없다"
            f"(계산값 {price}). 더 작은 --tick을 지정할 것."
        )
    if (is_buy and price >= reference) or (not is_buy and price <= reference):
        raise ValueError(
            f"스냅 결과 {price}가 현재가 {reference}보다 불리하지 않다 — 체결될 수 있다. "
            "--tick을 확인할 것."
        )
    return price


def main() -> int:
    parser = argparse.ArgumentParser(description="주문 제출→조회→취소 왕복 실측 (모의계좌 전용, 1계약)")
    parser.add_argument("--symbol", required=True, help="단축상품번호 (선물 6자리 예: 101S03 / 옵션 9자리)")
    parser.add_argument("--side", default="BUY", choices=["BUY", "SELL"])
    parser.add_argument("--confirm", action="store_true", help="실제로 주문을 보낸다(없으면 예행연습)")
    parser.add_argument("--force-outside-hours", action="store_true", help="정규장 밖에서도 진행")
    parser.add_argument(
        "--market", default=None,
        help="FID_COND_MRKT_DIV_CODE (F=지수선물 / O=지수옵션). 생략하면 종목코드 길이로 판별한다.",
    )
    parser.add_argument(
        "--tick", type=float, default=DEFAULT_TICK_SIZE,
        help=f"호가단위(기본 {DEFAULT_TICK_SIZE} — 선물·옵션 양쪽에서 유효한 격자). "
             "프리미엄이 아주 낮은 옵션에서만 0.01로 내릴 것.",
    )
    args = parser.parse_args()

    settings = get_kis_settings()
    if not settings.is_mock:
        print("거부: KIS_ENV가 prod다. 이 스크립트는 모의계좌 전용이다.", file=sys.stderr)
        return 2

    now = db.local_now()
    # ⚠ **`session`은 시각만 본다 — 요일도 공휴일도 모른다.**
    # 그래서 요일은 여기서 따로 막는다(토요일 13시에 실행해도 `is_continuous_trading`은 True다 —
    # 실제로 그렇게 통과하는 것을 확인하고 이 검사를 추가했다).
    # **공휴일은 여전히 막지 못한다** — 마흐디에는 휴장일 달력이 없다(2026-08-17 광복절
    # 대체휴일이 그 예다). 그날 실행하면 KIS가 거부하는 것에 의존하게 된다.
    is_weekday = now.weekday() < 5
    if (not is_weekday or not session.is_continuous_trading(now)) and not args.force_outside_hours:
        reason = "주말" if not is_weekday else f"정규장 시간이 아님({now:%H:%M})"
        print(
            f"거부: {reason}. 접수 자체가 거부되거나 예약 주문이 될 수 있다.\n"
            "       정규장(평일 09:00~15:20)에 다시 실행하거나 --force-outside-hours를 붙일 것.\n"
            "       ⚠ 공휴일은 이 검사가 걸러내지 못한다(휴장일 달력이 없다).",
            file=sys.stderr,
        )
        return 2

    record: dict = {
        "at": now.isoformat(), "env": settings.kis_env, "symbol": args.symbol,
        "side": args.side, "qty": _QTY, "confirmed": args.confirm, "steps": [],
    }
    # main.py와 같은 방식으로 만든다(토큰 데몬이 필수 인자다).
    daemon = TokenDaemon(settings)
    if not _acquire_token(daemon):
        return 5
    client = KISRestClient(settings, daemon)

    market_div = args.market or market_div_for(args.symbol)
    record["market_div"] = market_div
    quote = client.get_quote(args.symbol, market_div_code=market_div)
    # 2026-08-18 실측: 옵션을 조회해도 응답에는 `optn_prpr`가 **없다**(공식 문서에는 있으나 그것은
    # 다른 TR의 스키마다). 선물·옵션 모두 `futs_prpr`가 그 종목 자신의 현재가다 —
    # 근거는 `docs/dev_memory/KIS_RAW_FIELD_RANGES.md`. 폴백은 TR이 바뀔 때를 위해 남겨 둔다.
    reference = float(quote.get("output1", {}).get("futs_prpr") or quote.get("output1", {}).get("optn_prpr") or 0)
    record["steps"].append({"step": "get_quote", "reference_price": reference, "raw": quote})
    if reference <= 0:
        print(
            f"거부: 현재가를 읽지 못했다(reference={reference}, 시장구분={market_div}). "
            "종목코드와 **시장구분**을 확인할 것 — KIS는 구분이 어긋나도 4xx가 아니라 "
            "전 필드 0으로 답한다(2026-08-18 실측). 선물이면 --market F, 옵션이면 --market O.",
            file=sys.stderr,
        )
        print(json.dumps(quote, ensure_ascii=False)[:600], file=sys.stderr)
        return 3

    output1 = quote.get("output1", {})
    low_limit = _optional_limit(output1.get("futs_llam"))
    high_limit = _optional_limit(output1.get("futs_mxpr"))
    try:
        price = _away_price(reference, args.side, args.tick, low_limit, high_limit)
    except ValueError as exc:
        print(f"거부: {exc}", file=sys.stderr)
        return 6
    record["tick"] = args.tick
    record["price_limit"] = {"low": low_limit, "high": high_limit}
    record["reference_price"] = reference
    record["order_price"] = price
    print(f"종목 {args.symbol}(구분 {market_div}) / {args.side} {_QTY}계약 / 현재가 {reference} → 지정가 {price} "
          f"({_PRICE_AWAY_RATIO:.0%} 불리한 쪽 = 체결 방지)")
    # 격자를 **눈으로 확인할 수 있게** 함께 인쇄한다 — 이 줄이 없어서 08-17까지 아무도
    # 지정가가 호가단위를 벗어난다는 것을 몰랐다(거리만 보고 격자는 안 봤다).
    print(f"  호가단위 {args.tick} 격자 스냅 완료 · 전송 문자열 {format_order_price(price)!r}")
    print(f"  일일 제한폭 {low_limit} ~ {high_limit}"
          f"{' — 지정가를 그 안으로 잘랐다' if low_limit and (price <= low_limit or price >= high_limit) else ''}")

    if not args.confirm:
        print("\n예행연습이다 — 주문을 보내지 않았다. 실제 실행은 --confirm.")
        return 0

    order_no = None
    try:
        submitted = client.submit_order(args.symbol, args.side, _QTY, price)
        record["steps"].append({"step": "submit_order", "raw": submitted})
        print(f"\n[제출] rt_cd={submitted.get('rt_cd')} msg={submitted.get('msg1')}")
        if submitted.get("rt_cd") != "0":
            print("  제출이 업무 실패다(HTTP는 200일 수 있다). 여기서 멈춘다.")
            return 4

        # 제출 응답의 output은 **array**이고 필드는 **대문자**다(tr_codes 주석 참고).
        output = submitted.get("output")
        rows = output if isinstance(output, list) else [output] if output else []
        order_no = str((rows[0] or {}).get(tr_codes.ORDER_SUBMIT_ORDER_NO_FIELD, "")).strip() if rows else ""
        print(f"  주문번호(ODNO) = {order_no!r}")
        record["order_no"] = order_no
        if not order_no:
            print("  ⚠ 주문번호를 못 읽었다 — 응답 구조가 문서와 다르다. 원문을 로그에서 확인할 것.")

        stamp = now.strftime("%Y%m%d")
        inquiry = client.inquire_ccnl(stamp, stamp, symbol=args.symbol)
        record["steps"].append({"step": "inquire_ccnl", "raw": inquiry})
        found = [
            r for r in (inquiry.get("output1") or [])
            if normalize_order_no(r.get(tr_codes.ORDER_INQUIRY_ORDER_NO_FIELD)) == normalize_order_no(order_no)
        ]
        print(f"[조회] rt_cd={inquiry.get('rt_cd')} / 전체 {len(inquiry.get('output1') or [])}건 / "
              f"이 주문 {len(found)}건")
        if found:
            print(f"  상태 매핑 = {parse_fill_status(found[0])}")
            record["fill_status"] = parse_fill_status(found[0])
        else:
            print("  ⚠ 방금 낸 주문이 조회에 없다 — 조회 파라미터나 지연을 확인할 것.")
    finally:
        if order_no:
            try:
                cancelled = client.cancel_order(order_no)
                record["steps"].append({"step": "cancel_order", "raw": cancelled})
                print(f"[취소] rt_cd={cancelled.get('rt_cd')} msg={cancelled.get('msg1')}")
            except Exception as exc:  # noqa: BLE001 — 취소 실패는 사람이 즉시 알아야 한다
                record["steps"].append({"step": "cancel_order", "error": repr(exc)})
                print(f"[취소] ⚠ 실패: {exc!r}\n  **HTS에서 잔여 주문을 직접 확인·취소할 것.**",
                      file=sys.stderr)

        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"\n원문 기록: {path}")
        print("이 파일의 응답 필드를 docs/dev_memory/KIS_RAW_FIELD_RANGES.md에 옮길 것(R8).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
