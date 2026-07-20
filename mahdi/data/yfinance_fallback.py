"""KIS 유료 구독 항목·KIS 경로 자체가 없는 항목의 공용 폴백 소스(2026-07-20 신규, ZN 전용
zn_fallback.py를 ES/MOVE 추가하며 일반화).

CME 계열 선물(ZN·ES)은 HTS [7936](해외선물옵션 거래소 실시간시세신청/조회) 확인 결과 KIS
API(유료) 탭에만 있고 기간이용료가 붙는다(월 228.8불) — 모의투자 개발 단계에서는 구독하지 않고
yfinance(비공식)로 근사치를 채운다. MOVE(ICE BofA MOVE Index)는 장외 파생 인덱스라 애초에
KIS 해외선물옵션 마스터파일(ffcode.mst)에 상품 자체가 없어 KIS 경로가 없다 — 이 모듈이 유일한
수집 경로다.

mahdi/main.py는 KIS 조회가 가능한 항목(ZN·ES)은 KIS를 먼저 시도하고 실패할 때만 이 모듈을
호출하므로, 나중에 KIS 유료 구독을 시작하면 코드 변경 없이 자동으로 KIS가 우선된다.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

ZN_FALLBACK_SYMBOL = "ZN=F"      # CME 10년 국채선물 연속계약
ES_FALLBACK_SYMBOL = "ES=F"      # CME E-mini S&P500 선물 연속계약
MOVE_FALLBACK_SYMBOL = "^MOVE"   # ICE BofA MOVE Index


def fetch_last_close(symbol: str) -> float | None:
    """
    계산: yfinance로 symbol의 최근 종가(1일치 1분봉 마지막 값)를 가져온다.
    실패 조건: yfinance 미설치, 네트워크 오류, 빈 응답이면 None — 호출측이 KIS 실패와 동일하게
              처리한다. 이 함수 자체는 절대 예외를 올리지 않는다(폴백 조회 실패가 매크로 스냅샷
              사이클 전체를 죽이면 안 됨).
    """
    try:
        import yfinance as yf

        history = yf.Ticker(symbol).history(period="1d", interval="1m")
        if history.empty:
            return None
        return float(history["Close"].iloc[-1])
    except Exception:
        logger.warning("yfinance 폴백 조회 실패 (symbol=%s)", symbol, exc_info=True)
        return None
