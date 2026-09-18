"""당일 **실현 변동성 분위**(조용/중간/시끄러움)를 08:45·09:30·10:00에 예측하는 모델의 WFA.

2026-09-06 [MW0601] — `RESEARCH_PREMARKET_EXTREMES_v1.md` §7.8의 후속. 그 절은 「조용한 날은 09:30
구간이 못 좁혀지고 과신한다」를 보였다. 그렇다면 **오늘이 어느 분위인지 미리 알 수 있는가**가 다음
질문이다. 지금까지의 분위는 ATR14/시가(08:45에 이미 아는 값)였는데, 그것은 「최근 2주가 조용했다」지
「오늘이 조용하다」가 아니다.

## 정의

  R = (당일 고가 − 저가) / 시가.  분위 경계 = 훈련창 60세션의 R 33/67 분위(예측 시점에 안다).
  목표: 오늘의 R 분위(0 조용 / 1 중간 / 2 시끄러움).

## 입력 (시각별로 누적)

  08:45 확정분 : log(ATR14/시가), log(ATR5/ATR14), log(전일 범위/ATR14), 갭/ATR, |갭|/ATR, 전일 종가 위치
  09:30 경로   : + log(그때까지 범위/ATR14), |그때까지 수익|/ATR
  09:30 피처   : + 미륵 10개(나스닥·VIX·외국인·기관·개인·프로그램·CVD·OFI·허스트·POC거리) 표준화
  10:00        : 같은 구성, 10:00 경로·피처

## 모델

  B0  항상 「중간」                                   (무정보 기준선)
  B1  ATR14/시가 분위 그대로                          (지금까지 쓰던 프록시)
  B2  전일 범위 분위 그대로
  V1  확정분 LAD 회귀 → log R 예측 → 훈련 분위로 분류
  V2  V1 + 경로 (LAD)
  V3  V2 + 미륵 피처 (릿지 λ=3)
  V4  경로 하나만: log R̂ = a + b·log(그때까지 범위/ATR)   (가장 단순한 2단계)

## 평가

  3분류 정확도 · 「조용 vs 아님」 2분류 정확도 · 시끄러움 재현율 · 예측 R과 실제 R의 순위상관.
  그리고 **쓸모 검정**: 예측 분위별로 훈련 잔차를 따로 잡아 80% 구간을 만들면 커버리지가
  §7.8의 과신(조용 64%)을 고치는가 — 08:45 M1과 09:30 P1에 대해.

실행: python scripts/premarket_volclass_wfa.py --test 2026-01-05 2026-09-04 --train 60
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import premarket_extremes_stage2 as S
import premarket_extremes_wfa as E
import premarket_levels_wfa as W

RIDGE = 3.0


def atr_n(i: int, n: int) -> float:
    trs, pc = [], None
    for d in W.DS[max(0, i - n):i]:
        h, l = W.day_hl(d)
        trs.append(h - l if pc is None else max(h - l, abs(h - pc), abs(l - pc)))
        pc = W.DAYS[d][-1][4]
    return sum(trs) / len(trs)


def x_fixed(r):
    return [1.0, math.log(r["atr"] / r["o"]), math.log(atr_n(r["i"], 5) / r["atr"]), math.log(max(r["r1"], 1e-3)),
            r["gap"], abs(r["gap"]), r["p1"]]


def x_path(r, at):
    p = S.path_at(r, at)
    if p is None:
        return None
    rng = max(p[0] + p[1], 1e-3)
    return [math.log(rng), abs(p[2])]


def spearman(a, b):
    ra = np.argsort(np.argsort(a)); rb = np.argsort(np.argsort(b))
    return float(np.corrcoef(ra, rb)[0, 1])


def classify(v, cuts):
    return 0 if v <= cuts[0] else (1 if v <= cuts[1] else 2)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="당일 변동성 분위 예측 WFA")
    ap.add_argument("--db", type=Path, default=W.default_db())
    ap.add_argument("--test", nargs=2, required=True)
    ap.add_argument("--train", type=int, default=60)
    ap.add_argument("--at", nargs="+", default=["09:30", "10:00"])
    ap.add_argument("--scale-floor", type=float, default=0.0, help="R̂ 스케일 하한(예 0.85). 0이면 하한 없음")
    ap.add_argument("--scale-cap", type=float, default=99.0, help="R̂ 스케일 상한")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    if not a.force:
        W.guard_intraday()
    sys.stdout.reconfigure(errors="replace")
    W.DAYS = W.load_days(a.db); W.DS = list(W.DAYS)
    rows = E.build_table()
    for r in rows:
        if r.get("ok"):
            r["R"] = (r["h"] - r["l"]) / r["o"]
    test = [r for r in rows if a.test[0] <= r["d"] <= a.test[1] and r["ok"] and r["i"] >= a.train + 16]
    feats = {at: S.load_feats_at(a.db, at) for at in a.at}
    print(f"검증 {test[0]['d']}~{test[-1]['d']} {len(test)}세션 · 훈련창 {a.train}세션")

    # 시각별 모델 목록
    plan = [("08:45", "B0"), ("08:45", "B1"), ("08:45", "B2"), ("08:45", "V1")]
    for at in a.at:
        plan += [(at, "V4"), (at, "V2"), (at, "V3")]
    res = {k: dict(pred=[], cls=[], true=[], trueR=[], predR=[]) for k in plan}
    band = {k: dict(cov80=0, n=0, by=[0, 0, 0], byn=[0, 0, 0], bya=[0, 0, 0], byan=[0, 0, 0], w=[]) for k in
            (("08:45", "M1"), ("08:45", "M1+분위"), ("08:45", "M1×R̂"), ("09:30", "P1"), ("09:30", "P1+분위"), ("09:30", "P1×R̂"))}
    atr_all = sorted(x["atr"] / x["o"] for x in test); ATR_CUT = (atr_all[len(atr_all) // 3], atr_all[2 * len(atr_all) // 3])

    for r in test:
        i = r["i"]
        train = [x for x in rows[max(16, i - a.train - 10):i] if x["ok"]][-a.train:]
        Rs = sorted(x["R"] for x in train)
        cuts = (Rs[len(Rs) // 3], Rs[2 * len(Rs) // 3])
        true_c = classify(r["R"], cuts)
        y = np.array([math.log(x["R"]) for x in train])

        def record(key, predR):
            d = res[key]; d["pred"].append(predR); d["cls"].append(classify(math.exp(predR), cuts))
            d["true"].append(true_c); d["trueR"].append(r["R"]); d["predR"].append(predR)

        # 기준선
        record(("08:45", "B0"), math.log(statistics.median(Rs)))
        atr_cuts = sorted(x["atr"] / x["o"] for x in train); ac = (atr_cuts[len(atr_cuts) // 3], atr_cuts[2 * len(atr_cuts) // 3])
        cls_b1 = classify(r["atr"] / r["o"], ac); record(("08:45", "B1"), math.log(Rs[[len(Rs) // 6, len(Rs) // 2, 5 * len(Rs) // 6][cls_b1]]))
        pr_cuts = sorted(x["r1"] * x["atr"] / x["o"] for x in train); pc_ = (pr_cuts[len(pr_cuts) // 3], pr_cuts[2 * len(pr_cuts) // 3])
        cls_b2 = classify(r["r1"] * r["atr"] / r["o"], pc_); record(("08:45", "B2"), math.log(Rs[[len(Rs) // 6, len(Rs) // 2, 5 * len(Rs) // 6][cls_b2]]))
        # V1 확정분
        X1 = np.array([x_fixed(x) for x in train]); b1 = E.lad_fit(X1, y)
        v1 = float(np.array(x_fixed(r)) @ b1); record(("08:45", "V1"), v1)
        pred_cls_0845 = classify(math.exp(v1), cuts)

        pred_cls_at = {}; pred_v2 = {}
        for at in a.at:
            xp = x_path(r, at)
            if xp is None:
                continue
            tr = [(x, x_path(x, at), feats[at].get(x["d"])) for x in train]
            tr = [(x, p, f) for x, p, f in tr if p is not None]
            yt = np.array([math.log(x["R"]) for x, _, _ in tr])
            X4 = np.array([[1.0, p[0]] for _, p, _ in tr]); b4 = E.lad_fit(X4, yt)
            record((at, "V4"), float(np.array([1.0, xp[0]]) @ b4))
            X2 = np.array([x_fixed(x) + p for x, p, _ in tr]); b2 = E.lad_fit(X2, yt)
            v2 = float(np.array(x_fixed(r) + xp) @ b2); record((at, "V2"), v2)
            pred_cls_at[at] = classify(math.exp(v2), cuts); pred_v2[at] = v2
            wf = [(x, p, f) for x, p, f in tr if f is not None]
            f_now = feats[at].get(r["d"])
            if len(wf) >= 30 and f_now is not None:
                F = np.array([f for _, _, f in wf]); mu, sd = F.mean(0), F.std(0) + 1e-9
                X3 = np.array([x_fixed(x) + p + list((np.array(f) - mu) / sd) for x, p, f in wf])
                y3 = np.array([math.log(x["R"]) for x, _, _ in wf])
                b3 = S.ridge_fit(X3, y3, RIDGE)
                record((at, "V3"), float(np.array(x_fixed(r) + xp + list((np.array(f_now) - mu) / sd)) @ b3))
            else:
                record((at, "V3"), v2)

        # ---- 쓸모 검정: 예측 분위별 잔차로 80% 구간 ----
        mfit = E.fit_models(train); m1 = mfit["M1"]; o, atr = r["o"], r["atr"]
        atr_c = classify(r["atr"] / r["o"], ATR_CUT)
        medR = statistics.median(Rs)
        def cover_m1(sub, scale=1.0):
            ru = [x["u"] - m1[0] for x in sub]; rd = [x["dn"] - m1[1] for x in sub]
            q = lambda v, k: float(np.quantile(v, k))
            lo_u, hi_u = q(ru, .1) * scale, q(ru, .9) * scale; lo_d, hi_d = q(rd, .1) * scale, q(rd, .9) * scale
            ch = o + (m1[0] + lo_u) * atr <= r["h"] <= o + (m1[0] + hi_u) * atr
            cl = o - (m1[1] + hi_d) * atr <= r["l"] <= o - (m1[1] + lo_d) * atr
            return ch + cl, ((hi_u - lo_u) + (hi_d - lo_d)) / 2 * atr / o * 100
        def tally(key, c, w):
            b = band[key]; b["cov80"] += c; b["n"] += 2; b["by"][true_c] += c; b["byn"][true_c] += 2; b["bya"][atr_c] += c; b["byan"][atr_c] += 2; b["w"].append(w)
        c, w = cover_m1(train); tally(("08:45", "M1"), c, w)
        c, w = cover_m1([x for x in train if classify(x["R"], cuts) == pred_cls_0845] or train); tally(("08:45", "M1+분위"), c, w)
        sc1 = min(a.scale_cap, max(a.scale_floor, math.exp(v1) / medR))
        c, w = cover_m1(train, scale=sc1); tally(("08:45", "M1×R̂"), c, w)
        if "09:30" in pred_cls_at:
            m2 = S.fit_stage2(train, "09:30", feats["09:30"]); p = S.path_at(r, "09:30")
            if m2 is not None and p is not None:
                bu, bd = m2["P1"]
                def cover_p1(sub_rows, scale=1.0):
                    ru, rd = [], []
                    for x, px, _ in sub_rows:
                        pu, pd = float(S.x_path(x, px) @ bu), float(S.x_path(x, px) @ bd)
                        ru.append(x["u"] - max(pu, px[0])); rd.append(x["dn"] - max(pd, px[1]))
                    q = lambda v, k: float(np.quantile(v, k))
                    lo_u, hi_u = q(ru, .1) * scale, q(ru, .9) * scale; lo_d, hi_d = q(rd, .1) * scale, q(rd, .9) * scale
                    pu, pd = max(float(S.x_path(r, p) @ bu), p[0]), max(float(S.x_path(r, p) @ bd), p[1])
                    ch = o + (pu + lo_u) * atr <= r["h"] <= o + (pu + hi_u) * atr
                    cl = o - (pd + hi_d) * atr <= r["l"] <= o - (pd + lo_d) * atr
                    return ch + cl, ((hi_u - lo_u) + (hi_d - lo_d)) / 2 * atr / o * 100
                allr = m2["train_rows"]
                subr = [t for t in allr if classify(t[0]["R"], cuts) == pred_cls_at["09:30"]] or allr
                c, w = cover_p1(allr); tally(("09:30", "P1"), c, w)
                c, w = cover_p1(subr); tally(("09:30", "P1+분위"), c, w)
                sc2 = min(a.scale_cap, max(a.scale_floor, math.exp(pred_v2["09:30"]) / medR))
                c, w = cover_p1(allr, scale=sc2); tally(("09:30", "P1×R̂"), c, w)

    print(f"\n## 분위 예측 정확도 (3분류 무정보 = 33%, 2분류 무정보 = 67%)")
    print(f"{'시각':6} {'모델':4} {'n':>4} {'3분류 정확':>8} {'조용vs아님':>9} {'조용 재현':>8} {'시끄 재현':>8} {'순위상관':>7} {'혼동(행=실제 조/중/시, 열=예측)':>32}")
    for key in plan:
        d = res[key]
        if not d["true"]:
            continue
        n = len(d["true"]); T = np.array(d["true"]); P = np.array(d["cls"])
        acc3 = float((T == P).mean()); acc2 = float(((T == 0) == (P == 0)).mean())
        rq = float((P[T == 0] == 0).mean()) if (T == 0).any() else 0; rn = float((P[T == 2] == 2).mean()) if (T == 2).any() else 0
        rho = spearman(np.array(d["predR"]), np.array(d["trueR"]))
        conf = [[int(((T == t) & (P == p)).sum()) for p in range(3)] for t in range(3)]
        print(f"{key[0]:6} {key[1]:4} {n:4d} {acc3:8.0%} {acc2:9.0%} {rq:8.0%} {rn:8.0%} {rho:7.2f}   {conf}")

    print("\n## 쓸모 검정 — 80% 구간 커버리지: 기본 / 예측 분위 부분집합(+분위) / 예측 R̂ 스케일링(×R̂)")
    print(f"{'시각':6} {'구간':9} {'전체':>5} {'폭중앙':>6} │ {'ATR14분위(사전) 조/중/시':>26} │ {'실현분위(사후) 조/중/시':>24}")
    for key, b in band.items():
        if not b["n"]:
            continue
        bya = " / ".join(f"{b['bya'][k]/b['byan'][k]:.0%}" if b["byan"][k] else "-" for k in range(3))
        by = " / ".join(f"{b['by'][k]/b['byn'][k]:.0%}" if b["byn"][k] else "-" for k in range(3))
        print(f"{key[0]:6} {key[1]:9} {b['cov80']/b['n']:5.0%} {statistics.median(b['w']):5.2f}% │ {bya:>26} │ {by:>24}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
