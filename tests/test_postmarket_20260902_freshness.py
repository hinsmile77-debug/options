"""2026-09-02 장후 — 신선도 지표 P1-5 · 위상 레버 Fix#10 회귀.

**이 파일이 지키는 것은 「이산 판정을 대체하지 않고 옆에 세웠다」이다.** 09-02 §1-11이 멈춘
자리가 그것이었다: `chain_input_source.stale_pct`가 08-18 이후 매일 98~100%라, 레버가
초 단위로 무엇을 바꿔도 지표가 안 움직였고 **「레버 실패」와 「지표가 그 레버를 못 잼」을
사람이 가를 수 없었다**(규약 J).

정의를 바꾸는 대신 셋을 새로 만들었고, 여기서 그 셋의 성질을 못박는다:
  ① `chain_newest_leg_age_seconds`(마이그레이션 036) — 같은 관측의 **연속판**
  ② `db.signal_reach.chain_newest_age_seconds.behind_minutes` — 「몇 분 뒤처졌나」
  ③ `log.cycles.congested` — 레버 E가 **직접** 조작하는 축(사이클 소요시간)
그리고 ④ 위상 레버가 실제로 25.0이 아니라 55.0으로 켜져 있는가.

상세 근거는 `db/migrations/036_*.sql`, `mahdi/main.py`의 Fix#10 절,
`mahdi/ops/log_metrics.py`의 `CONGESTED_HOURS` 절에 있다.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from mahdi.main import _build_signal_inputs
from mahdi.ops import db_metrics, log_metrics

_MONTHLY = date(2026, 9, 10)
_NOW = datetime(2026, 9, 2, 10, 0, 55)   # 위상 55초 — Fix#10 적용 후의 판단 시각


def _row(strike: float, opt: str, ts: datetime) -> dict:
    return {"strike": strike, "option_type": opt, "oi": 100.0, "iv": 0.2, "gamma": 0.02,
            "gex": 0.0, "expiry": _MONTHLY, "timestamp": ts, "rv_5d": 0.18}


def _patch_chain(monkeypatch, chain_rows, *, now=_NOW):
    monkeypatch.setattr("mahdi.main.db.local_now", lambda: now)
    monkeypatch.setattr("mahdi.main.db.latest_option_chain", lambda conn, underlying: chain_rows)
    monkeypatch.setattr("mahdi.main.db.latest_underlying_spot", lambda conn, underlying, **kw: 350.5)
    monkeypatch.setattr("mahdi.main.db.latest_investor_flow", lambda conn, underlying: (500.0, 0.0, 0.0))
    monkeypatch.setattr("mahdi.main.db.latest_market_microstructure", lambda conn, symbol, as_of=None: None)


# ===================================================================== ①
# 두 값은 **같은 `newest`** 에서 나온다 — 그래서 서로를 검산한다.

def test_the_continuous_age_agrees_with_the_discrete_verdict(monkeypatch):
    """`newest 나이 < 60초` ⟺ `current`. 이 항등식이 깨지면 둘은 다른 것을 재는 것이다."""
    chain = [_row(350.0, "C", datetime(2026, 9, 2, 10, 0)), _row(350.0, "P", datetime(2026, 9, 2, 10, 0))]
    _patch_chain(monkeypatch, chain)

    _inputs, chain_inputs = _build_signal_inputs(conn=object(), regime_state=None, underlying="KOSPI200")

    assert chain_inputs["chain_input_source"] == "current"
    assert chain_inputs["chain_newest_leg_age_seconds"] == pytest.approx(55.0)
    # 항등식은 「판단 초보다 작다」가 아니라 **「60초보다 작다」**이다: 레그 타임스탬프는 분
    # 단위로 잘려 있으므로 나이 = 판단 초 + 마이크로초이고, 판단 초와 **같아진다**(작지 않다).
    assert chain_inputs["chain_newest_leg_age_seconds"] < 60


def test_one_minute_late_and_three_minutes_old_are_no_longer_the_same_cell(monkeypatch):
    """`stale` 한 칸에 뭉쳐 있던 두 사건이 갈린다 — 이것이 09-02 §1-11이 못 본 자리다."""
    late = [_row(350.0, "C", datetime(2026, 9, 2, 9, 59))]
    old = [_row(350.0, "C", datetime(2026, 9, 2, 9, 57))]

    _patch_chain(monkeypatch, late)
    _i, one_minute = _build_signal_inputs(conn=object(), regime_state=None, underlying="KOSPI200")
    _patch_chain(monkeypatch, old)
    _i, three_minutes = _build_signal_inputs(conn=object(), regime_state=None, underlying="KOSPI200")

    assert one_minute["chain_input_source"] == three_minutes["chain_input_source"] == "stale"
    assert one_minute["chain_newest_leg_age_seconds"] == pytest.approx(115.0)
    assert three_minutes["chain_newest_leg_age_seconds"] == pytest.approx(235.0)


def test_no_chain_leaves_the_age_none_rather_than_zero(monkeypatch):
    """규약 C — 「쟀는데 없었다」를 0초로 찍으면 그 분이 가장 신선한 분이 된다."""
    _patch_chain(monkeypatch, [])

    _inputs, chain_inputs = _build_signal_inputs(conn=object(), regime_state=None, underlying="KOSPI200")

    assert chain_inputs["chain_input_source"] == "none"
    assert chain_inputs["chain_newest_leg_age_seconds"] is None


def test_the_new_column_is_actually_written():
    """컬럼 목록과 INSERT가 갈리면 배지(마이그레이션 미적용 감지)가 그 컬럼에 대해 눈이 먼다."""
    from mahdi.data import db as db_module

    assert "chain_newest_leg_age_seconds" in db_module.signal_decision_columns()


# ===================================================================== ②
# 집계는 「몇 분 뒤처졌나」를 **정확히** 센다: 나이 = 60k + 판단초 이므로 `age // 60 == k`.

class _FakeConn:
    def rollback(self):
        pass


def _patch_db(monkeypatch, *, one, many=None, raises=False):
    def fake_fetchone(conn, sql, params=None):
        if raises:
            raise RuntimeError("column chain_newest_leg_age_seconds does not exist")
        for needle, value in one.items():
            if needle in sql:
                return value
        return None

    def fake_fetchall(conn, sql, params=None):
        for needle, value in (many or {}).items():
            if needle in sql:
                return value
        return []

    monkeypatch.setattr(db_metrics, "_fetchone", fake_fetchone)
    monkeypatch.setattr(db_metrics, "_fetchall", fake_fetchall)


def test_behind_minutes_splits_what_stale_pct_merged(monkeypatch):
    _patch_db(
        monkeypatch,
        one={"ORDER BY chain_newest_leg_age_seconds": (494, 55.0, 115.0, 235.0)},
        many={"floor(chain_newest_leg_age_seconds / 60)": [(0, 460), (1, 30), (3, 4)]},
    )

    out = db_metrics._chain_newest_age_stats(_FakeConn(), date(2026, 9, 2))

    assert out["available"] is True
    assert (out["p50"], out["p95"], out["max"]) == (55.0, 115.0, 235.0)
    # 키가 문자열인 것은 계약이다 — 이 dict은 JSON 사이드카로 왕복한다.
    assert out["behind_minutes"] == {"0": 460, "1": 30, "3": 4}


def test_a_day_without_the_column_says_it_could_not_measure(monkeypatch):
    """규약 C — 036 이전 이력이 「체인이 항상 최신이었다」로 보이면 안 된다."""
    _patch_db(monkeypatch, one={}, raises=True)

    out = db_metrics._chain_newest_age_stats(_FakeConn(), date(2026, 8, 11))

    assert out["available"] is False
    assert "036" in out["reason"]


def test_a_day_with_the_column_but_no_rows_is_also_not_zero(monkeypatch):
    _patch_db(monkeypatch, one={"chain_newest_leg_age_seconds": (0, None, None, None)})

    assert db_metrics._chain_newest_age_stats(_FakeConn(), date(2026, 9, 2))["available"] is False


# ===================================================================== ③
# 레버 E가 **직접** 조작하는 축. 창은 산출물에 함께 실린다.

def _cycle(hour: int, minute: int, end_second: float, rest: float) -> dict:
    end = hour * 3600 + minute * 60 + end_second
    return {"start": end - rest, "end": end, "rest": rest, "slip": 0.0, "rows": 10,
            "foreign": 0, "foreign_by_group": {}, "poll_minute": "%02d:%02d" % (hour, minute)}


def test_the_congested_window_is_printed_with_its_own_numbers():
    """창을 함께 인쇄하지 않으면, 나중에 창을 옮겼을 때 두 날의 값이 왜 갈리는지 알 수 없다."""
    cycles = [_cycle(9, m, 20.0, 19.5) for m in range(10)] + [_cycle(11, m, 52.0, 50.0) for m in range(10)]

    out = log_metrics._congested_cycle_seconds(cycles)

    assert out["hours"] == [10, 11, 12, 13, 14]
    assert out["cycles"] == 10, "09시는 창 밖이다 — 레버가 09시를 얻더라도 이 창은 안 따라간다"
    assert out["rest_p50"] == 50.0
    assert out["end_second_p50"] == 52.0
    assert out["ended_before_55s_pct"] == 100.0


def test_the_phase_ceiling_is_reported_as_a_share_not_a_verdict():
    """`ended_before_55s_pct`는 `current` 비율의 **상한**이다 — 둘이 갈리면 볼 곳이 다르다."""
    cycles = [_cycle(12, 0, 52.0, 50.0), _cycle(12, 1, 56.5, 54.0)]

    out = log_metrics._congested_cycle_seconds(cycles)

    assert out["ended_before_55s_pct"] == 50.0
    assert out["end_second_max"] == 56.5


def test_a_day_without_congested_cycles_reports_none_not_zero():
    """규약 C — 「그날 사이클이 0.0초였다」와 「표본이 없었다」는 다르다."""
    out = log_metrics._congested_cycle_seconds([])

    assert out["cycles"] == 0
    assert out["rest_p50"] is None and out["ended_before_55s_pct"] is None


# ===================================================================== ④
# 레버가 실제로 켜져 있는가 — 「상수가 존재한다」는 켜진 것이 아니다(09-02가 배운 것).

def test_fix10_is_actually_on_and_not_at_the_stale_2026_08_11_value():
    """08-11의 예측치 25.0초는 7거래일 실측에서 전 구간 0%였다(사이클이 그때 아직 안 끝난다).

    이 테스트가 막는 것은 회귀가 아니라 **되돌림**이다: 값이 10.0으로 돌아가면 판단은 다시
    매분 직전 분 체인을 보고, 그 사실은 `stale_pct`가 다시 100%가 될 때까지 아무 데도 안 남는다.
    """
    import mahdi.main as main_module

    assert main_module.SIGNAL_FUSION_PHASE_OFFSET_SECONDS == 55.0
    assert main_module.SIGNAL_FUSION_PHASE_OFFSET_SECONDS < main_module.SIGNAL_FUSION_POLL_INTERVAL_SECONDS
