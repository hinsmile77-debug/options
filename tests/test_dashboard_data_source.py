from contextlib import contextmanager

from mahdi.dashboard.data_source import _synthetic_snapshot, load_snapshot
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
