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
from mahdi.features.options_intel import OptionLeg, calculate_gex, calculate_vrp

logger = logging.getLogger("mahdi.main")

KOSPI200_OPTION_STRIKE_INTERVAL = 2.5
STRIKES_EACH_SIDE = 3  # (2*3+1)*2(C/P) = 14 슬롯, MAX_SUBSCRIPTIONS(41) 여유 확보
SYMBOL_MASTER_CACHE_DIR = Path("data/symbol_master_cache")  # KIS 마스터파일은 매일 갱신됨
UNDERLYING = "KOSPI200"
OPTION_CHAIN_POLL_INTERVAL_SECONDS = 60  # WS 구독(ATM±3) 범위와 동일한 종목을 REST로 주기 조회


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
) -> None:
    """
    입력: 이미 연결된 WS 클라이언트, 구독 롤링 매니저, REST 클라이언트, 최근월 지수선물 단축코드
         (예: "101S03" — 분기마다 바뀌므로 종목코드 마스터파일/설정으로 최신화 필요).
    계산: "선물옵션 시세"(inquire-price, F 시장)로 초기 스팟(KOSPI200 지수) 조회 → ATM 구독 →
         WS 메시지 수신 → 종목별로 1분봉 완성 시 market_raw_1m 적재(각 틱에 실린 종목코드를
         그대로 사용 — ATM±N 구독은 최대 14개 종목을 동시에 켜두므로 종목별 분리가 필수) →
         워밍업 레짐을 regime_state에 기록.
    실패 조건: DB 연결 실패·WS 단절 시 예외가 위로 전파된다 — 재시작은 프로세스 관리자(Ops) 책임.
    """
    quote = rest_client.get_quote(futures_symbol, market_div_code=tr_codes.FID_MRKT_DIV_INDEX_FUTURES)
    # output3 = KOSPI200 지수 자체, output1 = 조회한 선물 계약가(베이시스 존재) — 지수 우선, 없으면 선물가로 폴백
    spot = float(quote.get("output3", {}).get("bstp_nmix_prpr") or quote.get("output1", {}).get("futs_prpr") or 0.0)
    if spot > 0:
        await subscription_manager.roll_to_spot(spot)

    # ATM±N 구독은 최대 14개 종목(행사가×C/P)을 동시에 켜두므로, 종목별로 별도 집계기가 필요하다.
    # 하나의 aggregator를 공유하면 서로 다른 옵션 종목의 체결가가 한 봉에 뒤섞인다.
    aggregators: dict[str, MinuteBarAggregator] = {}

    async def handle_message(message: dict) -> None:
        raw = message.get("raw")
        if raw is None:
            return  # JSON 제어 메시지(구독 응답/PINGPONG)는 무시

        parsed = _parse_tick(raw)
        if parsed is None:
            return
        tick_symbol, tick = parsed

        aggregator = aggregators.setdefault(tick_symbol, MinuteBarAggregator())
        bar = aggregator.add_tick(tick)
        if bar is None:
            return

        with db.get_connection() as conn:
            db.insert_market_raw_1m(
                conn,
                {
                    "timestamp": bar.minute,
                    "symbol": tick_symbol,
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
_IDX_MKSC_SHRN_ISCD = 0  # 유가증권 단축종목코드 — ATM±N 구독은 종목이 여러 개라 필수로 읽어야 함
_IDX_BSOP_HOUR = 1  # 영업시간 HHMMSS
_IDX_OPTN_PRPR = 2  # 옵션 현재가
_IDX_LAST_CNQN = 9  # 최종 거래량(해당 체결의 체결수량)
_IDX_OPTN_ASKP1 = 41  # 옵션 매도호가1
_IDX_OPTN_BIDP1 = 42  # 옵션 매수호가1
_IDX_ASKP_RSQN1 = 43  # 매도호가 잔량1
_IDX_BIDP_RSQN1 = 44  # 매수호가 잔량1
_MIN_FIELDS = _IDX_BIDP_RSQN1 + 1


def _parse_tick(raw: str, today: date | None = None) -> tuple[str, Tick] | None:
    """
    KIS 실시간 체결가(H0IOCNT0) "^" 구분 파서 — 필드 순서는 위 _IDX_* 상수 참고(공식 문서 실측).

    입력: WS로 수신한 원시 문자열 1건. KIS 실시간 프레임은 "암호화유무|TR_ID|데이터건수|실제데이터"
         형태로, 실제 "^" 구분 데이터 앞에 "|" 구분 헤더 3개가 붙어 있다(2026-07-06 실거래 중
         VARCHAR(20) 오버플로로 발견 — 헤더를 안 벗기면 0번 필드에 헤더 전체가 종목코드 앞에
         달라붙는다). idx1(BSOP_HOUR) 이후 필드는 헤더와 무관하게 그대로 정렬되므로 영향 없음.
    계산: "|"로 헤더를 먼저 제거한 뒤 "^"로 실제 필드를 나눈다. 영업시간(BSOP_HOUR, HHMMSS)을
         오늘 날짜와 결합해 틱 타임스탬프로 사용 — 수신 지연이 있어도 거래소 기준 시각으로 1분
         버킷을 나눌 수 있다. 종목코드(0번 필드)를 함께 반환한다 — ATM±N 구독은 최대 14개 종목을
         동시에 켜두므로, 어느 종목의 틱인지 모르면 서로 다른 옵션의 체결가가 한 봉에 뒤섞인다.
    실패 조건: 필드 수가 부족하거나 숫자 파싱 실패 시 None(해당 틱 무시).
    """
    body = raw.split("|", 3)[-1]  # 암호화유무|TR_ID|데이터건수 헤더 제거(헤더 없는 입력은 그대로 통과)
    fields = body.split("^")
    if len(fields) < _MIN_FIELDS:
        return None
    try:
        hhmmss = fields[_IDX_BSOP_HOUR]
        tick_time = dtime(int(hhmmss[0:2]), int(hhmmss[2:4]), int(hhmmss[4:6]))
        timestamp = datetime.combine(today or date.today(), tick_time)
        symbol = fields[_IDX_MKSC_SHRN_ISCD]
        tick = Tick(
            timestamp=timestamp,
            price=float(fields[_IDX_OPTN_PRPR]),
            volume=float(fields[_IDX_LAST_CNQN]),
            bid_px=float(fields[_IDX_OPTN_BIDP1]),
            bid_qty=float(fields[_IDX_BIDP_RSQN1]),
            ask_px=float(fields[_IDX_OPTN_ASKP1]),
            ask_qty=float(fields[_IDX_ASKP_RSQN1]),
        )
        return symbol, tick
    except (ValueError, IndexError):
        return None


def _parse_option_quote(
    resp: dict, strike: float, option_type: str, poll_time: datetime
) -> tuple[dict, float] | None:
    """
    입력: KISRestClient.get_quote() 응답(output1=그릭스/IV/OI, output3=KOSPI200 지수 자체 —
         어느 옵션 종목을 조회하든 항상 지수 자체가 돌아온다), 요청한 행사가/콜풋 구분,
         폴링 시각(분 단위로 맞춰 같은 사이클 내 upsert 타임스탬프를 통일).
    계산: option_analysis_1m 1행(dict)과 기초자산 스팟을 함께 반환한다. gex는
         mahdi.features.options_intel.calculate_gex로 이 레그 하나만 넣어 즉시 계산해 저장
         (콜+/풋- 부호 규약은 calculate_gex 내부에서 처리). rv_5d는 KIS hist_vltl(과거변동성)을
         근사치로 사용 — 정확한 5일 realized vol 재계산은 Phase1 범위 밖(추후 개선 대상).
    실패 조건: 필수 필드가 없거나 숫자 변환 실패 시 None(해당 레그 스킵 — 폴링 루프가 계속 돌게 함).
    """
    output1 = resp.get("output1") or {}
    output3 = resp.get("output3") or {}
    try:
        spot = float(output3["bstp_nmix_prpr"])
        expiry = datetime.strptime(output1["futs_last_tr_date"], "%Y%m%d").date()
        gamma = float(output1["gama"])
        iv = float(output1["hts_ints_vltl"]) / 100
        rv = float(output1["hist_vltl"]) / 100
        oi = float(output1["hts_otst_stpl_qty"])
        t_years = max((expiry - poll_time.date()).days, 0) / 365.0
        leg = OptionLeg(strike=strike, option_type=option_type.lower(), oi=oi, iv=iv, t_years=t_years, gamma=gamma)
        row = {
            "timestamp": poll_time,
            "underlying": UNDERLYING,
            "expiry": expiry,
            "strike": strike,
            "option_type": option_type,
            "delta": float(output1["delta_val"]),
            "gamma": gamma,
            "theta": float(output1["theta"]),
            "vega": float(output1["vega"]),
            "vanna": None,
            "charm": None,
            "iv": iv,
            "rv_5d": rv,
            "vrp": calculate_vrp(iv, rv),
            "skew_25d": None,
            "gex": calculate_gex([leg], spot),
            "oi": int(oi),
            "oi_change": int(float(output1["otst_stpl_qty_icdc"])),
            "volume": int(float(output1["acml_vol"])),
            "spread_state": None,
        }
        return row, spot
    except (KeyError, ValueError, TypeError):
        return None


async def poll_option_chain(
    rest_client: KISRestClient,
    subscription_manager: RollingSubscriptionManager,
    master: IndexDerivativesMaster,
    underlying: str = UNDERLYING,
    interval_seconds: float = OPTION_CHAIN_POLL_INTERVAL_SECONDS,
) -> None:
    """
    입력: REST 클라이언트, ATM±N 구독 매니저(WS와 동일한 행사가 집합을 공유), 종목코드 마스터.
    계산: WS 구독 중인 행사가×콜/풋 각각에 대해 주기적으로 get_quote()를 호출해 그릭스/IV/OI를
         option_analysis_1m에, 기초자산 스팟을 underlying_spot_1m에 적재한다. get_quote()는
         동기(블로킹) httpx 호출이라 asyncio.to_thread로 실행해 WS 수신 루프를 막지 않는다.
    실패 조건: 개별 종목 조회/파싱 실패는 건너뛰고 다음 종목을 계속 처리한다 — REST 폴링 중
              하나가 실패했다고 WS 관측 전체가 죽으면 안 된다. 구독이 아직 없으면(기동 초입)
              2초 뒤 재확인.
    """
    while True:
        strikes = subscription_manager.desired_strikes
        if not strikes:
            await asyncio.sleep(2.0)
            continue

        poll_time = datetime.now().replace(second=0, microsecond=0)
        latest_spot: float | None = None
        rows: list[dict] = []
        for strike in strikes:
            for option_type in ("C", "P"):
                symbol = master.option_symbol(option_type, strike, underlying=underlying)
                if symbol is None:
                    continue
                try:
                    resp = await asyncio.to_thread(rest_client.get_quote, symbol)
                except Exception:
                    logger.warning("옵션 체인 폴링 실패: %s", symbol, exc_info=True)
                    continue
                parsed = _parse_option_quote(resp, strike, option_type, poll_time)
                if parsed is None:
                    continue
                row, spot = parsed
                rows.append(row)
                latest_spot = spot

        if rows:
            with db.get_connection() as conn:
                for row in rows:
                    db.insert_option_analysis_1m(conn, row)
                if latest_spot is not None:
                    db.insert_underlying_spot(conn, poll_time, underlying, latest_spot)

        await asyncio.sleep(interval_seconds)


_INVESTOR_FLOW_SECTORS = (
    tr_codes.FID_INVESTOR_FLOW_FUTURES,
    tr_codes.FID_INVESTOR_FLOW_CALL_OPTION,
    tr_codes.FID_INVESTOR_FLOW_PUT_OPTION,
)


async def poll_investor_flow(
    rest_client: KISRestClient,
    underlying: str = UNDERLYING,
    interval_seconds: float = OPTION_CHAIN_POLL_INTERVAL_SECONDS,
) -> None:
    """
    입력: REST 클라이언트, 기초자산 라벨.
    계산: KOSPI200 파생상품시장(선물+콜옵션+풋옵션) 세 세그먼트의 투자자별(외국인/기관계/개인)
         순매수 거래대금을 조회해 합산한 뒤 investor_flow_1m에 적재한다. "시장별 투자자매매동향
         (시세)"는 세션 누적치라, 이 값은 "1분간의 변화량"이 아니라 "그 시점까지의 누적 수급
         우위" 스냅샷이다. get_quote()류와 마찬가지로 동기 호출이라 asyncio.to_thread로 실행.
    실패 조건: 세그먼트 하나 실패는 건너뛰고 나머지로 합산 계속 — 셋 다 실패하면 이번 사이클은
              적재를 건너뛴다(마지막 성공값이 다음 사이클까지 화면에 남는다).
    """
    while True:
        foreign_total = 0.0
        institution_total = 0.0
        individual_total = 0.0
        got_any = False

        for sector in _INVESTOR_FLOW_SECTORS:
            try:
                resp = await asyncio.to_thread(
                    rest_client.get_investor_flow, tr_codes.FID_MRKT_DIV_DERIVATIVES, sector
                )
                output = resp.get("output") or []
                row = output[0] if isinstance(output, list) else output
                foreign_total += float(row["frgn_ntby_tr_pbmn"])
                institution_total += float(row["orgn_ntby_tr_pbmn"])
                individual_total += float(row["prsn_ntby_tr_pbmn"])
                got_any = True
            except Exception:
                logger.warning("투자자 수급 폴링 실패: %s", sector, exc_info=True)
                continue

        if got_any:
            poll_time = datetime.now().replace(second=0, microsecond=0)
            with db.get_connection() as conn:
                db.insert_investor_flow(
                    conn, poll_time, underlying,
                    foreign_net=foreign_total, institution_net=institution_total, individual_net=individual_total,
                )

        await asyncio.sleep(interval_seconds)


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
        await asyncio.gather(
            run_observation_loop(ws_client, subscription_manager, rest_client, futures_symbol=futures_symbol),
            poll_option_chain(rest_client, subscription_manager, master),
            poll_investor_flow(rest_client),
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Ctrl+C로 종료합니다.")
