"""증거 수집기는 **저장소를 잠글 수 없다** (2026-08-19).

## 왜 이 계약이 필요한가

`.git/index.lock` 0바이트 잔재가 이틀 연속 저장소를 막았다(08-18 16:20 · 08-19 12:41).
두 락 모두 **이 수집기를 돌린 세션이 마지막 산출물을 쓴 그 분**에 생겼다. 락을 쥔 것은
§1 「코드·커밋 상태」의 `git status --porcelain`이다 — status는 다음 실행을 빠르게 하려고
인덱스 캐시를 갱신하고, 그 갱신에 락이 필요하다(실측 5~6ms 보유 · 0바이트).
그 순간 세션 teardown의 트리 킬을 맞으면 atexit 정리가 안 돌아 락이 남는다.

`GIT_OPTIONAL_LOCKS=0`이 그 선택적 갱신을 포기하게 한다 — **락을 못 만들면 트리 킬이 락을
남길 수 없다.** 워치독의 청소(`mahdi.git_lock.sweep`)보다 한 자리 앞선 조치다.

## 왜 지표가 아니라 테스트가 지키는가

이 fix는 **일별 지표로 검정되지 않는다**(`hypotheses.yaml` `2026-08-19-fix10`의 `주의` 참고):
락이 남으려면 세션이 하필 그 순간 죽어야 하고, 그것은 우리가 못 정하는 조건이다.
안 죽은 날의 「청소 0건」은 이 fix의 증명이 아니다(규약 C). 그래서 계약을 여기서 못 박는다.
"""

from __future__ import annotations

import importlib.util
import subprocess
import threading
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COLLECTOR = PROJECT_ROOT / "docs" / "동작점검" / "tools" / "collect_evidence.py"


def _load_collector():
    """`tools/`는 패키지가 아니라 경로로 읽는다(`scripts/`를 얇게 두는 규약과 같은 자리)."""
    spec = importlib.util.spec_from_file_location("collect_evidence", COLLECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_collector_disables_optional_git_locks():
    """**설정이 사라지면 저장소가 다시 잠길 수 있다.** 그 한 줄을 여기서 고정한다."""
    collector = _load_collector()
    assert collector._GIT_ENV.get("GIT_OPTIONAL_LOCKS") == "0", (
        "GIT_OPTIONAL_LOCKS=0이 없다 — `git status`가 인덱스 락을 쥐고, 그 순간 세션이 "
        "트리 킬을 맞으면 0바이트 index.lock이 남아 저장소가 잠긴다(08-18·08-19에 실제로 그랬다)"
    )


def test_the_collector_never_waits_on_stdin():
    """git이 자격증명·pager로 입력을 기다리면 `timeout` 만료까지 세션이 매달린다.

    08-18 14:28에 고아 `git log --oneline -10` 쌍이 **3시간 50분** 살아 있었다.
    그 원인은 확정되지 않았지만, 입력 대기는 우리가 지금 막을 수 있는 경로다.
    """
    source = COLLECTOR.read_text(encoding="utf-8")
    assert "stdin=subprocess.DEVNULL" in source, "run_git이 표준 입력을 끊지 않는다"


def test_git_status_with_this_setting_creates_no_index_lock():
    """**설정이 실제로 락을 없애는가** — 상수만 보지 않고 진짜 git으로 확인한다.

    실측(343개 touch 후 = 최악 조건): 기본은 5~6회 관측, 이 설정이면 0회.
    """
    lock = PROJECT_ROOT / ".git" / "index.lock"
    if lock.exists():
        pytest.skip("이미 락이 있다 — 다른 git 작업 중이므로 판정할 수 없다")

    collector = _load_collector()
    seen: list[int] = []
    stop = threading.Event()

    def poll():
        while not stop.is_set():
            if lock.exists():
                seen.append(1)
            time.sleep(0.001)

    watcher = threading.Thread(target=poll)
    watcher.start()
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"], cwd=PROJECT_ROOT, capture_output=True,
            stdin=subprocess.DEVNULL, env=collector._GIT_ENV, timeout=60,
        )
    finally:
        time.sleep(0.03)
        stop.set()
        watcher.join()

    assert result.returncode == 0, "git status가 실패했다 — 이 테스트는 저장소 안에서만 유효하다"
    assert not seen, (
        f"이 설정으로도 index.lock이 생겼다({len(seen)}회 관측) — GIT_OPTIONAL_LOCKS가 "
        "이 git 버전에서 안 먹거나 다른 경로가 락을 쥔 것이다"
    )


def test_the_setting_does_not_change_what_git_reports():
    """**대가가 정확성이면 안 된다.** 느려지는 것은 받아들이되 답이 달라지면 못 쓴다."""
    collector = _load_collector()
    plain = subprocess.run(
        ["git", "status", "--porcelain"], cwd=PROJECT_ROOT,
        capture_output=True, stdin=subprocess.DEVNULL, timeout=60,
    )
    guarded = subprocess.run(
        ["git", "status", "--porcelain"], cwd=PROJECT_ROOT, capture_output=True,
        stdin=subprocess.DEVNULL, env=collector._GIT_ENV, timeout=60,
    )
    assert plain.returncode == guarded.returncode == 0
    assert plain.stdout == guarded.stdout, "GIT_OPTIONAL_LOCKS=0이 보고 내용을 바꿨다"


def test_the_code_and_commit_section_is_still_collected():
    """**없애서 고치지 않았다는 것**을 지킨다.

    「말미 git 명령을 빼면 되지 않나」가 첫 제안이었지만, §1은 `phases.md`가 요구하는 판정
    근거다: *"커밋 시각 < 관측 루프 기동 시각이어야 한다"*. 이것으로 가설 상태를 `refuted`가
    아니라 `untested`로 가른다(2026-08-04 p4 — 15분 차이로 하루를 잃은 그 규약).
    누군가 「소음을 줄이려고」 이 절을 지우면 여기서 깨진다.
    """
    source = COLLECTOR.read_text(encoding="utf-8")
    assert '"status", "--porcelain"' in source, "미커밋 현황 수집이 사라졌다"
    assert '"rev-parse", "--short", "HEAD"' in source, "HEAD 수집이 사라졌다"
    assert "## 1. 코드·커밋 상태" in source, "§1 절 자체가 사라졌다"
