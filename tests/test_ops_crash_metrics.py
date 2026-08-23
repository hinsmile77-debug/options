"""관측 루프가 **왜** 죽었는가 — 크래시 로그 파서 (2026-08-19).

08-19에 워치독이 두 번 재기동했고(09:54:01 · 10:36:01) 리포트는 그 사실까지만 말했다.
첫 번째의 사유는 `psycopg.OperationalError`였고 — DB 컨테이너가 09:50:56에 재시작됐다 —
그 사유는 `logs/observation_loop_crash.log`에만 있었다. **그 파일을 읽는 코드가 없었다.**
`observation_loop.log`에는 `OperationalError`가 0건이다(예외가 로깅을 거치지 않고 프로세스를
끝냈다). 08-18 보고서 §3-2와 같은 계열의 결함이고, 아래가 그 구멍을 막는다.
"""

from __future__ import annotations

from datetime import date

from mahdi.ops import crash_metrics, report

_TARGET = date(2026, 8, 19)

# 08-19 실측을 그대로 옮긴 것 — `^C`가 예외 줄 앞에 붙는 것까지 포함한다(실제로 그랬다).
_REAL_CRASH = [
    "[2026-08-19  7:30:00.78] ===== 관측 루프 기동 =====",
    "^CTraceback (most recent call last):",
    r'  File "C:\Users\82108\PycharmProjects\options\mahdi\main.py", line 1481, in handle_message',
    "    with db.get_connection() as conn:",
    r'  File "C:\Users\82108\PycharmProjects\options\mahdi\data\db.py", line 97, in get_connection',
    "    conn = psycopg.connect(settings.dsn)",
    "psycopg.OperationalError: connection failed: server closed the connection unexpectedly",
    "[2026-08-19  9:54:23.11] ===== 관측 루프 기동 =====",
    "[2026-08-19 10:36:22.40] ===== 관측 루프 기동 =====",
]


def test_the_cause_that_only_lived_in_the_crash_file_is_now_counted():
    m = crash_metrics.parse(_REAL_CRASH, _TARGET)
    assert m["starts"] == 3
    assert m["crashes"] == 1
    assert m["causes"] == {"psycopg.OperationalError": 1}


def test_the_full_dotted_name_is_kept():
    """마지막 조각만 세면 서로 다른 모듈의 같은 이름이 한 칸에 합쳐져 원인 귀속이 흐려진다."""
    assert "psycopg.OperationalError" in crash_metrics.parse(_REAL_CRASH, _TARGET)["causes"]


def test_a_console_caret_c_does_not_hide_the_exception():
    """08-19 로그의 트레이스백 셋이 전부 `^C`로 시작한다 — 그것 때문에 사유가 사라지면 안 된다."""
    assert crash_metrics.parse(_REAL_CRASH, _TARGET)["events"][0]["cause"] == "psycopg.OperationalError"


def test_the_last_frame_points_at_our_code():
    assert crash_metrics.parse(_REAL_CRASH, _TARGET)["events"][0]["last_frame"] == "db.py:97 in get_connection"


def test_the_marker_time_is_the_launch_not_the_death():
    """시각은 그 프로세스가 «뜬» 시각이다 — 죽은 시각은 로그 공백과 워치독이 답한다."""
    assert crash_metrics.parse(_REAL_CRASH, _TARGET)["events"][0]["at"] == "07:30:00"


def test_the_last_exception_wins_not_the_first():
    """파이썬은 예외를 겹쳐 쌓는다(`During handling of the above exception`) —
    프로세스를 실제로 끝낸 것은 **맨 아래** 것이다. 첫 것을 취하면 08-19가
    «asyncio.CancelledError»로 보고됐을 것이다."""
    lines = [
        "[2026-08-19  7:30:00.78] ===== 관측 루프 기동 =====",
        "Traceback (most recent call last):",
        "asyncio.CancelledError",
        "During handling of the above exception, another exception occurred:",
        "Traceback (most recent call last):",
        "psycopg.OperationalError: connection failed",
    ]
    assert crash_metrics.parse(lines, _TARGET)["causes"] == {"psycopg.OperationalError": 1}


def test_another_days_crash_is_not_counted_as_todays():
    lines = ["[2026-08-18 07:30:01.00] ===== 관측 루프 기동 =====", "ValueError: 어제 것"] + _REAL_CRASH
    m = crash_metrics.parse(lines, _TARGET)
    assert m["starts"] == 3 and m["causes"] == {"psycopg.OperationalError": 1}


def test_a_traceback_before_any_marker_is_unattributed_not_zero():
    """**「오늘 것이 아니다」와 「날짜를 모른다」는 다른 사실이다**(규약 C).

    이 파일은 2026-07-19부터 타임스탬프 없이 append돼 왔다 — 표식 이전의 트레이스백 셋을
    「오늘 아님」으로 접으면 그 셋이 영영 안 보이고, 오늘 것으로 세면 없는 사고가 생긴다.
    """
    lines = ["Traceback (most recent call last):", "KeyboardInterrupt"] + _REAL_CRASH
    m = crash_metrics.parse(lines, _TARGET)
    assert m["unattributed"] == 1
    assert m["crashes"] == 1  # 오늘 것에는 안 섞인다


def test_a_file_without_any_marker_says_it_cannot_count():
    """표식이 없는 날은 「크래시가 없었다」가 아니라 「셀 수 없었다」이다."""
    m = crash_metrics.parse(["Traceback (most recent call last):", "ValueError: x"], _TARGET)
    assert m["marker_present"] is False
    assert m["starts"] == 0 and m["unattributed"] == 1


def test_a_missing_file_is_none_not_an_empty_day(tmp_path):
    assert crash_metrics.collect(tmp_path, _TARGET) is None


def test_report_names_the_cause_next_to_the_restart_count():
    out = report.render({"date": "2026-08-19"}, crash=crash_metrics.parse(_REAL_CRASH, _TARGET))
    assert "psycopg.OperationalError" in out
    assert "기동 **3회**" in out


def test_report_shouts_when_a_death_left_no_reason():
    """08-19 10:32가 그 형태였다 — 예외 없이 사라지고 3분 뒤 워치독이 되살렸다.

    2026-08-23 Fix#7 — 「사유 없이 끝난 기동」은 이제 **종료 표식을 다 빼고 남은 것**이다.
    그래서 이 테스트도 그날의 종료 회계(장마감 자동 종료 1회 · 수동 정지 0회)를 함께 준다.
    그것이 없으면 리포트는 「모른다」라고 말한다(아래 테스트).
    """
    out = report.render({"date": "2026-08-19"}, crash=_with_shutdowns(_REAL_CRASH, clean=1))
    assert "사유 없이 끝난 기동 1건" in out


# ===== 2026-08-23 (08-21 §1-17 / §4 Fix#7) — 의도적 정지는 죽음이 아니다 =====
#
# 08-21 12:18에 사람이 `stop_mahdi_manual.bat`으로 내렸고, 그 정지가 지표에서 「사유 없이
# 끝난 기동 1건」으로 잡혔다. 표식은 둘이나 있었고(`.intentional_stop` · 기동 로그의 문구)
# 워치독은 그것을 정확히 읽었다(재기동 시도 0회) — **지표만 몰랐다.**

_STARTUP_LOG_0821 = [
    "[2026-08-21  7:30:00.90] ===== Mahdi 장전 기동 시작 ===== ",
    "[2026-08-21 12:18:25.50] ===== Mahdi 수동 정지 시작 (사람이 일부러 내림) ===== ",
    "[2026-08-21 12:18:26.57] ===== Mahdi 수동 정지 완료 (DB/Redis는 계속 실행) ===== ",
    "[2026-08-21 12:18:53.81] ===== Mahdi 장전 기동 시작 ===== ",
    "[2026-08-21 15:45:01.17] ===== Mahdi 장마감 자동 종료 시작 ===== ",
    "[2026-08-21 15:46:14.46] ===== 장마감 자동 종료 완료 (DB/Redis는 계속 실행) ===== ",
]


def _with_shutdowns(lines, *, clean=0, intentional=0, target=_TARGET):
    m = crash_metrics.parse(lines, target)
    m["clean_shutdowns"] = clean
    m["intentional_stops"] = intentional
    m["unexplained_deaths"] = crash_metrics.unexplained_deaths(
        m["starts"], m["crashes"], intentional, clean
    )
    return m


def test_the_startup_log_tells_the_two_kinds_of_deliberate_shutdown_apart():
    """장마감 자동 종료와 사람의 수동 정지는 **합치지 않는다** — 하나는 스케줄, 하나는 결정이다."""
    m = crash_metrics.parse_shutdowns(_STARTUP_LOG_0821, date(2026, 8, 21))
    assert m == {"intentional_stops": 1, "clean_shutdowns": 1}


def test_only_the_completion_lines_are_counted_not_the_start_lines():
    """「시작」과 「완료」가 쌍으로 남는다 — 둘 다 세면 종료 수가 정확히 두 배가 된다."""
    starts_only = [ln for ln in _STARTUP_LOG_0821 if "시작" in ln]
    assert crash_metrics.parse_shutdowns(starts_only, date(2026, 8, 21)) == {
        "intentional_stops": 0, "clean_shutdowns": 0,
    }


def test_the_2026_08_21_manual_stop_is_no_longer_an_unexplained_death():
    """**이 fix의 전부.** 기동 2 − 죽음 0 − 자동 종료 1 − 수동 정지 1 = **0**.

    종전 식(`기동 − 죽음 − 1`)이면 1이었고, 그 1이 08-21 지표의 오탐이다.
    """
    assert crash_metrics.unexplained_deaths(2, 0, 1, 1) == 0
    assert crash_metrics.unexplained_deaths(2, 0, 0, 1) == 1  # 수동 정지를 안 세면 오탐이 돌아온다


def test_a_real_unexplained_death_still_fires():
    """**경보를 끈 것이 아니라 고친 것**이어야 한다 — 08-19 10:32 형태를 여기서 고정한다.

    그날 기동 3회 · 사유가 남은 죽음 1건 · 장마감 자동 종료 1회 · 수동 정지 0회 →
    남는 1건이 「표식 없이 사라진 기동」이다(워치독이 3분 공백 뒤 되살렸다).
    """
    m = _with_shutdowns(_REAL_CRASH, clean=1)
    assert m["unexplained_deaths"] == 1


def test_more_shutdowns_than_starts_does_not_print_a_negative():
    """전날 프로세스를 오늘 껐으면 종료가 기동보다 많을 수 있다 — 음수는 그 자체가 오보다."""
    assert crash_metrics.unexplained_deaths(0, 0, 1, 1) == 0


def test_an_unreadable_startup_log_says_it_does_not_know(tmp_path):
    """종료 표식을 못 읽으면 **0이 아니라 「모른다」**다(규약 C) — 그리고 그 사실을 인쇄한다."""
    (tmp_path / crash_metrics.CRASH_LOG_FILENAME).write_text(
        chr(10).join(_REAL_CRASH), encoding="utf-8"
    )
    m = crash_metrics.collect(tmp_path, _TARGET)
    assert m["unexplained_deaths"] is None and m["intentional_stops"] is None
    assert "모른다" in report.render({"date": "2026-08-19"}, crash=m)


def test_the_report_names_the_manual_stop_instead_of_burying_it():
    """사람이 내린 정지는 **별도 줄**로 인쇄된다 — 빼기만 하면 그 사실이 사라진다."""
    out = report.render(
        {"date": "2026-08-21"},
        crash=_with_shutdowns(_REAL_CRASH, clean=1, intentional=1),
    )
    assert "사람이 일부러 내린 정지 **1회**" in out
    # 3 − 1 − 1 − 1 = 0 이므로 **경고 줄은 안 나온다**(안내 문구 안의 같은 표현과 구별한다).
    assert "**사유 없이 끝난 기동" not in out


def test_report_admits_when_it_cannot_attribute_at_all():
    out = report.render({"date": "2026-08-19"}, crash=crash_metrics.parse(["ValueError: x"], _TARGET))
    assert "셀 수 없었다" in out


def test_three_stacked_tracebacks_before_the_marker_are_not_squashed_into_one():
    """08-19 실측: 표식 없이 트레이스백 **세 개**가 쌓여 있었다.

    구간 하나를 「1건」으로 세면 그 셋이 하나로 뭉개진다. 상한이라도 개수를 세는 편이,
    「1건」이라는 정확해 보이는 거짓말보다 낫다.
    """
    lines = (
        ["Traceback (most recent call last):", "KeyboardInterrupt"] * 2
        + ["^CTraceback (most recent call last):", "psycopg.OperationalError: x"]
        + _REAL_CRASH
    )
    assert crash_metrics.parse(lines, _TARGET)["unattributed"] == 3
