import logging

import pandas as pd
import pytest

from mahdi.data import yfinance_fallback


class _FakePriceHistory:
    def __init__(self, last_error: str | None):
        self._last_error = last_error


class _FakeTicker:
    """yfinance.Ticker 대체용 — history()가 돌려줄 DataFrame과 내부 PriceHistory._last_error를
    각각 지정할 수 있다(2026-07-28, ZN 13:01 패턴 근본원인 조사용 진단 로깅 검증)."""

    def __init__(self, symbol: str, *, history_df: pd.DataFrame, last_error: str | None = None):
        self.symbol = symbol
        self._history_df = history_df
        self._price_history = _FakePriceHistory(last_error)
        self.history_calls: list[dict] = []

    def history(self, **kwargs) -> pd.DataFrame:
        self.history_calls.append(kwargs)
        return self._history_df

    def _lazy_load_price_history(self):
        return self._price_history


def test_fetch_last_close_returns_latest_close(monkeypatch):
    df = pd.DataFrame({"Close": [108.50, 108.5625]})
    monkeypatch.setattr(
        "yfinance.Ticker", lambda symbol: _FakeTicker(symbol, history_df=df)
    )

    assert yfinance_fallback.fetch_last_close("ZN=F") == pytest.approx(108.5625)


def test_fetch_last_close_requests_daily_bar_not_1m_intraday(monkeypatch):
    # 2026-07-28(ZN 13:01 패턴 근본원인 조사): interval="1m"으로 24시간 가까이 거래되는 ZN=F/ES=F를
    # 조회하면 사이클(5분)마다 1,000행 넘게 다시 받아왔다(실측 ZN 1,046행/ES 1,153행) — 필요한 건
    # 마지막 값 1개뿐이라 일봉 기본값(단 1행, 같은 Close값)으로 줄였다. interval 키워드를 아예
    # 넘기지 않는지(=일봉 기본값을 쓰는지) 회귀 테스트로 고정한다.
    df = pd.DataFrame({"Close": [108.5625]})
    captured = {}

    def _make_ticker(symbol):
        ticker = _FakeTicker(symbol, history_df=df)
        captured["ticker"] = ticker
        return ticker

    monkeypatch.setattr("yfinance.Ticker", _make_ticker)

    yfinance_fallback.fetch_last_close("ZN=F")

    assert captured["ticker"].history_calls == [{"period": "1d"}]


def test_fetch_last_close_logs_yfinance_internal_reason_when_empty(monkeypatch, caplog):
    # 2026-07-28: yfinance가 빈 DataFrame을 돌려줄 때(레이트리밋 등으로 인한 실패 포함) 지금까지는
    # "비어있다"는 사실만 남았다 — PriceHistory._last_error에 담긴 실제 사유(예: Yahoo
    # status_code)가 로그에 그대로 노출되는지 검증한다.
    empty = pd.DataFrame()
    monkeypatch.setattr(
        "yfinance.Ticker",
        lambda symbol: _FakeTicker(symbol, history_df=empty, last_error="(Yahoo status_code = 429)"),
    )

    with caplog.at_level(logging.WARNING, logger="mahdi.data.yfinance_fallback"):
        result = yfinance_fallback.fetch_last_close("ZN=F")

    assert result is None
    assert "429" in caplog.text


def test_fetch_last_close_falls_back_to_generic_message_when_reason_unavailable(monkeypatch, caplog):
    # _last_error 자체가 None이거나(진짜 사유 미기록) 속성 접근이 실패하는 yfinance 버전이어도
    # 조용히 일반 메시지로 대체돼야 한다 — 진단 정보 부재가 폴백 조회 자체를 막으면 안 된다.
    empty = pd.DataFrame()
    monkeypatch.setattr(
        "yfinance.Ticker", lambda symbol: _FakeTicker(symbol, history_df=empty, last_error=None)
    )

    with caplog.at_level(logging.WARNING, logger="mahdi.data.yfinance_fallback"):
        result = yfinance_fallback.fetch_last_close("ZN=F")

    assert result is None
    assert "사유 미확인" in caplog.text


def test_fetch_last_close_reason_lookup_never_raises_even_if_internals_missing(monkeypatch, caplog):
    # yfinance 내부 구조(_lazy_load_price_history/_last_error)가 다음 버전에서 바뀌어 없어져도
    # fetch_last_close 자체는 계속 None을 안전하게 돌려줘야 한다.
    class _TickerWithoutInternals:
        def __init__(self, symbol):
            pass

        def history(self, period=None, interval=None) -> pd.DataFrame:
            return pd.DataFrame()

    monkeypatch.setattr("yfinance.Ticker", _TickerWithoutInternals)

    with caplog.at_level(logging.WARNING, logger="mahdi.data.yfinance_fallback"):
        result = yfinance_fallback.fetch_last_close("ZN=F")

    assert result is None
    assert "사유 미확인" in caplog.text


def test_fetch_last_close_returns_none_and_logs_on_exception(monkeypatch, caplog):
    def _raise(symbol):
        raise RuntimeError("network down")

    monkeypatch.setattr("yfinance.Ticker", _raise)

    with caplog.at_level(logging.WARNING, logger="mahdi.data.yfinance_fallback"):
        result = yfinance_fallback.fetch_last_close("ZN=F")

    assert result is None
    assert "yfinance 폴백 조회 실패" in caplog.text
