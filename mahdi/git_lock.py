"""버려진 `.git/index.lock` 청소 — **세션이 죽으면서 남긴 자물쇠를 연다** (2026-08-19 신설).

## 무엇이 일어났는가

`.git/index.lock`이 0바이트로 남아 다음 git 작업을 전부 막는 일이 이틀 연속 있었다:

    2026-08-18 16:20   08-18 EOD 자동 점검 세션이 보고서를 쓴 그 분(CreationTime 16:20:53)
    2026-08-19 12:41   08-19 장중 점검 세션이 `_점검_intra.md`를 쓴 그 분

두 번 다 **git 프로세스는 하나도 살아 있지 않았고** 락은 0바이트였다. 그리고 08-18 14:28부터
세 시간 넘게 산 고아 `git log --oneline -10` 쌍이 같은 자리에 있었다 — **한 원인의 두 얼굴**이다.

## 원인 (2026-08-19 실측으로 확정)

인덱스를 쓰는 git 명령은 락을 **먼저 만들고 내용은 마지막에 쓴다.** 이 저장소 실측:

    git status (추적 341개 touch 후)   총 331ms 중 락 보유 끝부분 ~3ms · 0바이트
    git add    (문서 12개)             총 147ms 중 **101ms(69%)를 0바이트 락으로** 보유

그 구간에 프로세스가 **TerminateProcess(트리 킬)** 로 죽으면 atexit 정리가 안 돌아 락이 남는다.
정상 실패(`die()`)로는 절대 안 남고, 래퍼(`cmd\\git.exe`)만 죽여도 안 남는다 — 실자식
(`mingw64\\bin\\git.exe`)이 완주하고 정상 해제하기 때문이다. **스크래치 클론에서 실바이너리를
`git add` 45ms 지점에 킬해 동일한 0바이트 잔재를 재현했다.**

즉 **0바이트 `index.lock`은 트리 킬의 지문**이고, 이 PC에서 그것을 하는 주체는 자동 점검
세션의 종료(teardown)다.

## 왜 워치독이 치우는가

1분마다 도는 **유일한 상시 프로세스**다. 그리고 이 결함은 사람이 다음 커밋을 시도할 때까지
조용하다 — 08-19에 실제로 4시간 18분을 그렇게 있었다. 「예약이 안 뜬 것을 예약으로 감시할 수
없다」(Fix#4)와 같은 이유로, **git이 막힌 것을 git으로 알아챌 수 없다.**

## 삭제가 안전한 이유 — 그리고 안전하지 **않은** 경우

인덱스 본체(`.git/index`)는 락 파일에 다 쓴 뒤 **원자적 rename**으로만 교체된다. 0바이트 락은
「아직 아무것도 안 썼다」는 뜻이므로 그것을 지워서 잃는 데이터가 없다.

**그래서 세 조건을 모두 요구한다.** 하나라도 어긋나면 손대지 않는다:

    크기 0바이트     내용이 있으면 쓰는 중이거나 쓰다 만 것이다 — 우리가 판단할 일이 아니다
    나이 >= 10분     실측 최장 보유가 101ms다. 6,000배 여유 — 정상 작업과 겹칠 수 없다
    git.exe 부재     **살아 있으면 그 락은 주인이 있다**

세 번째는 **저장소를 안 가린다** — 다른 저장소의 git.exe도 청소를 막는다. 프로세스에서
작업 디렉터리를 알아내는 것보다 한 분 더 기다리는 편이 싸고, 다음 분에 다시 본다.
그리고 **모르면 안 지운다**: 프로세스 조회가 실패하면 `None`이고, `None`은 「없다」가 아니다
(`market_calendar`의 비대칭과 방향이 반대인 이유는 대가가 반대이기 때문이다 — 저쪽은 못 읽은
달력을 거래일로 접어야 감시가 계속되고, 이쪽은 모르는 채로 지우면 남의 작업을 깬다).

## 왜 `index.lock`만인가

`HEAD.lock`·`config.lock`·`refs/**/*.lock`은 보유 시간이 마이크로초 단위라 트리 킬과 겹칠
확률이 사실상 0이고, 실제로 관측된 적도 없다. **삭제하는 청소부의 사정거리는 관측된 것까지만**
넓힌다 — 넓히는 순간 그 파일들의 안전 조건을 따로 논증해야 하는데 근거가 없다.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("mahdi.git_lock")

# 실측 최장 보유(101ms)의 약 6,000배. 정상 작업이 이 나이에 도달할 수 없고, 워치독 주기가
# 1분이라 실제 청소는 락 생성 후 10~11분에 일어난다 — 사람이 커밋을 시도하기 전에 끝난다.
STALE_LOCK_MIN_AGE_SECONDS = 600

# 청소 대상. 위 docstring 「왜 `index.lock`만인가」 참고 — 관측된 것만 건드린다.
INDEX_LOCK_RELPATH = ("index.lock",)

# 프로세스 조회 상한. 워치독은 1분 주기라 여기서 오래 매달리면 08-12의 무력화가 재현된다
# (그때는 상속된 파이프였다 — `_restart`의 `capture_output` 주석). 조회 자체가 드물게만
# 일어나므로(아래 `sweep` 순서) 짧게 잡아도 잃는 것이 없다.
_TASKLIST_TIMEOUT_SECONDS = 5


def is_stale(*, size: int, age_seconds: float, git_running: bool | None) -> bool:
    """반환: 이 락을 지워도 되는가. **순수 함수** — 파일도 프로세스도 안 본다.

    입력: 락 파일 크기(바이트), 나이(초), 지금 git 프로세스가 도는가(`None` = 모른다).
    계산: 위 docstring의 세 조건을 **전부** 만족할 때만 True.
    해석: `git_running`이 `None`이면 False다 — **모르는 채로 지우면 남의 작업을 깬다.**
         「없다」와 「모른다」를 가르는 것이 이 모듈에서 가장 중요한 한 줄이고,
         그 구분이 없으면 조회가 실패한 순간 이 청소부가 가장 위험해진다.
    실패 조건: 없다.
    """
    if size != 0:
        return False
    if age_seconds < STALE_LOCK_MIN_AGE_SECONDS:
        return False
    return git_running is False


def git_processes_running() -> bool | None:
    """반환: 지금 이 PC에 `git.exe`가 도는가. 판정 불가면 `None`.

    계산: `tasklist /FI "IMAGENAME eq git.exe" /NH` — 매칭이 없으면 안내 문구 한 줄만 나오고,
         있으면 줄이 `git.exe`로 시작한다. **줄 머리로 판정**하므로 안내 문구의 언어와 무관하다
         (이 PC는 한국어 Windows다).
    해석: 저장소를 안 가린다 — 위 docstring 참고. psutil은 이 저장소 의존성이 아니라
         표준 도구를 쓴다.
    실패 조건: Windows가 아니거나 조회가 실패/시간초과면 `None`(= 모른다). 예외를 밖으로
              내지 않는다 — 청소부가 워치독을 죽이면 본말이 뒤집힌다.
    """
    if sys.platform != "win32":
        return None
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq git.exe", "/NH"],
            capture_output=True, timeout=_TASKLIST_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 — 조회 실패는 「모른다」이지 「없다」가 아니다
        logger.warning("git 프로세스 조회 실패 — 락을 건드리지 않는다", exc_info=True)
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.decode("utf-8", errors="replace")
    return any(line.strip().lower().startswith("git.exe") for line in text.splitlines())


def sweep(repo_root: Path, now: datetime, *, probe=git_processes_running) -> list[dict]:
    """버려진 `index.lock`을 지운다. 반환: 지운 것들의 기록(없으면 빈 목록).

    입력: 저장소 루트, 현재 시각, (테스트 주입용) 프로세스 조회 함수.
    계산: **싼 조건부터 본다** — 존재·크기·나이를 stat으로 거른 뒤에야 프로세스를 조회한다.
         잔재는 드물므로 `tasklist`는 사실상 안 돌고, 매분 프로세스를 세는 비용이 생기지 않는다.
    해석: 지운 사실을 **반드시 반환한다**(조용히 치우지 않는다 — 계명 12). 호출측이 그것을
         로그에 남기고, `ops.watchdog_metrics`가 그 줄을 세어 리포트에 올린다.
         **이 청소가 잦아지면 그것 자체가 신호다** — 세션 teardown이 git을 자주 죽인다는 뜻이다.
    실패 조건: 어떤 예외도 밖으로 내지 않는다. 지우지 못하면 그 항목을 건너뛴다 —
              다음 분에 다시 본다.
    """
    swept: list[dict] = []
    git_dir = Path(repo_root) / ".git"
    for relpath in INDEX_LOCK_RELPATH:
        lock = git_dir / relpath
        try:
            stat = lock.stat()
        except FileNotFoundError:
            continue
        except Exception:  # noqa: BLE001
            logger.warning("락 파일 stat 실패: %s", lock, exc_info=True)
            continue
        age = (now - datetime.fromtimestamp(stat.st_mtime)).total_seconds()
        if stat.st_size != 0 or age < STALE_LOCK_MIN_AGE_SECONDS:
            continue
        # 여기까지 온 것만 프로세스를 조회한다 — 싼 조건이 이미 걸렀다.
        running = probe()
        if not is_stale(size=stat.st_size, age_seconds=age, git_running=running):
            continue
        try:
            lock.unlink()
        except Exception:  # noqa: BLE001 — 다음 분에 다시 본다
            logger.warning("락 파일 삭제 실패: %s", lock, exc_info=True)
            continue
        swept.append({"path": str(lock), "age_minutes": round(age / 60.0, 1), "size": stat.st_size})
    return swept
