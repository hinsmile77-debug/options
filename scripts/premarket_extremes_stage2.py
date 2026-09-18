"""되돌림 고점·저점 **2단계** 예측 — 08:45 확정분(5개)로 1차, 피처가 안정된 T시각에 미륵 피처로 재예측.

2026-09-06 [MW0601] — `premarket_extremes_wfa.py`(1단계)의 후속. 사용자 요구: 「08:45 확정분 3~5개 +
피처값이 안정된 뒤(예 09:30)의 미륵 피처를 반영해 예측·검증하라.」

## 안정 시각의 결정 (데이터로)

미륵 `raw_features`의 분별 결측률 실측(2026-01-05~09-04, 약 158세션):
  - 08:46~08:50: 매크로·수급 피처가 **7~9%** 일수에만 있다 → 장전 재산출 불가.
  - 09:00부터: macro_nasdaq_chg·macro_vix·foreign/institution/retail_futures_net·program_arb_net·
    cvd_delta_norm·ofi_norm·hurst·poc_distance **100%**. 이것이 「값이 존재하는」 안정 시각.
  - opt_chain_pcr·opt_gex_bn·vkospi·vpin·kyle_lambda·trend_efficiency: **22~24%**(라이브 수집일만,
    나머지는 백필 행) → 쓰지 않는다. hurst_ready 플래그도 09:30에야 22%.
  - 수급 피처는 「존재」와 「정보」가 다르다 — 09:00의 외국인 순매수는 15분치라 잡음이다. 그래서
    T를 09:05 / 09:30 / 10:00 세 개로 두고 **어느 T가 검증 오차를 줄이는지**로 정한다.

## 2단계에서 아는 것

  1단계 입력(08:45 확정분 5개): 시가 O, 갭 g, ATR14, 전일 범위 r₋₁, 전일 종가 위치 p₋₁.
  T시각 추가: 그때까지의 고가·저가·현재가(경로), 미륵 피처 벡터.
  **경로가 하한을 준다** — T까지의 고가는 그날 고가의 하한이다. 그래서 「피처 덕」과 「경로 덕」을
  가르기 위해 경로만 쓰는 모델(P1)을 반드시 같이 낸다.

## 모델 (T시각)

  P0  1단계 M1을 경로로 자른 것      H̑ = max(M1_H, 지금까지 고가)         — 갱신의 하한선
  P1  경로 회귀                       u ~ 1 + g + r₋₁ + u_T + d_T + ret_T   (미륵 피처 없음)
  P2  P1 + 미륵 피처 10개 (릿지)       표준화 후 λ=3 릿지                      (전부)
  P3  P1 + 선별 3개 (LAD)             foreign_futures_net·macro_nasdaq_chg·cvd_delta_norm (사전 선택)
  P4  P1 + foreign_futures_net 하나    단일 피처 — 「피처가 나쁜가, 개수가 나쁜가」
  P5  P1 + program_arb_net 하나        단일 피처
  P6  경로만 릿지                      추정기(LAD vs 릿지) 효과 분리
  모든 예측은 경로 하한/상한으로 잘라낸다(H̑ ≥ 지금까지 고가, L̑ ≤ 지금까지 저가).

실행: python scripts/premarket_extremes_stage2.py --test 2026-01-05 2026-09-04 --train 60 --at 09:05 09:30 10:00
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import premarket_extremes_wfa as E
import premarket_levels_wfa as W

FEAT_KEYS = ("macro_nasdaq_chg", "macro_vix", "foreign_futures_net", "institution_futures_net",
             "retail_futures_net", "program_arb_net", "cvd_delta_norm", "ofi_norm", "hurst", "poc_distance")
SUBSET = ("foreign_futures_net", "macro_nasdaq_chg", "cvd_delta_norm")
RIDGE_LAMBDA = 3.0


def load_feats_at(db: Path, at: str) -> dict[str, list[float] | None]:
    """날짜 → T시각(T~T+4분 첫 행)의 피처 벡터. 하나라도 없으면 None."""
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    hi = f"{int(at[:2]):02d}:{int(at[3:]) + 4:02d}"
    out: dict[str, list[float] | None] = {}
    for ts, f in con.execute("SELECT ts, features FROM raw_features WHERE substr(ts,12,5) BETWEEN ? AND ? ORDER BY ts", (at, hi)):
        d = ts[:10]
        if d in out:
            continue
        j = json.loads(f)
        vals = [j.get(k) for k in FEAT_KEYS]
        out[d] = None if any(v is None for v in vals) else [float(v) for v in vals]
    con.close()
    return out


def path_at(r, at: str):
    """T시각까지의 경로 — (u_T, d_T, ret_T) ATR 단위. T 이전 봉이 없으면 None."""
    bars = [b for b in W.DAYS[r["d"]] if b[0] <= at]
    if not bars:
        return None
    o, atr = r["o"], r["atr"]
    return ((max(b[2] for b in bars) - o) / atr, (o - min(b[3] for b in bars)) / atr, (bars[-1][4] - o) / atr)


def x_path(r, p):
    return np.array([1.0, r["gap"], r["r1"], p[0], p[1], p[2]])


def ridge_fit(X, y, lam):
    n = X.shape[1]
    I = np.eye(n); I[0, 0] = 0.0
    return np.linalg.solve(X.T @ X + lam * I, X.T @ y)


def fit_stage2(train, at, feats):
    rows = [(r, path_at(r, at), feats.get(r["d"])) for r in train]
    rows = [(r, p, f) for r, p, f in rows if p is not None]
    m = {"n": len(rows)}
    if len(rows) < 30:
        return None
    Xp = np.array([x_path(r, p) for r, p, _ in rows])
    u = np.array([r["u"] for r, _, _ in rows]); dn = np.array([r["dn"] for r, _, _ in rows])
    m["P1"] = (E.lad_fit(Xp, u), E.lad_fit(Xp, dn))
    withf = [(r, p, f) for r, p, f in rows if f is not None]
    if len(withf) >= 30:
        F = np.array([f for _, _, f in withf]); mu, sd = F.mean(0), F.std(0) + 1e-9
        Xf = np.array([np.concatenate([x_path(r, p), (np.array(f) - mu) / sd]) for r, p, f in withf])
        uf = np.array([r["u"] for r, _, _ in withf]); df = np.array([r["dn"] for r, _, _ in withf])
        m["P2"] = (ridge_fit(Xf, uf, RIDGE_LAMBDA), ridge_fit(Xf, df, RIDGE_LAMBDA), mu, sd)
        idx = [FEAT_KEYS.index(k) for k in SUBSET]
        Xs = np.array([np.concatenate([x_path(r, p), ((np.array(f) - mu) / sd)[idx]]) for r, p, f in withf])
        m["P3"] = (E.lad_fit(Xs, uf), E.lad_fit(Xs, df), mu, sd, idx)
        # 단일 피처 변형 — 피처가 나쁜지, 개수가 나쁜지 가른다
        for name, key in (("P4", "foreign_futures_net"), ("P5", "program_arb_net")):
            j = [FEAT_KEYS.index(key)]
            Xj = np.array([np.concatenate([x_path(r, p), ((np.array(f) - mu) / sd)[j]]) for r, p, f in withf])
            m[name] = (E.lad_fit(Xj, uf), E.lad_fit(Xj, df), mu, sd, j)
    # 경로만 릿지 — 추정기(LAD vs 릿지) 효과 분리
    m["P6"] = (ridge_fit(Xp, u, RIDGE_LAMBDA), ridge_fit(Xp, dn, RIDGE_LAMBDA))
    m["train_rows"] = rows
    return m


def predict_stage2(name, m, m1, r, p, f):
    """(u_hat, d_hat) ATR 단위 — 경로 하한 적용 전."""
    if name == "P0":
        return m1
    if name == "P1" or (name in ("P2", "P3", "P4", "P5") and (f is None or name not in m)):
        bu, bd = m["P1"]; x = x_path(r, p)
        return float(x @ bu), float(x @ bd)
    if name == "P6":
        bu, bd = m["P6"]; x = x_path(r, p)
        return float(x @ bu), float(x @ bd)
    if name == "P2":
        bu, bd, mu, sd = m["P2"]; x = np.concatenate([x_path(r, p), (np.array(f) - mu) / sd])
        return float(x @ bu), float(x @ bd)
    bu, bd, mu, sd, idx = m[name]; x = np.concatenate([x_path(r, p), ((np.array(f) - mu) / sd)[idx]])
    return float(x @ bu), float(x @ bd)


def bands(name, m, m1, feats, at):
    ru, rd = [], []
    for r, p, f in m["train_rows"]:
        pu, pd = predict_stage2(name, m, m1, r, p, f)
        ru.append(r["u"] - max(pu, p[0])); rd.append(r["dn"] - max(pd, p[1]))
    q = lambda a, k: float(np.quantile(a, k))
    return dict(u50=(q(ru, .25), q(ru, .75)), u80=(q(ru, .1), q(ru, .9)), d50=(q(rd, .25), q(rd, .75)), d80=(q(rd, .1), q(rd, .9)))


MODELS = ("P0", "P1", "P2", "P3", "P4", "P5", "P6")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="되돌림 고점·저점 2단계(T시각) 예측 WFA")
    ap.add_argument("--db", type=Path, default=W.default_db())
    ap.add_argument("--test", nargs=2, required=True)
    ap.add_argument("--train", type=int, default=60)
    ap.add_argument("--at", nargs="+", default=["09:05", "09:30", "10:00"])
    ap.add_argument("--show", nargs="*", default=["2026-09-04"])
    ap.add_argument("--models", nargs="+", default=list(MODELS), help="돌릴 모델만 (예: P0 P1)")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    if not a.force:
        W.guard_intraday()
    sys.stdout.reconfigure(errors="replace")
    W.DAYS = W.load_days(a.db); W.DS = list(W.DAYS)
    rows = E.build_table()
    test = [r for r in rows if a.test[0] <= r["d"] <= a.test[1] and r["ok"] and r["i"] >= a.train + 16]
    print(f"검증 {test[0]['d']}~{test[-1]['d']} {len(test)}세션 · 훈련창 {a.train}세션")

    models = [n for n in MODELS if n in a.models]
    # 1단계(08:45) 기준선 — M1 (오차·80% 구간 폭·커버리지)
    base = {"eh": [], "el": [], "w80": [], "cov80": 0, "pe": []}
    width_rows = []   # (T, 모델, M1폭, 모델폭) — 날짜별 폭 비교
    eff = {}          # (T, 모델) → {"pe": [검증 |오차|%], "cov80": 커버 수}
    obs = []          # 관측 1건(세션×측)마다 dict — 변동성 분위별 분해용
    for at in a.at:
        feats = load_feats_at(a.db, at)
        st = {n: dict(eh=[], el=[], hit5=0, cov50=0, cov80=0, w80=[], n=0, pe=[]) for n in models}
        shown = []
        for r in test:
            i = r["i"]
            train = [x for x in rows[max(16, i - a.train - 10):i] if x["ok"]][-a.train:]
            m = fit_stage2(train, at, feats)
            p = path_at(r, at)
            if m is None or p is None:
                continue
            mfit = E.fit_models(train); m1 = mfit["M1"]
            pct = 100 / r["o"]; atr = r["atr"]; o = r["o"]
            b1 = E.residual_bands("M1", mfit, train)
            m1_w = ((b1["u80"][1] - b1["u80"][0]) * atr * pct, (b1["d80"][1] - b1["d80"][0]) * atr * pct)
            if at == a.at[0]:
                eh1, el1 = abs(o + m1[0] * atr - r["h"]), abs(o - m1[1] * atr - r["l"])
                base["eh"].append(eh1); base["el"].append(el1); base["pe"] += [eh1 * pct, el1 * pct]
                base["w80"] += list(m1_w)
                c80h = o + (m1[0] + b1["u80"][0]) * atr <= r["h"] <= o + (m1[0] + b1["u80"][1]) * atr
                c80l = o - (m1[1] + b1["d80"][1]) * atr <= r["l"] <= o - (m1[1] + b1["d80"][0]) * atr
                c50h = o + (m1[0] + b1["u50"][0]) * atr <= r["h"] <= o + (m1[0] + b1["u50"][1]) * atr
                c50l = o - (m1[1] + b1["d50"][1]) * atr <= r["l"] <= o - (m1[1] + b1["d50"][0]) * atr
                base["cov80"] += c80h + c80l
                vol = atr / o
                obs.append(dict(at="08:45", model="M1", vol=vol, pe=eh1 * pct, e_pt=eh1, w80=m1_w[0], c50=c50h, c80=c80h))
                obs.append(dict(at="08:45", model="M1", vol=vol, pe=el1 * pct, e_pt=el1, w80=m1_w[1], c50=c50l, c80=c80l))
            f = feats.get(r["d"])
            detail = []
            for n in models:
                pu, pd = predict_stage2(n, m, m1, r, p, f)
                pu, pd = max(pu, p[0]), max(pd, p[1])          # 경로 하한
                H, L = o + pu * atr, o - pd * atr
                s = st[n]; s["n"] += 1
                s["eh"].append(abs(H - r["h"])); s["el"].append(abs(L - r["l"]))
                s["hit5"] += (abs(H - r["h"]) * pct <= 0.5) + (abs(L - r["l"]) * pct <= 0.5)
                b = bands(n, m, m1, feats, at)
                hu50 = (o + (pu + b["u50"][0]) * atr, o + (pu + b["u50"][1]) * atr)
                hu80 = (o + (pu + b["u80"][0]) * atr, o + (pu + b["u80"][1]) * atr)
                ld50 = (o - (pd + b["d50"][1]) * atr, o - (pd + b["d50"][0]) * atr)
                ld80 = (o - (pd + b["d80"][1]) * atr, o - (pd + b["d80"][0]) * atr)
                s["cov50"] += (hu50[0] <= r["h"] <= hu50[1]) + (ld50[0] <= r["l"] <= ld50[1])
                s["cov80"] += (hu80[0] <= r["h"] <= hu80[1]) + (ld80[0] <= r["l"] <= ld80[1])
                s["w80"] += [(hu80[1] - hu80[0]) * pct, (ld80[1] - ld80[0]) * pct]
                s["pe"] += [abs(H - r["h"]) * pct, abs(L - r["l"]) * pct]
                ef = eff.setdefault((at, n), {"pe": [], "cov80": 0})
                ef["pe"] += [abs(H - r["h"]) * pct, abs(L - r["l"]) * pct]
                ef["cov80"] += (hu80[0] <= r["h"] <= hu80[1]) + (ld80[0] <= r["l"] <= ld80[1])
                width_rows.append((at, n, m1_w[0], (hu80[1] - hu80[0]) * pct)); width_rows.append((at, n, m1_w[1], (ld80[1] - ld80[0]) * pct))
                obs.append(dict(at=at, model=n, vol=atr / o, pe=abs(H - r["h"]) * pct, e_pt=abs(H - r["h"]), w80=(hu80[1] - hu80[0]) * pct,
                                c50=hu50[0] <= r["h"] <= hu50[1], c80=hu80[0] <= r["h"] <= hu80[1]))
                obs.append(dict(at=at, model=n, vol=atr / o, pe=abs(L - r["l"]) * pct, e_pt=abs(L - r["l"]), w80=(ld80[1] - ld80[0]) * pct,
                                c50=ld50[0] <= r["l"] <= ld50[1], c80=ld80[0] <= r["l"] <= ld80[1]))
                detail.append((n, H, hu50, hu80, L, ld50, ld80))
            if r["d"] in a.show:
                shown.append((r, p, f, detail))

        print(f"\n# T = {at}  (피처 있는 검증일 {sum(1 for r in test if feats.get(r['d']) is not None)}/{len(test)})")
        print(f"{'모델':4} {'n':>4} {'MAE pt':>7} {'MAE %':>6} {'MAE/ATR':>8} {'±0.5%':>6} {'50%cov':>7} {'80%cov':>7} {'80%폭':>6}")
        if base["eh"]:
            e = base["eh"] + base["el"]
            print(f"{'M1':4} {len(e):4d} {statistics.mean(e):7.1f} {statistics.mean(x*100/r['o'] for x, r in zip(base['eh'], test)):5.2f}% {'':>8} {'':>6}   (08:45 1단계 기준선)")
        for n in models:
            s = st[n]
            if not s["n"]:
                continue
            e = s["eh"] + s["el"]; k = 2 * s["n"]
            pcts = [x * 100 / r["o"] for x, r in zip(s["eh"], test)] + [x * 100 / r["o"] for x, r in zip(s["el"], test)]
            atrs = [x / r["atr"] for x, r in zip(s["eh"], test)] + [x / r["atr"] for x, r in zip(s["el"], test)]
            print(f"{n:4} {k:4d} {statistics.mean(e):7.1f} {statistics.mean(pcts):5.2f}% {statistics.mean(atrs):8.2f} {s['hit5']/k:6.0%} {s['cov50']/k:7.0%} {s['cov80']/k:7.0%} {statistics.median(s['w80']):5.2f}%")
        for r, p, f, detail in shown:
            print(f"\n  [{r['d']} @ {at}] 시가 {r['o']:.2f} · 지금까지 고 {r['o']+p[0]*r['atr']:.1f} 저 {r['o']-p[1]*r['atr']:.1f} 현재 {r['o']+p[2]*r['atr']:.1f}"
                  f" · 피처 {'있음' if f else '없음'}")
            if f:
                print("   미륵 피처:", {k: round(v, 3) for k, v in zip(FEAT_KEYS, f)})
            print(f"  {'모델':4} {'고점':>8} {'50%':>15} {'80%':>15} {'저점':>8} {'50%':>15} {'80%':>15}")
            for n, H, hu50, hu80, L, ld50, ld80 in detail:
                print(f"  {n:4} {H:8.1f} {'%.0f~%.0f' % hu50:>15} {'%.0f~%.0f' % hu80:>15} {L:8.1f} {'%.0f~%.0f' % ld50:>15} {'%.0f~%.0f' % ld80:>15}")
            print(f"  실제 고 {r['h']:.2f} 저 {r['l']:.2f}")

    # ---- 80% 구간 폭 비교: 1단계(08:45 M1) vs 2단계(T) ----
    q80 = lambda xs: float(np.quantile(xs, 0.8))
    print("\n## 80% 구간이 좁혀지는가 — 1단계(08:45 M1) 대 2단계(T)")
    print("   명목 폭 = 훈련 잔차 10/90 분위로 만든 구간의 폭(시가 대비 %) · 실효 반폭 = 검증 |오차|의 80분위(보정 무관, 대칭 구간이면 이 ×2가 진짜 80% 폭)")
    print(f"{'단계':12} {'모델':4} {'명목폭 중앙':>10} {'커버리지':>8} {'실효 80%반폭':>12} {'실효폭/명목폭':>12}")
    if base["w80"]:
        k = len(base["pe"])
        print(f"{'08:45':12} {'M1':4} {statistics.median(base['w80']):9.2f}% {base['cov80']/k:8.0%} {q80(base['pe']):11.2f}% {2*q80(base['pe'])/statistics.median(base['w80']):12.2f}")
    for at in a.at:
        for n in models:
            ws = [w for t, mm, _, w in width_rows if t == at and mm == n]
            m1w = [w1 for t, mm, w1, _ in width_rows if t == at and mm == n]
            if not ws:
                continue
            pe = eff[(at, n)]["pe"]; cov = eff[(at, n)]["cov80"] / len(pe)
            narrower = sum(1 for w1, w in zip(m1w, ws) if w < w1) / len(ws)
            print(f"{at:12} {n:4} {statistics.median(ws):9.2f}% {cov:8.0%} {q80(pe):11.2f}% {2*q80(pe)/statistics.median(ws):12.2f}"
                  f"   (M1 대비 좁아진 날 {narrower:.0%} · 폭 비율 중앙 {statistics.median(w/w1 for w1, w in zip(m1w, ws)):.2f})")

    # ---- 변동성(ATR/시가) 3분위별 분해 — 조용한 날 / 중간 / 시끄러운 날 ----
    if obs:
        vols = sorted({(o_["vol"]) for o_ in obs})
        c1, c2 = vols[len(vols) // 3], vols[2 * len(vols) // 3]
        tiers = (("조용(하위⅓)", lambda v: v <= c1), ("중간", lambda v: c1 < v <= c2), ("시끄러움(상위⅓)", lambda v: v > c2))
        print(f"\n## 변동성 분위별 커버리지 — ATR14/시가 3분위 (경계 {c1*100:.1f}% / {c2*100:.1f}%)")
        print(f"{'분위':14} {'시각':6} {'모델':4} {'n':>4} {'MAE pt':>7} {'±0.5%':>6} {'명목80%폭':>9} {'50%cov':>7} {'80%cov':>7} {'실효80%반폭':>10} {'실효/명목':>8}")
        keys = [("08:45", "M1")] + [(t, n) for t in a.at for n in models]
        for tier, sel in tiers:
            for t, n in keys:
                rows_ = [o_ for o_ in obs if o_["at"] == t and o_["model"] == n and sel(o_["vol"])]
                if not rows_:
                    continue
                k = len(rows_)
                w = statistics.median(o_["w80"] for o_ in rows_); eff80 = q80([o_["pe"] for o_ in rows_])
                print(f"{tier:14} {t:6} {n:4} {k:4d} {statistics.mean(o_['e_pt'] for o_ in rows_):7.1f} "
                      f"{sum(1 for o_ in rows_ if o_['pe'] <= 0.5)/k:6.0%} {w:8.2f}% {sum(o_['c50'] for o_ in rows_)/k:7.0%} "
                      f"{sum(o_['c80'] for o_ in rows_)/k:7.0%} {eff80:9.2f}% {2*eff80/w:8.2f}")
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
