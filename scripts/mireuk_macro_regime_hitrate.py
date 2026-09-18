"""미륵의 **장전 매크로 레짐(RISK_ON / NEUTRAL / RISK_OFF)** 이 그날 시장을 맞혔는지 데이터로 센다.

2026-09-06 [MW0601]. 근거: `docs/Dev_md/RESEARCH_MIREUK_MACRO_REGIME_HITRATE_v1.md`.

## 미륵 쪽 정의 (collection/macro/regime_classifier.py)

  점수 = VIX(<15 +2, <20 +1, >25 −1, >30 −2) + SP500(>+0.5% +1, <−0.5% −1) + USD/KRW(>+0.5% −1, <−0.5% +1)
  점수 ≥ +2 → RISK_ON, ≤ −2 → RISK_OFF, 나머지 NEUTRAL.  08:58 장전 1회(main.py `_pre_market_stage2`).
  나스닥·US10Y는 인자로 받지만 점수에 안 들어간다.

## 두 원천

  A. 로그 `[Regime]` 줄 — 실제로 판정된 값(logs/*.log). 이것이 「미륵이 예측한 레짐」이다.
  B. `raw_features` 09:00 첫 행의 정규화 매크로값을 역변환해 같은 규칙으로 재구성. 로그가 없는 날까지
     늘리지만 **백필 행은 매크로가 전일값에 얼어 있다**(라이브 날만 신뢰). 로그와 20/21 일치.

## 「적중」의 정의

  방향: RISK_ON이면 시가→종가 상승, RISK_OFF면 하락(NEUTRAL 제외). 전일종가→종가도 같이 낸다 —
        갭이 이미 야간 정보를 반영하므로 **시가→종가**가 장중 매매에 의미 있는 쪽이다.
  대조: 기저율(그 기간 상승일 비율), 나스닥 야간 부호 그대로.
  변동성: 범위/ATR, |시가→종가| > 1% 비율 — RISK_OFF가 「위험」을 뜻한다면 여기서 갈려야 한다.

실행: python scripts/mireuk_macro_regime_hitrate.py   (미륵 폴더는 형제 경로 ../futures, --root로 변경)
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import math
import re
import sqlite3
import statistics
import sys
from datetime import datetime, time as dtime
from pathlib import Path

PAT = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}).*\[Regime\] (RISK_ON|NEUTRAL|RISK_OFF) \(점수=(-?\d+)\) \| VIX=([\d.]+).*SP500=([+-][\d.]+)%.*USD/KRW ([+-]?[\d.]+)%")
VIX_BASE, VIX_FEAR = 15.0, 40.0


def classify(vix, sp, krw):
    s = (2 if vix < 15 else 1 if vix < 20 else -2 if vix > 30 else -1 if vix > 25 else 0)
    s += 1 if sp > 0.5 else -1 if sp < -0.5 else 0
    s += -1 if krw > 0.5 else 1 if krw < -0.5 else 0
    return ("RISK_ON" if s >= 2 else "RISK_OFF" if s <= -2 else "NEUTRAL"), s


def wilson(k, n, z=1.96):
    if n == 0:
        return 0.0, 0.0
    p = k / n; den = 1 + z * z / n
    ctr = (p + z * z / (2 * n)) / den; half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return ctr - half, ctr + half


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2] / "futures")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    now = datetime.now()
    if not a.force and now.weekday() < 5 and dtime(8, 40) <= now.time() <= dtime(15, 40):
        print("장중에는 미륵 라이브 DB를 읽지 않는다"); return 2
    sys.stdout.reconfigure(errors="replace")

    # A. 로그
    logged = {}
    for f in sorted(glob.glob(str(a.root / "logs" / "*.log"))):
        for line in open(f, encoding="utf-8", errors="replace"):
            m = PAT.match(line)
            if m and m.group(1) not in logged:
                logged[m.group(1)] = dict(regime=m.group(3), score=int(m.group(4)), vix=float(m.group(5)), sp=float(m.group(6)), krw=float(m.group(7)))

    # B. 재구성 + 일봉
    clip = float(re.search(r"^_CHG_CLIP\s*=\s*([\d.]+)", (a.root / "features/macro/macro_feature_transformer.py").read_text(encoding="utf-8"), re.M).group(1))
    con = sqlite3.connect(f"file:{(a.root / 'data/db/raw_data.db').as_posix()}?mode=ro", uri=True)
    recon = {}
    for ts, f in con.execute("SELECT ts, features FROM raw_features WHERE substr(ts,12,5) BETWEEN '09:00' AND '09:05' ORDER BY ts"):
        d = ts[:10]
        if d in recon:
            continue
        j = json.loads(f)
        if j.get("macro_vix") is None:
            continue
        vix = VIX_BASE + (VIX_FEAR - VIX_BASE) * j["macro_vix"]; sp = j["macro_sp500_chg"] * clip * 100; krw = j["macro_krw_chg"] * clip * 100
        reg, s = classify(vix, sp, krw)
        recon[d] = dict(regime=reg, score=s, vix=vix, sp=sp, krw=krw, live=bool(j.get("quality_macro_available")), nasdaq=j["macro_nasdaq_chg"] * clip * 100)
    days = collections.OrderedDict()
    for ts, o, h, l, c in con.execute("SELECT ts, open, high, low, close FROM raw_candles ORDER BY ts"):
        days.setdefault(ts[:10], []).append((float(o), float(h), float(l), float(c)))
    con.close()
    ds = list(days); daily = {}
    for i, d in enumerate(ds):
        b = days[d]
        if len(b) < 300 or i == 0:
            continue
        o, c = b[0][0], b[-1][3]; h = max(x[1] for x in b); l = min(x[2] for x in b); pc = days[ds[i - 1]][-1][3]
        trs, p = [], None
        for dd in ds[max(0, i - 14):i]:
            bb = days[dd]; hh = max(x[1] for x in bb); ll = min(x[2] for x in bb)
            trs.append(hh - ll if p is None else max(hh - ll, abs(hh - p), abs(ll - p))); p = bb[-1][3]
        daily[d] = dict(oc=(c - o) / o * 100, cc=(c - pc) / pc * 100, gap=(o - pc) / pc * 100, rng_atr=(h - l) / (sum(trs) / len(trs)),
                        R=(h - l) / o * 100)
    # 실현 변동성 분위 — 직전 60세션 범위%의 33/67 분위(예측 시점에 아는 경계)
    for i, d in enumerate(ds):
        if d in daily and i >= 60:
            hist = sorted(daily[x]["R"] for x in ds[i - 60:i] if x in daily)
            if len(hist) >= 30:
                c = (hist[len(hist) // 3], hist[2 * len(hist) // 3])
                daily[d]["vc"] = 0 if daily[d]["R"] <= c[0] else 1 if daily[d]["R"] <= c[1] else 2
    for d, v in logged.items():
        if d in recon:
            v["nasdaq"] = recon[d]["nasdaq"]
    agree = sum(1 for d in logged if d in recon and recon[d]["regime"] == logged[d]["regime"])
    print(f"로그 판정 {len(logged)}일 · 재구성 {len(recon)}일(라이브 {sum(v['live'] for v in recon.values())}) · 로그 vs 재구성 일치 {agree}/{sum(1 for d in logged if d in recon)}")
    ds2 = sorted(recon)
    frozen = sum(1 for i in range(1, len(ds2)) if (recon[ds2[i]]["vix"], recon[ds2[i]]["sp"]) == (recon[ds2[i - 1]]["vix"], recon[ds2[i - 1]]["sp"]))
    print(f"신선도: 09:00 (VIX, SP500)이 전일과 같은 날 {frozen}/{len(ds2) - 1} — 백필 구간은 매크로가 얼어 있다")

    def evaluate(title, src):
        src = {d: v for d, v in src.items() if d in daily}
        print(f"\n## {title} (n={len(src)})")
        print(f"{'레짐':9} {'n':>3} {'시→종 상승':>9} {'95%CI':>9} {'전일→종 상승':>10} {'평균 시→종%':>10} {'평균 갭%':>8} {'범위/ATR':>8} {'|시→종|>1%':>9}")
        for reg in ("RISK_ON", "NEUTRAL", "RISK_OFF", "ALL"):
            sel = [d for d, v in src.items() if reg == "ALL" or v["regime"] == reg]
            if not sel:
                continue
            n = len(sel); up = sum(1 for d in sel if daily[d]["oc"] > 0); up2 = sum(1 for d in sel if daily[d]["cc"] > 0); lo, hi = wilson(up, n)
            print(f"{reg:9} {n:3d} {up/n:9.0%} {'%.0f~%.0f%%' % (lo*100, hi*100):>9} {up2/n:10.0%} {statistics.mean(daily[d]['oc'] for d in sel):+10.2f} "
                  f"{statistics.mean(daily[d]['gap'] for d in sel):+8.2f} {statistics.mean(daily[d]['rng_atr'] for d in sel):8.2f} {sum(1 for d in sel if abs(daily[d]['oc']) > 1)/n:9.0%}")
        dirsel = [(d, v) for d, v in src.items() if v["regime"] != "NEUTRAL"]
        if dirsel:
            k = sum(1 for d, v in dirsel if (daily[d]["oc"] > 0) == (v["regime"] == "RISK_ON")); k2 = sum(1 for d, v in dirsel if (daily[d]["cc"] > 0) == (v["regime"] == "RISK_ON"))
            lo, hi = wilson(k, len(dirsel))
            print(f"  방향 적중(ON→상승/OFF→하락): 시가→종가 {k}/{len(dirsel)}={k/len(dirsel):.0%} (95%CI {lo:.0%}~{hi:.0%}) · 전일→종가 {k2}/{len(dirsel)}={k2/len(dirsel):.0%}")
        ns = [(d, v) for d, v in src.items() if abs(v.get("nasdaq", 0)) > 0.05]
        if ns:
            k = sum(1 for d, v in ns if (daily[d]["oc"] > 0) == (v["nasdaq"] > 0))
            print(f"  대조 — 나스닥 야간 부호 그대로: 시가→종가 {k}/{len(ns)}={k/len(ns):.0%}")
        print(f"  기저율: 시가→종가 상승 {sum(1 for d in src if daily[d]['oc'] > 0)/len(src):.0%} · 전일→종가 상승 {sum(1 for d in src if daily[d]['cc'] > 0)/len(src):.0%}")
        # 실현 변동성 분위 교차표
        vs = {d: v for d, v in src.items() if "vc" in daily[d]}
        if vs:
            print(f"  [실현 변동성 분위]  {'레짐':9} {'n':>3} {'조용':>5} {'중간':>5} {'시끄러움':>7} {'평균 범위%':>9}")
            for reg in ("RISK_ON", "NEUTRAL", "RISK_OFF", "기저율"):
                sel = [d for d, v in vs.items() if reg == "기저율" or v["regime"] == reg]
                if not sel:
                    print(f"                     {reg:9}   0   (판정된 날 없음)"); continue
                cnt = collections.Counter(daily[d]["vc"] for d in sel); n = len(sel)
                print(f"                     {reg:9} {n:3d} {cnt[0]/n:5.0%} {cnt[1]/n:5.0%} {cnt[2]/n:7.0%} {statistics.mean(daily[d]['R'] for d in sel):9.2f}")
            def rank_corr(a, b):
                import numpy as np
                ra = np.argsort(np.argsort(a)); rb = np.argsort(np.argsort(b)); return float(np.corrcoef(ra, rb)[0, 1])
            dd = list(vs); rr = [daily[d]["R"] for d in dd]
            print(f"                     순위상관(vs 실현 범위): 점수 {rank_corr([vs[d]['score'] for d in dd], rr):+.2f} · VIX {rank_corr([vs[d]['vix'] for d in dd], rr):+.2f} · |SP500| {rank_corr([abs(vs[d]['sp']) for d in dd], rr):+.2f}")

    evaluate("A. 로그에 남은 실제 장전 판정", logged)
    evaluate("B. 재구성 — 매크로가 실제 수집된 라이브 날", {d: v for d, v in recon.items() if v["live"]})
    evaluate("C. 재구성 — 전체(백필 포함, 참고용)", recon)
    return 0


if __name__ == "__main__":
    sys.exit(main())
