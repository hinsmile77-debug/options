"""정규장 마감(15:20) 전후 REST수집 낙폭 — 2026-09-10 제5부 고도화 3 (09-10 §3-1).

## 이 파일이 존재하는 이유

09-10 장후 회차는 그날의 가장 직접적인 증거를 **산문으로만** 남겼다: 14시대 REST수집 평균
37~42초가 마감 직후 19~20초로 떨어졌고, 그것이 그날 혼잡을 KIS(장 참여자 총량)에 귀속시킨
근거였다. 그 수를 얻기 위해 회차가 `logs/observation_loop.log`를 손으로 grep해 세 줄
(15:30:36 · 15:31:19 · 15:32:18)을 인용했다 — **다음날 아무도 그 수를 재현할 수 없다.**

로그는 이틀치만 남는다. 재현이 안 되는 수는 며칠 뒤에 근거가 아니라 기억이 된다.

## 무엇을 지키는가

① 낙폭이 실제로 계산되는가 · ② **규약 C** — 표본이 없는 쪽은 `0.0`이 아니라 `None`인가 ·
③ **판정 무변경** — 이 축을 붙여도 기존 사이클 지표(`rest_seconds`·`congested`·`count`)가
한 글자도 안 바뀌는가.
"""

from __future__ import annotations

from mahdi.ops import log_metrics, report


def _cycle(hhmm: str, rest: float) -> dict:
    """사이클 한 건. `start`는 그날 0시부터의 초 — 파서가 내는 것과 같은 축이다."""
    hours, minutes = hhmm.split(":")
    start = int(hours) * 3600 + int(minutes) * 60
    return {"start": start, "rest": rest, "end": start + rest, "rows": 20,
            "slip": 0.0, "poll_minute": hhmm}


# ===== ① 낙폭이 계산되는가 =====


def test_drop_pct_is_the_median_fall_across_the_close():
    """09-10의 형태 — 마감 전이 느리고 마감 후가 빠르면 낙폭은 양수다."""
    cycles = [_cycle("14:%02d" % m, 40.0) for m in range(10)]
    cycles += [_cycle("15:%02d" % m, 20.0) for m in range(20, 30)]
    out = log_metrics._close_shift_cycle_seconds(cycles)

    assert out["before_p50"] == 40.0
    assert out["after_p50"] == 20.0
    assert out["drop_pct"] == 50.0
    assert out["before_cycles"] == 10
    assert out["after_cycles"] == 10


def test_the_window_is_printed_alongside_the_value():
    """창을 산출물에 함께 인쇄한다 — 나중에 창을 옮기면 옛 사이드카와 왜 갈리는지가
    지표 자신에게 적혀 있어야 한다(`CONGESTED_HOURS`와 같은 규약, 09-02 P1-5)."""
    out = log_metrics._close_shift_cycle_seconds([])
    assert out["before_window"] == ["13:20", "15:20"]
    assert out["after_from"] == "15:20"


def test_the_boundary_minute_belongs_to_after():
    """15:20 정각 사이클은 **마감 후**다. 경계가 양쪽에 걸치면 낙폭이 스스로를 희석한다."""
    out = log_metrics._close_shift_cycle_seconds([_cycle("15:20", 20.0), _cycle("15:19", 40.0)])
    assert out["before_cycles"] == 1
    assert out["after_cycles"] == 1


def test_morning_cycles_are_outside_the_before_window():
    """마감 전 창은 하루 전체가 아니라 **직전 2시간**이다 — 오전의 한산한 구간이 분모에
    섞이면 낙폭이 과소평가된다(§3-1이 비교한 것도 「14시대」다)."""
    out = log_metrics._close_shift_cycle_seconds([_cycle("09:30", 20.0), _cycle("14:00", 40.0)])
    assert out["before_cycles"] == 1
    assert out["before_p50"] == 40.0


# ===== ② 규약 C — 표본 없음은 0이 아니다 =====


def test_no_cycles_at_all_yields_none_not_zero():
    """장이 안 선 날·기동 실패한 날. `0.0`을 내면 「낙폭이 없었다」와 같은 칸이 된다."""
    out = log_metrics._close_shift_cycle_seconds([])
    assert out["before_p50"] is None
    assert out["after_p50"] is None
    assert out["drop_pct"] is None
    assert out["before_cycles"] == 0
    assert out["after_cycles"] == 0


def test_early_close_leaves_drop_pct_none():
    """마감 전에 관측이 끝난 날 — 마감 후 표본이 없으면 낙폭은 **모른다**이지 0이 아니다."""
    out = log_metrics._close_shift_cycle_seconds([_cycle("14:00", 40.0)])
    assert out["before_p50"] == 40.0
    assert out["after_p50"] is None
    assert out["drop_pct"] is None


def test_zero_before_median_does_not_divide_by_zero():
    """마감 전 중앙값이 0인 날(전멸 등)은 비율이 정의되지 않는다 — 터지지 말고 None."""
    out = log_metrics._close_shift_cycle_seconds([_cycle("14:00", 0.0), _cycle("15:30", 20.0)])
    assert out["before_p50"] == 0.0
    assert out["drop_pct"] is None


def test_key_is_present_even_on_a_day_with_no_cycles_at_all():
    """규약 C — 사이클이 0건인 날도 `cycles.close_shift`가 실린다. 키가 빠지면
    「낙폭이 없었다」와 「안 셌다」가 같은 칸이 된다."""
    empty = log_metrics._cycle_metrics([], [], [])
    assert "close_shift" in empty
    assert empty["close_shift"]["drop_pct"] is None


def test_a_slower_close_gives_a_negative_drop():
    """마감 후가 오히려 느린 날도 있다. 그것을 0으로 접지 않고 **음수**로 낸다 —
    부호가 사라지면 「마감이 안 풀어 줬다」는 사실이 안 남는다."""
    out = log_metrics._close_shift_cycle_seconds([_cycle("14:00", 20.0), _cycle("15:30", 40.0)])
    assert out["drop_pct"] == -100.0


# ===== ③ 판정 무변경 =====


def test_existing_cycle_metrics_are_untouched():
    """이 축을 붙여도 기존 사이클 지표는 한 글자도 안 바뀐다.

    09-10 실측으로도 확인했다 — HEAD 수집기와 새 수집기를 같은 로그(`--date 2026-09-10`)에
    돌려 사이드카를 통째로 대조했고, 차이는 `cycles.close_shift` **한 키의 추가**뿐이었다
    (나머지는 재실행 시각 때문에 움직이는 `watchdog.*`·`delta_baseline.sidecar_found`).
    """
    cycles = [_cycle("14:%02d" % m, 40.0) for m in range(10)]
    cycles += [_cycle("15:%02d" % m, 20.0) for m in range(20, 30)]
    out = log_metrics._cycle_metrics(list(cycles), [], [])

    assert out["count"] == 20
    assert out["rest_seconds"]["mean"] == 30.0
    # 혼잡 시간대(10~14시) 축은 마감 후 사이클을 **한 건도** 주워 오지 않는다.
    assert out["congested"]["cycles"] == 10
    assert out["congested"]["rest_p50"] == 40.0


def test_headline_row_prints_a_dash_when_the_axis_is_none():
    """§1 한눈에 표 — 값이 없는 날 「0.0%」가 아니라 「—」로 찍혀야 한다.
    0으로 찍으면 「마감이 아무것도 안 풀어 줬다」로 읽힌다."""
    rendered = "\n".join(report._render_headline({"cycles": {"close_shift": {"drop_pct": None}}}, None))
    assert "└ 마감 전후 낙폭 | —" in rendered


def test_headline_row_is_listed_after_the_rest_average():
    """이 줄은 `REST수집 평균`의 **자식**이다 — 떨어뜨려 놓으면 무엇의 낙폭인지가 안 보인다."""
    labels = [label for label, *_ in report.HEADLINE_METRICS]
    assert labels[labels.index("REST수집 평균") + 1] == "└ 마감 전후 낙폭"


def test_the_axis_carries_no_threshold():
    """⛔ 이 축에는 경보선이 없다. 「낙폭 N% 넘으면 KIS 귀속」을 그으면 08-16이 하루로
    지표를 정했다가 뒤집힌 것과 같은 실수가 된다 — 귀속 판정은 §17이 §6과 맞춰 본다.
    개선 방향(`direction`)도 `None`이어야 한다(규약 G: 이 축은 그날 KIS 상태에 비례한다)."""
    row = [spec for spec in report.HEADLINE_METRICS if spec[1] == "cycles.close_shift.drop_pct"]
    assert len(row) == 1
    assert row[0][3] is None
