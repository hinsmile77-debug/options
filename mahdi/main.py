"""관측 전용 오케스트레이터 (Phase1 범위) — 토큰 데몬 -> WS 수집기 -> 1분 집계 -> DB 적재.

Regime 갱신은 mahdi.engines.regime_pipeline.RegimeStateMachine에 위임한다(2026-07-10 실배선
완료) — 선물봉마다 §7.3 6개 피처를 실데이터로 계산해 feature_store에 축적하고, data/models/에
캘리브레이션된 모델이 있으면 predict(), 없으면 실제 gap_zscore/투자자수급 기반 macro_score/전일
마감 레짐을 넣은 warmup_fallback()을 쓴다. HMM fit()은 feature_store가 충분히(수십 세션) 쌓인
뒤 scripts/fit_regime_engine.py로 오프라인 실행한다.

주문 실행/리스크 게이트는 Phase2 범위이므로 여기서는 다루지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import date, datetime, time as dtime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx
import websockets

from mahdi.broker import tr_codes
from mahdi.broker.rest_client import KISRestClient
from mahdi.broker.token_daemon import TokenDaemon
from mahdi.broker.ws_client import ApprovalKeyIssuer, KISWebSocketClient, Subscription, WSConnection
from mahdi.config.settings import PROJECT_ROOT, get_db_settings, get_kis_settings, get_slack_settings
from mahdi import notify
from mahdi.data import db
from mahdi.data.collector import MinuteBarAggregator, Tick, VolumeBucketAggregator
from mahdi.data.subscription_manager import RollingSubscriptionManager
from mahdi.data.overseas_future_master import OverseasFutureMaster, load_overseas_future_master
from mahdi.data.symbol_master import IndexDerivativesMaster, load_index_derivatives_master
from mahdi.data import yfinance_fallback
from mahdi.engines.regime_pipeline import RegimeStateMachine
from mahdi.features.options_intel import OptionLeg, calculate_gex, calculate_vrp
from mahdi.features.orderflow import calculate_vpin
from mahdi.logutil import WarningThrottle

logger = logging.getLogger("mahdi.main")

# 2026-07-19(§5-5 "로그 위생") — logs/observation_loop.log가 로테이션 없이 105MB까지 누적된 문제.
# 배치파일(scripts/start_mahdi_premarket.bat)의 stdout 리다이렉트(`>> logs\observation_loop.log`)를
# 걷어내고 Python 로깅이 이 파일을 직접 소유·회전시킨다 — 파일당 10MB, 최근 10개(최대 약 110MB)만
# 유지하고 그 이전은 자동 삭제된다.
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "observation_loop.log"
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 10

# 2026-07-20(고도화) — 07-17(금) 15:45 장마감 자동 종료가 스케줄대로 실행되지 못하고 다음날
# 지연 실행됐던 사례처럼, 예약 실행이 하루 이상 건너뛰어도 사람이 매번 로그 타임스탬프를 손으로
# 비교하지 않아도 알아챌 수 있게 한다. 날짜 연산은 배치파일(cmd.exe)이 아니라 여기서 한다 —
# cmd.exe 쪽 날짜 연산/문자열 처리는 로케일·인코딩에 따라 예측 못한 방식으로 깨지기 쉽다는 걸
# 이번 작업 중 실제로 겪었다(격리 테스트 중 재현 및 회복이 어려운 방식으로 멈춘 사례 있음 —
# 라이브 스케줄 스크립트에는 반영하지 않기로 결정).
LAST_START_MARKER_FILE = LOG_DIR / ".last_successful_start.txt"

# 같은 종류의 WARNING이 짧은 시간 안에 반복되면(예: 얇은 옵션 종목의 NumericValueOutOfRange가
# 60초 사이클 안에서 레그마다 반복 — §3-1) 창(window)당 최초 1건만 로깅해 로그 파일이 그 반복으로
# 파묻히지 않게 한다(mahdi/logutil.py 참고). 각 폴러 함수 안에서 지역 변수로 만든다(모듈
# 전역으로 두면 실제 운영에선 어차피 함수당 1회만 호출돼 차이가 없지만, 테스트에서 서로 다른
# 테스트 함수 호출이 같은 60초 실시간 윈도를 공유해 억제 상태가 새어나가는 문제가 생긴다).
WARNING_THROTTLE_WINDOW_SECONDS = 60.0

KOSPI200_OPTION_STRIKE_INTERVAL = 2.5
# 2026-07-10: 위클리를 월/목 두 북으로 분리하며 3(먼슬리+위클리월+위클리목)으로 늘어난
# 북 수에 맞춰 ATM±3에서 ATM±2로 축소 — (2*2+1)*2(C/P)=10슬롯×3북+1(선물)=31/MAX_SUBSCRIPTIONS(41).
# ATM±3 유지 시 14×3+1=43으로 한도 초과(RESEARCH_EXPIRY_SELECTION_v1.md §3.1 예상대로).
STRIKES_EACH_SIDE = 2
SYMBOL_MASTER_CACHE_DIR = Path("data/symbol_master_cache")  # KIS 마스터파일은 매일 갱신됨
OVERSEAS_FUTURE_MASTER_CACHE_DIR = Path("data/overseas_future_master_cache")

# Cross-asset stress(v6 §7.3) 매크로 스냅샷 — 스펙이 요구하는 "5분" 주기(§5.1 표).
MACRO_SNAPSHOT_POLL_INTERVAL_SECONDS = 300
# US10Y는 계좌에 CBOT 거래소 신청이 안 된 동안 일봉 API로만 얻을 수 있어(tr_codes.py 상단 주석
# 참고) 매 사이클 짧은 기간을 조회해 최신 종가 1건만 취한다 — 10일이면 공휴일이 며칠 껴도
# 최소 1건은 반환된다.
US10Y_LOOKBACK_DAYS = 10
UNDERLYING = "KOSPI200"
OPTION_CHAIN_POLL_INTERVAL_SECONDS = 60  # WS 구독(ATM±STRIKES_EACH_SIDE) 범위와 동일한 종목을 REST로 주기 조회

# 2026-07-08 실측: 레이트리밋 버스트 등으로 사이클 내 모든 종목 조회가 한꺼번에 실패하는 경우
# (정규장 405분 중 203분치 옵션체인 데이터가 통째로 유실됨을 DB로 확인)가 있었다 — 60초 다음
# 사이클까지 기다리지 않고 짧게 대기 후 한 번 더 시도해 유실을 줄인다.
CYCLE_RETRY_BACKOFF_SECONDS = 5.0

# 2026-07-19(§5-4 "능동 알림") — 마지막 성공 사이클 이후 이만큼 지나면(재시도까지 실패한 사이클이
# 누적돼) option_analysis_1m 결손을 Slack으로 알린다. 운영점검보고서가 예시로 든 기준(5분)을 그대로 씀.
OPTION_CHAIN_GAP_ALERT_SECONDS = 300.0

# 2026-07-21(장전 점검 후속) — macro_snapshot_5m INSERT가 연속 실패하면(2026-07-21 실측:
# db/migrations 010/011 라이브 미적용으로 UndefinedColumn이 종일 반복됨) Slack으로 알린다.
# 사이클 자체가 이미 5분 주기라 1회 실패는 일시적 DB 지연 등일 수 있어 즉시 알리지 않고, 2회
# 연속(=10분)부터 스키마 불일치 같은 지속성 문제로 보고 알린다.
MACRO_SNAPSHOT_INSERT_FAILURE_ALERT_STREAK = 2

# VPIN 등거래량 버킷 크기 — 실거래 일평균거래량 관찰 전까지 쓰는 잠정치. 학계 관례는
# "일평균거래량/50"이지만 이 모의투자 환경의 실제 거래량 분포를 아직 모른다(2026-07-06 결정).
# 옵션은 선물보다 훨씬 얇아 버킷이 완성되기까지 오래 걸리거나 VPIN이 0.5(중립) 근처에 자주
# 머물 수 있다는 걸 알고 쓴다(2026-07-06 사용자에게 설명 후 옵션에도 적용하기로 확정).
VPIN_BUCKET_SIZE = 50
_VPIN_HISTORY_LIMIT = 500  # calculate_vpin 기본 window(50)의 10배 — 무한정 누적 방지

# Phase 1.5-③(만기 유동성 기준선, 2026-07-06 추가) — 연구문서(RESEARCH_EXPIRY_SELECTION_v1.md)가
# 권고하는 "ATM±2 집중"(Cao-Wei %스프레드 기준, 스캘핑에 최적인 구간). 도입 당시엔 WS 구독 범위
# (ATM±3)보다 좁았으나, 2026-07-10 위클리 월/목 분리로 STRIKES_EACH_SIDE 자체가 2로 줄어
# 지금은 WS 구독 범위와 폭이 같다(그래도 별도 상수로 유지 — 의미가 달라 향후 STRIKES_EACH_SIDE가
# 다시 늘어나도 유동성 관측 구간은 ATM±2로 고정하고 싶을 수 있음). get_asking_price()는 북당
# 5행사가×2(C/P)=10건 신규 REST 호출이라, 오늘 이미 1x 부하에서 403/500 레이트리밋을 관찰한
# 점을 고려해 폴링 주기를 옵션체인(60초)보다 훨씬 길게 잡았다 — 어차피 이 지표의 용도는 실시간
# 판단이 아니라 20거래일 기준선 축적이라 촘촘히 볼 필요가 없다(사용자 확인 후 %스프레드 포함
# 정식 스펙대로 진행하되, 호출빈도로 부하를 완화하기로 결정).
LIQUIDITY_ATM_EACH_SIDE = 2
EXPIRY_LIQUIDITY_POLL_INTERVAL_SECONDS = 300

# 2026-07-09 실측: poll_option_chain(60초, ~28콜)과 poll_expiry_liquidity(300초, ~11콜)가 같은
# 순간에 공유 _RateLimiter 큐에 들어가면 그 사이클만 대기시간이 늘어나 poll_option_chain의
# "작업 후 sleep" 실측 주기(60초+작업시간, 평소에도 60초를 넘김)가 분 경계를 하나 더 건너뛰어
# 정확히 5분 간격(EXPIRY_LIQUIDITY_POLL_INTERVAL_SECONDS 배수)으로 옵션체인 1분봉이 통째로
# 유실되는 패턴을 DB로 확인했다 — 두 폴러의 사이클 시작을 어긋나게 둬 충돌 확률 자체를 낮춘다
# (근본 수정은 poll_option_chain/poll_investor_flow의 고정 틱 스케줄링, 이건 보조 완화).
EXPIRY_LIQUIDITY_STARTUP_OFFSET_SECONDS = 30.0


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
    regime_state_machine: RegimeStateMachine,
) -> None:
    """
    입력: 이미 연결된 WS 클라이언트, 구독 롤링 매니저 목록(2026-07-06부터 리스트 — 먼슬리/위클리
         등 여러 만기 북을 동시에 굴리기 위함, 북마다 별도 인스턴스), REST 클라이언트, 최근월
         지수선물 단축코드(예: "101S03" — 분기마다 바뀌므로 종목코드 마스터파일/설정으로 최신화
         필요), 레짐 상태머신(mahdi.engines.regime_pipeline.RegimeStateMachine).
    계산: "선물옵션 시세"(inquire-price, F 시장)로 초기 스팟(KOSPI200 지수) 조회 → 모든 구독
         매니저를 그 스팟으로 롤링(각자 자기 만기 시리즈에서 ATM±N을 계산) → 선물 실시간체결가
         (H0IFCNT0)도 함께 구독(2026-07-06 추가) → 현재 선물 단축코드를 active_futures_symbol에
         등록(대시보드가 "이 종목이 선물인지" 바로 조회 가능하게) → WS 메시지 수신 → 종목(선물·
         옵션·만기북 구분 없이)별로 1분봉 완성 시 market_raw_1m 적재(각 틱에 실린 종목코드를
         그대로 사용 — 북마다 최대 14개 옵션 종목이 동시에 켜지므로 종목별 분리가 필수). 레짐은
         선물봉이 완성될 때만 regime_state_machine.step()으로 갱신한다(2026-07-10 — 레짐은
         기초자산 하나에 대한 판단이라 옵션 레그 봉 완성 타이밍과는 무관해야 함; 이전에는 모든
         종목의 봉 완성마다 재계산해 같은 분에 여러 번 덮어쓰고 있었다). 모든 종목의 틱은
         등거래량 버킷(VolumeBucketAggregator)에도 먹여 VPIN을 계산하고 그 종목의 봉에 실어
         적재한다(2026-07-06: 처음엔 선물에만 적용했으나, 옵션도 원한다는 사용자 요청으로 종목
         구분 없이 통일 — 옵션은 거래량이 얇아 버킷이 느리게 완성되거나 VPIN이 0.5 근처에 자주
         머물 수 있음을 알고 진행).
    실패 조건: DB 연결 실패·WS 단절 시 예외가 위로 전파된다. WS 단절(재연결)은
              run_observation_loop_forever가 감싸서 처리하므로, 이 함수 자체는 여전히 "한 번의
              연결이 끊기면 예외를 던지고 끝"으로 단순하게 남겨둔다(2026-07-19).
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
        db.upsert_active_futures_symbol(conn, UNDERLYING, futures_symbol, db.local_now())

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
            if tick_symbol == futures_symbol:
                regime_state_machine.update_bar(bar)
                state = regime_state_machine.step(conn, bar.minute)
                db.insert_regime_state(
                    conn,
                    timestamp=bar.minute,
                    regime=int(state.regime),
                    prob_vector=list(state.prob_vector),
                    higher_tf_regime=int(state.higher_tf_regime) if state.higher_tf_regime is not None else None,
                    stability_flag=state.stability_flag,
                )

    await ws_client.listen(handle_message)


# WS 재연결(2026-07-19, 운영점검보고서 §5-2) — 재연결 시도 사이 대기시간. 반복적으로 계속
# 끊기는 상황(네트워크 장애 등)에서 무한정 짧은 간격으로 재시도해 KIS 서버에 부하를 주지 않도록
# 지수 백오프를 쓰되, 한 번이라도 연결에 성공하면 다음 끊김부터는 다시 초기값부터 시작한다
# (연결이 오래 잘 유지되다 어쩌다 한 번 끊긴 경우까지 계속 escalate될 이유가 없음).
WS_RECONNECT_INITIAL_BACKOFF_SECONDS = 5.0
WS_RECONNECT_MAX_BACKOFF_SECONDS = 60.0

# websockets 라이브러리 자체 예외(ConnectionClosed 등)는 WebSocketException 계열이고, 소켓 단의
# 실패(연결 거부 등)는 OSError(ConnectionError는 OSError의 서브클래스) 계열이다 — 재연결 대상은
# 이 둘뿐이다. DB 예외/ValueError(구독 슬롯 한도 등)는 재시도로 해결되지 않는 별개의 문제라
# 여기서 잡지 않고 그대로 전파해 사람이 보게 한다.
_WS_DISCONNECT_ERRORS = (OSError, websockets.WebSocketException)


async def run_observation_loop_forever(
    ws_client: KISWebSocketClient,
    subscription_managers: list[RollingSubscriptionManager],
    rest_client: KISRestClient,
    futures_symbol: str,
    regime_state_machine: RegimeStateMachine,
    approval_key: str,
    connect=websockets.connect,
) -> None:
    """
    입력: run_observation_loop과 동일 + 이미 연결된 첫 WS 클라이언트(호출측이 최초 1회 연결해
         넘긴다 — main()의 `async with websockets.connect(...)` 블록 안에서 만든 것), 재연결 시
         새 WS 클라이언트를 만드는 데 쓸 approval_key, connect(테스트 주입용, 기본값 websockets.connect).
    계산: run_observation_loop을 감싸 WS 단절(_WS_DISCONNECT_ERRORS) 시 프로세스를 죽이는 대신
         backoff초 대기 후 재연결한다. 2026-07-16 점검(§3-1B/§4/§5-2)에서 "WS 연결이 끊기면
         재연결 로직이 아예 없어 그대로 죽는다"고 지적된 항목 — Phase1(관측)에서는 "관측 공백"
         정도지만 Phase2가 실제 포지션을 잡기 시작하면 "포지션을 인지 못 하는" 리스크가 되므로
         Phase2 착수 전에 먼저 처리해야 한다는 게 그 이유. 재연결마다 새 KISWebSocketClient를
         만들고 모든 subscription_managers를 rebind()한다 — rebind()가 각 매니저의
         _desired_strikes를 비우므로, 다음 run_observation_loop() 안의 roll_to_spot() 호출이
         diff가 아니라 현재 ATM±N 범위 전체를 새 연결에 처음부터 다시 구독한다(그렇지 않으면
         서버 쪽 구독 상태는 재연결로 완전히 사라졌는데 매니저만 "이미 구독 중"이라고 착각해
         아무것도 재구독하지 않는 버그가 생긴다). asyncio.gather로 함께 도는 REST 폴러
         (poll_option_chain 등)는 이 함수와 독립된 태스크라 재연결 시도 중에도 계속 관측을
         이어간다 — 이번 수정 전에는 WS 단절 하나가 asyncio.run(main())까지 예외를 전파시켜
         REST 폴러까지 전부 함께 죽었다. 2026-07-19(§5-4): "연결됨→끊김" 전환 시점에 한 번,
         "끊김→재연결 성공" 전환 시점에 한 번만 Slack 알림을 보낸다(재연결 재시도마다 매번
         보내면 네트워크 장애가 길어질 때 스팸이 된다) — currently_connected 플래그로 전환
         시점만 골라낸다. 이미 끊긴 상태에서 반복되는 재연결 시도 실패 로그는(§5-5 "로그 위생")
         WarningThrottle로 60초당 최초 1건만 남긴다 — 장애가 길어지면 백오프 간격(5~60초)마다
         계속 같은 경고가 찍혀 로그 파일이 그 반복으로 파묻히는 걸 막는다.
    실패 조건: DB 오류·ValueError(구독 슬롯 한도 등) 등 연결 문제가 아닌 예외는 재시도 없이 그대로
              전파한다 — 재시도로 해결되지 않는 코드/설정 문제이므로 사람이 봐야 한다. 정상적으로는
              run_observation_loop이 listen()의 무한 수신 루프라 정상 반환하지 않지만, 혹시라도
              반환하면(예: 테스트) 재연결 없이 그대로 종료한다.
    """
    backoff = WS_RECONNECT_INITIAL_BACKOFF_SECONDS
    currently_connected = True
    warning_throttle = WarningThrottle(logger, window_seconds=WARNING_THROTTLE_WINDOW_SECONDS)
    try:
        await run_observation_loop(
            ws_client, subscription_managers, rest_client,
            futures_symbol=futures_symbol, regime_state_machine=regime_state_machine,
        )
        return
    except _WS_DISCONNECT_ERRORS:
        logger.warning("WS 연결 끊김 — %.0f초 후 재연결 시도", backoff, exc_info=True)
        notify.notify("WS 연결 끊김 — 자동 재연결 시도 중. 장시간 지속되면 KIS/네트워크 상태 확인 필요.", "CRITICAL")
        currently_connected = False

    while True:
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, WS_RECONNECT_MAX_BACKOFF_SECONDS)
        try:
            async with connect(tr_codes.MARKET_DATA_WS_DOMAIN) as raw_ws:
                new_client = KISWebSocketClient(approval_key=approval_key, connection=_WebsocketsAdapter(raw_ws))
                for manager in subscription_managers:
                    manager.rebind(new_client)
                backoff = WS_RECONNECT_INITIAL_BACKOFF_SECONDS  # 연결 성공 — 다음 끊김은 다시 초기값부터
                if not currently_connected:
                    notify.notify("WS 재연결 성공 — 관측 재개.", "INFO")
                    currently_connected = True
                await run_observation_loop(
                    new_client, subscription_managers, rest_client,
                    futures_symbol=futures_symbol, regime_state_machine=regime_state_machine,
                )
                return
        except _WS_DISCONNECT_ERRORS:
            if currently_connected:
                logger.warning("WS 재연결 후 다시 끊김 — %.0f초 후 재시도", backoff, exc_info=True)
                notify.notify("WS 연결 끊김 — 자동 재연결 시도 중. 장시간 지속되면 KIS/네트워크 상태 확인 필요.", "CRITICAL")
                currently_connected = False
            else:
                # 2026-07-19(§5-5): 장애가 길어지면 백오프 간격마다(5~60초) 계속 반복되는 경고 —
                # 이미 위에서 "끊김" 알림은 한 번 나갔으므로 여기선 로그 위생만 신경 쓴다.
                warning_throttle.warning(
                    "ws_reconnect_attempt_failed", "WS 재연결 시도 실패 — %.0f초 후 재시도", backoff, exc_info=True,
                )
            continue


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
    참고: 여기서 새 KIS 필드를 DB 컬럼에 매핑하기 전에 `docs/dev_memory/KIS_RAW_FIELD_RANGES.md`부터
         확인할 것 — theta가 정규화 안 된 원화 절대값이라 DECIMAL(8,6)에서 상시 오버플로우하던
         버그(2026-07-16 발견, 2026-07-21 근본원인 확정)를 반복하지 않기 위한 참고표다.
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
            # DB 컬럼이 아닌 진단용 필드 — _upsert()는 _OPTION_ANALYSIS_1M_COLUMNS에 있는
            # 키만 읽으므로 여기 얹어도 INSERT 쿼리에 섞이지 않는다. 2026-07-16 점검에서
            # 특정 행사가의 IV 등이 DECIMAL(8,6) 범위를 넘어 삽입이 실패하는데 원인(실제 raw
            # 값이 뭐였는지)을 알 길이 없었던 문제 — 실패 시점에 KIS 원본 응답을 그대로 로깅할
            # 수 있게 파싱 이전 output1을 함께 들고 다닌다.
            "_raw_kis_output1": output1,
        }
        return row, spot
    except (KeyError, ValueError, TypeError):
        return None


async def _collect_option_chain_cycle(
    rest_client: KISRestClient,
    books: list[tuple[RollingSubscriptionManager, str]],
    master: IndexDerivativesMaster,
    underlying: str,
    poll_time: datetime,
    warning_throttle: WarningThrottle,
) -> tuple[list[dict], float | None, bool]:
    """
    입력/계산: poll_option_chain 한 사이클분 — 북마다 행사가×콜/풋 각각에 get_quote()를 호출해
         파싱 성공한 행(option_analysis_1m용)과 마지막으로 확인된 기초자산 스팟을 모은다.
         warning_throttle은 poll_option_chain이 사이클 전체에서 공유하는 인스턴스를 그대로
         전달받는다(레그별 실패 로그를 §5-5와 동일하게 60초당 최초 1건으로 억제하기 위함,
         2026-07-20).
    실패 조건: 개별 종목 조회/파싱 실패는 건너뛰고 다음 종목을 계속 처리한다(rows에서 빠질 뿐).
              반환하는 any_strikes=False는 "아직 구독 자체가 없다"(기동 초입)를 뜻하고, rows가
              빈 리스트인 것과는 구분된다 — 호출측이 재시도 여부를 판단하는 데 쓴다.
    로그(2026-07-20): 조회 실패는 `_log_kis_call_failure`로 응답 바디(레이트리밋 등 KIS 원인
              코드)를 함께 남기고, `warning_throttle`로 반복을 억제한다 — 이전에는 매 건 풀
              트레이스백이 그대로 찍혀 로그가 파묻혔고, 원인(레이트리밋 vs 그 외)도 알 수 없었다.
    """
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
                except Exception as exc:
                    _log_kis_call_failure(
                        f"옵션 체인 폴링 실패: {symbol}", exc,
                        throttle=warning_throttle, category="option_chain_leg_fetch_failure",
                    )
                    continue
                parsed = _parse_option_quote(resp, strike, option_type, poll_time)
                if parsed is None:
                    continue
                row, spot = parsed
                rows.append(row)
                latest_spot = spot
    return rows, latest_spot, any_strikes


def _update_atm_iv(regime_state_machine: RegimeStateMachine | None, rows: list[dict], latest_spot: float | None) -> None:
    """
    입력: 레짐 상태머신(없으면 아무 것도 안 함), 이번 사이클에서 파싱된 옵션체인 행들, 최신 스팟.
    계산: 스팟에 가장 가까운 행사가를 ATM으로 보고 그 행사가의 콜/풋 IV 평균을 구해
         RegimeFeatureBuilder의 iv_chg 롤링 윈도에 흘려넣는다(§7.3 ATM IV 변화율 근사 입력).
    실패 조건: rows나 latest_spot이 없으면 건너뛴다.
    """
    if regime_state_machine is None or not rows or latest_spot is None:
        return
    atm_strike = min(rows, key=lambda r: abs(r["strike"] - latest_spot))["strike"]
    ivs = [r["iv"] for r in rows if r["strike"] == atm_strike and r.get("iv") is not None]
    if ivs:
        regime_state_machine.update_iv(sum(ivs) / len(ivs))


async def poll_option_chain(
    rest_client: KISRestClient,
    books: list[tuple[RollingSubscriptionManager, str]],
    master: IndexDerivativesMaster,
    underlying: str = UNDERLYING,
    interval_seconds: float = OPTION_CHAIN_POLL_INTERVAL_SECONDS,
    retry_backoff_seconds: float = CYCLE_RETRY_BACKOFF_SECONDS,
    regime_state_machine: RegimeStateMachine | None = None,
) -> None:
    """
    입력: REST 클라이언트, (구독 매니저, series) 튜플 목록(2026-07-06부터 리스트 — 먼슬리
         "regular" 북과 위클리(월) "weekly_mon"/위클리(목) "weekly_thu" 북을 동시에 폴링하기
         위함, 2026-07-10 위클리 분리). 각 북은 WS와 동일한 행사가
         집합을 공유), 종목코드 마스터.
    계산: 북마다 WS 구독 중인 행사가×콜/풋 각각에 대해 주기적으로 get_quote()를 호출해
         그릭스/IV/OI를 option_analysis_1m에, 기초자산 스팟을 underlying_spot_1m에 적재한다.
         get_quote()는 동기(블로킹) httpx 호출이라 asyncio.to_thread로 실행해 WS 수신 루프를
         막지 않는다. 구독 종목이 있는데 한 건도 성공하지 못했다면(레이트리밋 버스트 등으로
         사이클 전체가 실패) retry_backoff_seconds 뒤 그 사이클을 한 번만 재시도한다.
    실패 조건: 개별 종목 조회/파싱 실패는 건너뛰고 다음 종목을 계속 처리한다 — REST 폴링 중
              하나가 실패했다고 WS 관측 전체가 죽으면 안 된다. 재시도까지 실패하면 이번 사이클은
              포기하고(로그만 남김) 다음 정규 사이클(interval_seconds)로 넘어간다. 북 전부가
              아직 구독이 없으면(기동 초입) 2초 뒤 재확인.
    스케줄: "작업 후 interval만큼 sleep"이 아니라 절대시각 기준 고정 틱(next_tick)으로 다음
           사이클을 예약한다 — 사이클 소요시간(레이트리밋 대기 포함)만큼 실제 주기가 매번 밀리면
           poll_time(분 단위로 자른 시각)이 분 경계를 건너뛰어 그 분의 1분봉이 통째로 유실되는
           현상을 2026-07-09에 DB로 확인했다. 사이클이 interval_seconds보다 오래 걸려 다음 틱을
           이미 지나쳤으면(delay<0) 따라잡으려 하지 않고 그 시점으로 스케줄을 재기준한다.
    알림(2026-07-19, §5-4): 마지막으로 rows를 받은 시각(last_success_time) 대비
              OPTION_CHAIN_GAP_ALERT_SECONDS(5분)를 넘겨도 재시도까지 계속 실패하면 결손을
              Slack으로 한 번 알리고(gap_alerted), 다음에 rows가 다시 들어오면 복구를 한 번
              알린다 — 매 사이클(60초)마다 반복 경고하면 스팸이 되므로 상태 전환 시점에만 보낸다.
    로그 위생(2026-07-19, §5-5): 얇은 옵션 종목의 NumericValueOutOfRange(§3-1)는 한 사이클 안에서
              레그마다(최대 수십 건) 반복 재발할 수 있다 — WarningThrottle로 60초당 최초 1건만
              실제로 로깅해 105MB까지 로테이션 없이 누적됐던 로그가 이 반복으로 다시 파묻히지
              않게 한다.
    """
    next_tick: float | None = None
    last_success_time: datetime | None = None
    gap_alerted = False
    warning_throttle = WarningThrottle(logger, window_seconds=WARNING_THROTTLE_WINDOW_SECONDS)
    while True:
        poll_time = db.local_now().replace(second=0, microsecond=0)
        rows, latest_spot, any_strikes = await _collect_option_chain_cycle(
            rest_client, books, master, underlying, poll_time, warning_throttle
        )

        if not any_strikes:
            next_tick = None  # 구독 전이므로 아직 고정 스케줄을 시작하지 않음
            await asyncio.sleep(2.0)
            continue

        if not rows:
            logger.warning("옵션 체인 폴링 전체 실패 — %.0f초 후 재시도", retry_backoff_seconds)
            await asyncio.sleep(retry_backoff_seconds)
            rows, latest_spot, any_strikes = await _collect_option_chain_cycle(
                rest_client, books, master, underlying, poll_time, warning_throttle
            )
            if not rows:
                logger.warning("옵션 체인 폴링 재시도도 실패 — 이번 사이클 포기")

        if rows:
            if gap_alerted:
                notify.notify("옵션 체인(option_analysis_1m) 데이터 결손 복구됨 — 정상 수신 재개.", "INFO")
                gap_alerted = False
            last_success_time = poll_time
        elif last_success_time is not None and not gap_alerted:
            gap_seconds = (poll_time - last_success_time).total_seconds()
            if gap_seconds >= OPTION_CHAIN_GAP_ALERT_SECONDS:
                notify.notify(
                    f"옵션 체인(option_analysis_1m) 데이터가 {gap_seconds / 60:.0f}분째 결손 중 — "
                    f"REST 폴링이 계속 실패하고 있습니다.",
                    "WARNING",
                )
                gap_alerted = True

        _update_atm_iv(regime_state_machine, rows, latest_spot)

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
                        # 2026-07-16: strike/type만으로는 "왜" 범위를 넘었는지(delta_val이 이상한지,
                        # hts_ints_vltl이 이상한지 등) 알 수 없었다 — KIS 원본 응답(output1)을 함께
                        # 남겨 다음 재발 시 바로 원인 필드를 특정할 수 있게 한다.
                        # 2026-07-19: 같은 사이클 안에서 레그마다 반복될 수 있어(§3-1, 한 사이클에
                        # 최대 수십 건) 60초당 최초 1건만 실제로 로깅한다(§5-5).
                        warning_throttle.warning(
                            "option_chain_leg_insert_failure",
                            "옵션 체인 적재 실패(값 이상 등): strike=%s type=%s raw_kis_output1=%s",
                            row.get("strike"), row.get("option_type"), row.get("_raw_kis_output1"),
                            exc_info=True,
                        )
                        conn.rollback()
                        continue
                if latest_spot is not None:
                    try:
                        db.insert_underlying_spot(conn, poll_time, underlying, latest_spot)
                    except Exception:
                        logger.warning("기초자산 스팟 적재 실패", exc_info=True)
                        conn.rollback()

        loop_now = asyncio.get_running_loop().time()
        next_tick = interval_seconds + (loop_now if next_tick is None else next_tick)
        delay = next_tick - loop_now
        overrun_seconds = max(-delay, 0.0)
        if delay < 0:
            logger.warning(
                "옵션 체인 폴링 사이클이 주기(%.0f초)를 초과해 스케줄이 %.1f초 밀렸습니다 — 이번 틱은 즉시 재기준",
                interval_seconds, overrun_seconds,
            )
            next_tick = loop_now
            delay = 0.0
        # 2026-07-23(운영점검보고서 §2-1/§4 Fix#4): 관측 루프와 COCKPIT은 별도 프로세스라
        # 공유 _RateLimiter의 실시간 배율을 COCKPIT이 직접 읽을 수 없다 — 매 사이클(60초)마다
        # 싱글턴 행에 최신 배율/직전 밀림 초를 기록해 "오늘의 점검 요약" 배지가 재시작 없이
        # 바로 보게 한다. 기록 실패가 폴링 루프 자체를 막으면 안 되므로 조용히 넘어간다.
        try:
            with db.get_connection() as conn:
                # db.local_now()를 다시 부르지 않고 이번 사이클의 poll_time을 그대로 쓴다 — 일부
                # 테스트가 db.local_now()를 정해진 시각 시퀀스로 모킹해두는데, 여기서 한 번 더
                # 부르면 그 시퀀스를 예상보다 빨리 소진시켜 poll_time 자체가 어긋난다.
                db.record_rate_limiter_status(
                    conn, poll_time, rest_client.rate_limit_backoff_multiplier, overrun_seconds,
                )
        except Exception:
            logger.warning("레이트리밋 근접도 기록 실패", exc_info=True)
        await asyncio.sleep(delay)


def _atm_liquidity_window(strikes: frozenset[float], each_side: int) -> list[float]:
    """
    입력: 구독 매니저의 ATM±STRIKES_EACH_SIDE 전체 행사가 집합(WS 구독 범위, 2026-07-10부터
         ATM±2=5개), 유동성 지표에 쓸 편측 개수(LIQUIDITY_ATM_EACH_SIDE=2 — 현재는 WS 구독
         범위와 폭이 같아 사실상 전체를 그대로 씀).
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


def _parse_asking_price_leg(resp: dict) -> dict | None:
    """
    입력: KISRestClient.get_asking_price() 응답(output1=현재가/누적거래량, output2=5단계 호가).
    계산: %스프레드(호가잔량 포함)와 누적거래량을 서로 독립적으로 파싱한다 — Cao & Wei(2010)가
         옵션은 만기·머니니스에 따라 달러 스프레드가 기계적으로 달라지므로 유동성 지표로 부적합
         하다고 지적한 근거를 따라 %스프레드를 쓴다.
    해석: 원래는 호가(ask1/bid1)가 없어 %스프레드를 못 구하면 acml_vol이 이미 찍혀 있어도 레그
         전체를 버렸다 — 2026-07-10 위클리(목)의 누적거래량이 매 사이클 0으로만 나와 조사한
         결과, 얇은 신규 위클리 종목은 순간적으로 양쪽 호가가 비는 경우가 잦아(체결은 있었지만
         지금 이 순간 MM 호가가 없는 상태) 그 레그의 실제 누적거래량까지 통째로 누락되고
         있었다. 스프레드/깊이와 거래량을 독립적으로 얻어, 호가가 없어도 거래량만은 살린다.
    실패 조건: 스프레드/깊이/거래량 셋 다 파싱 불가(필드 누락·숫자 변환 실패, 양쪽 호가 모두
              비어 mid<=0 등)면 None — 이번 레그에서 아무 것도 못 건진 경우만 집계에서 제외.
    """
    output1 = resp.get("output1") or {}
    output2 = resp.get("output2") or {}

    volume: float | None
    try:
        volume = float(output1["acml_vol"])
    except (KeyError, ValueError, TypeError):
        volume = None

    spread_pct: float | None = None
    depth: float | None = None
    try:
        ask1 = float(output2["futs_askp1"])
        bid1 = float(output2["futs_bidp1"])
        ask_qty = float(output2["askp_rsqn1"])
        bid_qty = float(output2["bidp_rsqn1"])
        mid = (ask1 + bid1) / 2
        if mid > 0:
            spread_pct = (ask1 - bid1) / mid
            depth = ask_qty + bid_qty
    except (KeyError, ValueError, TypeError):
        pass

    if volume is None and spread_pct is None:
        return None
    return {"spread_pct": spread_pct, "depth": depth, "volume": volume}


async def poll_expiry_liquidity(
    rest_client: KISRestClient,
    books: list[tuple[RollingSubscriptionManager, str]],
    master: IndexDerivativesMaster,
    underlying: str = UNDERLYING,
    interval_seconds: float = EXPIRY_LIQUIDITY_POLL_INTERVAL_SECONDS,
    startup_offset_seconds: float = 0.0,
) -> None:
    """
    입력: REST 클라이언트, (구독 매니저, series) 튜플 목록(먼슬리 "regular" + 위클리(월)
         "weekly_mon" + 위클리(목) "weekly_thu", 2026-07-10 위클리 분리),
         종목코드 마스터, 기동 시 최초 사이클을 지연시킬 초(startup_offset_seconds — 2026-07-09
         추가: poll_option_chain과 동시에 기동하면 두 폴러의 정규 사이클이 계속 같은 순간에
         겹쳐 공유 레이트리미터 큐가 길어지므로, 오프셋으로 사이클 시작 시각을 어긋나게 둔다).
    계산: 북마다 ATM±2(_atm_liquidity_window) 구간의 콜/풋 각각에 get_asking_price()를 호출해
         %스프레드·깊이·거래량을 집계하고, 만기일은 ATM 종목 1건만 get_quote()로 별도 확인해
         (_parse_option_quote 재사용) expiry_liquidity_1m에 적재한다. 만기 확인용 get_quote()는
         북당 사이클당 1건뿐이라 REST 부하에 미치는 영향은 무시할 만하다. poll_option_chain과
         동일하게 절대시각 고정 틱(next_tick)으로 다음 사이클을 예약해 사이클 소요시간에 따라
         실제 주기가 매번 밀리지 않게 한다.
    실패 조건: 개별 레그 조회/파싱 실패는 건너뛰고 나머지로 계속 집계한다. 유효한 레그가 하나도
              없거나 만기를 확인하지 못하면 그 북은 이번 사이클을 건너뛴다. 모든 북에 구독 행사가가
              없으면(기동 초입) 2초 뒤 재확인.
    로그(2026-07-20): REST 조회 실패는 poll_option_chain과 동일하게 `_log_kis_call_failure`로
              응답 바디를 남기고 `warning_throttle`로 반복을 억제한다 — 만기확인용 get_quote()
              실패는 이전엔 아예 로그도 안 남기고 조용히 삼켰다(원인 추적 불가능한 사각지대였음).
    """
    if startup_offset_seconds > 0:
        await asyncio.sleep(startup_offset_seconds)

    next_tick: float | None = None
    warning_throttle = WarningThrottle(logger, window_seconds=WARNING_THROTTLE_WINDOW_SECONDS)
    while True:
        poll_time = db.local_now().replace(second=0, microsecond=0)
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
                except Exception as exc:
                    _log_kis_call_failure(
                        f"만기 유동성 만기확인 조회 실패: {anchor_symbol}", exc,
                        throttle=warning_throttle, category="expiry_liquidity_anchor_fetch_failure",
                    )
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
                    except Exception as exc:
                        _log_kis_call_failure(
                            f"만기 유동성 폴링 실패: {symbol}", exc,
                            throttle=warning_throttle, category="expiry_liquidity_leg_fetch_failure",
                        )
                        continue
                    parsed_leg = _parse_asking_price_leg(resp)
                    if parsed_leg is None:
                        continue
                    if parsed_leg["spread_pct"] is not None:
                        spread_values.append(parsed_leg["spread_pct"])
                    if parsed_leg["depth"] is not None:
                        depth_total += parsed_leg["depth"]
                    if parsed_leg["volume"] is not None:
                        volume_total += parsed_leg["volume"]

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
            next_tick = None  # 구독 전이므로 아직 고정 스케줄을 시작하지 않음
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

        loop_now = asyncio.get_running_loop().time()
        next_tick = interval_seconds + (loop_now if next_tick is None else next_tick)
        delay = next_tick - loop_now
        if delay < 0:
            logger.warning(
                "만기 유동성 폴링 사이클이 주기(%.0f초)를 초과해 스케줄이 %.1f초 밀렸습니다 — 이번 틱은 즉시 재기준",
                interval_seconds, -delay,
            )
            next_tick = loop_now
            delay = 0.0
        await asyncio.sleep(delay)


def _parse_overseas_future_last_price(resp: dict) -> float | None:
    """입력: get_overseas_future_price() 응답. 계산: output1.last_price를 float로(KIS 필드는
    앞에 공백 패딩이 있으나 float()가 알아서 무시한다). 실패 조건: 필드 없음/변환 불가면 None."""
    try:
        return float(resp["output1"]["last_price"])
    except (KeyError, ValueError, TypeError):
        return None


def _parse_overseas_daily_last_price(resp: dict) -> float | None:
    """입력: get_overseas_daily_chartprice() 응답(국채구분 I/환율구분 X 등 공통 스키마). 계산:
    output1.ovrs_nmix_prpr(최신 종가 — I구분이면 수익률%, X구분이면 환율)를 float로. 실패 조건:
    필드 없음/변환 불가면 None."""
    try:
        return float(resp["output1"]["ovrs_nmix_prpr"])
    except (KeyError, ValueError, TypeError):
        return None


def _log_kis_call_failure(
    message: str,
    exc: Exception,
    *,
    throttle: WarningThrottle | None = None,
    category: str | None = None,
) -> None:
    """
    입력: 로그 메시지, 발생한 예외, (선택) 반복 경고 억제용 WarningThrottle과 그 category.
    계산: httpx.HTTPStatusError는 KIS의 실제 에러 코드/메시지(rt_cd/msg_cd/msg1)가 응답 바디에
         있는데, raise_for_status()가 만드는 예외 메시지 자체엔 그게 안 실려 그냥 재로깅하면
         "Server error 500"만 남고 원인(레이트리밋/계좌 미신청/일시 장애 등)을 구분할 수 없다
         (2026-07-10 CBOT 신청 후에도 ZN 조회가 500을 반복해 원인 확인 중 필요해짐) — 바디
         텍스트를 함께 남긴다.
         2026-07-20: `_collect_option_chain_cycle`의 레그별 조회 실패처럼 한 사이클 안에서 레그마다
         (최대 수십 건) 반복 재발할 수 있는 호출측은 throttle/category를 함께 넘기면 §5-5와 동일한
         패턴(WarningThrottle)으로 억제된다. 매크로 스냅샷처럼 사이클당 호출이 1건뿐이라 반복
         스팸 우려가 없는 호출측은 throttle을 안 넘기면 기존처럼 즉시 로깅된다.
    실패 조건: 없음 — 로깅 자체는 항상 성공한다고 가정.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        fmt, args = "%s — %s", (message, exc.response.text)
    else:
        fmt, args = "%s", (message,)
    if throttle is not None and category is not None:
        throttle.warning(category, fmt, *args, exc_info=True)
    else:
        logger.warning(fmt, *args, exc_info=True)


async def _collect_macro_snapshot_cycle(
    rest_client: KISRestClient, overseas_master: OverseasFutureMaster, poll_time: datetime
) -> dict | None:
    """
    입력: REST 클라이언트, 해외선물 종목코드 마스터, 폴링 시각(분 단위로 자름).
    계산: VIX 선물(CBOE VX) 근월·차근월, USDCNH 선물(HKEx CNH) 근월 현재가를 조회해 콘탱고/
         백워데이션(vix_term_structure = vix_next/vix_front - 1)을 계산하고, US10Y·USDKRW는 일봉
         API(해외주식 종목_지수_환율기간별시세, 국채구분 I / 환율구분 X)로 최신 종가를, ZN·ES
         선물(둘 다 CME 상장) 근월물 현재가로 5분 주기 급변 감지용 값을, MOVE(ICE BofA MOVE
         Index)는 yfinance 전용으로 함께 담는다.
         - ZN·ES: CME 계열 실시간시세는 KIS 유료 항목(2026-07-20 HTS [7936] 확인: 월 228.8불,
           ZN은 CME|CBOT·ES는 CME|CME 서브거래소로 별개 구독)이라 모의투자 개발 단계에서는
           미구독 상태다 — KIS 조회가 안 되면(마스터 미매핑·EGW00552 등)
           mahdi/data/yfinance_fallback.py로 대신 채우고, *_source 필드("kis"|"yfinance_fallback"|
           None)로 실제 출처를 구분한다.
         - USDKRW: 해외선물옵션이 아니라 해외주식 도메인(inquire-daily-chartprice)이라 애초에
           CBOT 같은 SUB거래소 신청 게이트가 없다 — US10Y와 동일하게 계좌 제약 없이 무료로 얻는다.
         - MOVE: 장외 파생 인덱스라 KIS 해외선물옵션 마스터파일에 상품 자체가 없음 — KIS 경로가
           없으므로 처음부터 yfinance_fallback만 시도한다.
         종목코드를 마스터에서 못 찾거나 개별 조회가 실패해도 나머지 필드는 계속 채운다(부분 실패
         허용).
    실패 조건: vix_front/vix_next/usdcnh 셋 다 실패하면(레이트리밋 버스트 등) None을 반환해
              호출측이 이번 사이클 적재를 건너뛰게 한다 — 나머지 필드만 성공한 상태로 5분 행을
              남기는 건 의미가 없다.
    """
    vix_front_code, vix_next_code = overseas_master.front_two_codes(tr_codes.OVERSEAS_FUTURE_PRODUCT_VIX)
    cnh_front_code, _ = overseas_master.front_two_codes(tr_codes.OVERSEAS_FUTURE_PRODUCT_CNH)
    zn_front_code, _ = overseas_master.front_two_codes(tr_codes.OVERSEAS_FUTURE_PRODUCT_ZN)
    es_front_code, _ = overseas_master.front_two_codes(tr_codes.OVERSEAS_FUTURE_PRODUCT_ES)

    vix_front = vix_next = usdcnh = None
    if vix_front_code is not None:
        try:
            vix_front = _parse_overseas_future_last_price(
                await asyncio.to_thread(rest_client.get_overseas_future_price, vix_front_code)
            )
        except Exception as exc:
            _log_kis_call_failure(f"VIX 근월물 조회 실패: {vix_front_code}", exc)
    if vix_next_code is not None:
        try:
            vix_next = _parse_overseas_future_last_price(
                await asyncio.to_thread(rest_client.get_overseas_future_price, vix_next_code)
            )
        except Exception as exc:
            _log_kis_call_failure(f"VIX 차근월물 조회 실패: {vix_next_code}", exc)
    if cnh_front_code is not None:
        try:
            usdcnh = _parse_overseas_future_last_price(
                await asyncio.to_thread(rest_client.get_overseas_future_price, cnh_front_code)
            )
        except Exception as exc:
            _log_kis_call_failure(f"USDCNH 선물 조회 실패: {cnh_front_code}", exc)

    if vix_front is None and vix_next is None and usdcnh is None:
        return None

    date_to = poll_time.strftime("%Y%m%d")
    date_from = (poll_time - timedelta(days=US10Y_LOOKBACK_DAYS)).strftime("%Y%m%d")

    us10y_yield = None
    try:
        us10y_yield = _parse_overseas_daily_last_price(
            await asyncio.to_thread(
                rest_client.get_overseas_daily_chartprice,
                tr_codes.FID_MRKT_DIV_OVERSEAS_TREASURY,
                tr_codes.FID_INPUT_ISCD_US10Y,
                date_from,
                date_to,
            )
        )
    except Exception as exc:
        _log_kis_call_failure("US10Y 일봉 조회 실패", exc)

    usdkrw = None
    try:
        usdkrw = _parse_overseas_daily_last_price(
            await asyncio.to_thread(
                rest_client.get_overseas_daily_chartprice,
                tr_codes.FID_MRKT_DIV_OVERSEAS_FX,
                tr_codes.FID_INPUT_ISCD_USDKRW,
                date_from,
                date_to,
            )
        )
    except Exception as exc:
        _log_kis_call_failure("USDKRW 일봉 조회 실패", exc)

    zn_front = None
    zn_front_source = None
    if zn_front_code is not None:
        try:
            zn_front = _parse_overseas_future_last_price(
                await asyncio.to_thread(rest_client.get_overseas_future_price, zn_front_code)
            )
            if zn_front is not None:
                zn_front_source = "kis"
        except Exception as exc:
            _log_kis_call_failure(f"ZN(10년 국채선물) 근월물 조회 실패: {zn_front_code}", exc)

    if zn_front is None:
        # CME|CBOT 실시간시세는 KIS 유료 항목(HTS [7936] 확인: 월 228.8불)이라 모의투자 개발
        # 단계에서는 미구독 상태다 — KIS 조회가 안 됐을 때만(마스터 미매핑이든 호출 실패든) 여기서
        # yfinance 폴백을 시도한다. 폴백도 실패하면 zn_front_source는 None으로 남는다.
        zn_front = await asyncio.to_thread(yfinance_fallback.fetch_last_close, yfinance_fallback.ZN_FALLBACK_SYMBOL)
        if zn_front is not None:
            zn_front_source = "yfinance_fallback"

    es_front = None
    es_front_source = None
    if es_front_code is not None:
        try:
            es_front = _parse_overseas_future_last_price(
                await asyncio.to_thread(rest_client.get_overseas_future_price, es_front_code)
            )
            if es_front is not None:
                es_front_source = "kis"
        except Exception as exc:
            _log_kis_call_failure(f"ES(E-mini S&P500) 근월물 조회 실패: {es_front_code}", exc)

    if es_front is None:
        # CME|CME(ES) 실시간시세도 ZN(CME|CBOT)과 마찬가지로 KIS 유료 항목 — 미구독 상태에서는
        # yfinance 폴백으로 대신 채운다.
        es_front = await asyncio.to_thread(yfinance_fallback.fetch_last_close, yfinance_fallback.ES_FALLBACK_SYMBOL)
        if es_front is not None:
            es_front_source = "yfinance_fallback"

    # MOVE(ICE BofA MOVE Index)는 장외 파생 인덱스라 KIS 해외선물옵션 마스터파일에 상품 자체가
    # 없다 — KIS 시도 없이 처음부터 yfinance 폴백만 쓴다.
    move_index = await asyncio.to_thread(yfinance_fallback.fetch_last_close, yfinance_fallback.MOVE_FALLBACK_SYMBOL)
    move_index_source = "yfinance_fallback" if move_index is not None else None

    vix_term_structure = (vix_next / vix_front - 1) if (vix_front and vix_next) else None

    return {
        "timestamp": poll_time,
        "vix_front": vix_front,
        "vix_next": vix_next,
        "vix_term_structure": vix_term_structure,
        "usdcnh": usdcnh,
        "us10y_yield": us10y_yield,
        "usdkrw": usdkrw,
        "zn_front": zn_front,
        "zn_front_source": zn_front_source,
        "es_front": es_front,
        "es_front_source": es_front_source,
        "move_index": move_index,
        "move_index_source": move_index_source,
        "quality_flag": 0 if (vix_front is not None and vix_next is not None and usdcnh is not None) else 1,
    }


async def poll_macro_snapshot(
    rest_client: KISRestClient,
    overseas_master: OverseasFutureMaster,
    interval_seconds: float = MACRO_SNAPSHOT_POLL_INTERVAL_SECONDS,
) -> None:
    """
    입력: REST 클라이언트, 해외선물 종목코드 마스터(main() 기동 시 1회 로드).
    계산: Cross-asset stress 원시값(VIX 기간구조·USDCNH·US10Y, v6 §7.3)을 5분 주기로
         macro_snapshot_5m에 적재한다. poll_option_chain/poll_investor_flow와 동일하게 절대시각
         고정 틱(next_tick)으로 다음 사이클을 예약해 사이클 소요시간에 따라 실제 주기가 밀리지
         않게 한다.
    실패 조건: 이번 사이클 전체가 실패하면(_collect_macro_snapshot_cycle이 None) 적재를
              건너뛰고 다음 정규 사이클을 기다린다 — 재시도 백오프는 두지 않는다(사이클당
              REST 호출이 4건뿐이라 다른 폴러만큼 레이트리밋에 취약하지 않음).
    알림(2026-07-19, §5-4; 2026-07-20 문구 갱신): 적재에 성공한 첫 사이클에서 zn_front가 그때도
              None이면(KIS 미구독 + yfinance 폴백까지 실패 — 둘 다 안 되는 경우만 해당,
              yfinance_fallback.py 참고) Slack으로 한 번만 알린다. 5분마다 매번 알리면 하루 종일
              (정규장 기준 최대 78회) 반복 경고가 되므로, 이 프로세스 실행당(=거래일당, 매일
              재시작되므로) 최초 1회만 보낸다.
    알림(2026-07-21 추가): INSERT 자체가 MACRO_SNAPSHOT_INSERT_FAILURE_ALERT_STREAK회 연속
              실패하면(2026-07-21 실측: 마이그레이션 라이브 미적용으로 UndefinedColumn이 종일
              반복됐는데 로그에만 WARNING으로 남고 아무도 알아채지 못함) Slack으로 알린다. 이후
              한 번이라도 성공하면 복구 알림을 보내고 스트릭/알림 상태를 리셋 — gap_alerted
              (poll_option_chain)와 동일한 "지속되면 알리고, 회복되면 알린다" 패턴.
    """
    next_tick: float | None = None
    cbot_alert_sent = False
    insert_failure_streak = 0
    insert_failure_alerted = False
    while True:
        poll_time = db.local_now().replace(second=0, microsecond=0)
        row = await _collect_macro_snapshot_cycle(rest_client, overseas_master, poll_time)

        if row is None:
            logger.warning("매크로 스냅샷 폴링 전체 실패 — 이번 사이클 건너뜀")
        else:
            insert_ok = True
            with db.get_connection() as conn:
                try:
                    db.insert_macro_snapshot_5m(conn, row)
                except Exception:
                    logger.warning("매크로 스냅샷 적재 실패", exc_info=True)
                    conn.rollback()
                    insert_ok = False
            if insert_ok:
                if insert_failure_alerted:
                    notify.notify("매크로 스냅샷(macro_snapshot_5m) 적재 복구됨 — 정상 적재 재개.", "INFO")
                    insert_failure_alerted = False
                insert_failure_streak = 0
            else:
                insert_failure_streak += 1
                if insert_failure_streak >= MACRO_SNAPSHOT_INSERT_FAILURE_ALERT_STREAK and not insert_failure_alerted:
                    insert_failure_alerted = True
                    notify.notify(
                        f"매크로 스냅샷(macro_snapshot_5m) 적재가 {insert_failure_streak}회 연속 실패했습니다 "
                        "— db/migrations 라이브 미적용(스키마 불일치) 등 DB 문제일 수 있습니다. 로그 확인 필요.",
                        "WARNING",
                    )
            if row["zn_front"] is None and not cbot_alert_sent:
                cbot_alert_sent = True
                notify.notify(
                    "ZN(10년 국채선물) 데이터를 KIS·yfinance 폴백 양쪽 모두에서 가져오지 못했습니다 "
                    "— zn_front가 계속 NULL. 네트워크 상태 또는 yfinance 응답을 확인해주세요.",
                    "WARNING",
                )

        loop_now = asyncio.get_running_loop().time()
        next_tick = interval_seconds + (loop_now if next_tick is None else next_tick)
        delay = next_tick - loop_now
        if delay < 0:
            logger.warning(
                "매크로 스냅샷 폴링 사이클이 주기(%.0f초)를 초과해 스케줄이 %.1f초 밀렸습니다 — 이번 틱은 즉시 재기준",
                interval_seconds, -delay,
            )
            next_tick = loop_now
            delay = 0.0
        await asyncio.sleep(delay)


_INVESTOR_FLOW_SECTORS = (
    tr_codes.FID_INVESTOR_FLOW_FUTURES,
    tr_codes.FID_INVESTOR_FLOW_CALL_OPTION,
    tr_codes.FID_INVESTOR_FLOW_PUT_OPTION,
)


async def _collect_investor_flow_cycle(
    rest_client: KISRestClient, warning_throttle: WarningThrottle
) -> tuple[float, float, float, bool]:
    """
    입력/계산: poll_investor_flow 한 사이클분 — 선물/콜/풋 세 세그먼트를 조회해 합산한다.
         warning_throttle은 poll_investor_flow가 사이클 전체에서 공유하는 인스턴스를 그대로
         전달받는다(§5-5와 동일 패턴, 2026-07-20).
    실패 조건: 세그먼트 하나 실패는 건너뛰고 나머지로 합산 계속. got_any=False는 셋 다 실패했음을
              뜻한다 — 호출측이 재시도 여부를 판단하는 데 쓴다.
    로그(2026-07-20): 조회 실패는 `_log_kis_call_failure`로 응답 바디(레이트리밋 등 KIS 원인
              코드)를 함께 남기고, `warning_throttle`로 반복을 억제한다(poll_option_chain과 동일).
    """
    foreign_total = 0.0
    institution_total = 0.0
    individual_total = 0.0
    got_any = False

    for sector in _INVESTOR_FLOW_SECTORS:
        try:
            resp = await asyncio.to_thread(rest_client.get_investor_flow, tr_codes.FID_MRKT_DIV_DERIVATIVES, sector)
            output = resp.get("output") or []
            row = output[0] if isinstance(output, list) else output
            foreign_total += float(row["frgn_ntby_tr_pbmn"])
            institution_total += float(row["orgn_ntby_tr_pbmn"])
            individual_total += float(row["prsn_ntby_tr_pbmn"])
            got_any = True
        except Exception as exc:
            _log_kis_call_failure(
                f"투자자 수급 폴링 실패: {sector}", exc,
                throttle=warning_throttle, category="investor_flow_segment_fetch_failure",
            )
            continue

    return foreign_total, institution_total, individual_total, got_any


async def poll_investor_flow(
    rest_client: KISRestClient,
    underlying: str = UNDERLYING,
    interval_seconds: float = OPTION_CHAIN_POLL_INTERVAL_SECONDS,
    retry_backoff_seconds: float = CYCLE_RETRY_BACKOFF_SECONDS,
) -> None:
    """
    입력: REST 클라이언트, 기초자산 라벨.
    계산: KOSPI200 파생상품시장(선물+콜옵션+풋옵션) 세 세그먼트의 투자자별(외국인/기관계/개인)
         순매수 거래대금을 조회해 합산한 뒤 investor_flow_1m에 적재한다. "시장별 투자자매매동향
         (시세)"는 세션 누적치라, 이 값은 "1분간의 변화량"이 아니라 "그 시점까지의 누적 수급
         우위" 스냅샷이다. get_quote()류와 마찬가지로 동기 호출이라 asyncio.to_thread로 실행.
         세 세그먼트가 모두 실패하면(레이트리밋 버스트 등) retry_backoff_seconds 뒤 한 번만
         재시도한다.
    실패 조건: 세그먼트 하나 실패는 건너뛰고 나머지로 합산 계속 — 셋 다 실패하고 재시도까지
              실패하면 이번 사이클은 적재를 건너뛴다(마지막 성공값이 다음 사이클까지 화면에
              남는다).
    스케줄: poll_option_chain과 동일하게 절대시각 고정 틱(next_tick)으로 다음 사이클을 예약한다
           (2026-07-09 — "작업 후 sleep" 누적 드리프트로 poll_time이 분 경계를 건너뛰는 것을 방지).
    """
    next_tick: float | None = None
    warning_throttle = WarningThrottle(logger, window_seconds=WARNING_THROTTLE_WINDOW_SECONDS)
    while True:
        foreign_total, institution_total, individual_total, got_any = await _collect_investor_flow_cycle(
            rest_client, warning_throttle
        )

        if not got_any:
            logger.warning("투자자 수급 폴링 전체 실패 — %.0f초 후 재시도", retry_backoff_seconds)
            await asyncio.sleep(retry_backoff_seconds)
            foreign_total, institution_total, individual_total, got_any = await _collect_investor_flow_cycle(
                rest_client, warning_throttle
            )
            if not got_any:
                logger.warning("투자자 수급 폴링 재시도도 실패 — 이번 사이클 포기")

        if got_any:
            poll_time = db.local_now().replace(second=0, microsecond=0)
            with db.get_connection() as conn:
                db.insert_investor_flow(
                    conn, poll_time, underlying,
                    foreign_net=foreign_total, institution_net=institution_total, individual_net=individual_total,
                )

        loop_now = asyncio.get_running_loop().time()
        next_tick = interval_seconds + (loop_now if next_tick is None else next_tick)
        delay = next_tick - loop_now
        if delay < 0:
            logger.warning(
                "투자자 수급 폴링 사이클이 주기(%.0f초)를 초과해 스케줄이 %.1f초 밀렸습니다 — 이번 틱은 즉시 재기준",
                interval_seconds, -delay,
            )
            next_tick = loop_now
            delay = 0.0
        await asyncio.sleep(delay)


def _configure_logging() -> None:
    """
    계산: 콘솔(stdout)과 로테이션 파일(logs/observation_loop.log) 양쪽에 동시에 로깅한다.
         2026-07-19(§5-5) — 예전엔 scripts/start_mahdi_premarket.bat가 stdout을
         `>> logs\\observation_loop.log 2>&1`로 그대로 리다이렉트해서 로테이션이 전혀 없었고
         (105MB까지 무한 누적 확인), Python 로깅은 콘솔에만 나갔다. 이제 Python 로깅이 이 파일을
         직접 소유해 파일당 LOG_MAX_BYTES(10MB), 최근 LOG_BACKUP_COUNT(10)개(최대 약 110MB)만
         유지하고 그 이전은 자동 삭제한다 — 배치파일도 함께 고쳐 stdout 리다이렉트를 없애고
         (콘솔 창엔 여전히 실시간으로 보임) stderr만 별도의 회전 없는 크래시 전용 로그로 남긴다
         (로깅 설정 자체가 안 끝난 극초반 크래시까지 잡기 위함 — 흔치 않은 이벤트라 회전 불필요).
    실패 조건: logs/ 디렉터리가 없으면 미리 만든다(최초 실행 시).
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
    console_handler = logging.StreamHandler(sys.stdout)
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])


def _log_startup_gap_since_last_run() -> None:
    """
    계산: LAST_START_MARKER_FILE에 남아있는 직전 정상 기동 시각과 현재 시각의 차이를 INFO로
         남긴 뒤, 이번 기동 시각으로 마커를 갱신한다(2026-07-20 고도화 항목). 예약된 장전 기동이
         하루 이상 건너뛰거나(Docker 미기동으로 조용히 실패하는 경우 등), 반대로 예상보다 훨씬
         일찍(수동 재시작 등) 다시 떴는지를 observation_loop.log 한 줄로 바로 알아챌 수 있다.
    실패 조건: 마커 파일이 없으면(최초 실행) 또는 파싱 실패하면 비교 없이 건너뛰고, 그래도 마커
              갱신은 시도한다. 마커 읽기/쓰기 자체가 실패해도(권한 등) 예외를 삼키고 로그만
              남긴다 — 이 기능 하나 때문에 관측 루프 기동 전체가 죽으면 안 된다.
    """
    try:
        if LAST_START_MARKER_FILE.exists():
            last = datetime.fromisoformat(LAST_START_MARKER_FILE.read_text(encoding="utf-8").strip())
            gap_hours = (db.local_now() - last).total_seconds() / 3600
            logger.info(
                "직전 정상 기동: %s (%.1f시간 전)", last.strftime("%Y-%m-%d %H:%M:%S"), gap_hours,
            )
        else:
            logger.info("직전 정상 기동 기록 없음(최초 실행 또는 마커 파일 삭제됨)")
    except Exception:
        logger.warning("직전 기동 기록 확인 실패", exc_info=True)

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        LAST_START_MARKER_FILE.write_text(db.local_now().isoformat(), encoding="utf-8")
    except Exception:
        logger.warning("직전 기동 기록 저장 실패", exc_info=True)


async def main() -> None:
    _configure_logging()
    _log_startup_gap_since_last_run()
    kis_settings = get_kis_settings()
    get_db_settings()  # 조기 검증(연결 문자열 구성 오류를 기동 시점에 노출)

    # 종목코드 마스터파일은 매일 갱신되므로 기동 시 1회 내려받아 최근월물/행사가↔단축코드
    # 매핑을 확정한다 (모의투자 REST에는 옵션 체인 전체를 한 번에 주는 API가 없어 필수).
    master = load_index_derivatives_master(SYMBOL_MASTER_CACHE_DIR)
    futures_symbol = master.front_month_future_code("KOSPI200")
    if futures_symbol is None:
        raise RuntimeError("종목코드 마스터파일에서 KOSPI200 선물 최근월물을 찾지 못했습니다")

    # 해외선물 마스터(VX/CNH 근월·차근월)는 국내 마스터와 별개 파일 — 하나가 실패해도 다른
    # 하나는 관측을 계속할 수 있어야 하므로 별도로 로드한다(2026-07-10 신규, Cross-asset
    # stress §7.3). 다운로드 실패 시(네트워크 등) 매크로 폴링만 건너뛰고 나머지 관측은 계속한다.
    try:
        overseas_future_master = load_overseas_future_master(OVERSEAS_FUTURE_MASTER_CACHE_DIR)
    except Exception:
        logger.warning("해외선물 종목코드 마스터 로드 실패 — 매크로 스냅샷 폴링을 건너뜁니다", exc_info=True)
        overseas_future_master = None

    token_daemon = TokenDaemon(kis_settings)
    rest_client = KISRestClient(kis_settings, token_daemon)
    approval_key = ApprovalKeyIssuer(kis_settings).issue()
    regime_state_machine = RegimeStateMachine(underlying=UNDERLYING, futures_symbol=futures_symbol)

    # 시세(H0IOCNT0/H0IOASP0)는 계좌 무관 공개 데이터라 모의투자 전용 도메인이 없다 —
    # is_mock 여부와 상관없이 MARKET_DATA_WS_DOMAIN(실전 도메인) 하나로 접속한다.
    async with websockets.connect(tr_codes.MARKET_DATA_WS_DOMAIN) as raw_ws:
        ws_client = KISWebSocketClient(approval_key=approval_key, connection=_WebsocketsAdapter(raw_ws))
        # 먼슬리(정규 월물)·위클리(월)·위클리(목) 세 북을 별도 매니저로 동시에 굴린다(2026-07-06
        # 먼슬리+위클리 2북 도입, 2026-07-10 위클리를 월/목 두 상품으로 분리) — 3북×ATM±2(10슬롯)
        # + 1(선물) = 31 / MAX_SUBSCRIPTIONS(41). ATM±3(14슬롯)을 유지하면 3북 합계가 43으로
        # 한도를 넘겨(RESEARCH_EXPIRY_SELECTION_v1.md §3.1이 미리 지적한 트레이드오프) 세 북
        # 모두 ATM±2로 축소하기로 결정(사용자 확인, [[DECISION_LOG]] 참고).
        monthly_manager = RollingSubscriptionManager(
            ws_client,
            tr_id=tr_codes.WS_TR_OPTION_CONTRACT,
            strike_interval=KOSPI200_OPTION_STRIKE_INTERVAL,
            strikes_each_side=STRIKES_EACH_SIDE,
            symbol_formatter=lambda strike, opt: master.option_symbol(opt, strike, underlying="KOSPI200"),
        )
        weekly_mon_manager = RollingSubscriptionManager(
            ws_client,
            tr_id=tr_codes.WS_TR_OPTION_CONTRACT,
            strike_interval=KOSPI200_OPTION_STRIKE_INTERVAL,
            strikes_each_side=STRIKES_EACH_SIDE,
            symbol_formatter=lambda strike, opt: master.option_symbol(
                opt, strike, underlying="KOSPI200", series="weekly_mon"
            ),
        )
        weekly_thu_manager = RollingSubscriptionManager(
            ws_client,
            tr_id=tr_codes.WS_TR_OPTION_CONTRACT,
            strike_interval=KOSPI200_OPTION_STRIKE_INTERVAL,
            strikes_each_side=STRIKES_EACH_SIDE,
            symbol_formatter=lambda strike, opt: master.option_symbol(
                opt, strike, underlying="KOSPI200", series="weekly_thu"
            ),
        )
        books = [
            (monthly_manager, "regular"),
            (weekly_mon_manager, "weekly_mon"),
            (weekly_thu_manager, "weekly_thu"),
        ]
        tasks = [
            run_observation_loop_forever(
                ws_client,
                [monthly_manager, weekly_mon_manager, weekly_thu_manager],
                rest_client,
                futures_symbol=futures_symbol,
                regime_state_machine=regime_state_machine,
                approval_key=approval_key,
            ),
            poll_option_chain(rest_client, books, master, regime_state_machine=regime_state_machine),
            poll_expiry_liquidity(
                rest_client, books, master, startup_offset_seconds=EXPIRY_LIQUIDITY_STARTUP_OFFSET_SECONDS
            ),
            poll_investor_flow(rest_client),
        ]
        if overseas_future_master is not None:
            tasks.append(poll_macro_snapshot(rest_client, overseas_future_master))
        # 2026-07-19(§5-4) — .env에 토큰/채널이 설정된 경우에만 워커를 띄운다. notify.notify()가
        # 이미 미설정 시 조용히 무시하지만, 워커까지 안 띄우면 알림 기능이 꺼진 상태에서 불필요한
        # 태스크가 asyncio.gather에 남지 않는다.
        if get_slack_settings().is_configured:
            tasks.append(notify.run_slack_worker())
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Ctrl+C로 종료합니다.")
