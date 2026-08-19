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


# ===== 적재 정지 판정 (2026-08-14 §2-1 / Fix#2) =====


def test_degraded_lines_are_counted_apart_from_restarts():
    """**RESTART와 섞지 않는다.** 저쪽은 「조치했다」, 이쪽은 「조치하지 않기로 했다」이다.

    08-14 14:00~15:23의 84분은 재기동으로 고칠 수 있는 사건이 아니었다(원인은 KIS 지연이고,
    재기동은 관측만 12~14초 끊는다). 둘을 한 칸에 세면 다음날 "워치독이 몇 번 개입했나"에
    답할 수 없다.
    """
    lines = _healthy_day() + [
        "[2026-08-12 14:10:01] DEGRADED — 관측 루프 적재 정지(no_ingest) — 직전 10분 동안 "
        "옵션체인 적재가 **0분**이다",
        "[2026-08-12 14:20:01] DEGRADED — 관측 루프 적재 정지(no_ingest) — 직전 10분 동안 "
        "옵션체인 적재가 **0분**이다",
    ]

    metrics = watchdog_metrics.parse(lines, _TARGET)

    assert metrics["degraded_checks"] == 2
    assert metrics["restarts"] == 0
    assert metrics["alert_only"] == 0


def test_a_quiet_day_reports_zero_degraded_not_a_missing_key():
    """규약 C — 「적재가 끊긴 분이 없었다」와 「이 판정이 없던 버전이다」를 소비측이 갈라야 한다."""
    assert watchdog_metrics.parse(_healthy_day(), _TARGET)["degraded_checks"] == 0


def test_report_shouts_when_the_loop_was_alive_but_empty():
    out = report.render(
        {"date": "2026-08-14"},
        watchdog={
            "checks": 49, "restarts": 0, "restart_failures": 0, "alert_only": 0,
            "degraded_checks": 7,
            "max_silence_minutes": 10.0, "max_silence_window": "08:50~09:00",
            "first_at": "07:40:02", "last_at": "15:40:01",
            "silence_warn_minutes": watchdog_metrics.SILENCE_WARN_MINUTES,
        },
    )
    assert "적재 정지(DEGRADED)" in out
    assert "재기동은 하지 않는다" in out


# ===== 2026-08-19 (08-18 보고서 §1-2 / Fix#4) — 장전 점검 미발화 경보 =====
#
# 08-18 장전 회차는 08:30 예정에 **13:28:36**(298분 지연)에 떴고, 같은 날 14:30 회차는 정시에
# 떴다. **예약이 안 뜬 것을 예약으로 감시할 수 없으므로** 10분마다 도는 워치독이 유일한 신호다.
# 08-17 보고서가 남긴 그 항목이 사흘째 미이행이었다.


def test_a_missing_check_alert_is_counted_on_its_own_axis():
    """`restarts`/`degraded_checks`와 **섞이면 안 된다** — 조치가 완전히 다르다.

    08-18은 인프라가 하루 종일 초록인 채로(재기동 0 · DEGRADED 0) 장전 점검만 298분 늦었다.
    한 축에 합치면 그 하루가 「아무 일도 없었다」로 인쇄된다.
    """
    lines = _healthy_day() + [
        f"[2026-08-12 09:00:03] {watchdog_metrics.MISSING_CHECK_MARKER} — 오늘 장전 점검 산출물이 없다"
    ]
    metrics = watchdog_metrics.parse(lines, _TARGET)
    assert metrics["missing_check_alerts"] == 1
    assert metrics["restarts"] == 0
    assert metrics["degraded_checks"] == 0
    # 판정한 것은 맞으므로 `checks`에는 들어간다 — 그 분에 워치독이 실제로 돌았다.
    assert metrics["checks"] == len(_healthy_day()) + 1


def test_a_quiet_day_reports_zero_missing_checks_not_a_missing_key():
    """규약 C — 「점검이 제때 있었다」와 「이 판정이 없던 버전이다」를 소비측이 갈라야 한다."""
    assert watchdog_metrics.parse(_healthy_day(), _TARGET)["missing_check_alerts"] == 0


def test_the_marker_matches_the_script_that_writes_it():
    """복제본이 갈라지면 경보가 0건으로 세어지고, 그 0은 「점검이 제때 있었다」로 읽힌다."""
    import importlib

    loop = importlib.import_module("scripts.watchdog_observation_loop")
    assert loop._MISSING_CHECK_MARKER == watchdog_metrics.MISSING_CHECK_MARKER


# ===== 2026-08-19 — 버려진 git 락 청소 =====
#
# 0바이트 `.git/index.lock`이 이틀 연속 남아 다음 git 작업을 전부 막았다(08-18 16:20 ·
# 08-19 12:41). 워치독이 그것을 열고, 이 지표가 **연 횟수**를 센다 — 그 값이 자라는 것 자체가
# 「세션 teardown이 git을 죽이고 있다」는 신호다.


def test_a_lock_sweep_is_counted_on_its_own_axis():
    """**관측 루프와 무관한 축이다.** 저장소가 막혀 있던 것이고 루프는 멀쩡했다."""
    lines = _healthy_day() + [
        f"[2026-08-12 16:20:01] {watchdog_metrics.LOCK_SWEEP_MARKER} — 버려진 git 락을 열었다: .git/index.lock"
    ]
    metrics = watchdog_metrics.parse(lines, _TARGET)
    assert metrics["stale_lock_sweeps"] == 1
    assert metrics["restarts"] == 0
    assert metrics["degraded_checks"] == 0
    assert metrics["missing_check_alerts"] == 0


def test_a_quiet_day_reports_zero_sweeps_not_a_missing_key():
    """규약 C — 「버려진 락이 없었다」와 「이 청소가 없던 버전이다」를 소비측이 갈라야 한다."""
    assert watchdog_metrics.parse(_healthy_day(), _TARGET)["stale_lock_sweeps"] == 0


def test_the_sweep_marker_matches_the_script_that_writes_it():
    """갈라지면 청소 건수가 0으로 세어지고, 그 0은 「락이 안 남았다」로 읽힌다."""
    import importlib

    loop = importlib.import_module("scripts.watchdog_observation_loop")
    assert loop._LOCK_SWEEP_MARKER == watchdog_metrics.LOCK_SWEEP_MARKER


def test_report_shouts_when_it_had_to_open_a_lock():
    out = report.render(
        {"date": "2026-08-19"},
        watchdog={
            "checks": 53, "restarts": 0, "restart_failures": 0, "alert_only": 0,
            "degraded_checks": 0, "missing_check_alerts": 0, "stale_lock_sweeps": 2,
            "max_silence_minutes": 10.0, "max_silence_window": "08:50~09:00",
            "first_at": "07:40:02", "last_at": "15:40:01",
            "silence_warn_minutes": watchdog_metrics.SILENCE_WARN_MINUTES,
        },
    )
    assert "버려진 git 락 청소" in out
    assert "트리 킬의 지문" in out
    assert "잦아지면 청소가 아니라 원인을 봐야 한다" in out
