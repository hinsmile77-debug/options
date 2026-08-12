from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

import pytest

from mahdi.data import db
from mahdi.dashboard.data_source import (
    DEFAULT_MODEL_PATH,
    FLOW_RADAR_ROW_CAP,
    FLOW_RADAR_WINDOW_MINUTES,
    _rest_demand_check,
    _backoff_headroom_check,
    _entry_cutoff_check,
    _monthly_coverage_check,
    _overrun_count_check,
    HealthCheck,
    _cbot_status_check,
    _atm_roll_churn_check,
    _fossil_data_check,
    _macro_freshness_check,
    _freshness_check,
    _futures_freshness_check,
    _is_trading_hours,
    _market_halt_check,
    _option_chain_freshness_check,
    _option_chain_leg_balance_check,
    _rate_limiter_health_check,
    _regime_fit_progress_check,
    _regime_stability_check,
    _schema_integrity_check,
    _shutdown_reliability_check,
    _synthetic_snapshot,
    get_account_status_view,
    get_health_summary,
    get_latest_decision_context,
    get_market_halt_status,
    get_slack_alerts_enabled,
    load_snapshot,
    record_cockpit_startup,
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


_SPOT_TS = datetime(2026, 7, 6, 9, 31)

_BASE_RESPONSES = {
    # 마지막 칸은 is_warmup(마이그레이션 025, 2026-08-05 P1-7) — prob_vector가 학습된 확률인지
    # warmup_fallback()의 one-hot 상수인지. 화면이 그 둘을 같게 그리면 안 된다.
    "regime": [(_SPOT_TS, 2, [0.1] * 8, None, False, False)],
    # 2026-08-05(P1-6): `latest_underlying_spot_row()`가 값과 **관측 시각**을 함께 읽는다 —
    # 화면이 "이 현재가가 언제 것인지"를 표시할 수 있어야 하기 때문(그 함수 docstring 참고).
    "spot": [(1333.77, _SPOT_TS)],
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
        "regime": [(ts, 2, [0.1] * 8, None, False, False)],
        "chain": [
            (1340.0, "C", 363, 0.9, 0.0047, 1000.0, date(2026, 7, 9), ts, 0.72),
            (1340.0, "P", 200, 0.85, 0.0040, -800.0, date(2026, 7, 9), ts, 0.72),
        ],
        "investor_flow": [(-150.0, 250.0, -40.0)],
    }

    @contextmanager
    def fake_get_connection(settings=None):
        yield _FakeConnection(responses)

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", fake_get_connection)
    # 2026-08-05(P0-2): 체인 레그가 `signal_book_legs()`를 거치면서 만기 경과분이 배제되므로,
    # 이 테스트의 만기(7/9)가 미래가 되도록 기준일을 고정한다(고정 안 하면 실행 날짜에 따라 결과가 바뀐다).
    monkeypatch.setattr("mahdi.dashboard.data_source.db.local_now", lambda: ts)

    snap = load_snapshot()

    assert snap.is_live is True
    assert snap.spot == 1333.77  # market_raw_1m의 옵션 체결가가 아니라 진짜 지수 스팟
    assert len(snap.chain) == 1  # 같은 행사가의 콜/풋이 하나로 합산됨
    assert snap.chain[0].strike == 1340.0
    assert snap.chain[0].gex == pytest.approx(200.0)  # 1000.0 + (-800.0)
    assert snap.gex_expiry == date(2026, 7, 9)
    # 2026-08-05(P1-6): 값만으로는 장전 전일 종가인지 장중 실시간 지수인지 구분할 수 없다.
    assert snap.spot_asof == ts
    assert snap.foreign_net == -150.0
    assert snap.institution_net == 250.0
    assert snap.individual_net == -40.0


def test_load_snapshot_carries_the_warmup_flag_from_the_regime_row(monkeypatch):
    """2026-08-05 P1-7 — `RegimeState.is_warmup`은 07-10부터 필드로 있었지만 DB에 저장된 적이 없어
    COCKPIT이 one-hot 상수와 학습된 사후확률을 구분할 수 없었다(마이그레이션 025)."""
    ts = datetime(2026, 8, 5, 12, 12)
    warmup_prob = [0.0] * 8
    warmup_prob[2] = 1.0
    responses = {
        **_BASE_RESPONSES,
        "regime": [(ts, 2, warmup_prob, None, False, True)],
        "spot": [(1042.85, ts)],
    }

    @contextmanager
    def fake_get_connection(settings=None):
        yield _FakeConnection(responses)

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.dashboard.data_source.db.local_now", lambda: ts)

    assert load_snapshot().regime_is_warmup is True


def test_load_snapshot_keeps_unknown_warmup_flag_as_none(monkeypatch):
    """마이그레이션 025 이전 행은 NULL — bool()로 뭉개면 "학습된 판정"이라고 거짓말하게 된다."""
    ts = datetime(2026, 8, 5, 12, 12)
    responses = {
        **_BASE_RESPONSES,
        "regime": [(ts, 2, [0.125] * 8, None, False, None)],
        "spot": [(1042.85, ts)],
    }

    @contextmanager
    def fake_get_connection(settings=None):
        yield _FakeConnection(responses)

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.dashboard.data_source.db.local_now", lambda: ts)

    assert load_snapshot().regime_is_warmup is None


def test_load_snapshot_gamma_map_uses_only_the_monthly_book_like_the_engine(monkeypatch):
    """2026-08-05 P0-2 회귀 — **화면과 판단이 같은 체인을 봐야 한다.**

    관측 루프는 08-04 Fix#5로 먼슬리 한 북만 쓰는데(`signal_book_legs`), COCKPIT만 그 이전
    상태(세 북 평탄화)로 남아 있었다. 잔존 1일 위클리는 감마가 압도적이라 GEX 프로파일을
    사실상 그 북이 지배했고, 화면에는 그 사실을 알릴 표시조차 없었다.
    """
    ts = datetime(2026, 8, 5, 12, 12)
    monthly, weekly_thu, weekly_mon = date(2026, 8, 13), date(2026, 8, 6), date(2026, 8, 10)
    responses = {
        **_BASE_RESPONSES,
        "regime": [(ts, 2, [0.1] * 8, None, False, False)],
        "chain": [
            (1045.0, "C", 100.0, 0.60, 0.02, 1_000.0, monthly, ts, 0.5),
            (1047.5, "P", 120.0, 0.62, 0.03, -400.0, monthly, ts, 0.5),
            # 위클리 두 북 — 화면에 섞이면 안 된다(만기 Pinning은 별도 신호).
            (1045.0, "C", 900.0, 0.90, 0.09, 90_000.0, weekly_thu, ts, 0.5),
            (1050.0, "C", 800.0, 0.85, 0.08, 80_000.0, weekly_mon, ts, 0.5),
        ],
    }

    @contextmanager
    def fake_get_connection(settings=None):
        yield _FakeConnection(responses)

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.dashboard.data_source.db.local_now", lambda: ts)

    snap = load_snapshot()

    assert snap.gex_expiry == monthly
    # 막대는 먼슬리 두 행사가뿐 — 위클리의 거대한 GEX(+90,000/+80,000)가 섞이면 안 된다.
    assert [(c.strike, c.gex) for c in snap.chain] == [(1045.0, 1_000.0), (1047.5, -400.0)]


def test_load_snapshot_omits_gamma_wall_when_top_exposure_is_zero(monkeypatch):
    """2026-08-05 P0-3 회귀 — OI가 전부 0이면 `gamma_walls()`도 1등을 돌려주지만 그건 월이 아니다.

    관측 루프는 `walls[0][1] > 0`으로 막고 있었는데 COCKPIT은 노출값을 버리고 행사가만 취해
    가드가 없었다 — 값이 없는 자리에 선을 긋는 것은 `find_gamma_flip()`이 전 구간 0인 곡선에서
    허수 flip을 냈던 것과 같은 종류의 결함이다.
    """
    ts = datetime(2026, 8, 5, 12, 12)
    expiry = date(2026, 8, 13)
    responses = {
        **_BASE_RESPONSES,
        "regime": [(ts, 2, [0.1] * 8, None, False, False)],
        "chain": [
            (1045.0, "C", 0.0, 0.60, 0.02, 0.0, expiry, ts, 0.5),  # oi=0 -> 노출 0
            (1047.5, "P", 0.0, 0.62, 0.03, 0.0, expiry, ts, 0.5),
        ],
    }

    @contextmanager
    def fake_get_connection(settings=None):
        yield _FakeConnection(responses)

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.dashboard.data_source.db.local_now", lambda: ts)

    snap = load_snapshot()

    assert snap.gamma_walls == []


def test_load_snapshot_reports_a_single_gamma_wall_matching_the_engine(monkeypatch):
    """엔진은 `gamma_walls(top_n=1)` — 행사가 창이 ATM±2(5개)뿐이라 3개를 그리면 창의 양 끝을
    가리키는 것에 가까워지고, 무엇보다 화면의 월과 판단이 쓰는 월이 달라진다."""
    ts = datetime(2026, 8, 5, 12, 12)
    expiry = date(2026, 8, 13)
    responses = {
        **_BASE_RESPONSES,
        "regime": [(ts, 2, [0.1] * 8, None, False, False)],
        "spot": [(1045.0, ts)],
        "chain": [
            (1040.0, "C", 10.0, 0.60, 0.01, 100.0, expiry, ts, 0.5),
            (1045.0, "C", 900.0, 0.60, 0.09, 900.0, expiry, ts, 0.5),  # 압도적 1등
            (1050.0, "P", 20.0, 0.62, 0.02, -200.0, expiry, ts, 0.5),
        ],
    }

    @contextmanager
    def fake_get_connection(settings=None):
        yield _FakeConnection(responses)

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.dashboard.data_source.db.local_now", lambda: ts)

    snap = load_snapshot()

    assert snap.gamma_walls == [1045.0]


def test_load_snapshot_splits_futures_and_option_flow_series(monkeypatch):
    # 2026-07-06 발견: 선물이 WS 구독 덕에 거의 매분 체결돼 "가장 최근 활동"만으로 대표 종목을
    # 뽑으면 옵션이 영원히 안 뽑힌다 — Flow Radar는 선물/옵션 계열을 각각 따로 조회해야 한다.
    # 선물 식별은 active_futures_symbol 레지스트리로 명시적으로 한다(vpin 유무 휴리스틱은
    # 옵션에도 VPIN을 적용하면서 깨졌음).
    ts = datetime(2026, 7, 6, 9, 31)
    responses = {
        **_BASE_RESPONSES,
        "regime": [(ts, 2, [0.1] * 8, None, False, False)],
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


def test_load_snapshot_bounds_both_flow_series_by_the_same_time_window(monkeypatch):
    """2026-08-05 P2-9 회귀 — 종전에는 `LIMIT 60`(행 상한)이라 두 계열의 실효 창이 달랐다.

    선물은 거의 매분 체결돼 60행 ≈ 60분이었지만 거래가 뜸한 옵션은 60행이 몇 시간에 걸쳤고,
    x축만 선물 창으로 강제돼 **창 밖 점이 보이지도 않으면서 y축만 잡아늘였다**(08-05 실측:
    OFI 축 −30까지인데 보이는 값은 −6~+8, 가격 축 26까지인데 보이는 최대 21).
    """
    ts = datetime(2026, 8, 5, 12, 12)
    responses = {
        **_BASE_RESPONSES,
        "regime": [(ts, 2, [0.1] * 8, None, False, False)],
        "spot": [(1042.85, ts)],
        "futures_symbol": [("A01609",)],
        "futures_symbol_value": "A01609",
        "futures_rows": [(ts, 1046.3, 5.0, 1046.2, 0.5)],
        "option_symbol": [("B09F9WA21",)],
        "option_rows": [(ts, 19.5, 2.0, 19.45, 0.47)],
    }
    conn = _FakeConnection(responses)

    @contextmanager
    def fake_get_connection(settings=None):
        yield conn

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.dashboard.data_source.db.local_now", lambda: ts)

    load_snapshot()

    series_queries = [
        (q, p) for q, p in conn.query_log
        if "market_raw_1m" in q and "close, ofi, microprice, vpin" in q
    ]
    assert len(series_queries) == 2  # 선물 + 옵션
    expected_cutoff = ts - timedelta(minutes=FLOW_RADAR_WINDOW_MINUTES)
    for query, params in series_queries:
        assert "timestamp >=" in query, "행 상한이 아니라 시간 창으로 잘라야 한다"
        # 룩백 기준은 datetime.now()가 아니라 스냅샷 시각(regime_state.timestamp)이어야
        # 리플레이/재현 시나리오에서도 창이 실제 데이터 시각 기준으로 맞는다.
        assert params[1] == expected_cutoff
        assert params[2] == FLOW_RADAR_ROW_CAP  # 창 안에서도 행 폭증 방어는 유지


def test_load_snapshot_picks_option_flow_symbol_by_windowed_volume_with_deterministic_tiebreak(monkeypatch):
    # 2026-07-06 위클리 북 추가 후 실측: 여러 위클리 종목이 같은 1분봉 timestamp로 동시에 찍혀서
    # "ORDER BY max(timestamp) DESC"만 쓰면 동률 처리가 비결정적이라 COCKPIT 리런(10초)마다
    # 뽑히는 종목이 계속 바뀌었다(차트가 매번 다른 종목으로 바뀌어 보임). 최근 룩백 윈도 누적
    # 거래량 + symbol 오름차순 타이브레이커로 쿼리가 바뀌었는지 검증한다.
    ts = datetime(2026, 7, 6, 9, 31)
    responses = {
        **_BASE_RESPONSES,
        "regime": [(ts, 2, [0.1] * 8, None, False, False)],
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
        "regime": [(ts, 2, [0.1] * 8, None, False, False)],
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
        "regime": [(ts, 2, [0.1] * 8, None, False, False)],
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
        "regime": [(ts, 2, [0.1] * 8, None, False, False)],
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
        # 2026-08-05(P1-4): 매크로 신선도 배지는 최신 시각만 본다 — LOCF 분기보다 앞에 둔다.
        elif "macro_snapshot_5m" in query and "MAX(timestamp)" in query:
            self._kind, self._value = "one", self._responses.get("macro_max_timestamp", (None,))
        # 2026-08-05(P1-4): LOCF 쿼리가 timestamp를 맨 앞에 함께 읽으므로 폴백 행도 한 칸 길어졌다
        # (값의 관측 시각을 호출측이 표시할 수 있어야 한다 — db.latest_macro_snapshot 참고).
        elif "macro_snapshot_5m" in query and "us10y_yield IS NOT NULL" in query:
            self._kind, self._value = "one", self._responses.get("macro_fallback_row")
        elif "macro_snapshot_5m" in query and "usdkrw IS NOT NULL" in query:
            self._kind, self._value = "one", self._responses.get("usdkrw_fallback_row")
        # 2026-07-31: 매크로 항목별 갱신 주기 분리로 zn_front/move_index도 LOCF 대상이 됐다
        # (값+출처를 같은 행에서 가져오므로 timestamp 포함 3컬럼 튜플). 이 분기가 없으면 아래
        # 범용 분기가 13컬럼짜리 macro_row를 돌려줘 언패킹이 깨진다.
        elif "macro_snapshot_5m" in query and "zn_front IS NOT NULL" in query:
            self._kind, self._value = "one", self._responses.get("zn_fallback_row", (None, None, None))
        elif "macro_snapshot_5m" in query and "move_index IS NOT NULL" in query:
            self._kind, self._value = "one", self._responses.get("move_fallback_row", (None, None, None))
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
        elif "rate_limiter_status_log" in query:
            self._kind, self._value = "one", self._responses.get("rate_limiter_status_row")
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


# --- _macro_freshness_check (2026-08-05 P1-4) -------------------------------------------------
#
# 매크로는 신선도를 보는 배지가 하나도 없던 유일한 데이터 경로였다. CBOT 배지가 같은 스냅샷을
# 읽지만 `zn_front_source`만 보므로, 폴러가 며칠 죽어 있어도 "yfinance 폴백 사용 중" 파란불이
# 그대로 뜬다(위 테스트가 정확히 그 상태를 고정하고 있다).


def test_macro_freshness_check_ok_within_signal_path_threshold():
    now = datetime(2026, 8, 5, 12, 12)
    conn = _FakeHealthConnection({"macro_max_timestamp": (datetime(2026, 8, 5, 12, 5),)})

    check = _macro_freshness_check(conn, now)

    assert check.status == "ok"
    assert "08-05 12:05" in check.detail


def test_macro_freshness_check_warns_when_signal_path_would_drop_the_vix_signal():
    now = datetime(2026, 8, 5, 12, 12)
    conn = _FakeHealthConnection({"macro_max_timestamp": (datetime(2026, 8, 5, 11, 50),)})

    check = _macro_freshness_check(conn, now)

    assert check.status == "warning"
    # 배지와 판단이 같은 임계를 쓴다는 사실이 문구에 드러나야 한다 — 다르면 어느 쪽을 믿을지 모른다.
    assert "VIX 기간구조" in check.detail


def test_macro_freshness_check_is_info_when_never_polled():
    conn = _FakeHealthConnection({"macro_max_timestamp": (None,)})
    assert _macro_freshness_check(conn, datetime(2026, 8, 5, 12, 12)).status == "info"


def test_macro_freshness_check_handles_query_error():
    conn = _BrokenHealthConnection()
    check = _macro_freshness_check(conn, datetime(2026, 8, 5, 12, 12))
    assert check.status == "warning"
    assert check.detail == "조회 실패"
    assert conn.rollback_calls == 1


# --- _atm_roll_churn_check (2026-08-05 P2-12) -------------------------------------------------
#
# 08-03에 "창이 하루 종일 안 움직였다"를 고친 뒤 그 반대편 비용(창이 너무 자주 움직인다)이
# 계측되지 않았다. 롤 1회마다 창을 벗어난 종목의 1분봉이 끊기므로 이 값은 관측 연속성의
# 선행지표다 — P0-1에서 정직하게 그리기 시작한 Flow Radar 공백의 **원인 쪽**이다.
_NOW = datetime(2026, 8, 5, 12, 12)


def _ws_status(atm_roll_count: int, dropped: int | None = 0) -> dict:
    return {
        "updated_at": _NOW, "connected_since": datetime(2026, 8, 5, 7, 30),
        "last_message_at": _NOW, "reconnect_count_today": 0,
        "market_op_subscribed_at": datetime(2026, 8, 5, 7, 30),
        "atm_roll_count_today": atm_roll_count,
        # 2026-08-07 고도화#2 — 판정 축이 여기로 옮겨왔다(마이그레이션 028).
        "atm_roll_dropped_subs_today": dropped,
    }


def test_atm_roll_churn_check_ok_below_threshold(monkeypatch):
    monkeypatch.setattr("mahdi.dashboard.data_source.db.latest_ws_status", lambda conn: _ws_status(6))

    check = _atm_roll_churn_check(object(), _NOW)

    assert check.status == "ok"
    assert check.group == "관측 품질"
    assert "6회" in check.detail


def test_atm_roll_churn_check_no_longer_warns_on_roll_count_alone(monkeypatch):
    """2026-08-07 고도화#2 — 롤 횟수는 시장 변동성의 함수라 통제 대상이 아니다.

    08-05는 77회를 "중간점 근처에서 오간 결과"로 읽고 임계 20을 걸었다. 08-07에 롤 76회를
    **전수 검산**하니 전부 히스테리시스 임계(2.5 x 0.75 = 1.875p)를 정당하게 넘은 것이었다 —
    그날 지수가 40p를 움직였다. 통제 못 하는 값에 임계를 걸면 상시 경고가 되고, 상시 경고는
    곧 안 읽는 경고다.
    """
    monkeypatch.setattr("mahdi.dashboard.data_source._live_book_count", lambda conn: 2)
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db.latest_ws_status", lambda conn: _ws_status(77, dropped=0)
    )

    check = _atm_roll_churn_check(object(), _NOW)

    assert check.status == "ok"
    assert "77회" in check.detail
    assert "0건" in check.detail


def test_atm_roll_churn_check_warns_when_the_pool_is_not_doing_its_job(monkeypatch):
    """판정 축은 끊긴 구독의 **절대 건수가 아니라 롤·북당 비율**이다.

    08-07 커밋 직후 선물 1분봉 300개(스팟 41.75p = 행사가 17칸 이동)를 실제 풀에 흘려보내
    절대 건수 임계 4가 25~100배 틀렸다는 것을 확인했다 — 끊긴 구독은 스팟 이동거리의 함수다.
    풀 없이 돌 때의 값은 북 수와 무관하게 롤·북당 **2.42**로 고정된다.
    """
    monkeypatch.setattr("mahdi.dashboard.data_source._live_book_count", lambda conn: 2)
    monkeypatch.setattr(
        # 롤 96회 x 2북 x 2.42 ≈ 464 — 유지 풀이 없을 때의 값이다.
        "mahdi.dashboard.data_source.db.latest_ws_status", lambda conn: _ws_status(96, dropped=464)
    )

    check = _atm_roll_churn_check(object(), _NOW)

    assert check.status == "warning"
    assert "2.42" in check.detail


def test_atm_roll_churn_check_is_ok_when_the_pool_absorbs_the_round_trips(monkeypatch):
    """08-07 리플레이 실측: 2북 유지 풀 ON(예약 2)은 롤 96회에 끊긴 구독 97건 = 롤·북당 0.51."""
    monkeypatch.setattr("mahdi.dashboard.data_source._live_book_count", lambda conn: 2)
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db.latest_ws_status", lambda conn: _ws_status(96, dropped=97)
    )

    check = _atm_roll_churn_check(object(), _NOW)

    assert check.status == "ok"
    assert "0.51" in check.detail
    # 롤 횟수가 임계(20)를 크게 넘어도 초록인 것이 이 고도화의 목표 상태다.
    assert "96회" in check.detail


def test_atm_roll_churn_check_holds_the_ratio_verdict_without_a_book_count(monkeypatch):
    """북 수를 못 읽으면 비율을 지어내지 않는다 — 판정을 보류한다."""
    monkeypatch.setattr("mahdi.dashboard.data_source._live_book_count", lambda conn: 0)
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db.latest_ws_status", lambda conn: _ws_status(96, dropped=97)
    )

    check = _atm_roll_churn_check(object(), _NOW)

    assert check.status == "ok"
    assert "보류" in check.detail


def test_atm_roll_churn_check_falls_back_to_roll_count_before_migration_028(monkeypatch):
    """대가를 못 재는 구간에서는 대리 지표(롤 횟수)로라도 판정한다 — 침묵하지 않는다."""
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db.latest_ws_status", lambda conn: _ws_status(77, dropped=None)
    )

    check = _atm_roll_churn_check(object(), _NOW)

    assert check.status == "warning"
    assert "집계 전" in check.detail


def test_atm_roll_churn_check_says_not_counted_yet_instead_of_zero(monkeypatch):
    """2026-08-05 배포 당일 실측 — 컬럼을 라이브에 넣은 직후 관측 루프는 아직 구 코드였다.

    처음엔 `NOT NULL DEFAULT 0`으로 만들었더니 그날 실제로 77회 롤한 날에 배지가 **"0회"** 를
    표시했다(= "롤이 없었다"는 거짓말). 컬럼을 nullable로 바꾸고 여기서 "집계 전"이라 쓴다.
    """
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db.latest_ws_status", lambda conn: _ws_status(None)
    )

    check = _atm_roll_churn_check(object(), _NOW)

    assert check.status == "info"
    assert "집계 전" in check.detail
    assert "0회" not in check.detail


def test_atm_roll_churn_check_is_info_without_ws_status(monkeypatch):
    # 관측 루프 미기동 판정은 `_ws_liveness_check`의 몫 — 여기서 중복 경고하지 않는다.
    monkeypatch.setattr("mahdi.dashboard.data_source.db.latest_ws_status", lambda conn: None)
    assert _atm_roll_churn_check(object(), _NOW).status == "info"


def test_atm_roll_churn_check_handles_query_error(monkeypatch):
    def boom(conn):
        raise RuntimeError("DB 오류")

    monkeypatch.setattr("mahdi.dashboard.data_source.db.latest_ws_status", boom)
    conn = _BrokenHealthConnection()

    check = _atm_roll_churn_check(conn, _NOW)

    assert check.status == "warning"
    assert conn.rollback_calls == 1


# --- _schema_integrity_check ----------------------------------------------------------------------

def test_schema_integrity_check_ok_when_all_columns_present():
    # 2026-07-21: db.macro_snapshot_columns()(코드가 실제로 쓰는 컬럼 목록)와 라이브 DB의
    # information_schema.columns를 대조 — 전부 있으면 ok.
    # 2026-08-05(P1-7): 대상 테이블이 regime_state로 늘었다. 가짜 커서는 테이블 구분 없이 같은
    # 행 목록을 돌려주므로 두 테이블의 컬럼을 합쳐 넣는다.
    # 2026-08-11(고도화 B): signal_decisions가 추가됐다 — 매분 INSERT라 파급이 가장 크다.
    rows = [
        (c,)
        for c in (
            *db.macro_snapshot_columns(),
            *db.regime_state_columns(),
            *db.ws_status_columns(),
            *db.signal_decision_columns(),
        )
    ]
    conn = _FakeHealthConnection({"schema_columns_rows": rows})
    check = _schema_integrity_check(conn)
    assert check.status == "ok"


def test_schema_integrity_check_warns_when_regime_state_migration_missing():
    """2026-08-05 P1-7 — 마이그레이션 025(is_warmup) 미적용이면 레짐 적재가 실패하고 COCKPIT은
    조회 실패로 **합성 폴백**에 빠진다. 010/011로 실제로 겪은 사고와 같은 형태라 배지가 잡아야 한다."""
    rows = [
        (c,)
        for c in (
            *db.macro_snapshot_columns(),
            *(c for c in db.regime_state_columns() if c != "is_warmup"),
            *db.ws_status_columns(),
        )
    ]
    conn = _FakeHealthConnection({"schema_columns_rows": rows})

    check = _schema_integrity_check(conn)

    assert check.status == "warning"
    assert "regime_state" in check.detail
    assert "is_warmup" in check.detail


def test_schema_integrity_check_warns_when_ws_status_migration_missing():
    """2026-08-05 P2-12 — 마이그레이션 026 미적용이면 WS 하트비트 기록이 실패하고,
    `poll_ws_heartbeat`가 그 예외를 삼키므로(관측은 계속돼야 하니 옳다) WS 배지 3종이 조용히 멈춘다."""
    rows = [
        (c,)
        for c in (
            *db.macro_snapshot_columns(),
            *db.regime_state_columns(),
            *(c for c in db.ws_status_columns() if c != "atm_roll_count_today"),
        )
    ]
    conn = _FakeHealthConnection({"schema_columns_rows": rows})

    check = _schema_integrity_check(conn)

    assert check.status == "warning"
    assert "ws_status" in check.detail
    assert "atm_roll_count_today" in check.detail


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


def test_regime_fit_progress_check_tells_you_to_train_only_when_no_model_exists(tmp_path, monkeypatch):
    """2026-08-11 Fix#9 — **행 수만 보고 「실행 가능」이라고 쓰면 안 된다.**

    08-11은 모델이 08-10 16:56에 이미 배포돼 라이브로 predict()를 돌린 날이었는데, 이 배지는
    종일 *"fit_regime_engine.py 실행 가능"* 이라고 띄웠다 — 이미 한 일을 시키고 있었다.
    """
    monkeypatch.setattr("mahdi.dashboard.data_source.PROJECT_ROOT", tmp_path)
    conn = _FakeHealthConnection({"regime_fit_progress_row": (8500, 21)})

    # (1) 모델이 없으면 — 실행을 권한다.
    check = _regime_fit_progress_check(conn, "KOSPI200")
    assert check.status == "ok"
    assert "모델 미배포" in check.detail
    assert "fit_regime_engine.py" in check.detail

    # (2) 모델이 있으면 — 다시 학습하라고 하지 않는다.
    model = tmp_path / DEFAULT_MODEL_PATH
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_bytes(b"x")
    check = _regime_fit_progress_check(conn, "KOSPI200")
    assert "모델 배포됨" in check.detail
    assert "실행 가능" not in check.detail


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


# --- _rate_limiter_health_check (§2-1/§4 Fix#4 "레이트리밋 근접도 배지") ---------------------------

def test_rate_limiter_health_check_info_when_no_record_yet():
    conn = _FakeHealthConnection({"rate_limiter_status_row": None})
    check = _rate_limiter_health_check(conn)
    assert check.status == "info"


def test_rate_limiter_health_check_ok_when_no_backoff():
    row = (datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc), 1.0, 0.0)
    conn = _FakeHealthConnection({"rate_limiter_status_row": row})
    check = _rate_limiter_health_check(conn)
    assert check.status == "ok"
    assert "1.00배" in check.detail


def test_rate_limiter_health_check_warns_when_backing_off():
    row = (datetime(2026, 7, 24, 13, 5, tzinfo=timezone.utc), 2.25, 18.7)
    conn = _FakeHealthConnection({"rate_limiter_status_row": row})
    check = _rate_limiter_health_check(conn)
    assert check.status == "warning"
    assert "2.25배" in check.detail
    assert "18.7초" in check.detail


def test_rate_limiter_health_check_handles_query_error():
    conn = _BrokenHealthConnection()
    check = _rate_limiter_health_check(conn)
    assert check.status == "warning"
    assert conn.rollback_calls == 1


# --- 서킷브레이커/거래정지(market_halt) --------------------------------------------------------------

def test_market_halt_check_warns_when_detector_never_recorded_anything(monkeypatch):
    # 2026-07-30(운영점검 §2-4/§4 Fix#4): 관측 루프가 구독 직후 "정상" 행을 반드시 남기므로,
    # 조회 결과가 None이라는 건 "CB가 없었다"가 아니라 감지기가 아예 안 붙었다는 뜻이다 —
    # 예전에는 이걸 "ok/정상"으로 표시해 라이브 검증 불가 상태를 안심 신호로 덮고 있었다.
    monkeypatch.setattr("mahdi.dashboard.data_source.db.latest_market_halt_state", lambda conn: None)
    check = _market_halt_check(object())
    assert check.status == "warning"
    assert "미기록" in check.detail


def test_market_halt_check_ok_with_heartbeat_when_no_transition_yet(monkeypatch):
    # 하트비트만 갱신된 정상 상태(전이 이력 없음 → mkop_cls_code=None) — "직전: ... 해제됨"이
    # 아니라 "관측 루프 갱신 시각"과 "최근 장운영정보 수신 시각"을 **나눠서** 보여줘야 한다
    # (2026-07-31 §2-2: 07-30 설계는 둘을 한 값에 섞어 표시했다).
    monkeypatch.setattr("mahdi.dashboard.data_source.db.local_now", lambda: datetime(2026, 7, 31, 9, 6, 0))
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db.latest_market_halt_state",
        lambda conn: {
            "updated_at": datetime(2026, 7, 31, 9, 5, 0), "is_halted": False,
            "mkop_cls_code": None, "label": "정상", "halted_since": None,
            "last_message_at": datetime(2026, 7, 31, 9, 0, 5),
        },
    )
    # 2026-08-07 Fix#6 — 누적 수신 이력이 **있는** 경우가 이 시나리오다(없으면 아래 "미검증").
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db.market_halt_message_ever_received", lambda conn: True
    )
    check = _market_halt_check(object())
    assert check.status == "ok"
    assert "발동 이력 없음" in check.detail
    assert "09:05:00" in check.detail  # 관측 루프 하트비트
    assert "09:00:05" in check.detail  # 최근 장운영정보 수신


def test_market_halt_check_says_unverified_when_no_message_was_ever_received(monkeypatch):
    """2026-08-07 §A-3 / Fix#6 — 넉 달째 수신 누적 0건인데 배지는 초록이었다.

    구독 성립(`market_op_subscribed_at`)은 "보낸 요청이 받아들여졌다"이지 "데이터가 온다"가
    아니다. 하루치 0건에는 임계를 걸 수 없지만(정상일에도 0~2건) **누적 0건**은 "이 경로가
    살아 있는 것을 한 번도 본 적이 없다"는 다른 사실이다.

    경고가 아니라 정보인 이유: 이상이 있다는 증거도 없다 — 진짜로 CB가 없었을 수 있다.
    """
    monkeypatch.setattr("mahdi.dashboard.data_source.db.local_now", lambda: datetime(2026, 8, 7, 12, 15))
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db.latest_market_halt_state",
        lambda conn: {
            "updated_at": datetime(2026, 8, 7, 12, 14), "is_halted": False,
            "mkop_cls_code": None, "label": "정상", "halted_since": None,
            "last_message_at": None,
        },
    )
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db.market_halt_message_ever_received", lambda conn: False
    )

    check = _market_halt_check(object())

    assert check.status == "info"
    assert "미검증" in check.detail
    assert "누적 0건" in check.detail
    # "정상"이라고 쓰지 않는 것이 이 fix의 전부다.
    assert "정상(발동 이력 없음)" not in check.detail


def test_market_halt_check_still_flags_an_active_halt_regardless_of_history(monkeypatch):
    """미검증 판정이 **실제 발동 중**을 덮으면 안 된다 — 그건 최우선 경고다."""
    monkeypatch.setattr("mahdi.dashboard.data_source.db.local_now", lambda: datetime(2026, 8, 7, 12, 15))
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db.latest_market_halt_state",
        lambda conn: {
            "updated_at": datetime(2026, 8, 7, 12, 14), "is_halted": True,
            "mkop_cls_code": "20", "label": "CB 1단계", "halted_since": datetime(2026, 8, 7, 12, 10),
            "last_message_at": datetime(2026, 8, 7, 12, 10),
        },
    )
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db.market_halt_message_ever_received", lambda conn: False
    )

    check = _market_halt_check(object())

    assert check.status == "warning"
    assert "신규진입 차단" in check.detail


def test_market_halt_check_handles_tz_aware_timestamps_from_postgres(monkeypatch):
    # 2026-07-31 라이브 왕복에서 잡은 결함(단위테스트로는 절대 못 잡는 유형): psycopg는 TIMESTAMPTZ
    # 컬럼을 tz-aware로 돌려주는데 db.local_now()는 naive라, 그대로 빼면 TypeError로 헬스체크
    # 전체가 죽는다. 2026-07-20 `_freshness_check`에서 겪은 것과 정확히 같은 유형이다.
    monkeypatch.setattr("mahdi.dashboard.data_source.db.local_now", lambda: datetime(2026, 7, 31, 15, 44, 0))
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db.latest_market_halt_state",
        lambda conn: {
            "updated_at": datetime(2026, 7, 31, 15, 43, 0, tzinfo=timezone.utc), "is_halted": False,
            "mkop_cls_code": None, "label": "정상", "halted_since": None,
            "last_message_at": datetime(2026, 7, 31, 9, 0, 5, tzinfo=timezone.utc),
        },
    )
    check = _market_halt_check(object())
    assert check.status == "ok"
    assert "15:43:00" in check.detail


def test_market_halt_check_warns_when_heartbeat_is_stale(monkeypatch):
    # 핵심 회귀 방지(§2-2): 이제 updated_at은 메시지와 무관한 독립 하트비트(300초)가 갱신하므로,
    # 오래됐다는 건 **관측 루프가 멈췄다**는 뜻이다. 07-30 설계에서는 정상일에도 6시간 45분씩
    # 묵어 있어 이 임계를 걸 수 없었다.
    monkeypatch.setattr("mahdi.dashboard.data_source.db.local_now", lambda: datetime(2026, 7, 31, 15, 0, 0))
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db.latest_market_halt_state",
        lambda conn: {
            "updated_at": datetime(2026, 7, 31, 9, 0, 5), "is_halted": False,
            "mkop_cls_code": None, "label": "정상", "halted_since": None,
            "last_message_at": datetime(2026, 7, 31, 9, 0, 5),
        },
    )
    check = _market_halt_check(object())
    assert check.status == "warning"
    assert "하트비트" in check.detail


def test_market_halt_check_does_not_warn_on_long_message_silence(monkeypatch):
    # 반대 방향 회귀 방지: last_message_at은 정상일에도 수 시간 비어 있는 게 정상이므로
    # (07-31 실측: 09:00 이후 6시간 45분 무수신) 여기에 임계를 걸면 상시 오경보가 된다.
    monkeypatch.setattr("mahdi.dashboard.data_source.db.local_now", lambda: datetime(2026, 7, 31, 15, 44, 0))
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db.latest_market_halt_state",
        lambda conn: {
            "updated_at": datetime(2026, 7, 31, 15, 43, 0), "is_halted": False,
            "mkop_cls_code": None, "label": "정상", "halted_since": None,
            "last_message_at": datetime(2026, 7, 31, 9, 0, 5),
        },
    )
    check = _market_halt_check(object())
    assert check.status == "ok"


def test_market_halt_check_warns_when_currently_halted(monkeypatch):
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db.latest_market_halt_state",
        lambda conn: {
            "updated_at": datetime(2026, 7, 29, 9, 5, 0), "is_halted": True,
            "mkop_cls_code": "174", "label": "서킷브레이크 발동", "halted_since": datetime(2026, 7, 29, 9, 5, 0),
        },
    )
    check = _market_halt_check(object())
    assert check.status == "warning"
    assert "서킷브레이크 발동" in check.detail
    assert "174" in check.detail


def test_market_halt_check_ok_when_resumed(monkeypatch):
    monkeypatch.setattr("mahdi.dashboard.data_source.db.local_now", lambda: datetime(2026, 7, 29, 9, 26, 0))
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db.latest_market_halt_state",
        lambda conn: {
            "updated_at": datetime(2026, 7, 29, 9, 25, 0), "is_halted": False,
            "mkop_cls_code": "175", "label": "서킷브레이크 해제", "halted_since": None,
            "last_message_at": datetime(2026, 7, 29, 9, 25, 0),
        },
    )
    check = _market_halt_check(object())
    assert check.status == "ok"
    assert "서킷브레이크 해제" in check.detail


def test_get_market_halt_status_returns_latest_state(monkeypatch):
    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", fake_get_connection)
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db.latest_market_halt_state",
        lambda conn: {"is_halted": True, "mkop_cls_code": "174", "label": "서킷브레이크 발동"},
    )
    status = get_market_halt_status()
    assert status["is_halted"] is True


def test_get_market_halt_status_returns_none_on_db_failure(monkeypatch):
    @contextmanager
    def fake_get_connection(settings=None):
        raise ConnectionError("db down")
        yield  # pragma: no cover

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", fake_get_connection)
    assert get_market_halt_status() is None


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
    monkeypatch.setattr("mahdi.dashboard.data_source._market_halt_check", make_check("market_halt"))
    monkeypatch.setattr("mahdi.dashboard.data_source._option_chain_freshness_check", make_check("option_chain"))
    monkeypatch.setattr("mahdi.dashboard.data_source._futures_freshness_check", make_check("futures"))
    monkeypatch.setattr("mahdi.dashboard.data_source._option_chain_leg_balance_check", make_check("leg_balance"))
    monkeypatch.setattr("mahdi.dashboard.data_source._cbot_status_check", make_check("cbot"))
    # 2026-08-05(P1-4) 매크로 신선도 — CBOT 배지는 출처만 보므로 폴러가 죽어도 파란불이 뜬다
    monkeypatch.setattr("mahdi.dashboard.data_source._macro_freshness_check", make_check("macro_freshness"))
    monkeypatch.setattr("mahdi.dashboard.data_source._schema_integrity_check", make_check("schema"))
    monkeypatch.setattr("mahdi.dashboard.data_source._fossil_data_check", make_check("fossil"))
    monkeypatch.setattr("mahdi.dashboard.data_source._regime_stability_check", make_check("regime"))
    monkeypatch.setattr("mahdi.dashboard.data_source._regime_fit_progress_check", make_check("regime_fit_progress"))
    monkeypatch.setattr("mahdi.dashboard.data_source._shutdown_reliability_check", make_check("shutdown"))
    # 2026-08-12(§2-3 / Fix#8) 워치독 판정 신선도 — 08-12에 감시자가 5시간 31분 막혀 있었는데
    # 화면에는 아무 표시도 없었다. 종료 신뢰성 배지 옆 자리다.
    monkeypatch.setattr("mahdi.dashboard.data_source._watchdog_liveness_check", make_check("watchdog"))
    monkeypatch.setattr("mahdi.dashboard.data_source._rate_limiter_health_check", make_check("rate_limiter"))
    # 2026-08-01(§5-5) 관측 품질 5종
    monkeypatch.setattr("mahdi.dashboard.data_source._rest_demand_check", make_check("rest_demand"))
    monkeypatch.setattr("mahdi.dashboard.data_source._backoff_headroom_check", make_check("backoff_headroom"))
    monkeypatch.setattr("mahdi.dashboard.data_source._monthly_coverage_check", make_check("monthly_coverage"))
    monkeypatch.setattr("mahdi.dashboard.data_source._overrun_count_check", make_check("overrun_count"))
    monkeypatch.setattr("mahdi.dashboard.data_source._ws_liveness_check", make_check("ws_liveness"))
    # 2026-08-05(P2-12) ATM 롤 churn — P0-1에서 정직하게 그리기 시작한 Flow Radar 공백의 원인 쪽 지표
    monkeypatch.setattr("mahdi.dashboard.data_source._atm_roll_churn_check", make_check("atm_roll"))
    # 2026-08-03(§5-1) 신호 도달률 — 커버리지가 답하지 못하는 "판단까지 갔는가"
    monkeypatch.setattr("mahdi.dashboard.data_source._signal_reach_check", make_check("signal_reach"))
    # 2026-08-06(§2-2 / Fix#1) 진입 컷오프 — 진입이 없는 이유를 화면이 설명한다
    monkeypatch.setattr("mahdi.dashboard.data_source._entry_cutoff_check", make_check("entry_cutoff"))

    result = get_health_summary()

    assert calls == [
        "market_halt", "option_chain", "futures", "leg_balance", "cbot", "macro_freshness",
        "schema", "fossil",
        "regime", "regime_fit_progress", "shutdown", "watchdog", "rate_limiter",
        "rest_demand", "backoff_headroom", "monthly_coverage", "overrun_count", "ws_liveness",
        "atm_roll",
        "signal_reach",
        "entry_cutoff",
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


# --- record_cockpit_startup (2026-07-22 운영점검보고서 §1-1 "좀비 프로세스" 재발 방지) -------------

def test_record_cockpit_startup_writes_marker_when_none_exists(monkeypatch, tmp_path):
    import mahdi.dashboard.data_source as data_source

    fake_marker = tmp_path / "logs" / ".last_cockpit_start.txt"
    monkeypatch.setattr(data_source, "COCKPIT_START_MARKER_FILE", fake_marker)
    now = datetime(2026, 7, 22, 7, 30, 0)
    monkeypatch.setattr(data_source.db, "local_now", lambda: now)

    message = record_cockpit_startup()

    assert "직전 COCKPIT 기동 기록 없음" in message
    assert fake_marker.exists()
    assert fake_marker.read_text(encoding="utf-8") == now.isoformat()


def test_record_cockpit_startup_reports_elapsed_hours_and_updates_marker(monkeypatch, tmp_path):
    # 07-21 08:15에 뜬 좀비 COCKPIT이 07-22 07:30까지(약 23.25시간) 안 죽고 남아있던 사례처럼,
    # 다음 정상 기동 시점에 경과 시간이 메시지에 그대로 드러나야 한다.
    import mahdi.dashboard.data_source as data_source

    fake_log_dir = tmp_path / "logs"
    fake_log_dir.mkdir()
    fake_marker = fake_log_dir / ".last_cockpit_start.txt"
    last = datetime(2026, 7, 21, 8, 15, 29)
    fake_marker.write_text(last.isoformat(), encoding="utf-8")
    monkeypatch.setattr(data_source, "COCKPIT_START_MARKER_FILE", fake_marker)

    now = datetime(2026, 7, 22, 7, 30, 41)
    monkeypatch.setattr(data_source.db, "local_now", lambda: now)

    message = record_cockpit_startup()

    assert "직전 COCKPIT 기동: 2026-07-21 08:15:29 (23.3시간 전)" in message
    assert fake_marker.read_text(encoding="utf-8") == now.isoformat()


def test_record_cockpit_startup_handles_corrupted_marker_and_recovers(monkeypatch, tmp_path):
    import mahdi.dashboard.data_source as data_source

    fake_log_dir = tmp_path / "logs"
    fake_log_dir.mkdir()
    fake_marker = fake_log_dir / ".last_cockpit_start.txt"
    fake_marker.write_text("이건 타임스탬프가 아님", encoding="utf-8")
    monkeypatch.setattr(data_source, "COCKPIT_START_MARKER_FILE", fake_marker)

    now = datetime(2026, 7, 22, 7, 30, 0)
    monkeypatch.setattr(data_source.db, "local_now", lambda: now)

    message = record_cockpit_startup()

    assert "직전 COCKPIT 기동 기록 확인 실패" in message
    assert fake_marker.read_text(encoding="utf-8") == now.isoformat()  # 손상된 마커도 이번 기록으로 복구됨


# --- get_latest_decision_context / get_account_status_view (2026-07-29 "판단 현황"/"계좌 현황") ---

def test_get_latest_decision_context_returns_latest_and_history(monkeypatch):
    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    history = [
        {
            "timestamp": datetime(2026, 7, 29, 9, 5, 0), "conviction": "STANDARD", "decision": "ENTER",
            "reject_reason": None, "risk_gate_state": {"allowed_strategies": ["atm_long"]}, "exec_mode": "ADVISORY",
        },
        {
            "timestamp": datetime(2026, 7, 29, 9, 0, 0), "conviction": "NO_TRADE", "decision": "REJECT",
            "reject_reason": "no_strategy_for_this_cell", "risk_gate_state": {}, "exec_mode": "ADVISORY",
        },
    ]
    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.dashboard.data_source.db.recent_signal_decisions", lambda conn, limit: history)

    result = get_latest_decision_context()

    assert result["latest"] == history[0]
    assert result["history"] == history


def test_get_latest_decision_context_handles_no_rows(monkeypatch):
    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.dashboard.data_source.db.recent_signal_decisions", lambda conn, limit: [])

    result = get_latest_decision_context()

    assert result == {"latest": None, "history": []}


def test_get_latest_decision_context_falls_back_when_db_unavailable(monkeypatch):
    @contextmanager
    def broken_connection(settings=None):
        raise ConnectionError("DB 없음")
        yield  # pragma: no cover

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", broken_connection)

    assert get_latest_decision_context() == {"latest": None, "history": []}


_ACCOUNT_ROW_LATEST = {
    "timestamp": datetime(2026, 7, 29, 9, 0, 0), "prsm_dpast": 51_000_000.0,
    "evlu_pfls_amt_smtl": 500_000.0, "trad_pfls_amt_smtl": 0.0,
    "dnca_cash": 50_500_000.0, "ord_psbl_cash": 50_500_000.0, "mgna_tota": 0.0,
    "same_direction_buy_count": 0, "same_direction_sell_count": 0,
}
_ACCOUNT_ROW_YESTERDAY = {**_ACCOUNT_ROW_LATEST, "prsm_dpast": 50_000_000.0}


def test_get_account_status_view_computes_pnl_via_build_account_state(monkeypatch):
    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", fake_get_connection)
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db.latest_account_balance_snapshot", lambda conn: _ACCOUNT_ROW_LATEST
    )
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db.account_balance_snapshot_before", lambda conn, before: _ACCOUNT_ROW_YESTERDAY
    )
    monkeypatch.setattr("mahdi.dashboard.data_source.db.max_account_balance_ever", lambda conn: 51_000_000.0)
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db.local_now", lambda: datetime(2026, 7, 29, 9, 0, 0)
    )

    result = get_account_status_view()

    assert result["prsm_dpast"] == 51_000_000.0
    assert result["daily_pnl_pct"] == pytest.approx(0.02)
    assert result["weekly_pnl_pct"] == pytest.approx(0.02)
    assert result["drawdown_pct"] == pytest.approx(0.0)


def test_get_account_status_view_returns_none_when_no_snapshot(monkeypatch):
    @contextmanager
    def fake_get_connection(settings=None):
        yield object()

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", fake_get_connection)
    monkeypatch.setattr("mahdi.dashboard.data_source.db.latest_account_balance_snapshot", lambda conn: None)

    assert get_account_status_view() is None


def test_get_account_status_view_falls_back_when_db_unavailable(monkeypatch):
    @contextmanager
    def broken_connection(settings=None):
        raise ConnectionError("DB 없음")
        yield  # pragma: no cover

    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_connection", broken_connection)

    assert get_account_status_view() is None


# ===== 2026-08-01(운영점검보고서 2026-07-31 §5-5) 관측 품질 배지 4종 =====


def _demand(pct, threshold=2.29):
    return {"calls": 12947, "calls_per_second": round(pct / 100, 3), "capacity_pct": pct,
            "deficit_threshold_multiplier": threshold}


def test_rest_demand_badge_warns_above_the_budget_threshold(monkeypatch):
    # 07-31 실측 43.6%는 ok, 60% 이상이면 "폴러 추가를 멈추고 예산부터 본다".
    monkeypatch.setattr("mahdi.dashboard.data_source.db_metrics.rest_demand", lambda conn, d: _demand(43.6))
    assert _rest_demand_check(object(), datetime(2026, 8, 3, 10, 0)).status == "ok"

    monkeypatch.setattr("mahdi.dashboard.data_source.db_metrics.rest_demand", lambda conn, d: _demand(61.0))
    check = _rest_demand_check(object(), datetime(2026, 8, 3, 10, 0))
    assert check.status == "warning"
    assert check.group == "관측 품질"


def test_rest_demand_badge_says_not_yet_instead_of_inventing_a_number(monkeypatch):
    # 마이그레이션 019 적용 전이거나 표본 2건 미만이면 지어내지 않는다.
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db_metrics.rest_demand",
        lambda conn, d: {"calls": None, "calls_per_second": None, "capacity_pct": None,
                         "deficit_threshold_multiplier": None},
    )
    check = _rest_demand_check(object(), datetime(2026, 8, 3, 10, 0))
    assert check.status == "info"
    assert "집계 전" in check.detail


def test_backoff_headroom_badge_warns_when_approaching_the_deficit_threshold(monkeypatch):
    monkeypatch.setattr("mahdi.dashboard.data_source.db_metrics.rest_demand", lambda conn, d: _demand(43.6, 2.29))
    # 07-31 실측 최대 2.25배 / 임계 2.29배 = 98% → 경고(적자 진입 직전이었다).
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db_metrics._rate_limiter",
        lambda conn, d: {"rows": 447, "overrun_rows": 46, "max_multiplier": 2.25, "mean_multiplier": 1.13},
    )
    assert _backoff_headroom_check(object(), datetime(2026, 8, 3, 10, 0)).status == "warning"

    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db_metrics._rate_limiter",
        lambda conn, d: {"rows": 447, "overrun_rows": 5, "max_multiplier": 1.30, "mean_multiplier": 1.05},
    )
    assert _backoff_headroom_check(object(), datetime(2026, 8, 3, 10, 0)).status == "ok"


def test_monthly_coverage_badge_warns_below_95_percent(monkeypatch):
    # 핵심 지표: 07-31에 밀림은 83→46건으로 좋아졌는데 이 값은 95.0%→90.3%로 후퇴했다.
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db_metrics.monthly_book_coverage",
        lambda conn, d, underlying=None: {"expiry": date(2026, 8, 13), "minutes": 447,
                                     "elapsed_minutes": 495, "coverage_pct": 90.3},
    )
    check = _monthly_coverage_check(object(), "KOSPI200", datetime(2026, 8, 3, 15, 0))
    assert check.status == "warning"
    assert "90.3%" in check.detail

    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db_metrics.monthly_book_coverage",
        lambda conn, d, underlying=None: {"expiry": date(2026, 8, 13), "minutes": 490,
                                     "elapsed_minutes": 495, "coverage_pct": 99.0},
    )
    assert _monthly_coverage_check(object(), "KOSPI200", datetime(2026, 8, 3, 15, 0)).status == "ok"


def test_monthly_coverage_badge_is_info_before_the_expiry_can_be_resolved(monkeypatch):
    # 만기유동성 첫 행이 08:31 부근이라 장전에는 먼슬리 만기를 특정할 수 없다 — 경고가 아니다.
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db_metrics.monthly_book_coverage",
        lambda conn, d, underlying=None: {"expiry": None, "minutes": None, "coverage_pct": None,
                                     "reason": "만기유동성 미적재(장전)"},
    )
    check = _monthly_coverage_check(object(), "KOSPI200", datetime(2026, 8, 3, 9, 5))
    assert check.status == "info"


def test_monthly_coverage_badge_still_reports_after_the_close(monkeypatch):
    # 2026-08-03 COCKPIT 육안 점검 이후: 분모가 "09:00 이후 경과 분"이 아니라 **분자와 같은
    # 관측 구간**이 됐으므로 장 마감 뒤에도 그날 값이 그대로 유효하다(종전에는 "장중 아님" info).
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db_metrics.monthly_book_coverage",
        lambda conn, d, underlying=None: {"expiry": date(2026, 8, 13), "minutes": 489,
                                          "elapsed_minutes": 493, "coverage_pct": 99.2,
                                          "over_100": False},
    )
    check = _monthly_coverage_check(object(), "KOSPI200", datetime(2026, 8, 3, 20, 0))
    assert check.status == "ok"
    assert "99.2%" in check.detail
    assert "관측 493분" in check.detail


def test_monthly_coverage_badge_warns_when_it_exceeds_100_percent(monkeypatch):
    """2026-08-03 실측 회귀 — 커버리지 120.7%가 **초록불**로 표시되고 있었다.

    분자는 하루 전체(07:32~, 489분)를 세는데 COCKPIT이 분모로 "09:00 이후 경과 분"(405분)을
    넘기고 있었다. 배지는 `< 95%`일 때만 경고하므로 **지표가 고장났다는 사실 자체가 초록**이었고
    4주간 아무도 못 봤다. 100% 초과는 데이터가 좋다는 뜻이 아니라 기간 불일치라는 뜻이다.
    """
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db_metrics.monthly_book_coverage",
        lambda conn, d, underlying=None: {"expiry": date(2026, 8, 13), "minutes": 489,
                                          "elapsed_minutes": 405, "coverage_pct": 120.7,
                                          "over_100": True},
    )
    check = _monthly_coverage_check(object(), "KOSPI200", datetime(2026, 8, 3, 15, 44))
    assert check.status == "warning"
    assert "기간 불일치" in check.detail


def test_overrun_count_badge_warns_at_the_pacer_split_reopen_threshold(monkeypatch):
    # 30건은 페이서 분리 재개 조건과 같은 숫자다(2026-08-01 DECISION_LOG).
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db_metrics._rate_limiter",
        lambda conn, d: {"rows": 447, "overrun_rows": 46, "max_multiplier": 2.25, "mean_multiplier": 1.13},
    )
    assert _overrun_count_check(object(), datetime(2026, 8, 3, 15, 0)).status == "warning"

    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db_metrics._rate_limiter",
        lambda conn, d: {"rows": 447, "overrun_rows": 12, "max_multiplier": 1.5, "mean_multiplier": 1.05},
    )
    assert _overrun_count_check(object(), datetime(2026, 8, 3, 15, 0)).status == "ok"


def test_health_checks_carry_a_group_so_the_cockpit_can_split_rows():
    # app.py가 group으로 2행을 만든다 — 기존 배지는 기본값 "인프라"를 유지해야 한다.
    from mahdi.dashboard.data_source import HealthCheck

    assert HealthCheck("a", "ok", "b").group == "인프라"
    monkeypatched = HealthCheck("a", "ok", "b", group="관측 품질")
    assert monkeypatched.group == "관측 품질"


# ===== 2026-08-03 COCKPIT 육안 점검: 종가 단일가 구간 오경보 =====


def _futures_conn(latest: datetime):
    class _Cur:
        def execute(self, q, p=None): self._q = q
        def fetchone(self): return (latest,)
        def __enter__(self): return self
        def __exit__(self, *e): return False

    class _Conn:
        def cursor(self): return _Cur()
        def rollback(self): pass
    return _Conn()


def test_futures_badge_is_quiet_during_the_closing_auction(monkeypatch):
    """매 거래일 15:40~15:45에 반드시 뜨던 오경보를 없앤다.

    KOSPI200 선물·옵션 장 마감 동시호가(15:35~15:45)에는 연속 체결이 없어 WS 체결이 끊기고
    1분봉도 안 만들어진다. 그런데 _is_trading_hours()는 15:45까지 True이고 결손 임계는 5분이라
    **정상 상태가 매일 노란불**이 됐다. 08-03 15:44:53 화면이 그 순간을 잡았다("12분째 결손").
    """
    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_active_futures_symbol", lambda c, u: "A01609")
    monkeypatch.setattr("mahdi.dashboard.data_source.db.local_now", lambda: datetime(2026, 8, 3, 15, 44, 53))

    # 08-03 실측: 마지막 선물봉 15:34, 화면 시각 15:44:53 → 종전 로직이면 "12분째 결손".
    check = _futures_freshness_check(_futures_conn(datetime(2026, 8, 3, 15, 34)), "KOSPI200",
                                     datetime(2026, 8, 3, 15, 44, 53))
    assert check.status == "ok", "단일가 구간의 체결 공백은 정상이다"


def test_futures_badge_still_warns_when_it_died_before_the_auction(monkeypatch):
    # 반대 방향 회귀 방지: 단일가 시작 **전부터** 끊겨 있었으면 여전히 경고해야 한다.
    monkeypatch.setattr("mahdi.dashboard.data_source.db.get_active_futures_symbol", lambda c, u: "A01609")

    check = _futures_freshness_check(_futures_conn(datetime(2026, 8, 3, 14, 0)), "KOSPI200",
                                     datetime(2026, 8, 3, 15, 44, 53))
    assert check.status == "warning"
    assert "95분째 결손" in check.detail


def test_option_chain_badge_keeps_wall_clock_reference_during_the_auction(monkeypatch):
    # 옵션체인은 REST 폴링이라 단일가 구간에도 계속 들어온다 — 여기까지 완화하면 진짜 결손을 놓친다.
    class _Cur:
        def execute(self, q, p=None): pass
        def fetchone(self): return (datetime(2026, 8, 3, 15, 30),)
        def __enter__(self): return self
        def __exit__(self, *e): return False

    class _Conn:
        def cursor(self): return _Cur()
        def rollback(self): pass

    check = _option_chain_freshness_check(_Conn(), "KOSPI200", datetime(2026, 8, 3, 15, 44, 53))
    assert check.status == "warning"
    assert "15분째 결손" in check.detail


def test_market_halt_badge_warns_when_subscription_never_established(monkeypatch):
    """하트비트는 정상인데 H0UNMKO0 구독만 안 걸린 상태 — 08-03에 아무도 못 보던 사각지대.

    그날 장운영정보 수신이 0건이었는데 하트비트가 살아 있어 배지가 초록이었다.
    데이터 수신에는 임계를 걸 수 없으므로(정상일에도 하루 0~2건) 구독 성립 쪽을 따로 본다.
    """
    monkeypatch.setattr("mahdi.dashboard.data_source.db.local_now", lambda: datetime(2026, 8, 4, 15, 44))
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db.latest_market_halt_state",
        lambda conn: {"updated_at": datetime(2026, 8, 4, 15, 41), "is_halted": False,
                      "mkop_cls_code": None, "label": "정상", "halted_since": None,
                      "last_message_at": None},
    )
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db.latest_ws_status",
        lambda conn: {"updated_at": datetime(2026, 8, 4, 15, 41), "connected_since": None,
                      "last_message_at": None, "reconnect_count_today": 0,
                      "market_op_subscribed_at": None},
    )
    check = _market_halt_check(object())
    assert check.status == "warning"
    assert "구독 미성립" in check.detail


def test_market_halt_badge_shows_subscription_time_when_established(monkeypatch):
    monkeypatch.setattr("mahdi.dashboard.data_source.db.local_now", lambda: datetime(2026, 8, 4, 15, 44))
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db.latest_market_halt_state",
        lambda conn: {"updated_at": datetime(2026, 8, 4, 15, 41), "is_halted": False,
                      "mkop_cls_code": None, "label": "정상", "halted_since": None,
                      "last_message_at": None},
    )
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db.latest_ws_status",
        lambda conn: {"updated_at": datetime(2026, 8, 4, 15, 41), "connected_since": None,
                      "last_message_at": None, "reconnect_count_today": 0,
                      "market_op_subscribed_at": datetime(2026, 8, 4, 7, 31, 4)},
    )
    check = _market_halt_check(object())
    assert check.status == "ok"
    # 장운영정보 데이터가 0건이어도 구독확립 시각은 있어야 한다 — 그게 이 fix의 전부다.
    assert "구독확립 07:31:04" in check.detail
    assert "장운영정보 수신 이력 없음" in check.detail


# ===== 2026-08-06 §2-2 / Fix#1 — 진입 컷오프 배지 =====


def _decisions(enter_after_cutoff=0, blocked=0, enters=0, total=474):
    return {
        "total": total,
        "decision": {"ENTER": enters, "REJECT": total - enters},
        "entry_cutoff": {
            "cutoff_time": "14:50", "forced_flat_time": "15:10",
            "blocked_count": blocked, "enter_after_cutoff": enter_after_cutoff,
            "enter_after_forced_flat": max(0, enter_after_cutoff - 3),
        },
    }


def test_entry_cutoff_badge_warns_when_the_invariant_is_violated(monkeypatch):
    """08-06 실측(컷오프 이후 21건)이 화면에 노란불로 뜬다.

    빨간불이 아닌 이유: 이 배지가 잡는 것은 시장 이상이 아니라 **우리 코드의 회귀**다.
    """
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db_metrics.decisions",
        lambda conn, d: _decisions(enter_after_cutoff=21, enters=62),
    )
    check = _entry_cutoff_check(object(), datetime(2026, 8, 6, 15, 45))
    assert check.status == "warning"
    assert "21건" in check.detail
    assert check.group == "판단"


def test_entry_cutoff_badge_explains_why_there_are_no_entries_after_cutoff(monkeypatch):
    """컷오프 이후의 "진입 0건"이 신호 사망인지 규칙인지 화면에서 구분돼야 한다."""
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db_metrics.decisions",
        lambda conn, d: _decisions(blocked=7, enters=40),
    )
    check = _entry_cutoff_check(object(), datetime(2026, 8, 6, 15, 0))
    assert check.status == "info"
    assert "신규 진입 금지" in check.detail
    assert "7분" in check.detail


def test_entry_cutoff_badge_is_ok_before_the_cutoff(monkeypatch):
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db_metrics.decisions",
        lambda conn, d: _decisions(enters=12),
    )
    check = _entry_cutoff_check(object(), datetime(2026, 8, 6, 10, 0))
    assert check.status == "ok"
    assert "12건" in check.detail


def test_entry_cutoff_badge_says_not_yet_instead_of_inventing_a_number(monkeypatch):
    monkeypatch.setattr(
        "mahdi.dashboard.data_source.db_metrics.decisions",
        lambda conn, d: {"total": 0, "decision": {}, "entry_cutoff": {}},
    )
    check = _entry_cutoff_check(object(), datetime(2026, 8, 6, 10, 0))
    assert check.status == "info"
    assert "집계 전" in check.detail
