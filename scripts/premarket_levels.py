"""장전 맥점(상방 저항 / 하방 지지) 후보를 **전일까지의 데이터만으로** 낸다.

2026-09-04 — docs/Dev_md/RESEARCH_PREMARKET_LEVELS_v1.md 의 검증 도구.

## 이 스크립트가 생긴 이유

09-04 08:50에 외부 트레이더(@PeterLeejoa)가 낸 상방 저항 1049 / 1057 / 1063이 그날 장중에
그대로 마디로 작용했다(09:00~13:00 154분간 1049~1052 상단 봉쇄, 14:05 1057 일시 정체,
14:19 1063.85 고점 반전). 역산해 보니 그 세 수는 **공식(피봇·카마릴라·피보나치·ATR·이동평균)**
으로는 하나도 안 나오고, 전일 이전 세션의 **구조적 가격**(매물대 선반·갭 가장자리·전일 고저·
VWAP·옵션 OI 행사가)에서만 셋 다 나온다. 이 스크립트는 그 구조적 후보를 기계적으로 뽑아
합류도(같은 자리를 가리키는 근거 수) 순으로 정렬한다.

## 이 스크립트가 하지 않는 것

**판단하지 않는다.** 레벨은 후보이고 사람이(또는 나중에 신호 계층이) 쓴다. 자동 매매 경로에
연결돼 있지 않고, 결과를 DB에 쓰지도 않는다 — 콘솔에 적고 끝난다. 레벨을 「맞힌다」고 주장하지도
않는다: --evaluate 는 그날 봉이 있을 때 터치/반응을 **세기만** 한다(관대한 기준 — 문서 §5 참고).

## 데이터 규약

market_raw_1m.timestamp 는 naive KST가 "+00"으로 라벨된 값이다(마이그레이션 008). 그래서
날짜 비교는 timestamp::date 로 하고 타임존 변환을 하지 않는다 — 변환하면 15:00 이후 봉이
다음 날로 넘어간다(이 스크립트를 만들며 실제로 한 번 틀린 자리).

실행:
  python scripts/premarket_levels.py                  # 오늘(다음 거래일) 레벨
  python scripts/premarket_levels.py --date 2026-09-04 --evaluate
  python scripts/premarket_levels.py --sweep 9        # 최근 9세션 적중 요약
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mahdi.data import db

UNDERLYING = "KOSPI200"
BIN = 0.5            # 매물대 히스토그램 bin (선물 호가 단위 0.05 x 10)
LOOKBACK = 6         # 매물대·갭을 볼 과거 세션 수
PEAK_WINDOW = 2.0    # 이 폭(pt) 안에서 최대인 bin만 매물대 peak로 인정
PEAK_MIN_MINUTES = 60  # 3-bin 평활 합이 이 분 수 미만이면 매물대로 치지 않는다
MERGE_PT = 1.5       # 이 거리 안의 후보는 한 레벨로 합친다(합류도가 근거 수)
TOUCH_PT = 1.0       # 평가: 봉의 고저가 레벨 ±이 안에 들어오면 터치
REACT_PT = 3.0       # 평가: 터치 후 30분 안에 반대 방향으로 이만큼 가면 반응
REACT_BARS = 30


def session_bars(conn, symbol: str, day: date) -> list[tuple]:
    """(time, open, high, low, close, volume) — 그날 1분봉 전부. 없으면 []."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT timestamp::time, open, high, low, close, volume FROM market_raw_1m "
            "WHERE symbol=%s AND timestamp::date=%s ORDER BY timestamp",
            (symbol, day),
        )
        return [(t, float(o), float(h), float(l), float(c), int(v or 0)) for t, o, h, l, c, v in cur.fetchall()]


def sessions_before(conn, symbol: str, day: date, n: int) -> list[date]:
    """day 이전에 봉이 있는 세션 n개(오름차순)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT timestamp::date FROM market_raw_1m WHERE symbol=%s AND timestamp::date<%s "
            "ORDER BY 1 DESC LIMIT %s",
            (symbol, day, n),
        )
        return [r[0] for r in cur.fetchall()][::-1]


def top_oi_strikes(conn, day: date, top_n: int = 3) -> list[tuple[float, int]]:
    """그날 15:00~15:45 마지막 스냅샷 기준, 만기 합산 OI 상위 행사가."""
    with conn.cursor() as cur:
        cur.execute(
            """WITH snap AS (
                 SELECT DISTINCT ON (expiry, strike, option_type) strike, oi
                 FROM option_analysis_1m
                 WHERE underlying=%s
                   AND timestamp BETWEEN %s::date::timestamptz + interval '15 hours'
                                     AND %s::date::timestamptz + interval '15 hours 45 minutes'
                 ORDER BY expiry, strike, option_type, timestamp DESC)
               SELECT strike, SUM(oi) FROM snap GROUP BY 1 ORDER BY 2 DESC LIMIT %s""",
            (UNDERLYING, day, day, top_n),
        )
        return [(float(s), int(o or 0)) for s, o in cur.fetchall()]


def _vwap(bars: list[tuple]) -> float | None:
    vol = sum(b[5] for b in bars)
    return sum(b[4] * b[5] for b in bars) / vol if vol > 0 else None


def build_candidates(conn, symbol: str, day: date) -> tuple[float | None, dict[int, list[str]], list[date]]:
    """
    입력: 대상 거래일(레벨은 그 **이전** 세션들로만 만든다).
    계산: 네 원천의 후보를 정수 pt로 모아 MERGE_PT 안을 합친다.
      (a) 매물대 — 과거 LOOKBACK 세션의 1분봉이 지나간 분 수를 BIN별로 누적, 국소 최대 bin
      (b) 갭 가장자리 — 연속 세션 범위가 안 겹치는 구간의 양 끝
      (c) 전일 고/저, 전일 세션 VWAP, 최근 4세션 VWAP(주간 앵커)
      (d) 전일 마감 옵션 OI 상위 행사가
    반환: (전일 마지막 봉 종가, {레벨: [근거...]}, 사용한 세션 목록). 전일 봉이 없으면 (None, {}, []).
    """
    hist = sessions_before(conn, symbol, day, LOOKBACK)
    if not hist:
        return None, {}, []
    bars_by_day = {d: session_bars(conn, symbol, d) for d in hist}
    prev = hist[-1]
    cands: dict[int, list[str]] = defaultdict(list)

    # (a) 매물대
    dwell: dict[float, int] = defaultdict(int)
    dwell_days: dict[float, set] = defaultdict(set)
    for d, bars in bars_by_day.items():
        for _, _, h, l, _, _ in bars:
            b = round(l / BIN) * BIN
            while b <= h + 1e-9:
                dwell[b] += 1
                dwell_days[b].add(d)
                b = round(b + BIN, 2)
    bins = sorted(dwell)
    smooth = {b: dwell.get(round(b - BIN, 2), 0) + dwell[b] + dwell.get(round(b + BIN, 2), 0) for b in bins}
    for b in bins:
        window = [x for x in bins if abs(x - b) <= PEAK_WINDOW]
        if smooth[b] >= PEAK_MIN_MINUTES and smooth[b] == max(smooth[x] for x in window):
            cands[round(b)].append(f"매물대{b}({smooth[b]}분/{len(dwell_days[b])}세션)")

    # (b) 갭 가장자리
    for a, b_ in zip(hist, hist[1:]):
        A, B = bars_by_day[a], bars_by_day[b_]
        if not A or not B:
            continue
        ah, al = max(x[2] for x in A), min(x[3] for x in A)
        bh, bl = max(x[2] for x in B), min(x[3] for x in B)
        if bl > ah:   # 갭 상승 — 메워지지 않은 구간 [ah, bl]
            cands[round(ah)].append(f"갭하변{a:%m/%d}고{ah}")
            cands[round(bl)].append(f"갭상변{b_:%m/%d}저{bl}")
        if bh < al:   # 갭 하락 — 메워지지 않은 구간 [bh, al]
            cands[round(al)].append(f"갭상변{a:%m/%d}저{al}")
            cands[round(bh)].append(f"갭하변{b_:%m/%d}고{bh}")

    # (c) 전일 고저 / VWAP
    P = bars_by_day[prev]
    if not P:
        return None, {}, hist
    ph, pl = max(x[2] for x in P), min(x[3] for x in P)
    cands[round(ph)].append(f"전일고{ph}")
    cands[round(pl)].append(f"전일저{pl}")
    if (v := _vwap(P)) is not None:
        cands[round(v)].append(f"전일VWAP{v:.1f}")
    week = [x for d in hist[-4:] for x in bars_by_day[d]]
    if (wv := _vwap(week)) is not None:
        cands[round(wv)].append(f"4세션VWAP{wv:.1f}")

    # (d) 옵션 OI 행사가
    for strike, oi in top_oi_strikes(conn, prev):
        cands[round(strike)].append(f"OI행사가{strike:g}({oi})")

    merged: dict[int, list[str]] = {}
    for lv in sorted(cands):
        key = next((k for k in merged if abs(k - lv) <= MERGE_PT), None)
        if key is None:
            merged[lv] = list(cands[lv])
        else:
            merged[key].extend(cands[lv])
    return P[-1][4], merged, hist


def pick(merged: dict[int, list[str]], ref: float, n: int = 3) -> tuple[list[int], list[int]]:
    """ref 위로 가까운 n개(상방), 아래로 가까운 n개(하방). ref±1 안은 어느 쪽도 아니다."""
    ups = sorted(k for k in merged if k > ref + 1)[:n]
    dns = sorted((k for k in merged if k < ref - 1), reverse=True)[:n]
    return ups, dns


def reaction(bars: list[tuple], level: int, side: str) -> tuple[str | None, bool]:
    """(첫 터치 시각 HH:MM 또는 None, 터치 후 REACT_BARS 안에 REACT_PT 반대 이동 여부)."""
    for idx, x in enumerate(bars):
        if x[3] - TOUCH_PT <= level <= x[2] + TOUCH_PT:
            after = bars[idx:idx + REACT_BARS]
            if side == "up":
                reacted = min(a[3] for a in after) <= level - REACT_PT
            else:
                reacted = max(a[2] for a in after) >= level + REACT_PT
            return x[0].strftime("%H:%M"), reacted
    return None, False


def report(conn, symbol: str, day: date, evaluate: bool, ref_mode: str) -> tuple[int, int, int]:
    """한 날짜를 인쇄하고 (레벨 수, 터치 수, 반응 수)를 돌려준다(sweep 집계용)."""
    prev_close, merged, hist = build_candidates(conn, symbol, day)
    if prev_close is None:
        print(f"[{day}] 전일 봉이 없다 — 레벨을 만들 수 없다")
        return 0, 0, 0
    today = session_bars(conn, symbol, day)
    ref = prev_close
    ref_label = f"전일종가 {prev_close}"
    if ref_mode == "open" and today:
        ref = today[0][1]
        ref_label = f"당일 08:45 시가 {ref}"
    ups, dns = pick(merged, ref)
    print(f"\n=== {day}  기준 {ref_label}  (원천 세션 {hist[0]:%m/%d}~{hist[-1]:%m/%d}, 후보 {len(merged)}개)")
    counts = [0, 0, 0]
    for label, side, levels in (("상방", "up", ups), ("하방", "dn", dns)):
        for i, k in enumerate(levels, 1):
            tail = ""
            if evaluate and today:
                touched, reacted = reaction(today, k, side)
                counts[0] += 1
                counts[1] += touched is not None
                counts[2] += reacted
                tail = ("  터치" + touched + ("→반응" if reacted else "")) if touched else "  미도달"
            print(f"  {label}{i}차 {k:5d}  합류{len(merged[k])}{tail}  근거={merged[k]}")
    if evaluate and today:
        print(f"  (실제: 시가 {today[0][1]} 고 {max(x[2] for x in today)} 저 {min(x[3] for x in today)})")
    return counts[0], counts[1], counts[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="장전 맥점(저항/지지) 후보 산출")
    parser.add_argument("--date", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(), default=date.today())
    parser.add_argument("--symbol", default=None, help="선물 단축코드(기본: active_futures_symbol)")
    parser.add_argument("--evaluate", action="store_true", help="그날 봉이 있으면 터치/반응을 센다")
    parser.add_argument("--ref", choices=("close", "open"), default="close",
                        help="상하 분기 기준 — 전일종가(기본) 또는 당일 시가(08:50 이후 재산출용)")
    parser.add_argument("--sweep", type=int, default=0, help="최근 N세션을 --evaluate로 훑고 합계를 낸다")
    args = parser.parse_args(argv)

    # 콘솔이 cp949일 수 있다 — 한글은 되지만 안전하게 replace로 막는다(check_lever_due.py와 같은 이유).
    sys.stdout.reconfigure(errors="replace")

    with db.get_connection() as conn:
        symbol = args.symbol or db.get_active_futures_symbol(conn, UNDERLYING)
        if not symbol:
            print("active_futures_symbol이 비어 있다 — --symbol을 지정할 것")
            return 1
        if args.sweep:
            days = sessions_before(conn, symbol, args.date, args.sweep - 1) + [args.date]
            total = [0, 0, 0]
            for d in days:
                c = report(conn, symbol, d, evaluate=True, ref_mode=args.ref)
                total = [a + b for a, b in zip(total, c)]
            n, t, r = total
            if n:
                print(f"\n합계: 레벨 {n}  터치 {t}({t / n:.0%})  터치후 {REACT_BARS}분내 {REACT_PT:g}pt 반응 {r}({r / n:.0%})")
            return 0
        report(conn, symbol, args.date, evaluate=args.evaluate, ref_mode=args.ref)
    return 0


if __name__ == "__main__":
    sys.exit(main())
