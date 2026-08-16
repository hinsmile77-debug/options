"""오늘 어떤 레버가 켜져 있었는가 — 자동 지표가 그것을 인쇄한다 (2026-08-12 §1-1 / Fix#6).

## 이 모듈이 생긴 이유

08-12의 `NEXT_TODO`는 「켤 것 — 오늘 단 하나만」으로 **확신도 분모 레버**를 지정했다:
`strategy_params.yaml`의 `use_effective_member_count: true`.

그 레버는 **안 켜졌다.** 키가 파일에 없었고(`engine.py`가 기본값 `False`로 읽는다) 그날 커밋도
0건이었다. 그런데 자동 지표 §0은 `2026-08-11-eF` 가설을 **켜진 전제로 판정**했고, 「HIGH_CONVICTION이
34건보다 줄어야 한다」는 주장 옆에 실측 91건을 찍었다. 표만 보면 **분모 전환이 반대로 작동했다**고
읽힌다 — 실제로는 그 코드가 한 번도 실행되지 않았다.

## 규약 H — 레버가 꺼진 날의 숫자로 그 레버의 가설을 판정하지 않는다

규약 F(*"건수는 구조 변수에 비례한다"*)와 G(*"어떤 값은 그날 시장에 비례한다"*)의 형제다.
셋 다 **예측을 쓰는 순간에 상수처럼 느껴지는 변수**가 원인이고, 셋 다 결과가 같다 —
**멀쩡한 fix가 반증으로 찍히고, 그것을 믿으면 고칠 필요 없는 것을 고치게 된다.**

F/G와 다른 점: F/G는 예측을 **쓸 때** 막는 규칙이고(부등식의 형태를 본다), H는 예측을
**읽을 때** 거는 조건이다. 그래서 검사 대상이 yaml 문법이 아니라 **그날의 코드 상태**다.

## 왜 지표가 레버를 읽는가 — 사람의 기억에 두지 않는다

08-12에 필요했던 것은 「오늘 F를 켰던가?」라는 질문이고, 그 답은 **저장소 안에 있었다**
(`strategy_params.yaml`과 `git HEAD`). 아무도 묻지 않았을 뿐이다. `hypotheses.yaml`이
"예측을 미리 적어 사람의 습관에서 떼어낸" 것과 같은 이유로, 레버 상태도 여기서 떼어낸다.

## 레버를 추가하는 법

`collect()`에 한 줄 더한다. **판정은 하지 않는다** — 값과 "기본값과 다른가"만 낸다.
「이 값이 옳은가」는 그 레버의 가설이 답할 질문이지 이 모듈이 답할 질문이 아니다.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger("mahdi.ops.levers")


# 레버 하나의 기술(記述). `default`는 **꺼진 상태의 값**이다 — `on`은 그것과 다른가로 정한다.
#
# `key`는 `hypotheses.yaml`의 `전제레버`가 참조하는 이름이다. 짧게 유지한다(사람이 손으로 적는다).
_SPEC: tuple[tuple[str, str, Any], ...] = (
    # key, 위치(사람이 읽는 문자열), 꺼진 상태의 값
    ("use_effective_member_count", "strategy_params.yaml", False),
    ("reentry_cooldown_minutes", "strategy_params.yaml · strategy_gates", 0),
    ("SIGNAL_FUSION_PHASE_OFFSET_SECONDS", "mahdi/main.py", 10.0),
    ("OPTION_CHAIN_SLOW_SERIES_CONGESTED_HOURS", "mahdi/main.py", {}),
    ("OPTION_CHAIN_READ_TIMEOUT_SECONDS", "mahdi/broker/rest_client.py", None),
    ("REGIME_RESTORE_SESSION_WINDOW", "mahdi/engines/regime_pipeline.py", False),
)


def _read_values() -> dict[str, Any]:
    """레버의 **실제 현재 값**을 읽는다 — 문서가 아니라 코드/설정에서.

    임포트 실패는 그 레버만 `None`으로 두고 넘어간다: 이 모듈 때문에 리포트 전체가 죽으면
    안 된다(`daily_ops_report.build`의 다른 선택 절들과 같은 원칙).
    """
    values: dict[str, Any] = {}
    try:
        from mahdi.config.settings import get_strategy_params

        params = get_strategy_params() or {}
        values["use_effective_member_count"] = bool(params.get("use_effective_member_count", False))
        gates = (params.get("strategy_gates") or {})
        values["reentry_cooldown_minutes"] = gates.get("reentry_cooldown_minutes", 0)
    except Exception:
        logger.warning("strategy_params 레버 읽기 실패", exc_info=True)
    try:
        from mahdi import main as main_module

        values["SIGNAL_FUSION_PHASE_OFFSET_SECONDS"] = main_module.SIGNAL_FUSION_PHASE_OFFSET_SECONDS
        values["OPTION_CHAIN_SLOW_SERIES_CONGESTED_HOURS"] = dict(
            main_module.OPTION_CHAIN_SLOW_SERIES_CONGESTED_HOURS
        )
    except Exception:
        logger.warning("main 레버 읽기 실패", exc_info=True)
    try:
        from mahdi.broker import rest_client

        values["OPTION_CHAIN_READ_TIMEOUT_SECONDS"] = rest_client.OPTION_CHAIN_READ_TIMEOUT_SECONDS
    except Exception:
        logger.warning("rest_client 레버 읽기 실패", exc_info=True)
    try:
        from mahdi.engines import regime_pipeline

        values["REGIME_RESTORE_SESSION_WINDOW"] = regime_pipeline.REGIME_RESTORE_SESSION_WINDOW
    except Exception:
        logger.warning("regime_pipeline 레버 읽기 실패", exc_info=True)
    return values


def _git_head(project_root: Path | None) -> str | None:
    """오늘 돌던 코드가 어느 커밋인가.

    레버 값만으로는 "어제와 같은 코드였는가"에 답할 수 없다 — 08-12에 커밋이 0건이었다는 사실이
    「레버가 안 켜졌다」의 결정적 근거였다. `git` 없이도 리포트는 나와야 하므로 실패는 None이다.
    """
    if project_root is None:
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--short", "HEAD"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, text=True,
        )
    except Exception:
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def collect(project_root: Path | None = None) -> dict:
    """
    반환: `{"levers": [{key, 위치, value, default, on}], "git_head": str|None}`.
    계산: 레버의 현재 값을 읽고 **꺼진 상태의 값과 다른가**(`on`)만 판정한다.
    해석: `on`은 「좋다/나쁘다」가 아니라 「그 코드가 오늘 실행됐는가」다. 그 구분이 규약 H의 전부다.
    실패 조건: 없다 — 못 읽은 레버는 `value=None, on=None`("모른다")으로 남는다.
             **`on=False`("꺼져 있었다")와 `on=None`("못 읽었다")을 섞지 않는다**: 전자는
             가설을 「미실행」으로 닫아도 되지만 후자는 사람이 봐야 한다.
    """
    values = _read_values()
    levers = []
    for key, where, default in _SPEC:
        present = key in values
        value = values.get(key)
        levers.append(
            {
                "key": key,
                "위치": where,
                "value": value,
                "default": default,
                "on": (value != default) if present else None,
            }
        )
    return {"levers": levers, "git_head": _git_head(project_root)}


def lever_value(levers: dict | None, key: str) -> Any:
    """반환: 그 레버의 **현재 값**. 못 읽었거나 미등록이면 None.

    `lever_state()`가 「켜졌는가」에 답한다면 이쪽은 「무슨 값이었는가」에 답한다 —
    2026-08-14 Fix#3이 `OPTION_CHAIN_READ_TIMEOUT_SECONDS`의 실제 값을 필요로 하면서 생겼다.
    p50과 비교할 임계는 **그날 실제로 걸려 있던 타임아웃**이어야 하고, 그것은 레버가 켜진 날
    전역값(4.0초)과 다르다.

    ⚠ 여기서도 `None`을 기본값으로 접지 않는다 — 이 레버는 **꺼진 상태의 값 자체가 `None`**
    (= 전역값 사용)이라, 「모른다」와 「꺼져 있었다」가 같은 표현이 된다. 호출측이 폴백을 정한다.
    """
    for lever in (levers or {}).get("levers", []):
        if lever.get("key") == key:
            return lever.get("value")
    return None


def lever_state(levers: dict | None, key: str) -> bool | None:
    """반환: 그 레버가 켜져 있었는가. 모르면(집계 없음/못 읽음/미등록) None.

    **None을 False로 접지 않는다.** 「꺼져 있었다」와 「모른다」의 조치가 다르다 — 전자는
    가설을 미실행으로 닫고, 후자는 레버 목록에 그 이름이 없다는 뜻이라 오타를 의심해야 한다
    (규약 F/G가 「실측 없음」과 「경로 없음」을 가른 것과 같은 구분이다).
    """
    for lever in (levers or {}).get("levers", []):
        if lever.get("key") == key:
            return lever.get("on")
    return None
