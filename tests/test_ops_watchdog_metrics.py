"""워치독 로그 파서 — **감시자의 침묵**을 다음 날이 보게 한다 (2026-08-12 §2-3 / Fix#8).

08-12에 워치독은 10:14:01에 판정하고 재기동했다. 그 뒤 `watchdog.log`의 마지막 줄이 「RESTART」라
사고 대응 중인 것처럼 보였는데, 실제로는 재기동 호출이 상속된 파이프에 물려 15:45:02까지
막혀 있었고 그동안 매분 실행이 전부 무시됐다 — **10:20~15:40 사이 `OK` 줄이 한 개도 없다.**

그 사실은 개입 횟수가 아니라 **줄과 줄 사이의 간격**으로만 드러난다.
"""

from __future__ import annotations

from datetime import date

from mahdi.ops import report, watchdog_metrics

_TARGET = date(2026, 8, 12)


def _healthy_day() -> list[str]:
    """정상일 — 07:40~15:45 감시 창을 10분 간격 `OK`로 채운다."""
    lines = []
    for hour in range(7, 16):
        for minute in (0, 10, 20, 30, 40, 50):
            if (hour, minute) < (7, 40) or (hour, minute) > (15, 40):
                continue
            lines.append(f"[2026-08-12 {hour:02d}:{minute:02d}:01] OK — 정상 — 마지막 박동 8초 전")
    return lines


def test_a_healthy_day_has_no_long_silence():
    metrics = watchdog_metrics.parse(_healthy_day(), _TARGET)
    assert metrics["restarts"] == 0
    # 창 경계(07:40 시작 / 15:45 끝)까지 세므로 최장 간격은 10분 근처다.
    assert metrics["max_silence_minutes"] <= watchdog_metrics.SILENCE_WARN_MINUTES
    assert metrics["silence_over_cadence_ratio"] <= 2.0


def test_the_claim_metric_is_normalized_not_a_raw_duration():
    """**규약 F.** 원값(`_minutes`)은 감시 창 길이와 기록 주기에 비례한다 — 그 둘이 분모에서
    약분되는 배수에만 부등식을 건다. 도입 당일 `test_ops_hypotheses`가 이 실수를 잡았다."""
    healthy = watchdog_metrics.parse(_healthy_day(), _TARGET)
    assert healthy["silence_over_cadence_ratio"] == round(
        healthy["max_silence_minutes"] / 10.0, 2
    )


def test_the_2026_08_12_blackout_is_measured():
    """**그날의 재현.** 10:14 이후 줄이 없으면 창 끝까지가 침묵이다.

    이 값이 없으면 「RESTART로 끝난 로그」와 「RESTART 뒤에 멈춘 로그」가 구분되지 않는다.
    """
    lines = [
        line for line in _healthy_day()
        if line < "[2026-08-12 10:14"
    ] + [
        "[2026-08-12 10:14:01] RESTART — 관측 루프 생존 신호 이상(stale) — 생존 신호가 239초째 갱신되지 않음",
        "[2026-08-12 10:14:01] 재기동 시도: 기동 스크립트가 300초 안에 끝나지 않음",
    ]
    metrics = watchdog_metrics.parse(lines, _TARGET)

    assert metrics["restarts"] == 1
    assert metrics["restart_failures"] == 1
    # 10:14 → 15:45(감시 창 끝) = 331분. 08-12 실측과 같은 값이다.
    assert metrics["max_silence_minutes"] == 331.0
    assert metrics["silence_over_cadence_ratio"] == 33.1  # 정상 주기의 33배
    assert metrics["max_silence_window"] == "10:14~15:45"


def test_a_successful_restart_is_not_counted_as_a_failure():
    """「재기동 실패 보고」는 **로그 문구를 센 것이지 실패를 센 것이 아니다.**"""
    lines = [
        "[2026-08-12 10:14:01] RESTART — 관측 루프 생존 신호 이상(stale)",
        "[2026-08-12 10:14:01] 재기동 시도: 기동 스크립트 실행 완료",
    ]
    assert watchdog_metrics.parse(lines, _TARGET)["restart_failures"] == 0


def test_a_day_the_watchdog_never_ran_is_the_whole_window():
    """08-06~08-11에 6영업일 연속 그랬고 아무도 몰랐다 — 그때 이 값이 창 전체가 된다."""
    metrics = watchdog_metrics.parse([], _TARGET)
    assert metrics["checks"] == 0
    assert metrics["max_silence_minutes"] == 485.0  # 07:40~15:45
    assert metrics["first_at"] is None


def test_other_days_lines_are_ignored():
    lines = _healthy_day() + ["[2026-08-11 09:00:01] OK — 정상"]
    assert watchdog_metrics.parse(lines, _TARGET)["checks"] == len(_healthy_day())


def test_missing_log_file_is_unknown_not_healthy(tmp_path):
    """**None은 「정상」이 아니라 「모른다」다** — 파일이 없는 PC와 워치독이 안 도는 PC는 다르다."""
    assert watchdog_metrics.collect(tmp_path, _TARGET) is None


def test_report_warns_when_the_watchdog_went_silent():
    out = report.render(
        {"date": "2026-08-12"},
        watchdog={
            "checks": 12, "restarts": 1, "restart_failures": 1, "alert_only": 0,
            "max_silence_minutes": 331.0, "max_silence_window": "10:14~15:45",
            "first_at": "07:40:01", "last_at": "10:14:01",
            "silence_warn_minutes": watchdog_metrics.SILENCE_WARN_MINUTES,
        },
    )
    assert "## 11-1. 워치독" in out
    assert "331분" in out
    assert "아무도 되살리지 않는다" in out


def test_report_shouts_when_the_watchdog_never_ran():
    out = report.render(
        {"date": "2026-08-12"},
        watchdog={
            "checks": 0, "restarts": 0, "restart_failures": 0, "alert_only": 0,
            "max_silence_minutes": 485.0, "max_silence_window": "07:40~15:45",
            "first_at": None, "last_at": None,
            "silence_warn_minutes": watchdog_metrics.SILENCE_WARN_MINUTES,
        },
    )
    assert "한 줄도 안 남겼다" in out
