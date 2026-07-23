"""KIS REST 클라이언트 — 옵션 체인 조회, 잔고 조회, 주문 제출 (모의/실전 겸용).

TR ID/경로 상수는 tr_codes.py 단일 소스를 사용한다.
"""

from __future__ import annotations

import logging
import threading
import time

import httpx

from mahdi.broker import tr_codes
from mahdi.broker.token_daemon import TokenDaemon
from mahdi.config.settings import KISSettings

logger = logging.getLogger("mahdi.broker.rest_client")

# 2026-07-08 실측: main.py의 옵션체인/수급/유동성 폴링 루프 3개가 동시에(asyncio.gather) 60초
# 주기로 REST를 호출하는데, 각 루프 내부는 순차 호출이라도 서로 다른 asyncio.to_thread 스레드가
# 겹치는 순간 KIS 앱키의 초당 호출 한도를 넘겨 500 Internal Server Error가 대량 발생함(정규장
# 405분 중 203분치 옵션체인 데이터가 통째로 유실됨을 DB로 확인). 당시 문서화된 모의투자 TPS
# 한도가 없어 보수적으로 2건/초(0.5초 간격)로 제한.
#
# 2026-07-20 재실측: 2건/초로도 부족함을 확인 — _collect_option_chain_cycle이 행사가마다
# 콜→풋 순서로 호출하는데, DB로 확인한 결과 콜은 거의 항상 성공(행사가당 18~19건/8분)하고
# 풋만 계속 500(행사가당 3건/8분)이 되는 정확한 교대 패턴이 5개 행사가 전부에서 동일하게
# 나타났다. 매 쌍의 두 번째 호출(0.5초 뒤)만 계속 걸리는 이 패턴은 KIS 모의투자의 실제 한도가
# 2건/초가 아니라 1건/초에 더 가깝다는 강한 정황이다 — 1건/초(1.0초 간격)로 상향한다.
# 사이클당 필요한 최대 호출(옵션체인 ~30 + 수급 3 = 33)도 33초면 끝나 60초 주기 안에 들어간다.
DEFAULT_MIN_REQUEST_INTERVAL_SECONDS = 1.0


class _RateLimiter:
    """여러 스레드(asyncio.to_thread)가 공유하는 최소 호출 간격 페이서.

    2026-07-20(고도화): 고정 간격 대신 적응형으로 개선했다 — KIS 모의투자의 실제 초당 호출
    한도는 문서화돼 있지 않고, 이미 2026-07-08(2건/초로 추정) → 2026-07-20(실측 결과 1건/초에
    더 가까움)로 한 번 틀렸던 적이 있다. 앞으로도 계좌/시간대별로 실제 한도가 달라질 가능성을
    고려해, 레이트리밋(500 + KIS 에러코드 EGW00201)이 감지되면 다음 호출부터 간격을 즉시
    넓히고(record_rate_limit_hit), 그 넓어진 간격에서 성공이 충분히 이어지면 서서히 기준
    간격(min_interval)까지만 되돌린다(record_success) — 기준 간격 밑으로는 절대 안 내려가고,
    무한정 넓어지지도 않도록 상한(_MAX_INTERVAL_MULTIPLIER배)을 둔다.

    락은 "다음 호출 가능 시각" 예약에만 쓰고 실제 대기(time.sleep)는 락 밖에서 하므로,
    대기 중인 스레드가 다른 스레드의 예약을 막지 않는다.
    """

    _BACKOFF_MULTIPLIER = 1.5  # 레이트리밋 감지될 때마다 현재 간격에 곱하는 값
    _MAX_INTERVAL_MULTIPLIER = 4.0  # 기준 간격(min_interval) 대비 최대 몇 배까지 늘어날 수 있는지
    # 2026-07-22 재조정 시도(운영점검보고서 §2-1): 임계값 20일 때는 4배(최대치)에서 1배로
    # 완전히 되돌아오는 데 성공 약 260건이 필요해(0.9배씩 13단계 × 20건) 20으로는 회복이 느려
    # 보였고, 그날 하루치(EGW00201 83건/14,852건, 스케줄 밀림 57건)를 근거로 8로 낮췄다.
    # 2026-07-23 재검토(운영점검보고서 §2-1): 8로 바꾼 첫 전체 거래일(임계값 20 그대로였던
    # 07-22와 동일 방법론으로 나란히 집계) 결과 EGW00201 비율(0.38%→0.48%)·스케줄 밀림
    # (57→83건)·평균 지연(10.7초→18.7초)·최대 지연(45.5초→76.2초)이 전부 악화됐다 — 임계값을
    # 너무 낮추면 백오프에서 너무 빨리 벗어나 다시 레이트리밋에 바로 부딪히는 "플래핑"이
    # 실제로 일어났을 가능성이 높다(당시 커밋 메시지에도 이 위험이 언급돼 있었음). 후속
    # 프로젝트 messiah(fuoption)도 같은 계약의 RateLimiter를 독립적으로 튜닝하며 기본값 20을
    # 그대로 유지하고 있어(src/messiah/broker/kis/rest_client.py) 원래 값으로 되돌린다. 이번엔
    # 아래 record_rate_limit_hit/record_success 로깅을 함께 추가해, 다음에 파라미터를 다시
    # 바꿀 때는 간접 증상(EGW00201 횟수)이 아니라 실제 배율 전이 로그로 검증할 수 있게 한다.
    _RECOVERY_SUCCESS_THRESHOLD = 20  # 이만큼 연속 성공하면 간격을 한 단계 되돌림
    _RECOVERY_FACTOR = 0.9  # 되돌릴 때 곱하는 축소 비율(급하게 되돌리지 않고 서서히)

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._current_interval = min_interval
        self._lock = threading.Lock()
        self._next_allowed = 0.0
        self._consecutive_successes = 0

    @property
    def current_multiplier(self) -> float:
        """계산: 현재 페이싱 간격이 기준 간격(min_interval)의 몇 배인지 — 1.0이면 백오프 없음,
        _MAX_INTERVAL_MULTIPLIER(4.0)에 가까울수록 레이트리밋에 강하게 걸려 있는 상태다.
        COCKPIT 헬스체크(§2-1 고도화 방안, "레이트리밋 근접도 배지")가 읽는 값."""
        if self._min_interval <= 0:
            return 1.0
        return self._current_interval / self._min_interval

    def wait(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_allowed)
            self._next_allowed = start + self._current_interval
        delay = start - now
        if delay > 0:
            time.sleep(delay)

    def record_rate_limit_hit(self) -> None:
        """레이트리밋 실패가 감지되면 호출 — 다음 wait()부터 넓어진 간격이 바로 적용된다.

        2026-07-23(운영점검보고서 §2-1 Fix#1): 배율 확대 시점마다 이전/이후 배율을 로깅한다 —
        지금까지는 EGW00201 발생 횟수·스케줄 밀림 같은 간접 증상으로만 백오프 상태를 추정할 수
        있었고, 그래서 07-22의 임계값 조정이 역효과였다는 것도 다음날 로그를 정밀분석해야만
        알 수 있었다. 이제는 이 로그 한 줄로 "지금 몇 배 백오프 중인지"를 바로 알 수 있다."""
        if self._min_interval <= 0:
            return
        with self._lock:
            self._consecutive_successes = 0
            before = self._current_interval
            max_interval = self._min_interval * self._MAX_INTERVAL_MULTIPLIER
            self._current_interval = min(
                max(self._current_interval, self._min_interval) * self._BACKOFF_MULTIPLIER, max_interval
            )
            after = self._current_interval
        if after != before:
            logger.info(
                "레이트리밋 백오프 확대: %.2fs -> %.2fs (기준 대비 %.2f배)",
                before, after, after / self._min_interval,
            )

    def record_success(self) -> None:
        """호출 성공마다 호출 — 넓어진 간격이 있을 때만 연속 성공을 세어 서서히 되돌린다.

        2026-07-23: 실제로 한 단계 되돌린 시점만 로깅한다(성공마다 찍으면 정상 상태에서도
        매 호출 로그가 남아 record_rate_limit_hit 로그가 파묻힌다)."""
        if self._current_interval <= self._min_interval:
            return
        with self._lock:
            if self._current_interval <= self._min_interval:
                return
            self._consecutive_successes += 1
            if self._consecutive_successes >= self._RECOVERY_SUCCESS_THRESHOLD:
                self._consecutive_successes = 0
                before = self._current_interval
                self._current_interval = max(self._current_interval * self._RECOVERY_FACTOR, self._min_interval)
                after = self._current_interval
            else:
                return
        logger.info(
            "레이트리밋 백오프 회복: %.2fs -> %.2fs (기준 대비 %.2f배)",
            before, after, after / self._min_interval,
        )


def _is_kis_rate_limit_error(exc: httpx.HTTPStatusError) -> bool:
    """
    계산: KIS가 초당 거래건수 초과 시 돌려주는 특정 에러코드(EGW00201)인지 확인한다(2026-07-20
         US10Y 조회 500 응답 바디에서 {"msg_cd":"EGW00201","msg1":"초당 거래건수를 초과하였습니다"}
         실측). 이 코드일 때만 백오프를 키운다 — 그 외 500(계좌 미승인, 존재하지 않는 종목 등)은
         페이싱과 무관한 원인이라 무분별하게 전체 호출을 느리게 만들면 안 된다.
    실패 조건: 응답 바디가 JSON이 아니거나 msg_cd가 없으면 False(레이트리밋 아님으로 취급).
    """
    try:
        return exc.response.json().get("msg_cd") == "EGW00201"
    except Exception:
        return False


class KISRestClient:
    def __init__(
        self,
        settings: KISSettings,
        token_daemon: TokenDaemon,
        client: httpx.Client | None = None,
        min_request_interval: float = DEFAULT_MIN_REQUEST_INTERVAL_SECONDS,
    ) -> None:
        self._settings = settings
        self._token_daemon = token_daemon
        self._client = client or httpx.Client(timeout=10.0)
        self._rate_limiter = _RateLimiter(min_request_interval)

    @property
    def rate_limit_backoff_multiplier(self) -> float:
        """현재 공유 레이트리미터의 배율(1.0=백오프 없음) — COCKPIT 헬스체크가 읽는 값."""
        return self._rate_limiter.current_multiplier

    @property
    def _domain(self) -> str:
        return tr_codes.VPS_REST_DOMAIN if self._settings.is_mock else tr_codes.REAL_REST_DOMAIN

    @property
    def _env_key(self) -> str:
        return "vps" if self._settings.is_mock else "real"

    def _headers(self, tr_id: str) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self._token_daemon.get_token()}",
            "appkey": self._settings.kis_app_key,
            "appsecret": self._settings.kis_app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }

    def _get(self, url: str, **kwargs) -> dict:
        """모든 REST GET 호출의 단일 진입점 — 실제 전송 직전에 _rate_limiter로 페이싱하고,
        결과에 따라 적응형 백오프 상태를 갱신한다(2026-07-20, _RateLimiter 참고)."""
        self._rate_limiter.wait()
        response = self._client.get(url, **kwargs)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if _is_kis_rate_limit_error(exc):
                self._rate_limiter.record_rate_limit_hit()
            raise
        self._rate_limiter.record_success()
        return response.json()

    def _post(self, url: str, **kwargs) -> dict:
        """모든 REST POST 호출의 단일 진입점 — GET과 동일한 공유 레이트리미터를 통과시킨다."""
        self._rate_limiter.wait()
        response = self._client.post(url, **kwargs)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if _is_kis_rate_limit_error(exc):
                self._rate_limiter.record_rate_limit_hit()
            raise
        self._rate_limiter.record_success()
        return response.json()

    def get_quote(self, symbol: str, market_div_code: str = tr_codes.FID_MRKT_DIV_INDEX_OPTION) -> dict:
        """
        단일 종목(선물 1건 또는 옵션 1건) 시세 조회 — "선물옵션 시세"(inquire-price).

        입력: 종목코드(단축코드), FID_COND_MRKT_DIV_CODE(F=지수선물, O=지수옵션 등).
        계산: PATH_FUTUREOPTION_QUOTE GET 호출.
        해석: 이 엔드포인트는 종목 1건 시세만 반환한다 — 모의투자에는 체인 전체를 한 번에
             반환하는 REST가 없으므로(전광판류는 실전 전용), 여러 행사가를 조회하려면 종목코드
             마스터파일 기준으로 이 호출을 반복해야 한다(아직 미구현 — KIS 종목코드 마스터파일
             연동 필요, github.com/koreainvestment/open-trading-api/tree/main/stocks_info).
        실패 조건: 4xx/5xx면 httpx.HTTPStatusError 그대로 전파 — 호출측이 재시도/알림 처리.
        """
        tr_id = tr_codes.TR_OPTION_QUOTE[self._env_key]
        return self._get(
            f"{self._domain}{tr_codes.PATH_FUTUREOPTION_QUOTE}",
            headers=self._headers(tr_id),
            params={"FID_COND_MRKT_DIV_CODE": market_div_code, "FID_INPUT_ISCD": symbol},
        )

    def get_asking_price(self, symbol: str, market_div_code: str = tr_codes.FID_MRKT_DIV_INDEX_OPTION) -> dict:
        """단일 종목 시세호가(5단계 매도/매수 호가) — "선물옵션 시세호가"(inquire-asking-price)."""
        tr_id = tr_codes.TR_OPTION_ASKING_PRICE[self._env_key]
        return self._get(
            f"{self._domain}{tr_codes.PATH_FUTUREOPTION_ASKING_PRICE}",
            headers=self._headers(tr_id),
            params={"FID_COND_MRKT_DIV_CODE": market_div_code, "FID_INPUT_ISCD": symbol},
        )

    def get_investor_flow(self, market_code: str, sector_code: str) -> dict:
        """
        시장별 투자자매매동향(시세) — 외국인/개인/기관계 등 순매수 수량·거래대금.

        입력: FID_INPUT_ISCD(시장구분, 파생상품은 "K2I"), FID_INPUT_ISCD_2(업종구분 — K2I일 때
             F001=선물/OC01=콜옵션/OP01=풋옵션).
        계산: "모의 TR_ID/Domain: 모의투자 미지원"으로 문서화되어 있지만, 계좌 무관 공개
             시세성 데이터라 실측 결과 모의투자 앱키로도 REAL_REST_DOMAIN 호출이 200 OK로
             성공한다(2026-07-06 확인) — 시세 WS와 같은 이유로 실전 도메인을 고정 사용한다.
        실패 조건: 4xx/5xx면 httpx.HTTPStatusError 전파.
        """
        headers = self._headers(tr_codes.TR_INVESTOR_FLOW_BY_MARKET)
        return self._get(
            f"{tr_codes.REAL_REST_DOMAIN}{tr_codes.PATH_INVESTOR_FLOW_BY_MARKET}",
            headers=headers,
            params={"FID_INPUT_ISCD": market_code, "FID_INPUT_ISCD_2": sector_code},
        )

    def get_overseas_future_price(self, srs_cd: str) -> dict:
        """
        해외선물 현재가(inquire-price) — VIX 선물(VX)·USDCNH 선물(CNH) 등 Cross-asset stress
        프록시에 쓴다(v6 §7.3).

        입력: 해외선물 단축코드(예: "VXN26" — 종목코드 마스터파일에서 최근월물/차근월물로 찾음,
             mahdi.data.overseas_future_master 참고).
        계산: PATH_OVERSEAS_FUTUREOPTION_PRICE GET 호출. 이 엔드포인트는 계좌 파라미터가 없어
             계좌 무관 공개 시세로 보이지만, 상품(거래소)에 따라 계좌에 별도 거래소 신청이 걸려
             있어야 한다(2026-07-10 실측: CBOE(VX)·HKEx(CNH)는 모의계좌로 바로 성공, CME/CBOT
             (ZN 등)는 "EGW00552: CBOT SUB거래소 신청 계좌가 아닙니다"로 거부됨 — 코드가 아니라
             계좌 설정 문제이므로 호출측이 이 에러를 구분해 재시도하지 말아야 한다).
        실패 조건: 4xx/5xx면 httpx.HTTPStatusError 그대로 전파.
        """
        tr_id = tr_codes.TR_OVERSEAS_FUTUREOPTION_PRICE
        return self._get(
            f"{self._domain}{tr_codes.PATH_OVERSEAS_FUTUREOPTION_PRICE}",
            headers=self._headers(tr_id),
            params={"SRS_CD": srs_cd},
        )

    def get_overseas_daily_chartprice(
        self, market_div_code: str, symbol: str, date_from: str, date_to: str, period_div_code: str = "D"
    ) -> dict:
        """
        해외주식 종목_지수_환율기간별시세(일_주_월_년) — US10Y(국채구분 I, 심볼 Y0202) 등
        해외선물옵션 계좌 신청 없이도 얻을 수 있는 일봉 대체 경로(v6 §7.3).

        입력: FID_COND_MRKT_DIV_CODE(N=해외지수, X=환율, I=국채, S=금선물), 종목코드(예: "Y0202"),
             조회 시작/종료일(YYYYMMDD), 기간 구분(D=일 기본값).
        계산: PATH_OVERSEAS_INDEX_DAILY_CHARTPRICE GET 호출.
        해석: 2026-07-10 실측 — 같은 API 계열의 분봉 엔드포인트(inquire-time-indexchartprice)는
             I(국채) 구분을 "ERROR INVALID FID_COND_MRKT_DIV_CODE"로 거부해 분봉 미지원이 확정됐다
             — 이 함수(일봉)만이 US10Y를 계좌 제약 없이 얻는 유일한 경로다.
        실패 조건: 4xx/5xx면 httpx.HTTPStatusError 그대로 전파.
        """
        tr_id = tr_codes.TR_OVERSEAS_INDEX_DAILY_CHARTPRICE
        return self._get(
            f"{self._domain}{tr_codes.PATH_OVERSEAS_INDEX_DAILY_CHARTPRICE}",
            headers=self._headers(tr_id),
            params={
                "FID_COND_MRKT_DIV_CODE": market_div_code,
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_DATE_1": date_from,
                "FID_INPUT_DATE_2": date_to,
                "FID_PERIOD_DIV_CODE": period_div_code,
            },
        )

    def get_balance(self) -> dict:
        """
        계산: PATH_FUTUREOPTION_BALANCE GET 호출 (계좌번호는 설정에서 사용).
        실패 조건: 4xx/5xx면 httpx.HTTPStatusError 전파.
        """
        tr_id = tr_codes.TR_BALANCE_INQUIRY[self._env_key]
        return self._get(
            f"{self._domain}{tr_codes.PATH_FUTUREOPTION_BALANCE}",
            headers=self._headers(tr_id),
            params={
                "CANO": self._settings.kis_account_no,
                "ACNT_PRDT_CD": self._settings.kis_account_product_code,
            },
        )

    def submit_order(self, symbol: str, side: str, qty: int, price: float, order_dvsn_cd: str = "01") -> dict:
        """
        입력: 종목코드(단축상품번호 — 선물 6자리/옵션 9자리, 예: B01603955), BUY/SELL, 수량, 가격,
             주문구분코드(ORD_DVSN_CD: 01=지정가, 02=시장가, 03=조건부, 04=최유리 등).
        계산: PATH_FUTUREOPTION_ORDER POST 호출. ORD_PRCS_DVSN_CD="02"(주문전송)과 ORD_DVSN_CD는
             "선물옵션 주문" 문서 기준 필수(Required=Y) 필드 — 누락 시 KIS가 주문을 거부한다.
        실패 조건: 4xx/5xx면 httpx.HTTPStatusError 전파 — 상위 Order State Machine이 REJECTED로 기록.
        """
        tr_id = tr_codes.TR_ORDER_NEW[self._env_key]
        return self._post(
            f"{self._domain}{tr_codes.PATH_FUTUREOPTION_ORDER}",
            headers=self._headers(tr_id),
            json={
                "ORD_PRCS_DVSN_CD": "02",  # 02: 주문전송 (고정값)
                "CANO": self._settings.kis_account_no,
                "ACNT_PRDT_CD": self._settings.kis_account_product_code,
                "SLL_BUY_DVSN_CD": "01" if side.upper() == "SELL" else "02",
                "SHTN_PDNO": symbol,
                "ORD_QTY": str(qty),
                "UNIT_PRICE": str(price),
                "ORD_DVSN_CD": order_dvsn_cd,
            },
        )
