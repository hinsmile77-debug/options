"""워치독 자신의 로그를 읽는다 — **감시자가 돌기는 했는가** (2026-08-12 §2-3 / Fix#8).

## 왜 별도 파서인가

`log_metrics`는 `logs/observation_loop.log`를 읽는다. 워치독은 **의도적으로 다른 파일**에
쓴다(`logs/watchdog.log`) — 관측 루프가 죽은 구간의 기록이라 같은 파일에 쓰면 *"그 시간대엔
아무 로그도 없다"* 는 사실 자체가 흐려지기 때문이다(`watchdog_observation_loop._log` 주석).
그 분리가 옳으므로 파서도 분리한다.

## 무엇을 재는가 — 「개입했는가」가 아니라 「침묵했는가」

08-12에 워치독은 10:14:01에 판정하고 재기동했다. 그 뒤 `watchdog.log`의 마지막 줄이 「RESTART」라
**사고 대응 중인 것처럼** 보였는데, 실제로는 재기동 호출이 상속된 파이프에 물려 15:45:02까지
막혀 있었고 그동안 매분 실행이 전부 무시됐다(`MultipleInstances=IgnoreNew`).

**그 사실은 로그의 침묵으로만 드러난다** — 10:20~15:40 사이 `OK` 줄이 한 개도 없다.
정상일에는 10분에 한 줄씩 찍히므로, **연속한 두 줄 사이의 간격**이 그 침묵의 길이다.

그래서 이 모듈의 주 지표는 개입 횟수가 아니라 `max_silence_minutes`다.

## 감시 창 경계를 함께 센다

마지막 줄과 창 끝(`liveness.WATCH_WINDOW_END`) 사이도 침묵이다 — 08-12의 331분이 바로 그
형태였다(10:14 이후 줄이 없다). 첫 줄과 창 시작 사이도 같은 이유로 센다: 그 구간이 길면
**워치독이 아침에 아예 안 떴다**는 뜻이고, 그것은 08-06~08-11에 실제로 6영업일 연속 있었던 일이다.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time as dtime
from pathlib import Path

from mahdi import liveness

logger = logging.getLogger("mahdi.ops.watchdog_metrics")

WATCHDOG_LOG_FILENAME = "watchdog.log"

# `watchdog_observation_loop._MISSING_CHECK_MARKER`의 **복제본**이다 — 이 모듈은 `scripts/`를
# import하지 않는다(로직은 모듈에, 스크립트는 얇게). 갈라지면 경보가 0건으로 세어지고 그 0은
# 「점검이 제때 있었다」로 읽히므로, `tests/test_ops_watchdog_metrics.py`가 일치를 강제한다.
MISSING_CHECK_MARKER = "MISSING_CHECK"

# 2026-08-23 (08-21 §4 Fix#4) — DEGRADED 사건의 **종료 줄**.
# 원본은 `scripts/watchdog_observation_loop.py`가 `RECOVERED — ...`로 남긴다.
RECOVERED_MARKER = "RECOVERED"

# `watchdog_observation_loop._LOCK_SWEEP_MARKER`의 복제본(2026-08-19). 같은 이유로 계약
# 테스트가 일치를 강제한다 — 갈라지면 청소 건수가 0으로 세어지고, 그 0은 「락이 안 남았다」로
# 읽힌다. 실제로는 **세션 teardown이 git을 죽이고 있다**는 신호를 통째로 잃는 것이다.
LOCK_SWEEP_MARKER = "LOCK_SWEPT"

# `[2026-08-12 10:14:01] RESTART — ...` / `[...] OK — ...` / `[...] 재기동 시도: ...`
_LINE_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):(\d{2})\]\s*(.*)$")

# 정상일의 `OK` 기록 주기(분) — `watchdog_observation_loop.main`의 `now.minute % 10`.
# **`silence_over_cadence_ratio`의 분모**이고, 이 값이 바뀌면 그 비율이 자동으로 따라간다.
_OK_CADENCE_MINUTES = 10.0

# 그 두 배를 넘는 침묵은 **판정이 실제로 멈춘 것**으로 본다 — 한 번 놓친 것과 계속 못 도는 것을
# 가른다. 임계를 10분에 두면 스케줄러 지터 한 번에 매일 ⚠가 뜬다.
SILENCE_WARN_MINUTES = _OK_CADENCE_MINUTES * 2


def _watch_window_bounds(target: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(target, liveness.WATCH_WINDOW_START),
        datetime.combine(target, liveness.WATCH_WINDOW_END),
    )


def parse(lines: list[str], target: date) -> dict:
    """
    입력: `watchdog.log`의 줄들(여러 날치가 섞여 있어도 된다), 대상 날짜.
    계산: 그날 줄만 골라 판정 횟수·개입 횟수·**최장 침묵 구간**을 낸다.
    해석: `checks == 0`은 「워치독이 그날 한 줄도 안 남겼다」이고, 그것은 **미등록이거나
         통째로 안 돈 날**이다(08-06~08-11이 그랬다). 그때 `max_silence_minutes`는 감시 창
         전체가 되고, 그 값이 정확히 그 사실을 말한다.
    실패 조건: 없다 — 형식이 안 맞는 줄은 건너뛴다.
    """
    start, end = _watch_window_bounds(target)
    stamps: list[datetime] = []
    restarts = 0
    restart_failures = 0
    alert_only = 0
    degraded = 0
    missing_check = 0
    lock_swept = 0
    # 2026-08-23 (08-21 §1-11 / §4 Fix#4) — **사건이 몇 번 끝났는가.**
    # `degraded_checks`는 「아팠던 분 수」이고 이 값은 「사건 수」다. 08-21에 16분이 세 구간
    # (13:39~14:02 · 14:44 · 15:14)이었는데, 그 구분을 사람이 비-OK 16행을 손으로 묶어 냈다.
    recovered = 0
    for line in lines:
        m = _LINE_RE.match(line.rstrip("\n"))
        if not m:
            continue
        if m.group(1) != target.isoformat():
            continue
        at = datetime.combine(
            target, dtime(int(m.group(2)), int(m.group(3)), int(m.group(4)))
        )
        body = m.group(5)
        stamps.append(at)
        if body.startswith(LOCK_SWEEP_MARKER):
            # 2026-08-19 — **관측 루프와 무관한 축이다.** 저장소가 막혀 있던 것이고, 워치독은
            # 그것을 연 것뿐이다. `restarts`/`degraded`와 섞으면 「루프가 아팠다」로 읽힌다.
            lock_swept += 1
        elif body.startswith(MISSING_CHECK_MARKER):
            # 2026-08-19 (08-18 §1-2 / Fix#4) — **관측 루프 이상이 아니다.** 루프는 멀쩡한데
            # 사람의 장전 점검이 09:00까지 안 뜬 것이고, 08-18에는 인프라가 하루 종일 초록인
            # 채로 그 회차가 298분 늦었다. `restarts`/`degraded`와 섞으면 조치가 뒤섞인다.
            missing_check += 1
        elif body.startswith("RESTART"):
            restarts += 1
        elif body.startswith("ALERT_ONLY"):
            alert_only += 1
        elif body.startswith(RECOVERED_MARKER):
            # **회복은 DEGRADED의 반대가 아니라 그 사건의 닫힘이다.** 종료 줄이 없던 08-21까지는
            # 이 값이 0이고, 그 0은 「사건이 없었다」가 아니라 「셀 수 없었다」이다(규약 C).
            recovered += 1
        elif body.startswith("DEGRADED"):
            # 2026-08-14 Fix#2 — **살아 있는데 적재가 0인 분.** 재기동을 유발하지 않으므로
            # `restarts`와 섞으면 안 된다: 저쪽은 「조치했다」이고 이쪽은 「조치하지 않기로
            # 했다」이다. 08-14에 이 판정이 있었다면 14:10에 첫 줄이 남았을 것이다.
            degraded += 1
        elif body.startswith("재기동 시도:") and "실행 완료" not in body:
            # 08-12의 「300초 안에 끝나지 않음」이 이것이다 — 재기동은 **성공했는데** 실패로
            # 보고됐다. 건수만 세고 판정은 사람이 한다(로그 문구로 성패를 단정하지 않는다).
            restart_failures += 1

    stamps.sort()
    # 침묵 = 연속한 판정 사이의 간격. **창 경계를 양끝에 붙인다**(위 docstring).
    edges = [start] + [s for s in stamps if start <= s <= end] + [end]
    gaps = [(b - a).total_seconds() / 60.0 for a, b in zip(edges, edges[1:])]
    max_gap = max(gaps) if gaps else 0.0
    worst_at = None
    if gaps:
        worst = max(range(len(gaps)), key=lambda i: gaps[i])
        worst_at = f"{edges[worst]:%H:%M}~{edges[worst + 1]:%H:%M}"

    return {
        "checks": len(stamps),
        "restarts": restarts,
        "restart_failures": restart_failures,
        "alert_only": alert_only,
        # 2026-08-14 Fix#2. **0은 두 가지다**(규약 C): 이 키가 있고 0이면 「적재가 끊긴 분이
        # 없었다」이고, 키가 아예 없으면 「그날은 이 판정 자체가 없던 버전이다」이다.
        "degraded_checks": degraded,
        # 2026-08-23 Fix#4. **`degraded_checks`와 나란히 읽는다** — 「16분 / 3사건」과
        # 「16분 / 1사건」은 완전히 다른 하루다. 키가 있고 0이면 「그날 닫힌 사건이 없었다」
        # (아직 아프거나, 아예 안 아팠거나), 키가 없으면 「그날은 종료 줄 자체가 없던 버전이다」.
        "recovered_episodes": recovered,
        # 2026-08-19 Fix#4. **0은 두 가지다**(규약 C): 키가 있고 0이면 「장전 점검이 제때 있었다」,
        # 키가 아예 없으면 「그날은 이 판정 자체가 없던 버전이다」. 그 구분이 이 키의 존재 이유다.
        "missing_check_alerts": missing_check,
        # 2026-08-19. **0은 두 가지다**(규약 C): 키가 있고 0이면 「버려진 락이 없었다」,
        # 키가 없으면 「그날은 이 청소 자체가 없던 버전이다」. 그리고 이 값이 **자라는 것 자체가
        # 신호**다 — 세션 teardown이 git 프로세스를 자주 죽이고 있다는 뜻이다.
        "stale_lock_sweeps": lock_swept,
        "max_silence_minutes": round(max_gap, 1),
        # 2026-08-12 규약 F — **주장 지표는 절대 건수로 세우지 않는다.**
        #
        # `max_silence_minutes`에 `<= 20`을 걸려다 `test_repo_pending_hypotheses_obey_the_
        # normalized_claim_rule`에 걸렸고, 그 지적이 옳았다: 그 값은 감시 창 길이와 `OK` 기록
        # 주기에 비례하는데, 예측을 쓰는 순간에는 둘 다 상수처럼 느껴진다. 창을 07:40~15:45가
        # 아닌 다른 값으로 바꾸거나 기록 주기를 10분에서 바꾸면 임계가 조용히 틀려진다.
        #
        # 정상 기록 주기로 나눈 **배수**는 그 둘이 분모에서 약분된다 — 정상일이면 언제나 1.0
        # 근처이고, 08-12는 33.1이다. 그래서 이 값에는 부등식을 걸어도 된다.
        "silence_over_cadence_ratio": (
            round(max_gap / _OK_CADENCE_MINUTES, 2) if _OK_CADENCE_MINUTES else None
        ),
        "max_silence_window": worst_at,
        "first_at": f"{stamps[0]:%H:%M:%S}" if stamps else None,
        "last_at": f"{stamps[-1]:%H:%M:%S}" if stamps else None,
        "silence_warn_minutes": SILENCE_WARN_MINUTES,
    }


def collect(log_dir: Path, target: date) -> dict | None:
    """반환: `parse()` 결과, 로그 파일이 없으면 None(= 「모른다」, 「정상」이 아니다)."""
    path = Path(log_dir) / WATCHDOG_LOG_FILENAME
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return None
    except Exception:
        logger.warning("워치독 로그 읽기 실패: %s", path, exc_info=True)
        return None
    return parse(lines, target)
