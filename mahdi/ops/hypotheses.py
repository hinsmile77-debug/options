"""가설 파일 로드 + 예측 대조 (§5-1 "예측 → 실측 검정"의 자동화).

2026-08-01(운영점검보고서 2026-07-31 §5-1). 07-31 §2-0의 "가설 8개 검정" 표가 가능했던 유일한
이유는 **07-30 세션이 NEXT_TODO에 검증 항목을 남겨뒀기 때문**이다 — 지금은 그게 사람의 습관에만
의존한다. `docs/동작점검/hypotheses.yaml`에 예측치를 적어두면 다음 거래일 리포트가 자동 대조한다.

**의도적으로 하지 않는 것 두 가지**:
1. **YAML의 `상태`를 자동으로 고치지 않는다.** 자동 판정이 틀렸을 때 조용히 덮이는 것을 막는다 —
   사람이 보고서를 쓰면서 손으로 확정한다.
2. **해석 못 하는 `expect`를 억지로 판정하지 않는다.** 실측치와 예측 문구를 나란히 보여주고
   판정은 "수기"로 남긴다(억지 자동 판정보다 낫다).
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path
from typing import Any

logger = logging.getLogger("mahdi.ops.hypotheses")

# `expect` 문법 — 이 셋만 해석한다. 더 늘리고 싶어지면 그건 지표를 잘못 고른 신호일 가능성이 높다.
_COMPARISON_RE = re.compile(r"^\s*(<=|>=|<|>|==)\s*([\d.]+)\s*$")
_RANGE_RE = re.compile(r"^\s*([\d.]+)\s*~\s*([\d.]+)\s*$")

VERDICT_CONFIRMED = "확인"
VERDICT_REFUTED = "반증"
VERDICT_MANUAL = "수기 판정"
VERDICT_NO_DATA = "실측 없음"


def load(path: Path) -> list[dict]:
    """
    계산: 가설 YAML을 읽어 리스트로 반환한다.
    실패 조건: 파일이 없으면 빈 리스트(가설을 안 적어둔 날은 그 절이 안 나올 뿐이다).
              파싱 실패는 로그만 남기고 빈 리스트 — 리포트 전체를 막지 않는다.
    """
    if not path.exists():
        return []
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("가설 파일 파싱 실패: %s", path, exc_info=True)
        return []
    return [item for item in (data or []) if isinstance(item, dict)]


def _lookup(metrics: dict | None, db_metrics: dict | None, path: str) -> Any:
    """`"overrun.count"` / `"db.monthly_coverage.coverage_pct"` 둘 다 받는다."""
    from mahdi.ops.report import dig

    if path.startswith("db."):
        return dig(db_metrics or {}, path[3:])
    return dig(metrics or {}, path)


def _verdict(actual: Any, expect: str) -> str:
    if actual is None:
        return VERDICT_NO_DATA
    if not isinstance(actual, (int, float)):
        return VERDICT_MANUAL
    m = _COMPARISON_RE.match(expect)
    if m:
        op, raw = m.group(1), float(m.group(2))
        ok = {
            "<=": actual <= raw, "<": actual < raw,
            ">=": actual >= raw, ">": actual > raw,
            "==": abs(actual - raw) < 1e-9,
        }[op]
        return VERDICT_CONFIRMED if ok else VERDICT_REFUTED
    m = _RANGE_RE.match(expect)
    if m:
        low, high = float(m.group(1)), float(m.group(2))
        return VERDICT_CONFIRMED if low <= actual <= high else VERDICT_REFUTED
    return VERDICT_MANUAL


def evaluate(
    entries: list[dict], target: date, metrics: dict | None, db_metrics: dict | None = None
) -> list[dict]:
    """
    입력: 가설 목록, 대상 날짜, 로그 지표, (선택) DB 지표.
    계산: `검증예정일`이 대상 날짜 **이하**이고 `상태`가 `pending`인 가설만 골라 예측과 실측을
         나란히 낸다 — 예정일이 지났는데 아직 확정 안 된 항목이 계속 보이는 편이, 하루 놓치면
         영영 사라지는 것보다 낫다.
    실패 조건: 항목 형식이 어긋나면 그 항목만 건너뛴다(로그만 남긴다).
    """
    out: list[dict] = []
    for entry in entries:
        try:
            if str(entry.get("상태", "pending")).lower() != "pending":
                continue
            due = entry.get("검증예정일")
            if isinstance(due, str):
                due = date.fromisoformat(due)
            if due is not None and due > target:
                continue
            for prediction in entry.get("예측") or []:
                path = prediction["metric"]
                actual = _lookup(metrics, db_metrics, path)
                expect = str(prediction["expect"])
                out.append(
                    {
                        "id": entry.get("id", "?"),
                        "가설": entry.get("가설", ""),
                        "metric": path,
                        "actual": actual,
                        "expect": expect,
                        "verdict": _verdict(actual, expect),
                    }
                )
        except Exception:
            logger.warning("가설 항목 처리 실패: %s", entry.get("id"), exc_info=True)
    return out
