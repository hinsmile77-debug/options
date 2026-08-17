"""관측 루프 생존 신호 + 워치독 판정 (2026-08-06 §2-1 / Fix#2).

08-06 10:04~10:23의 19분 공백을 이 코드가 3분 안에 잡아야 한다. 아래 테스트 중 절반은
**오경보를 막는 쪽**이다 — 워치독이 재기동까지 하므로, 살아 있는 루프를 죽이면 그 순간
진짜 공백이 생긴다.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

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


def test_the_2026_08_15_weekend_boot_would_not_happen():
    """**08-15(토) 07:40:02에 워치독이 토요일에 시스템 전체를 부팅했다.**

    장전 기동 작업은 월~금 트리거라 안 떴는데, 감시 창이 `now.time()`만 봐서 요일을 몰랐다.
    08-16(일) 10:14:39에도 같은 일이 반복됐고, 두 날 모두 재기동 상한 3회를 채운 뒤
    `ALERT_ONLY`를 94줄·113줄 쏟았다.
    """
    saturday = datetime(2026, 8, 15, 7, 40, 2)
    sunday = datetime(2026, 8, 16, 10, 14, 39)
    for weekend in (saturday, sunday):
        decision = liveness.decide(None, weekend)
        assert decision.action == liveness.ACTION_IDLE
        assert "주말" in decision.detail
    # 같은 시각의 평일은 종전대로 되살린다 — 주말 검사가 감시를 통째로 끄면 안 된다.
    assert liveness.decide(None, datetime(2026, 8, 17, 7, 40, 2)).action == liveness.ACTION_RESTART


def test_a_listed_holiday_suspends_everything():
    """08-17(대체공휴일)은 기동 작업이 월~금이라 **정상적으로** 떠서 종일 돌았다."""
    decision = liveness.decide(
        _beat(datetime(2026, 8, 17, 10, 0)), datetime(2026, 8, 17, 10, 30),
        holiday="광복절 대체공휴일",
    )
    assert decision.action == liveness.ACTION_IDLE
    assert decision.reason == liveness.REASON_HOLIDAY
    assert "광복절 대체공휴일" in decision.detail  # 달력이 틀렸을 때 오답을 드러내는 자리다


def test_a_holiday_outranks_a_startup_in_progress():
    """휴장일에 기동 스크립트가 도는 것 자체가 이상 신호다 — 「기동 중」으로 덮으면 사라진다.

    시각은 감시 창(07:40~) **안**이어야 한다. 07:30 기동 자체는 창 밖이라 이 분기까지 오지도
    않는다 — 휴장일 사유가 의미를 갖는 것은 재기동이 일어날 수 있는 구간뿐이다.
    """
    decision = liveness.decide(
        None, datetime(2026, 8, 17, 7, 45), starting=True, holiday="광복절 대체공휴일",
    )
    assert decision.reason == liveness.REASON_HOLIDAY


def test_no_holiday_keeps_the_old_judgement_exactly():
    """`holiday`를 안 주면 08-17 이전과 판정이 한 글자도 달라지지 않아야 한다."""
    died_at = datetime(2026, 8, 6, 10, 4)  # 목요일
    assert liveness.decide(_beat(died_at), died_at + timedelta(seconds=181)) == liveness.decide(
        _beat(died_at), died_at + timedelta(seconds=181), holiday=None
    )


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


# ===== 적재 감시 (2026-08-14 §2-1 / Fix#2) =====
#
# 08-14 14:00~15:23, 옵션체인이 84분 연속으로 비었는데 워치독 판정은 49건 전부 OK였다.
# 아래 다섯 중 넷은 **오경보를 막는 쪽**이다 — 이 배지는 재기동을 유발하지 않는 대신
# 사람이 매일 보는 것이라, 한 번 무시되기 시작하면 없는 배지와 같아진다.


_AFTERNOON = datetime(2026, 8, 14, 14, 10)


def test_a_live_loop_that_ingests_nothing_is_degraded():
    """**이 절의 존재 이유.** 박동은 30초마다 정확했고 적재는 84분간 0이었다."""
    decision = liveness.decide(
        _beat(_AFTERNOON - timedelta(seconds=20)), _AFTERNOON, ingest_minutes_recent=0
    )
    assert decision.action == liveness.ACTION_DEGRADED
    assert decision.reason == liveness.REASON_NO_INGEST
    assert decision.should_alert is True
    assert "적재" in decision.detail


def test_degraded_never_restarts():
    """08-14의 원인은 KIS 지연이었다 — 재기동은 아무것도 안 고치고 관측만 끊는다."""
    now = _AFTERNOON
    decision = liveness.decide(_beat(now - timedelta(seconds=20)), now, ingest_minutes_recent=0)
    state = liveness.next_state({"date": "2026-08-14", "restarts": 0}, now, decision)
    assert decision.action != liveness.ACTION_RESTART
    assert state["restarts"] == 0


def test_unknown_ingest_falls_back_to_the_old_judgement():
    """DB를 못 읽은 것은 「적재 0」이 아니라 **「모른다」**다.

    여기서 None을 0으로 접으면 DB가 죽은 날 워치독이 매분 degraded를 외친다
    (2026-08-12 Fix#1: 감시자를 감시 대상에 묶지 마라).
    """
    decision = liveness.decide(
        _beat(_AFTERNOON - timedelta(seconds=20)), _AFTERNOON, ingest_minutes_recent=None
    )
    assert decision.action == liveness.ACTION_OK


def test_ingest_outside_the_regular_session_is_not_an_alert():
    """15:20 마감과 15:45 종료 사이는 폴링이 잦아드는 정상 구간이다 — 여기서 울리면 매일 운다."""
    late = datetime(2026, 8, 14, 15, 30)
    assert liveness.decide(_beat(late), late, ingest_minutes_recent=0).action == liveness.ACTION_OK
    early = datetime(2026, 8, 14, 8, 30)
    assert liveness.decide(_beat(early), early, ingest_minutes_recent=0).action == liveness.ACTION_OK


def test_a_dead_heartbeat_still_wins_over_the_ingest_check():
    """둘 다 이상이면 **조치가 있는 쪽**이 이겨야 한다 — 죽은 루프는 되살려야 한다."""
    died = datetime(2026, 8, 14, 14, 0)
    decision = liveness.decide(
        _beat(died), died + timedelta(seconds=181), ingest_minutes_recent=0
    )
    assert decision.action == liveness.ACTION_RESTART
    assert decision.reason == liveness.REASON_STALE


def test_degraded_alerts_are_throttled_like_the_others():
    now = _AFTERNOON
    state = {"date": "2026-08-14", "restarts": 0, "last_alert_at": (now - timedelta(seconds=60)).isoformat()}
    decision = liveness.decide(
        _beat(now - timedelta(seconds=20)), now, state, ingest_minutes_recent=0
    )
    assert decision.action == liveness.ACTION_DEGRADED
    assert decision.should_alert is False


def test_ingest_window_sits_inside_the_watch_window():
    """적재 창은 감시 창보다 좁아야 한다 — 넓으면 기동·종료 경계에서 매일 오경보가 난다."""
    assert liveness.WATCH_WINDOW_START <= liveness.INGEST_WATCH_START
    assert liveness.INGEST_WATCH_END <= liveness.WATCH_WINDOW_END
    # 08-14의 절벽(14:00 시작)은 이 창 안이다.
    assert liveness.in_ingest_window(datetime(2026, 8, 14, 14, 10))


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


# ===== 2026-08-17 — 의도적 정지 표식 =====
#
# 08-17에 워치독이 관측 루프를 세 번(15:00 / 15:06 / 15:28) 되살렸다. 세 번 다 사람이 코드를
# 고치려고 창을 닫은 직후였고, 세 번 다 판정 자체는 설계대로였다 — **입력에 「사람이 껐다」를
# 표현할 자리가 없었을 뿐이다.** 아래 테스트의 절반은 그 표식이 **너무 오래 살지 않게** 막는
# 쪽이다: 잘못 감시하면 재기동 1회지만, 잘못 침묵하면 08-06의 19분이 돌아온다.


def _stamp_marker(path, at: datetime) -> None:
    """표식 파일의 mtime을 지정 시각으로 맞춘다 — 판정이 내용이 아니라 mtime을 보므로."""
    path.write_text("intentional stop", encoding="utf-8")
    os.utime(path, (at.timestamp(), at.timestamp()))


def test_intentional_stop_marker_is_read_by_mtime_not_content(tmp_path):
    """cmd.exe가 쓴 날짜 문자열은 로케일을 탄다 — 파싱하면 조용히 오판한다(기동 표식과 같다)."""
    path = liveness.intentional_stop_path(tmp_path)
    _stamp_marker(path, _MORNING)
    assert liveness.intentional_stop_at(path, _MORNING + timedelta(minutes=5)) == _MORNING


def test_no_marker_means_the_watchdog_keeps_watching(tmp_path):
    assert liveness.intentional_stop_at(liveness.intentional_stop_path(tmp_path), _MORNING) is None


def test_yesterdays_marker_cannot_silence_todays_window(tmp_path):
    """밤사이 PC가 꺼져 아침 기동 스크립트가 못 돈 날 — 어제 15:45의 표식이 남아 있다.

    기동 스크립트의 `del`이 주 만료 경로이고 이 날짜 검사가 그 경로가 실패했을 때의 두 번째
    그물이다. 이게 없으면 **되살릴 것이 없는 아침에 워치독이 조용해진다.**
    """
    path = liveness.intentional_stop_path(tmp_path)
    _stamp_marker(path, datetime(2026, 8, 16, 15, 45))
    assert liveness.intentional_stop_at(path, datetime(2026, 8, 17, 7, 45)) is None


def test_a_newer_heartbeat_consumes_the_marker(tmp_path):
    """표식이 남은 채로 사람이 별도 터미널에서 루프를 띄운 경우(2026-07-21에 실제로 있었다).

    그 경로는 창 제목 규약을 안 타므로 기동 스크립트의 `del`을 거치지 않는다. 박동이 표식보다
    새로우면 **의도는 이미 소비된 것**이고, 그 뒤에 죽으면 되살려야 한다.
    """
    path = liveness.intentional_stop_path(tmp_path)
    stopped = datetime(2026, 8, 17, 15, 0)
    _stamp_marker(path, stopped)
    revived = _beat(stopped + timedelta(minutes=5))
    assert liveness.intentional_stop_at(path, stopped + timedelta(minutes=30), revived) is None
    # 표식보다 **앞선** 박동은 소비가 아니다 — 정지 직전의 마지막 박동이 그것이다.
    last = _beat(stopped - timedelta(seconds=30))
    assert liveness.intentional_stop_at(path, stopped + timedelta(minutes=30), last) == stopped


def test_an_unreadable_marker_falls_back_to_watching(tmp_path):
    """감시자는 의심스러울 때 **감시하는 쪽으로** 넘어져야 한다."""
    assert liveness.intentional_stop_at(tmp_path / "없는디렉터리" / "표식", _MORNING) is None


def test_write_intentional_stop_never_raises(tmp_path):
    """표식을 못 썼다고 종료가 막히면 안 된다 — 못 쓰면 종전처럼 되살아날 뿐이다."""
    blocker = tmp_path / "blocker"
    blocker.write_text("파일이라 하위 디렉터리를 만들 수 없다", encoding="utf-8")
    liveness.write_intentional_stop(blocker / "sub" / ".intentional_stop", _MORNING)  # 예외 없음


def test_write_then_read_marker_roundtrip(tmp_path):
    """방금 쓴 표식은 곧바로 유효해야 한다 — `main.py`의 Ctrl+C 경로가 이 왕복에 기댄다.

    `now`는 실제 시계를 쓴다. 판정이 보는 것은 **mtime**이고 그것은 OS가 찍으므로, 인자로
    과거 시각을 넘겨도 파일은 오늘 것이 된다(그래서 날짜 검사에 걸린다). 인자의 `now`는
    파일 **내용**에만 들어가고, 그 내용은 사람이 로그를 읽을 때를 위한 것이다.
    """
    path = liveness.intentional_stop_path(tmp_path)
    now = datetime.now()
    liveness.write_intentional_stop(path, now)
    assert path.exists()
    assert liveness.intentional_stop_at(path, now + timedelta(minutes=1)) is not None


# ----- 판정에 미치는 영향 -----


def test_the_2026_08_17_restarts_would_not_have_happened():
    """**이 절의 존재 이유.** 15:24:52에 사람이 껐고 15:28:02에 워치독이 되살렸다."""
    stopped = datetime(2026, 8, 17, 15, 24, 52)
    judged = datetime(2026, 8, 17, 15, 28, 2)
    # 표식이 없던 그날의 판정
    assert liveness.decide(_beat(stopped), judged).action == liveness.ACTION_RESTART
    # 표식이 있는 지금의 판정
    decision = liveness.decide(_beat(stopped), judged, stopped_at=stopped)
    assert decision.action == liveness.ACTION_IDLE
    assert decision.reason == liveness.REASON_INTENTIONAL_STOP
    assert "15:24:52" in decision.detail


def test_a_missing_heartbeat_is_also_held_when_stopped():
    """정식 종료 경로는 하트비트를 지운다 — 그래서 `missing`으로 오는 쪽도 막아야 한다."""
    stopped = datetime(2026, 8, 17, 15, 45)
    decision = liveness.decide(None, stopped + timedelta(minutes=1), stopped_at=stopped)
    assert decision.action == liveness.ACTION_IDLE


def test_the_default_keeps_the_old_judgement_exactly():
    """`stopped_at`을 안 주면 08-17 이전과 판정이 한 글자도 달라지지 않아야 한다."""
    died_at = datetime(2026, 8, 6, 10, 4)
    assert liveness.decide(_beat(died_at), died_at + timedelta(seconds=181)) == liveness.decide(
        _beat(died_at), died_at + timedelta(seconds=181), stopped_at=None
    )


def test_starting_wins_over_a_stop_marker():
    """기동 스크립트가 시작하며 정지 표식을 지우므로 둘이 겹치는 것은 그 사이 몇 밀리초뿐이다.

    그때는 **더 짧게 살고 더 구체적인** 쪽이 이겨야 로그가 사실과 맞는다.
    """
    now = datetime(2026, 8, 17, 15, 30)
    decision = liveness.decide(None, now, starting=True, stopped_at=now - timedelta(minutes=5))
    assert decision.action == liveness.ACTION_IDLE
    assert "기동" in decision.detail


def test_a_stop_never_costs_a_restart_or_an_alert():
    """08-17의 진짜 대가는 재기동 3회가 아니라 **그날 남은 감시 예산**이었다."""
    stopped = datetime(2026, 8, 17, 15, 0)
    state = {"date": "2026-08-17", "restarts": 0, "last_alert_at": None}
    decision = liveness.decide(_beat(stopped), stopped + timedelta(minutes=10), state,
                               stopped_at=stopped)
    assert decision.should_alert is False
    assert liveness.next_state(state, stopped + timedelta(minutes=10), decision)["restarts"] == 0


def test_a_stop_outside_the_window_is_still_plain_idle():
    """감시 창 밖이라는 사실이 먼저다 — 표식이 그 판정을 가로채면 사유가 흐려진다."""
    night = datetime(2026, 8, 17, 22, 0)
    decision = liveness.decide(None, night, stopped_at=night - timedelta(hours=6))
    assert decision.action == liveness.ACTION_IDLE
    assert decision.reason is None


# ----- 콘솔 제어 이벤트 (2026-08-17 2차) -----
#
# 08-17의 세 번의 정지는 전부 **창 닫기**였다 — 그날 로그에 `Ctrl+C로 종료합니다`가 0건인데
# 프로세스는 정상 사이클 로그 한복판에서 끊겼다. 표식을 쓸 자리가 파이썬 코드 안에 없었다.
# 아래 테스트는 그 자리를 만들되 **너무 넓게 잡지 않았는지**를 본다.


def test_the_close_button_is_an_intentional_stop():
    assert liveness.is_intentional_console_stop(liveness.CTRL_CLOSE_EVENT) is True


def test_ctrl_c_is_left_to_python():
    """여기서 잡아채면 `KeyboardInterrupt` 자체가 사라진다 — 표식 하나 얻자고 종료를 부순다."""
    assert liveness.is_intentional_console_stop(liveness.CTRL_C_EVENT) is False
    assert liveness.is_intentional_console_stop(liveness.CTRL_BREAK_EVENT) is False


def test_a_reboot_is_not_an_intentional_stop():
    """**재부팅 한 번이 그날 감시를 통째로 끄면 안 된다.**

    장전 기동은 07:30 주간 트리거라 낮에 재부팅해도 다시 안 돈다. 그날 시스템을 되살릴 수
    있는 유일한 주체가 워치독인데, 로그오프/셧다운에 표식을 남기면 그 주체가 침묵한다.
    창 하나를 닫는 것과 PC를 내리는 것은 **의도의 범위가 다르다.**
    """
    assert liveness.is_intentional_console_stop(liveness.CTRL_LOGOFF_EVENT) is False
    assert liveness.is_intentional_console_stop(liveness.CTRL_SHUTDOWN_EVENT) is False


def test_unknown_console_events_are_not_intentional():
    """모르는 이벤트는 의도가 아니다 — 의심스러우면 감시하는 쪽으로 넘어진다."""
    assert liveness.is_intentional_console_stop(99) is False


@pytest.mark.skipif(os.name != "nt", reason="SetConsoleCtrlHandler는 Windows 전용")
def test_the_handler_actually_installs_on_windows():
    """등록 자체가 되는지 본다. **이벤트를 흉내 낼 수는 없다** — 창 닫기는 OS만 보낼 수 있고,
    `GenerateConsoleCtrlEvent`는 Ctrl+C/Break만 보낼 수 있다. 실제 창 닫기 검증은 손으로 했다
    (DECISION_LOG 2026-08-17 2차)."""
    assert liveness.install_console_stop_handler(lambda: None) is True
    assert liveness._console_handler_ref is not None  # GC가 가져가면 OS가 죽은 콜백을 부른다


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
