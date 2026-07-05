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

    def get_option_chain(self, underlying_code: str) -> dict:
        """
        입력: 기초자산 코드(예: KOSPI200 지수옵션 코드).
        계산: PATH_FUTUREOPTION_CHAIN GET 호출.
        실패 조건: 4xx/5xx면 httpx.HTTPStatusError 그대로 전파 — 호출측이 재시도/알림 처리.
        """
        tr_id = tr_codes.TR_OPTION_CHAIN[self._env_key]
        response = self._client.get(
            f"{self._domain}{tr_codes.PATH_FUTUREOPTION_CHAIN}",
            headers=self._headers(tr_id),
            params={"FID_COND_MRKT_DIV_CODE": "O", "FID_INPUT_ISCD": underlying_code},
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

    def submit_order(self, symbol: str, side: str, qty: int, price: float, order_type: str = "01") -> dict:
        """
        입력: 종목코드, BUY/SELL, 수량, 가격, 주문구분 코드(01=지정가 등 KIS 코드).
        계산: PATH_FUTUREOPTION_ORDER POST 호출.
        실패 조건: 4xx/5xx면 httpx.HTTPStatusError 전파 — 상위 Order State Machine이 REJECTED로 기록.
        """
        tr_id = tr_codes.TR_ORDER_NEW[self._env_key]
        response = self._client.post(
            f"{self._domain}{tr_codes.PATH_FUTUREOPTION_ORDER}",
            headers=self._headers(tr_id),
            json={
                "CANO": self._settings.kis_account_no,
                "ACNT_PRDT_CD": self._settings.kis_account_product_code,
                "SLL_BUY_DVSN_CD": "01" if side.upper() == "SELL" else "02",
                "SHTN_PDNO": symbol,
                "ORD_QTY": str(qty),
                "UNIT_PRICE": str(price),
                "ORD_DVSN": order_type,
            },
        )
        response.raise_for_status()
        return response.json()
