"""당일 **되돌림 고점·저점**(일중 고가·저가)을 시가 시점에 예측하는 규칙의 워크포워드 검증.

2026-09-06 [MW0601] — 선행 두 문서(RESEARCH_PREMARKET_LEVELS_v1 / _WFA_v1)의 결론을 받아 **질문을
바꾼다.** 구조 레벨은 극값을 고르지 못했다(무작위와 동일). 그러나 ATR 투영은 자기 대조군을 2배
이겼다(WFA_v1 §4.2) — 극값의 **위치**가 아니라 **거리**에 정보가 있다는 뜻이다. 그래서 여기서는
「시가에서 얼마나 올라가서 되돌리는가 / 얼마나 내려가서 되돌리는가」를 **거리로 직접 예측**하고,
그 오차 분포를 신뢰도로 제시한다.

## 정의

  대상일 D, 시가 O(08:45), 일중 고가 H, 저가 L.
  상방 되돌림 거리 u = (H − O) / ATR14,  하방 되돌림 거리 d = (O − L) / ATR14.
  예측 시점에 아는 것: O, 전일 종가 C₋₁, 갭 g = (O − C₋₁)/ATR, 전일 범위 r₋₁ = (H₋₁ − L₋₁)/ATR,
  전일 종가 위치 p₋₁ = (C₋₁ − L₋₁)/(H₋₁ − L₋₁), 전일 수익률, 요일, ATR14(전일까지).

## 규칙군 (훈련창에서 계수를 맞추고 다음 날을 예측)

  N0  전일 고저 그대로       H̑ = H₋₁, L̑ = L₋₁                                 (사람들이 가장 흔히 쓰는 기준)
  N1  시가 ± 전일 범위/2      H̑ = O + r₋₁·ATR/2                                  (단순 대칭)
  M1  ATR 중앙값             H̑ = O + med(u)·ATR                                 (변동성 스케일만)
  M2  갭 조건부 ATR 중앙값    갭 버킷(−, 0, +)별 med(u), med(d)                    (갭 방향 비대칭)
  M3  선형(최소절대편차 근사)  u ~ 1 + g + r₋₁ + p₋₁ + ret₋₁ (반복 가중 LS로 중앙값 회귀)
  M4  M3 + 구조 스냅          M3 추정치에서 0.35·ATR 안에 구조 레벨이 있으면 그리로 이동
  M5  M3 + 미륵 피처          09:00~09:10 첫 행의 macro_nasdaq_chg·macro_vix·foreign_futures_net·
                             institution_futures_net (훈련창 안에서 표준화). 08:45가 아니라 **09:05
                             재산출**이다 — 미륵이 장전에는 그 값을 아직 안 채운다(72일 중 43일만).
                             VKOSPI·PCR·GEX는 37일에만 있어 쓰지 않는다.

## 신뢰도

  훈련창 잔차(ATR 단위)의 25/75·10/90 분위로 **50%·80% 구간**을 만들고, 검증일에서 실제 H·L이
  구간에 든 비율(coverage)을 잰다. 점추정의 오차는 MAE(pt·%·ATR)와 ±0.3%·±0.5% 적중률로 낸다.
  구간 폭이 넓어 coverage가 맞는 것과 구간이 좁은데 맞는 것은 다르므로 폭도 같이 적는다.

실행: python scripts/premarket_extremes_wfa.py --test 2026-01-05 2026-09-04 --train 60
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import premarket_levels_wfa as W

SNAP_ATR = 0.35
EXTRA_KEYS = ("macro_nasdaq_chg", "macro_vix", "foreign_futures_net", "institution_futures_net")
EXTRA: dict[str, list[float] | None] = {}


def load_extra(db: Path) -> None:
    """날짜 → 09:00~09:10 첫 행의 미륵 피처. 없으면 None(그날은 M5가 M3로 떨어진다)."""
    import json
    import sqlite3
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    seen = set()
    for ts, f in con.execute("SELECT ts, features FROM raw_features WHERE substr(ts,12,5) BETWEEN '09:00' AND '09:10' ORDER BY ts"):
        d = ts[:10]
        if d in seen:
            continue
        seen.add(d)
        j = json.loads(f)
        vals = [j.get(k) for k in EXTRA_KEYS]
        EXTRA[d] = None if any(v is None for v in vals) else [float(v) for v in vals]
    con.close()


# ---------------------------------------------------------------- 일봉 표

def build_table():
    rows = []
    ds = W.DS
    for i, d in enumerate(ds):
        b = W.DAYS[d]
        o = b[0][1]; h, l = W.day_hl(d); c = b[-1][4]
        rows.append(dict(i=i, d=d, o=o, h=h, l=l, c=c))
    for i, r in enumerate(rows):
        if i < 15:
            r["ok"] = False; continue
        atr = W.daily_atr(i)
        p = rows[i - 1]
        r["atr"] = atr
        r["gap"] = (r["o"] - p["c"]) / atr
        r["r1"] = (p["h"] - p["l"]) / atr
        r["p1"] = (p["c"] - p["l"]) / max(1e-9, p["h"] - p["l"])
        r["ret1"] = (p["c"] - rows[i - 2]["c"]) / atr
        r["u"] = (r["h"] - r["o"]) / atr
        r["dn"] = (r["o"] - r["l"]) / atr
        r["ok"] = not W.contaminated(i, 6)
    return rows


def feats(r):
    return np.array([1.0, r["gap"], r["r1"], r["p1"], r["ret1"]])


def lad_fit(X, y, iters=30):
    """최소절대편차(중앙값 회귀) — 반복 가중 최소제곱 근사."""
    w = np.ones(len(y))
    beta = np.linalg.lstsq(X, y, rcond=None)[0]
    for _ in range(iters):
        res = np.abs(y - X @ beta)
        w = 1.0 / np.maximum(res, 1e-3)
        Xw = X * w[:, None]
        beta = np.linalg.lstsq(Xw.T @ X, Xw.T @ y, rcond=None)[0]
    return beta


def gap_bucket(g):
    return -1 if g < -0.5 else (1 if g > 0.5 else 0)


# ---------------------------------------------------------------- 모델

def fit_models(train):
    m = {}
    u = np.array([r["u"] for r in train]); dn = np.array([r["dn"] for r in train])
    m["M1"] = (float(np.median(u)), float(np.median(dn)))
    m["M2"] = {}
    for bkt in (-1, 0, 1):
        sel = [r for r in train if gap_bucket(r["gap"]) == bkt]
        if len(sel) >= 8:
            m["M2"][bkt] = (statistics.median(r["u"] for r in sel), statistics.median(r["dn"] for r in sel))
        else:
            m["M2"][bkt] = m["M1"]
    X = np.array([feats(r) for r in train])
    m["M3"] = (lad_fit(X, u), lad_fit(X, dn))
    # M5 — 미륵 피처가 있는 훈련행만으로 맞춘다(표준화 파라미터도 훈련창에서)
    sel = [r for r in train if EXTRA.get(r["d"]) is not None]
    if len(sel) >= 30:
        E = np.array([EXTRA[r["d"]] for r in sel])
        mu, sd = E.mean(0), E.std(0) + 1e-9
        X5 = np.array([np.concatenate([feats(r), (np.array(EXTRA[r["d"]]) - mu) / sd]) for r in sel])
        m["M5"] = (lad_fit(X5, np.array([r["u"] for r in sel])), lad_fit(X5, np.array([r["dn"] for r in sel])), mu, sd)
    else:
        m["M5"] = None
    return m


def predict_ratio(name, m, r):
    if name == "M1":
        return m["M1"]
    if name == "M2":
        return m["M2"][gap_bucket(r["gap"])]
    if name in ("M3", "M4"):
        bu, bd = m["M3"]; x = feats(r)
        return float(max(0.0, x @ bu)), float(max(0.0, x @ bd))
    if name == "M5":
        if m["M5"] is None or EXTRA.get(r["d"]) is None:
            return predict_ratio("M3", m, r)
        bu, bd, mu, sd = m["M5"]
        x = np.concatenate([feats(r), (np.array(EXTRA[r["d"]]) - mu) / sd])
        return float(max(0.0, x @ bu)), float(max(0.0, x @ bd))
    raise KeyError(name)


def residual_bands(name, m, train):
    ru, rd = [], []
    for r in train:
        pu, pd = predict_ratio(name, m, r)
        ru.append(r["u"] - pu); rd.append(r["dn"] - pd)
    q = lambda a, p: float(np.quantile(a, p))
    return dict(u50=(q(ru, .25), q(ru, .75)), u80=(q(ru, .10), q(ru, .90)),
                d50=(q(rd, .25), q(rd, .75)), d80=(q(rd, .10), q(rd, .90)))


def snap(level: float, i: int, side: str, atr: float):
    merged = W.candidates(i, 6, frozenset())
    o = W.DAYS[W.DS[i]][0][1]
    pool = [k for k in merged if (k > o + 1) == (side == "up")]
    if not pool:
        return level
    near = min(pool, key=lambda k: abs(k - level))
    return float(near) if abs(near - level) <= SNAP_ATR * atr else level


def point_levels(name, m, r, rows):
    o, atr, i = r["o"], r["atr"], r["i"]
    if name == "N0":
        p = rows[i - 1]; return p["h"], p["l"]
    if name == "N1":
        half = r["r1"] * atr / 2; return o + half, o - half
    pu, pd = predict_ratio(name, m, r)
    H, L = o + pu * atr, o - pd * atr
    if name == "M4":
        H, L = snap(H, i, "up", atr), snap(L, i, "dn", atr)
    return H, L


# ---------------------------------------------------------------- 검증

MODELS = ("N0", "N1", "M1", "M2", "M3", "M4", "M5")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="당일 되돌림 고점·저점 예측 WFA")
    ap.add_argument("--db", type=Path, default=W.default_db())
    ap.add_argument("--test", nargs=2, required=True)
    ap.add_argument("--train", type=int, default=60)
    ap.add_argument("--show", nargs="*", default=["2026-09-04"], help="예측 상세를 찍을 날짜")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    if not a.force:
        W.guard_intraday()
    sys.stdout.reconfigure(errors="replace")
    W.DAYS = W.load_days(a.db); W.DS = list(W.DAYS)
    load_extra(a.db)
    rows = build_table()
    test = [r for r in rows if a.test[0] <= r["d"] <= a.test[1] and r["ok"] and r["i"] >= a.train + 16]
    print(f"검증 {test[0]['d']}~{test[-1]['d']} {len(test)}세션 · 훈련창 {a.train}세션 (만기 오염·품질 제외)")

    stats = {n: dict(eh=[], el=[], hit3=0, hit5=0, cov50=0, cov80=0, w50=[], w80=[], n=0) for n in MODELS}
    for r in test:
        i = r["i"]
        train = [x for x in rows[max(16, i - a.train - 10):i] if x["ok"] and x["i"] >= 16][-a.train:]
        if len(train) < 30:
            continue
        m = fit_models(train)
        pct = 100.0 / r["o"]
        for n in MODELS:
            H, L = point_levels(n, m, r, rows)
            s = stats[n]; s["n"] += 1
            s["eh"].append(abs(H - r["h"])); s["el"].append(abs(L - r["l"]))
            s["hit3"] += (abs(H - r["h"]) * pct <= 0.3) + (abs(L - r["l"]) * pct <= 0.3)
            s["hit5"] += (abs(H - r["h"]) * pct <= 0.5) + (abs(L - r["l"]) * pct <= 0.5)
            if n in ("M1", "M2", "M3", "M4", "M5"):
                b = residual_bands(n if n != "M4" else "M3", m, train)
                pu, pd = predict_ratio(n, m, r)
                atr = r["atr"]
                for tag, (lo, hi) in (("50", b["u50"]), ("80", b["u80"])):
                    lo_H, hi_H = r["o"] + (pu + lo) * atr, r["o"] + (pu + hi) * atr
                    s["cov" + tag] += lo_H <= r["h"] <= hi_H
                    s["w" + tag].append((hi_H - lo_H) * pct)
                for tag, (lo, hi) in (("50", b["d50"]), ("80", b["d80"])):
                    lo_L, hi_L = r["o"] - (pd + hi) * atr, r["o"] - (pd + lo) * atr
                    s["cov" + tag] += lo_L <= r["l"] <= hi_L
                    s["w" + tag].append((hi_L - lo_L) * pct)
            if r["d"] in a.show:
                print(f"\n[{r['d']}] {n}: 시가 {r['o']:.2f} ATR {r['atr']:.1f} 갭 {r['gap']:+.2f}ATR → 예측 고점 {H:.1f} / 저점 {L:.1f}"
                      f"  (실제 고 {r['h']:.2f} 저 {r['l']:.2f}, 오차 {H-r['h']:+.1f} / {L-r['l']:+.1f})")

    print(f"\n## 점추정 오차 (관측 = 세션×측, 고·저 합산)\n{'모델':6} {'n':>4} {'MAE pt':>8} {'MAE %':>7} {'MAE/ATR':>8} {'±0.3%적중':>9} {'±0.5%적중':>9}")
    for n in MODELS:
        s = stats[n]
        if not s["n"]:
            continue
        err = s["eh"] + s["el"]
        mae = statistics.mean(err)
        pcts = [e * 100 / r["o"] for e, r in zip(s["eh"], test)] + [e * 100 / r["o"] for e, r in zip(s["el"], test)]
        atrs = [e / r["atr"] for e, r in zip(s["eh"], test)] + [e / r["atr"] for e, r in zip(s["el"], test)]
        print(f"{n:6} {2*s['n']:4d} {mae:8.1f} {statistics.mean(pcts):6.2f}% {statistics.mean(atrs):8.2f} "
              f"{s['hit3']/(2*s['n']):8.0%} {s['hit5']/(2*s['n']):8.0%}")
    # 변동성 3분위별 M1 오차 — 신뢰도가 시장 상태에 따라 얼마나 다른지
    vol = sorted(r["atr"] / r["o"] for r in test)
    cut = (vol[len(vol) // 3], vol[2 * len(vol) // 3])
    print(f"\n## M1 오차의 변동성(ATR/시가) 3분위 분해  (경계 {cut[0]*100:.1f}% / {cut[1]*100:.1f}%)")
    print(f"{'분위':10} {'n':>4} {'MAE pt':>8} {'MAE %':>7} {'±0.5%적중':>9} {'ATR 중앙(pt)':>12}")
    for name, sel in (("낮음", lambda v: v <= cut[0]), ("중간", lambda v: cut[0] < v <= cut[1]), ("높음", lambda v: v > cut[1])):
        sub = [(e, r) for e, r in zip(stats["M1"]["eh"], test) if sel(r["atr"] / r["o"])] + \
              [(e, r) for e, r in zip(stats["M1"]["el"], test) if sel(r["atr"] / r["o"])]
        if not sub:
            continue
        print(f"{name:10} {len(sub):4d} {statistics.mean(e for e, _ in sub):8.1f} {statistics.mean(e*100/r['o'] for e, r in sub):6.2f}% "
              f"{sum(1 for e, r in sub if e*100/r['o'] <= 0.5)/len(sub):8.0%} {statistics.median(r['atr'] for _, r in sub):12.1f}")

    print(f"\n## 구간 신뢰도 (훈련 잔차 분위로 만든 구간이 실제 고·저를 담은 비율 / 구간 폭 중앙값)\n{'모델':6} {'50%구간 coverage':>16} {'폭%':>6} {'80%구간 coverage':>16} {'폭%':>6}")
    for n in ("M1", "M2", "M3", "M4", "M5"):
        s = stats[n]
        if not s["n"]:
            continue
        print(f"{n:6} {s['cov50']/(2*s['n']):16.0%} {statistics.median(s['w50']):5.2f}% {s['cov80']/(2*s['n']):16.0%} {statistics.median(s['w80']):5.2f}%")

    # 예측 상세 — 구간 포함
    for r in test:
        if r["d"] not in a.show:
            continue
        i = r["i"]
        train = [x for x in rows[max(16, i - a.train - 10):i] if x["ok"]][-a.train:]
        m = fit_models(train)
        print(f"\n## {r['d']} 예측표 (시가 {r['o']:.2f}, ATR14 {r['atr']:.1f}, 갭 {r['gap']:+.2f}ATR, 전일범위 {r['r1']:.2f}ATR)")
        print(f"{'모델':6} {'고점':>8} {'50%구간':>16} {'80%구간':>16} {'저점':>8} {'50%구간':>16} {'80%구간':>16}")
        for n in ("M1", "M2", "M3", "M4", "M5"):
            H, L = point_levels(n, m, r, rows)
            b = residual_bands(n if n != "M4" else "M3", m, train)
            pu, pd = predict_ratio(n, m, r); atr = r["atr"]; o = r["o"]
            hu50 = (o + (pu + b['u50'][0]) * atr, o + (pu + b['u50'][1]) * atr)
            hu80 = (o + (pu + b['u80'][0]) * atr, o + (pu + b['u80'][1]) * atr)
            ld50 = (o - (pd + b['d50'][1]) * atr, o - (pd + b['d50'][0]) * atr)
            ld80 = (o - (pd + b['d80'][1]) * atr, o - (pd + b['d80'][0]) * atr)
            print(f"{n:6} {H:8.1f} {'%.1f~%.1f' % hu50:>16} {'%.1f~%.1f' % hu80:>16} {L:8.1f} {'%.1f~%.1f' % ld50:>16} {'%.1f~%.1f' % ld80:>16}")
        print(f"실제   고 {r['h']:.2f} 저 {r['l']:.2f}")
        bu, bd = m["M3"]
        print(f"M3 계수(u): 절편 {bu[0]:+.2f} 갭 {bu[1]:+.2f} 전일범위 {bu[2]:+.2f} 종가위치 {bu[3]:+.2f} 전일수익 {bu[4]:+.2f}")
        print(f"M3 계수(d): 절편 {bd[0]:+.2f} 갭 {bd[1]:+.2f} 전일범위 {bd[2]:+.2f} 종가위치 {bd[3]:+.2f} 전일수익 {bd[4]:+.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
