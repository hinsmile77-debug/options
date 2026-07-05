"""관측 전용 오케스트레이터 (Phase1 범위) — 토큰 데몬 -> WS 수집기 -> 1분 집계 -> DB 적재.

Regime 갱신은 §7.4/§16.1 워밍업 규칙을 따른다: 세션 초반에는 warmup_fallback()을 사용하고,
Hurst/ADX 등 레짐 입력 피처를 축적해 RegimeEngine.fit()을 실행하는 것은 연속 세션 데이터가
쌓인 뒤(Research 단계) 진행하는 다음 단계다 — 이 스크립트는 그 전환 지점까지의 배선을 담당한다.

주문 실행/리스크 게이트는 Phase2 범위이므로 여기서는 다루지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time as dtime
from pathlib import Path

import websockets

from mahdi.broker import tr_codes
from mahdi.broker.rest_client import KISRestClient
from mahdi.broker.token_daemon import TokenDaemon
from mahdi.broker.ws_client import ApprovalKeyIssuer, KISWebSocketClient, WSConnection
from mahdi.config.settings import get_db_settings, get_kis_settings
from mahdi.data import db
from mahdi.data.collector import MinuteBarAggregator, Tick
from mahdi.data.subscription_manager import RollingSubscriptionManager
from mahdi.data.symbol_master import IndexDerivativesMaster, load_index_derivatives_master
from mahdi.engines.regime import RegimeLabel, warmup_fallback

logger = logging.getLogger("mahdi.main")

KOSPI200_OPTION_STRIKE_INTERVAL = 2.5
STRIKES_EACH_SIDE = 3  # (2*3+1)*2(C/P) = 14 슬롯, MAX_SUBSCRIPTIONS(41) 여유 확보
SYMBOL_MASTER_CACHE_DIR = Path("data/symbol_master_cache")  # KIS 마스터파일은 매일 갱신됨


class _WebsocketsAdapter(WSConnection):
    """websockets 라이브러리의 연결 객체를 KISWebSocketClient가 기대하는 프로토콜에 맞춘다."""

    def __init__(self, ws) -> None:
        self._ws = ws

    async def send(self, message: str) -> None:
        await self._ws.send(message)

    async def recv(self) -> str:
        return await self._ws.recv()

    async def close(self) -> None:
        await self._ws.close()


async def run_observation_loop(
    ws_client: KISWebSocketClient,
    subscription_manager: RollingSubscriptionManager,
    rest_client: KISRestClient,
    futures_symbol: str,
    symbol: str,
) -> None:
    """
    입력: 이미 연결된 WS 클라이언트, 구독 롤링 매니저, REST 클라이언트, 최근월 지수선물 단축코드
         (예: "101S03" — 분기마다 바뀌므로 종목코드 마스터파일/설정으로 최신화 필요), 구독 종목코드.
    계산: "선물옵션 시세"(inquire-price, F 시장)로 초기 스팟(KOSPI200 지수) 조회 → ATM 구독 →
         WS 메시지 수신 → 1분봉 완성 시 market_raw_1m 적재 → 워밍업 레짐을 regime_state에 기록.
    실패 조건: DB 연결 실패·WS 단절 시 예외가 위로 전파된다 — 재시작은 프로세스 관리자(Ops) 책임.
    """
    quote = rest_client.get_quote(futures_symbol, market_div_code=tr_codes.FID_MRKT_DIV_INDEX_FUTURES)
    # output3 = KOSPI200 지수 자체, output1 = 조회한 선물 계약가(베이시스 존재) — 지수 우선, 없으면 선물가로 폴백
    spot = float(quote.get("output3", {}).get("bstp_nmix_prpr") or quote.get("output1", {}).get("futs_prpr") or 0.0)
    if spot > 0:
        await subscription_manager.roll_to_spot(spot)

    aggregator = MinuteBarAggregator()

    async def handle_message(message: dict) -> None:
        raw = message.get("raw")
        if raw is None:
            return  # JSON 제어 메시지(구독 응답/PINGPONG)는 무시

        tick = _parse_tick(raw)
        if tick is None:
            return

        bar = aggregator.add_tick(tick)
        if bar is None:
            return

        with db.get_connection() as conn:
            db.insert_market_raw_1m(
                conn,
                {
                    "timestamp": bar.minute,
                    "symbol": symbol,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                    "vwap": bar.vwap,
                    "ofi": bar.ofi,
                    "microprice": bar.microprice,
                    "bid_ask_spread": bar.bid_ask_spread,
                    "buy_volume": bar.buy_volume,
                    "sell_volume": bar.sell_volume,
                    "quality_flag": bar.quality_flag,
                },
            )
            state = warmup_fallback(RegimeLabel.RANGE_BALANCED, macro_score=0.0, gap_zscore=0.0)
            db.insert_regime_state(
                conn,
                timestamp=bar.minute,
                regime=int(state.regime),
                prob_vector=list(state.prob_vector),
                higher_tf_regime=None,
                stability_flag=state.stability_flag,
            )

    await ws_client.listen(handle_message)


# H0IOCNT0(지수옵션 실시간체결가) 응답 필드 인덱스 — "^" 구분, 0-based.
# 출처: docs/efriend/한국투자증권_오픈API_전체문서 시트 "지수옵션  실시간체결가"(API ID 실시간-014).
_IDX_BSOP_HOUR = 1  # 영업시간 HHMMSS
_IDX_OPTN_PRPR = 2  # 옵션 현재가
_IDX_LAST_CNQN = 9  # 최종 거래량(해당 체결의 체결수량)
_IDX_OPTN_ASKP1 = 41  # 옵션 매도호가1
_IDX_OPTN_BIDP1 = 42  # 옵션 매수호가1
_IDX_ASKP_RSQN1 = 43  # 매도호가 잔량1
_IDX_BIDP_RSQN1 = 44  # 매수호가 잔량1
_MIN_FIELDS = _IDX_BIDP_RSQN1 + 1


def _parse_tick(raw: str, today: date | None = None) -> Tick | None:
    """
    KIS 실시간 체결가(H0IOCNT0) "^" 구분 파서 — 필드 순서는 위 _IDX_* 상수 참고(공식 문서 실측).

    입력: WS로 수신한 원시 문자열 1건.
    계산: 영업시간(BSOP_HOUR, HHMMSS)을 오늘 날짜와 결합해 틱 타임스탬프로 사용 —
         수신 지연이 있어도 거래소 기준 시각으로 1분 버킷을 나눌 수 있다.
    실패 조건: 필드 수가 부족하거나 숫자 파싱 실패 시 None(해당 틱 무시).
    """
    fields = raw.split("^")
    if len(fields) < _MIN_FIELDS:
        return None
    try:
        hhmmss = fields[_IDX_BSOP_HOUR]
        tick_time = dtime(int(hhmmss[0:2]), int(hhmmss[2:4]), int(hhmmss[4:6]))
        timestamp = datetime.combine(today or date.today(), tick_time)
        return Tick(
            timestamp=timestamp,
            price=float(fields[_IDX_OPTN_PRPR]),
            volume=float(fields[_IDX_LAST_CNQN]),
            bid_px=float(fields[_IDX_OPTN_BIDP1]),
            bid_qty=float(fields[_IDX_BIDP_RSQN1]),
            ask_px=float(fields[_IDX_OPTN_ASKP1]),
            ask_qty=float(fields[_IDX_ASKP_RSQN1]),
        )
    except (ValueError, IndexError):
        return None


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    kis_settings = get_kis_settings()
    get_db_settings()  # 조기 검증(연결 문자열 구성 오류를 기동 시점에 노출)

    # 종목코드 마스터파일은 매일 갱신되므로 기동 시 1회 내려받아 최근월물/행사가↔단축코드
    # 매핑을 확정한다 (모의투자 REST에는 옵션 체인 전체를 한 번에 주는 API가 없어 필수).
    master = load_index_derivatives_master(SYMBOL_MASTER_CACHE_DIR)
    futures_symbol = master.front_month_future_code("KOSPI200")
    if futures_symbol is None:
        raise RuntimeError("종목코드 마스터파일에서 KOSPI200 선물 최근월물을 찾지 못했습니다")

    token_daemon = TokenDaemon(kis_settings)
    rest_client = KISRestClient(kis_settings, token_daemon)
    approval_key = ApprovalKeyIssuer(kis_settings).issue()

    # 시세(H0IOCNT0/H0IOASP0)는 계좌 무관 공개 데이터라 모의투자 전용 도메인이 없다 —
    # is_mock 여부와 상관없이 MARKET_DATA_WS_DOMAIN(실전 도메인) 하나로 접속한다.
    async with websockets.connect(tr_codes.MARKET_DATA_WS_DOMAIN) as raw_ws:
        ws_client = KISWebSocketClient(approval_key=approval_key, connection=_WebsocketsAdapter(raw_ws))
        subscription_manager = RollingSubscriptionManager(
            ws_client,
            tr_id=tr_codes.WS_TR_OPTION_CONTRACT,
            strike_interval=KOSPI200_OPTION_STRIKE_INTERVAL,
            strikes_each_side=STRIKES_EACH_SIDE,
            symbol_formatter=lambda strike, opt: master.option_symbol(opt, strike, underlying="KOSPI200"),
        )
        await run_observation_loop(
            ws_client, subscription_manager, rest_client, futures_symbol=futures_symbol, symbol="KOSPI200_OPT"
        )


if __name__ == "__main__":
    asyncio.run(main())
