"""KIS OpenAPI 엔드포인트·TR ID 상수 — 한 파일에 모아두어 KIS 개발자센터 실제 스펙과
대조·수정하기 쉽게 한다. 옵션 관련 TR ID(주문/잔고/실시간)는 KIS 개발자센터 문서·공식
샘플(github.com/koreainvestment/open-trading-api)로 최종 확인 후 조정할 것.
"""

from __future__ import annotations

REAL_REST_DOMAIN = "https://openapi.koreainvestment.com:9443"
VPS_REST_DOMAIN = "https://openapivts.koreainvestment.com:29443"  # 모의투자
REAL_WS_DOMAIN = "ws://ops.koreainvestment.com:21000"
VPS_WS_DOMAIN = "ws://ops.koreainvestment.com:31000"  # 모의투자

PATH_TOKEN = "/oauth2/tokenP"
PATH_WS_APPROVAL = "/oauth2/Approval"
PATH_FUTUREOPTION_ORDER = "/uapi/domestic-futureoption/v1/trading/order"
PATH_FUTUREOPTION_ORDER_MODIFY_CANCEL = "/uapi/domestic-futureoption/v1/trading/order-rvsecncl"
PATH_FUTUREOPTION_BALANCE = "/uapi/domestic-futureoption/v1/trading/inquire-balance"
PATH_FUTUREOPTION_CHAIN = "/uapi/domestic-futureoption/v1/quotations/display-board-option-list"

# 주문 TR ID (실전 T / 모의 V 접두 관례)
TR_ORDER_NEW = {"real": "TTTO1101U", "vps": "VTTO1101U"}
TR_ORDER_MODIFY_CANCEL = {"real": "TTTO1103U", "vps": "VTTO1103U"}
TR_BALANCE_INQUIRY = {"real": "CTFO6118R", "vps": "VTFO6118R"}
TR_OPTION_CHAIN = {"real": "FHPIF05030100", "vps": "FHPIF05030100"}

# 실시간(WebSocket) TR ID
WS_TR_OPTION_CONTRACT = "H0IOCNT0"  # 지수옵션 실시간체결가
WS_TR_OPTION_ORDERBOOK = "H0IOASP0"  # 지수옵션 실시간호가
