"""당일 맥점 예측 — **거리 모델**(시가 ± k·ATR)과 **구조 모델**(매물대·갭 변·전일 고저/VWAP·OI 행사가)의
08:50 / 09:30 산출.

2026-09-06 [MW0601]. 근거 문서: `docs/Dev_md/RESEARCH_PREMARKET_EXTREMES_v1.md`(거리 모델 §2·§7),
`RESEARCH_PREMARKET_LEVELS_WFA_v1.md`(구조 모델 §5.5). 검증 스크립트(`scripts/premarket_*_wfa.py`)의
규칙을 **운용용으로 옮긴 것**이며 규칙 자체는 바꾸지 않았다.

## 두 단계

  08:50 — 기준가 = 당일 08:45 시가(첫 봉). 거리 모델 M1: 고점 = 시가 + med(u)·ATR14, 저점 = 시가 − med(d)·ATR14,
          구간 = 훈련 잔차 25/75·10/90 분위. 구조 모델: 후보 중 시가 위·아래 가까운 3개.
  09:30 — 기준가 = 09:30 현재가. 거리 모델 P1: u ~ 1 + 갭 + 전일범위 + u_T + d_T + ret_T (중앙값 회귀),
          예측은 그때까지의 고·저를 하한으로 자른다. 구조 모델: 후보에 오프닝 레인지 고·저를 더해 현재가 기준 3개.

## 검증된 신뢰도 (144세션 워크포워드, 문서 표)

  08:50 거리: MAE 14.2pt · 50% 구간 47% · 80% 구간 78%.  09:30 거리(P1): MAE 11.0pt · 80% 구간 74%(약간 과신).
  구조 모델: 극값 안착·정거장률 모두 **무작위와 구분되지 않는다**(문서 §5.5). 배지에 그 사실을 캡션으로 단다 —
  검증 결과를 화면이 부정하면 안 된다.

## 무엇을 하지 않는가

  신호 계층·진입·청산에 연결하지 않는다. DB에 쓰지 않는다 — 산출은 `data/premarket_levels/<날짜>.json`에
  남긴다(장후 채점의 원료). 이 모듈은 순수 계산이고 DB·파일·시각은 호출측(`scripts/premarket_levels_publish.py`,
  `dashboard/data_source.py`)이 댄다.

## 이력의 출처

  훈련창 60세션이 필요한데 마흐디 DB의 선물 봉은 2026-08-20부터다. 그래서 이력은 **미륵(futures) 1분봉**을
  씨앗으로 하고 마흐디 봉으로 매일 이어 붙인다(`history_cache.json`). 미륵 DB는 장중(08:40~15:40) 읽기
  금지 규정이 있어 **08:40 이전 기동 시에만** 새로 읽고, 그 외에는 캐시를 쓴다.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

import numpy as np

BIN = 0.5                 # 매물대 히스토그램 bin
LOOKBACK = 6              # 구조 후보를 볼 과거 세션 수
PEAK_WINDOW = 2.0         # 매물대 봉우리 판정 창(pt)
PEAK_MIN_MINUTES = 60     # 3-bin 평활 합 최소 분
MERGE_PT = 1.5            # 후보 병합 거리
TRAIN_SESSIONS = 60       # 거리 모델 훈련창
MIN_TRAIN_SESSIONS = 30   # 이보다 적으면 거리 모델을 내지 않는다(지어내지 않는다)
ATR_N = 14
STAGE2_TIME = "09:30"


@dataclass(frozen=True, slots=True)
class Bar:
    t: str      # "HH:MM"
    o: float
    h: float
    l: float
    c: float
    v: int


@dataclass
class SessionSummary:
    """세션 하나의 요약 — 거리 모델 훈련에 필요한 전부. 봉은 최근 LOOKBACK+1세션만 따로 보관한다."""
    d: str
    o: float
    h: float
    l: float
    c: float
    hp_t: float | None = None    # 09:30까지 고가 − 시가 (pt) — 봉을 버리기 전에 요약 시점에 굳힌다
    lp_t: float | None = None    # 시가 − 09:30까지 저가 (pt)
    cp_t: float | None = None    # 09:30 현재가 − 시가 (pt)
    u_t: float | None = None     # hp_t / ATR14 — fill_derived()가 채운다
    d_t: float | None = None     # lp_t / ATR14
    ret_t: float | None = None   # cp_t / ATR14
    atr: float | None = None     # 그 세션 기준 ATR14(전일까지)


# ---------------------------------------------------------------- 요약·ATR

def summarize_session(d: str, bars: list[Bar]) -> SessionSummary:
    s = SessionSummary(d=d, o=bars[0].o, h=max(b.h for b in bars), l=min(b.l for b in bars), c=bars[-1].c)
    early = [b for b in bars if b.t <= STAGE2_TIME]
    if early and bars[0].t <= "09:00":   # 09:00 이후에 시작한 결손 세션은 경로를 남기지 않는다
        s.hp_t = max(b.h for b in early) - s.o; s.lp_t = s.o - min(b.l for b in early); s.cp_t = early[-1].c - s.o
    return s


def path_at(bars: list[Bar], at: str, o: float, atr: float) -> tuple[float, float, float] | None:
    early = [b for b in bars if b.t <= at]
    if not early or atr <= 0:
        return None
    return (max(b.h for b in early) - o) / atr, (o - min(b.l for b in early)) / atr, (early[-1].c - o) / atr


def atr_of(summaries: list[SessionSummary], upto: int, n: int = ATR_N) -> float | None:
    """summaries[upto] 세션의 ATR14 = 그 이전 n세션의 TR 평균."""
    seg = summaries[max(0, upto - n):upto]
    if len(seg) < 5:
        return None
    trs, pc = [], None
    for s in seg:
        trs.append(s.h - s.l if pc is None else max(s.h - s.l, abs(s.h - pc), abs(s.l - pc)))
        pc = s.c
    return sum(trs) / len(trs)


def fill_derived(summaries: list[SessionSummary], bars_by_day: dict[str, list[Bar]] | None = None) -> None:
    """각 요약에 ATR과 ATR 단위 09:30 경로를 채운다. 경로의 원자료(pt)는 요약 시점에 이미 굳어 있다."""
    for i, s in enumerate(summaries):
        s.atr = atr_of(summaries, i)
        if s.atr and s.hp_t is not None:
            s.u_t, s.d_t, s.ret_t = s.hp_t / s.atr, s.lp_t / s.atr, s.cp_t / s.atr
        else:
            s.u_t = s.d_t = s.ret_t = None


# ---------------------------------------------------------------- 거리 모델

def lad_fit(X: np.ndarray, y: np.ndarray, iters: int = 30) -> np.ndarray:
    """최소절대편차(중앙값 회귀) — 반복 가중 최소제곱 근사. 검증 스크립트와 같은 구현."""
    beta = np.linalg.lstsq(X, y, rcond=None)[0]
    for _ in range(iters):
        w = 1.0 / np.maximum(np.abs(y - X @ beta), 1e-3)
        Xw = X * w[:, None]
        beta = np.linalg.lstsq(Xw.T @ X, Xw.T @ y, rcond=None)[0]
    return beta


def _q(a, p) -> float:
    return float(np.quantile(a, p))


def fit_stage1(train: list[SessionSummary]) -> dict | None:
    """M1: u·d 중앙값과 잔차 분위. train은 ATR이 있는 세션만."""
    rows = [s for s in train if s.atr]
    if len(rows) < MIN_TRAIN_SESSIONS:
        return None
    u = [(s.h - s.o) / s.atr for s in rows]
    d = [(s.o - s.l) / s.atr for s in rows]
    mu, md = statistics.median(u), statistics.median(d)
    ru = [x - mu for x in u]; rd = [x - md for x in d]
    return dict(n=len(rows), med_u=mu, med_d=md,
                u50=(_q(ru, .25), _q(ru, .75)), u80=(_q(ru, .1), _q(ru, .9)),
                d50=(_q(rd, .25), _q(rd, .75)), d80=(_q(rd, .1), _q(rd, .9)))


def _x2(gap: float, r1: float, u_t: float, d_t: float, ret_t: float) -> list[float]:
    return [1.0, gap, r1, u_t, d_t, ret_t]


def fit_stage2(summaries: list[SessionSummary], end: int) -> dict | None:
    """P1: 09:30 경로 회귀. summaries[end-60:end] 중 경로·ATR·전일이 있는 세션으로 맞춘다."""
    rows = []
    for i in range(max(1, end - TRAIN_SESSIONS), end):
        s, p = summaries[i], summaries[i - 1]
        if s.atr and s.u_t is not None:
            gap = (s.o - p.c) / s.atr; r1 = (p.h - p.l) / s.atr
            rows.append((_x2(gap, r1, s.u_t, s.d_t, s.ret_t), (s.h - s.o) / s.atr, (s.o - s.l) / s.atr, s.u_t, s.d_t))
    if len(rows) < MIN_TRAIN_SESSIONS:
        return None
    X = np.array([r[0] for r in rows]); u = np.array([r[1] for r in rows]); d = np.array([r[2] for r in rows])
    bu, bd = lad_fit(X, u), lad_fit(X, d)
    ru = [float(ui - max(x @ bu, ut)) for x, ui, ut in zip(X, u, (r[3] for r in rows))]
    rd = [float(di - max(x @ bd, dt)) for x, di, dt in zip(X, d, (r[4] for r in rows))]
    return dict(n=len(rows), bu=[float(b) for b in bu], bd=[float(b) for b in bd],
                u50=(_q(ru, .25), _q(ru, .75)), u80=(_q(ru, .1), _q(ru, .9)),
                d50=(_q(rd, .25), _q(rd, .75)), d80=(_q(rd, .1), _q(rd, .9)))


# ---- R̂ 스케일링 (문서 §7.9 채택, 2026-09-06) ----
# 당일 실현 범위 R = (고−저)/시가 를 log 회귀로 예측해, 구간 폭을 R̂/훈련 중앙 R 배로 늘리거나 줄인다.
# 검증(144세션): 하한 0.85에서 08:45 커버리지 78→83%(폭 +3%), 09:30 74→81%(폭 동일). 하한 1.0은 폭 +9~11%로 과보정.
RHAT_FLOOR = 0.85
RHAT_CAP = 2.0            # 검증에서는 상한이 없었다(닿은 적 없음) — 회귀 폭주에 대한 안전 장치일 뿐


def _x_fixed(s: SessionSummary, prev: SessionSummary, atr5: float | None) -> list[float] | None:
    if not s.atr or s.atr <= 0 or not atr5 or (prev.h - prev.l) <= 0:
        return None
    return [1.0, math.log(s.atr / s.o), math.log(atr5 / s.atr), math.log((prev.h - prev.l) / s.atr),
            (s.o - prev.c) / s.atr, abs(s.o - prev.c) / s.atr, (prev.c - prev.l) / (prev.h - prev.l)]


def fit_rhat(summaries: list[SessionSummary], end: int, with_path: bool) -> dict | None:
    """log R 회귀. with_path=False → 08:45 V1(확정분), True → 09:30 V2(확정분 + 그때까지 범위·|수익|)."""
    rows = []
    for i in range(max(1, end - TRAIN_SESSIONS), end):
        s, p = summaries[i], summaries[i - 1]
        x = _x_fixed(s, p, atr_of(summaries, i, 5))
        if x is None:
            continue
        if with_path:
            if s.u_t is None:
                continue
            x = x + [math.log(max(s.u_t + s.d_t, 1e-3)), abs(s.ret_t)]
        rows.append((x, math.log((s.h - s.l) / s.o)))
    if len(rows) < MIN_TRAIN_SESSIONS:
        return None
    X = np.array([r[0] for r in rows]); y = np.array([r[1] for r in rows])
    return dict(n=len(rows), beta=[float(b) for b in lad_fit(X, y)], med_r=float(np.exp(np.median(y))))


def rhat_scale(params: dict | None, x: list[float] | None) -> float | None:
    if not params or x is None or len(x) != len(params["beta"]):
        return None
    r = math.exp(float(np.array(x) @ np.array(params["beta"])))
    return min(RHAT_CAP, max(RHAT_FLOOR, r / params["med_r"]))


def _band(ref: float, atr: float, center: float, q: tuple[float, float], sign: int, scale: float = 1.0) -> tuple[float, float]:
    lo, hi = ref + sign * (center + q[0] * scale) * atr, ref + sign * (center + q[1] * scale) * atr
    return (min(lo, hi), max(lo, hi))


def _bands(o: float, atr: float, pu: float, pd: float, params: dict, scale: float) -> dict:
    return dict(high50=_band(o, atr, pu, params["u50"], +1, scale), high80=_band(o, atr, pu, params["u80"], +1, scale),
                low50=_band(o, atr, pd, params["d50"], -1, scale), low80=_band(o, atr, pd, params["d80"], -1, scale))


def distance_stage1(params: dict, o: float, atr: float, scale: float | None = None) -> dict:
    """08:50 거리 모델 — 시가·ATR로 고점·저점과 50%/80% 구간. scale이 있으면 R̂ 스케일 구간을 기본으로, 원 구간은 raw에."""
    mu, md = params["med_u"], params["med_d"]
    out = dict(ref=o, high=o + mu * atr, low=o - md * atr, scale=scale)
    out.update(_bands(o, atr, mu, md, params, scale or 1.0))
    out["raw"] = _bands(o, atr, mu, md, params, 1.0) if scale else None
    return out


def distance_stage2(params: dict, o: float, atr: float, gap_pt: float, prev_range_pt: float,
                    path: tuple[float, float, float], scale: float | None = None) -> dict:
    """09:30 거리 모델 — 경로 회귀 + 그때까지 고·저 하한. scale은 distance_stage1과 같은 뜻."""
    u_t, d_t, ret_t = path
    x = np.array(_x2(gap_pt / atr, prev_range_pt / atr, u_t, d_t, ret_t))
    pu = max(float(x @ np.array(params["bu"])), u_t)
    pd = max(float(x @ np.array(params["bd"])), d_t)
    out = dict(ref=o + ret_t * atr, high=o + pu * atr, low=o - pd * atr, scale=scale,
               so_far_high=o + u_t * atr, so_far_low=o - d_t * atr)
    out.update(_bands(o, atr, pu, pd, params, scale or 1.0))
    out["raw"] = _bands(o, atr, pu, pd, params, 1.0) if scale else None
    return out


# ---------------------------------------------------------------- 구조 모델

def _vwap(bars: list[Bar]) -> float | None:
    vol = sum(b.v for b in bars)
    return sum(b.c * b.v for b in bars) / vol if vol else None


def structural_candidates(hist: list[tuple[str, list[Bar]]], oi_strikes: list[tuple[float, int]] = ()) -> dict[int, list[str]]:
    """전일까지 LOOKBACK세션의 봉으로 후보 {레벨: [근거]}. `scripts/premarket_levels.py`와 같은 규칙."""
    hist = hist[-LOOKBACK:]
    if not hist:
        return {}
    cands: dict[int, list[str]] = defaultdict(list)
    dwell: dict[float, int] = defaultdict(int)
    days: dict[float, set] = defaultdict(set)
    for d, bars in hist:
        for b in bars:
            k = round(b.l / BIN) * BIN
            while k <= b.h + 1e-9:
                dwell[k] += 1; days[k].add(d); k = round(k + BIN, 2)
    bins = sorted(dwell)
    sm = {k: dwell.get(round(k - BIN, 2), 0) + dwell[k] + dwell.get(round(k + BIN, 2), 0) for k in bins}
    for k in bins:
        win = [x for x in bins if abs(x - k) <= PEAK_WINDOW]
        if sm[k] >= PEAK_MIN_MINUTES and sm[k] == max(sm[x] for x in win):
            cands[round(k)].append(f"매물대{k}({sm[k]}분/{len(days[k])}세션)")
    for (a, A), (b_, B) in zip(hist, hist[1:]):
        ah, al = max(x.h for x in A), min(x.l for x in A); bh, bl = max(x.h for x in B), min(x.l for x in B)
        if bl > ah:
            cands[round(ah)].append(f"갭하변{a[5:]}고{ah}"); cands[round(bl)].append(f"갭상변{b_[5:]}저{bl}")
        if bh < al:
            cands[round(al)].append(f"갭상변{a[5:]}저{al}"); cands[round(bh)].append(f"갭하변{b_[5:]}고{bh}")
    prev_d, P = hist[-1]
    ph, pl = max(x.h for x in P), min(x.l for x in P)
    cands[round(ph)].append(f"전일고{ph}"); cands[round(pl)].append(f"전일저{pl}")
    if (v := _vwap(P)) is not None:
        cands[round(v)].append(f"전일VWAP{v:.1f}")
    week = [x for _, bars in hist[-4:] for x in bars]
    if (wv := _vwap(week)) is not None:
        cands[round(wv)].append(f"4세션VWAP{wv:.1f}")
    for strike, oi in oi_strikes:
        cands[round(strike)].append(f"OI행사가{strike:g}({oi})")
    merged: dict[int, list[str]] = {}
    for lv in sorted(cands):
        key = next((k for k in merged if abs(k - lv) <= MERGE_PT), None)
        if key is None:
            merged[lv] = list(cands[lv])
        else:
            merged[key].extend(cands[lv])
    return merged


def with_opening_range(merged: dict[int, list[str]], early: list[Bar], at: str) -> dict[int, list[str]]:
    out = {k: list(v) for k, v in merged.items()}
    oh, ol = max(b.h for b in early), min(b.l for b in early)
    for lv, tag in ((round(oh), f"OR고{at}"), (round(ol), f"OR저{at}")):
        key = next((k for k in out if abs(k - lv) <= MERGE_PT), None)
        if key is None:
            out[lv] = [tag]
        else:
            out[key].append(tag)
    return out


def select_nearest(merged: dict[int, list[str]], ref: float, n: int = 3) -> tuple[list[int], list[int]]:
    ups = sorted(k for k in merged if k > ref + 1)[:n]
    dns = sorted((k for k in merged if k < ref - 1), reverse=True)[:n]
    return ups, dns


# ---------------------------------------------------------------- 단계 산출 (배지 페이로드)

def compute_stage(stage: str, today_bars: list[Bar], prev: SessionSummary, atr: float,
                  p1: dict | None, p2: dict | None, candidates: dict[int, list[str]],
                  rhat1: dict | None = None, rhat2: dict | None = None, atr5: float | None = None) -> dict:
    """
    입력: stage "0850"|"0930", 당일 봉(08:45부터 산출 시각까지), 전일 요약, ATR14, 거리 모델 파라미터, 구조 후보,
         (선택) R̂ 회귀 파라미터와 ATR5 — 있으면 구간을 R̂ 스케일로 낸다.
    반환: 배지에 그대로 쓸 dict. 거리 모델은 파라미터가 없으면 None(지어내지 않는다).
    """
    o = today_bars[0].o
    today = SessionSummary(d="", o=o, h=o, l=o, c=o, atr=atr)
    xf = _x_fixed(today, prev, atr5)
    if stage == "0850":
        ref = o
        dist = distance_stage1(p1, o, atr, rhat_scale(rhat1, xf)) if p1 else None
        merged = candidates
    else:
        early = [b for b in today_bars if b.t <= STAGE2_TIME]
        if not early:
            raise ValueError("09:30까지의 봉이 없다")
        path = path_at(early, STAGE2_TIME, o, atr)
        ref = early[-1].c
        x2 = (xf + [math.log(max(path[0] + path[1], 1e-3)), abs(path[2])]) if xf else None
        dist = distance_stage2(p2, o, atr, o - prev.c, prev.h - prev.l, path, rhat_scale(rhat2, x2)) if p2 else None
        merged = with_opening_range(candidates, early, STAGE2_TIME)
    ups, dns = select_nearest(merged, ref)
    return dict(stage=stage, ref=ref, open=o, atr=atr, distance=dist,
                structure=dict(up=[(k, merged[k]) for k in ups], down=[(k, merged[k]) for k in dns]))


# ---------------------------------------------------------------- 이력 캐시 (JSON)

@dataclass
class History:
    summaries: list[SessionSummary] = field(default_factory=list)
    bars: dict[str, list[Bar]] = field(default_factory=dict)   # 최근 LOOKBACK+1세션의 봉만
    source: dict[str, str] = field(default_factory=dict)      # 날짜 → "mireuk"|"mahdi"

    def to_json(self) -> dict:
        return dict(summaries=[asdict(s) for s in self.summaries],
                    bars={d: [asdict(b) for b in bs] for d, bs in self.bars.items()}, source=self.source)

    @classmethod
    def from_json(cls, j: dict) -> "History":
        h = cls()
        h.summaries = [SessionSummary(**s) for s in j.get("summaries", [])]
        h.bars = {d: [Bar(**b) for b in bs] for d, bs in j.get("bars", {}).items()}
        h.source = dict(j.get("source", {}))
        return h

    def upsert(self, d: str, bars: list[Bar], source: str) -> None:
        """세션 하나를 넣거나 갱신한다. 마흐디 봉이 미륵 봉을 덮는다(마흐디가 정본)."""
        if not bars:
            return
        if d in self.source and self.source[d] == "mahdi" and source != "mahdi":
            return
        s = summarize_session(d, bars)
        self.summaries = [x for x in self.summaries if x.d != d] + [s]
        self.summaries.sort(key=lambda x: x.d)
        self.bars[d] = bars
        self.source[d] = source
        keep = {x.d for x in self.summaries[-(LOOKBACK + 1):]}
        self.bars = {k: v for k, v in self.bars.items() if k in keep}

    def finalize(self) -> None:
        fill_derived(self.summaries, self.bars)


def load_history(path: Path) -> History:
    if not path.exists():
        return History()
    return History.from_json(json.loads(path.read_text(encoding="utf-8")))


def save_history(path: Path, h: History) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(h.to_json(), ensure_ascii=False), encoding="utf-8", newline="\n")


def is_quarterly_expiry_window(d: date, hist_days: list[str]) -> bool:
    """이력 창(LOOKBACK)에 분기 만기(3·6·9·12월 둘째 목요일)가 들면 구조 후보는 롤 오염 — 표시는 하되 경고한다."""
    def second_thursday(y: int, m: int) -> date:
        first = date(y, m, 1); off = (3 - first.weekday()) % 7
        return date(y, m, 1 + off + 7)
    exp = {second_thursday(d.year, m).isoformat() for m in (3, 6, 9, 12)} | {second_thursday(d.year - 1, 12).isoformat()}
    return any(x in exp for x in hist_days[-LOOKBACK:]) or d.isoformat() in exp
