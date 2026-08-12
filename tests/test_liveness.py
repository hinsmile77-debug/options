"""관측 루프 생존 신호 + 워치독 판정 (2026-08-06 §2-1 / Fix#2).

08-06 10:04~10:23의 19분 공백을 이 코드가 3분 안에 잡아야 한다. 아래 테스트 중 절반은
**오경보를 막는 쪽**이다 — 워치독이 재기동까지 하므로, 살아 있는 루프를 죽이면 그 순간
진짜 공백이 생긴다.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, time as dtime, timedelta

import pytest

from mahdi import liveness


_MORNING = datetime(2026, 8, 6, 10, 4)


# ===== 하트비트 파일 =====


def test_write_then_read_roundtrip(tmp_path):
    path = liveness.heartbeat_path(tmp_path)
    liveness.write_heartbeat(path, _MORNING, beats=7)
    beat = liveness.read_heartbeat(path)
    assert beat["at"] == _MORNING
    assert beat["beats"] == 7
    assert beat["pid"] == os.getpid()


def test_write_is_atomic_and_leaves_no_partial_file(tmp_path):
    """워치독이 쓰기 도중 파일을 읽고 파싱에 실패하면 그것을 '죽음'으로 오독한다.

    임시 파일 → replace 경로를 쓰는지 확인한다(임시 파일이 남지 않아야 한다).
    """
    path = liveness.heartbeat_path(tmp_path)
    liveness.write_heartbeat(path, _MORNING, beats=1)
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != path.name]
    assert leftovers == []


def test_read_missing_file_is_none_not_an_error(tmp_path):
    assert liveness.read_heartbeat(liveness.heartbeat_path(tmp_path)) is None


def test_read_corrupt_file_is_none(tmp_path):
    path = liveness.heartbeat_path(tmp_path)
    path.write_text("{절반만 쓰다 만", encoding="utf-8")
    assert liveness.read_heartbeat(path) is None


def test_write_never_raises_even_when_the_path_is_unusable(tmp_path):
    """**생존 신호를 못 썼다고 관측이 멈추면 본말이 뒤집힌다.**"""
    blocker = tmp_path / "blocker"
    blocker.write_text("파일이라 하위 디렉터리를 만들 수 없다", encoding="utf-8")
    liveness.write_heartbeat(blocker / "sub" / "hb.json", _MORNING, beats=1)  # 예외 없음


def test_clear_is_idempotent(tmp_path):
    path = liveness.heartbeat_path(tmp_path)
    liveness.clear_heartbeat(path)  # 없어도 조용히
    liveness.write_heartbeat(path, _MORNING, beats=1)
    liveness.clear_heartbeat(path)
    assert not path.exists()


def test_age_and_staleness():
    beat = {"pid": 1, "at": _MORNING, "beats": 1}
    assert liveness.heartbeat_age_seconds(beat, _MORNING + timedelta(seconds=45)) == 45
    assert liveness.is_stale(beat, _MORNING + timedelta(seconds=45)) is False
    assert liveness.is_stale(beat, _MORNING + timedelta(seconds=181)) is True


def test_absent_heartbeat_is_not_stale():
    """없는 것과 늙은 것은 다르다 — 같이 취급하면 장마감 후와 기동 전에 매일 두 번 알림이 뜬다."""
    assert liveness.is_stale(None, _MORNING) is False


def test_stale_threshold_is_a_multiple_of_the_write_interval():
    """임계가 기록 주기보다 크게 여유 있어야 GC 한 번에 오경보가 안 난다."""
    assert liveness.HEARTBEAT_STALE_SECONDS >= liveness.HEARTBEAT_INTERVAL_SECONDS * 4


# ===== 감시 창 =====


def test_watch_window_covers_the_trading_day_but_not_before_startup():
    assert liveness.in_watch_window(datetime(2026, 8, 6, 7, 39)) is False  # 기동 직후 유예
    assert liveness.in_watch_window(datetime(2026, 8, 6, 7, 40)) is True
    assert liveness.in_watch_window(datetime(2026, 8, 6, 15, 45)) is True
    assert liveness.in_watch_window(datetime(2026, 8, 6, 15, 46)) is False


def test_watch_window_ends_with_the_trading_day():
    """규약 B — 장 마감 시각은 `mahdi.session` 하나에서 온다."""
    from mahdi import session

    assert liveness.WATCH_WINDOW_END is session.TRADING_DAY_END


# ===== 판정 =====


def _beat(at: datetime) -> dict:
    return {"pid": 4242, "at": at, "beats": 100}


def test_healthy_heartbeat_is_ok():
    decision = liveness.decide(_beat(_MORNING), _MORNING + timedelta(seconds=40))
    assert decision.action == liveness.ACTION_OK


def test_the_2026_08_06_outage_is_caught_within_three_minutes():
    """**이 파일의 존재 이유.** 10:04:00에 죽었고 사람이 안 것은 10:20이었다."""
    died_at = datetime(2026, 8, 6, 10, 4)
    # 10:07 — 임계(180초)를 막 넘긴 첫 순간
    decision = liveness.decide(_beat(died_at), died_at + timedelta(seconds=181))
    assert decision.action == liveness.ACTION_RESTART
    assert decision.reason == liveness.REASON_STALE
    assert decision.should_alert is True
    # 사람이 알아챈 시각(16분 뒤)보다 훨씬 앞이다
    assert timedelta(seconds=181) < timedelta(minutes=16)


def test_a_loop_that_never_started_is_reported_as_missing_not_stale():
    """08-06 10:20:11의 `^C`로 중단된 기동이 이 경우다 — 조치는 같아도 사유가 달라야 한다."""
    decision = liveness.decide(None, datetime(2026, 8, 6, 8, 0))
    assert decision.action == liveness.ACTION_RESTART
    assert decision.reason == liveness.REASON_MISSING


def test_outside_the_watch_window_nothing_happens():
    """장마감 후 하트비트가 없는 것은 정상이다 — 여기서 알리면 매일 밤 오경보다."""
    assert liveness.decide(None, datetime(2026, 8, 6, 22, 0)).action == liveness.ACTION_IDLE
    assert liveness.decide(None, datetime(2026, 8, 6, 6, 0)).action == liveness.ACTION_IDLE


def test_a_start_in_progress_suspends_judgement():
    """기동 스크립트는 Docker를 최대 180초 기다린다 — 그 사이 판정하면 기동이 서로를 덮어쓴다."""
    decision = liveness.decide(None, datetime(2026, 8, 6, 10, 22), starting=True)
    assert decision.action == liveness.ACTION_IDLE
    assert "기동" in decision.detail


def test_restart_cap_switches_to_alert_only():
    """재기동은 그 자체로 공백을 만든다 — 안 풀리는 문제를 하루 종일 재시도하지 않는다."""
    state = {"date": "2026-08-06", "restarts": liveness.MAX_RESTARTS_PER_DAY, "last_alert_at": None}
    decision = liveness.decide(None, datetime(2026, 8, 6, 11, 0), state)
    assert decision.action == liveness.ACTION_ALERT_ONLY
    assert decision.should_alert is True
    assert "사람이 봐야" in decision.detail


def test_restart_cap_resets_on_a_new_day():
    state = {"date": "2026-08-05", "restarts": 9, "last_alert_at": None}
    assert liveness.decide(None, datetime(2026, 8, 6, 11, 0), state).action == liveness.ACTION_RESTART


def test_alerts_are_throttled_but_restarts_are_not():
    """1분 주기 워치독이 매분 같은 말을 하면 곧 무시된다. 그러나 조치는 눌리면 안 된다."""
    now = datetime(2026, 8, 6, 11, 0)
    state = {"date": "2026-08-06", "restarts": 0, "last_alert_at": (now - timedelta(seconds=60)).isoformat()}
    decision = liveness.decide(None, now, state)
    assert decision.action == liveness.ACTION_RESTART
    assert decision.should_alert is False


def test_alert_is_due_again_after_the_cooldown():
    now = datetime(2026, 8, 6, 11, 0)
    old = (now - timedelta(seconds=liveness.ALERT_COOLDOWN_SECONDS + 1)).isoformat()
    state = {"date": "2026-08-06", "restarts": 0, "last_alert_at": old}
    assert liveness.decide(None, now, state).should_alert is True


def test_corrupt_state_does_not_silence_the_watchdog():
    """상태 파일이 깨졌다고 감시가 멈추면, 그 상태가 곧 사각지대가 된다."""
    state = {"date": "2026-08-06", "restarts": "셋", "last_alert_at": "어제쯤"}
    decision = liveness.decide(None, datetime(2026, 8, 6, 11, 0), state)
    assert decision.action == liveness.ACTION_RESTART
    assert decision.should_alert is True


# ===== 상태 전이 =====


def test_next_state_counts_restarts_and_stamps_alerts():
    now = datetime(2026, 8, 6, 11, 0)
    decision = liveness.WatchdogDecision(liveness.ACTION_RESTART, liveness.REASON_STALE, should_alert=True)
    state = liveness.next_state({"date": "2026-08-06", "restarts": 1}, now, decision)
    assert state == {"date": "2026-08-06", "restarts": 2, "last_alert_at": now.isoformat()}


def test_next_state_does_not_count_alert_only_as_a_restart():
    now = datetime(2026, 8, 6, 11, 0)
    decision = liveness.WatchdogDecision(liveness.ACTION_ALERT_ONLY, liveness.REASON_STALE, should_alert=True)
    state = liveness.next_state({"date": "2026-08-06", "restarts": 3}, now, decision)
    assert state["restarts"] == 3


def test_next_state_folds_the_counter_on_a_new_day():
    now = datetime(2026, 8, 7, 8, 0)
    decision = liveness.WatchdogDecision(liveness.ACTION_OK)
    state = liveness.next_state({"date": "2026-08-06", "restarts": 3, "last_alert_at": "2026-08-06T11:00:00"}, now, decision)
    assert state == {"date": "2026-08-07", "restarts": 0, "last_alert_at": None}


# ===== 기동 표식 =====


def test_startup_marker_is_read_by_mtime_not_content(tmp_path):
    """cmd.exe가 쓴 날짜 문자열은 로케일을 탄다 — 파싱하면 조용히 오판한다."""
    path = liveness.startup_marker_path(tmp_path)
    path.write_text("아무 내용이나 — 심지어 깨진 인코딩", encoding="utf-8")
    assert liveness.startup_in_progress(path, datetime.now()) is True


def test_missing_startup_marker_means_not_starting(tmp_path):
    assert liveness.startup_in_progress(liveness.startup_marker_path(tmp_path), datetime.now()) is False


def test_stale_startup_marker_is_ignored(tmp_path):
    """기동 스크립트가 중간에 죽어 표식만 남는 경우 — 08-06 10:20:11의 `^C`가 그 예다."""
    path = liveness.startup_marker_path(tmp_path)
    path.write_text("startup", encoding="utf-8")
    much_later = datetime.now() + timedelta(seconds=liveness.STARTUP_MARKER_GRACE_SECONDS + 60)
    assert liveness.startup_in_progress(path, much_later) is False


# ===== 2026-08-12 §2-3 / Fix#8 — 감시자를 감시한다 =====
#
# 08-12에 워치독은 10:14:01에 정확히 판정하고 정확히 되살렸다. 그런데 재기동 호출이 상속된
# 파이프에 물려 15:45:02까지 막혔고, 작업 스케줄러가 `MultipleInstances=IgnoreNew`라 그동안
# 매분 실행이 전부 무시됐다 — **재기동에 성공한 그 순간부터 장 마감까지 감시가 없었다.**
#
# 기존 신호로는 셋 다 정상으로 보였다: 프로세스는 살아 있었고(막혀 있었을 뿐), 스케줄러는
# `State: Ready` / `LastTaskResult: 0` / `NumberOfMissedRuns: 0`, `watchdog.log`의 마지막 줄은
# 「RESTART」라 사고 대응 중인 것처럼 보였다. **프로세스 생존은 기능 생존의 증거가 아니다.**


def test_watchdog_check_roundtrip(tmp_path):
    path = liveness.watchdog_check_path(tmp_path)
    now = datetime(2026, 8, 12, 10, 14, 1)
    liveness.write_watchdog_check(path, now, action=liveness.ACTION_RESTART, detail="stale")

    check = liveness.read_watchdog_check(path)
    assert check["at"] == now
    assert check["action"] == liveness.ACTION_RESTART
    assert check["detail"] == "stale"


def test_the_action_is_recorded_not_just_the_time(tmp_path):
    """시각만 남기면 「감시 창 밖이라 아무것도 안 했다」와 「정상이라고 판정했다」가 안 갈린다."""
    path = liveness.watchdog_check_path(tmp_path)
    liveness.write_watchdog_check(path, datetime(2026, 8, 12, 3, 0), action=liveness.ACTION_IDLE)
    assert liveness.read_watchdog_check(path)["action"] == liveness.ACTION_IDLE


def test_missing_watchdog_check_is_unknown_not_dead(tmp_path):
    """**None은 「멈췄다」가 아니라 「모른다」다** — 이 파일 이전 버전과 미등록 PC가 둘 다 None이다."""
    assert liveness.read_watchdog_check(liveness.watchdog_check_path(tmp_path)) is None


def test_corrupt_watchdog_check_is_none(tmp_path):
    path = liveness.watchdog_check_path(tmp_path)
    path.write_text("{깨진", encoding="utf-8")
    assert liveness.read_watchdog_check(path) is None


def test_watchdog_check_write_never_raises(tmp_path):
    """자기 기록을 못 썼다고 워치독이 멈추면 안 된다 — `write_heartbeat`와 같은 계약."""
    unusable = tmp_path / "없는디렉터리" / "sub" / "x.json"
    (tmp_path / "없는디렉터리").write_text("파일이라 mkdir이 실패한다", encoding="utf-8")
    liveness.write_watchdog_check(unusable, datetime(2026, 8, 12, 10, 0), action="ok")


def test_watchdog_check_age(tmp_path):
    path = liveness.watchdog_check_path(tmp_path)
    liveness.write_watchdog_check(path, datetime(2026, 8, 12, 10, 0), action="ok")
    check = liveness.read_watchdog_check(path)
    age = liveness.watchdog_check_age_seconds(check, datetime(2026, 8, 12, 10, 5))
    assert age == 300.0
    assert liveness.watchdog_check_age_seconds(None, datetime(2026, 8, 12, 10, 5)) is None


def test_the_watchdog_staleness_threshold_is_a_multiple_of_its_own_period():
    """워치독은 1분 주기다. 임계가 그보다 작거나 같으면 스케줄러 지터 한 번에 오경보가 난다."""
    assert liveness.WATCHDOG_CHECK_STALE_SECONDS >= 120.0
    # 08-12의 공백은 331분이었다 — 이 임계로는 3분 안에 화면에 떴을 것이다.
    assert liveness.WATCHDOG_CHECK_STALE_SECONDS <= 300.0


def test_the_2026_08_12_watchdog_blackout_would_have_been_visible(tmp_path):
    """그날 10:14:01 이후 판정이 없었다 — 10:20에는 이미 임계를 넘는다."""
    path = liveness.watchdog_check_path(tmp_path)
    liveness.write_watchdog_check(
        path, datetime(2026, 8, 12, 10, 14, 1), action=liveness.ACTION_RESTART
    )
    check = liveness.read_watchdog_check(path)
    age = liveness.watchdog_check_age_seconds(check, datetime(2026, 8, 12, 10, 20, 0))
    assert age > liveness.WATCHDOG_CHECK_STALE_SECONDS
