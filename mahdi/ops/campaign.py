"""검증 캠페인 — **표본이 찰 때까지 판정을 미루는 자리** (2026-08-18 신설).

`hypotheses.yaml`은 하루 뒤 판정하는 예측을 담는다: `구현일 → 검증예정일 → 상태`.
검증예정일이 **단일 날짜**이고 그날 결론이 난다. 그런데 어떤 질문은 표본이 며칠~몇 주 걸린다.

2026-08-18에 그 빈자리가 실제로 드러났다. `2026-08-17-p1` 반증의 후속이 셋으로 갈렸는데
((a) 구독 창 확대 / (b) 델타 밴드 재정의 / (c) 계측 불가 기록) **그 질문이 살 곳이 없었다** —
가설에 넣으면 검증예정일을 정해야 하고 표본이 안 차면 매일 날짜를 미루게 되며, `NEXT_TODO`
체크박스에 넣으면 아무도 표본을 세지 않는다. 마흐디에는 이미 같은 형태의 전례가 있다:
레버 E는 *"발동 조건이 네 번 성립했고 네 번 미뤘다"* 로 기록됐고, **그 네 번을 세는 자리가
없어 매번 산문으로 다시 세었다.**

## 이 모듈이 지키는 네 가지

1. **`표본 미달`은 `불합격`이 아니다.** 「아직 모른다」와 「틀렸다」는 다른 사건인데, 지금
   `hypotheses`는 둘 다 `inconclusive`로 뭉갠다. 여기서는 자료구조가 그것을 가른다 —
   *"하루치로 결론 내지 않는다"* 를 사람의 자제력이 아니라 상태값으로 강제한다.
2. **DB를 읽지 않는다.** 일별 지표(`docs/동작점검/auto/YYYY-MM-DD_지표.json`)가 이미 커밋되는
   위치에 매일 쌓이므로 그것을 접기만 한다. 새 폴러·테이블·스케줄러가 없다.
3. **없는 날은 0이 아니다.** 채널이 지목한 경로가 그날 json에 없으면(그 지표가 그 채널
   개시일 이후에 생겼을 수 있다) **표본에 넣지 않는다.** 0으로 채우면 「못 쟀다」와 「쟀는데
   0이다」가 뭉개진다(규약 C).
4. **주장에 절대 건수를 못 걸게 한다.** 누적 건수는 관측 일수에 비례하므로 그대로 임계를
   걸면 «며칠 지났는가»를 재게 된다 — 규약 F가 하루 지표에 대해 막는 것과 같은 함정이
   누적에서는 더 크다. 그래서 주장 규칙은 **비율 또는 관측**만 허용한다.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path
from typing import Any

from mahdi.ops.hypotheses import ROLE_CLAIM, is_market_state_dependent

logger = logging.getLogger("mahdi.ops.campaign")

# ===== 판정 상태 =====
#
# `hypotheses`의 `확인/반증`과 **일부러 다른 낱말을 쓴다.** 두 축이 같은 낱말을 쓰면 리포트에서
# 어느 축의 판정인지 알 수 없게 되고, 그때부터 둘은 서로 다른 사실을 말할 수 있다(규약 A).
VERDICT_INSUFFICIENT = "표본 미달"
VERDICT_PASS = "합격"
VERDICT_FAIL = "불합격"
VERDICT_OBSERVE = "관측"
VERDICT_BLOCKED = "선행 대기"
VERDICT_PATH_DEAD = "경로 없음"

STATUS_OPEN = "open"
STATUS_JUDGED = "judged"
STATUS_CLOSED = "closed"
STATUSES = frozenset({STATUS_OPEN, STATUS_JUDGED, STATUS_CLOSED})

RULE_OBSERVE = "관측"
_RATIO_RE = re.compile(r"^비율\s*(<=|<|>=|>|==)\s*(-?\d+(?:\.\d+)?)$")


def load(path: Path) -> dict:
    """
    계산: 캠페인 YAML을 읽어 `{"channels": [...], "decisions": [...]}`로 반환한다.
    실패 조건: 파일이 없거나 파싱이 실패하면 빈 구조 — **리포트 전체를 막지 않는다**
              (`hypotheses.load()`와 같은 원칙: 이 절이 안 나올 뿐이다).
    """
    empty: dict = {"channels": [], "decisions": []}
    if not path.exists():
        return empty
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.warning("캠페인 파일 파싱 실패: %s", path, exc_info=True)
        return empty
    if not isinstance(data, dict):
        return empty
    return {
        "channels": [c for c in (data.get("channels") or []) if isinstance(c, dict)],
        "decisions": [d for d in (data.get("decisions") or []) if isinstance(d, dict)],
    }


_DAILY_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_지표\.json$")


def load_daily_metrics(auto_dir: Path, until: date | None = None) -> list[tuple[date, dict]]:
    """
    입력: `docs/동작점검/auto` 경로, (선택) 이 날짜까지만.
    계산: `YYYY-MM-DD_지표.json`을 전부 읽어 `(날짜, dict)` 목록을 날짜순으로 돌려준다.
    해석: **이 파일들이 캠페인의 유일한 입력이다** — DB를 다시 읽지 않는다. 그 덕에 캠페인은
         과거를 재해석할 수 있고(채널을 나중에 만들어도 개시일부터 소급된다), 새 수집 경로가
         필요 없다.
    실패 조건: 못 읽는 파일은 건너뛴다 — 하루가 깨졌다고 캠페인 전체가 멈추면 안 된다.
    """
    import json

    out: list[tuple[date, dict]] = []
    if not auto_dir.exists():
        return out
    for path in sorted(auto_dir.glob("*_지표.json")):
        m = _DAILY_FILE_RE.match(path.name)
        if not m:
            continue
        try:
            day = date.fromisoformat(m.group(1))
            if until is not None and day > until:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("일별 지표 파일 건너뜀: %s", path, exc_info=True)
            continue
        if isinstance(payload, dict):
            out.append((day, payload))
    return out


def lookup(daily: dict, path: str) -> Any:
    """
    입력: 하루치 지표 json 전체, 지표 경로(`"overrun.count"` / `"db.decisions.x"`).
    계산: `db.` 접두어면 `daily["db"]` 아래에서, 아니면 최상위에서 찾는다 —
         `hypotheses._lookup()`과 **같은 규약**이다(경로 문법이 두 축에서 갈리면 안 된다).
    실패 조건: 없음 — 못 찾으면 None.
    """
    from mahdi.ops.report import dig

    if path.startswith("db."):
        return dig(daily.get("db") or {}, path[3:])
    return dig(daily, path)


def path_present(daily: dict, path: str) -> bool:
    """계산: 그날 json에 이 경로의 **부모 구조**가 있는가 — `hypotheses.path_exists()`와 같은 판정."""
    from mahdi.ops.report import dig

    is_db = path.startswith("db.")
    root = (daily.get("db") or {}) if is_db else daily
    rest = path[3:] if is_db else path
    parent, _, _ = rest.rpartition(".")
    if not parent:
        return rest in root
    return dig(root, parent) is not None


def rule_kind(rule: Any) -> str:
    """반환: `"관측"` | `"비율"` | `""`(알 수 없음). 규칙 언어는 Phase 1에서 이 둘뿐이다."""
    text = str(rule or "").strip()
    if text == RULE_OBSERVE:
        return RULE_OBSERVE
    return "비율" if _RATIO_RE.match(text) else ""


def violates_ratio_claim_rule(role: str, rule: Any) -> bool:
    """
    계산: **주장** 역할인데 규칙이 비율도 관측도 아닌가 — 즉 누적 건수에 임계를 걸었는가.
    해석: 규약 F의 캠페인판이다. 누적 건수는 관측 일수에 비례하므로 임계를 걸면 그 지표는
         «며칠 지났는가»를 재게 된다 — 하루 지표에서보다 함정이 더 크다.
    실패 조건: 없음.
    """
    return str(role) == ROLE_CLAIM and rule_kind(rule) == ""


def violates_market_state_rule(role: str, metric: str, rule: Any) -> bool:
    """
    계산: **주장** 역할 + 시장 상태 의존 지표 + 임계(관측이 아님)인가.
    해석: 규약 G의 캠페인판. 시장 상태에 의존하는 지표는 «오늘 시장이 어땠는가»의 함수라
         누적해도 그 성질이 사라지지 않는다 — 임계를 걸면 조용한 구간이 반증으로 찍힌다.
         그런 지표는 `관측`으로만 등록할 수 있다.
    실패 조건: 없음.
    """
    if str(role) != ROLE_CLAIM or not is_market_state_dependent(str(metric)):
        return False
    return rule_kind(rule) != RULE_OBSERVE


def validate(channel: dict) -> list[str]:
    """
    계산: 채널 하나의 스키마·규약 위반을 전부 모아 돌려준다.
    해석: 「영원히 검정 불가한 채널」을 만들지 않기 위한 관문이다 —
         `test_ops_hypotheses`가 지표 경로에 대해 하는 일을 채널에도 한다.
    실패 조건: 없음 — 위반이 없으면 빈 목록.
    """
    problems: list[str] = []
    if not channel.get("id"):
        problems.append("id 없음")
    if not channel.get("질문"):
        problems.append("질문 없음")
    if channel.get("상태") not in STATUSES:
        problems.append(f"상태가 {sorted(STATUSES)} 중 하나가 아니다: {channel.get('상태')!r}")

    sample = channel.get("표본") or {}
    if not sample.get("metric"):
        problems.append("표본.metric 없음 — 무엇을 세는지 정하지 않으면 표본 미달을 판정할 수 없다")
    if not isinstance(sample.get("min_samples"), int) or sample.get("min_samples", 0) <= 0:
        problems.append("표본.min_samples가 양의 정수가 아니다")
    if not isinstance(sample.get("min_days"), int) or sample.get("min_days", 0) <= 0:
        problems.append("표본.min_days가 양의 정수가 아니다 — 단일일 쏠림을 막는 값이다")

    judgements = channel.get("판정") or []
    if not judgements:
        problems.append("판정 규칙 없음")
    for j in judgements:
        metric, role, rule = j.get("metric"), j.get("역할"), j.get("rule")
        if not metric:
            problems.append("판정.metric 없음")
            continue
        if rule_kind(rule) == "":
            problems.append(f"{metric}: 규칙을 못 읽었다({rule!r}) — '비율 < 0.30' 또는 '관측'")
        if violates_ratio_claim_rule(role, rule):
            problems.append(f"{metric}: 주장 규칙은 비율 또는 관측만 가능하다(누적 건수 임계 금지)")
        if violates_market_state_rule(role, metric, rule):
            problems.append(f"{metric}: 시장 상태 의존 지표는 관측으로만 등록한다(규약 G)")
    return problems


def accumulate(channel: dict, daily_metrics: list[tuple[date, dict]]) -> dict:
    """
    입력: 채널, `(날짜, 그날 지표 json)` 목록(개시일 필터는 호출측이 이미 했다고 가정하지 않는다 —
         여기서 `개시일`로 다시 거른다).
    계산: 표본 경로와 판정 경로를 날짜별로 더한다. **경로가 없는 날은 건너뛴다** — 0으로 채우면
         「못 쟀다」와 「쟀는데 0이다」가 뭉개진다(규약 C).
    해석: 반환 dict의 `days`는 **표본을 실제로 센 날 수**다. 달력 일수가 아니다 — 휴장일과
         지표가 아직 없던 날은 빠진다.
    실패 조건: 없음 — 아무 날도 못 세면 `samples=0, days=0`.
    """
    start = channel.get("개시일")
    sample_metric = str((channel.get("표본") or {}).get("metric") or "")
    totals: dict[str, float] = {}
    counted_days: dict[str, int] = {}
    samples, days = 0.0, 0
    sample_path_seen = False
    # 2026-08-18 — **「아직 개시 전」과 「경로가 없다」를 가른다.** 이 값을 안 세면 개시일이
    # 미래인 채널(막 등재한 채널)이 「경로 없음」으로 찍힌다 — 그건 오타를 뜻하는 상태이고,
    # 새 채널을 등재할 때마다 거짓 경보가 뜬다.
    days_in_range = 0

    for day, daily in daily_metrics:
        if start and isinstance(start, date) and day < start:
            continue
        days_in_range += 1
        if not sample_metric or not path_present(daily, sample_metric):
            continue
        sample_path_seen = True
        value = lookup(daily, sample_metric)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        samples += float(value)
        days += 1
        for j in channel.get("판정") or []:
            metric = str(j.get("metric") or "")
            if not metric or not path_present(daily, metric):
                continue
            v = lookup(daily, metric)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                totals[metric] = totals.get(metric, 0.0) + float(v)
                counted_days[metric] = counted_days.get(metric, 0) + 1

    return {
        "samples": samples,
        "days": days,
        "days_in_range": days_in_range,
        "totals": totals,
        "metric_days": counted_days,
        "sample_path_seen": sample_path_seen,
    }


def _compare(actual: float, op: str, threshold: float) -> bool:
    return {
        "<=": actual <= threshold, "<": actual < threshold,
        ">=": actual >= threshold, ">": actual > threshold,
        "==": abs(actual - threshold) < 1e-9,
    }[op]


def judge(channel: dict, sample: dict, judged_ids: frozenset[str] = frozenset()) -> dict:
    """
    입력: 채널, `accumulate()` 결과, 이미 판정된 채널 id 집합(선행 채널 게이트용).
    계산: 판정 순서가 곧 우선순위다 —
         선행 대기 → 경로 없음 → 표본 미달 → (관측 | 합격/불합격).
    해석: **`표본 미달`이 `불합격`보다 먼저 온다**는 것이 이 모듈의 존재 이유다.
         표본이 없는데 비율을 계산하면 그 값은 판정이 아니라 잡음이다.
    실패 조건: 없음 — 모든 경로가 상태값으로 드러난다.
    """
    prerequisite = channel.get("선행")
    if prerequisite and str(prerequisite) not in judged_ids:
        return {"verdict": VERDICT_BLOCKED, "detail": f"선행 채널 미판정: {prerequisite}"}

    sample_cfg = channel.get("표본") or {}
    min_samples = int(sample_cfg.get("min_samples") or 0)
    min_days = int(sample_cfg.get("min_days") or 0)

    progress = f"{sample['samples']:.0f}/{min_samples} ({sample['days']}일)"
    if not sample["sample_path_seen"]:
        # 개시일 이후 지표 파일이 아직 하나도 없으면 «아직 시작 안 했다»이지 «오타»가 아니다.
        if sample["days_in_range"] == 0:
            return {"verdict": VERDICT_INSUFFICIENT, "progress": progress, "detail": "개시 전"}
        return {
            "verdict": VERDICT_PATH_DEAD,
            "progress": progress,
            "detail": f"표본 경로가 개시일 이후 어느 날에도 없다: {sample_cfg.get('metric')}",
        }
    if sample["samples"] < min_samples or sample["days"] < min_days:
        return {"verdict": VERDICT_INSUFFICIENT, "progress": progress, "detail": _observed(channel, sample)}

    # 표본은 찼다 — 규칙별로 접는다. 관측 규칙이 하나라도 있으면 그 채널은 판정하지 않는다.
    results: list[str] = []
    failed = False
    observe_only = True
    for j in channel.get("판정") or []:
        metric = str(j.get("metric") or "")
        rule = j.get("rule")
        kind = rule_kind(rule)
        total = sample["totals"].get(metric)
        if kind == RULE_OBSERVE:
            results.append(f"{_leaf(metric)}={_ratio_text(total, sample['samples'])} (관측)")
            continue
        observe_only = False
        m = _RATIO_RE.match(str(rule).strip())
        if m is None or total is None or sample["samples"] <= 0:
            results.append(f"{_leaf(metric)}=판정 불가")
            failed = True
            continue
        ratio = total / sample["samples"]
        ok = _compare(ratio, m.group(1), float(m.group(2)))
        failed = failed or not ok
        results.append(f"{_leaf(metric)}={ratio:.3f} ({'OK' if ok else 'NG'} {rule})")

    verdict = VERDICT_OBSERVE if observe_only else (VERDICT_FAIL if failed else VERDICT_PASS)
    return {"verdict": verdict, "progress": progress, "detail": " · ".join(results)}


def _leaf(metric: str) -> str:
    return metric.rsplit(".", 1)[-1]


def _ratio_text(total: float | None, samples: float) -> str:
    if total is None or samples <= 0:
        return "-"
    return f"{total / samples:.3f}"


def _observed(channel: dict, sample: dict) -> str:
    """표본 미달이어도 **지금까지 무엇이 보이는지**는 인쇄한다 — 값이 없으면 진행조차 안 보인다."""
    parts = [
        f"{_leaf(str(j.get('metric')))}={_ratio_text(sample['totals'].get(str(j.get('metric'))), sample['samples'])}"
        for j in (channel.get("판정") or [])
    ]
    return " · ".join(parts)


def evaluate(
    campaign: dict, daily_metrics: list[tuple[date, dict]]
) -> list[dict]:
    """
    입력: `load()` 결과, `(날짜, 지표 json)` 목록.
    계산: 채널마다 누적·판정하고, 확정 결정(📌)을 붙여 리포트가 그릴 수 있는 dict 목록을 낸다.
    해석: **판정과 결정은 별개다.** 판정은 매일 재계산되지만 결정은 사람이 확정해 둔 것이라
         판정이 바뀌어도 남는다 — 그 분리가 이 제도를 도입한 이유의 절반이다(Phase 2에서
         레지스트리가 채워지고, 여기서는 이미 붙일 자리를 만든다).
    실패 조건: 없음 — 스키마 위반은 `problems`로 드러나고 그 채널은 판정하지 않는다.
    """
    decisions = {str(d.get("channel")): d for d in campaign.get("decisions") or []}
    judged_ids = frozenset(
        str(c.get("id")) for c in campaign.get("channels") or []
        if c.get("상태") in (STATUS_JUDGED, STATUS_CLOSED)
    )
    out: list[dict] = []
    for channel in campaign.get("channels") or []:
        if channel.get("상태") == STATUS_CLOSED:
            continue
        problems = validate(channel)
        row: dict = {
            "id": str(channel.get("id") or "?"),
            "질문": channel.get("질문") or "",
            "problems": problems,
        }
        if problems:
            row.update(verdict="스키마 오류", detail="; ".join(problems), progress="")
        else:
            sample = accumulate(channel, daily_metrics)
            row.update(judge(channel, sample, judged_ids))
            row.setdefault("progress", "")
        decision = decisions.get(row["id"])
        if decision:
            row["decision"] = decision.get("decision")
            row["decision_date"] = decision.get("date")
        out.append(row)
    return out
