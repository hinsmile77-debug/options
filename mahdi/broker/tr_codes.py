"""KIS OpenAPI 엔드포인트·TR ID 상수 — 한국투자증권 공식 API 문서
(docs/efriend/한국투자증권_오픈API_전체문서_20260705_030000.xlsx, 시트 "API 목록" +
"선물옵션 주문"/"선물옵션 시세"/"선물옵션 시세호가"/"지수옵션 실시간체결가"/"지수옵션 실시간호가")로
2026-07-06 검증 완료. 값이 바뀌면 이 파일만 고치면 되도록 단일 소스로 유지한다.
"""

from __future__ import annotations

REAL_REST_DOMAIN = "https://openapi.koreainvestment.com:9443"
VPS_REST_DOMAIN = "https://openapivts.koreainvestment.com:29443"  # 모의투자

# 실시간(WS) 도메인: 계좌 종류(모의/실전)가 아니라 "무엇을 구독하는가"에 따라 달라진다.
#  - 시세(시장 데이터: 체결가/호가 등)는 계좌와 무관한 공개 데이터라 모의투자 전용 도메인이
#    아예 없다 — 지수옵션 실시간체결가(H0IOCNT0)/실시간호가(H0IOASP0)는 "모의 TR_ID: 모의투자
#    미지원", "모의 Domain: 모의투자 미지원"으로 명시되어 있어, 모의투자 계좌로 운용 중이어도
#    시세 구독은 REAL_WS_DOMAIN 하나로만 접속한다.
#  - 반대로 "주문체결통보"(내 주문의 체결 알림)는 계좌를 특정해야 하므로 모의/실전이 TR_ID와
#    도메인 모두 분리되어 있다 (예: 선물옵션 실시간체결통보 H0IFCNI0(실전)/H0IFCNI9(모의),
#    모의 Domain: ws://ops.koreainvestment.com:31000). Phase1은 시세 구독만 다루므로
#    MARKET_DATA_WS_DOMAIN을 쓰고, 체결통보 연동(Phase2)에서만 REAL_WS_DOMAIN/VPS_WS_DOMAIN을
#    is_mock으로 분기해서 쓴다.
REAL_WS_DOMAIN = "ws://ops.koreainvestment.com:21000"
VPS_WS_DOMAIN = "ws://ops.koreainvestment.com:31000"  # 계좌별 체결통보 전용 (Phase2)
MARKET_DATA_WS_DOMAIN = REAL_WS_DOMAIN  # 시세 구독은 항상 이 도메인 (모의투자 전용 시세 도메인 없음)

PATH_TOKEN = "/oauth2/tokenP"
PATH_TOKEN_REVOKE = "/oauth2/revokeP"
PATH_WS_APPROVAL = "/oauth2/Approval"

PATH_FUTUREOPTION_ORDER = "/uapi/domestic-futureoption/v1/trading/order"
PATH_FUTUREOPTION_ORDER_MODIFY_CANCEL = "/uapi/domestic-futureoption/v1/trading/order-rvsecncl"
PATH_FUTUREOPTION_BALANCE = "/uapi/domestic-futureoption/v1/trading/inquire-balance"

# 단일 종목 시세/시세호가 — 실전·모의 겸용. "국내옵션전광판_*"(display-board-*) 계열은 모의투자
# 미지원이라 Phase1(모의투자)에서는 쓸 수 없다. 전 종목 체인을 한 번에 받는 REST는 모의투자에
# 존재하지 않으므로, 스트라이크별로 이 엔드포인트를 반복 호출하거나(종목코드 마스터파일 필요,
# github.com/koreainvestment/open-trading-api/tree/main/stocks_info 참고) WS 실시간체결가로
# 체인을 구성해야 한다 — 아직 미해결 과제로 남겨둔다.
PATH_FUTUREOPTION_QUOTE = "/uapi/domestic-futureoption/v1/quotations/inquire-price"
PATH_FUTUREOPTION_ASKING_PRICE = "/uapi/domestic-futureoption/v1/quotations/inquire-asking-price"

# 시장별 투자자매매동향(시세) — "모의 TR_ID/Domain: 모의투자 미지원"이지만 계좌 무관 공개 시세성
# 데이터라, 시세 WS와 같은 이유로 모의투자 앱키로도 REAL_REST_DOMAIN 호출이 그대로 성공한다
# (2026-07-06 실측 확인, 200 OK). 그래서 실전/모의 분기 없이 TR ID 하나만 둔다.
PATH_INVESTOR_FLOW_BY_MARKET = "/uapi/domestic-stock/v1/quotations/inquire-investor-time-by-market"
TR_INVESTOR_FLOW_BY_MARKET = "FHPTJ04030000"

# FID_INPUT_ISCD=K2I(선물/콜옵션/풋옵션 통합 시장구분)일 때 FID_INPUT_ISCD_2(업종구분) 값
FID_INVESTOR_FLOW_FUTURES = "F001"
FID_INVESTOR_FLOW_CALL_OPTION = "OC01"
FID_INVESTOR_FLOW_PUT_OPTION = "OP01"
FID_MRKT_DIV_DERIVATIVES = "K2I"

# 주문 TR ID (실전 T / 모의 V 접두 관례) — "선물옵션 주문" 시트 실측
TR_ORDER_NEW = {"real": "TTTO1101U", "vps": "VTTO1101U"}
TR_ORDER_MODIFY_CANCEL = {"real": "TTTO1103U", "vps": "VTTO1103U"}
TR_BALANCE_INQUIRY = {"real": "CTFO6118R", "vps": "VTFO6118R"}
TR_OPTION_QUOTE = {"real": "FHMIF10000000", "vps": "FHMIF10000000"}
TR_OPTION_ASKING_PRICE = {"real": "FHMIF10010000", "vps": "FHMIF10010000"}

# FID_COND_MRKT_DIV_CODE (선물옵션 시세/시세호가 공통 쿼리 파라미터)
FID_MRKT_DIV_INDEX_FUTURES = "F"   # 지수선물
FID_MRKT_DIV_INDEX_OPTION = "O"    # 지수옵션

# 실시간(WebSocket) TR ID — 계좌 무관, MARKET_DATA_WS_DOMAIN 하나로 접속
WS_TR_OPTION_CONTRACT = "H0IOCNT0"  # 지수옵션 실시간체결가
WS_TR_OPTION_ORDERBOOK = "H0IOASP0"  # 지수옵션 실시간호가

# 선물옵션 실시간체결통보 (계좌별 주문체결 알림 — Phase2에서 사용, 모의/실전 TR_ID·도메인 모두 분리)
WS_TR_ORDER_NOTICE = {"real": "H0IFCNI0", "vps": "H0IFCNI9"}
