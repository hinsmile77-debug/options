"""HMM 재학습 **기준선 박제** (2026-08-06 고도화#3).

## 왜 필요한가

`feature_store` 누적이 08-06 장마감 기준 7,422 / 8,000(92.8%)이고 하루 약 390행이 쌓인다 —
**08-10(월)에 임계에 도달한다.** 그날 `scripts/fit_regime_engine.py`를 돌리면 레짐 엔진이
warmup 폴백에서 학습 모델로 전환된다.

그런데 지금 상태를 아무도 기록해 두지 않으면, 재학습 뒤에 *"좋아졌다"* 를 말할 수 없다.
비교 대상이 없기 때문이다. 08-06 §14-3이 드러낸 현행 상태는 이렇다:

    레짐          23영업일 연속 **상태 2 하나**
    regime_hmm    399분 **전량 중립**(평균 +0.0000, 강세 0 / 약세 0 / 중립 399)
    rv_ratio      중립값 탈출 100% · book_thinning 99%

이 숫자들이 재학습 성공의 판정 기준이다. **재학습 후에 이 파일을 다시 만들지 않는다** —
박제는 한 번 찍고 그대로 두는 것이고, 그래야 비교가 성립한다.

## 이 모듈이 하지 않는 것

**재학습을 실행하지 않는다.** 그것은 `scripts/fit_regime_engine.py`의 몫이고, 여기서는
"돌리기 전의 상태"만 기록한다. 챔피언-도전자 절차(v6)상 새 모델은 도전자로 섀도우
운영해야 하므로, 판단 경로를 바꾸는 결정은 사람이 별도로 내린다.

## 규약

박제 파일은 **덮어쓰지 않는다**(`capture_to_file`이 거부한다). 재학습 후 숫자를 보고 기준선을
고치는 것은 `hypotheses.yaml`의 예측을 실측 뒤에 고치는 것과 같은 종류의 자기기만이다 —
README의 "소급 적용하지 않는다"와 같은 뿌리다.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import date
from pathlib import Path

from mahdi.data.db import ConnectionLike
from mahdi.engines.regime_pipeline import FEATURE_VERSION
from mahdi.ops.db_metrics import HMM_MIN_SAMPLES, _fetchall, _fetchone

logger = logging.getLogger("mahdi.ops.regime_baseline")


def eta_business_days(total_rows: int, distinct_days: int, target_rows: int = HMM_MIN_SAMPLES) -> int:
    """반환: 임계까지 남은 영업일 수(**올림**). 이미 도달했으면 0.

    올림인 이유: 부분 영업일에는 임계에 도달하지 않는다(2026-08-06 Fix#6). 이 함수를 여기 두는
    것은 COCKPIT 배지와 **같은 계산을 공유하기 위해서**다 — 배지와 박제가 다른 날짜를 내면
    어느 쪽을 믿을지 알 수 없다(README 규약).
    """
    if total_rows >= target_rows:
        return 0
    if not distinct_days:
        return 0
    per_day = total_rows / distinct_days
    if per_day <= 0:
        return 0
    return math.ceil((target_rows - total_rows) / per_day)


def capture(conn: ConnectionLike, target: date, underlying: str = "KOSPI200") -> dict:
    """
    입력: DB 커넥션, 박제 기준일, 기초자산.
    계산: 재학습 **전** 상태를 한 dict로 모은다 — 피처 누적/도달 예정, 레짐 상태 분포,
         `regime_hmm` 멤버가 실제로 무슨 점수를 냈는지.
    해석: 세 축이 전부 필요하다. 피처 누적만 보면 "언제 돌릴 수 있나"에만 답하고,
         레짐 분포만 보면 "상태가 갈렸나"에만 답한다. **판단이 달라졌는가**는 `regime_hmm`이
         비영 점수를 내기 시작했는지로만 알 수 있다(08-06 §14-3이 그 표를 처음 냈다).
    실패 조건: 축마다 독립적으로 시도하고, 실패한 축은 키에 `available: False`를 남긴다 —
              하나가 죽어도 나머지는 박제된다(`db_metrics.collect()`와 같은 원칙).
    """
    return {
        "captured_on": target.isoformat(),
        "underlying": underlying,
        "feature_version": FEATURE_VERSION,
        "hmm_threshold": HMM_MIN_SAMPLES,
        "feature_store": _feature_store(conn, underlying),
        "regime_states": _regime_states(conn),
        "regime_hmm_scores": _regime_hmm_scores(conn, target),
    }


def _feature_store(conn: ConnectionLike, underlying: str) -> dict:
    try:
        row = _fetchone(
            conn,
            "SELECT count(*), count(DISTINCT timestamp::date) FROM feature_store "
            "WHERE symbol=%s AND feature_version=%s",
            (underlying, FEATURE_VERSION),
        )
    except Exception:
        conn.rollback()
        logger.warning("기준선: feature_store 조회 실패", exc_info=True)
        return {"available": False}
    total, days = (int(row[0]), int(row[1])) if row else (0, 0)
    return {
        "available": True,
        "total_rows": total,
        "distinct_days": days,
        "rows_per_day": round(total / days, 1) if days else None,
        "progress_pct": round(total / HMM_MIN_SAMPLES * 100, 1),
        "eta_business_days": eta_business_days(total, days),
    }


def _regime_states(conn: ConnectionLike) -> dict:
    """레짐이 **몇 개 상태를 실제로 방문했는가.** 재학습의 첫 성공 기준이다."""
    try:
        rows = _fetchall(
            conn,
            "SELECT regime, count(*), count(DISTINCT timestamp::date) "
            "FROM regime_state GROUP BY 1 ORDER BY 2 DESC",
        )
    except Exception:
        conn.rollback()
        logger.warning("기준선: regime_state 조회 실패", exc_info=True)
        return {"available": False}
    states = [
        {"regime": int(r), "rows": int(n), "days": int(d)} for r, n, d in rows
    ]
    return {
        "available": True,
        "distinct_states": len(states),
        "states": states,
    }


def _regime_hmm_scores(conn: ConnectionLike, target: date) -> dict:
    """`regime_hmm` 멤버가 낸 점수의 분포 — 08-06에 399분 전량 0이었다.

    **이 축이 이 박제의 핵심이다.** 레짐 상태가 여러 개로 갈려도 그 멤버가 계속 0을 내면
    앙상블은 여전히 실질 3멤버이고, 재학습은 판단을 바꾸지 못한 것이다.
    """
    try:
        row = _fetchone(
            conn,
            "SELECT count(*),"
            "       avg((risk_gate_state->'member_scores'->>'regime_hmm')::numeric),"
            "       count(*) FILTER (WHERE (risk_gate_state->'member_scores'->>'regime_hmm')::numeric > 0),"
            "       count(*) FILTER (WHERE (risk_gate_state->'member_scores'->>'regime_hmm')::numeric < 0),"
            "       count(*) FILTER (WHERE (risk_gate_state->'member_scores'->>'regime_hmm')::numeric = 0)"
            " FROM signal_decisions"
            " WHERE timestamp::date=%s AND risk_gate_state->'member_scores' ? 'regime_hmm'",
            (target,),
        )
    except Exception:
        conn.rollback()
        logger.warning("기준선: regime_hmm 점수 조회 실패", exc_info=True)
        return {"available": False}
    if not row or not row[0]:
        return {"available": False, "reason": "그날 regime_hmm 점수 기록 없음"}
    scored = int(row[0])
    return {
        "available": True,
        "scored_minutes": scored,
        "mean": round(float(row[1]), 4) if row[1] is not None else None,
        "bullish_minutes": int(row[2]),
        "bearish_minutes": int(row[3]),
        "neutral_minutes": int(row[4]),
        # 이 값이 100%면 그 멤버는 살아 있으면서 아무 말도 안 한 것이다.
        "neutral_pct": round(int(row[4]) / scored * 100, 1),
    }


def render(baseline: dict) -> str:
    """박제를 사람이 읽을 마크다운으로. **JSON과 같은 파일에 나란히 둔다**(기계/사람 둘 다 읽는다)."""
    fs = baseline.get("feature_store") or {}
    rs = baseline.get("regime_states") or {}
    hs = baseline.get("regime_hmm_scores") or {}
    lines = [
        f"# HMM 재학습 기준선 — {baseline.get('captured_on')} 박제",
        "",
        "> 2026-08-06 고도화#3. **재학습 후 이 파일을 다시 만들지 않는다** — 박제는 한 번 찍고",
        "> 그대로 둬야 비교가 성립한다. 재학습 성공의 판정은 아래 세 축으로 한다.",
        "",
        "## 1. 피처 누적 — 언제 돌릴 수 있는가",
        "",
        f"- 누적 **{fs.get('total_rows', 0):,}행** / 임계 {baseline.get('hmm_threshold'):,} "
        f"({fs.get('progress_pct')}%) · {fs.get('distinct_days')}영업일 · "
        f"하루 평균 {fs.get('rows_per_day')}행",
        f"- 임계 도달까지 **{fs.get('eta_business_days')}영업일**",
        "",
        "## 2. 레짐 상태 — 갈렸는가",
        "",
        f"- 방문 상태 **{rs.get('distinct_states')}종**",
    ]
    for state in rs.get("states") or []:
        lines.append(f"  - 상태 {state['regime']}: {state['rows']:,}행 / {state['days']}영업일")
    lines += [
        "",
        "> **1~2종이면 레짐이 사실상 고정 출력이다.** 재학습의 첫 성공 기준은 이 값이 늘어나는 것이고,",
        "> 안 늘면 데이터가 아니라 모델 구성을 의심해야 한다(`fit_regime_engine.py`의 잠재상태 경고).",
        "",
        "## 3. `regime_hmm` 멤버 점수 — 판단이 달라졌는가",
        "",
    ]
    if hs.get("available"):
        lines += [
            f"- 산출 **{hs.get('scored_minutes')}분** · 평균 **{hs.get('mean'):+}** · "
            f"강세 {hs.get('bullish_minutes')} / 약세 {hs.get('bearish_minutes')} / "
            f"중립 **{hs.get('neutral_minutes')}**({hs.get('neutral_pct')}%)",
            "",
            "> **이 축이 이 박제의 핵심이다.** 상태가 여러 개로 갈려도 이 멤버가 계속 0을 내면",
            "> 앙상블은 여전히 실질 3멤버이고(고도화#2의 `effective_member_count`), 재학습은 판단을",
            "> 바꾸지 못한 것이다.",
        ]
    else:
        lines.append(f"- 집계 불가 — {hs.get('reason', '기록 없음')}")
    lines += [
        "",
        "## 재학습 절차(v6 챔피언-도전자)",
        "",
        "1. `uv run python scripts/fit_regime_engine.py --dry-run` — 기계적 오류 먼저 걷는다.",
        "2. 이 박제와 `hypotheses.yaml`의 예측치를 대조할 준비가 됐는지 확인한다.",
        "3. 실제 학습은 **새 모델을 도전자로** 두고 섀도우 운영한다 — 저장하는 순간",
        "   `RegimeStateMachine`이 그 파일을 보고 곧바로 `predict()`로 전환하므로,",
        "   판단 경로를 바꾸는 결정은 사람이 별도로 내린다.",
        "",
    ]
    return "\n".join(lines)


def capture_to_file(conn: ConnectionLike, target: date, path: Path, underlying: str = "KOSPI200") -> Path:
    """박제를 JSON + 마크다운으로 남긴다. **이미 있으면 거부한다.**

    덮어쓰기를 막는 이유: 재학습 후 숫자를 보고 기준선을 고치는 것은 예측을 실측 뒤에 고치는
    것과 같은 자기기만이다(README "소급 적용하지 않는다"와 같은 뿌리).
    """
    baseline = capture(conn, target, underlying)
    json_path = path.with_suffix(".json")
    md_path = path.with_suffix(".md")
    for existing in (json_path, md_path):
        if existing.exists():
            raise FileExistsError(
                f"기준선이 이미 있다: {existing} — 박제는 덮어쓰지 않는다(다른 경로를 쓰거나 "
                "정말 다시 찍어야 하는 이유를 먼저 적을 것)"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(baseline, ensure_ascii=False, indent=1), encoding="utf-8")
    md_path.write_text(render(baseline), encoding="utf-8")
    return md_path
