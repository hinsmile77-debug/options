"""「정거장이 맞는 정도」를 수치로 — 장전 맥점 레벨의 **마디 역할**을 네 지표로 잰다.

2026-09-06 [MW0601] — `RESEARCH_PREMARKET_LEVELS_WFA_v1.md` §5의 후속. 그 문서가 답한 것은
「극값(종점)을 고르는가」였고 답은 아니오였다. 이 스크립트가 답하는 것은 **「마디(정거장) 노릇은
하는가」**이고, 그것은 다른 질문이다.

## 왜 터치율이 아닌가

일중 가격 경로는 연속이므로 **레벨이 그날 범위 안에 있으면 반드시 터치된다.** 즉 터치율은
「레벨이 맞았는가」가 아니라 「레벨이 그날 범위 안이었는가」를 재는 것이고, 무작위 레벨도 같은
거리 분포면 같은 값이 나온다(실측 80% vs 무작위 78% — 선행 문서 §4.2). 그래서 아래 네 지표를 쓴다.

  A. **체류 집중도** — 레벨 ±허용 안에서 보낸 분 수. 마디면 가격이 그 자리에서 머뭇거린다.
  B. **경주(race)** — 첫 터치 후, 되돌림 X가 먼저인가 돌파 X가 먼저인가. 저항이면 되돌림이 먼저다.
       무작위보행에서는 대칭 임계일 때 정확히 50%다(이론 귀무값). 셔플 대조군도 함께 낸다.
  C. **정지 시간** — 첫 터치 후 ±X를 벗어나는 데 걸린 분. 마디면 오래 걸린다.
  D. **극값 근접도** — 일중 고가와 가장 가까운 상방 레벨의 거리(저가도 동일). 종점을 못 골라도
       *가까이는* 가는지를 연속값으로 본다.

## 대조군 (두 가지 — 반드시 함께 읽는다)

같은 날의 **가격 격자 전체**를 미리 채점해 두고, 실제 레벨의 거리 분포를 날짜 간 셔플해 뽑는다.

  raw : 시가 대비 **절대 거리(pt)** 를 셔플. 단순하지만 **변동성 교란**이 있다 — 실제 레벨은
        최근 세션에서 나오므로 오늘의 변동성에 자동으로 맞춰지는데, 셔플은 조용한 날의 거리를
        큰 날에 붙인다. 그러면 무작위 레벨이 그날 범위 밖으로 자주 나가 불리해진다.
  atr : 거리를 **그날 ATR14로 나눈 값**을 셔플하고 다시 그날 ATR을 곱한다. 변동성 스케일은
        보존하고 **구조적 위치만** 파괴한다. 이쪽이 공정한 귀무가설이다.

미륵 DB는 읽기 전용, 장중 실행 금지 — `premarket_levels_wfa.py`와 같은 규약.

실행: python scripts/premarket_levels_station_test.py --from 2025-10-01 --to 2026-09-04
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import premarket_levels_wfa as W

BIN = 0.5
THRESHOLDS = (0.003, 0.005, 0.010)   # 경주·정지의 이동 임계 (가격 대비)
TOLS = (0.0015, 0.003, 0.005)        # 체류·극값의 허용 (가격 대비)
RULE = dict(lookback=6, sources=frozenset(), spacing="nearest", gap_tilt=0, k=1.0)


def day_dwell(d: str) -> dict[float, int]:
    """가격 bin별 체류 분 수 — 봉의 고저가 지나간 bin을 1분씩 센다."""
    h: dict[float, int] = defaultdict(int)
    for _, _, hi, lo, _, _ in W.DAYS[d]:
        b = round(lo / BIN) * BIN
        while b <= hi + 1e-9:
            h[b] += 1
            b = round(b + BIN, 2)
    return h


def dwell_minutes(hist: dict[float, int], level: float, tol: float) -> int:
    b = round((level - tol) / BIN) * BIN
    total = 0
    while b <= level + tol + 1e-9:
        total += hist.get(round(b, 2), 0)
        b = round(b + BIN, 2)
    return total


def race(bars, level: float, side: str, thr: float):
    """첫 터치 후 되돌림/돌파 경주. 반환 (결과, 정지분) — 결과 ∈ {"rev","brk",None}."""
    first = None
    for j, x in enumerate(bars):
        if x[3] <= level <= x[2]:
            first = j
            break
    if first is None:
        return None, None
    for j in range(first, len(bars)):
        x = bars[j]
        down = x[3] <= level - thr
        up = x[2] >= level + thr
        if down and up:      # 같은 봉에서 양쪽 — 판정 불가
            return None, j - first
        if down:
            return ("rev" if side == "up" else "brk"), j - first
        if up:
            return ("brk" if side == "up" else "rev"), j - first
    return None, len(bars) - first


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="장전 맥점 레벨의 마디 역할 측정")
    ap.add_argument("--db", type=Path, default=W.default_db())
    ap.add_argument("--from", dest="d_from", default="2025-10-01")
    ap.add_argument("--to", dest="d_to", default="2026-09-04")
    ap.add_argument("--null", type=int, default=200)
    ap.add_argument("--ref", choices=("open", "close"), default="open",
                    help="상·하 분기 기준가 — 당일 08:45 시가(open) 또는 전일 종가(close)")
    ap.add_argument("--max-range-pct", type=float, default=None,
                    help="일중 범위/시가가 이 값(%%) 이하인 세션만 — 변동성 조건부 검정")
    ap.add_argument("--oi", action="store_true", help="옵션 OI 행사가 원천을 마흐디 DB에서 붙인다(원래 4원천 모델)")
    ap.add_argument("--at", default=None, help="재산출 시각(예 09:30): 기준가=그 시각 현재가, 오프닝 레인지 고·저를 후보에 추가")
    ap.add_argument("--target", choices=("day", "after"), default="day",
                    help="극값 표적 — 그날 전체(day) 또는 --cut 이후 남은 구간(after)")
    ap.add_argument("--cut", default=None, help="after 표적의 시작 시각(기본 = --at). 08:45 레벨을 09:30 이후 극값으로 채점할 때 --cut 09:30")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    if not a.force:
        W.guard_intraday()
    sys.stdout.reconfigure(errors="replace")
    if a.oi:
        n_oi = W.load_mahdi_oi()
        RULE["sources"] = frozenset({"oi"})
        print(f"옵션 OI 원천(마흐디 DB): {n_oi}일 적재")

    W.DAYS = W.load_days(a.db)
    W.DS = list(W.DAYS)
    ds = W.DS
    idxs = [i for i, d in enumerate(ds) if a.d_from <= d <= a.d_to and i >= 21
            and not W.contaminated(i, RULE["lookback"])]
    if a.max_range_pct is not None:
        def rng_pct(i):
            hi, lo = W.day_hl(ds[i])
            return 100 * (hi - lo) / W.DAYS[ds[i]][0][1]
        idxs = [i for i in idxs if rng_pct(i) <= a.max_range_pct]
        print(f"변동성 조건: 일중 범위/시가 ≤ {a.max_range_pct}%")
    if len(idxs) < 10:
        print("표본이 10세션 미만이다 — 재지 않는다"); return 1
    print(f"표본: {ds[idxs[0]]}~{ds[idxs[-1]]} {len(idxs)}세션 (만기 오염·품질 제외 후)")

    # 표적 구간 — 그날 전체 또는 --at 이후
    cut = a.cut or a.at
    def bars_of(i):
        b = W.DAYS[ds[i]]
        return [x for x in b if x[0] > cut] if (a.target == "after" and cut) else b

    def hl_of(i):
        b = bars_of(i)
        return max(x[2] for x in b), min(x[3] for x in b)
    idxs = [i for i in idxs if len(bars_of(i)) >= 30]

    # 실제 레벨 — 기준가(ref)에서 위로 3개 / 아래로 3개
    if a.at:
        early = {i: [x for x in W.DAYS[ds[i]] if x[0] <= a.at] for i in idxs}
        idxs = [i for i in idxs if early[i]]
        refs = {i: early[i][-1][4] for i in idxs}                          # --at 시각 현재가
    else:
        refs = {i: (W.DAYS[ds[i]][0][1] if a.ref == "open" else W.DAYS[ds[i - 1]][-1][4]) for i in idxs}
    real = []   # (i, side, level)
    for i in idxs:
        merged = dict(W.candidates(i, RULE["lookback"], RULE["sources"]))
        if a.at:  # 오프닝 레인지 고·저를 후보로 (구조 후보와 1.5pt 안이면 합류)
            oh, ol = max(x[2] for x in early[i]), min(x[3] for x in early[i])
            for lv, tag in ((round(oh), f"OR고{a.at}"), (round(ol), f"OR저{a.at}")):
                key = next((k for k in merged if abs(k - lv) <= 1.5), None)
                if key is None:
                    merged[lv] = [tag]
                else:
                    merged[key] = merged[key] + [tag]
        gap = W.DAYS[ds[i]][0][1] - W.DAYS[ds[i - 1]][-1][4]
        u, dn = W.select_levels(merged, refs[i], RULE, W.daily_atr(i), gap)
        real += [(i, "up", k) for k in u] + [(i, "dn", k) for k in dn]
    ref_label = (f"{a.at} 현재가 + 오프닝 레인지 후보" if a.at else ("당일 08:45 시가" if a.ref == "open" else "전일 종가(15:08 봉)"))
    print(f"기준가 = {ref_label} · 표적 = {'그날 전체 극값' if a.target == 'day' else cut + ' 이후 극값'} · 레벨 {len(real)}개 · 세션 {len(idxs)}")

    hists = {i: day_dwell(ds[i]) for i in idxs}
    opens = refs   # 거리·격자의 원점도 같은 기준가

    # 날짜별 격자 사전 채점 — 실제·무작위가 같은 표에서 값을 읽는다
    grid: dict[tuple[int, float, str], dict] = {}
    for i in idxs:
        bars = bars_of(i)
        hi, lo = hl_of(i)
        px = opens[i]
        b = round((lo - 0.02 * px) / BIN) * BIN
        top = hi + 0.02 * px
        while b <= top:
            for side in ("up", "dn"):
                cell = {"dwell": {t: dwell_minutes(hists[i], b, t * px) for t in TOLS}}
                for thr in THRESHOLDS:
                    cell[thr] = race(bars, b, side, thr * px)
                grid[(i, round(b, 2), side)] = cell
            b = round(b + BIN, 2)

    def cell_of(i, level, side):
        return grid.get((i, round(round(level / BIN) * BIN, 2), side))

    def measure(levels):
        dwell = {t: [] for t in TOLS}
        rr = {thr: [0, 0] for thr in THRESHOLDS}      # [반전, 판정된 것]
        stall = {thr: [] for thr in THRESHOLDS}
        for i, side, k in levels:
            c = cell_of(i, k, side)
            if c is None:
                continue
            for t in TOLS:
                dwell[t].append(c["dwell"][t])
            for thr in THRESHOLDS:
                out, mins = c[thr]
                if out is not None:
                    rr[thr][1] += 1
                    rr[thr][0] += out == "rev"
                if mins is not None:
                    stall[thr].append(mins)
        return dwell, rr, stall

    def extremes(levels):
        """일중 고가/저가와 가장 가까운 같은 편 레벨의 거리(가격 대비 %)."""
        by_day = defaultdict(lambda: {"up": [], "dn": []})
        for i, side, k in levels:
            by_day[i][side].append(k)
        out = []
        for i, sides in by_day.items():
            hi, lo = hl_of(i)
            px = opens[i]
            if sides["up"]:
                out.append(min(abs(hi - k) for k in sides["up"]) / px)
            if sides["dn"]:
                out.append(min(abs(lo - k) for k in sides["dn"]) / px)
        return out

    def landing(levels, tol):
        e = extremes(levels)
        return sum(1 for x in e if x <= tol) / len(e) if e else 0.0

    d_real, r_real, s_real = measure(real)
    e_real = extremes(real)

    # 무작위 — 거리 분포를 날짜 간 셔플 (raw = pt 그대로, atr = ATR 단위로 정규화 후)
    atr = {i: W.daily_atr(i) for i in idxs}

    def run_null(mode: str):
        random.seed(17)
        unit = (lambda i: 1.0) if mode == "raw" else (lambda i: max(atr[i], 1e-9))
        du = [(k - opens[i]) / unit(i) for i, s, k in real if s == "up"]
        dd = [(opens[i] - k) / unit(i) for i, s, k in real if s == "dn"]
        dw = {t: [] for t in TOLS}
        land = {t: [] for t in TOLS}
        rr = {thr: [] for thr in THRESHOLDS}
        st = {thr: [] for thr in THRESHOLDS}
        ex, miss = [], []
        for _ in range(a.null):
            random.shuffle(du); random.shuffle(dd)
            iu = idn = 0
            fake = []
            for i, side, _k in real:
                if side == "up":
                    fake.append((i, "up", opens[i] + du[iu] * unit(i))); iu += 1
                else:
                    fake.append((i, "dn", opens[i] - dd[idn] * unit(i))); idn += 1
            d_n, r_n, s_n = measure(fake)
            for t in TOLS:
                dw[t].append(statistics.mean(d_n[t]) if d_n[t] else 0)
            for thr in THRESHOLDS:
                rr[thr].append(r_n[thr][0] / r_n[thr][1] if r_n[thr][1] else 0)
                st[thr].append(statistics.median(s_n[thr]) if s_n[thr] else 0)
            ex.append(statistics.median(extremes(fake)))
            miss.append(out_of_range(fake))
            for t in TOLS:
                land[t].append(landing(fake, t))
        return dw, rr, st, ex, miss, land

    def out_of_range(levels) -> float:
        n = bad = 0
        for i, _side, k in levels:
            hi, lo = hl_of(i)
            n += 1
            bad += not (lo <= k <= hi)
        return bad / n if n else 0.0

    nulls = {m: run_null(m) for m in ("raw", "atr")}

    def band(xs):
        xs = sorted(xs)
        return xs[len(xs) // 2], xs[int(len(xs) * 0.05)], xs[int(len(xs) * 0.95)]

    def cmp_line(real_val, xs, fmt="%.1f", better="high"):
        med, lo, hi = band(xs)
        mark = "우세" if (real_val > hi if better == "high" else real_val < lo) else (
               "열세" if (real_val < lo if better == "high" else real_val > hi) else "차이없음")
        return f"{fmt % med:>9} {('%s~%s' % (fmt % lo, fmt % hi)):>17} {mark:>7}"

    print(f"레벨이 그날 범위 밖(미터치): 실제 {out_of_range(real):.0%} · "
          f"무작위raw {statistics.median(nulls['raw'][4]):.0%} · 무작위atr {statistics.median(nulls['atr'][4]):.0%}\n")

    print("## A. 체류 집중도 — 레벨 ±허용 안에서 보낸 분 (하루 384분 중, 높을수록 마디)")
    print(f"{'허용':>8} {'실제':>8} │ {'무작위raw':>9} {'90%구간':>17} {'판정':>7} │ {'무작위atr':>9} {'90%구간':>17} {'판정':>7}")
    for t in TOLS:
        m = statistics.mean(d_real[t])
        print(f"{'±%.2f%%' % (t*100):>8} {m:8.1f} │ {cmp_line(m, nulls['raw'][0][t])} │ {cmp_line(m, nulls['atr'][0][t])}")

    print("\n## B. 경주 — 첫 터치 후 되돌림이 먼저인가 (무작위보행 귀무값 50%, 높을수록 마디)")
    print(f"{'임계':>8} {'판정n':>6} {'실제':>7} │ {'무작위raw':>9} {'90%구간':>17} {'판정':>7} │ {'무작위atr':>9} {'90%구간':>17} {'판정':>7}")
    for thr in THRESHOLDS:
        h, n = r_real[thr]
        v = 100 * h / n if n else 0
        print(f"{'±%.1f%%' % (thr*100):>8} {n:6d} {v:6.1f}% │ "
              f"{cmp_line(v, [100*x for x in nulls['raw'][1][thr]])} │ {cmp_line(v, [100*x for x in nulls['atr'][1][thr]])}")

    print("\n## C. 정지 시간 — 첫 터치 후 ±임계를 벗어나는 데 걸린 분 (중앙값, 길수록 마디)")
    print(f"{'임계':>8} {'실제':>8} │ {'무작위raw':>9} {'90%구간':>17} {'판정':>7} │ {'무작위atr':>9} {'90%구간':>17} {'판정':>7}")
    for thr in THRESHOLDS:
        m = statistics.median(s_real[thr]) if s_real[thr] else 0
        print(f"{'±%.1f%%' % (thr*100):>8} {m:8.0f} │ {cmp_line(m, nulls['raw'][2][thr], '%.0f')} │ {cmp_line(m, nulls['atr'][2][thr], '%.0f')}")

    med_e = 100 * statistics.median(e_real)
    print("\n## D. 극값 근접도 — 일중 고가(저가)와 가장 가까운 같은 편 레벨의 거리 (작을수록 마디)")
    print(f"{'':>8} {'실제':>8} │ {'무작위raw':>9} {'90%구간':>17} {'판정':>7} │ {'무작위atr':>9} {'90%구간':>17} {'판정':>7}")
    print(f"{'중앙값':>8} {med_e:7.2f}% │ {cmp_line(med_e, [100*x for x in nulls['raw'][3]], '%.2f', 'low')} │ "
          f"{cmp_line(med_e, [100*x for x in nulls['atr'][3]], '%.2f', 'low')}")
    q = sorted(e_real)
    print(f"  실제 분포: 하위25% {q[len(q)//4]*100:.2f}% · 중앙 {med_e:.2f}% · 상위25% {q[3*len(q)//4]*100:.2f}%")

    print("\n## E. 극값 안착률 — 그 거리가 허용 안에 든 비율 (D의 이산 버전)")
    print(f"{'허용':>8} {'실제':>8} │ {'무작위raw':>9} {'90%구간':>17} {'판정':>7} │ {'무작위atr':>9} {'90%구간':>17} {'판정':>7}")
    for t in TOLS:
        v = 100 * landing(real, t)
        print(f"{'±%.2f%%' % (t*100):>8} {v:7.1f}% │ "
              f"{cmp_line(v, [100*x for x in nulls['raw'][5][t]])} │ {cmp_line(v, [100*x for x in nulls['atr'][5][t]])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
