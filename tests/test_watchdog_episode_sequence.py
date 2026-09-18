"""DEGRADED 줄이 **「오늘 몇 번째 삽화인가」**를 말한다 (2026-09-18 §1-10 / 제4부 P1-2).

09-18에 `no_ingest` 삽화가 **9회 · 누적 196분**이었다(정규장 380분의 49.7%). 그런데 그
「9회」는 로그가 말한 것이 아니다 — 장중②가 8회, 장후가 9회를 **사람 손으로 비-OK 줄을 묶어**
냈다. 08-21에 16분/3구간을 손으로 묶었던 것과 같은 자리이고, 그때 만든 답이
`track_degraded_episode()`(삽화 **안**의 분)였다. 이 축은 그 바깥, **삽화들 사이**다.

**이 파일이 지키는 것은 셋이다.**
1. 순번 문구가 **억제된다** — 삽화 1회당 최대 1줄이고, 09-18 재현으로도 8줄이다.
2. **판정 무변경** — 대장은 `track_degraded_episode()`에도 `next_state()`에도 되먹이지
   않고, 문구를 줄 끝에만 붙였으므로 `watchdog_metrics`의 `startswith` 집계가 안 바뀐다.
3. **규약 C** — 「순번을 말한 줄이 없었다」(0)와 「그날은 이 문구 자체가 없던 버전이다」
   (키 없음)가 갈린다.
"""

from __future__ import annotations

import importlib.util
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from mahdi import liveness
from mahdi.ops import watchdog_metrics

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "scripts" / "watchdog_observation_loop.py"

_DAY = date(2026, 9, 18)


def _at(hhmm: str) -> datetime:
    hour, minute = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 9, 18, hour, minute, 1)


def _run(spans: list[tuple[str, str]], ledger=None) -> tuple[dict | None, list[str]]:
    """삽화 구간 목록을 분 단위로 재생한다. 반환: (마지막 대장, 나온 순번 문구들).

    `track_degraded_episode()`를 **실제로 거쳐서** 대장을 먹인다 — 두 함수의 맞물림까지
    한 번에 검사하기 위해서다(대장만 단독으로 부르면 `since`가 언제 갈리는지를 못 본다).
    """
    notes: list[str] = []
    episode_state = None
    now = _at("09:00")
    end = _at("15:45")
    minutes = {}
    for since, until in spans:
        cursor = _at(since)
        while cursor <= _at(until):
            minutes[cursor] = True
            cursor += timedelta(minutes=1)

    while now <= end:
        action = liveness.ACTION_DEGRADED if now in minutes else liveness.ACTION_OK
        episode_state, _ongoing, _closing = liveness.track_degraded_episode(
            episode_state, now, action,
        )
        reason = liveness.REASON_NO_INGEST if now in minutes else None
        ledger, note = liveness.track_no_ingest_ledger(
            ledger, now, episode_state, reason,
        )
        if note:
            notes.append(note)
        now += timedelta(minutes=1)
    return ledger, notes


# ===== 억제 — 삽화 1회당 최대 한 줄 =====


def test_the_first_episode_of_the_day_says_nothing():
    """첫 삽화엔 「직전」이 없다. 「오늘 1번째」는 아무것도 말해 주지 않으면서
    평범한 하루마다 한 줄을 더한다 — 그래서 안 붙인다."""
    _ledger, notes = _run([("10:29", "10:32")])
    assert notes == []


def test_the_second_episode_names_its_number_and_the_gap():
    _ledger, notes = _run([("10:29", "10:32"), ("10:54", "11:04")])
    assert len(notes) == 1
    assert notes[0] == "오늘 2번째 삽화 · 직전 삽화(10:29~10:32, 4분) 종료로부터 22분"


def test_the_note_rides_only_the_first_minute():
    """11분짜리 삽화에 순번 줄은 **한 번**이다. 매 분 붙으면 억제가 통째로 풀린다."""
    _ledger, notes = _run([("10:29", "10:32"), ("10:54", "11:04")])
    assert len(notes) == 1


# ===== 09-18의 재현 — 이 파일이 존재하는 이유 =====

_2026_09_18 = [
    ("10:29", "10:32"), ("10:54", "11:04"), ("11:24", "11:27"),
    ("11:37", "12:39"), ("12:53", "12:55"), ("13:07", "13:17"),
    ("13:27", "14:27"), ("14:39", "14:44"), ("14:54", "15:26"),
]


def test_the_2026_09_18_day_is_counted_to_nine():
    """그날 사람이 손으로 묶어 낸 9회를 로그가 스스로 말한다."""
    ledger, notes = _run(_2026_09_18)
    assert len(ledger["episodes"]) == 9
    # 첫 삽화엔 순번이 없으므로 줄은 여덟이다.
    assert len(notes) == 8
    assert notes[-1].startswith("오늘 9번째 삽화 · 직전 삽화(14:39~14:44, 6분) 종료로부터 10분")


def test_the_2026_09_18_day_stays_inside_the_line_budget():
    """§5 억제 규약 — 하루 30줄을 넘기면 억제가 안 듣는 것이다.

    역대 최다인 이날조차 순번 줄은 8줄이고, 같은 줄에 붙는 회복 참고 문구(그날 14줄)와
    합쳐도 예산 안이다. 그리고 둘 다 **기존 줄 끝에** 붙으므로 새 줄은 0이다.
    """
    _ledger, notes = _run(_2026_09_18)
    assert len(notes) <= 30


def test_every_gap_is_measured_from_the_previous_episode_end():
    _ledger, notes = _run(_2026_09_18)
    gaps = [int(note.rsplit("종료로부터 ", 1)[1].removesuffix("분")) for note in notes]
    assert gaps == [22, 20, 10, 14, 12, 10, 12, 10]


# ===== 규약 C — 「없었다」와 「셀 수 없었다」 =====


def test_a_quiet_day_produces_no_ledger_at_all():
    ledger, notes = _run([])
    assert ledger is None
    assert notes == []


def test_a_new_day_rewinds_the_count():
    """어제 대장을 들고 오늘을 시작해도 순번은 1부터다 — 어제 삽화를 오늘로 세지 않는다."""
    yesterday = {
        "date": "2026-09-17",
        "episodes": [{"since": "2026-09-17T14:00:01", "last_at": "2026-09-17T14:30:01",
                      "minutes": 31}],
    }
    ledger, notes = _run([("10:29", "10:32")], ledger=yesterday)
    assert ledger["date"] == "2026-09-18"
    assert len(ledger["episodes"]) == 1
    assert notes == []


def test_a_broken_ledger_does_not_raise_and_starts_over():
    """못 읽은 과거를 지어내지 않는다. 순번이 되감기는 것은 「삽화가 없었다」가 아니라
    **「셀 수 없었다」**이고, 문구가 안 나오는 것으로 드러난다."""
    for broken in ({"date": "2026-09-18", "episodes": "밥"},
                   {"date": "2026-09-18", "episodes": [{"since": "?"}]},
                   {"episodes": []},
                   {}):
        ledger, notes = _run([("10:29", "10:32")], ledger=broken)
        assert ledger["date"] == "2026-09-18"
        assert notes == []


def test_a_gap_in_the_record_starts_a_new_episode():
    """무기록 뒤 재시작(`stale`)은 **이어 세지 않고 새로 센다** — 그 사이를 못 봤기 때문이다.
    `minutes == 1`이 아니라 `since`로 가르는 이유가 이것이다."""
    ledger, notes = _run([("10:29", "10:32"), ("10:54", "10:56")])
    assert len(ledger["episodes"]) == 2
    assert len(notes) == 1


# ===== 판정 무변경 — B등급 항목의 값 =====


def test_a_heartbeat_degraded_is_not_counted_as_an_ingest_episode():
    """`no_ingest`가 아닌 DEGRADED는 이 대장의 축이 아니다(규약 E — 공유 축에 얹지 않는다)."""
    ledger, note = liveness.track_no_ingest_ledger(
        None, _at("10:29"),
        {"date": "2026-09-18", "since": "2026-09-18T10:29:01",
         "last_at": "2026-09-18T10:29:01", "minutes": 1},
        "no_heartbeat",
    )
    assert ledger is None
    assert note is None


def test_the_ledger_never_changes_the_episode_judgement():
    """**판정 무변경.** 대장을 돌려도 `track_degraded_episode()`의 세 반환값이 한 글자도
    안 바뀐다 — 대장은 그 결과를 **읽기만** 하고 되먹이지 않는다."""
    now = _at("10:29")
    episode = None
    without = []
    for _ in range(5):
        episode, ongoing, closing = liveness.track_degraded_episode(
            episode, now, liveness.ACTION_DEGRADED,
        )
        without.append((dict(episode), ongoing, closing))
        now += timedelta(minutes=1)

    now = _at("10:29")
    episode = None
    ledger = None
    with_ledger = []
    for _ in range(5):
        episode, ongoing, closing = liveness.track_degraded_episode(
            episode, now, liveness.ACTION_DEGRADED,
        )
        ledger, _note = liveness.track_no_ingest_ledger(
            ledger, now, episode, liveness.REASON_NO_INGEST,
        )
        with_ledger.append((dict(episode), ongoing, closing))
        now += timedelta(minutes=1)

    assert without == with_ledger


def test_the_ongoing_note_is_untouched():
    """09-16이 확인한 「연속 N분째(HH:MM부터)」는 그대로다 — 순번 줄은 그 **뒤에** 붙는다."""
    episode, ongoing, _closing = liveness.track_degraded_episode(
        None, _at("10:29"), liveness.ACTION_DEGRADED,
    )
    assert ongoing == "연속 1분째(10:29부터)"
    _episode2, ongoing2, _c2 = liveness.track_degraded_episode(
        episode, _at("10:30"), liveness.ACTION_DEGRADED,
    )
    assert ongoing2 == "연속 2분째(10:29부터)"


# ===== 판정 무변경 — 파서 =====


def _degraded_line(hhmm: str, tail: str = "") -> str:
    body = (
        f"[2026-09-18 {hhmm}:01] DEGRADED — 관측 루프 적재 정지(no_ingest) — "
        "직전 10분 동안 옵션체인 적재가 **0분**이다 · 연속 1분째"
    )
    return body + tail


def test_the_sequence_note_does_not_move_the_startswith_tallies():
    """**판정 무변경.** 문구는 줄 **끝**에만 붙으므로 `degraded_checks` · `restarts` ·
    `recovered_episodes`가 문구 유무에 상관없이 같다(09-03 P2-3이 세운 규약)."""
    bare = [_degraded_line("10:29"), _degraded_line("10:30")]
    tagged = [
        _degraded_line("10:29"),
        _degraded_line("10:30", " · 오늘 2번째 삽화 · 직전 삽화(10:00~10:10, 11분) 종료로부터 20분"),
    ]
    a = watchdog_metrics.parse(bare, _DAY)
    b = watchdog_metrics.parse(tagged, _DAY)
    for key in ("degraded_checks", "restarts", "recovered_episodes",
                "restart_failures", "alert_only", "checks"):
        assert a[key] == b[key], key


def test_the_new_axis_counts_only_the_tagged_lines():
    tagged = [
        _degraded_line("10:29"),
        _degraded_line("10:30", " · 오늘 2번째 삽화 · 직전 삽화(10:00~10:10, 11분) 종료로부터 20분"),
        _degraded_line("10:31", " · 오늘 3번째 삽화 · 직전 삽화(10:30~10:30, 1분) 종료로부터 1분"),
    ]
    metrics = watchdog_metrics.parse(tagged, _DAY)
    assert metrics["episode_sequence_notes"] == 2
    assert metrics["degraded_checks"] == 3


def test_the_axis_is_present_even_when_nothing_was_tagged():
    """**규약 C.** 키가 있고 0이면 「순번을 말한 줄이 없었다」이고, 키가 없으면 「그날은 이
    문구 자체가 없던 버전이다」 — 09-18 이전 로그가 전부 그쪽이다."""
    metrics = watchdog_metrics.parse([_degraded_line("10:29")], _DAY)
    assert metrics["episode_sequence_notes"] == 0

    quiet = watchdog_metrics.parse(["[2026-09-18 10:00:01] OK — 정상"], _DAY)
    assert quiet["episode_sequence_notes"] == 0


# ===== 스크립트 배선 =====


@pytest.fixture(scope="module")
def loop():
    spec = importlib.util.spec_from_file_location("watchdog_loop_p1_2", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_ledger_lives_in_its_own_file(loop):
    """⛔ 삽화 상태 파일에 얹지 않는다 — 그 파일은 삽화가 닫힐 때 **지워진다**.
    얹었으면 이 카운터가 영원히 1에서 멈춘다."""
    assert loop._NO_INGEST_LEDGER_STATE != loop._DEGRADED_EPISODE_STATE
    assert loop._NO_INGEST_LEDGER_STATE.name == ".watchdog_no_ingest_ledger.json"


def test_the_ledger_survives_a_closed_episode(loop, tmp_path):
    """이것이 이 파일을 나눈 이유다 — 삽화가 닫혀도 순번은 안 되감긴다."""
    ledger, _note = liveness.track_no_ingest_ledger(
        None, _at("10:29"),
        {"date": "2026-09-18", "since": "2026-09-18T10:29:01",
         "last_at": "2026-09-18T10:32:01", "minutes": 4},
        liveness.REASON_NO_INGEST,
    )
    # 삽화가 닫힌 분 — `track_degraded_episode()`는 None을 낸다(호출측이 파일을 지운다).
    ledger, note = liveness.track_no_ingest_ledger(ledger, _at("10:33"), None, None)
    assert note is None
    assert len(ledger["episodes"]) == 1
    # 다음 삽화는 **2번째**로 세어진다.
    _ledger, note = liveness.track_no_ingest_ledger(
        ledger, _at("10:54"),
        {"date": "2026-09-18", "since": "2026-09-18T10:54:01",
         "last_at": "2026-09-18T10:54:01", "minutes": 1},
        liveness.REASON_NO_INGEST,
    )
    assert note.startswith("오늘 2번째 삽화")
