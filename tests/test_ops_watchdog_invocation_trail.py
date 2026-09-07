"""워치독 **호출 공백**을 판정 공백과 가른다 (2026-09-07 Fix A / 09-07 §1-1).

09-07에 07:30 정규 기동이 안 떴고 워치독이 08:00:47에 대체 기동했다. 그 30분이
「작업 스케줄러가 워치독을 안 불렀다」인지 「불렸는데 남길 것이 없었다」인지 **그날 네 회차가
전부 답하지 못했다.** 답할 자료가 없었기 때문이다:

  - `watchdog.log`는 정상일에 `OK`를 10분에 한 줄만 남긴다 → 해상도가 10분이다.
  - `.watchdog_last_check.json`은 매번 갱신되지만 **덮어쓴다** → 과거를 못 묻는다.

그래서 「불렸다」만 담는 append-only 흔적을 따로 둔다. 이 파일이 지키는 것은 넷이다.
1. 흔적이 있으면 호출 공백이 **판정 공백과 다른 값**으로 나온다.
2. 흔적이 없으면 **0이 아니라 「모른다」**로 실린다(규약 C).
3. **판정 무변경** — `parse()`가 내던 키는 이름도 값도 그대로다.
4. 흔적은 `watchdog.log`를 한 줄도 늘리지 않는다(규약 E — 대가는 전용 축으로).
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from mahdi import liveness
from mahdi.ops import watchdog_metrics

DAY = date(2026, 9, 7)
TAB = "\t"


def _trail(hhmmss: str, action: str = "ok", day: str = "2026-09-07") -> str:
    return f"{day}{TAB}{hhmmss}{TAB}{action}"


def _minutewise(start_min: int, end_min: int, action: str = "ok") -> list[str]:
    """감시 창 안을 1분 간격으로 채운다 — 정상일의 모양이다."""
    return [
        _trail(f"{m // 60:02d}:{m % 60:02d}:01", action)
        for m in range(start_min, end_min + 1)
    ]


# ===== 1. 두 축이 갈린다 =====


def test_a_normal_day_has_a_small_invocation_gap_even_though_the_log_is_quiet():
    """이 fix의 전부 — **기록은 10분씩 조용해도 호출은 1분마다 있었다.**

    이 두 값이 같은 하루에서 다르게 나오는 것이 「불렸는데 남길 것이 없었다」의 증거다.
    """
    start = 7 * 60 + 40
    end = 15 * 60 + 45
    parsed = watchdog_metrics.parse_trail(_minutewise(start, end), DAY)

    assert parsed["invocation_trail_available"] is True
    # 창 끝(15:45:00)보다 1초 늦은 마지막 호출은 창 밖이다 — 그래서 `+1`이 아니다.
    assert parsed["invocations_in_window"] == end - start
    assert parsed["max_invocation_gap_minutes"] <= 1.1
    # 규약 F — 주장 지표는 배수로 잰다(창 길이·호출 주기가 분모에서 약분된다).
    assert parsed["invocation_gap_over_cadence_ratio"] <= 1.1


def test_the_09_07_morning_hole_shows_up_as_an_invocation_gap():
    """09-07 07:40~08:00 — 호출이 실제로 없었다면 이 축이 그것을 말한다.

    같은 20분을 `parse()`는 「기록 공백 20.8분」으로도 낸다. **두 값이 함께 커야** 원인이
    스케줄러 쪽으로 좁혀진다 — 이 축 하나로 단정하지 않는다.
    """
    parsed = watchdog_metrics.parse_trail(_minutewise(8 * 60, 15 * 60 + 45), DAY)

    assert parsed["max_invocation_gap_minutes"] == pytest.approx(20.0, abs=0.1)
    assert parsed["max_invocation_gap_window"] == "07:40~08:00"
    assert parsed["invocation_gap_over_cadence_ratio"] > watchdog_metrics.INVOCATION_GAP_WARN_MINUTES


def test_only_the_target_day_is_counted():
    """어제 줄이 남아 있어도 오늘 판정에 안 섞인다(파일 회전이 늦은 날)."""
    parsed = watchdog_metrics.parse_trail(
        [_trail("09:00:01", day="2026-09-04"), _trail("09:01:01"), _trail("09:02:01")], DAY
    )

    assert parsed["invocations"] == 2
    assert parsed["invocation_first_at"] == "09:01:01"


def test_the_action_is_kept_so_idle_and_ok_stay_distinguishable():
    """「불렸지만 감시 창 밖이었다」와 「불려서 정상이라고 판정했다」는 다른 사실이다."""
    parsed = watchdog_metrics.parse_trail(
        [_trail("06:00:01", "idle"), _trail("09:00:01", "ok"), _trail("09:01:01", "ok")], DAY
    )

    assert parsed["invocation_actions"] == {"idle": 1, "ok": 2}
    # 창 밖 호출도 `invocations`에는 실린다 — 「스케줄러가 살아 있는가」는 창과 무관하다.
    assert parsed["invocations"] == 3 and parsed["invocations_in_window"] == 2


def test_a_malformed_row_is_skipped_not_fatal():
    """형식이 어긋난 줄 하나가 그날 계측 전체를 죽이면 안 된다."""
    parsed = watchdog_metrics.parse_trail(
        ["쓰레기", f"2026-09-07{TAB}못읽는시각{TAB}ok", _trail("09:00:01")], DAY
    )

    assert parsed["invocations"] == 1


# ===== 2. 흔적이 없는 날은 「0」이 아니라 「모른다」 (규약 C) =====


def test_a_missing_trail_reads_as_unknown_not_as_zero_gap(tmp_path):
    """**0으로 접으면 「호출이 끊긴 적 없다」로 읽힌다** — 그것이 이 축이 막으려는 오독이다."""
    result = watchdog_metrics.collect_trail(tmp_path, DAY)

    assert result["invocation_trail_available"] is False
    assert result["max_invocation_gap_minutes"] is None
    assert result["invocations"] is None
    # 눈금 자체는 실린다 — 사람이 「무엇으로 잴 뻔했는가」를 알아야 한다.
    assert result["invocation_cadence_minutes"] == 1.0


def test_a_present_but_empty_trail_means_it_was_never_called():
    """파일은 있는데 그날 줄이 0 → **하루 종일 한 번도 안 불렸다.** 위와 다른 사실이다."""
    parsed = watchdog_metrics.parse_trail([_trail("09:00:01", day="2026-09-04")], DAY)

    assert parsed["invocation_trail_available"] is True
    assert parsed["invocations"] == 0
    assert parsed["invocation_first_at"] is None


# ===== 3. 판정 무변경 (B등급 조건) =====


def test_collect_keeps_every_key_the_old_parser_produced(tmp_path):
    """**종전 축은 이름도 값도 안 바뀐다** — 새 키는 전부 `invocation*`이라 겹치지 않는다."""
    log = tmp_path / watchdog_metrics.WATCHDOG_LOG_FILENAME
    lines = [
        "[2026-09-07 08:00:46] RESTART — 관측 루프 생존 신호 이상(no_heartbeat) — x",
        "[2026-09-07 08:10:02] OK — 정상",
        "[2026-09-07 15:40:02] OK — 정상",
    ]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    plain = watchdog_metrics.parse(lines, DAY)
    merged = watchdog_metrics.collect(tmp_path, DAY)

    for key, value in plain.items():
        assert merged[key] == value, f"종전 축 {key}가 움직였다"
    # 09-07 실측과 같은 모양인지 — 재기동 1회, 판정 3회.
    assert merged["restarts"] == 1 and merged["checks"] == 3
    # 그리고 새 축이 **없는 것이 아니라 「모른다」로** 붙는다.
    assert merged["invocation_trail_available"] is False


def test_the_trail_does_not_add_a_single_line_to_the_watchdog_log(tmp_path):
    """규약 E — 대가는 전용 축으로 잰다.

    `watchdog.log`에 매분 한 줄을 얹으면 하루 1,000줄이고, 그 순간 `checks`와
    `max_silence_minutes`가 통째로 뜻을 잃는다(둘 다 「그 로그의 줄」로 정의돼 있다).
    """
    log = tmp_path / watchdog_metrics.WATCHDOG_LOG_FILENAME
    log.write_text("[2026-09-07 08:10:02] OK — 정상\n", encoding="utf-8")

    for minute in range(30):
        liveness.append_watchdog_check(
            liveness.watchdog_trail_path(tmp_path),
            datetime(2026, 9, 7, 8, minute, 1),
            action="ok",
        )

    assert log.read_text(encoding="utf-8").count("\n") == 1
    assert watchdog_metrics.collect(tmp_path, DAY)["checks"] == 1
    assert watchdog_metrics.collect(tmp_path, DAY)["invocations"] == 30


# ===== 4. 쓰기 쪽 — 하루치만, 그리고 절대 안 죽는다 =====


def test_the_trail_starts_over_when_the_date_rolls(tmp_path):
    """1분 주기 × 24시간 = 1,440줄이다. 날짜가 바뀌면 새로 시작한다."""
    path = liveness.watchdog_trail_path(tmp_path)
    liveness.append_watchdog_check(path, datetime(2026, 9, 4, 15, 44, 1), action="ok")
    liveness.append_watchdog_check(path, datetime(2026, 9, 4, 15, 45, 1), action="ok")
    liveness.append_watchdog_check(path, datetime(2026, 9, 7, 7, 40, 1), action="ok")

    rows = path.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1 and rows[0].startswith("2026-09-07")


def test_appending_never_raises_even_when_the_path_is_unusable(tmp_path):
    """**자기 기록을 못 썼다고 워치독이 멈추면 안 된다** — `write_watchdog_check`와 같은 계약."""
    blocked = tmp_path / "not_a_dir"
    blocked.write_text("x", encoding="utf-8")

    liveness.append_watchdog_check(
        blocked / "trail.tsv", datetime(2026, 9, 7, 7, 40, 1), action="ok"
    )


def test_the_round_trip_lands_on_the_parser(tmp_path):
    """쓰는 쪽과 읽는 쪽이 갈리면 이 축이 **조용히 0을 낸다** — 계약을 여기서 못박는다."""
    path = liveness.watchdog_trail_path(tmp_path)
    for minute in (40, 41, 47):
        liveness.append_watchdog_check(
            path, datetime(2026, 9, 7, 7, minute, 1), action="idle"
        )

    parsed = watchdog_metrics.parse_trail(path.read_text(encoding="utf-8").splitlines(), DAY)

    assert parsed["invocations"] == 3
    assert parsed["invocation_actions"] == {"idle": 3}
    assert parsed["invocation_first_at"] == "07:40:01"
    assert parsed["invocation_last_at"] == "07:47:01"
    # 마지막 호출 뒤로 창 끝(15:45)까지가 최장 공백이다 — `parse()`가 창 경계를 양끝에
    # 붙이는 것과 같은 규약이고, 08-12의 「끝난 뒤 다시 시작하지 않은 것」을 잡는 자리다.
    assert parsed["max_invocation_gap_window"] == "07:47~15:45"


def test_the_filename_is_the_same_on_both_sides(tmp_path):
    """복제한 파일명이 갈라지면 수집기가 영원히 「없음」을 인쇄한다(ANCHORS와 같은 계약).

    쓰는 쪽은 `mahdi/liveness.py`, 읽는 쪽은 `mahdi/ops/watchdog_metrics.py`와
    `docs/동작점검/tools/collect_evidence.py` 셋이다 — 이름이 세 곳에 복제돼 있다.
    """
    assert liveness.watchdog_trail_path(tmp_path).name == watchdog_metrics.TRAIL_FILENAME

    collector = _load_collector()
    assert collector.WATCHDOG_TRAIL_FILENAME == watchdog_metrics.TRAIL_FILENAME


def _load_collector():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "docs" / "동작점검" / "tools" / "collect_evidence.py"
    spec = importlib.util.spec_from_file_location("collect_evidence_watchdog_trail", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
