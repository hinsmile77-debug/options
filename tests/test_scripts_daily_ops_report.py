"""`scripts/daily_ops_report.py` — 사이드카 **재생성**이 사실을 파괴하지 않는가 (2026-08-19).

08-19 보고서 Fix#8이 *"`--date 2026-08-18`로 재실행하면 된다. DB는 남아 있으므로 복구
가능하다"* 라고 적었고, 실제로 돌려 보니 절반만 맞았다 — 키를 하나도 잃지 않고 125개를
얻었지만(그중 `execution_logs`가 `c1` 판정의 전부였다) **기존 값 14개가 오늘 값으로 덮였다.**

이 파일이 지키는 것은 그 14개다. 상세 근거는 `daily_ops_report._RUN_TIME_SECTIONS` 위 절 주석.
"""

from __future__ import annotations

import inspect
import re

import pytest

from scripts.daily_ops_report import (
    _CUMULATIVE_LEAVES,
    _CUMULATIVE_ROW_LEAVES,
    _RUN_TIME_SECTIONS,
    _restore_run_time_fields,
)


def _payload(**over):
    base = {
        "levers": {"git_head": "NEW"},
        "cycles": {"count": 999},
        "db": {
            "ws_status": {"connected_since": "10:36:29"},
            "market_halt": {"updated_at": "15:41:36"},
            "feature_store": {"total": 10660, "hmm_progress_pct": 133.2, "today": 402},
            "regime": {
                "visits": [{"regime": "3", "today": 109, "total": 786, "days": 5}],
                "today_total": 402,
            },
            "decisions": {"total": 485},
        },
    }
    base.update(over)
    return base


def _previous():
    return {
        "levers": {"git_head": "OLD"},
        "cycles": {"count": 111},
        "db": {
            "ws_status": {"connected_since": "07:30:22"},
            "market_halt": {"updated_at": "15:40:26"},
            "feature_store": {"total": 10258, "hmm_progress_pct": 128.2, "today": 1},
            "regime": {
                "visits": [{"regime": "3", "today": 1, "total": 677, "days": 4}],
                "today_total": 1,
            },
        },
    }


def test_current_state_sections_come_from_the_run_that_actually_saw_that_day():
    """`ws_status`/`market_halt`/`levers`는 **그날 15:45의 실행만이** 관측할 수 있었다."""
    payload = _payload()
    restored = _restore_run_time_fields(payload, _previous())

    assert payload["db"]["ws_status"]["connected_since"] == "07:30:22"
    assert payload["db"]["market_halt"]["updated_at"] == "15:40:26"
    assert payload["levers"]["git_head"] == "OLD"
    assert "levers" in restored and "db.ws_status" in restored


def test_cumulative_leaves_are_preserved_but_their_same_day_siblings_are_not():
    """`feature_store.total`은 누적이라 하루가 지나면 값이 달라진다. `today`는 날짜로 필터된다 —
    절 단위로 보존하면 재생성이 고쳐 주는 그 값까지 함께 막힌다."""
    payload = _payload()
    _restore_run_time_fields(payload, _previous())

    assert payload["db"]["feature_store"]["total"] == 10258
    assert payload["db"]["feature_store"]["hmm_progress_pct"] == 128.2
    assert payload["db"]["feature_store"]["today"] == 402, "그날치는 새로 낸 값이 맞다"


def test_regime_visits_split_inside_the_row_not_by_section():
    """한 행 안에서 갈린다 — `today`는 그날치, `total`/`days`는 전 기간 누적."""
    payload = _payload()
    _restore_run_time_fields(payload, _previous())

    [row] = payload["db"]["regime"]["visits"]
    assert (row["total"], row["days"]) == (677, 4)
    assert row["today"] == 109, "그날치는 새로 낸 값이 맞다"


def test_everything_else_is_left_alone_because_regeneration_is_the_point():
    """되돌리는 것은 「재생성이 알 수 없는 것」뿐이다. 새 키를 막으면 재생성할 이유가 없어진다."""
    payload = _payload()
    payload["db"]["tables"] = [{"table": "execution_logs", "rows": 0}]
    _restore_run_time_fields(payload, _previous())

    assert payload["cycles"]["count"] == 999
    assert payload["db"]["decisions"]["total"] == 485
    assert payload["db"]["tables"] == [{"table": "execution_logs", "rows": 0}]


def test_a_section_missing_from_the_old_sidecar_is_skipped_not_blanked():
    """옛 파일에 그 절이 없으면 **그날엔 그 지표가 없었다**는 뜻이다 — 지우면 안 된다."""
    payload = _payload()
    previous = _previous()
    del previous["db"]["ws_status"]
    restored = _restore_run_time_fields(payload, previous)

    assert payload["db"]["ws_status"] == {"connected_since": "10:36:29"}
    assert "db.ws_status" not in restored


def test_an_empty_previous_sidecar_does_not_raise():
    assert _restore_run_time_fields(_payload(), {}) == []


# ===== 목록이 낡는 것을 막는 계약 =====


def test_the_preserved_section_list_matches_the_collectors_that_ignore_the_target_date():
    """`db_metrics.collect()`에서 **`target`을 안 쓰는** 절이 곧 「지금 상태」 절이다.

    판별은 두 겹이다: 시그니처가 `_target`(파이썬의 「안 쓴다」 관례)인가 **그리고** 본문에
    `target`이 한 번도 안 나오는가. 이름만 보면 `slack_alerts`처럼 `target`으로 적고 안 쓰는
    경우를 놓치고(08-19에 실제로 그랬다), 본문만 보면 시그니처의 이름이 검사에 안 걸린다.

    새 싱글턴 절이 추가되면 이 테스트가 먼저 깨진다 — 그러지 않으면 그 절은 다음 재생성에서
    조용히 지금 값으로 덮이고, **그 사실은 아무 데도 안 남는다.**
    """
    from mahdi.ops import db_metrics

    source = inspect.getsource(db_metrics.collect)
    ignores_target = set()
    for key, fname in re.findall(r'\(\s*"([a-z_]+)"\s*,\s*([\w.]+)\s*\)', source):
        fn = getattr(db_metrics, fname, None)
        if fn is None:
            continue
        body = inspect.getsource(fn).split(chr(10), 1)[-1]  # 시그니처 줄을 뺀 본문
        if "target" not in body:
            ignores_target.add(f"db.{key}")

    declared = {s for s in _RUN_TIME_SECTIONS if s.startswith("db.")}
    assert declared == ignores_target, (
        f"보존 목록이 실제 수집기와 갈라졌다: 빠짐 {ignores_target - declared} / "
        f"남음 {declared - ignores_target} — "
        "`target`을 안 쓰는 절은 재생성이 복원할 수 없으므로 반드시 보존 목록에 있어야 한다"
    )


def test_a_collector_that_ignores_the_date_says_so_in_its_signature():
    """안 쓰는 인자는 `_target`으로 적는다 — 그 이름이 위 테스트의 사람 쪽 짝이다.

    08-19에 `slack_alerts`가 `target`으로 적고 안 쓰고 있었다. 이름만으로는 「날짜를 보는
    절」로 읽히고, 그래서 보존 목록에서 빠질 뻔했다.
    """
    from mahdi.ops import db_metrics

    source = inspect.getsource(db_metrics.collect)
    wrong = []
    for key, fname in re.findall(r'\(\s*"([a-z_]+)"\s*,\s*([\w.]+)\s*\)', source):
        fn = getattr(db_metrics, fname, None)
        if fn is None:
            continue
        params = list(inspect.signature(fn).parameters)
        body = inspect.getsource(fn).split(chr(10), 1)[-1]  # 시그니처 줄을 뺀 본문
        if ("target" not in body) != (params[1] == "_target"):
            wrong.append((key, params[1]))
    assert not wrong, (
        f"인자 이름이 실제 사용과 어긋난 수집기: {wrong} — "
        "날짜를 안 보면 `_target`, 보면 `target`으로 적는다"
    )


@pytest.mark.parametrize("path", list(_CUMULATIVE_LEAVES) + list(_CUMULATIVE_ROW_LEAVES))
def test_cumulative_paths_name_a_db_section(path):
    """오타가 나면 이 모듈은 **조용히 아무것도 보존하지 않는다** — 그래서 모양부터 못 박는다."""
    assert path.startswith("db.") and path.count(".") >= 2
