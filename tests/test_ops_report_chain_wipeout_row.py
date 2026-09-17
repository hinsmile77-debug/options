"""§1 「한눈에」가 **옵션체인 완전 전멸(ERROR)** 건수를 인쇄한다 (2026-09-17 제4부 Fix C).

09-17 15:00:52·15:01:53에 옵션체인이 그 분 20레그 중 0행만 적재했다 — 그날 유일한 ERROR
로그 2건이다. 그 2분 동안 판단은 신선도 창 안의 **직전 스냅샷**을 보고 났다. 폴백은 설계대로
돌았지만 그 사실은 §5-1 **산문에만** 있었고, §1만 읽는 사람은 그날을 평범한 날로 읽는다.

이 파일이 지키는 것 넷:

    1. **행이 실린다** — 판단 입력 표에, DB 줄들과 같은 표에.
    2. **정의가 겹치지 않는다** — 리포트 자신이 적은 회귀 위험이 이것이다. 이 행은 **ERROR
       로그 건수**를 세고 기존 두 행은 **DB `rows=0` 분 수**를 센다.
    3. **규약 C** — 전멸 0건인 날은 `—`가 아니라 **`0건`**이다. `—`는 「재지 않았다」이고,
       그것을 「좋았다」로 읽은 것이 08-14 사고다.
    4. ⛔ **판정 무변경** — 기존 네 DB 줄과 인프라 표는 칸 단위로 그대로다. 임계도 없다.
"""

from __future__ import annotations

from mahdi.ops import log_metrics, report


_TODAY = {
    "date": "2026-09-17",
    "rest": {"total_calls": 1000, "calls_per_second": 0.3},
    "cycles": {"count": 493},
    "qualitative": {"chain_cycle_empty": 2},
}


def _judgement_input_db(**overrides):
    db = {
        "monthly_coverage": {"coverage_pct": 95.1},
        "signal_reach": {"gex_input_missing_minutes": 3},
        "chain_minute_coverage": {
            "zero_row_longest_run": {"length": 2},
            "zero_row_isolated_count": 0,
        },
    }
    db.update(overrides)
    return db


def _rows_of(text: str) -> list[list[str]]:
    rows = [
        [c.strip() for c in line.strip().strip("|").split("|")]
        for line in text.splitlines()
        if line.startswith("|")
    ]
    return [r for r in rows if set("".join(r)) != {"-"}]     # 구분선은 뺀다


# ---------------------------------------------------------------- 행이 실린다
def test_the_wipeout_count_reaches_the_headline():
    """이 항목의 요점 — 09-17의 「2건」이 §1에서 보인다."""
    text = "\n".join(report._render_headline(_TODAY, None, _judgement_input_db()))

    assert "옵션체인 전멸(ERROR)" in text
    assert ["**옵션체인 전멸(ERROR)**", "2건"] in _rows_of(text)


def test_the_row_sits_in_the_judgement_table_not_the_infrastructure_one():
    """전멸은 **판단이 볼 것을 봤는가**의 문제다 — 인프라 표가 아니라 판단 입력 표에 선다."""
    text = "\n".join(report._render_headline(_TODAY, None, _judgement_input_db()))

    assert text.index("판단 입력") < text.index("옵션체인 전멸(ERROR)")


def test_the_row_carries_the_previous_day_delta():
    previous = {"date": "2026-09-16", **_TODAY,
                "qualitative": {"chain_cycle_empty": 0},
                "db": _judgement_input_db()}

    text = "\n".join(report._render_headline(_TODAY, previous, _judgement_input_db()))
    row = next(r for r in _rows_of(text) if r[0] == "**옵션체인 전멸(ERROR)**")
    assert row[1] == "2건" and row[2] == "0건"
    assert "▲" in row[3]              # 전멸이 는 것은 나빠진 것이다


# ---------------------------------------------------------------- 규약 C
def test_a_quiet_day_prints_zero_not_a_dash():
    """규약 C — **0건으로 찍혀야** 「전멸이 없었다」와 「그 줄이 없던 버전」이 갈린다."""
    calm = {**_TODAY, "qualitative": {"chain_cycle_empty": 0}}
    text = "\n".join(report._render_headline(calm, None, _judgement_input_db()))

    row = next(r for r in _rows_of(text) if r[0] == "**옵션체인 전멸(ERROR)**")
    assert row[1] == "0건"
    assert row[1] != "—"


def test_the_axis_is_registered_as_always_present():
    """위 0이 **실제로 나오게 하는 것**이 이 등록이다 — 없으면 대부분의 날 키가 빠진다."""
    assert "chain_cycle_empty" in log_metrics._QUALITATIVE_ALWAYS_PRESENT


def test_an_old_sidecar_without_the_key_still_says_it_was_not_measured():
    """키가 없는 옛 산출물을 다시 그리면 `—`다 — 그 날은 정말 **안 쟀다.**"""
    old = {k: v for k, v in _TODAY.items() if k != "qualitative"}
    text = "\n".join(report._render_headline(old, None, _judgement_input_db()))

    row = next(r for r in _rows_of(text) if r[0] == "**옵션체인 전멸(ERROR)**")
    assert row[1] == "—"


# ---------------------------------------------------------------- 정의가 겹치지 않는다
def test_the_two_definitions_are_spelled_out_for_the_reader():
    """리포트가 적은 회귀 위험 — 「정의가 겹치지 않게」를 사람이 읽을 자리에 남긴다."""
    text = "\n".join(report._render_headline(_TODAY, None, _judgement_input_db()))

    assert "다른 것을 센다" in text
    assert "ERROR 로그 건수" in text
    assert "`rows=0`으로 남은 분 수" in text


def test_the_db_axis_is_untouched_by_this_row():
    """두 축은 같이 움직이지 않는다 — 전멸 건수를 바꿔도 DB 0행 줄은 그대로다."""
    db = _judgement_input_db()
    quiet = "\n".join(report._render_headline(
        {**_TODAY, "qualitative": {"chain_cycle_empty": 0}}, None, db))
    loud = "\n".join(report._render_headline(
        {**_TODAY, "qualitative": {"chain_cycle_empty": 9}}, None, db))

    def db_rows(text):
        return [r for r in _rows_of(text) if r[0] != "**옵션체인 전멸(ERROR)**"]

    assert db_rows(quiet) == db_rows(loud)


# ---------------------------------------------------------------- 판정 무변경
def test_the_existing_four_rows_are_cell_for_cell_unchanged():
    """⛔ 기존 DB 네 줄은 칸 단위로 그대로이고, 새 행은 **뒤에만** 덧붙는다."""
    text = "\n".join(report._render_headline(_TODAY, None, _judgement_input_db()))
    rows = _rows_of(text)
    judgement = rows[rows.index(["지표", "오늘"]) + 1:]
    # 헤더가 두 번 나온다(인프라 표 · 판단 입력 표) — 뒤쪽 표만 본다.
    judgement = judgement[judgement.index(["지표", "오늘"]) + 1:]

    assert [r[0] for r in judgement] == [
        "**먼슬리 절대 커버리지**", "**GEX 입력 없던 분**",
        "**최장 연속 0행 구간**", "**단발 완전실패**",
        "**옵션체인 전멸(ERROR)**",          # ← 새 행은 맨 뒤다
    ]
    assert [r[1] for r in judgement[:4]] == ["95.1%", "3분", "2분", "0건"]


def test_this_row_declares_no_threshold():
    """⛔ 몇 건부터 위험한가는 긋지 않았다 — 세는 것이 전부다."""
    assert report.HEADLINE_JUDGEMENT_LOG_METRICS == [
        ("**옵션체인 전멸(ERROR)**", "qualitative.chain_cycle_empty", "{:,.0f}건", "down"),
    ]


def test_the_row_survives_a_day_with_no_db_metrics_at_all():
    """DB 집계가 통째로 없는 날에도 이 축은 살아 있다 — 같이 묻으면 규약 C를 스스로 어긴다."""
    text = "\n".join(report._render_headline(_TODAY, None, {"tables": []}))

    assert f"판단 입력 {len(report.HEADLINE_DB_METRICS)}행이 이 집계에 없다" in text
    assert ["**옵션체인 전멸(ERROR)**", "2건"] in _rows_of(text)
