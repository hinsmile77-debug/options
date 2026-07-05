"""관측 전용 오케스트레이터 (Phase1 범위) — 토큰 데몬 -> WS 수집기 -> 1분 집계 -> DB 적재.

Regime 갱신은 §7.4/§16.1 워밍업 규칙을 따른다: 세션 초반에는 warmup_fallback()을 사용하고,
Hurst/ADX 등 레짐 입력 피처를 축적해 RegimeEngine.fit()을 실행하는 것은 연속 세션 데이터가
쌓인 뒤(Research 단계) 진행하는 다음 단계다 — 이 스크립트는 그 전환 지점까지의 배선을 담당한다.

주문 실행/리스크 게이트는 Phase2 범위이므로 여기서는 다루지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time as dtime

import websockets

from mahdi.broker.rest_client import KISRestClient
from mahdi.broker.token_daemon import TokenDaemon
from mahdi.broker.ws_client import ApprovalKeyIssuer, KISWebSocketClient, WSConnection
from mahdi.config.settings import get_db_settings, get_kis_settings
from mahdi.data import db
from mahdi.data.collector import MinuteBarAggregator, Tick
from mahdi.data.subscription_manager import RollingSubscriptionManager
from mahdi.engines.regime import RegimeLabel, warmup_fallback

logger = logging.getLogger("mahdi.main")

KOSPI200_OPTION_STRIKE_INTERVAL = 2.5
STRIKES_EACH_SIDE = 3  # (2*3+1)*2(C/P) = 14 슬롯, MAX_SUBSCRIPTIONS(41) 여유 확보


def _option_symbol(strike: float, option_type: str) -> str:
    """행사가·콜풋을 KIS 옵션 종목코드로 변환 — 실제 코드 체계는 KIS 종목마스터로 확정 필요."""
    return f"{strike}{option_type}"


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
    underlying_code: str,
    symbol: str,
) -> None:
    """
    입력: 이미 연결된 WS 클라이언트, 구독 롤링 매니저, REST 클라이언트, 기초자산/구독 종목코드.
    계산: 옵션 체인 스냅샷으로 초기 ATM 구독 → WS 메시지 수신 → 1분봉 완성 시 market_raw_1m 적재
         → 워밍업 레짐을 regime_state에 기록.
    실패 조건: DB 연결 실패·WS 단절 시 예외가 위로 전파된다 — 재시작은 프로세스 관리자(Ops) 책임.
    """
    chain = rest_client.get_option_chain(underlying_code)
    spot = float(chain.get("output", {}).get("stck_prpr", 0)) or 0.0
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


def _parse_tick(raw: str) -> Tick | None:
    """KIS 실시간 체결/호가 파이프(|) 구분 포맷 파서 — 실제 필드 순서는 KIS 개발자센터 샘플로
    최종 확인 후 조정할 것(현재는 최소 스켈레톤)."""
    fields = raw.split("^")
    if len(fields) < 5:
        return None
    try:
        return Tick(
            timestamp=datetime.now(),
            price=float(fields[0]),
            volume=float(fields[1]),
            bid_px=float(fields[2]),
            bid_qty=float(fields[3]),
            ask_px=float(fields[4]),
            ask_qty=float(fields[5]) if len(fields) > 5 else float(fields[3]),
        )
    except (ValueError, IndexError):
        return None


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    kis_settings = get_kis_settings()
    get_db_settings()  # 조기 검증(연결 문자열 구성 오류를 기동 시점에 노출)

    token_daemon = TokenDaemon(kis_settings)
    rest_client = KISRestClient(kis_settings, token_daemon)
    approval_key = ApprovalKeyIssuer(kis_settings).issue()

    from mahdi.broker import tr_codes

    ws_domain = tr_codes.VPS_WS_DOMAIN if kis_settings.is_mock else tr_codes.REAL_WS_DOMAIN
    async with websockets.connect(ws_domain) as raw_ws:
        ws_client = KISWebSocketClient(approval_key=approval_key, connection=_WebsocketsAdapter(raw_ws))
        subscription_manager = RollingSubscriptionManager(
            ws_client,
            tr_id=tr_codes.WS_TR_OPTION_CONTRACT,
            strike_interval=KOSPI200_OPTION_STRIKE_INTERVAL,
            strikes_each_side=STRIKES_EACH_SIDE,
            symbol_formatter=_option_symbol,
        )
        await run_observation_loop(
            ws_client, subscription_manager, rest_client, underlying_code="201", symbol="KOSPI200_OPT"
        )


if __name__ == "__main__":
    asyncio.run(main())
