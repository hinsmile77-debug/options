"""원천 추가 실험 — 기본 4원천에 조사 원천을 하나씩 더하면 「되돌림 자리」·「머무는 자리」 정확도가 오르는가.

2026-09-06 [MW0601]. 원천 정의는 `premarket_levels_wfa.candidates()`. 레벨 선택은 기존과 같다
(08:45 시가 기준, 위·아래 가까운 3개). 원천마다 후보 수가 달라지므로 절대치가 아니라
**각 세트의 ATR 정규화 무작위 대조군 대비 초과분**으로 비교한다.

## 지표 (관측 = 레벨 또는 세션×측)

  되돌림 자리: E±0.3% / E±0.5% = 일중 고·저가가 레벨 ±허용 안에 든 비율(세션×측)
              B±0.5%           = 첫 터치 후 되돌림 0.5%가 돌파 0.5%보다 먼저인 비율(귀무 50%)
  머무는 자리: 정거장률 T75     = 터치된 레벨 중 첫 터치 후 밴드(0.098 ATR) 안에 무작위 75분위 이상 머문 비율
  범위 밖     = 그날 고저 밖이라 터치 기회가 없던 레벨 비율

실행: python scripts/premarket_levels_sources_test.py --from 2026-01-05 --to 2026-09-04
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import premarket_levels_wfa as W

BIN = 0.5
BAND_ATR = 0.098
RULE = dict(lookback=6, spacing="nearest", gap_tilt=0, k=1.0)

SETS = [
    ("기본(매물대·갭·전일고저·VWAP)", frozenset()),
    ("+옵션 OI 행사가(마흐디)", frozenset({"oi"})),
    ("+감마 월(마흐디)", frozenset({"gw"})),
    ("+플로어 피봇", frozenset({"pivot"})),
    ("+카마릴라", frozenset({"camarilla"})),
    ("+전일 거래량 POC/VAH/VAL", frozenset({"vp"})),
    ("+전일 VWAP ±1σ/2σ", frozenset({"vwapband"})),
    ("+전일 종가", frozenset({"prevclose"})),
    ("+이동평균 5/20/60", frozenset({"ma"})),
    ("+라운드 10pt", frozenset({"round"})),
    ("+라운드 5pt", frozenset({"round", "round5"})),
    ("+전주 고저", frozenset({"weekhl"})),
    ("+20세션 고저", frozenset({"hl20"})),
    ("+전일 스윙", frozenset({"swings"})),
    ("+선반 가장자리", frozenset({"edges"})),
    ("+2~5일 전 고저", frozenset({"multiday"})),
    ("피봇만(구조 제외)", frozenset({"pivot", "only"})),
    ("라운드10만(구조 제외)", frozenset({"round", "only"})),
    ("전부", frozenset({"oi", "gw", "pivot", "camarilla", "vp", "vwapband", "prevclose", "ma", "round", "weekhl", "hl20", "swings", "edges", "multiday"})),
]


def race_stall(bars, level, band, thr):
    """(첫 터치 여부, 되돌림 우선 판정: 'rev'/'brk'/None, 밴드 안 연속 체류 분)."""
    first = next((j for j, x in enumerate(bars) if x[3] <= level <= x[2]), None)
    if first is None:
        return False, None, None
    out = None
    for x in bars[first:]:
        down, up = x[3] <= level - thr, x[2] >= level + thr
        if down and up:
            break
        if down:
            out = "down"; break
        if up:
            out = "up"; break
    n = 0
    for x in bars[first:]:
        if abs(x[4] - level) <= band and x[3] - band <= level <= x[2] + band:
            n += 1
        else:
            break
    return True, out, n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=W.default_db())
    ap.add_argument("--from", dest="d_from", default="2026-01-05")
    ap.add_argument("--to", dest="d_to", default="2026-09-04")
    ap.add_argument("--null", type=int, default=100)
    ap.add_argument("--sets", nargs="*", default=None, help="이름에 이 문자열이 든 세트만 (예: 기본 라운드 피봇)")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    if not a.force:
        W.guard_intraday()
    sys.stdout.reconfigure(errors="replace")
    n_oi, n_gw = W.load_mahdi_oi(), W.load_mahdi_gamma_walls()
    W.DAYS = W.load_days(a.db); W.DS = list(W.DAYS); ds = W.DS
    idxs = [i for i, d in enumerate(ds) if a.d_from <= d <= a.d_to and i >= 61 and not W.contaminated(i, 6)]
    atr = {i: W.daily_atr(i) for i in idxs}
    opens = {i: W.DAYS[ds[i]][0][1] for i in idxs}
    print(f"표본 {ds[idxs[0]]}~{ds[idxs[-1]]} {len(idxs)}세션 · 마흐디 OI {n_oi}일 · 감마월 {n_gw}일 · 밴드 {BAND_ATR} ATR · 무작위 {a.null}회")

    # 격자 사전 채점 (세션당 한 번)
    grid = {}
    hl = {}
    for i in idxs:
        bars = W.DAYS[ds[i]]; hi, lo = W.day_hl(ds[i]); hl[i] = (hi, lo); px = opens[i]
        b = round((lo - 0.02 * px) / BIN) * BIN
        while b <= hi + 0.02 * px:
            grid[(i, round(b, 2))] = race_stall(bars, b, BAND_ATR * atr[i], 0.005 * px)
            b = round(b + BIN, 2)

    def cell(i, k):
        return grid.get((i, round(round(k / BIN) * BIN, 2)))

    def measure(levels, T=None):
        by = defaultdict(lambda: {"up": [], "dn": []})
        for i, side, k in levels:
            by[i][side].append(k)
        e3 = e5 = n_e = 0
        for i, sides in by.items():
            hi, lo = hl[i]; px = opens[i]
            for side, target in (("up", hi), ("dn", lo)):
                if sides[side]:
                    d = min(abs(target - k) for k in sides[side]) / px
                    n_e += 1; e3 += d <= 0.003; e5 += d <= 0.005
        rev = tot = 0; stalls = []; touched = 0
        for i, side, k in levels:
            c = cell(i, k)
            if c is None:
                continue
            t, out, st = c
            if not t:
                continue
            touched += 1; stalls.append(st)
            if out is not None:
                tot += 1; rev += (out == "down") if side == "up" else (out == "up")
        return dict(e3=e3 / n_e, e5=e5 / n_e, rev=rev / tot if tot else 0, stalls=stalls, miss=1 - touched / len(levels),
                    st=(sum(1 for s in stalls if s >= T) / len(stalls) if (T is not None and stalls) else None))

    def levels_for(sources):
        out = []
        for i in idxs:
            src = frozenset(s for s in sources if s != "only")
            merged = dict(W.candidates(i, 6, src))
            if "only" in sources:   # 구조 원천을 빼고 지정 원천만 — candidates()는 기본을 항상 넣으므로 태그로 거른다
                base = ("매물대", "갭", "전일고", "전일저", "전일VWAP", "4세션VWAP")
                merged = {k: v for k, v in merged.items() if any(not t.startswith(base) for t in v)}
            gap = W.DAYS[ds[i]][0][1] - W.DAYS[ds[i - 1]][-1][4]
            u, dn = W.select_levels(merged, opens[i], dict(RULE, sources=src), atr[i], gap)
            out += [(i, "up", k) for k in u] + [(i, "dn", k) for k in dn]
        return out

    print(f"\n{'원천 세트':28} {'레벨':>5} {'범위밖':>6} │ {'E±0.3%':>7} {'무작위':>6} {'초과':>6} │ {'E±0.5%':>7} {'무작위':>6} {'초과':>6} │ {'B되돌림':>7} {'무작위':>6} │ {'정거장T75':>8} {'무작위':>6} {'배율':>5}")
    for name, sources in SETS:
        if a.sets and not any(w in name for w in a.sets):
            continue
        real = levels_for(sources)
        # 무작위 — ATR 정규화 거리 셔플, T75는 무작위 stall 풀에서
        random.seed(31)
        du = [(k - opens[i]) / atr[i] for i, s, k in real if s == "up"]
        dd = [(opens[i] - k) / atr[i] for i, s, k in real if s == "dn"]
        fakes = []
        for _ in range(a.null):
            random.shuffle(du); random.shuffle(dd); iu = idn = 0; f = []
            for i, s, _k in real:
                if s == "up":
                    f.append((i, "up", opens[i] + du[iu] * atr[i])); iu += 1
                else:
                    f.append((i, "dn", opens[i] - dd[idn] * atr[i])); idn += 1
            fakes.append(f)
        pool = [s for f in fakes for s in measure(f)["stalls"]]
        T75 = float(np.quantile(pool, 0.75)) if pool else 0
        m = measure(real, T75)
        nulls = [measure(f, T75) for f in fakes]
        med = lambda key: statistics.median(x[key] for x in nulls if x[key] is not None)
        q = lambda key, p: sorted(x[key] for x in nulls if x[key] is not None)[int(len(nulls) * p)]
        def mark(v, key, higher=True):
            lo, hi = q(key, .05), q(key, .95)
            return "▲" if (v > hi if higher else v < lo) else ("▼" if (v < lo if higher else v > hi) else " ")
        print(f"{name:28} {len(real):5d} {m['miss']:6.0%} │ {m['e3']:7.1%} {med('e3'):6.1%} {100*(m['e3']-med('e3')):+5.1f}{mark(m['e3'],'e3')} │ "
              f"{m['e5']:7.1%} {med('e5'):6.1%} {100*(m['e5']-med('e5')):+5.1f}{mark(m['e5'],'e5')} │ {m['rev']:7.1%} {med('rev'):6.1%}{mark(m['rev'],'rev')} │ "
              f"{m['st']:8.1%} {med('st'):6.1%} {m['st']/med('st') if med('st') else 0:5.2f}{mark(m['st'],'st')}")
    print("\n▲ = 무작위 90%구간 위(우세), ▼ = 아래(열세), 공백 = 차이없음. 초과 = 실제 − 무작위 중앙(%p). T75 = 각 세트의 무작위 체류 75분위(분).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
