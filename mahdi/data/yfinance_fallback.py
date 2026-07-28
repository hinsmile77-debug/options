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
    계산: yfinance로 symbol의 최근 종가(당일 일봉 1건)를 가져온다.
    실패 조건: yfinance 미설치, 네트워크 오류, 빈 응답이면 None — 호출측이 KIS 실패와 동일하게
              처리한다. 이 함수 자체는 절대 예외를 올리지 않는다(폴백 조회 실패가 매크로 스냅샷
              사이클 전체를 죽이면 안 됨).
    로그(2026-07-28, ZN 13:01 패턴 근본원인 조사): yfinance는 히스토리 조회가 실패해도(레이트리밋
              등으로 온 빈 응답 포함) 기본적으로 예외를 던지지 않고 자체 로거(`logging.
              getLogger("yfinance")`)에만 사유를 남기고 빈 DataFrame을 돌려준다(라이브러리
              소스 `yfinance/scrapers/history.py`의 `raise_errors=False` 기본 경로 확인) — 그래서
              지금까지 mahdi 쪽 로그에는 "비어있다"는 결과만 남고 레이트리밋/네트워크/진짜 데이터
              없음을 구분할 근거가 전혀 없었다. `PriceHistory._last_error`에 이미 그 사유(Yahoo
              status_code 또는 에러 설명 포함)가 계산돼 있어 최선노력으로 읽어 함께 남긴다.
    부하(2026-07-28, 같은 조사): 원래 `interval="1m"`로 호출해 ZN=F/ES=F 같은 거의 24시간
              거래되는 선물은 사이클(5분)마다 1,000행 넘는 1분봉 전체를 매번 다시 받아왔다(실측
              ZN 1,046행/ES 1,153행) — 필요한 건 마지막 값 1개뿐이었다. `interval`을 지정하지
              않으면(일봉 기본값) 같은 `Close`값을 단 1행으로 받는다(실측 확인, 108.5625로 동일).
              `fast_info.last_price`도 검토했으나 내부적으로 `history(period="1y", ...)`(1년치
              일봉, 약 251행)를 호출해 지금 방식보다는 가볍지만 이 방식보다는 오히려 무거워
              채택하지 않았다. 이 반복적·과도한 요청량이 Yahoo의 비공식 레이트리밋을 매일 비슷한
              누적 시점(부팅 후 약 5.5시간)에 건드려 13:01 전후 다중 사이클 실패를 유발했을
              가능성이 유력한 가설이다 — §로그 항목의 `_last_error`가 다음 재발 시 이를 직접
              확인해줄 것이다.
    """
    try:
        import yfinance as yf

        ticker = yf.Ticker(symbol)
        history = ticker.history(period="1d")
        if history.empty:
            logger.warning(
                "yfinance 폴백 조회 결과 비어있음 (symbol=%s, yfinance 내부 사유=%s)",
                symbol, _last_error_reason(ticker) or "사유 미확인(yfinance 자체 로그 참고)",
            )
            return None
        return float(history["Close"].iloc[-1])
    except Exception:
        logger.warning("yfinance 폴백 조회 실패 (symbol=%s)", symbol, exc_info=True)
        return None


def _last_error_reason(ticker) -> str | None:
    """계산: yfinance `Ticker` 내부 `PriceHistory._last_error`를 최선노력으로 읽는다 — 공식
    공개 API가 아니라 버전이 바뀌면 이름/구조가 달라질 수 있어, 어떤 이유로든 접근에 실패하면
    조용히 None을 돌려준다(이 함수는 진단용 부가 정보일 뿐 핵심 동작에 영향을 주면 안 된다)."""
    try:
        return ticker._lazy_load_price_history()._last_error
    except Exception:
        return None
