"""장전 맥점 예측 준비 — 이력 캐시 갱신 → 거리 모델 파라미터·구조 후보 산출 → `data/premarket_levels/<오늘>.json`.

2026-09-06 [MW0601]. `start_mahdi_premarket.bat`이 COCKPIT을 띄우기 **전에** 부른다. 08:50·09:30 단계 산출은
COCKPIT이 `mahdi.ops.premarket_levels_store.ensure_stage()`로 한다(`--stage`로 수동 실행도 된다).

## 이력 출처와 규칙

  1. 미륵 `../futures/data/db/raw_data.db` 1분봉(2025-08~) — **08:40 이전 또는 15:40 이후에만** 읽는다
     (미륵 CLAUDE.md: 장중 라이브 DB 스캔 금지). 그 시간대가 아니면 캐시를 그대로 쓴다.
  2. 마흐디 `market_raw_1m` 선물 봉(2026-08-20~) — 항상 읽는다. 같은 날은 마흐디가 미륵을 덮는다.
  3. 캐시 `history_cache.json`에 요약(전 세션)과 최근 7세션 봉을 둔다. 미륵 DB가 없는 PC에서도 캐시가
     있으면 돌아간다 — 캐시는 PC별 산출물이라 커밋하지 않는다.

## 하지 않는 것

  DB에 쓰지 않는다. 신호·진입에 연결하지 않는다. 훈련 세션이 30 미만이면 거리 모델을 **내지 않는다**.

실행:
  python scripts/premarket_levels_publish.py                 # 오늘 준비
  python scripts/premarket_levels_publish.py --date 2026-09-04 --stage 0850 --stage 0930   # 과거 날짜 재현
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import date, datetime, time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mahdi.config.settings import PROJECT_ROOT
from mahdi.data import db
from mahdi.features import premarket_levels as PL
from mahdi.ops import premarket_levels_store as store

MIREUK_DB = PROJECT_ROOT.parent / "futures" / "data" / "db" / "raw_data.db"


def mireuk_allowed(now: datetime) -> bool:
    return now.weekday() >= 5 or now.time() < dtime(8, 40) or now.time() > dtime(15, 40)


def load_mireuk(path: Path, since: str) -> dict[str, list[PL.Bar]]:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    rows = con.execute("SELECT ts, open, high, low, close, volume FROM raw_candles WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
    con.close()
    out: dict[str, list[PL.Bar]] = {}
    for ts, o, h, l, c, v in rows:
        out.setdefault(ts[:10], []).append(PL.Bar(t=ts[11:16], o=float(o), h=float(h), l=float(l), c=float(c), v=int(v or 0)))
    # 품질 제외 — 봉 300개 미만, 비정상 점프(검증 스크립트와 같은 규칙)
    clean, pc = {}, None
    for d in sorted(out):
        b = out[d]; hi, lo, o = max(x.h for x in b), min(x.l for x in b), b[0].o
        bad = len(b) < 300 or (hi - lo) > 0.2 * o or (pc is not None and abs(o - pc) > 0.15 * pc)
        if not bad:
            clean[d] = b
        pc = b[-1].c
    return clean


def load_mahdi(conn, since: date) -> dict[str, list[PL.Bar]]:
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT symbol FROM market_raw_1m WHERE timestamp >= %s", (datetime.combine(since, dtime(0, 0)),))
        symbols = [s for (s,) in cur.fetchall() if store.FUTURES_SYMBOL_RE.match(s or "")]
        cur.execute("SELECT DISTINCT timestamp::date FROM market_raw_1m WHERE symbol = ANY(%s) AND timestamp >= %s ORDER BY 1",
                    (symbols, datetime.combine(since, dtime(0, 0))))
        days = [r[0] for r in cur.fetchall()]
    out = {}
    for d in days:
        best = max((store.fetch_bars(conn, s, d) for s in symbols), key=len, default=[])
        if len(best) >= 300:
            out[d.isoformat()] = best
    return out


def oi_strikes(conn, prev: date, top_n: int = 3) -> list[tuple[float, int]]:
    with conn.cursor() as cur:
        cur.execute(
            """WITH snap AS (
                 SELECT DISTINCT ON (expiry, strike, option_type) strike, oi FROM option_analysis_1m
                 WHERE underlying='KOSPI200' AND timestamp >= %s AND timestamp < %s AND timestamp::time BETWEEN '15:00' AND '15:45'
                 ORDER BY expiry, strike, option_type, timestamp DESC)
               SELECT strike, SUM(oi) FROM snap GROUP BY 1 ORDER BY 2 DESC LIMIT %s""",
            (datetime.combine(prev, dtime(0, 0)), datetime.combine(prev, dtime(23, 59)), top_n))
        return [(float(s), int(o or 0)) for s, o in cur.fetchall()]


def publish(target: date, now: datetime, mireuk: Path | None) -> dict:
    hist = PL.load_history(store.HISTORY_PATH)
    notes: list[str] = []
    if mireuk and mireuk.exists() and mireuk_allowed(now):
        for d, bars in load_mireuk(mireuk, "2025-08-01").items():
            hist.upsert(d, bars, "mireuk")
        notes.append(f"미륵 이력 갱신({mireuk.name})")
    elif mireuk and mireuk.exists():
        notes.append("미륵 DB는 장중이라 읽지 않음 — 캐시 사용")
    else:
        notes.append("미륵 DB 없음 — 캐시/마흐디만")
    with db.get_connection() as conn:
        for d, bars in load_mahdi(conn, date(2026, 8, 1)).items():
            if d < target.isoformat():
                hist.upsert(d, bars, "mahdi")
        hist.finalize()
        PL.save_history(store.HISTORY_PATH, hist)
        past = [s for s in hist.summaries if s.d < target.isoformat()]
        if not past:
            raise SystemExit("이력이 없다 — 미륵 DB 경로나 마흐디 봉을 확인할 것")
        prev = past[-1]
        atr = PL.atr_of(past, len(past))
        p1 = PL.fit_stage1([s for s in past[-PL.TRAIN_SESSIONS:]])
        p2 = PL.fit_stage2(past, len(past))
        rhat1 = PL.fit_rhat(past, len(past), with_path=False)
        rhat2 = PL.fit_rhat(past, len(past), with_path=True)
        atr5 = PL.atr_of(past, len(past), 5)
        hist_bars = [(s.d, hist.bars[s.d]) for s in past[-PL.LOOKBACK:] if s.d in hist.bars]
        oi = oi_strikes(conn, date.fromisoformat(prev.d))
    cands = PL.structural_candidates(hist_bars, oi)
    warnings = []
    if len(hist_bars) < PL.LOOKBACK:
        warnings.append(f"구조 후보 이력 {len(hist_bars)}세션(6 필요)")
    if PL.is_quarterly_expiry_window(target, [d for d, _ in hist_bars]):
        warnings.append("분기 만기 창 — 구조 후보 롤 오염 가능")
    if not oi:
        warnings.append("전일 옵션 OI 없음")
    if atr is None:
        raise SystemExit("ATR을 낼 이력이 없다")
    payload = dict(date=target.isoformat(), generated_at=now.strftime("%Y-%m-%d %H:%M:%S"), notes=notes, warnings=warnings,
                   history_sessions=len(past), history_last=prev.d, atr=atr,
                   prev=dict(d=prev.d, o=prev.o, h=prev.h, l=prev.l, c=prev.c),
                   p1=p1, p2=p2, rhat1=rhat1, rhat2=rhat2, atr5=atr5,
                   candidates={str(k): v for k, v in sorted(cands.items())}, stages={})
    existing = store.load_day(target)
    if existing and existing.get("stages"):
        payload["stages"] = existing["stages"]     # 이미 굳힌 단계는 지키기
    store.save_day(target, payload)
    return payload


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="장전 맥점 예측 준비")
    ap.add_argument("--date", type=date.fromisoformat, default=None)
    ap.add_argument("--mireuk", type=Path, default=MIREUK_DB)
    ap.add_argument("--stage", action="append", choices=("0850", "0930"), default=[], help="준비 뒤 단계도 산출(과거 날짜 재현용)")
    ap.add_argument("--force-stage", action="store_true", help="이미 굳힌 단계를 지우고 다시 산출")
    ap.add_argument("--score", action="store_true", help="장후 채점(실제 고·저 vs 산출)을 오늘 파일에 남기고 표를 찍는다")
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(errors="replace")
    now = db.local_now()
    target = a.date or now.date()
    payload = publish(target, now, a.mireuk)
    p1, p2 = payload["p1"], payload["p2"]
    print(f"[{target}] 이력 {payload['history_sessions']}세션(~{payload['history_last']}) · ATR14 {payload['atr']:.1f} · "
          f"M1 {'med_u %.2f med_d %.2f' % (p1['med_u'], p1['med_d']) if p1 else '미산출'} · P1 {'n=%d' % p2['n'] if p2 else '미산출'} · "
          f"구조 후보 {len(payload['candidates'])}개 · {' / '.join(payload['notes'])}"
          + (f" · ⚠ {' / '.join(payload['warnings'])}" if payload["warnings"] else ""))
    for stage in a.stage:
        if a.force_stage:
            day = store.load_day(target); day["stages"].pop(stage, None); store.save_day(target, day)
        at = datetime.combine(target, dtime(23, 59)) if target != now.date() else now
        day = store.ensure_stage(stage, at)
        s = (day or {}).get("stages", {}).get(stage)
        if not s:
            print(f"  {stage}: 미산출 — {(day or {}).get('stage_notes', {}).get(stage, '때가 안 됐거나 봉 없음')}")
            continue
        dist = s["distance"]
        print(f"  {stage}: 기준가 {s['ref']:.2f}")
        if dist:
            print(f"    거리모델 고점 {dist['high']:.1f} (50% {dist['high50'][0]:.0f}~{dist['high50'][1]:.0f} · 80% {dist['high80'][0]:.0f}~{dist['high80'][1]:.0f})"
                  f" / 저점 {dist['low']:.1f} (50% {dist['low50'][0]:.0f}~{dist['low50'][1]:.0f} · 80% {dist['low80'][0]:.0f}~{dist['low80'][1]:.0f})")
        print(f"    구조모델 상방 {[k for k, _ in s['structure']['up']]} 하방 {[k for k, _ in s['structure']['down']]}"
              + (f"  (거리모델 R̂스케일 {dist['scale']:.2f})" if dist and dist.get("scale") else ""))
    if a.score:
        print(store.score_markdown(target))
    return 0


if __name__ == "__main__":
    sys.exit(main())
