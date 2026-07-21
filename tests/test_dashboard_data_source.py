from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

import pytest

from mahdi.data import db
from mahdi.dashboard.data_source import (
    HealthCheck,
    _cbot_status_check,
    _fossil_data_check,
    _freshness_check,
    _futures_freshness_check,
    _is_trading_hours,
    _option_chain_freshness_check,
    _option_chain_leg_balance_check,
    _regime_fit_progress_check,
    _regime_stability_check,
    _schema_integrity_check,
    _shutdown_reliability_check,
    _synthetic_snapshot,
    get_health_summary,
    get_slack_alerts_enabled,
    load_snapshot,
    set_slack_alerts_enabled,
)
from mahdi.engines.regime import RegimeLabel


def test_synthetic_snapshot_is_flagged_not_live_and_internally_consistent():
    snap = _synthetic_snapshot(seed=42)

    assert snap.is_live is False
    assert len(snap.timestamps) == len(snap.ofi_series) == len(snap.vpin_series) == len(snap.price_series)
    assert all(0.0 <= v <= 1.0 for v in snap.vpin_series)
    assert abs(sum(snap.regime_prob.values()) - 1.0) < 1e-9
    assert snap.regime in RegimeLabel
    assert len(snap.chain) > 0


def test_synthetic_snapshot_is_deterministic_given_seed():
    a = _synthetic_snapshot(seed=7)
    b = _synthetic_snapshot(seed=7)
    assert a.spot == b.spot
    assert a.ofi_series == b.ofi_series


def test_load_snapshot_falls_back_to_synthetic_when_db_unavailable(monkeypatch):
    @contextmanager
    def broken_connection(settings=None):
        raise ConnectionError("DB 없음")
        yield  # pragma: no cover

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", broken_connection)

    snap = load_snapshot()

    assert snap.is_live is False


class _FakeCursor:
    """쿼리 문자열/파라미터로 어떤 조회인지 구분해 미리 준비한 결과를 돌려준다."""

    def __init__(self, responses: dict, query_log: list | None = None):
        self._responses = responses
        self._current: list = []
        self._query_log = query_log if query_log is not None else []

    def execute(self, query: str, params=None) -> None:
        self._query_log.append((query, params))
        if "regime_state" in query:
            self._current = self._responses["regime"]
        elif "underlying_spot_1m" in query:
            self._current = self._responses["spot"]
        elif "option_analysis_1m" in query:
            self._current = self._responses["chain"]
        elif "investor_flow_1m" in query:
            self._current = self._responses["investor_flow"]
        elif "active_futures_symbol" in query:
            self._current = self._responses["futures_symbol"]
        elif "GROUP BY symbol" in query:
            self._current = self._responses["option_symbol"]
        elif "expiry_liquidity_1m" in query:
            self._current = self._responses.get("expiry_liquidity", [])
        elif "market_raw_1m" in query and params and params[0] == self._responses.get("futures_symbol_value"):
            self._current = self._responses["futures_rows"]
        elif "market_raw_1m" in query:
            self._current = self._responses["option_rows"]
        else:
            self._current = []

    def fetchone(self):
        return self._current[0] if self._current else None

    def fetchall(self):
        return self._current

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConnection:
    def __init__(self, responses: dict):
        self._responses = responses
        self.query_log: list = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._responses, self.query_log)


_BASE_RESPONSES = {
    "regime": [(datetime(2026, 7, 6, 9, 31), 2, [0.1] * 8, None, False)],
    "spot": [(1333.77,)],
    "chain": [],
    "futures_symbol": [],
    "futures_symbol_value": None,
    "futures_rows": [],
    "option_symbol": [],
    "option_rows": [],
    "investor_flow": [],
}


def test_load_snapshot_builds_live_snapshot_with_real_spot_and_chain(monkeypatch):
    # 2026-07-06 발견한 버그의 회귀 테스트: 기초자산 현재가는 underlying_spot_1m에서,
    # Gamma Map은 option_analysis_1m 체인에서 와야 한다(예전엔 market_raw_1m의 고정 라벨
    # "KOSPI200_OPT"를 잘못 "기초자산"으로 표시했었음).
    ts = datetime(2026, 7, 6, 9, 31)
    responses = {
        **_BASE_RESPONSES,
        "regime": [(ts, 2, [0.1] * 8, None, False)],
        "chain": [
            (1340.0, "C", 363, 0.9, 0.0047, 1000.0, date(2026, 7, 9), ts),
            (1340.0, "P", 200, 0.85, 0.0040, -800.0, date(2026, 7, 9), ts),
        ],
        "investor_flow": [(-150.0, 250.0, -40.0)],
    }

    @contextmanager
    def fake_get_connection(settings=None):
        yield _FakeConnection(responses)

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", fake_get_connection)

    snap = load_snapshot()

    assert snap.is_live is True
    assert snap.spot == 1333.77  # market_raw_1m의 옵션 체결가가 아니라 진짜 지수 스팟
    assert len(snap.chain) == 1  # 같은 행사가의 콜/풋이 하나로 합산됨
    assert snap.chain[0].strike == 1340.0
    assert snap.chain[0].gex == pytest.approx(200.0)  # 1000.0 + (-800.0)
    assert snap.foreign_net == -150.0
    assert snap.institution_net == 250.0
    assert snap.individual_net == -40.0


def test_load_snapshot_splits_futures_and_option_flow_series(monkeypatch):
    # 2026-07-06 발견: 선물이 WS 구독 덕에 거의 매분 체결돼 "가장 최근 활동"만으로 대표 종목을
    # 뽑으면 옵션이 영원히 안 뽑힌다 — Flow Radar는 선물/옵션 계열을 각각 따로 조회해야 한다.
    # 선물 식별은 active_futures_symbol 레지스트리로 명시적으로 한다(vpin 유무 휴리스틱은
    # 옵션에도 VPIN을 적용하면서 깨졌음).
    ts = datetime(2026, 7, 6, 9, 31)
    responses = {
        **_BASE_RESPONSES,
        "regime": [(ts, 2, [0.1] * 8, None, False)],
        "futures_symbol": [("A01609",)],
        "futures_symbol_value": "A01609",
        "futures_rows": [(ts, 1271.15, 92.0, 1270.89, 0.62)],
        "option_symbol": [("B01607B38",)],
        "option_rows": [(ts, 40.65, 12.0, 40.7, 0.55)],
    }

    @contextmanager
    def fake_get_connection(settings=None):
        yield _FakeConnection(responses)

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", fake_get_connection)

    snap = load_snapshot()

    assert snap.futures_flow_symbol == "A01609"
    assert snap.price_series == [1271.15]
    assert snap.vpin_series == [0.62]

    assert snap.option_flow_symbol == "B01607B38"
    assert snap.option_price_series == [40.65]
    assert snap.option_ofi_series == [12.0]
    assert snap.option_microprice_series == [40.7]
    assert snap.option_vpin_series == [0.55]  # 2026-07-06: 옵션도 VPIN이 실제로 계산됨


def test_load_snapshot_picks_option_flow_symbol_by_windowed_volume_with_deterministic_tiebreak(monkeypatch):
    # 2026-07-06 위클리 북 추가 후 실측: 여러 위클리 종목이 같은 1분봉 timestamp로 동시에 찍혀서
    # "ORDER BY max(timestamp) DESC"만 쓰면 동률 처리가 비결정적이라 COCKPIT 리런(10초)마다
    # 뽑히는 종목이 계속 바뀌었다(차트가 매번 다른 종목으로 바뀌어 보임). 최근 룩백 윈도 누적
    # 거래량 + symbol 오름차순 타이브레이커로 쿼리가 바뀌었는지 검증한다.
    ts = datetime(2026, 7, 6, 9, 31)
    responses = {
        **_BASE_RESPONSES,
        "regime": [(ts, 2, [0.1] * 8, None, False)],
    }
    conn = _FakeConnection(responses)

    @contextmanager
    def fake_get_connection(settings=None):
        yield conn

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", fake_get_connection)

    load_snapshot()

    option_queries = [(q, p) for q, p in conn.query_log if "GROUP BY symbol" in q]
    assert len(option_queries) == 1
    query, params = option_queries[0]
    assert "sum(volume) DESC" in query
    assert "symbol ASC" in query  # 동률(거래량·시각 모두 같음)까지 결정론적으로 고정하는 최종 타이브레이커
    assert "timestamp >=" in query  # 단일 최근 틱이 아니라 룩백 윈도 내 누적 활동 기준
    # 룩백 기준 시각은 datetime.now()가 아니라 스냅샷 자체의 시각(regime_state.timestamp)이어야
    # 리플레이/재현 시나리오에서도 윈도가 항상 실제 데이터 시각 기준으로 맞는다.
    assert params[-1] == ts - timedelta(minutes=10)


def test_load_snapshot_defaults_vpin_to_zero_when_null(monkeypatch):
    # 아직 등거래량 버킷이 한 번도 안 닫혔으면 vpin은 NULL — 0.0으로 안전하게 처리돼야 한다.
    ts = datetime(2026, 7, 6, 9, 31)
    responses = {
        **_BASE_RESPONSES,
        "regime": [(ts, 2, [0.1] * 8, None, False)],
        "futures_symbol": [("A01609",)],
        "futures_symbol_value": "A01609",
        "futures_rows": [(ts, 1271.15, 92.0, 1270.89, None)],
    }

    @contextmanager
    def fake_get_connection(settings=None):
        yield _FakeConnection(responses)

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", fake_get_connection)

    snap = load_snapshot()

    assert snap.vpin_series == [0.0]


def test_load_snapshot_reads_expiry_liquidity_per_series(monkeypatch):
    # Phase 1.5-④(2026-07-06 추가): 먼슬리/위클리 두 북의 최신 유동성 스냅샷이 그대로 실려야 함.
    ts = datetime(2026, 7, 6, 9, 31)
    responses = {
        **_BASE_RESPONSES,
        "regime": [(ts, 2, [0.1] * 8, None, False)],
        "expiry_liquidity": [
            ("regular", date(2026, 7, 30), 0.041, 220.0, 480.0, 24),
            ("weekly", date(2026, 7, 9), 0.093, 70.0, 140.0, 3),
        ],
    }

    @contextmanager
    def fake_get_connection(settings=None):
        yield _FakeConnection(responses)

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", fake_get_connection)

    snap = load_snapshot()

    assert len(snap.expiry_liquidity) == 2
    by_series = {row["series"]: row for row in snap.expiry_liquidity}
    assert by_series["regular"]["expiry"] == date(2026, 7, 30)
    assert by_series["regular"]["atm_spread_pct"] == pytest.approx(0.041)
    assert by_series["regular"]["days_to_expiry"] == 24
    assert by_series["weekly"]["depth"] == pytest.approx(70.0)
    assert by_series["weekly"]["volume"] == pytest.approx(140.0)


def test_load_snapshot_defaults_investor_flow_to_zero_when_not_yet_polled(monkeypatch):
    ts = datetime(2026, 7, 6, 9, 31)
    responses = {
        **_BASE_RESPONSES,
        "regime": [(ts, 2, [0.1] * 8, None, False)],
    }

    @contextmanager
    def fake_get_connection(settings=None):
        yield _FakeConnection(responses)

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", fake_get_connection)

    snap = load_snapshot()

    assert snap.is_live is True
    assert snap.foreign_net == 0.0
    assert snap.institution_net == 0.0
    assert snap.individual_net == 0.0


class _FakeSlackSettingsCursor:
    def __init__(self, row):
        self._row = row

    def execute(self, query, params=None) -> None:
        pass

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeSlackSettingsConnection:
    def __init__(self, row):
        self._row = row
        self.committed = False

    def cursor(self):
        return _FakeSlackSettingsCursor(self._row)

    def commit(self) -> None:
        self.committed = True


def test_get_slack_alerts_enabled_reads_stored_value(monkeypatch):
    # 2026-07-19(§5-4): COCKPIT과 mahdi.main은 서로 다른 프로세스라 DB가 단일 진실 공급원이다.
    @contextmanager
    def fake_get_connection(settings=None):
        yield _FakeSlackSettingsConnection((False,))

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", fake_get_connection)

    assert get_slack_alerts_enabled() is False


def test_get_slack_alerts_enabled_falls_back_to_true_when_db_unavailable(monkeypatch):
    # DB 연결 실패 시 "꺼짐"으로 잘못 표시해 사용자를 안심시키는 것보다 "켜짐"으로 보수적으로
    # 표시하는 게 안전한 방향이라 True로 폴백한다.
    @contextmanager
    def broken_connection(settings=None):
        raise ConnectionError("DB 없음")
        yield  # pragma: no cover

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", broken_connection)

    assert get_slack_alerts_enabled() is True


def test_set_slack_alerts_enabled_writes_and_commits(monkeypatch):
    conn = _FakeSlackSettingsConnection((True,))

    @contextmanager
    def fake_get_connection(settings=None):
        yield conn

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", fake_get_connection)

    set_slack_alerts_enabled(False)  # 예외 없이 조용히 저장돼야 함

    assert conn.committed is True


def test_set_slack_alerts_enabled_swallows_db_errors(monkeypatch):
    # COCKPIT 렌더링 도중 저장이 실패해도 대시보드 자체가 죽으면 안 된다.
    @contextmanager
    def broken_connection(settings=None):
        raise ConnectionError("DB 없음")
        yield  # pragma: no cover

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", broken_connection)

    set_slack_alerts_enabled(True)  # 예외가 전파되면 이 줄에서 테스트가 실패한다


class _FakeHealthCursor:
    """쿼리 문자열의 특정 부분으로 어떤 조회인지 구분해 미리 준비한 값을 돌려준다 —
    get_health_summary()가 여러 종류의 쿼리(직접 SQL + db.py 함수 경유)를 섞어 쓰기 때문에
    범용으로 만들었다."""

    def __init__(self, responses: dict, log: list):
        self._responses = responses
        self._log = log
        self._kind = "one"
        self._value = None

    def execute(self, query: str, params=None) -> None:
        self._log.append((query, params))
        if "option_analysis_1m" in query and "MAX(timestamp)" in query:
            self._kind, self._value = "one", self._responses.get("option_chain_latest")
        elif "option_analysis_1m" in query and "GROUP BY option_type" in query:
            self._kind, self._value = "all", self._responses.get("leg_balance_rows", [])
        elif "active_futures_symbol" in query:
            self._kind, self._value = "one", self._responses.get("futures_symbol_row")
        elif "market_raw_1m" in query and "MAX(timestamp)" in query:
            self._kind, self._value = "one", self._responses.get("futures_latest")
        elif "market_raw_1m" in query and "count(*)" in query:
            self._kind, self._value = "one", self._responses.get("legacy_symbol_count_row", (0,))
        elif "macro_snapshot_5m" in query and "us10y_yield IS NOT NULL" in query:
            self._kind, self._value = "one", self._responses.get("macro_fallback_row")
        elif "macro_snapshot_5m" in query and "usdkrw IS NOT NULL" in query:
            self._kind, self._value = "one", self._responses.get("usdkrw_fallback_row")
        elif "macro_snapshot_5m" in query:
            self._kind, self._value = "one", self._responses.get("macro_row")
        elif "information_schema.columns" in query:
            self._kind, self._value = "all", self._responses.get("schema_columns_rows", [])
        elif "expiry_liquidity_1m" in query:
            self._kind, self._value = "all", self._responses.get("fossil_series_rows", [])
        elif "regime_state" in query:
            self._kind, self._value = "one", self._responses.get("regime_stability_row")
        elif "feature_store" in query:
            self._kind, self._value = "one", self._responses.get("regime_fit_progress_row")
        elif "shutdown_check_log" in query:
            self._kind, self._value = "one", self._responses.get("shutdown_check_row")
        else:
            self._kind, self._value = "one", None

    def fetchone(self):
        return self._value

    def fetchall(self):
        return self._value if self._value is not None else []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeHealthConnection:
    def __init__(self, responses: dict):
        self._responses = responses
        self.log: list = []
        self.rollback_calls = 0

    def cursor(self) -> _FakeHealthCursor:
        return _FakeHealthCursor(self._responses, self.log)

    def rollback(self) -> None:
        self.rollback_calls += 1


class _BrokenHealthCursor:
    def execute(self, *args, **kwargs) -> None:
        raise RuntimeError("DB 오류")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _BrokenHealthConnection:
    def __init__(self):
        self.rollback_calls = 0

    def cursor(self) -> _BrokenHealthCursor:
        return _BrokenHealthCursor()

    def rollback(self) -> None:
        self.rollback_calls += 1


# --- _is_trading_hours / _freshness_check (순수 로직, DB 불필요) ------------------------------

def test_is_trading_hours_true_during_weekday_market_window():
    assert _is_trading_hours(datetime(2026, 7, 20, 10, 0)) is True  # 월요일 10:00


def test_is_trading_hours_false_on_weekend():
    assert _is_trading_hours(datetime(2026, 7, 18, 10, 0)) is False  # 토요일


def test_is_trading_hours_false_outside_market_window():
    assert _is_trading_hours(datetime(2026, 7, 20, 8, 59)) is False
    assert _is_trading_hours(datetime(2026, 7, 20, 15, 46)) is False


def test_freshness_check_is_info_outside_trading_hours_even_without_data():
    # 장중이 아니면 데이터가 안 들어와도 정상 — 결손으로 오판하면 안 된다.
    check = _freshness_check("라벨", None, datetime(2026, 7, 18, 10, 0))
    assert check.status == "info"


def test_freshness_check_warning_when_no_data_during_trading_hours():
    check = _freshness_check("라벨", None, datetime(2026, 7, 20, 10, 0))
    assert check.status == "warning"


def test_freshness_check_ok_when_recently_updated():
    now = datetime(2026, 7, 20, 10, 5)
    check = _freshness_check("라벨", now - timedelta(seconds=30), now)
    assert check.status == "ok"


def test_freshness_check_warning_when_stale_beyond_threshold():
    # §5-4 Slack 알림과 동일한 5분 기준.
    now = datetime(2026, 7, 20, 10, 10)
    check = _freshness_check("라벨", now - timedelta(minutes=6), now)
    assert check.status == "warning"
    assert "6분째 결손" in check.detail


def test_freshness_check_handles_timezone_aware_latest_ts_from_db(monkeypatch):
    # 2026-07-20 실측 버그: latest_ts는 TIMESTAMPTZ 컬럼(MAX(timestamp))에서 psycopg가 읽어와
    # tzinfo가 붙어 있는데, now(db.local_now())는 naive라 "now - latest_ts"가
    # "can't subtract offset-naive and offset-aware datetimes" TypeError로 죽었다 — 장외시간
    # 실측만으로는 이 경로(_is_trading_hours 통과 후 실제로 뺄셈)가 한 번도 실행 안 돼 숨어있다가,
    # 정규장 시간에 처음 실제로 터진 것을 실측 확인했다. now가 naive-KST일 때 latest_ts가
    # tzinfo=UTC로 붙어와도(db.local_now() 정책상 벽시계 숫자는 이미 같은 좌표계) 죽지 않고
    # 정상 계산돼야 한다.
    now = datetime(2026, 7, 20, 10, 5)
    aware_latest_ts = datetime(2026, 7, 20, 10, 4, 30, tzinfo=timezone.utc)  # 30초 전, tzinfo 있음
    check = _freshness_check("라벨", aware_latest_ts, now)
    assert check.status == "ok"
    assert "30초 전 갱신" in check.detail


# --- _option_chain_freshness_check ------------------------------------------------------------

def test_option_chain_freshness_check_ok():
    now = datetime(2026, 7, 20, 10, 0)
    # TIMESTAMPTZ 컬럼에서 psycopg가 실제로 돌려주는 형태(tzinfo 있음)를 그대로 재현 —
    # naive로만 테스트하면 2026-07-20에 실측한 tzinfo 불일치 버그를 못 잡는다.
    aware_latest = (now - timedelta(seconds=20)).replace(tzinfo=timezone.utc)
    conn = _FakeHealthConnection({"option_chain_latest": (aware_latest,)})
    check = _option_chain_freshness_check(conn, "KOSPI200", now)
    assert check.status == "ok"


def test_option_chain_freshness_check_handles_query_error():
    conn = _BrokenHealthConnection()
    check = _option_chain_freshness_check(conn, "KOSPI200", datetime(2026, 7, 20, 10, 0))
    assert check.status == "warning"
    assert conn.rollback_calls == 1


# --- _futures_freshness_check -------------------------------------------------------------------

def test_futures_freshness_check_info_when_no_futures_symbol_registered():
    now = datetime(2026, 7, 20, 10, 0)
    conn = _FakeHealthConnection({"futures_symbol_row": None})
    check = _futures_freshness_check(conn, "KOSPI200", now)
    assert check.status == "info"


def test_futures_freshness_check_ok_when_recent():
    now = datetime(2026, 7, 20, 10, 0)
    aware_latest = (now - timedelta(seconds=15)).replace(tzinfo=timezone.utc)
    conn = _FakeHealthConnection(
        {"futures_symbol_row": ("101S03",), "futures_latest": (aware_latest,)}
    )
    check = _futures_freshness_check(conn, "KOSPI200", now)
    assert check.status == "ok"


# --- _option_chain_leg_balance_check (2026-07-20, 콜/풋 조회 성공률 비대칭 발견) -----------------

def test_leg_balance_check_info_when_no_data_in_lookback_window():
    now = datetime(2026, 7, 20, 7, 30)
    conn = _FakeHealthConnection({"leg_balance_rows": []})
    check = _option_chain_leg_balance_check(conn, "KOSPI200", now)
    assert check.status == "info"
    assert "데이터 없음" in check.detail


def test_leg_balance_check_ok_when_call_and_put_roughly_balanced():
    now = datetime(2026, 7, 20, 7, 30)
    conn = _FakeHealthConnection({"leg_balance_rows": [("C", 18), ("P", 15)]})
    check = _option_chain_leg_balance_check(conn, "KOSPI200", now)
    assert check.status == "ok"
    assert "콜 18건 / 풋 15건" in check.detail


def test_leg_balance_check_warns_when_put_side_mostly_failing():
    # 2026-07-20 실측 그대로: 콜 18~19건 vs 풋 3건.
    now = datetime(2026, 7, 20, 7, 30)
    conn = _FakeHealthConnection({"leg_balance_rows": [("C", 18), ("P", 3)]})
    check = _option_chain_leg_balance_check(conn, "KOSPI200", now)
    assert check.status == "warning"
    assert "풋 조회만" in check.detail


def test_leg_balance_check_warns_when_call_side_mostly_failing():
    # 대칭 방향(콜만 실패)도 똑같이 잡아야 한다.
    now = datetime(2026, 7, 20, 7, 30)
    conn = _FakeHealthConnection({"leg_balance_rows": [("C", 2), ("P", 17)]})
    check = _option_chain_leg_balance_check(conn, "KOSPI200", now)
    assert check.status == "warning"
    assert "콜 조회만" in check.detail


def test_leg_balance_check_not_gated_by_trading_hours():
    # 다른 헬스체크(_freshness_check)와 달리 장중 여부로 게이팅하지 않는다 — 이 문제가 실제로
    # 발견된 시각도 07:30 장전이었다.
    weekend = datetime(2026, 7, 18, 10, 0)  # 토요일
    conn = _FakeHealthConnection({"leg_balance_rows": [("C", 18), ("P", 3)]})
    check = _option_chain_leg_balance_check(conn, "KOSPI200", weekend)
    assert check.status == "warning"


def test_leg_balance_check_handles_query_error():
    conn = _BrokenHealthConnection()
    check = _option_chain_leg_balance_check(conn, "KOSPI200", datetime(2026, 7, 20, 7, 30))
    assert check.status == "warning"
    assert conn.rollback_calls == 1


# --- _cbot_status_check --------------------------------------------------------------------------

def test_cbot_status_check_info_when_no_macro_snapshot_yet():
    conn = _FakeHealthConnection({"macro_row": None})
    check = _cbot_status_check(conn)
    assert check.status == "info"
    assert "매크로 스냅샷" in check.detail


def test_cbot_status_check_info_when_zn_front_still_null():
    conn = _FakeHealthConnection(
        {
            "macro_row": (
                datetime(2026, 7, 20, 9, 5), 17.5, 17.8, 0.017, 6.78, 4.5, 1352.0,
                None, None, None, None, None, None,
            )
        }
    )
    check = _cbot_status_check(conn)
    assert check.status == "info"
    assert "미승인" in check.detail


def test_cbot_status_check_ok_when_zn_front_from_kis():
    conn = _FakeHealthConnection(
        {
            "macro_row": (
                datetime(2026, 7, 20, 9, 5), 17.5, 17.8, 0.017, 6.78, 4.5, 1352.0,
                110.25, "kis", None, None, None, None,
            )
        }
    )
    check = _cbot_status_check(conn)
    assert check.status == "ok"
    assert "110.25" in check.detail


def test_cbot_status_check_info_when_zn_front_from_yfinance_fallback():
    # 2026-07-20: CME|CBOT가 KIS 유료 항목(월 228.8불)이라 미구독 상태 — zn_front가
    # yfinance 폴백값이면 실제 CBOT 승인처럼 "ok"로 보이면 안 되고, 폴백 사용 중임을 알려야 한다.
    conn = _FakeHealthConnection(
        {
            "macro_row": (
                datetime(2026, 7, 20, 9, 5), 17.5, 17.8, 0.017, 6.78, 4.5, 1352.0,
                108.50, "yfinance_fallback", None, None, None, None,
            )
        }
    )
    check = _cbot_status_check(conn)
    assert check.status == "info"
    assert "폴백" in check.detail
    assert "108.50" in check.detail


# --- _schema_integrity_check ----------------------------------------------------------------------

def test_schema_integrity_check_ok_when_all_columns_present():
    # 2026-07-21: db.macro_snapshot_columns()(코드가 실제로 쓰는 컬럼 목록)와 라이브 DB의
    # information_schema.columns를 대조 — 전부 있으면 ok.
    rows = [(c,) for c in db.macro_snapshot_columns()]
    conn = _FakeHealthConnection({"schema_columns_rows": rows})
    check = _schema_integrity_check(conn)
    assert check.status == "ok"


def test_schema_integrity_check_warns_when_migration_not_applied_live():
    # 2026-07-21 실측 그대로 재현: 마이그레이션 010/011이 라이브 DB에 미적용돼
    # zn_front_source/usdkrw/es_front/es_front_source/move_index/move_index_source 6개
    # 컬럼이 빠진 상태.
    present = {
        "timestamp", "vix_front", "vix_next", "vix_term_structure", "usdcnh", "us10y_yield",
        "quality_flag", "zn_front",
    }
    rows = [(c,) for c in present]
    conn = _FakeHealthConnection({"schema_columns_rows": rows})
    check = _schema_integrity_check(conn)
    assert check.status == "warning"
    assert "usdkrw" in check.detail
    assert "es_front" in check.detail
    assert "zn_front_source" in check.detail


def test_schema_integrity_check_handles_query_error():
    conn = _BrokenHealthConnection()
    check = _schema_integrity_check(conn)
    assert check.status == "warning"
    assert conn.rollback_calls == 1


# --- _fossil_data_check --------------------------------------------------------------------------

def test_fossil_data_check_ok_when_clean():
    now = datetime(2026, 7, 20, 10, 0)
    conn = _FakeHealthConnection({"fossil_series_rows": [], "legacy_symbol_count_row": (0,)})
    check = _fossil_data_check(conn, "KOSPI200", now)
    assert check.status == "ok"


def test_fossil_data_check_warning_when_fossil_series_found():
    now = datetime(2026, 7, 20, 10, 0)
    conn = _FakeHealthConnection({"fossil_series_rows": [("weekly",)], "legacy_symbol_count_row": (0,)})
    check = _fossil_data_check(conn, "KOSPI200", now)
    assert check.status == "warning"
    assert "weekly" in check.detail


# --- _regime_stability_check -----------------------------------------------------------------------

def test_regime_stability_check_info_when_no_data_today():
    conn = _FakeHealthConnection({"regime_stability_row": (0, 0)})
    check = _regime_stability_check(conn, datetime(2026, 7, 20, 10, 0))
    assert check.status == "info"
    assert "데이터 없음" in check.detail


def test_regime_stability_check_reports_percentage():
    conn = _FakeHealthConnection({"regime_stability_row": (0, 337)})
    check = _regime_stability_check(conn, datetime(2026, 7, 20, 10, 0))
    assert check.status == "info"
    assert "0% 안정" in check.detail
    assert "0/337" in check.detail


# --- _regime_fit_progress_check (§5-7 "20영업일 도달 카운트다운") -----------------------------------

def test_regime_fit_progress_check_info_when_no_data_yet():
    conn = _FakeHealthConnection({"regime_fit_progress_row": (0, 0)})
    check = _regime_fit_progress_check(conn, "KOSPI200")
    assert check.status == "info"
    assert "아직" in check.detail


def test_regime_fit_progress_check_reports_progress_and_eta():
    # 2026-07-19(§5-7): 8,000행 목표 중 4,000행이 10영업일 만에 쌓였다면 하루 평균 400행 —
    # 남은 4,000행은 약 10영업일 더 걸릴 것으로 추정돼야 한다.
    conn = _FakeHealthConnection({"regime_fit_progress_row": (4000, 10)})
    check = _regime_fit_progress_check(conn, "KOSPI200")
    assert check.status == "info"
    assert "4,000/8,000행" in check.detail
    assert "10/20영업일" in check.detail
    assert "10영업일 남음" in check.detail


def test_regime_fit_progress_check_ok_when_target_reached():
    conn = _FakeHealthConnection({"regime_fit_progress_row": (8500, 21)})
    check = _regime_fit_progress_check(conn, "KOSPI200")
    assert check.status == "ok"
    assert "fit_regime_engine.py 실행 가능" in check.detail


def test_regime_fit_progress_check_handles_query_error():
    conn = _BrokenHealthConnection()
    check = _regime_fit_progress_check(conn, "KOSPI200")
    assert check.status == "warning"
    assert conn.rollback_calls == 1


# --- _shutdown_reliability_check (§5-3 "종료 신뢰성 배지") -----------------------------------------

def test_shutdown_reliability_check_info_when_no_record_yet():
    # 마이그레이션 013 적용 전이거나, log_marketclose_stop.py가 아직 한 번도 기록한 적 없는 상태.
    conn = _FakeHealthConnection({"shutdown_check_row": None})
    check = _shutdown_reliability_check(conn)
    assert check.status == "info"
    assert "기록 없음" in check.detail


def test_shutdown_reliability_check_ok_when_no_processes_remained():
    checked_at = datetime(2026, 7, 21, 15, 45, 5)
    conn = _FakeHealthConnection({"shutdown_check_row": (checked_at, 0)})
    check = _shutdown_reliability_check(conn)
    assert check.status == "ok"
    assert "정상 종료" in check.detail


def test_shutdown_reliability_check_warns_when_processes_remained():
    # 2026-07-21 §3-1 실측 재현: taskkill이 "No tasks running"만 남기고도 프로세스가 살아있었음.
    checked_at = datetime(2026, 7, 21, 15, 45, 5)
    conn = _FakeHealthConnection({"shutdown_check_row": (checked_at, 2)})
    check = _shutdown_reliability_check(conn)
    assert check.status == "warning"
    assert "2개 잔존" in check.detail


def test_shutdown_reliability_check_handles_query_error():
    conn = _BrokenHealthConnection()
    check = _shutdown_reliability_check(conn)
    assert check.status == "warning"
    assert conn.rollback_calls == 1


# --- get_health_summary (오케스트레이션) ------------------------------------------------------------

def test_get_health_summary_runs_all_checks_in_order(monkeypatch):
    calls: list[str] = []

    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    def make_check(name):
        def _check(*args, **kwargs):
            calls.append(name)
            return HealthCheck(name, "ok", "테스트")
        return _check

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.dashboard.data_source._option_chain_freshness_check", make_check("option_chain"))
    monkeypatch.setattr("mahdi.dashboard.data_source._futures_freshness_check", make_check("futures"))
    monkeypatch.setattr("mahdi.dashboard.data_source._option_chain_leg_balance_check", make_check("leg_balance"))
    monkeypatch.setattr("mahdi.dashboard.data_source._cbot_status_check", make_check("cbot"))
    monkeypatch.setattr("mahdi.dashboard.data_source._schema_integrity_check", make_check("schema"))
    monkeypatch.setattr("mahdi.dashboard.data_source._fossil_data_check", make_check("fossil"))
    monkeypatch.setattr("mahdi.dashboard.data_source._regime_stability_check", make_check("regime"))
    monkeypatch.setattr("mahdi.dashboard.data_source._regime_fit_progress_check", make_check("regime_fit_progress"))
    monkeypatch.setattr("mahdi.dashboard.data_source._shutdown_reliability_check", make_check("shutdown"))

    result = get_health_summary()

    assert calls == [
        "option_chain", "futures", "leg_balance", "cbot", "schema", "fossil",
        "regime", "regime_fit_progress", "shutdown",
    ]
    assert [c.label for c in result] == calls


def test_get_health_summary_falls_back_to_single_warning_when_db_unavailable(monkeypatch):
    @contextmanager
    def broken_connection(settings=None):
        raise ConnectionError("DB 없음")
        yield  # pragma: no cover

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", broken_connection)

    result = get_health_summary()

    assert len(result) == 1
    assert result[0].status == "warning"
