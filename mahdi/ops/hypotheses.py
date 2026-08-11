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

# 2026-08-07(§4 / Fix#5) — **규약 F: 주장 지표는 절대 건수로 세우지 않는다.**
#
# 08-07 하루에 같은 형태의 예측 오류가 **세 번** 났다:
#
#   p4  `chain_leg_median == 10`            5분 스냅샷 창을 계산에 안 넣음   → 실측 12
#   e1  `atm_roll_dropped_subs <= 4`        스팟 이동거리를 안 넣음          → 실측 97~469
#   p2  `expiry_liquidity_1m.rows >= 100`   북 수(3→2)를 안 넣음             → 실측 85
#
# 셋 다 **fix는 맞았는데 예측이 틀렸다**. 그리고 셋 다 자동 판정에서 「반증」으로 찍혔다 —
# 그대로 두면 멀쩡한 fix를 되돌리게 된다(08-04 p4의 `untested` 구분과 같은 위험이다).
#
# 공통 원인은 부주의가 아니라 **형태**다: 건수는 관측 길이·북 수·시장 이동거리 같은 구조
# 변수에 비례하는데, 예측을 쓰는 순간에는 그 변수들이 상수처럼 느껴진다. 비율·중앙값·
# 정규화 지표는 그 변수들이 분모에서 약분되므로 같은 실수를 할 수 없다.
#
# 그래서 **주장 역할에 한해** 건수 계열 지표에 부등식을 걸면 경고한다. 대가·참고는 막지 않는다
# (대가는 "얼마나 늘었나"가 본질이라 건수가 맞는 경우가 많다). 정말 건수로 재야 하면
# `수기 판정`으로 내리면 된다 — 그건 사람이 그날의 구조 변수를 보고 읽겠다는 선언이다.
_COUNT_METRIC_SUFFIXES = ("_minutes", "_count", "_calls", ".rows", "_rows", "_legs", "_today")
# **접미사로만** 판정한다 — 부분 문자열로 보면 `_minutes`가 `_min`에 걸려 정규화로 오분류된다.
_NORMALIZED_SUFFIXES = ("_pct", "_ratio", "_median", "_mean", "_avg", "_max", "_min")
_NORMALIZED_SUBSTRINGS = ("per_", "_per_")


def is_count_shaped(metric: str) -> bool:
    """
    입력: 지표 경로.
    계산: **절대 건수 계열**인가 — 정규화 힌트가 하나라도 있으면 아니다.
    해석: 근거는 위 규약 F 주석. 완벽한 분류가 목적이 아니라, 예측을 쓰는 사람이 "이 값이
         무엇에 비례하는가"를 한 번 더 생각하게 만드는 것이 목적이다.
    실패 조건: 없다.
    """
    lowered = metric.lower()
    if lowered.endswith(_NORMALIZED_SUFFIXES) or any(x in lowered for x in _NORMALIZED_SUBSTRINGS):
        return False
    return lowered.endswith(_COUNT_METRIC_SUFFIXES)


def violates_normalized_claim_rule(role: str, metric: str, expect: str) -> bool:
    """
    입력: 예측의 역할·지표 경로·기대식.
    계산: 규약 F 위반인가 — **주장** 역할 + 건수 계열 지표 + 자동 판정 부등식(`<=`/`>=`/범위).
    해석: **구조 변수에 비례하지 않는 경계는 통과시킨다.**
         - `== 0` / `>= 0`: 관측 길이·북 수·이동거리가 무엇이든 판정이 안 바뀐다.
           08-07 Fix#1의 `chain_leg_over_design_minutes == 0`(불변식)과
           08-06 Fix#3의 `underlying_spot_1m.rows >= 0`(경로 생존 확인)이 그 형태다.
         - `수기 판정`: 사람이 그날의 구조 변수를 보고 읽겠다는 선언이다.
    실패 조건: 없다.
    """
    if str(role) != ROLE_CLAIM or not is_count_shaped(metric):
        return False
    text = str(expect).strip()
    # **평가기가 자동 판정하는 형태만** 본다. `수기 판정`은 애초에 「반증」을 못 만들므로
    # 이 규약의 대상이 아니다 — 그리고 그 문구 안의 `~`(예: "07:31~09:00")를 범위로 오인하면
    # 규칙이 자기 목적을 넘어 문서를 검열하게 된다(도입 당일 실제로 그랬다).
    comparison = _COMPARISON_RE.match(text)
    if comparison:
        op, raw = comparison.group(1), float(comparison.group(2))
        # `== 0` / `>= 0`은 구조 변수가 무엇이든 판정이 안 바뀐다 — 불변식·경로 생존 확인이다.
        return not (op == "==" or (op in (">=", ">") and raw == 0))
    return bool(_RANGE_RE.match(text))


# 2026-08-11(§3-2 / Fix#6) — **규약 G: 시장 상태에 의존하는 지표에 무조건부 하한을 걸지 않는다.**
#
# 규약 F와 **같은 병의 다른 얼굴**이다. F는 "건수는 구조 변수에 비례한다"였고, G는
# "어떤 값은 그날 시장이 무엇이었는가에 비례한다"이다. 둘 다 예측을 쓰는 순간에는 그 변수가
# 상수처럼 느껴진다는 점이 같다.
#
# ## 08-11에 무슨 일이 있었는가
#
# 레짐 엔진이 25영업일 만에 켜진 날이었다. 검증 기준으로 두 가지를 걸어 뒀다:
#
#   §14-3 `regime_hmm` 비영 산출 분 > 0
#   `db.decisions.member_count.dead_axis_mean` < 1.02
#
# **둘 다 반증으로 찍혔다.** 그런데 엔진은 완벽히 돌았다 — 09:14에 WARMUP을 벗고, 3종 레짐을
# 방문하고, 전이 2회로 채터링도 없었다. 반증의 정체는 이것이다:
#
#   `signal_layer._TREND_DIRECTION`은 TREND_UP/DOWN_STRONG **두 상태에만** 방향을 준다.
#   그날 방문한 셋(VOL_COMPRESSION 372분 / RANGE_BALANCED 29 / RANGE_BREAK_PREP 9)은
#   v6 §7 정의상 전부 방향이 없다. 그래서 `regime_hmm`은 419분 전량 0점이었다 — **설계대로.**
#
# 즉 그 기준은 *"엔진이 도는가"* 와 *"엔진이 추세를 봤는가"* 를 섞었다. 시장이 조용한 날마다
# 멀쩡한 엔진이 반증으로 찍히고, 그 반증을 믿으면 **고칠 필요 없는 것을 고치게 된다.**
#
# ## 무엇을 막는가
#
# **주장 역할** + 시장 상태 의존 지표 + **하한**(`>=`/`>`, 0 초과) 조합만 경고한다.
#   - 상한(`<=`/`<`)은 막지 않는다 — "이것보다 많으면 이상"은 시장이 조용해도 성립한다
#     (채터링 감시 `전이 <= 20회`가 그 형태다).
#   - `== 0` 불변식과 `>= 0` 경로 생존 확인은 F와 같은 이유로 통과시킨다.
#   - `수기 판정`은 사람이 그날 시장을 보고 읽겠다는 선언이므로 대상이 아니다.
#
# 정말 하한을 걸어야 하면 **조건을 지표로 만들어 함께 걸면 된다** — 08-11에 실제로 그렇게 했다
# (`db.regime.trend_minutes`를 전제 조건으로 신설하고, 그 값이 0이면 「판정 불가」).
_MARKET_STATE_DEPENDENT_METRICS = (
    # 그날 시장이 추세였는가에 비례한다 — 위 08-11 사고의 당사자들.
    "member_count.dead_axis_mean",
    "member_count.effective_mean",
    "regime.trend_minutes",
    # 그날 딜러 감마 지형이 단조였는가에 비례한다(08-04 §2-3: 전 구간 단조면 flip이 없다).
    "gamma_flip_pct",
    "signal_reach.gamma_flip",
    # 그날 옵션에 체결이 있었는가에 비례한다(08-07: 얇은 날은 봉 자체가 안 생긴다).
    "market_raw_1m.rows",
)


def is_market_state_dependent(metric: str) -> bool:
    """
    입력: 지표 경로.
    계산: `_MARKET_STATE_DEPENDENT_METRICS`의 어느 항목이 경로에 포함되는가.
    해석: 근거는 위 규약 G 주석. **완전한 목록이 목적이 아니다** — 아는 것부터 등록하고,
         새로 데이는 것이 있으면 그때 추가한다(규약 F의 `_COUNT_METRIC_SUFFIXES`와 같은 방식).
    실패 조건: 없다.
    """
    lowered = metric.lower()
    return any(token.lower() in lowered for token in _MARKET_STATE_DEPENDENT_METRICS)


def violates_market_state_rule(role: str, metric: str, expect: str) -> bool:
    """
    입력: 예측의 역할·지표 경로·기대식.
    계산: 규약 G 위반인가 — **주장** 역할 + 시장 상태 의존 지표 + **0이 아닌 하한**.
    해석: 상세 근거는 위 규약 G 주석. 상한은 통과시킨다(조용한 날에도 성립하는 방향이다).
    실패 조건: 없다.
    """
    if str(role) != ROLE_CLAIM or not is_market_state_dependent(metric):
        return False
    comparison = _COMPARISON_RE.match(str(expect).strip())
    if not comparison:
        return False
    op, raw = comparison.group(1), float(comparison.group(2))
    # **규약 F와 여기서 갈린다.** F는 `> 0`을 "경로 생존 확인"으로 보아 통과시키는데, G에서는
    # `> 0`이야말로 08-11 사고의 원형이다 — *"`regime_hmm` 비영 산출 분 > 0"* 이 그 문장이었고,
    # 그것은 경로가 살았는지가 아니라 **시장이 추세였는지**를 물었다.
    # `>= 0`만 통과시킨다: 항상 참이라 거짓 반증을 만들 수 없다.
    return op == ">" or (op == ">=" and raw > 0)

# 2026-08-11(§3-6 / Fix#8) — 확정 대기가 이 일수를 넘으면 **자동으로 강등**한다.
#
# ## 왜 자동으로 닫는가 — 규약과 부딪히는 것처럼 보이지만 아니다
#
# 이 저장소의 규약은 *"`상태`는 자동으로 안 바뀐다(사람이 확정)"* 이다. 그 규약이 지키려는 것은
# **판정의 품질**이다 — 기계가 `확인`/`반증`을 찍으면 아무도 그 숫자를 안 읽게 된다.
#
# 여기서 하는 것은 판정이 아니라 **포기 선언**이다. 90일이 지나도록 아무도 안 닫은 가설은
# 그 사이에 코드가 여러 번 바뀌었을 것이고, 그때의 실측을 지금 대조해도 귀속이 안 갈린다.
# `inconclusive`는 *"판정하지 못했다"* 이지 *"틀렸다/맞았다"* 가 아니므로 규약을 안 어긴다.
# (08-10에 `2026-08-07-e4`를 사람이 정확히 그렇게 닫았다 — 이건 그 판단의 기계화다.)
#
# ## 왜 90일인가
#
# 08-11 실측으로 확정 대기가 23건이었고 §0이 그 목록을 매일 인쇄한다. 목록이 길어지면
# **진짜 반증이 그 소음에 묻힌다** — 규약 F/G가 막으려는 것과 같은 실패 모드다.
# 90일은 "이 저장소에서 한 분기"이고, 그 사이 영업일이 60일쯤 되므로 어떤 가설도 그보다
# 오래 살아 있을 이유가 없다. 짧게 잡으면 정당한 장기 관측(며칠 쌓아 본다)을 자른다.
STALE_PENDING_DAYS = 90


def _is_stale_pending(due: date | None, target: date) -> bool:
    """반환: 확정 대기가 `STALE_PENDING_DAYS`를 넘겼는가. 예정일이 없으면 판단하지 않는다."""
    return due is not None and (target - due).days > STALE_PENDING_DAYS


VERDICT_UNJUDGEABLE = "판정 불가"

# 2026-08-06 §3-1 / Fix#3 — **경로가 애초에 존재하지 않는다.**
#
# 08-05에 세운 예측 13건 중 **6건이 주장 지표를 하나도 못 받았다.** `db.decisions.…`,
# `db.tables.<이름>.rows`, `db.member_availability.<멤버>.…` — 사람이 자연스럽게 적은 경로들이
# 리포트 구조에 없었고, 전부 조용히 `VERDICT_NO_DATA`("실측 없음")로 표시됐다. 그래서 `p1`의
# **대가 지표가 12배 초과**(ENTER 예측 ≤5에 실측 62건)한 것을 아무도 자동으로 알아채지 못했다.
#
# 같은 날 아침 커밋이 이 위험을 정확히 인지하고 회귀 테스트까지 붙였는데, 그 테스트가
# `db.` 접두사를 **명시적으로 제외**해서(`tests/test_ops_hypotheses.py`) 나머지 절반이 통과했다.
#
# ## 왜 "실측 없음"과 갈라야 하는가
#
# 둘은 **조치가 다르다.**
#   실측 없음  그날 그 값이 안 나왔다 → 내일 다시 본다. 정상일 수 있다.
#   경로 없음  그 지표는 **영원히** 안 나온다 → yaml을 고쳐야 한다. 절대 정상이 아니다.
# 한 이름으로 부르면 후자가 전자의 소음에 묻힌다 — 08-06에 28행 표에서 실제로 그랬다.
#
# ## 판별 방법
#
# **부모 컨테이너가 해석되는가**로 가른다. `db.decisions.reject_reason.strategy_palette:wait_only`
# 에서 잎(`strategy_palette:wait_only`)은 그날 그 사유가 안 나오면 없는 게 맞다 — 데이터 의존이다.
# 그러나 부모(`db.decisions.reject_reason`)는 **구조**라 항상 있어야 한다. 부모가 없으면 오타다.
VERDICT_PATH_DEAD = "경로 없음"


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


def path_exists(metrics: dict | None, db_metrics: dict | None, path: str) -> bool:
    """반환: 이 지표 경로가 **구조적으로** 존재하는가 (2026-08-06 Fix#3).

    잎이 아니라 **부모**를 본다 — 잎은 그날 데이터에 따라 없을 수 있고(그건 「실측 없음」),
    부모가 없으면 오타다(그건 「경로 없음」). 마디가 하나뿐인 경로는 그 자체가 절 이름이므로
    최상위 dict에서 찾는다.

    부모가 리스트(예: `db.tables`)인 경우도 존재로 본다 — `report.dig()`가 자연 키로 색인하므로
    그 리스트에 그 이름의 행이 없는 것은 "그날 그 테이블에 행이 없었다"에 해당한다.
    """
    from mahdi.ops.report import dig

    is_db = path.startswith("db.")
    root = (db_metrics or {}) if is_db else (metrics or {})
    rest = path[3:] if is_db else path
    parent, _, _ = rest.rpartition(".")
    if not parent:
        return rest in root
    return dig(root, parent) is not None


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
                # 2026-08-06 Fix#3 — 경로 자체가 없는 것과 그날 값이 없는 것을 가른다.
                # 지표를 하나도 못 모은 날(DB 다운 등)에는 전 경로가 "없음"이 되므로,
                # 집계가 아예 비어 있으면 이 판별을 하지 않는다(전부 경로 없음으로 뜨면
                # 진짜 오타가 그 소음에 묻힌다 — 이 fix가 고치려던 바로 그 실패다).
                collected = bool(metrics) or bool(db_metrics)
                dead_path = (
                    collected and actual is None
                    and not path_exists(metrics, db_metrics, path)
                )
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
                        "path_dead": dead_path,
                        "대가": entry.get("대가"),
                        "verdict": (
                            VERDICT_UNJUDGEABLE if claim_missing
                            else VERDICT_PATH_DEAD if dead_path
                            else _verdict(actual, expect)
                        ),
                        # 2026-08-03 §5-4: 예정일이 **지난** 채로 아직 pending인 항목.
                        # 규약상 `상태`는 사람이 손으로 확정해야 하는데, 확정 안 된 것이 표에
                        # 섞여 들어가면 놓치기 쉽고 그러면 규약 자체가 무력해진다.
                        "overdue": due is not None and due < target,
                        # 2026-08-11 Fix#8 — **얼마나** 지났는가. 08-11 §0이 「확정 대기 23건」을
                        # 이름만 나열했는데, 4일 지난 것과 넉 달 지난 것이 같은 줄로 보이면
                        # 사람은 목록 전체를 소음으로 취급한다. 정렬 가능한 수를 함께 낸다.
                        "overdue_days": (target - due).days if due is not None and due < target else 0,
                        "stale": _is_stale_pending(due, target),
                        "검증예정일": due.isoformat() if due is not None else None,
                    }
                )
        except Exception:
            logger.warning("가설 항목 처리 실패: %s", entry.get("id"), exc_info=True)
    return out
