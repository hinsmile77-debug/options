"""장전 맥점 산출 규칙의 **워크포워드(WFA) 검증** — 미륵(futures) 1분봉 이력으로.

2026-09-06 [MW0601] — `docs/Dev_md/RESEARCH_PREMARKET_LEVELS_v1.md` §5 프로토콜의 실행체.

## 왜 미륵 DB인가

마흐디 DB의 선물 1분봉은 08/20부터(12세션)라 파라미터를 고르고 남은 날로 검증할 수 없다.
미륵 `raw_data.db::raw_candles`는 2025-08-19부터 256세션(일 384봉, 08:45~15:08)이 있어
**훈련창(과거 N세션)에서 규칙을 고르고 다음 날 하루로 채점**하는 WFA가 성립한다.

미륵 DB는 **읽기 전용**으로만 연다(`mode=ro`). 미륵 CLAUDE.md 규정대로 **장중(08:40~15:40)에는
돌리지 않는다** — 라이브 DB 스캔이 미륵 파이프라인 지연(CB⑤)을 유발한 전례(2026-08-10)가 있다.
기본 경로는 이 저장소의 형제 폴더 `../futures/data/db/raw_data.db`이고 `--db`로 바꾼다(절대경로 금지).

## 정확도의 정의 (사전 등록 — 결과를 보고 바꾸지 않는다)

세션 × 측(상방/하방) 하나가 관측 1건이다. **그날 고가(상방) / 저가(하방)가 예측 레벨 3개 중
하나의 ±TOL 안에 들어오면 적중.** 09/04의 1063.85 ← 1063이 바로 이 사건이다.
TOL = 기준가의 0.15%(현재 1050 수준에서 ≈1.6pt; 문서의 ±1.5pt와 같은 기준을 가격 수준이 다른
과거 훈련창에도 그대로 옮긴 것). `--tol-pt`로 절대값을 강제할 수 있다.
「터치 후 반응」은 무작위와 구분이 안 돼 폐기했다(문서 §4.2).

## 제외 규칙 (데이터 품질)

  - 봉 수 300 미만인 세션(04/28~30, 05/14, 08/19 등) — 그날은 예측 대상에서도, 이력에서도 뺀다
  - 일중 범위가 가격의 20%를 넘거나 전일 대비 갭이 15%를 넘는 세션(05/13 +752 점프) — 데이터 오류
  - 분기 만기(3·6·9·12월 둘째 목요일)를 이력 창에 포함하는 표본 — 롤오버로 가격대가 어긋난다

## 규칙 공간 (훈련창에서 고른다 — 검증일 정보는 들어가지 않는다)

  lookback  : 매물대·갭을 볼 과거 세션 수 {6, 10, 20}
  sources   : 기본(매물대 봉우리·갭 변·전일 고저·VWAP) + 선택 {선반 가장자리, 전일 스윙, 다일 고저}
  spacing   : "nearest"(가까운 3개) | "atr"(기대 범위 R=k·ATR14를 3구간으로 나눠 구간마다 합류 최대)
  gap_tilt  : 갭 방향으로 기대 범위를 기울일지 {0, 1}
  k         : {0.8, 1.0, 1.3}

실행:
  python scripts/premarket_levels_wfa.py --test 2026-08-31 2026-09-04 --train 60
  python scripts/premarket_levels_wfa.py --test 2026-07-01 2026-09-04 --train 60   # 장기 롤링
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import sys
from collections import OrderedDict, defaultdict
from datetime import datetime, time as dtime
from functools import lru_cache
from pathlib import Path

# ---------------------------------------------------------------- 데이터

EXPIRIES = {"2025-09-11", "2025-12-11", "2026-03-12", "2026-06-11", "2026-09-10"}
MIN_BARS = 300

# 옵션 OI 행사가 원천 — **마흐디 DB**(option_analysis_1m). 날짜 → [(strike, oi), ...] 상위 3.
# 미륵 DB에는 옵션 체인이 없어 이 원천만 마흐디에서 가져온다. `load_mahdi_oi()`로 채운 뒤
# sources에 "oi"를 넣으면 candidates()가 쓴다. 전일 15:00~15:45 마지막 스냅샷의 만기 합산 OI.
OI_STRIKES: dict[str, list[tuple[float, int]]] = {}
GW_STRIKES: dict[str, list[tuple[float, float]]] = {}   # 날짜 → [(strike, |gamma×oi| 합)] 상위 2 (마흐디 gamma_walls와 같은 서열)


def load_mahdi_oi(top_n: int = 3, since: str = "2026-06-15") -> int:
    """마흐디 DB에서 날짜별 마감 OI 상위 행사가를 OI_STRIKES에 채운다. 반환: 날짜 수(0이면 DB 없음)."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from mahdi.data import db as mdb
        with mdb.get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """WITH snap AS (
                     SELECT DISTINCT ON (timestamp::date, expiry, strike, option_type) timestamp::date AS d, strike, oi
                     FROM option_analysis_1m
                     WHERE underlying='KOSPI200' AND timestamp >= %s AND timestamp::time BETWEEN '15:00' AND '15:45'
                     ORDER BY timestamp::date, expiry, strike, option_type, timestamp DESC)
                   SELECT d, strike, SUM(oi) AS oi FROM snap GROUP BY d, strike ORDER BY d, oi DESC""", (since,))
            for d, strike, oi in cur.fetchall():
                lst = OI_STRIKES.setdefault(str(d), [])
                if len(lst) < top_n:
                    lst.append((float(strike), int(oi or 0)))
    except Exception as exc:  # DB가 없는 PC에서는 원천 없이 진행
        print(f"마흐디 OI 원천 로드 실패 — {exc}")
        return 0
    return len(OI_STRIKES)


def load_mahdi_gamma_walls(top_n: int = 2, since: str = "2026-06-15") -> int:
    """마흐디 DB에서 날짜별 |gamma × oi| 합 상위 행사가(감마 월)를 GW_STRIKES에 채운다."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from mahdi.data import db as mdb
        with mdb.get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """WITH snap AS (
                     SELECT DISTINCT ON (timestamp::date, expiry, strike, option_type) timestamp::date AS d, strike, gamma, oi
                     FROM option_analysis_1m
                     WHERE underlying='KOSPI200' AND timestamp >= %s AND timestamp::time BETWEEN '15:00' AND '15:45'
                     ORDER BY timestamp::date, expiry, strike, option_type, timestamp DESC)
                   SELECT d, strike, SUM(ABS(COALESCE(gamma,0) * COALESCE(oi,0))) AS g FROM snap GROUP BY d, strike ORDER BY d, g DESC""", (since,))
            for d, strike, g in cur.fetchall():
                lst = GW_STRIKES.setdefault(str(d), [])
                if len(lst) < top_n and g and g > 0:
                    lst.append((float(strike), float(g)))
    except Exception as exc:
        print(f"마흐디 감마월 원천 로드 실패 — {exc}")
        return 0
    return len(GW_STRIKES)


def default_db() -> Path:
    return Path(__file__).resolve().parents[2] / "futures" / "data" / "db" / "raw_data.db"


def guard_intraday() -> None:
    now = datetime.now()
    if now.weekday() < 5 and dtime(8, 40) <= now.time() <= dtime(15, 40):
        print("장중(08:40~15:40)에는 미륵 라이브 DB를 읽지 않는다 — 장 마감 후 실행할 것")
        sys.exit(2)


def load_days(db: Path) -> "OrderedDict[str, list[tuple]]":
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    rows = con.execute("SELECT ts, open, high, low, close, volume FROM raw_candles ORDER BY ts").fetchall()
    con.close()
    days: OrderedDict[str, list[tuple]] = OrderedDict()
    for ts, o, h, l, c, v in rows:
        days.setdefault(ts[:10], []).append((ts[11:16], float(o), float(h), float(l), float(c), int(v or 0)))
    # 품질 제외
    clean: OrderedDict[str, list[tuple]] = OrderedDict()
    prev_close = None
    for d, b in days.items():
        hi, lo, o = max(x[2] for x in b), min(x[3] for x in b), b[0][1]
        bad = len(b) < MIN_BARS or (hi - lo) > 0.2 * o or (prev_close is not None and abs(o - prev_close) > 0.15 * prev_close)
        if not bad:
            clean[d] = b
        prev_close = b[-1][4]
    return clean


# ---------------------------------------------------------------- 후보 생성

BIN = 0.5
MERGE_PT = 1.5
DAYS: "OrderedDict[str, list[tuple]]" = OrderedDict()
DS: list[str] = []


def _vwap(bars):
    vol = sum(b[5] for b in bars)
    return sum(b[4] * b[5] for b in bars) / vol if vol else None


@lru_cache(maxsize=None)
def day_hl(d: str):
    b = DAYS[d]
    return max(x[2] for x in b), min(x[3] for x in b)


@lru_cache(maxsize=None)
def daily_atr(i: int, n: int = 14) -> float:
    trs, prev_close = [], None
    for d in DS[max(0, i - n):i]:
        h, l = day_hl(d)
        tr = h - l if prev_close is None else max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)
        prev_close = DAYS[d][-1][4]
    return sum(trs) / len(trs)


def zigzag(bars, min_move: float):
    highs, lows = [], []
    trend, hi_i, lo_i = 0, 0, 0
    for i, b in enumerate(bars):
        if b[2] > bars[hi_i][2]:
            hi_i = i
        if b[3] < bars[lo_i][3]:
            lo_i = i
        if trend >= 0 and bars[hi_i][2] - b[3] >= min_move:
            highs.append(bars[hi_i][2]); trend = -1; lo_i = i
        elif trend <= 0 and b[2] - bars[lo_i][3] >= min_move:
            lows.append(bars[lo_i][3]); trend = 1; hi_i = i
    return highs, lows


@lru_cache(maxsize=None)
def candidates(i: int, lookback: int, sources: frozenset) -> dict:
    """DS[i]를 위한 후보 — DS[i-lookback:i]만 본다. {레벨: [근거]}"""
    hist = DS[max(0, i - lookback):i]
    prev = hist[-1]
    cands: dict[int, list[str]] = defaultdict(list)

    dwell: dict[float, int] = defaultdict(int)
    for d in hist:
        for _, _, h, l, _, _ in DAYS[d]:
            b = round(l / BIN) * BIN
            while b <= h + 1e-9:
                dwell[b] += 1
                b = round(b + BIN, 2)
    bins = sorted(dwell)
    sm = {b: dwell.get(round(b - BIN, 2), 0) + dwell[b] + dwell.get(round(b + BIN, 2), 0) for b in bins}
    peaks = []
    for b in bins:
        win = [x for x in bins if abs(x - b) <= 2.0]
        if sm[b] >= 60 and sm[b] == max(sm[x] for x in win):
            peaks.append(b); cands[round(b)].append(f"매물대{b}")
    if "edges" in sources:
        for pk in peaks:
            thr = 0.35 * sm[pk]
            for step in (-BIN, BIN):
                b = pk
                while round(b + step, 2) in sm and sm[round(b + step, 2)] >= thr:
                    b = round(b + step, 2)
                if abs(b - pk) >= 2.0:
                    cands[round(b)].append(f"선반변{b}")

    for a, b_ in zip(hist, hist[1:]):
        ah, al = day_hl(a); bh, bl = day_hl(b_)
        if bl > ah:
            cands[round(ah)].append(f"갭하변{a[5:]}"); cands[round(bl)].append(f"갭상변{b_[5:]}")
        if bh < al:
            cands[round(al)].append(f"갭상변{a[5:]}"); cands[round(bh)].append(f"갭하변{b_[5:]}")

    P = DAYS[prev]
    ph, pl = day_hl(prev)
    cands[round(ph)].append("전일고"); cands[round(pl)].append("전일저")
    if (v := _vwap(P)) is not None:
        cands[round(v)].append("전일VWAP")
    week = [x for d in hist[-4:] for x in DAYS[d]]
    if (wv := _vwap(week)) is not None:
        cands[round(wv)].append("4세션VWAP")

    if "swings" in sources:
        hs, ls = zigzag(P, max(3.0, 0.25 * daily_atr(i)))
        for s in hs:
            cands[round(s)].append("전일스윙고")
        for s in ls:
            cands[round(s)].append("전일스윙저")
    if "multiday" in sources:
        for d in hist[-5:-1]:
            h, l = day_hl(d)
            cands[round(h)].append(f"고{d[5:]}"); cands[round(l)].append(f"저{d[5:]}")
    if "oi" in sources:
        for strike, oi in OI_STRIKES.get(prev, []):
            cands[round(strike)].append(f"OI행사가{strike:g}({oi})")
    if "gw" in sources:
        for strike, g in GW_STRIKES.get(prev, []):
            cands[round(strike)].append(f"감마월{strike:g}")

    # ---- 조사 원천(2026-09-06) — 문헌·실무의 지지/저항 후보 ----
    pc = P[-1][4]
    if "pivot" in sources:      # 플로어 피봇
        piv = (ph + pl + pc) / 3
        for lv, tag in ((2 * piv - pl, "R1"), (piv + (ph - pl), "R2"), (2 * piv - ph, "S1"), (piv - (ph - pl), "S2"), (piv, "P")):
            cands[round(lv)].append(f"피봇{tag}")
    if "camarilla" in sources:  # 카마릴라 R3/R4/S3/S4
        rng = ph - pl
        for lv, tag in ((pc + rng * 1.1 / 4, "R3"), (pc + rng * 1.1 / 2, "R4"), (pc - rng * 1.1 / 4, "S3"), (pc - rng * 1.1 / 2, "S4")):
            cands[round(lv)].append(f"카마{tag}")
    if "vp" in sources:         # 전일 거래량 프로파일 POC/VAH/VAL (0.5pt bin, 70%)
        hist_v: dict[float, float] = defaultdict(float)
        for x in P:
            hist_v[round(x[4] / BIN) * BIN] += x[5]
        if hist_v and sum(hist_v.values()) > 0:
            keys = sorted(hist_v); poc = max(keys, key=lambda k: hist_v[k]); j = keys.index(poc)
            lo_i = hi_i = j; acc = hist_v[poc]; total = sum(hist_v.values())
            while acc < 0.7 * total and (lo_i > 0 or hi_i < len(keys) - 1):
                up_v = hist_v[keys[hi_i + 1]] if hi_i < len(keys) - 1 else -1
                dn_v = hist_v[keys[lo_i - 1]] if lo_i > 0 else -1
                if up_v >= dn_v:
                    hi_i += 1; acc += up_v
                else:
                    lo_i -= 1; acc += dn_v
            cands[round(poc)].append("POC"); cands[round(keys[hi_i])].append("VAH"); cands[round(keys[lo_i])].append("VAL")
    if "vwapband" in sources:   # 전일 VWAP ±1σ/±2σ (거래량 가중)
        v = _vwap(P)
        if v is not None:
            vol = sum(x[5] for x in P)
            sd = (sum(x[5] * (x[4] - v) ** 2 for x in P) / vol) ** 0.5 if vol else 0
            for m in (1, 2):
                cands[round(v + m * sd)].append(f"VWAP+{m}σ"); cands[round(v - m * sd)].append(f"VWAP-{m}σ")
    if "prevclose" in sources:
        cands[round(pc)].append("전일종가")
    if "ma" in sources:         # 세션 종가 이동평균 5/20/60
        closes = [DAYS[d][-1][4] for d in DS[:i]]
        for n in (5, 20, 60):
            if len(closes) >= n:
                cands[round(sum(closes[-n:]) / n)].append(f"MA{n}")
    if "round" in sources:      # 라운드 넘버 — 전일 종가 ±1.5ATR 안의 10pt 배수(5pt 배수는 "round5")
        atr = daily_atr(i); step = 5 if "round5" in sources else 10
        k = int((pc - 1.5 * atr) // step) * step
        while k <= pc + 1.5 * atr:
            if k > 0:
                cands[round(k)].append(f"라운드{k}")
            k += step
    if "weekhl" in sources:     # 전주 고저 (ISO 주)
        from datetime import date as _date
        wk = _date.fromisoformat(prev).isocalendar()[1]
        prev_week = [d for d in DS[:i] if _date.fromisoformat(d).isocalendar()[1] == wk - 1 and _date.fromisoformat(d).year == _date.fromisoformat(prev).year]
        if prev_week:
            cands[round(max(day_hl(d)[0] for d in prev_week))].append("전주고"); cands[round(min(day_hl(d)[1] for d in prev_week))].append("전주저")
    if "hl20" in sources:       # 20세션 고저
        win = DS[max(0, i - 20):i]
        cands[round(max(day_hl(d)[0] for d in win))].append("20일고"); cands[round(min(day_hl(d)[1] for d in win))].append("20일저")

    merged: dict[int, list[str]] = {}
    for lv in sorted(cands):
        key = next((k for k in merged if abs(k - lv) <= MERGE_PT), None)
        if key is None:
            merged[lv] = list(cands[lv])
        else:
            merged[key].extend(cands[lv])
    return merged


# ---------------------------------------------------------------- 선택·채점

def select_levels(merged, ref: float, p: dict, atr: float, gap: float):
    ups_all = sorted(k for k in merged if k > ref + 1)
    dns_all = sorted((k for k in merged if k < ref - 1), reverse=True)
    if p["spacing"] == "nearest":
        return ups_all[:3], dns_all[:3]
    R_up = R_dn = p["k"] * atr
    if p["gap_tilt"] and atr > 0:
        z = max(-1.0, min(1.0, gap / atr))
        R_up *= 1 + 0.5 * z
        R_dn *= 1 - 0.5 * z

    def pick(side_all, R, sign):
        out = []
        for lo, hi in ((0, 0.35), (0.35, 0.7), (0.7, 1.25)):
            pool = [k for k in side_all if lo * R <= sign * (k - ref) < hi * R and k not in out]
            if pool:
                out.append(max(pool, key=lambda k: (len(merged[k]), -abs(k - ref))))
        for k in side_all:
            if len(out) >= 3:
                break
            if k not in out:
                out.append(k)
        return sorted(out, key=lambda k: sign * (k - ref))[:3]
    return pick(ups_all, R_up, 1), pick(dns_all, R_dn, -1)


def predict(i: int, p: dict):
    merged = candidates(i, p["lookback"], p["sources"])
    ref = DAYS[DS[i]][0][1]
    gap = ref - DAYS[DS[i - 1]][-1][4]
    return select_levels(merged, ref, p, daily_atr(i), gap), merged


def tol_for(i: int, tol_pct: float, tol_pt: float | None) -> float:
    return tol_pt if tol_pt is not None else tol_pct * DAYS[DS[i]][0][1]


def score(i: int, up, dn, tol: float) -> tuple[int, int]:
    hi, lo = day_hl(DS[i])
    return int(any(abs(hi - k) <= tol for k in up)), int(any(abs(lo - k) <= tol for k in dn))


def contaminated(i: int, lookback: int) -> bool:
    return any(d in EXPIRIES for d in DS[max(0, i - lookback):i + 1])


GRID = [
    dict(lookback=lb, sources=src, spacing=sp, gap_tilt=gt, k=k)
    for lb in (6, 10, 20)
    for src in (frozenset(), frozenset({"edges"}), frozenset({"swings"}), frozenset({"edges", "swings", "multiday"}))
    for sp, gt, k in ((("nearest", 0, 1.0),) + tuple(("atr", gt, k) for gt in (0, 1) for k in (0.8, 1.0, 1.3)))
]
BASELINE = dict(lookback=6, sources=frozenset(), spacing="nearest", gap_tilt=0, k=1.0)


def fmt(p):
    return f"lb={p['lookback']} src={'+'.join(sorted(p['sources'])) or '기본'} {p['spacing']}" + (
        f"(k={p['k']},tilt={p['gap_tilt']})" if p["spacing"] == "atr" else "")


def main(argv=None) -> int:
    global DAYS, DS
    ap = argparse.ArgumentParser(description="장전 맥점 규칙 WFA")
    ap.add_argument("--db", type=Path, default=default_db())
    ap.add_argument("--test", nargs=2, metavar=("FROM", "TO"), required=True)
    ap.add_argument("--train", type=int, default=60)
    ap.add_argument("--tol-pct", type=float, default=0.0015)
    ap.add_argument("--tol-pt", type=float, default=None)
    ap.add_argument("--null", type=int, default=300)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    if not a.force:
        guard_intraday()
    sys.stdout.reconfigure(errors="replace")

    DAYS = load_days(a.db); DS = list(DAYS)
    test_idx = [i for i, d in enumerate(DS) if a.test[0] <= d <= a.test[1] and i >= a.train + 21]
    if not test_idx:
        print("검증일이 없다"); return 1
    print(f"데이터(품질 제외 후) {DS[0]}~{DS[-1]} {len(DS)}세션 · 검증 {DS[test_idx[0]]}~{DS[test_idx[-1]]} "
          f"{len(test_idx)}일 · 훈련창 {a.train}세션 · TOL {'±%.2gpt' % a.tol_pt if a.tol_pt else '기준가의 %.2f%%' % (a.tol_pct*100)}")

    # (일, 규칙)별 점수 캐시 — 훈련·검증 모두 여기서 읽는다
    need = range(test_idx[0] - a.train, test_idx[-1] + 1)
    table: dict[tuple[int, int], tuple[int, int] | None] = {}
    for i in need:
        tol = tol_for(i, a.tol_pct, a.tol_pt)
        for r, p in enumerate(GRID):
            if contaminated(i, p["lookback"]):
                table[(i, r)] = None
                continue
            (u, dn), _ = predict(i, p)
            table[(i, r)] = score(i, u, dn, tol)

    rows = []
    wf = [0, 0]; bl = [0, 0]
    for i in test_idx:
        train = range(i - a.train, i)
        best_r, best_acc = 0, -1.0
        for r in range(len(GRID)):
            h = t = 0
            for j in train:
                s = table[(j, r)]
                if s is not None:
                    h += s[0] + s[1]; t += 2
            acc = h / t if t else 0.0
            if acc > best_acc + 1e-9:
                best_r, best_acc = r, acc
        p = GRID[best_r]
        tol = tol_for(i, a.tol_pct, a.tol_pt)
        (u, dn), merged = predict(i, p)
        (bu, bdn), _ = predict(i, BASELINE)
        su, sb = score(i, u, dn, tol), score(i, bu, bdn, tol)
        wf[0] += sum(su); wf[1] += 2; bl[0] += sum(sb); bl[1] += 2
        rows.append((i, u, dn, su, p, best_acc, bu, bdn, sb, merged, tol))

    print("\n## 일별 — 예측은 전날까지의 데이터 + 당일 08:45 시가(08:50 재산출 조건)로만")
    for i, u, dn, su, p, bacc, bu, bdn, sb, merged, tol in rows:
        d = DS[i]; o = DAYS[d][0][1]; hi, lo = day_hl(d)
        print(f"\n[{d}] 시가 {o:.2f} 고 {hi:.2f} 저 {lo:.2f}  (허용 ±{tol:.2f})")
        print(f"  WFA 규칙: {fmt(p)}  (훈련창 적중 {bacc:.0%})")
        print(f"  WFA   상방 {u} 하방 {dn}  → 고가 {'적중' if su[0] else '미적중'} · 저가 {'적중' if su[1] else '미적중'}")
        print(f"        근거 상방: " + " | ".join(f"{k}:{'/'.join(merged[k][:3])}" for k in u))
        print(f"        근거 하방: " + " | ".join(f"{k}:{'/'.join(merged[k][:3])}" for k in dn))
        print(f"  기준선 상방 {bu} 하방 {bdn}  → 고가 {'적중' if sb[0] else '미적중'} · 저가 {'적중' if sb[1] else '미적중'}")

    print("\n## 합계 (관측 = 세션×측)")
    print(f"  WFA   : {wf[0]}/{wf[1]} = {wf[0]/wf[1]:.0%}")
    print(f"  기준선: {bl[0]}/{bl[1]} = {bl[0]/bl[1]:.0%}   (현행 premarket_levels.py 규칙 lb=6·기본·nearest, ref=open)")

    random.seed(11)
    du = [k - DAYS[DS[i]][0][1] for i, u, *_ in rows for k in u]
    dd = [DAYS[DS[i]][0][1] - k for i, _, dn, *_ in rows for k in dn]
    nulls = []
    for _ in range(a.null):
        random.shuffle(du); random.shuffle(dd); iu = idn = 0; h = 0
        for i, u, dn, *_ , tol in rows:
            o = DAYS[DS[i]][0][1]
            nu = [round(o + x) for x in du[iu:iu + len(u)]]; iu += len(u)
            nd = [round(o - x) for x in dd[idn:idn + len(dn)]]; idn += len(dn)
            s = score(i, nu, nd, tol); h += s[0] + s[1]
        nulls.append(h)
    nulls.sort()
    print(f"  무작위(거리 셔플 {a.null}회): 중앙값 {nulls[len(nulls)//2]}/{wf[1]} · 95% 상한 {nulls[int(len(nulls)*0.95)]}/{wf[1]} · "
          f"p(무작위≥WFA)={sum(1 for x in nulls if x >= wf[0])/len(nulls):.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
