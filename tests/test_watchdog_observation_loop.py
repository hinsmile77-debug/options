"""워치독 재기동 경로의 계약 — **재기동이 워치독 자신을 멈추면 안 된다** (2026-08-12 §2-3 / Fix#1).

## 이 파일이 지키는 것

08-12 10:14:01, 워치독은 관측 루프의 죽음을 정확히 판정하고 정확히 되살렸다. 기동 스크립트는
10:14:12에 **10초 만에** 끝났다(`premarket_startup.log`가 증인이다). 그런데 워치독은
「300초 안에 끝나지 않음 = 재기동 실패」를 기록했고, **15:45:02까지 매달려 있었다.**

원인은 `subprocess.run(..., capture_output=True)` 한 인자였다. 기동 스크립트는
`start "..." cmd /k ...`로 COCKPIT과 관측 루프를 새 창에 띄우는데, 그 손자 프로세스들이
부모가 만든 파이프 핸들을 **상속**한다. bat이 끝나도 파이프가 안 닫히므로 부모는 EOF를 못 받고,
`TimeoutExpired` 처리 경로가 자식을 죽인 뒤 `communicate()`를 다시 부르면서 또 막힌다 —
**`timeout` 값조차 상한이 못 된다.**

작업 스케줄러가 `MultipleInstances=IgnoreNew`라 그동안 매분 실행이 전부 무시됐다:
10:20~15:40 사이 `watchdog.log`에 `OK` 줄이 **한 개도 없다.**

## 왜 이 테스트가 진짜 재현인가

`_restart()`가 부르는 것을 가짜로 바꾸지 않는다. **실제 `subprocess.run`으로 실제 bat을 돌리고**,
그 bat이 `start`로 오래 사는 손자를 남긴 채 즉시 끝난다 — 08-12와 같은 형태다.
`capture_output=True`로 되돌리면 이 테스트가 **손자의 수명만큼 걸려** 실패한다.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import time
from datetime import datetime

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="`start`로 새 콘솔을 띄우는 cmd 배치가 있어야 재현되는 계약이다(운영 환경은 Windows).",
)

watchdog = importlib.import_module("scripts.watchdog_observation_loop")

# 손자 프로세스의 수명. 이 값보다 **한참 짧게** 반환해야 통과한다 — 버그가 있으면 반환이 이
# 시간(또는 그 이상)에 묶인다. 8초로 잡은 이유: CI/느린 디스크에서도 `cmd` 기동 지터(~1초)와
# 확실히 구분되고, 테스트 전체가 10초를 안 넘긴다.
_GRANDCHILD_SECONDS = 8
_MUST_RETURN_WITHIN = 3.0


@pytest.fixture()
def bat_that_spawns_a_window(tmp_path):
    """`start`로 오래 사는 창을 띄우고 **즉시** 끝나는 배치 — 기동 스크립트와 같은 형태."""
    script = tmp_path / "fake_start.bat"
    script.write_text(
        "@echo off\r\n"
        f'start "Mahdi Test Grandchild" cmd /c "ping -n {_GRANDCHILD_SECONDS + 1} 127.0.0.1 >nul"\r\n'
        "echo BAT_DONE\r\n",
        encoding="ascii",
        newline="",
    )
    yield script
    subprocess.run(
        ["taskkill", "/F", "/FI", "WINDOWTITLE eq Mahdi Test Grandchild*"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )


def test_restart_returns_as_soon_as_the_batch_exits(monkeypatch, bat_that_spawns_a_window):
    """**08-12 재현.** 기동 스크립트가 끝나면 `_restart()`도 끝나야 한다.

    `capture_output=True`로 되돌리면 손자가 파이프를 쥐고 있어 여기서 8초 이상 걸린다
    (실측으로는 `timeout=8`을 준 재현에서 350.1초가 걸렸다 — 타임아웃은 상한이 아니다).
    """
    monkeypatch.setattr(watchdog, "START_SCRIPT", bat_that_spawns_a_window)

    started = time.monotonic()
    ok, summary = watchdog._restart()
    elapsed = time.monotonic() - started

    assert ok, summary
    assert elapsed < _MUST_RETURN_WITHIN, (
        f"기동 스크립트는 즉시 끝났는데 _restart()가 {elapsed:.1f}초 걸렸다 — "
        "손자 프로세스가 표준 출력 파이프를 쥐고 있다(08-12 §2-3의 그 버그)."
    )


def test_restart_does_not_capture_output():
    """계약을 **호출 인자 수준에서** 못 박는다.

    위 테스트는 증상을 잡고 이 테스트는 원인을 잡는다 — 누군가 `capture_output=True`를 되살리면
    (예: "디버깅용으로 잠깐") 위 테스트는 환경에 따라 통과할 수도 있지만 이건 반드시 깨진다.
    """
    recorded: dict = {}

    class _Result:
        returncode = 0

    def fake_run(cmd, **kwargs):
        recorded.update(kwargs)
        return _Result()

    original = subprocess.run
    subprocess.run = fake_run  # noqa: S RUF — 모듈 전역을 직접 갈아끼운다(monkeypatch 없이도 복원)
    try:
        watchdog._restart()
    finally:
        subprocess.run = original

    assert recorded.get("capture_output") is None, (
        "capture_output은 손자 프로세스에 파이프를 상속시킨다 — 08-12에 워치독을 5시간 31분 세웠다."
    )
    assert recorded.get("stdout") is subprocess.DEVNULL
    assert recorded.get("stderr") is subprocess.DEVNULL


def test_restart_timeout_is_generous_enough_for_a_slow_docker():
    """**타임아웃을 줄이지 못하게 막는다.**

    08-12 보고서 초안이 "실측 10초니까 120초로 줄이자"고 적었는데 그것은 틀렸다 — 기동
    스크립트의 Docker 대기(최대 180초)는 `cmd /c bat`의 **안**에서 흐르므로 이 타임아웃에
    그대로 포함된다. 줄이면 Docker가 느린 아침마다 기동을 마이그레이션 중간에 죽인다.
    """
    assert watchdog._RESTART_TIMEOUT_SECONDS >= 300


def test_every_invocation_records_that_it_judged(tmp_path, monkeypatch):
    """Fix#8 — **판정했다는 사실 자체**를 남긴다. 감시 창 밖(IDLE)에도 남긴다.

    08-12에 워치독이 5시간 31분 막혀 있었는데 `watchdog.log`의 마지막 줄이 「RESTART」라
    사고 대응 중인 것처럼 보였다. 「돌긴 했는데 감시 창 밖이었다」와 「막혀서 조용하다」를
    가르려면 IDLE에도 기록이 있어야 한다.
    """
    from mahdi import liveness

    monkeypatch.setattr(watchdog, "LOG_DIR", tmp_path)
    monkeypatch.setattr(watchdog, "STATE_FILE", tmp_path / ".watchdog_state.json")
    monkeypatch.setattr(watchdog, "WATCHDOG_LOG", tmp_path / "watchdog.log")
    # 감시 창 밖(새벽) — 종전에는 여기서 아무 흔적도 안 남았다.
    monkeypatch.setattr(watchdog.db, "local_now", lambda: datetime(2026, 8, 12, 3, 0, 0))

    watchdog.main()

    check = liveness.read_watchdog_check(liveness.watchdog_check_path(tmp_path))
    assert check is not None, "IDLE에도 판정 기록이 남아야 한다"
    assert check["action"] == liveness.ACTION_IDLE
    assert check["at"] == datetime(2026, 8, 12, 3, 0, 0)


# ===== 2026-08-19 (08-18 보고서 §1-2 / Fix#4) — 장전 점검 미발화 판정 =====
#
# 이 판정 자체는 파일 존재 확인 한 번이라 플랫폼과 무관하지만, **파일 모듈의 `pytestmark`
# (win32 한정)를 그대로 물려받는다** — 운영 환경이 Windows이고, 여기만 예외로 빼면 이 파일의
# 스킵 조건이 두 갈래가 되어 다음 사람이 어느 쪽이 진짜인지 물어야 한다.
def test_before_the_open_it_never_fires(monkeypatch, tmp_path):
    """08:30 예정에서 30분 지터로 매일 울리면 그 경보는 곧 안 읽힌다 — 임계는 개장(09:00)이다."""
    monkeypatch.setattr(watchdog, "_CHECK_DOC_DIR", tmp_path)
    assert watchdog._premarket_check_missing(datetime(2026, 8, 18, 8, 59)) is False


def test_after_the_open_a_missing_document_fires(monkeypatch, tmp_path):
    monkeypatch.setattr(watchdog, "_CHECK_DOC_DIR", tmp_path)
    assert watchdog._premarket_check_missing(datetime(2026, 8, 18, 9, 0)) is True


def test_a_document_that_exists_silences_it(monkeypatch, tmp_path):
    monkeypatch.setattr(watchdog, "_CHECK_DOC_DIR", tmp_path)
    (tmp_path / "2026-08-18_점검_pre.md").write_text("x", encoding="utf-8")
    assert watchdog._premarket_check_missing(datetime(2026, 8, 18, 13, 28)) is False


def test_yesterdays_document_does_not_count_as_todays(monkeypatch, tmp_path):
    """**날짜가 요점이다.** 08-18에 08-17본이 있다고 오늘 점검이 뜬 것은 아니다."""
    monkeypatch.setattr(watchdog, "_CHECK_DOC_DIR", tmp_path)
    (tmp_path / "2026-08-17_점검_pre.md").write_text("x", encoding="utf-8")
    assert watchdog._premarket_check_missing(datetime(2026, 8, 18, 9, 30)) is True


def test_it_alerts_once_a_day_not_once_a_minute(monkeypatch, tmp_path):
    """09:00~15:45 매분이면 397건이다 — 08-15~16의 `ALERT_ONLY` 94·113줄을 반복하지 않는다."""
    sent: list[str] = []
    monkeypatch.setattr(watchdog, "_CHECK_DOC_DIR", tmp_path)
    monkeypatch.setattr(watchdog, "_MISSING_CHECK_STATE", tmp_path / "state.json")
    monkeypatch.setattr(watchdog, "WATCHDOG_LOG", tmp_path / "watchdog.log")
    monkeypatch.setattr(watchdog, "LOG_DIR", tmp_path)
    monkeypatch.setattr(watchdog.notify, "notify_sync", lambda msg, level="INFO": sent.append(msg))

    assert watchdog._alert_missing_premarket_check(datetime(2026, 8, 18, 9, 0)) is True
    assert watchdog._alert_missing_premarket_check(datetime(2026, 8, 18, 9, 1)) is False
    assert len(sent) == 1
    # 로그 줄은 `watchdog_metrics`가 세는 마커로 시작해야 한다(그쪽 계약 테스트의 짝).
    logged = (tmp_path / "watchdog.log").read_text(encoding="utf-8")
    assert f"] {watchdog._MISSING_CHECK_MARKER} —" in logged


def test_a_new_day_alerts_again(monkeypatch, tmp_path):
    sent: list[str] = []
    monkeypatch.setattr(watchdog, "_CHECK_DOC_DIR", tmp_path)
    monkeypatch.setattr(watchdog, "_MISSING_CHECK_STATE", tmp_path / "state.json")
    monkeypatch.setattr(watchdog, "WATCHDOG_LOG", tmp_path / "watchdog.log")
    monkeypatch.setattr(watchdog, "LOG_DIR", tmp_path)
    monkeypatch.setattr(watchdog.notify, "notify_sync", lambda msg, level="INFO": sent.append(msg))

    watchdog._alert_missing_premarket_check(datetime(2026, 8, 18, 9, 0))
    assert watchdog._alert_missing_premarket_check(datetime(2026, 8, 19, 9, 0)) is True
    assert len(sent) == 2
