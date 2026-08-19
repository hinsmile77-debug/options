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
    """08-19 10:32가 그 형태였다 — 예외 없이 사라지고 3분 뒤 워치독이 되살렸다."""
    out = report.render({"date": "2026-08-19"}, crash=crash_metrics.parse(_REAL_CRASH, _TARGET))
    assert "사유 없이 끝난 기동 1건" in out


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
