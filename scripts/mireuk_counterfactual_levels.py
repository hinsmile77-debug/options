"""미륵 실제 진입 이력에 **예측 고점·저점 규칙**을 반사실(counterfactual)로 적용하면 손익이 좋아지는가.

2026-09-06 [MW0601]. 규칙(사용자 지정):
  1. 예측 고점 부근에서는 상방(LONG) 진입 자제, 하방(SHORT) 허용
  2. 예측 저점 부근에서는 하방(SHORT) 진입 자제, 상방(LONG) 허용
「허용」은 현행과 같으므로 반사실은 **자제된 진입을 없앤 것**뿐이다.

## 예측

  08:45 M1 (`premarket_extremes_wfa.py`) : 고점 = 시가 + med(u)·ATR, 저점 = 시가 − med(d)·ATR — 09:30 이전 진입에 적용
  09:30 P1 (`premarket_extremes_stage2.py`): 확정분 5개 + 09:30 경로 회귀            — 09:30 이후 진입에 적용
  둘 다 직전 60세션으로 맞춘 워크포워드 값(그날 정보 없음). 미륵 봉(raw_candles)으로 계산한다.

## 「부근」

  LONG 자제:  진입가 ≥ 예측고점 − tol,   SHORT 자제: 진입가 ≤ 예측저점 + tol.   tol = k·ATR14, k ∈ {0, 0.1, 0.2, 0.3}
  (예측고점 위에서 사는 것도 「부근」이다 — 위쪽은 열어 둔다)

## 대조

  거울 규칙(placebo): LONG을 예측**저점** 부근에서, SHORT를 예측**고점** 부근에서 막는다 — 규칙이 방향 특이적인지.
  무작위 차단: 같은 수의 진입을 무작위로 없앤 500회 — 「진입 수를 줄이면 무조건 손실이 줄어드는」 효과 분리.

## 손익 단위

  `trades.net_pnl_krw`(수수료 차감 원화)와 `pnl_pts × quantity`. 부분청산 행은 같은 진입(entry_ts·방향·진입가)으로 묶는다.

실행: python scripts/mireuk_counterfactual_levels.py --train 60
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import premarket_extremes_stage2 as S
import premarket_extremes_wfa as E
import premarket_levels_wfa as W


def load_entries(db: Path):
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    rows = con.execute("SELECT entry_ts, direction, entry_price, quantity, pnl_pts, net_pnl_krw FROM trades ORDER BY entry_ts").fetchall()
    con.close()
    ent: dict[tuple, dict] = {}
    for ts, d, px, q, pts, krw in rows:
        k = (ts, d, round(float(px), 2))
        e = ent.setdefault(k, dict(ts=ts, day=ts[:10], t=ts[11:16], dir=d, px=float(px), pts=0.0, krw=0.0, rows=0))
        e["pts"] += float(pts or 0) * int(q or 1); e["krw"] += float(krw or 0); e["rows"] += 1
    return list(ent.values())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=W.default_db())
    ap.add_argument("--trades", type=Path, default=W.default_db().parent / "trades.db")
    ap.add_argument("--train", type=int, default=60)
    ap.add_argument("--exclude-preopen", action="store_true", help="08:45 이전 진입(동기화·복구 잔재)을 표본에서 뺀다")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    if not a.force:
        W.guard_intraday()
    sys.stdout.reconfigure(errors="replace")
    W.DAYS = W.load_days(a.db); W.DS = list(W.DAYS)
    rows = E.build_table(); by_day = {r["d"]: r for r in rows}
    entries = load_entries(a.trades)
    days = sorted({e["day"] for e in entries})
    feats = S.load_feats_at(a.db, "09:30")

    # 날짜별 예측 (그날 정보 없이)
    pred = {}
    for d in days:
        r = by_day.get(d)
        if r is None or not r.get("ok") or r["i"] < a.train + 16:
            continue
        i = r["i"]
        train = [x for x in rows[max(16, i - a.train - 10):i] if x["ok"]][-a.train:]
        m1 = E.fit_models(train)["M1"]; o, atr = r["o"], r["atr"]
        H1, L1 = o + m1[0] * atr, o - m1[1] * atr
        m2 = S.fit_stage2(train, "09:30", feats); p = S.path_at(r, "09:30")
        if m2 is not None and p is not None:
            pu, pd = S.predict_stage2("P1", m2, m1, r, p, None)
            H2, L2 = o + max(pu, p[0]) * atr, o - max(pd, p[1]) * atr
        else:
            H2, L2 = H1, L1
        pred[d] = dict(H1=H1, L1=L1, H2=H2, L2=L2, atr=atr, o=o, h=r["h"], l=r["l"])

    covered = [e for e in entries if e["day"] in pred]
    if a.exclude_preopen:
        pre = [e for e in covered if e["t"] < "08:45"]
        print(f"08:45 이전 진입 {len(pre)}건(손익 {sum(e['krw'] for e in pre):+,.0f}원) 제외 — 장전 예측이 성립하지 않는 시각")
        covered = [e for e in covered if e["t"] >= "08:45"]
    print(f"진입 {len(entries)}건 / {len(days)}일 · 예측 가능한 날의 진입 {len(covered)}건 / {len(pred)}일 "
          f"(제외: 만기 롤 오염·훈련창 부족·봉 결손일)")
    base_krw = sum(e["krw"] for e in covered); base_pts = sum(e["pts"] for e in covered)
    wins = sum(1 for e in covered if e["krw"] > 0)
    print(f"기준(현행): 순손익 {base_krw:,.0f}원 · {base_pts:+.1f}pt · 승률 {wins/len(covered):.0%} · 진입당 {base_krw/len(covered):,.0f}원")

    def blocked_set(k, mirror=False):
        out = []
        for e in covered:
            p = pred[e["day"]]; tol = k * p["atr"]
            H, L = (p["H1"], p["L1"]) if e["t"] < "09:30" else (p["H2"], p["L2"])
            if not mirror:
                b = (e["dir"] == "LONG" and e["px"] >= H - tol) or (e["dir"] == "SHORT" and e["px"] <= L + tol)
            else:
                b = (e["dir"] == "LONG" and e["px"] <= L + tol) or (e["dir"] == "SHORT" and e["px"] >= H - tol)
            if b:
                out.append(e)
        return out

    def summarize(label, blocked):
        n = len(blocked)
        if n == 0:
            print(f"{label:24} 차단 0건"); return
        bk = sum(e["krw"] for e in blocked); bp = sum(e["pts"] for e in blocked)
        rest = base_krw - bk
        bw = sum(1 for e in blocked if e["krw"] > 0) / n
        # 무작위 차단 대조: 같은 수를 무작위로 없앨 때 제거되는 손익의 분포
        random.seed(3); rnd = []
        for _ in range(500):
            s = random.sample(covered, n); rnd.append(sum(e["krw"] for e in s))
        rnd.sort(); med = rnd[250]; lo, hi = rnd[25], rnd[475]
        better = sum(1 for x in rnd if x >= bk) / len(rnd)
        nl = sum(1 for e in blocked if e["dir"] == "LONG")
        print(f"{label:24} 차단 {n:3d}건(L{nl}/S{n-nl}) · 차단분 손익 {bk:+,.0f}원 {bp:+.1f}pt · 차단분 승률 {bw:.0%} "
              f"· 반사실 순손익 {rest:,.0f}원 ({rest-base_krw:+,.0f}) · 무작위 {n}건 제거 중앙 {med:+,.0f} [{lo:+,.0f}~{hi:+,.0f}] "
              f"· p(무작위≥차단분)={better:.2f}")

    print("\n## 규칙 적용 (08:45 M1 → 09:30 이전 진입, 09:30 P1 → 이후 진입)")
    print("   p(무작위≥차단분): 차단한 진입들의 손익이 무작위로 같은 수를 뺀 것보다 얼마나 나쁜가 — 작을수록 규칙이 '나쁜 진입'을 골라낸 것")
    for k in (0.0, 0.1, 0.2, 0.3):
        summarize(f"tol={k:.1f}·ATR", blocked_set(k))
    print("\n## 거울 규칙 (placebo — LONG을 예측저점 부근에서, SHORT를 예측고점 부근에서 차단)")
    for k in (0.0, 0.1, 0.2, 0.3):
        summarize(f"거울 tol={k:.1f}·ATR", blocked_set(k, mirror=True))

    # 시각별 분해 — 08:45 예측이 적용된 진입과 09:30 예측이 적용된 진입
    print("\n## tol=0.2·ATR 차단분의 시각·방향 분해")
    b = blocked_set(0.2)
    for label, sel in (("09:30 이전(M1 적용)", [e for e in b if e["t"] < "09:30"]), ("09:30 이후(P1 적용)", [e for e in b if e["t"] >= "09:30"])):
        if sel:
            print(f"  {label:18} {len(sel):3d}건 · 손익 {sum(e['krw'] for e in sel):+,.0f}원 · 승률 {sum(1 for e in sel if e['krw']>0)/len(sel):.0%}")
    # 예측 고/저 부근에서 실제로 되돌렸는가(진입 후 결과가 아니라 그날 극값 대비)
    hits = sum(1 for d, p in pred.items() if abs(p["h"] - p["H2"]) <= 0.15 * p["atr"] or abs(p["l"] - p["L2"]) <= 0.15 * p["atr"])
    print(f"\n참고: 예측 가능한 {len(pred)}일 중 09:30 예측 고점 또는 저점이 실제 극값 ±0.15ATR 안이었던 날 {hits}일")
    return 0


if __name__ == "__main__":
    sys.exit(main())
