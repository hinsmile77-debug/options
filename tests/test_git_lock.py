"""버려진 `.git/index.lock` 청소 — **지워도 되는 경우만 지운다** (2026-08-19).

0바이트 락이 이틀 연속 남아 다음 git 작업을 전부 막았다(08-18 16:20 · 08-19 12:41, 각각 자동
점검 세션이 마지막 산출물을 쓴 그 분). 원인은 트리 킬이고, 재현은 스크래치 클론에서
`git add` 45ms 지점에 실바이너리를 `TerminateProcess`해서 확인했다.

**이 파일의 절반은 「안 지우는 경우」를 지킨다.** 지우는 청소부의 위험은 안 지울 때가 아니라
잘못 지울 때 나오고, 그 실패는 남의 작업을 깨면서 조용하다.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from mahdi import git_lock

_NOW = datetime(2026, 8, 19, 17, 0, 0)
_OLD = git_lock.STALE_LOCK_MIN_AGE_SECONDS + 60


def _lock(tmp_path, *, size: int = 0, age_seconds: float = _OLD):
    git_dir = tmp_path / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)
    lock = git_dir / "index.lock"
    lock.write_bytes(b"x" * size)
    stamp = (_NOW - timedelta(seconds=age_seconds)).timestamp()
    os.utime(lock, (stamp, stamp))
    return lock


# ===== 순수 판정 — 세 조건을 **전부** 요구한다 =====


def test_the_observed_artifact_is_swept():
    """08-18 16:20 · 08-19 12:41에 남은 것이 정확히 이 모양이다: 0바이트 · 오래됨 · git 부재."""
    assert git_lock.is_stale(size=0, age_seconds=_OLD, git_running=False) is True


def test_a_lock_with_content_is_never_touched():
    """내용이 있으면 쓰는 중이거나 쓰다 만 것이다 — 우리가 판단할 일이 아니다."""
    assert git_lock.is_stale(size=1, age_seconds=_OLD, git_running=False) is False


def test_a_young_lock_is_never_touched():
    """실측 최장 보유는 `git add` 101ms다. 임계는 그 6,000배 — 정상 작업과 겹칠 수 없다."""
    assert git_lock.is_stale(size=0, age_seconds=5, git_running=False) is False
    assert git_lock.is_stale(
        size=0, age_seconds=git_lock.STALE_LOCK_MIN_AGE_SECONDS - 1, git_running=False
    ) is False


def test_a_running_git_owns_its_lock():
    assert git_lock.is_stale(size=0, age_seconds=_OLD, git_running=True) is False


def test_not_knowing_is_not_the_same_as_absent():
    """**이 모듈에서 가장 중요한 한 줄이다.**

    프로세스 조회가 실패하면 `None`이고, `None`을 「없다」로 접으면 조회가 실패한 순간
    이 청소부가 가장 위험해진다 — 아무도 안 보는 상태에서 남의 락을 지운다.
    `market_calendar`가 못 읽은 달력을 「거래일」로 접는 것과 방향이 반대인 이유는
    대가가 반대이기 때문이다(저쪽은 접어야 감시가 계속되고, 이쪽은 접으면 작업을 깬다).
    """
    assert git_lock.is_stale(size=0, age_seconds=_OLD, git_running=None) is False


# ===== 실제 청소 — 파일이 실제로 사라지고, 사라진 사실이 반환된다 =====


def test_sweep_removes_the_orphan_and_reports_it(tmp_path):
    """**조용히 치우지 않는다**(계명 12) — 호출측이 로그에 남기고 지표가 그것을 센다."""
    lock = _lock(tmp_path)
    swept = git_lock.sweep(tmp_path, _NOW, probe=lambda: False)
    assert not lock.exists()
    assert len(swept) == 1
    assert swept[0]["size"] == 0
    assert swept[0]["age_minutes"] >= git_lock.STALE_LOCK_MIN_AGE_SECONDS / 60


def test_sweep_leaves_a_live_lock_alone(tmp_path):
    lock = _lock(tmp_path)
    assert git_lock.sweep(tmp_path, _NOW, probe=lambda: True) == []
    assert lock.exists()


def test_sweep_leaves_the_lock_when_it_cannot_tell(tmp_path):
    lock = _lock(tmp_path)
    assert git_lock.sweep(tmp_path, _NOW, probe=lambda: None) == []
    assert lock.exists()


def test_sweep_leaves_a_non_empty_lock_alone(tmp_path):
    lock = _lock(tmp_path, size=64)
    assert git_lock.sweep(tmp_path, _NOW, probe=lambda: False) == []
    assert lock.exists()


def test_the_expensive_probe_does_not_run_on_a_quiet_day(tmp_path):
    """**싼 조건이 먼저다.** 잔재는 드무므로 `tasklist`가 매분 도는 비용이 생기면 안 된다.

    워치독은 1분 주기이고, 08-12에 재기동이 파이프에 물려 5시간 31분 막힌 전례가 있다 —
    그 자리에 매분 프로세스 조회를 새로 놓을 이유가 없다.
    """
    calls = []

    def probe():
        calls.append(1)
        return False

    (tmp_path / ".git").mkdir()
    assert git_lock.sweep(tmp_path, _NOW, probe=probe) == []  # 락 자체가 없다
    _lock(tmp_path, age_seconds=5)                            # 있어도 너무 어리다
    assert git_lock.sweep(tmp_path, _NOW, probe=probe) == []
    assert calls == [], "싼 조건이 거르기 전에 프로세스를 조회했다"


def test_a_repo_without_a_lock_is_silent(tmp_path):
    (tmp_path / ".git").mkdir()
    assert git_lock.sweep(tmp_path, _NOW, probe=lambda: False) == []


def test_sweep_never_raises_on_a_broken_repo(tmp_path):
    """청소부가 워치독을 죽이면 본말이 뒤집힌다 — `.git`이 없어도 조용히 넘어간다."""
    assert git_lock.sweep(tmp_path / "없는저장소", _NOW, probe=lambda: False) == []


def test_only_index_lock_is_in_range(tmp_path):
    """**삭제하는 청소부의 사정거리는 관측된 것까지만.**

    `HEAD.lock`·`config.lock`은 보유 시간이 마이크로초 단위라 트리 킬과 겹칠 확률이 사실상
    0이고 관측된 적도 없다. 넓히는 순간 그 파일들의 안전 조건을 따로 논증해야 한다.
    """
    assert git_lock.INDEX_LOCK_RELPATH == ("index.lock",)
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    head = git_dir / "HEAD.lock"
    head.write_bytes(b"")
    stamp = (_NOW - timedelta(seconds=_OLD)).timestamp()
    os.utime(head, (stamp, stamp))
    assert git_lock.sweep(tmp_path, _NOW, probe=lambda: False) == []
    assert head.exists()


# ===== 프로세스 조회 자체 =====


def test_the_probe_answers_or_admits_it_cannot():
    """실제 조회는 `True`/`False`/`None` 셋 중 하나여야 한다 — 예외를 밖으로 내지 않는다."""
    assert git_lock.git_processes_running() in (True, False, None)
