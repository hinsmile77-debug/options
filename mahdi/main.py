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
from mahdi.broker.ws_client import ApprovalKeyIssuer, KISWebSocketClient, Subscription, WSConnection
from mahdi.config.settings import get_db_settings, get_kis_settings
from mahdi.data import db
from mahdi.data.collector import MinuteBarAggregator, Tick, VolumeBucketAggregator
from mahdi.data.subscription_manager import RollingSubscriptionManager
from mahdi.data.symbol_master import IndexDerivativesMaster, load_index_derivatives_master
from mahdi.engines.regime import RegimeLabel, warmup_fallback
from mahdi.features.options_intel import OptionLeg, calculate_gex, calculate_vrp
from mahdi.features.orderflow import calculate_vpin

logger = logging.getLogger("mahdi.main")

KOSPI200_OPTION_STRIKE_INTERVAL = 2.5
STRIKES_EACH_SIDE = 3  # (2*3+1)*2(C/P) = 14 슬롯, MAX_SUBSCRIPTIONS(41) 여유 확보
SYMBOL_MASTER_CACHE_DIR = Path("data/symbol_master_cache")  # KIS 마스터파일은 매일 갱신됨
UNDERLYING = "KOSPI200"
OPTION_CHAIN_POLL_INTERVAL_SECONDS = 60  # WS 구독(ATM±3) 범위와 동일한 종목을 REST로 주기 조회

# VPIN 등거래량 버킷 크기 — 실거래 일평균거래량 관찰 전까지 쓰는 잠정치. 학계 관례는
# "일평균거래량/50"이지만 이 모의투자 환경의 실제 거래량 분포를 아직 모른다(2026-07-06 결정).
# 옵션은 선물보다 훨씬 얇아 버킷이 완성되기까지 오래 걸리거나 VPIN이 0.5(중립) 근처에 자주
# 머물 수 있다는 걸 알고 쓴다(2026-07-06 사용자에게 설명 후 옵션에도 적용하기로 확정).
VPIN_BUCKET_SIZE = 50
_VPIN_HISTORY_LIMIT = 500  # calculate_vpin 기본 window(50)의 10배 — 무한정 누적 방지

# Phase 1.5-③(만기 유동성 기준선, 2026-07-06 추가) — 연구문서(RESEARCH_EXPIRY_SELECTION_v1.md)가
# 권고하는 "ATM±2 집중"(Cao-Wei %스프레드 기준, 스캘핑에 최적인 구간)은 WS 구독 범위(ATM±3)보다
# 좁다. get_asking_price()는 북당 5행사가×2(C/P)=10건 신규 REST 호출이라, 오늘 이미 1x 부하에서
# 403/500 레이트리밋을 관찰한 점을 고려해 폴링 주기를 옵션체인(60초)보다 훨씬 길게 잡았다 —
# 어차피 이 지표의 용도는 실시간 판단이 아니라 20거래일 기준선 축적이라 촘촘히 볼 필요가 없다
# (사용자 확인 후 %스프레드 포함 정식 스펙대로 진행하되, 호출빈도로 부하를 완화하기로 결정).
LIQUIDITY_ATM_EACH_SIDE = 2
EXPIRY_LIQUIDITY_POLL_INTERVAL_SECONDS = 300


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
    subscription_managers: list[RollingSubscriptionManager],
    rest_client: KISRestClient,
    futures_symbol: str,
) -> None:
    """
    입력: 이미 연결된 WS 클라이언트, 구독 롤링 매니저 목록(2026-07-06부터 리스트 — 먼슬리/위클리
         등 여러 만기 북을 동시에 굴리기 위함, 북마다 별도 인스턴스), REST 클라이언트, 최근월
         지수선물 단축코드(예: "101S03" — 분기마다 바뀌므로 종목코드 마스터파일/설정으로 최신화
         필요).
    계산: "선물옵션 시세"(inquire-price, F 시장)로 초기 스팟(KOSPI200 지수) 조회 → 모든 구독
         매니저를 그 스팟으로 롤링(각자 자기 만기 시리즈에서 ATM±N을 계산) → 선물 실시간체결가
         (H0IFCNT0)도 함께 구독(2026-07-06 추가) → 현재 선물 단축코드를 active_futures_symbol에
         등록(대시보드가 "이 종목이 선물인지" 바로 조회 가능하게) → WS 메시지 수신 → 종목(선물·
         옵션·만기북 구분 없이)별로 1분봉 완성 시 market_raw_1m 적재(각 틱에 실린 종목코드를
         그대로 사용 — 북마다 최대 14개 옵션 종목이 동시에 켜지므로 종목별 분리가 필수) → 워밍업
         레짐을 regime_state에 기록. 모든 종목의 틱은 등거래량 버킷(VolumeBucketAggregator)에도
         먹여 VPIN을 계산하고 그 종목의 봉에 실어 적재한다(2026-07-06: 처음엔 선물에만 적용했으나,
         옵션도 원한다는 사용자 요청으로 종목 구분 없이 통일 — 옵션은 거래량이 얇아 버킷이 느리게
         완성되거나 VPIN이 0.5 근처에 자주 머물 수 있음을 알고 진행).
    실패 조건: DB 연결 실패·WS 단절 시 예외가 위로 전파된다 — 재시작은 프로세스 관리자(Ops) 책임.
    """
    quote = rest_client.get_quote(futures_symbol, market_div_code=tr_codes.FID_MRKT_DIV_INDEX_FUTURES)
    # output3 = KOSPI200 지수 자체, output1 = 조회한 선물 계약가(베이시스 존재) — 지수 우선, 없으면 선물가로 폴백
    spot = float(quote.get("output3", {}).get("bstp_nmix_prpr") or quote.get("output1", {}).get("futs_prpr") or 0.0)
    if spot > 0:
        for subscription_manager in subscription_managers:
            await subscription_manager.roll_to_spot(spot)

    # 시세(H0IFCNT0)도 계좌 무관 공개 데이터라 모의투자 전용 도메인이 없다 — MARKET_DATA_WS_DOMAIN
    # 하나로 옵션 구독과 함께 붙는다.
    await ws_client.subscribe(Subscription(tr_codes.WS_TR_FUTURES_CONTRACT, futures_symbol))

    with db.get_connection() as conn:
        db.upsert_active_futures_symbol(conn, UNDERLYING, futures_symbol, datetime.now())

    # ATM±N 구독은 최대 14개 옵션 종목(행사가×C/P) + 선물 1건을 동시에 켜두므로, 종목별로 별도
    # 집계기가 필요하다 — 하나를 공유하면 서로 다른 종목의 체결가가 한 봉에 뒤섞인다.
    aggregators: dict[str, MinuteBarAggregator] = {}
    volume_buckets: dict[str, VolumeBucketAggregator] = {}
    vpin_returns: dict[str, list[float]] = {}
    vpin_volumes: dict[str, list[float]] = {}

    async def handle_message(message: dict) -> None:
        raw = message.get("raw")
        if raw is None:
            return  # JSON 제어 메시지(구독 응답/PINGPONG)는 무시

        body = raw.split("|", 3)[-1]
        peek_symbol = body.split("^", 1)[0]

        parsed = _parse_futures_tick(raw) if peek_symbol == futures_symbol else _parse_tick(raw)
        if parsed is None:
            return
        tick_symbol, tick = parsed

        bucket = volume_buckets.setdefault(tick_symbol, VolumeBucketAggregator(VPIN_BUCKET_SIZE)).add_tick(
            tick.price, tick.volume
        )
        if bucket is not None:
            returns = vpin_returns.setdefault(tick_symbol, [])
            volumes = vpin_volumes.setdefault(tick_symbol, [])
            returns.append(bucket.open_to_close_return)
            volumes.append(bucket.volume)
            if len(returns) > _VPIN_HISTORY_LIMIT:
                excess = len(returns) - _VPIN_HISTORY_LIMIT
                del returns[:excess]
                del volumes[:excess]

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
                    "vpin": calculate_vpin(vpin_returns.get(tick_symbol, []), vpin_volumes.get(tick_symbol, [])),
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


# H0IFCNT0(지수선물 실시간체결가) 응답 필드 인덱스 — "^" 구분, 0-based. 옵션(H0IOCNT0)과 필드
# 순서가 다르다(가격 idx5, 매도/매수호가 idx34/35 등). 출처: docs/efriend 시트 "지수선물
# 실시간체결가"(API ID 실시간-010), 2026-07-06 실측.
_FUT_IDX_SHRN_ISCD = 0  # 선물 단축종목코드
_FUT_IDX_BSOP_HOUR = 1  # 영업시간 HHMMSS
_FUT_IDX_PRPR = 5  # 선물 현재가
_FUT_IDX_LAST_CNQN = 9  # 최종 거래량(체결량)
_FUT_IDX_ASKP1 = 34  # 선물 매도호가1
_FUT_IDX_BIDP1 = 35  # 선물 매수호가1
_FUT_IDX_ASKP_RSQN1 = 36  # 매도호가 잔량1
_FUT_IDX_BIDP_RSQN1 = 37  # 매수호가 잔량1
_FUT_MIN_FIELDS = _FUT_IDX_BIDP_RSQN1 + 1


def _parse_futures_tick(raw: str, today: date | None = None) -> tuple[str, Tick] | None:
    """
    KIS 실시간 체결가(H0IFCNT0, 지수선물) "^" 구분 파서 — 필드 순서는 위 _FUT_IDX_* 참고.

    입력/계산/실패 조건: _parse_tick과 동일한 파이프 헤더 스트립 로직을 공유한다(암호화유무|
    TR_ID|데이터건수 헤더 제거 후 "^" 분리) — 옵션과 필드 순서만 다를 뿐 헤더 형태는 동일.
    """
    body = raw.split("|", 3)[-1]
    fields = body.split("^")
    if len(fields) < _FUT_MIN_FIELDS:
        return None
    try:
        hhmmss = fields[_FUT_IDX_BSOP_HOUR]
        tick_time = dtime(int(hhmmss[0:2]), int(hhmmss[2:4]), int(hhmmss[4:6]))
        timestamp = datetime.combine(today or date.today(), tick_time)
        symbol = fields[_FUT_IDX_SHRN_ISCD]
        tick = Tick(
            timestamp=timestamp,
            price=float(fields[_FUT_IDX_PRPR]),
            volume=float(fields[_FUT_IDX_LAST_CNQN]),
            bid_px=float(fields[_FUT_IDX_BIDP1]),
            bid_qty=float(fields[_FUT_IDX_BIDP_RSQN1]),
            ask_px=float(fields[_FUT_IDX_ASKP1]),
            ask_qty=float(fields[_FUT_IDX_ASKP_RSQN1]),
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
    books: list[tuple[RollingSubscriptionManager, str]],
    master: IndexDerivativesMaster,
    underlying: str = UNDERLYING,
    interval_seconds: float = OPTION_CHAIN_POLL_INTERVAL_SECONDS,
) -> None:
    """
    입력: REST 클라이언트, (구독 매니저, series) 튜플 목록(2026-07-06부터 리스트 — 먼슬리
         "regular" 북과 위클리 "weekly" 북을 동시에 폴링하기 위함. 각 북은 WS와 동일한 행사가
         집합을 공유), 종목코드 마스터.
    계산: 북마다 WS 구독 중인 행사가×콜/풋 각각에 대해 주기적으로 get_quote()를 호출해
         그릭스/IV/OI를 option_analysis_1m에, 기초자산 스팟을 underlying_spot_1m에 적재한다.
         get_quote()는 동기(블로킹) httpx 호출이라 asyncio.to_thread로 실행해 WS 수신 루프를
         막지 않는다.
    실패 조건: 개별 종목 조회/파싱 실패는 건너뛰고 다음 종목을 계속 처리한다 — REST 폴링 중
              하나가 실패했다고 WS 관측 전체가 죽으면 안 된다. 북 전부가 아직 구독이 없으면
              (기동 초입) 2초 뒤 재확인.
    """
    while True:
        poll_time = datetime.now().replace(second=0, microsecond=0)
        latest_spot: float | None = None
        rows: list[dict] = []
        any_strikes = False
        for subscription_manager, series in books:
            strikes = subscription_manager.desired_strikes
            if not strikes:
                continue
            any_strikes = True
            for strike in strikes:
                for option_type in ("C", "P"):
                    symbol = master.option_symbol(option_type, strike, underlying=underlying, series=series)
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

        if not any_strikes:
            await asyncio.sleep(2.0)
            continue

        if rows:
            with db.get_connection() as conn:
                for row in rows:
                    try:
                        db.insert_option_analysis_1m(conn, row)
                    except Exception:
                        # 2026-07-06 위클리 도입 후 실측: 거래가 없는 얇은 종목이 IV 등에 비정상적으로
                        # 큰 값을 돌려줘 DECIMAL(8,6) 컬럼 범위를 넘는 numeric field overflow가 발생함 —
                        # 레그 하나가 죽는다고 사이클 전체(선물 틱 수신까지)가 죽으면 안 되므로 스킵.
                        # rollback 필수: psycopg는 실패한 트랜잭션에서 커밋 없이 다음 execute를 허용 안 함.
                        logger.warning(
                            "옵션 체인 적재 실패(값 이상 등): strike=%s type=%s",
                            row.get("strike"), row.get("option_type"), exc_info=True,
                        )
                        conn.rollback()
                        continue
                if latest_spot is not None:
                    try:
                        db.insert_underlying_spot(conn, poll_time, underlying, latest_spot)
                    except Exception:
                        logger.warning("기초자산 스팟 적재 실패", exc_info=True)
                        conn.rollback()

        await asyncio.sleep(interval_seconds)


def _atm_liquidity_window(strikes: frozenset[float], each_side: int) -> list[float]:
    """
    입력: 구독 매니저의 ATM±STRIKES_EACH_SIDE 전체 행사가 집합(WS 구독 범위, 예: ATM±3=7개),
         유동성 지표에 쓸 편측 개수(LIQUIDITY_ATM_EACH_SIDE=2).
    계산: 오름차순 정렬 후 정중앙(ATM)을 기준으로 ±each_side만 잘라낸다 — strikes_around_atm()이
         항상 ATM을 중앙에 두는 대칭 격자를 만들기 때문에 정렬 후 중앙 인덱스를 잡으면 된다.
    실패 조건: strikes가 비어 있으면 빈 리스트(호출측이 이번 사이클을 건너뜀).
    """
    ordered = sorted(strikes)
    if not ordered:
        return []
    mid = len(ordered) // 2
    lo = max(mid - each_side, 0)
    hi = min(mid + each_side + 1, len(ordered))
    return ordered[lo:hi]


def _parse_asking_price_leg(resp: dict) -> tuple[float, float, float] | None:
    """
    입력: KISRestClient.get_asking_price() 응답(output1=현재가/누적거래량, output2=5단계 호가).
    계산: 최우선 매도/매수호가로 상대(%) 스프레드를 구한다 — Cao & Wei(2010)가 옵션은 만기·
         머니니스에 따라 달러 스프레드가 기계적으로 달라지므로 유동성 지표로 부적합하다고 지적한
         근거를 따라 %스프레드를 쓴다. 호가잔량 합(깊이)과 누적거래량도 함께 반환.
    실패 조건: 필드 누락/숫자 변환 실패, 또는 양쪽 호가가 모두 비어 mid<=0(체결 자체가 없는
              얇은 종목)이면 None — %스프레드 정의 자체가 불가하므로 이번 레그는 집계에서 제외.
    """
    output1 = resp.get("output1") or {}
    output2 = resp.get("output2") or {}
    try:
        ask1 = float(output2["futs_askp1"])
        bid1 = float(output2["futs_bidp1"])
        ask_qty = float(output2["askp_rsqn1"])
        bid_qty = float(output2["bidp_rsqn1"])
        volume = float(output1["acml_vol"])
    except (KeyError, ValueError, TypeError):
        return None
    mid = (ask1 + bid1) / 2
    if mid <= 0:
        return None
    spread_pct = (ask1 - bid1) / mid
    depth = ask_qty + bid_qty
    return spread_pct, depth, volume


async def poll_expiry_liquidity(
    rest_client: KISRestClient,
    books: list[tuple[RollingSubscriptionManager, str]],
    master: IndexDerivativesMaster,
    underlying: str = UNDERLYING,
    interval_seconds: float = EXPIRY_LIQUIDITY_POLL_INTERVAL_SECONDS,
) -> None:
    """
    입력: REST 클라이언트, (구독 매니저, series) 튜플 목록(먼슬리 "regular" + 위클리 "weekly"),
         종목코드 마스터.
    계산: 북마다 ATM±2(_atm_liquidity_window) 구간의 콜/풋 각각에 get_asking_price()를 호출해
         %스프레드·깊이·거래량을 집계하고, 만기일은 ATM 종목 1건만 get_quote()로 별도 확인해
         (_parse_option_quote 재사용) expiry_liquidity_1m에 적재한다. 만기 확인용 get_quote()는
         북당 사이클당 1건뿐이라 REST 부하에 미치는 영향은 무시할 만하다.
    실패 조건: 개별 레그 조회/파싱 실패는 건너뛰고 나머지로 계속 집계한다. 유효한 레그가 하나도
              없거나 만기를 확인하지 못하면 그 북은 이번 사이클을 건너뛴다. 모든 북에 구독 행사가가
              없으면(기동 초입) 2초 뒤 재확인.
    """
    while True:
        poll_time = datetime.now().replace(second=0, microsecond=0)
        any_strikes = False
        rows: list[dict] = []
        for subscription_manager, series in books:
            window = _atm_liquidity_window(subscription_manager.desired_strikes, LIQUIDITY_ATM_EACH_SIDE)
            if not window:
                continue
            any_strikes = True

            atm_strike = window[len(window) // 2]
            anchor_symbol = master.option_symbol("C", atm_strike, underlying=underlying, series=series)
            expiry = None
            if anchor_symbol is not None:
                try:
                    anchor_resp = await asyncio.to_thread(rest_client.get_quote, anchor_symbol)
                    parsed_anchor = _parse_option_quote(anchor_resp, atm_strike, "C", poll_time)
                except Exception:
                    parsed_anchor = None
                if parsed_anchor is not None:
                    expiry = parsed_anchor[0]["expiry"]
            if expiry is None:
                continue

            spread_values: list[float] = []
            depth_total = 0.0
            volume_total = 0.0
            for strike in window:
                for option_type in ("C", "P"):
                    symbol = master.option_symbol(option_type, strike, underlying=underlying, series=series)
                    if symbol is None:
                        continue
                    try:
                        resp = await asyncio.to_thread(rest_client.get_asking_price, symbol)
                    except Exception:
                        logger.warning("만기 유동성 폴링 실패: %s", symbol, exc_info=True)
                        continue
                    parsed_leg = _parse_asking_price_leg(resp)
                    if parsed_leg is None:
                        continue
                    spread_pct, depth, volume = parsed_leg
                    spread_values.append(spread_pct)
                    depth_total += depth
                    volume_total += volume

            if not spread_values:
                continue

            rows.append(
                {
                    "timestamp": poll_time,
                    "underlying": underlying,
                    "series": series,
                    "expiry": expiry,
                    "atm_spread_pct": sum(spread_values) / len(spread_values),
                    "depth": depth_total,
                    "volume": volume_total,
                    "days_to_expiry": max((expiry - poll_time.date()).days, 0),
                }
            )

        if not any_strikes:
            await asyncio.sleep(2.0)
            continue

        if rows:
            with db.get_connection() as conn:
                for row in rows:
                    try:
                        db.insert_expiry_liquidity_1m(conn, row)
                    except Exception:
                        logger.warning("만기 유동성 적재 실패: series=%s", row.get("series"), exc_info=True)
                        conn.rollback()
                        continue

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
        # 먼슬리(정규 월물)와 위클리를 별도 매니저로 동시에 굴린다(2026-07-06 추가) — 슬롯 예산:
        # 14(먼슬리) + 14(위클리) + 1(선물) = 29 / MAX_SUBSCRIPTIONS(41), 여유 있음.
        monthly_manager = RollingSubscriptionManager(
            ws_client,
            tr_id=tr_codes.WS_TR_OPTION_CONTRACT,
            strike_interval=KOSPI200_OPTION_STRIKE_INTERVAL,
            strikes_each_side=STRIKES_EACH_SIDE,
            symbol_formatter=lambda strike, opt: master.option_symbol(opt, strike, underlying="KOSPI200"),
        )
        weekly_manager = RollingSubscriptionManager(
            ws_client,
            tr_id=tr_codes.WS_TR_OPTION_CONTRACT,
            strike_interval=KOSPI200_OPTION_STRIKE_INTERVAL,
            strikes_each_side=STRIKES_EACH_SIDE,
            symbol_formatter=lambda strike, opt: master.option_symbol(
                opt, strike, underlying="KOSPI200", series="weekly"
            ),
        )
        books = [(monthly_manager, "regular"), (weekly_manager, "weekly")]
        await asyncio.gather(
            run_observation_loop(
                ws_client, [monthly_manager, weekly_manager], rest_client, futures_symbol=futures_symbol
            ),
            poll_option_chain(rest_client, books, master),
            poll_expiry_liquidity(rest_client, books, master),
            poll_investor_flow(rest_client),
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Ctrl+C로 종료합니다.")
