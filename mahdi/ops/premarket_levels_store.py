"""당일 맥점 예측의 **파일 저장소와 단계 산출** — `data/premarket_levels/<날짜>.json`.

2026-09-06 [MW0601]. 순수 계산은 `mahdi/features/premarket_levels.py`, 이력 갱신은
`scripts/premarket_levels_publish.py`. 이 모듈은 그 둘 사이에서 **오늘 파일을 읽고, 때가 되면 단계를
산출해 굳히고(freeze), 배지 카드로 바꾼다.** COCKPIT(`dashboard/data_source.py`)이 매 갱신마다 부른다.

## 굳히기(freeze)

08:50 산출은 08:45 시가만 쓰므로 언제 계산해도 같지만, 09:30 산출은 「09:30까지의 봉」을 쓰므로 그 뒤에
계산하면 봉이 더 있어도 09:30 컷은 같다 — 그래도 **처음 계산한 값을 파일에 굳혀** 화면과 장후 채점이 같은
수를 보게 한다. 한 번 굳힌 단계는 다시 계산하지 않는다.

## 산출 시각

  08:50 단계: 벽시계 ≥ 08:50 이고 마흐디 DB에 08:45 봉이 있을 때.
  09:30 단계: 벽시계 ≥ 09:30 이고 09:30 이전 봉이 5개 이상일 때(수집 결손이면 「미산출」로 남는다).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

from mahdi.config.settings import PROJECT_ROOT
from mahdi.data import db
from mahdi.features import premarket_levels as PL

logger = logging.getLogger("mahdi.ops.premarket_levels_store")

DIR = PROJECT_ROOT / "data" / "premarket_levels"
HISTORY_PATH = DIR / "history_cache.json"
STAGE_DUE = {"0850": dtime(8, 50), "0930": dtime(9, 30)}
STAGE_CUT = {"0850": "08:45", "0930": "09:30"}
FUTURES_SYMBOL_RE = re.compile(r"^A\d{5}$")


def today_path(d: date) -> Path:
    return DIR / f"{d.isoformat()}.json"


def load_day(d: date) -> dict | None:
    p = today_path(d)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("맥점 파일을 읽지 못했다 — %s", p, exc_info=True)
        return None


def save_day(d: date, payload: dict) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    today_path(d).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")


def fetch_bars(conn, symbol: str, d: date, until: str | None = None) -> list[PL.Bar]:
    """그날 선물 1분봉(거래량 포함). until("HH:MM")이 있으면 그 시각 이하만."""
    start = datetime.combine(d, dtime(0, 0)); end = start + timedelta(days=1)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT timestamp, open, high, low, close, volume FROM market_raw_1m "
            "WHERE symbol=%s AND timestamp >= %s AND timestamp < %s ORDER BY timestamp",
            (symbol, start, end),
        )
        rows = cur.fetchall()
    bars = [PL.Bar(t=ts.strftime("%H:%M"), o=float(o), h=float(h), l=float(l), c=float(c), v=int(v or 0)) for ts, o, h, l, c, v in rows]
    return [b for b in bars if until is None or b.t <= until]


def futures_symbol(conn) -> str | None:
    return db.get_active_futures_symbol(conn, "KOSPI200")


def ensure_stage(stage: str, now: datetime) -> dict | None:
    """때가 됐고 아직 없으면 단계를 산출해 파일에 굳힌다. 반환: 오늘 파일 dict(없으면 None)."""
    d = now.date()
    day = load_day(d)
    if day is None or stage in day.get("stages", {}):
        return day
    if now.time() < STAGE_DUE[stage]:
        return day
    try:
        with db.get_connection() as conn:
            symbol = futures_symbol(conn)
            if not symbol:
                return day
            bars = fetch_bars(conn, symbol, d, until=STAGE_CUT[stage])
    except Exception:
        logger.warning("맥점 %s 단계 — 당일 봉 조회 실패", stage, exc_info=True)
        return day
    if not bars or bars[0].t > "08:50" or (stage == "0930" and len(bars) < 5):
        day.setdefault("stage_notes", {})[stage] = f"봉 부족({len(bars)}개) — 미산출"
        save_day(d, day)
        return day
    prev = PL.SessionSummary(**day["prev"])
    cands = {int(k): v for k, v in day["candidates"].items()}
    try:
        out = PL.compute_stage(stage, bars, prev, day["atr"], day.get("p1"), day.get("p2"), cands,
                               day.get("rhat1"), day.get("rhat2"), day.get("atr5"))
    except Exception:
        logger.warning("맥점 %s 단계 산출 실패", stage, exc_info=True)
        return day
    out["computed_at"] = now.strftime("%H:%M:%S"); out["symbol"] = symbol; out["bars"] = len(bars)
    day.setdefault("stages", {})[stage] = out
    save_day(d, day)
    logger.info("맥점 %s 단계 산출 — 기준가 %.2f 거리모델 %s 구조 상방 %s 하방 %s", stage, out["ref"],
                "있음" if out["distance"] else "없음", [k for k, _ in out["structure"]["up"]], [k for k, _ in out["structure"]["down"]])
    return day


# ---------------------------------------------------------------- 배지 카드

def _fmt_dist(dist: dict | None) -> str:
    if not dist:
        return "미산출 — 훈련 이력 부족(60세션 필요)"
    sc = f" · R̂×{dist['scale']:.2f}" if dist.get("scale") else ""
    return (f"고점 **{dist['high']:.1f}** (50% {dist['high50'][0]:.0f}~{dist['high50'][1]:.0f} · 80% {dist['high80'][0]:.0f}~{dist['high80'][1]:.0f})\n\n"
            f"저점 **{dist['low']:.1f}** (50% {dist['low50'][0]:.0f}~{dist['low50'][1]:.0f} · 80% {dist['low80'][0]:.0f}~{dist['low80'][1]:.0f}){sc}")


def _fmt_struct(struct: dict) -> str:
    def side(items):
        return " · ".join(f"**{k}**({len(v)})" for k, v in items) if items else "없음"
    return f"상방 {side(struct['up'])}\n\n하방 {side(struct['down'])}"


def badge_cards(now: datetime | None = None) -> list[dict]:
    """COCKPIT용 카드 4장 — [08:50 거리] [08:50 구조] [09:30 거리] [09:30 구조]. 산출 전이면 대기 카드."""
    now = now or db.local_now()
    day = None
    for stage in ("0850", "0930"):
        day = ensure_stage(stage, now) or day
    cards = []
    for stage, due in (("0850", "08:50"), ("0930", "09:30")):
        s = (day or {}).get("stages", {}).get(stage)
        note = (day or {}).get("stage_notes", {}).get(stage)
        if day is None:
            v = "오늘 파일 없음 — `scripts/premarket_levels_publish.py`가 장전에 돌지 않았다"
            cards += [dict(label=f"{due} 맥점 · 거리모델", value=v, status="warning"),
                      dict(label=f"{due} 맥점 · 구조모델", value=v, status="warning")]
            continue
        if s is None:
            v = note or (f"{due} 이후 산출" if now.time() < STAGE_DUE[stage] else "봉 대기 중")
            st = "warning" if note else "info"
            cards += [dict(label=f"{due} 맥점 · 거리모델", value=v, status=st),
                      dict(label=f"{due} 맥점 · 구조모델", value=v, status=st)]
            continue
        ref_txt = f"기준가 {s['ref']:.2f} · ATR {s['atr']:.1f} · {s['computed_at']} 산출"
        dist_help = ("검증(144세션): MAE 14.2pt · 80% 구간 78% → R̂ 스케일(하한 0.85) 적용 시 83%" if stage == "0850"
                     else "검증(144세션): MAE 11.0pt · 80% 구간 74% → R̂ 스케일(하한 0.85) 적용 시 81%")
        cards.append(dict(label=f"{due} 맥점 · 거리모델", value=f"{_fmt_dist(s['distance'])}\n\n{ref_txt}",
                          status="ok" if s["distance"] else "warning", help=dist_help))
        cards.append(dict(label=f"{due} 맥점 · 구조모델", value=f"{_fmt_struct(s['structure'])}\n\n{ref_txt}", status="info",
                          help="괄호 = 합류(근거 수). 검증(26주): 극값 안착·정거장률 모두 무작위와 구분되지 않음 — 참고용 후보"
                               + (" · " + " / ".join(day.get("warnings", [])) if day.get("warnings") else "")))
    return cards


# ---------------------------------------------------------------- 장후 채점 (일일점검 산출물 §18)

def _inside(v: float, band) -> bool:
    return bool(band[0] <= v <= band[1])


def score_day(d: date) -> dict | None:
    """실제 고·저(마흐디 봉 전체)로 그날의 두 단계를 채점해 파일에 남긴다. 반환: score dict(파일·단계 없으면 None)."""
    day = load_day(d)
    if day is None or not day.get("stages"):
        return None
    try:
        with db.get_connection() as conn:
            symbol = futures_symbol(conn)
            bars = fetch_bars(conn, symbol, d) if symbol else []
    except Exception:
        logger.warning("맥점 채점 — 당일 봉 조회 실패", exc_info=True)
        return None
    if len(bars) < 200:
        return None
    hi, lo = max(b.h for b in bars), min(b.l for b in bars)
    score: dict = dict(high=hi, low=lo, bars=len(bars), stages={})
    for stage, s in day["stages"].items():
        row: dict = {}
        dist = s.get("distance")
        if dist:
            raw = dist.get("raw")
            row["distance"] = dict(
                err_high=dist["high"] - hi, err_low=dist["low"] - lo,
                in50=[_inside(hi, dist["high50"]), _inside(lo, dist["low50"])],
                in80=[_inside(hi, dist["high80"]), _inside(lo, dist["low80"])],
                in80_raw=[_inside(hi, raw["high80"]), _inside(lo, raw["low80"])] if raw else None,
                scale=dist.get("scale"))
        ups = [k for k, _ in s["structure"]["up"]]; dns = [k for k, _ in s["structure"]["down"]]
        tol = 0.005 * s["ref"]
        row["structure"] = dict(
            near_high=min((abs(k - hi) for k in ups), default=None), near_low=min((abs(k - lo) for k in dns), default=None),
            hit_high=bool(ups) and min(abs(k - hi) for k in ups) <= tol, hit_low=bool(dns) and min(abs(k - lo) for k in dns) <= tol)
        score["stages"][stage] = row
    day["score"] = score
    save_day(d, day)
    return score


def cumulative_scores() -> dict:
    """저장된 모든 날짜의 채점을 모아 누적한다 — 「R̂ 채택의 손익」을 날마다 추적하는 자리."""
    agg: dict = {}
    for p in sorted(DIR.glob("????-??-??.json")):
        try:
            sc = json.loads(p.read_text(encoding="utf-8")).get("score")
        except Exception:
            continue
        if not sc:
            continue
        for stage, row in sc["stages"].items():
            a = agg.setdefault(stage, dict(n=0, abs_err=0.0, in50=0, in80=0, in80_raw=0, n_raw=0, s_hit=0, s_n=0))
            dist = row.get("distance")
            if dist:
                a["n"] += 2; a["abs_err"] += abs(dist["err_high"]) + abs(dist["err_low"])
                a["in50"] += sum(dist["in50"]); a["in80"] += sum(dist["in80"])
                if dist.get("in80_raw"):
                    a["n_raw"] += 2; a["in80_raw"] += sum(dist["in80_raw"])
            st = row.get("structure")
            if st and st["near_high"] is not None and st["near_low"] is not None:
                a["s_n"] += 2; a["s_hit"] += int(st["hit_high"]) + int(st["hit_low"])
    return agg


def score_markdown(d: date) -> str:
    """일일점검 산출물에 붙일 절 — 그날 채점 + 누적. 파일이 없어도 절은 나온다(없다는 사실을 적는다)."""
    sc = score_day(d)
    lines = ["## 18. 당일 맥점 예측 채점 — 08:50 / 09:30 × 거리모델 / 구조모델", ""]
    if sc is None:
        lines.append("> 채점 없음 — 오늘 파일이 없거나 단계가 산출되지 않았다(`data/premarket_levels/`).")
        return "\n".join(lines) + "\n"
    lines.append(f"실제 고 **{sc['high']:.2f}** · 저 **{sc['low']:.2f}** (봉 {sc['bars']}개)")
    lines.append("")
    lines.append("| 단계 | 거리 고점 오차 | 거리 저점 오차 | 50% 안착 고/저 | 80% 안착 고/저 | 80% 원구간 고/저 | R̂ | 구조 최근접 고/저(pt) | 구조 ±0.5% 고/저 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")

    def mark(pair) -> str:
        return "/".join("○" if x else "×" for x in pair) if pair else "-"

    for stage, row in sc["stages"].items():
        label = f"{stage[:2]}:{stage[2:]}"
        dist = row.get("distance"); st = row["structure"]
        if dist:
            cell = (f"| {label} | {dist['err_high']:+.1f} | {dist['err_low']:+.1f} | {mark(dist['in50'])} | {mark(dist['in80'])} | "
                    f"{mark(dist.get('in80_raw'))} | {dist['scale']:.2f} | " if dist.get("scale") else
                    f"| {label} | {dist['err_high']:+.1f} | {dist['err_low']:+.1f} | {mark(dist['in50'])} | {mark(dist['in80'])} | - | - | ")
        else:
            cell = f"| {label} | 미산출 | 미산출 | - | - | - | - | "
        if st["near_high"] is not None and st["near_low"] is not None:
            cell += f"{st['near_high']:.1f} / {st['near_low']:.1f} | {mark([st['hit_high'], st['hit_low']])} |"
        else:
            cell += "- | - |"
        lines.append(cell)
    agg = cumulative_scores()
    parts = []
    for k, a in sorted(agg.items()):
        if not a["n"]:
            continue
        raw_txt = f"(원구간 {a['in80_raw'] / a['n_raw']:.0%})" if a["n_raw"] else ""
        s_txt = f" · 구조 ±0.5% {a['s_hit'] / a['s_n']:.0%}" if a["s_n"] else ""
        parts.append(f"{k[:2]}:{k[2:]} 거리 MAE {a['abs_err'] / a['n']:.1f}pt · 50% {a['in50'] / a['n']:.0%} · 80% {a['in80'] / a['n']:.0%}{raw_txt}{s_txt} [n={a['n'] // 2}일]")
    lines.append("")
    lines.append("누적: " + (" · ".join(parts) if parts else "없음"))
    lines.append("")
    lines.append("> 80% 안착은 R̂ 스케일 구간, 「원구간」은 스케일 전 — 두 열의 차이가 R̂ 채택의 손익이다. "
                 "검증 기대치(144세션): 08:50 83% vs 78%, 09:30 81% vs 74%. 구조 ±0.5%의 검증 기대치는 무작위와 같은 ~47%.")
    return "\n".join(lines) + "\n"
