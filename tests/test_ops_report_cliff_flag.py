"""§0-1 가설 검정 표가 **절벽일에 오염 주의를 병기한다** (2026-09-03 제5부 고도화 3).

09-03 §1-1 — 두 가설의 지표가 실제로 절벽에 오염됐다.
`2026-08-12-eE-on-congested-hours`의 대가 축 `gex_input_missing_minutes`가 50분(기준선 4분)
이었는데 그 대부분이 절벽 구간과 겹치고, `2026-09-02-fix10-phase-offset-55s`의 주장 축
`chain_newest_age_seconds.p50` 57.4초도 같은 이유로 순수하게 못 읽는다.
**오염을 사람이 매번 기억해서 발라내야 하는 상태**였고, 그 기억이 빠지는 날 규약 E가
막으려는 것(대가를 다른 사건의 값으로 재는 것)이 그대로 일어난다.

⚠ **새 축도 새 임계도 만들지 않았다.** 최장 연속 0행 축은 08-14 Fix#3으로 이미 있었고
(`db.chain_minute_coverage.zero_row_longest_run.length`), 임계도
`db_metrics.ZERO_ROW_RUN_ALERT_MINUTES = 20`으로 이미 정해져 있었다. 있는 것을 읽는다.

⛔ **판정을 무르지 않는다.** 표의 `판정` 열은 한 글자도 안 바뀐다.
"""

from __future__ import annotations

from mahdi.ops import db_metrics, report

_RESULTS = [
    {
        "id": "2026-09-02-fix10-phase-offset-55s",
        "가설": "판단 위상을 :55초로 옮기면 그 분의 체인으로 GEX를 낸다",
        "metric": "db.signal_reach.chain_newest_age_seconds.p50",
        "actual": 57.4, "expect": "<= 55.0", "역할": "주장", "verdict": "반증",
    },
]


def _db(run_minutes: int | None) -> dict:
    if run_minutes is None:  # 08-14 Fix#3 이전 집계 — 그 키가 아예 없다
        return {"chain_minute_coverage": {"available": True}}
    return {
        "chain_minute_coverage": {
            "available": True,
            "zero_row_longest_run": {
                "length": run_minutes, "start": "14:31", "end": "15:23",
            },
        }
    }


def _flag_lines(db: dict | None) -> list[str]:
    return [ln for ln in report._render_hypotheses(_RESULTS, db) if "절벽 발생일" in ln]


# ===== 뜨는 날 =====


def test_a_cliff_day_is_announced_above_the_table():
    """09-03 실측 재현 — 53분(14:31~15:23)."""
    lines = report._render_hypotheses(_RESULTS, _db(53))
    flags = [i for i, ln in enumerate(lines) if "절벽 발생일" in ln]
    table = [i for i, ln in enumerate(lines) if ln.startswith("| id ")]

    assert flags, "절벽일인데 아무 말도 없으면 이 항목이 존재할 이유가 없다"
    assert flags[0] < table[0], "표 아래에 적으면 사람이 이미 숫자를 읽은 뒤다"
    assert "53분" in lines[flags[0]]


def test_the_flag_cites_the_shared_threshold():
    """임계를 이 항목이 새로 정하지 않았다는 것이 문구에 보여야 한다."""
    line = _flag_lines(_db(53))[0]

    assert f"{db_metrics.ZERO_ROW_RUN_ALERT_MINUTES}분" in line


def test_the_flag_tells_people_to_remeasure_not_to_pass():
    """**판정을 무르라는 말이 아니다** — 절벽 없는 날에 다시 재라는 말이다."""
    line = _flag_lines(_db(53))[0]

    assert "재실측" in line
    assert "규약 E" in line


def test_exactly_at_the_threshold_counts():
    assert _flag_lines(_db(db_metrics.ZERO_ROW_RUN_ALERT_MINUTES))


# ===== 안 뜨는 날 — 없는 근거로 주의를 띄우는 것이 소음이다 =====


def test_a_calm_day_says_nothing():
    for run in (0, 1, 19):
        assert not _flag_lines(_db(run)), run


def test_a_day_without_db_metrics_says_nothing():
    """DB를 안 돌린 날은 「절벽이 없었다」가 아니라 「모른다」다 — 그래도 안 띄운다.
    규약 C의 「측정 불가」 구분은 §4의 `_render_zero_row_run`이 그 자리에서 말한다."""
    for db in (None, {}, _db(None)):
        assert not _flag_lines(db), db


# ===== 판정 무변경 =====


def test_the_verdict_column_is_untouched_on_a_cliff_day():
    """플래그는 표 **위의 한 줄**이다. 자동으로 판정을 무르면 그것이야말로
    「숫자를 보고 기준을 고치는 것」이다."""
    calm = [ln for ln in report._render_hypotheses(_RESULTS, _db(0))]
    cliff = [ln for ln in report._render_hypotheses(_RESULTS, _db(53))]

    def rows(lines):
        return [ln for ln in lines if ln.startswith("| ")]

    assert rows(calm) == rows(cliff), "표가 한 글자라도 달라지면 판정을 무른 것이다"


def test_a_calm_day_renders_byte_identical_to_before_this_change():
    """**회귀 판정선** — 절벽이 없던 날의 §0-1 출력은 한 글자도 안 바뀐다."""
    assert report._render_hypotheses(_RESULTS) == report._render_hypotheses(_RESULTS, _db(0))
