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

# 2026-08-05(고도화#2 / 규약 E) — 예측 지표의 **역할**.
#
# ## 왜 역할이 필요한가
#
# 08-04 `p4`는 *"ATM 히스테리시스로 롤링 왕복이 사라진다"* 고 주장하면서 등록 지표가
# `chain_age_seconds_max`와 `log_volume.human_lines`였다. 둘 다 **왕복을 재지 않는다.**
# 그래서 왕복률이 36.1% → 47.5%로 **나빠졌는데도** §0은 "확인"을 냈다. 자기 주장을 검정하지
# 않는 지표로 받은 합격이다.
#
# 그리고 08-04 Fix#8은 **의도적으로 데이터를 버리는** fix였는데(예산 초과 시 레그 포기)
# 버린 양을 세는 지표가 없었다. 밀림 0건이라는 훌륭한 숫자 뒤에서 먼슬리 북이 38% 확률로
# 얇아지고 4분이 통째로 사라진 것을 08-05에야 알았다.
#
# ## 규약
#
#   주장  이 가설이 **실제로 주장하는 것**을 재는 지표. **없으면 판정 불가다.**
#   대가  그 fix가 **무엇을 포기했는지**를 재는 지표. 항목에 `대가:` 문구가 있으면 필수다.
#   참고  나머지(부수 확인용). 역할을 안 적으면 참고로 본다.
#
# 주장 지표는 *얻은 것*, 대가 지표는 *잃은 것*이다. 규약 A/B/C/D가 "같은 것을 두 번 쓰지
# 마라"의 변주라면, E는 **"한쪽만 재지 마라"** 다.
ROLE_CLAIM = "주장"
ROLE_COST = "대가"
ROLE_REFERENCE = "참고"

VERDICT_UNJUDGEABLE = "판정 불가"


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
         영영 사라지는 것보다 낫다. 예정일이 **지난** 항목에는 `overdue=True`를 달아 리포트가
         표 위로 따로 띄운다(2026-08-03 §5-4).
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
            predictions = entry.get("예측") or []
            roles = [str(p.get("역할", ROLE_REFERENCE)) for p in predictions]
            # 2026-08-05 고도화#2 — 주장 지표가 없으면 **그 가설은 판정할 수 없다.**
            # 실측/예측은 사실이므로 그대로 두고 **판정만** 무효화한다. 여기서 "확인"을
            # 그대로 두면 08-04 p4의 오독이 그대로 재현된다.
            claim_missing = ROLE_CLAIM not in roles
            # 규약 E — `대가:` 문구로 트레이드오프를 선언해 놓고 그것을 재는 지표가 없으면
            # "무엇을 포기했는지 모르는 채 개선을 주장하는" 상태다.
            cost_missing = bool(entry.get("대가")) and ROLE_COST not in roles
            for prediction, role in zip(predictions, roles):
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
                        "역할": role,
                        "claim_missing": claim_missing,
                        "cost_missing": cost_missing,
                        "대가": entry.get("대가"),
                        "verdict": (
                            VERDICT_UNJUDGEABLE if claim_missing else _verdict(actual, expect)
                        ),
                        # 2026-08-03 §5-4: 예정일이 **지난** 채로 아직 pending인 항목.
                        # 규약상 `상태`는 사람이 손으로 확정해야 하는데, 확정 안 된 것이 표에
                        # 섞여 들어가면 놓치기 쉽고 그러면 규약 자체가 무력해진다.
                        "overdue": due is not None and due < target,
                        "검증예정일": due.isoformat() if due is not None else None,
                    }
                )
        except Exception:
            logger.warning("가설 항목 처리 실패: %s", entry.get("id"), exc_info=True)
    return out
