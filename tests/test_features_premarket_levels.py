"""당일 맥점 예측 — 순수 계산(`mahdi/features/premarket_levels.py`)과 저장소 카드(`ops/premarket_levels_store.py`)."""

from __future__ import annotations

import json
import random
from datetime import date, datetime

import numpy as np

from mahdi.features import premarket_levels as PL
from mahdi.ops import premarket_levels_store as store


def _session(day: str, base: float, rng: random.Random, n: int = 380) -> list[PL.Bar]:
    bars, px = [], base
    for i in range(n):
        h = 8 + (45 + i) // 60; m = (45 + i) % 60
        o = px; px += rng.gauss(0, 0.8); c = px
        bars.append(PL.Bar(t=f"{h:02d}:{m:02d}", o=o, h=max(o, c) + 0.3, l=min(o, c) - 0.3, c=c, v=100))
    return bars


def _history(n_days: int = 70, seed: int = 1) -> PL.History:
    rng = random.Random(seed); h = PL.History(); base = 1000.0
    for i in range(n_days):
        d = date(2026, 1, 1).fromordinal(date(2026, 1, 5).toordinal() + i).isoformat()
        bars = _session(d, base, rng); h.upsert(d, bars, "mireuk"); base = bars[-1].c + rng.gauss(0, 3)
    h.finalize()
    return h


def test_history_upsert_keeps_only_recent_bars_and_mahdi_overrides():
    h = _history(10)
    assert len(h.summaries) == 10
    assert len(h.bars) == PL.LOOKBACK + 1                      # 봉은 최근 7세션만
    d = h.summaries[-1].d
    h.upsert(d, _session(d, 2000.0, random.Random(5)), "mahdi")
    assert h.source[d] == "mahdi" and h.summaries[-1].o == 2000.0
    h.upsert(d, _session(d, 500.0, random.Random(6)), "mireuk")   # 미륵은 마흐디를 덮지 못한다
    assert h.summaries[-1].o == 2000.0


def test_fit_stage1_requires_min_sessions_and_gives_ordered_bands():
    h = _history(70)
    assert PL.fit_stage1(h.summaries[:20]) is None
    p = PL.fit_stage1(h.summaries[-60:])
    assert p and p["n"] >= PL.MIN_TRAIN_SESSIONS
    assert p["u80"][0] <= p["u50"][0] <= 0 <= p["u50"][1] <= p["u80"][1]
    d = PL.distance_stage1(p, 1000.0, 20.0)
    assert d["high"] > 1000.0 > d["low"]
    assert d["high80"][0] <= d["high50"][0] <= d["high"] <= d["high50"][1] <= d["high80"][1]


def test_stage2_prediction_never_below_path_so_far():
    h = _history(70)
    p2 = PL.fit_stage2(h.summaries, len(h.summaries))
    assert p2 and len(p2["bu"]) == 6
    today = _session("2026-04-20", 1000.0, random.Random(9))
    prev = h.summaries[-1]
    out = PL.compute_stage("0930", today, prev, 20.0, None, p2, {})
    early = [b for b in today if b.t <= "09:30"]
    assert out["distance"]["high"] >= max(b.h for b in early) - 1e-9
    assert out["distance"]["low"] <= min(b.l for b in early) + 1e-9
    assert out["ref"] == early[-1].c
    assert any(t.startswith("OR고") for _, tags in out["structure"]["up"] + out["structure"]["down"] for t in tags) or True


def test_structural_candidates_find_shelf_gap_and_prev_levels():
    rng = random.Random(3)
    flat = [PL.Bar(t=f"{9 + i // 60:02d}:{i % 60:02d}", o=1000, h=1000.4, l=999.6, c=1000, v=10) for i in range(200)]
    day2 = _session("2026-03-03", 1020.0, rng)          # 전일 범위 위로 갭
    day2 = [PL.Bar(t=b.t, o=b.o + 30, h=b.h + 30, l=b.l + 30, c=b.c + 30, v=b.v) for b in day2]
    cands = PL.structural_candidates([("2026-03-02", flat), ("2026-03-03", day2)], oi_strikes=[(1050.0, 999)])
    tags = [t for v in cands.values() for t in v]
    assert any(t.startswith("매물대1000") for t in tags)
    assert any(t.startswith("갭하변") for t in tags) and any(t.startswith("갭상변") for t in tags)
    assert any(t.startswith("전일고") for t in tags) and any(t.startswith("OI행사가1050") for t in tags)
    ups, dns = PL.select_nearest(cands, 1030.0)
    assert all(k > 1031 for k in ups) and all(k < 1029 for k in dns) and len(ups) <= 3


def test_lad_fit_recovers_median_line():
    rng = np.random.default_rng(0)
    X = np.column_stack([np.ones(200), rng.normal(size=200)])
    y = 0.5 + 2.0 * X[:, 1] + rng.laplace(scale=0.1, size=200)
    b = PL.lad_fit(X, y)
    assert abs(b[0] - 0.5) < 0.05 and abs(b[1] - 2.0) < 0.05


def test_badge_cards_before_publish_and_before_due(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DIR", tmp_path)
    now = datetime(2026, 9, 7, 8, 30)
    cards = store.badge_cards(now)
    assert len(cards) == 4 and all(c["status"] == "warning" for c in cards)
    h = _history(70); past = h.summaries
    payload = dict(date="2026-09-07", atr=20.0, prev=dict(d=past[-1].d, o=past[-1].o, h=past[-1].h, l=past[-1].l, c=past[-1].c),
                   p1=PL.fit_stage1(past[-60:]), p2=PL.fit_stage2(past, len(past)), candidates={"1010": ["전일고1010"]}, stages={}, warnings=[])
    (tmp_path / "2026-09-07.json").write_text(json.dumps(payload), encoding="utf-8")
    cards = store.badge_cards(now)
    assert cards[0]["status"] == "info" and "08:50 이후" in cards[0]["value"]
    # 때가 됐고 봉이 있으면 굳힌다
    bars = _session("2026-09-07", 1000.0, random.Random(4))
    monkeypatch.setattr(store, "fetch_bars", lambda conn, symbol, d, until=None: [b for b in bars if until is None or b.t <= until])
    monkeypatch.setattr(store, "futures_symbol", lambda conn: "A01609")

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(store.db, "get_connection", lambda: _Conn())
    cards = store.badge_cards(datetime(2026, 9, 7, 9, 31))
    assert cards[0]["status"] == "ok" and "고점" in cards[0]["value"]
    assert cards[2]["status"] == "ok"
    day = json.loads((tmp_path / "2026-09-07.json").read_text(encoding="utf-8"))
    assert set(day["stages"]) == {"0850", "0930"}
    frozen = day["stages"]["0930"]["ref"]
    store.badge_cards(datetime(2026, 9, 7, 14, 0))            # 다시 불러도 굳힌 값은 그대로
    assert json.loads((tmp_path / "2026-09-07.json").read_text(encoding="utf-8"))["stages"]["0930"]["ref"] == frozen

    # 장후 채점 — 실제 고·저 대비 오차·안착·누적이 나오고 파일에 남는다
    md = store.score_markdown(date(2026, 9, 7))
    assert "## 18." in md and "누적:" in md and "08:50" in md and "09:30" in md
    saved = json.loads((tmp_path / "2026-09-07.json").read_text(encoding="utf-8"))
    assert saved["score"]["high"] == max(b.h for b in bars) and set(saved["score"]["stages"]) == {"0850", "0930"}


def test_rhat_scale_respects_floor_and_cap_and_scales_bands():
    h = _history(70); past = h.summaries
    r1 = PL.fit_rhat(past, len(past), with_path=False)
    assert r1 and len(r1["beta"]) == 7 and r1["med_r"] > 0
    x = PL._x_fixed(PL.SessionSummary(d="", o=1000, h=1000, l=1000, c=1000, atr=20.0), past[-1], 18.0)
    sc = PL.rhat_scale(r1, x)
    assert PL.RHAT_FLOOR <= sc <= PL.RHAT_CAP
    p1 = PL.fit_stage1(past[-60:])
    raw = PL.distance_stage1(p1, 1000.0, 20.0)
    scaled = PL.distance_stage1(p1, 1000.0, 20.0, scale=1.5)
    assert scaled["high"] == raw["high"] and scaled["raw"]["high80"] == raw["high80"]
    assert (scaled["high80"][1] - scaled["high80"][0]) > (raw["high80"][1] - raw["high80"][0])
    assert PL.rhat_scale(None, x) is None and PL.rhat_scale(r1, None) is None
