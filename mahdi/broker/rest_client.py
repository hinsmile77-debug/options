"""KIS REST 클라이언트 — 옵션 체인 조회, 잔고 조회, 주문 제출 (모의/실전 겸용).

TR ID/경로 상수는 tr_codes.py 단일 소스를 사용한다.
"""

from __future__ import annotations

import httpx

from mahdi.broker import tr_codes
from mahdi.broker.token_daemon import TokenDaemon
from mahdi.config.settings import KISSettings


class KISRestClient:
    def __init__(self, settings: KISSettings, token_daemon: TokenDaemon, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._token_daemon = token_daemon
        self._client = client or httpx.Client(timeout=10.0)

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
        response = self._client.get(
            f"{self._domain}{tr_codes.PATH_FUTUREOPTION_QUOTE}",
            headers=self._headers(tr_id),
            params={"FID_COND_MRKT_DIV_CODE": market_div_code, "FID_INPUT_ISCD": symbol},
        )
        response.raise_for_status()
        return response.json()

    def get_asking_price(self, symbol: str, market_div_code: str = tr_codes.FID_MRKT_DIV_INDEX_OPTION) -> dict:
        """단일 종목 시세호가(5단계 매도/매수 호가) — "선물옵션 시세호가"(inquire-asking-price)."""
        tr_id = tr_codes.TR_OPTION_ASKING_PRICE[self._env_key]
        response = self._client.get(
            f"{self._domain}{tr_codes.PATH_FUTUREOPTION_ASKING_PRICE}",
            headers=self._headers(tr_id),
            params={"FID_COND_MRKT_DIV_CODE": market_div_code, "FID_INPUT_ISCD": symbol},
        )
        response.raise_for_status()
        return response.json()

    def get_balance(self) -> dict:
        """
        계산: PATH_FUTUREOPTION_BALANCE GET 호출 (계좌번호는 설정에서 사용).
        실패 조건: 4xx/5xx면 httpx.HTTPStatusError 전파.
        """
        tr_id = tr_codes.TR_BALANCE_INQUIRY[self._env_key]
        response = self._client.get(
            f"{self._domain}{tr_codes.PATH_FUTUREOPTION_BALANCE}",
            headers=self._headers(tr_id),
            params={
                "CANO": self._settings.kis_account_no,
                "ACNT_PRDT_CD": self._settings.kis_account_product_code,
            },
        )
        response.raise_for_status()
        return response.json()

    def submit_order(self, symbol: str, side: str, qty: int, price: float, order_dvsn_cd: str = "01") -> dict:
        """
        입력: 종목코드(단축상품번호 — 선물 6자리/옵션 9자리, 예: B01603955), BUY/SELL, 수량, 가격,
             주문구분코드(ORD_DVSN_CD: 01=지정가, 02=시장가, 03=조건부, 04=최유리 등).
        계산: PATH_FUTUREOPTION_ORDER POST 호출. ORD_PRCS_DVSN_CD="02"(주문전송)과 ORD_DVSN_CD는
             "선물옵션 주문" 문서 기준 필수(Required=Y) 필드 — 누락 시 KIS가 주문을 거부한다.
        실패 조건: 4xx/5xx면 httpx.HTTPStatusError 전파 — 상위 Order State Machine이 REJECTED로 기록.
        """
        tr_id = tr_codes.TR_ORDER_NEW[self._env_key]
        response = self._client.post(
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
        response.raise_for_status()
        return response.json()
