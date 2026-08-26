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


# ===== 2026-08-14 §5 / 고도화 4 — **「기제가 달랐다」는 확인도 반증도 아니다** =====
#
# 08-14 장중 점검이 이렇게 외삽했다: *"시간당 +5.6초가 유지되면 13시 ≈53초, 14시 ≈58초로
# 수집 예산 50초를 상시 초과한다. 그때부터 매분 레그가 잘리고, 컷 대상이 위클리를 소진하면
# 먼슬리로 넘어간다."*
#
# 그날 저녁 실측은 이랬다: 13시 **50.5초** / 14시 **49.9초**. 소요는 예산 천장에 눌려 **안 늘었고**,
# 대신 **적재가 0이 됐다**(84분). 컷 대상이 먼슬리로 넘어간 것은 맞았다(regular 컷 13시 15회 →
# 14시 94회). 우선순위 불변식이 시험받으리라는 것은 틀렸다 — 전멸한 분에는 위클리도 안 남아
# **정의상 위반이 성립할 수 없었다.**
#
# 이것을 `확인`으로 닫으면 「예산 초과가 는다」는 틀린 기제가 살아남고, `반증`으로 닫으면
# 「오후에 수집이 무너진다」는 맞은 방향이 죽는다. **둘 다 그날 배운 것을 지운다.**
#
# 오늘 최대 수확은 «예측이 틀린 방식» 그 자체였다 — 열화는 연속량이 아니라 **임계 현상**이고,
# 연속 지표는 절벽 직전까지 완만하게 오르다 **절벽에서 오히려 좋아진다**(14시 소요가 13시보다
# 낮았다). 그 교훈은 확인/반증 어느 칸에도 안 들어간다.
#
# ## 쓰는 법
#
# `상태: mechanism_differed`로 닫고, **판정근거에 두 기제를 나란히 적는다** — 예측한 기제와
# 실제 기제. 하나만 적으면 다음 사람이 「그래서 맞았다는 건가 틀렸다는 건가」를 다시 묻는다.
#
# ## 남용 방지
#
# 이 상태는 **방향이 맞았을 때만** 쓴다. 방향까지 틀렸으면 그냥 `refuted`다. 아무 반증에나
# 「기제가 달랐다」를 붙이면 이 저장소에 반증이 한 건도 없게 되고, 그 순간 예측 규약 전체가
# 장식이 된다 — 규약 F/G/H가 막으려는 것과 정확히 같은 실패 모드다.
STATUS_MECHANISM_DIFFERED = "mechanism_differed"

# `상태`가 가질 수 있는 값의 **유일한 출처**. 테스트가 이 집합을 그대로 쓴다 —
# 어휘를 두 곳에 적으면 한쪽이 조용히 뒤처지고, 그때 새 상태는 「오타」로 판정된다.
STATUSES = frozenset({
    "pending",              # 아직 예정일 전이거나 사람이 안 닫았다
    "confirmed",            # 예측대로였다
    "refuted",              # 예측과 달랐다 — **방향까지** 달랐다
    "inconclusive",         # 판정하지 못했다(대조군 없음·교란·전제 미충족)
    "untested",             # 그날 프로세스에 fix가 안 실려 검증 자체가 성립 안 됐다
    STATUS_MECHANISM_DIFFERED,  # 방향은 맞고 **기제가 달랐다**(2026-08-14 고도화 4)
    # 2026-08-26 — **전제가 바뀌어 그 질문 자체가 끝났다.** 판정은 유효한데 더 이상 물을
    # 수 없는 상태이고, 후속 항목이 그 자리를 이어받는다.
    #
    # 이 어휘는 `hypotheses.yaml`이 **먼저 쓰고 있었다**: `2026-08-23-wiring2`의 주의 절이
    # *"`KIS_HTS_ID`를 채우면 … 이 가설은 `superseded`로 닫고 「구독이 성립하는가」로 다시
    # 적는다"* 라고 절차까지 적어 뒀고, 08-26 장전에 사람이 그 키를 채우면서 커밋 `a1433da`가
    # 실제로 그렇게 닫았다. **그런데 이 집합에는 안 들어와 있었다** —
    # `test_repository_hypotheses_file_is_valid_and_uses_resolvable_metric_paths`가
    # 그 순간부터 붉었고, 위 주석이 예고한 「어휘를 두 곳에 적으면 한쪽이 조용히 뒤처진다」가
    # 정확히 일어난 것이다. 뒤처진 쪽을 여기서 따라붙인다.
    #
    # ⚠ `inconclusive`와 다르다: 저쪽은 **판정하지 못한 것**이고 이쪽은 **판정은 났는데
    # 질문이 끝난 것**이다. 섞으면 「검증을 못 했다」와 「검증하고 넘어갔다」가 한 칸이 된다.
    "superseded",
})

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

# 2026-08-12(§1-1 / Fix#6) — **규약 H: 레버가 꺼진 날의 숫자로 그 레버의 가설을 판정하지 않는다.**
#
# 규약 F/G의 셋째다. F는 *"건수는 구조 변수에 비례한다"*, G는 *"어떤 값은 그날 시장에 비례한다"*,
# H는 **"어떤 값은 그 코드가 실제로 돌았는가에 비례한다"** 이다. 셋 다 예측을 쓰는 순간에는 그
# 변수가 상수처럼 느껴지고, 셋 다 결과가 같다 — 멀쩡한 fix가 반증으로 찍힌다.
#
# ## 08-12에 무슨 일이 있었는가
#
# 그날 `NEXT_TODO`의 「켤 것 — 오늘 단 하나만」은 확신도 분모 레버(`use_effective_member_count`)
# 였다. **안 켜졌다** — 키가 `strategy_params.yaml`에 없었고 커밋도 0건이었다. 그런데 §0은
# `2026-08-11-eF`를 켜진 전제로 판정해 「HIGH_CONVICTION이 34건보다 줄어야 한다」 옆에 91건을
# 찍었다. 표만 보면 분모 전환이 **반대로 작동한 것처럼** 보인다.
#
# ## F/G와 검사 시점이 다르다
#
# F/G는 예측을 **쓸 때** 형태를 보고 막는다(부등식의 모양). H는 막을 것이 없다 — 예측 자체는
# 옳았고 그날 실행되지 않았을 뿐이다. 그래서 H는 **읽을 때** 거는 조건이고, 검사 대상은 yaml
# 문법이 아니라 **그날의 코드 상태**(`mahdi.ops.levers`)다.
#
# ## 쓰는 법
#
# `hypotheses.yaml` 항목에 `전제레버: use_effective_member_count`를 적는다(리스트도 된다 —
# 전부 켜져 있어야 판정한다). 레버가 꺼져 있으면 그 항목의 모든 예측이 「미실행」이 되고
# 확인/반증 어느 쪽으로도 세지 않는다.
#
# **레버 이름을 모르면(오타·미등록) 「미실행」으로 닫지 않는다.** 그것은 「꺼져 있었다」가 아니라
# 「못 읽었다」이고, 조치가 다르다 — 08-06이 「실측 없음」과 「경로 없음」을 가른 것과 같은 구분이다.
VERDICT_LEVER_OFF = "미실행"


def lever_gate(entry: dict, levers: dict | None) -> tuple[bool, list[str], list[str]]:
    """
    입력: 가설 항목, `mahdi.ops.levers.collect()` 결과.
    반환: `(전부 켜져 있는가, 꺼진 레버 목록, 상태를 모르는 레버 목록)`.
    해석: `전제레버`가 없으면 항상 `(True, [], [])` — 레버와 무관한 가설이 대다수다.
         집계 자체가 없으면(`levers is None`) **판정을 막지 않는다**: 레버를 못 읽은 날에
         전 가설이 「미실행」이 되면 이 규약이 08-06 「경로 없음」 사고를 그대로 재현한다.
    실패 조건: 없다.
    """
    from mahdi.ops.levers import lever_state

    required = entry.get("전제레버")
    if not required:
        return True, [], []
    if isinstance(required, str):
        required = [required]
    if levers is None:
        return True, [], [str(k) for k in required]
    off, unknown = [], []
    for key in required:
        state = lever_state(levers, str(key))
        if state is None:
            unknown.append(str(key))
        elif not state:
            off.append(str(key))
    return (not off), off, unknown


# ===== 2026-08-13 고도화 4 + 2026-08-14 고도화 5 — **유예를 무행동으로 성립시키지 않는다** =====
#
# 레버 F(`use_effective_member_count`)는 08-12·08-13·08-14 **세 번** 「오늘 켤 것」으로 지정되고
# 세 번 다 안 켜졌다. 레버 E(`OPTION_CHAIN_SLOW_SERIES_CONGESTED_HOURS`)는 **일곱 번** 미뤄졌다.
# 열 번 중 한 번도 「안 켜기로 했다」고 적힌 적이 없다 — 전부 **아침에 잊은 것**이다.
#
# 규약 H는 「꺼진 날의 숫자로 판정하지 않는다」를 보장할 뿐 **켜지는 것을 보장하지 않는다.**
# 그 빈자리를 메우는 것이 아래 두 필드다:
#
#   발동일:       그날 아침 기동 스크립트가 콘솔에 크게 경고한다(`scripts/check_lever_due.py`).
#   무조건발동일: 이 날짜가 지났는데 레버가 꺼져 있으면 **테스트가 실패한다.**
#
# 경고만으로는 세 번 실패했다. 테스트 실패가 이 장치의 전부이고, 경고는 그 앞에 두는 예고편이다.
#
# **자동으로 켜지는 않는다** — 판단은 사람이 한다(2026-07-08 페이서 자동 적응이 500 폭주로
# 203분을 태운 전례). 이 장치가 강제하는 것은 「켜라」가 아니라 **「켜거나, 사유를 적고 날짜를
# 옮겨라」**이다. 후자도 사람의 한 줄이면 끝난다.
FIELD_ACTIVATE_ON = "발동일"
FIELD_DEADLINE = "무조건발동일"
FIELD_DEFERRALS = "유예횟수"


def lever_deadline_breaches(entries: list[dict], today: date, levers: dict | None) -> list[dict]:
    """
    입력: 가설 목록, 오늘 날짜, `levers.collect()` 결과.
    반환: **`무조건발동일`이 지났는데 전제레버가 아직 꺼져 있는** 항목들
          `[{id, 무조건발동일, 지난일수, off}]`.
    해석: 빈 리스트가 정상이다. 한 건이라도 있으면 그것은 「유예가 또 무행동으로 성립했다」는
         뜻이고, 테스트와 기동 경고가 같은 이 함수를 본다 — **두 곳이 다른 규칙을 쓰면
         한쪽이 조용히 틀린다.**
    실패 조건: 없다. 날짜 형식이 깨진 항목은 건너뛴다(그 오타는 YAML 검증이 잡을 일이다).
              **레버 상태를 못 읽었으면(`unknown`) 위반으로 세지 않는다** — 「꺼져 있었다」와
              「못 읽었다」의 조치가 다르다(규약 G와 같은 구분).
    """
    out = []
    for entry in entries:
        raw = entry.get(FIELD_DEADLINE)
        if not raw:
            continue
        try:
            deadline = date.fromisoformat(str(raw).strip())
        except ValueError:
            continue
        if deadline > today:
            continue
        _ok, off, _unknown = lever_gate(entry, levers)
        if off:
            out.append({
                "id": entry.get("id"),
                FIELD_DEADLINE: deadline.isoformat(),
                "지난일수": (today - deadline).days,
                "off": off,
                FIELD_DEFERRALS: entry.get(FIELD_DEFERRALS),
            })
    return out


def levers_due_today(entries: list[dict], today: date, levers: dict | None) -> list[dict]:
    """반환: **오늘이 `발동일`인데 아직 꺼져 있는** 레버 항목들 — 기동 전 경고의 재료.

    `lever_deadline_breaches`와 나눠 두는 이유: 이쪽은 «오늘 켤 차례다»(예고), 저쪽은
    «기한이 지났다»(위반)이다. 한 함수로 합치면 경고와 실패가 같은 세기로 울려서
    **매일 울리는 경고**가 되고, 그런 경고는 곧 무시된다.
    """
    out = []
    for entry in entries:
        raw = entry.get(FIELD_ACTIVATE_ON)
        if not raw:
            continue
        try:
            when = date.fromisoformat(str(raw).strip())
        except ValueError:
            continue
        if when != today:
            continue
        _ok, off, _unknown = lever_gate(entry, levers)
        if off:
            out.append({"id": entry.get("id"), FIELD_ACTIVATE_ON: when.isoformat(), "off": off})
    return out


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


# ===== 2026-08-19 장후 — **부모는 있고 잎이 영원히 없는 경로** =====
#
# `path_exists()`는 부모만 본다. 그 판별은 08-06에 옳았고 지금도 옳다 — 잎은 그날 데이터에
# 따라 없을 수 있다. 그러나 그 규칙에는 **한 종류의 오타가 통째로 빠져나가는 구멍**이 있다:
# 부모 절 이름은 맞게 적고 **잎 이름만 틀린** 경우다.
#
#     db.decisions.strategies         ->  dig() = None,  path_exists() = True
#     db.signal_reach.member_availability
#     db.regime.warmup_minutes
#
# 셋 다 「경로 없음」이 아니라 **「실측 없음」**(= 그날 값이 안 나왔다, 내일 다시 본다)으로
# 분류된다. **그 잎은 내일도, 다음 달에도 안 나온다.** 가설은 조용히 무력화되고, 아무도
# 그것을 신고하지 않는다 — 08-18 보고서가 이 결함을 **이틀에 두 번** 각각 다른 사고로 적었다
# (`c1`의 `db.tables.execution_logs` 3-2 · `2026-08-16-fix1`의 `db.signal_reach...` 7-3).
# 한 구멍이다.
#
# **가장 비쌌던 실례**: `2026-08-11-eF-effective-member-denominator`의 **대가** 지표가
# `db.decisions.strategies`인데 실재 키는 `entry_strategies`다. 레버 F의 무조건발동일은
# 2026-08-25 — 이 린트가 없었으면 **대가를 못 재는 채로 켰다**(규약 E 정면 위반).
#
# ## 왜 「부모가 dict일 때만」 판정하는가 — 오탐보다 미탐이 낫다
#
# 부모가 list면 `dig()`가 자연 키로 색인한다(`db.tables.<이름>`). 그 리스트에 그 이름의 행이
# 없는 것은 **오타가 아니라 「그날 그 테이블에 행이 없었다」**일 수 있다. 스칼라면 경로 자체가
# 더 깊이 들어갈 수 없으므로 이미 `path_exists()`의 관할이다. 둘 다 **판정하지 않는다.**
#
# ## 카운터 dict를 반드시 비켜 가야 한다
#
# `db.decisions.reject_reason.<사유>` · `db.decisions.entry_strategies.<전략>`처럼 **키가 그날의
# 데이터인** dict가 있다. 거기서 잎의 부재는 정상이다(그 사유가 오늘 안 났다). 판별은
# **값의 모양**으로 한다 — 부모 dict의 값이 **전부 스칼라**면 그것은 집계 카운터이므로
# 판정을 보류한다. 구조 dict는 거의 언제나 dict나 list를 하나 이상 품는다.
#
# 이 규칙이 틀리는 날이 오면(전부 스칼라인 구조 dict에서 잎 오타가 나면) 이 린트는 **침묵한다.**
# 그 방향의 실패를 고른 것이다 — 거짓 경보가 몇 번 나면 이 열 전체가 무시되고, 그러면
# 08-06 「경로 없음」이 겪은 일이 그대로 재현된다.
_SCALAR_TYPES = (str, int, float, bool)


def leaf_absent(metrics: dict | None, db_metrics: dict | None, path: str) -> bool:
    """반환: 부모는 구조로서 실재하는데 **그 잎 키가 부모 dict에 없는가**(= 영원히 값이 안 나온다).

    입력: 로그 지표 · DB 지표 · 점 표기 경로.
    계산: 부모를 `dig()`로 꺼내 **dict인 경우에만** 잎 키의 부재를 본다. list·스칼라·None이면
         판정하지 않는다(False). 값이 전부 스칼라인 dict는 **집계 카운터**로 보고 역시
         판정하지 않는다 — 상세 근거는 위 절 주석.
    해석: 이 값이 참이면 `VERDICT_PATH_DEAD`("경로 없음")다. **「실측 없음」과 조치가 다르다** —
         내일 다시 보는 것이 아니라 오늘 yaml을 고쳐야 한다.
    실패 조건: 없다. 어떤 입력에도 예외를 내지 않는다(판정 못 하면 False).
    """
    from mahdi.ops.report import dig

    is_db = path.startswith("db.")
    root = (db_metrics or {}) if is_db else (metrics or {})
    rest = path[3:] if is_db else path
    parent_path, _, leaf = rest.rpartition(".")
    if not parent_path or not leaf:
        return False  # 마디가 하나뿐인 경로는 `path_exists()`의 관할이다
    parent = dig(root, parent_path)
    if not isinstance(parent, dict) or not parent:
        return False
    if all(v is None or isinstance(v, _SCALAR_TYPES) for v in parent.values()):
        return False  # 집계 카운터 dict — 잎의 부재가 정상이다
    return leaf not in parent


def measurable_on(entry: dict, target: date) -> bool:
    """반환: 이 가설의 지표가 **그날 사이드카에 존재할 수 있었는가**.

    `구현일`이 대상 날짜보다 **뒤**면 그날 지표에 그 키가 없는 것이 정상이다 — 잎 린트를
    거기에 걸면 **fix를 낸 다음 날마다 거짓 경보가 뜬다.** 08-19에 실제로 그 형태를 만났다:
    `2026-08-19-fix4-missing-check-alert`의 `watchdog.missing_check_alerts`는 08-18 지표에
    없는 것이 맞다(그날은 구현 이전이다).

    ## 같은 날도 면제한다 — 사이드카는 15:45에 확정되고 커밋은 그 뒤에도 들어온다

    `구현일 == 지표 날짜`인 항목은 **하루 안의 순서**에 답이 달려 있고 우리는 그것을 모른다.
    08-19가 그 형태를 바로 냈다: `2026-08-19-fix7-censored-phase-is-a-ratio-not-a-verdict`의
    `slow_calls.censored.phase_ratio`는 **17:00 커밋**으로 생겼는데 그날 사이드카는 **15:45**에
    확정됐다 — 키가 없는 것이 맞고, 그것은 오타가 아니다. **모르는 것은 판정하지 않는다.**

    `구현일`이 없거나 형식이 어긋나면 **검사한다** — 모르는 것을 면제로 바꾸면 이 린트가
    「구현일을 안 적으면 통과」로 우회된다. 위 면제와 방향이 반대인 이유는 대상이 다르기
    때문이다: 저기서 모르는 것은 **하루 안의 시각**이고 여기서 모르는 것은 **날짜 자체**다.
    """
    made = entry.get("구현일")
    if isinstance(made, str):
        try:
            made = date.fromisoformat(made)
        except ValueError:
            return True
    return not (isinstance(made, date) and made >= target)


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
    entries: list[dict], target: date, metrics: dict | None, db_metrics: dict | None = None,
    levers: dict | None = None,
) -> list[dict]:
    """
    입력: 가설 목록, 대상 날짜, 로그 지표, (선택) DB 지표, (선택) 그날의 레버 상태
         (`mahdi.ops.levers.collect()` — 2026-08-12 규약 H).
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
            # 2026-08-12 규약 H — 전제 레버가 꺼져 있으면 **그 코드가 오늘 안 돌았다.**
            # 실측/예측은 사실이므로 그대로 두고 **판정만** 무효화한다(주장 지표 없음과 같은 처리).
            levers_on, levers_off, levers_unknown = lever_gate(entry, levers)
            for prediction, role in zip(predictions, roles):
                path = prediction["metric"]
                actual = _lookup(metrics, db_metrics, path)
                expect = str(prediction["expect"])
                # 2026-08-06 Fix#3 — 경로 자체가 없는 것과 그날 값이 없는 것을 가른다.
                # 지표를 하나도 못 모은 날(DB 다운 등)에는 전 경로가 "없음"이 되므로,
                # 집계가 아예 비어 있으면 이 판별을 하지 않는다(전부 경로 없음으로 뜨면
                # 진짜 오타가 그 소음에 묻힌다 — 이 fix가 고치려던 바로 그 실패다).
                collected = bool(metrics) or bool(db_metrics)
                # 2026-08-19 장후 — **잎 부재를 OR로 합친다.** 새 판정 열을 만들지 않는다
                # (규약 N — 라벨을 하나 늘리면 리포트·수집기·테스트 3종 세트 비용이 붙는다).
                # `measurable_on()`이 「구현일이 이 날짜보다 뒤」인 항목을 빼 준다 — 그 예외가
                # 없으면 fix를 낸 다음 날마다 거짓 경보가 뜬다.
                dead_path = collected and actual is None and (
                    not path_exists(metrics, db_metrics, path)
                    or (measurable_on(entry, target) and leaf_absent(metrics, db_metrics, path))
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
                        # 규약 H — 리포트가 표 위로 따로 띄운다(§0). `lever_unknown`은
                        # 「꺼져 있었다」가 아니라 「그 이름의 레버가 없다」이므로 판정을 막지
                        # 않고 경고만 낸다 — 오타를 「미실행」으로 덮으면 영영 안 고쳐진다.
                        "lever_off": levers_off,
                        "lever_unknown": levers_unknown,
                        "verdict": (
                            VERDICT_LEVER_OFF if not levers_on
                            else VERDICT_UNJUDGEABLE if claim_missing
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
