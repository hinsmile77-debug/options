"""마감 직전(15:00~15:20) 전멸 축 — 2026-09-11 장후 (09-11 §3-1).

## 이 파일이 존재하는 이유

09-11의 전멸 3분(15:01 · 15:02 · 15:20)과 09-10의 전멸 3분(15:00~15:02)은 **시각이 ±1분,
분량이 같다.** 09-11 장후가 그것을 "우연이 아니라 패턴"으로 읽었는데, 그 비교를 할 수 있었던
이유는 **사람이 어제 보고서 산문을 읽었기 때문**이다. 사이드카에 있는 것은 하루 전체를
뭉뚱그린 `cycles.zero_row_minutes`뿐이라 「마감 직전에 몰렸는가」가 표에서 안 보인다.

09-10 고도화 3(`close_shift`)이 고친 것과 같은 형태의 결손이다 — 판단의 재료가 산문에만
있으면 다음날 재현이 안 되고, 로그는 이틀치만 남는다.

## 무엇을 지키는가

① 창 안의 전멸만 세는가 · ② 최장 연속이 맞는가 · ③ **규약 C** — 표본이 없던 날과 전멸이
0건이던 날이 갈리는가 · ④ **판정 무변경** — 이 축을 붙여도 기존 `zero_row_minutes`를 비롯한
사이클 지표가 한 글자도 안 바뀌는가.

⛔ 이 파일은 **임계를 시험하지 않는다.** 「이틀 연속이면 구조적 패턴」은 사람이 가설로 판정할
문장이고(`2026-09-11-close-window-zero-rows-axis`), 이 축에는 경보선이 없다.
"""

from __future__ import annotations

from mahdi.ops import log_metrics


def _cycle(hhmm: str, rows: int) -> dict:
    """사이클 한 건. `start`는 그날 0시부터의 초 — 파서가 내는 것과 같은 축이다."""
    hours, minutes = hhmm.split(":")
    start = int(hours) * 3600 + int(minutes) * 60
    return {"start": start, "rest": 30.0, "end": start + 30.0, "rows": rows,
            "slip": 0.0, "poll_minute": hhmm}


def _day(zero_minutes: tuple[str, ...], span: range = range(0, 21)) -> list[dict]:
    """15시대 하루 — `zero_minutes`만 적재 0행이고 나머지는 정상이다."""
    return [_cycle("15:%02d" % m, 0 if "15:%02d" % m in zero_minutes else 20) for m in span]


# ===== ① 창 안의 전멸만 센다 =====


def test_counts_only_the_close_window():
    """09-11 실측 그대로 — 15:01 · 15:02 · 15:20 셋이 창 안이다."""
    out = log_metrics._close_window_zero_rows(_day(("15:01", "15:02", "15:20")))

    assert out["rows_zero_minutes"] == ["15:01", "15:02", "15:20"]
    assert out["rows_zero_count"] == 3
    assert out["cycles"] == 21


def test_minutes_outside_the_window_are_not_counted():
    """14:59와 15:21은 창 밖이다 — 마감 직전에 몰렸는지를 묻는 축이므로 섞이면 안 된다."""
    cycles = [_cycle("14:59", 0), _cycle("15:10", 0), _cycle("15:21", 0), _cycle("15:30", 0)]
    out = log_metrics._close_window_zero_rows(cycles)

    assert out["rows_zero_minutes"] == ["15:10"]
    assert out["cycles"] == 1


def test_1520_is_inside_the_window():
    """09-11의 전멸 한 건이 **정확히 15:20**이다. 15:20에서 끊으면 3분 중 1분이 창 밖으로
    나가 어제와 다른 잣대가 된다 — 그래서 창은 15:20:59까지다."""
    out = log_metrics._close_window_zero_rows([_cycle("15:20", 0)])
    assert out["rows_zero_minutes"] == ["15:20"]


def test_the_window_is_printed_alongside_the_value():
    """창을 산출물에 함께 인쇄한다 — 나중에 창을 옮기면 옛 사이드카와 값이 왜 갈리는지가
    지표 자신에게 적혀 있어야 한다(`congested`·`close_shift`와 같은 규약)."""
    assert log_metrics._close_window_zero_rows([])["window"] == ["15:00", "15:21"]


# ===== ② 최장 연속 =====


def test_max_consecutive_is_the_longest_run_not_the_total():
    """09-11은 2분 연속 + 단발 1건이다. 총 3건과 최장 2분은 **다른 것을 말한다** —
    전자는 크기, 후자는 그것이 한 덩어리였는가다."""
    out = log_metrics._close_window_zero_rows(_day(("15:01", "15:02", "15:20")))
    assert out["rows_zero_count"] == 3
    assert out["max_consecutive"] == 2


def test_max_consecutive_reads_09_10_shape():
    """09-10은 15:00~15:02 **연속 3분**이었다 — 같은 총량(3)이지만 한 덩어리다."""
    out = log_metrics._close_window_zero_rows(_day(("15:00", "15:01", "15:02")))
    assert out["rows_zero_count"] == 3
    assert out["max_consecutive"] == 3


def test_scattered_minutes_never_form_a_run():
    out = log_metrics._close_window_zero_rows(_day(("15:03", "15:07", "15:15")))
    assert out["max_consecutive"] == 1


# ===== ③ 규약 C — 「없었다」와 「안 셌다」를 가른다 =====


def test_empty_day_still_carries_the_key():
    """사이클이 0건인 날에도 키가 실린다(규약 C)."""
    out = log_metrics._cycle_metrics([], [], [])
    assert "close_window" in out
    assert out["close_window"]["rows_zero_count"] == 0
    assert out["close_window"]["cycles"] == 0


def test_no_sample_and_no_failure_are_different_cells():
    """⛔ 이 축의 핵심. 둘 다 `rows_zero_count == 0`이지만 `cycles`가 갈라 준다 —
    「그날 마감이 안 막혔다」와 「그 창에 사이클이 아예 없었다」는 다른 사실이다."""
    quiet = log_metrics._close_window_zero_rows(_day(()))
    absent = log_metrics._close_window_zero_rows([])

    assert quiet["rows_zero_count"] == absent["rows_zero_count"] == 0
    assert quiet["cycles"] == 21
    assert absent["cycles"] == 0


# ===== ④ 판정 무변경 =====


def test_existing_zero_row_minutes_is_untouched():
    """⛔ 하루 전체 축은 08-10이 **세 원인**(사이클 0행 / 사이클 부재 / 행이 이웃 분으로 감)을
    가르려고 만든 것이다. 새 축은 그 부분집합을 따로 인쇄할 뿐 그 축을 좁히지 않는다."""
    cycles = [_cycle("10:15", 0), _cycle("13:40", 0)] + _day(("15:01", "15:02", "15:20"))
    out = log_metrics._cycle_metrics(cycles, [], [])

    # 하루 전체 축은 창 밖 2건을 **그대로** 갖고 있다.
    assert out["zero_row_minutes"] == ["10:15", "13:40", "15:01", "15:02", "15:20"]
    # 새 축은 창 안 3건만 본다.
    assert out["close_window"]["rows_zero_minutes"] == ["15:01", "15:02", "15:20"]


def test_other_cycle_metrics_do_not_move():
    """이 축은 **세는 것만** 한다 — 같은 입력의 다른 칸이 움직이면 안 된다."""
    cycles = _day(("15:01", "15:02", "15:20"))
    out = log_metrics._cycle_metrics(cycles, [], [])

    assert out["count"] == 21
    assert out["rows_distribution"] == {0: 3, 20: 18}
    assert out["minutes_with_cycle"] == ["15:%02d" % m for m in range(21)]
