"""관측 루프 워치독 — 1분마다 생존 신호를 보고, 죽어 있으면 알리고 되살린다.

2026-08-06 §2-1 / Fix#2. 그날 10:04~10:23 관측 루프와 COCKPIT이 흔적 없이 죽어 있었고
**19분 동안 아무도 몰랐다**(사람이 화면을 보고 10:20에 알아챘다). 상세 근거는 `mahdi/liveness.py`.

실행: `python scripts/watchdog_observation_loop.py` (인자 없음)
      Windows 작업 스케줄러가 **1분 주기**로 `scripts/watchdog_mahdi.bat`을 통해 호출한다.

## 이 스크립트가 얇은 이유

판정(`liveness.decide()`)은 전부 `mahdi/liveness.py`에 있고 여기서는 파일 I/O와 프로세스
기동만 한다 — `docs/동작점검/README.md`의 규약이다("로직은 파이썬 모듈에, `scripts/`는 얇게").
그래야 "언제 재기동해야 하는가"를 pytest가 검사할 수 있다.

## 재기동 수단으로 장전 기동 스크립트를 그대로 쓰는 이유

`start_mahdi_premarket.bat`은 이미 (1) 잔존 프로세스 정리, (2) Docker 확인, (3) 마이그레이션
재적용, (4) COCKPIT + 관측 루프 기동을 **멱등하게** 한다. 워치독이 자기만의 기동 경로를 따로
만들면 그 경로는 하루 한 번도 안 쓰이다가 정작 필요한 날 처음 실행된다 — 그때 처음 깨진다.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mahdi import git_lock, liveness, market_calendar, notify
from mahdi.config.settings import PROJECT_ROOT
from mahdi.data import db

LOG_DIR = PROJECT_ROOT / "logs"
WATCHDOG_LOG = LOG_DIR / "watchdog.log"
STATE_FILE = LOG_DIR / ".watchdog_state.json"
START_SCRIPT = PROJECT_ROOT / "scripts" / "start_mahdi_premarket.bat"

# 기동 스크립트는 Docker 데몬을 최대 180초까지 기다린다 — 그보다 넉넉히 잡되 무한은 아니다.
#
# **이 값을 줄이지 말 것.** 2026-08-12 보고서 초안이 "실측 10초니까 120초로 줄이자"고 적었는데
# 그것은 틀렸다 — Docker 대기 180초는 `cmd /c bat`의 **안**에서 흐르므로 이 타임아웃에 그대로
# 포함된다. 120초로 줄이면 Docker가 느린 아침마다 기동을 마이그레이션 중간에 죽인다.
_RESTART_TIMEOUT_SECONDS = 300

# 적재 조회의 상한 (2026-08-14 Fix#2).
#
# **2초를 넘기지 않는다.** 워치독은 1분 주기이고, DB가 굳은 날 여기서 오래 매달리면 08-12에
# 겪은 무력화가 그대로 재현된다(그때는 상속된 파이프였고 이번엔 DB다 — 원인은 달라도 결과는
# 같다: 작업 스케줄러의 `MultipleInstances=IgnoreNew`가 그 사이 매분 실행을 전부 버린다).
#
# connect와 statement 양쪽에 건다 — 붙는 데서 막히는 것과 쿼리에서 막히는 것은 다른 경로다.
_INGEST_QUERY_TIMEOUT_SECONDS = 2

# ===== 2026-08-19 (08-18 보고서 §1-2 / Fix#4) — **예약이 안 뜬 것을 예약으로 감시할 수 없다** =====
#
# ## 무엇이 사흘째 안 고쳐졌는가
#
# 08-18 장전 회차는 08:30 예정에 **13:28:36**(298분 지연)에 떴다. 같은 날 14:30 회차는 10분
# 만에 떴다 — **같은 날 같은 스케줄러에서 298분 지연과 정시가 함께 나왔다.** 그 대조 실험이
# 원인을 확정했다: 결함은 예약 자체가 아니라 **Claude 앱 기동 시각에 예약이 종속된 것**이다.
#
# 08-17 보고서가 남긴 `[P1] 그날 안에 알아챌 경로가 없다`가 그날로 사흘째였다. 더 나쁜 것은
# **산출물만 보면 「예약이 돈 것」과 「사람이 손으로 돌린 것」이 구별되지 않는다**는 점이다
# (08-18 12:54본이 그랬다 — `lastRunAt`을 봐야 알 수 있었다).
#
# ## 왜 여기인가
#
# 10분마다(실제로는 1분마다) 도는 **유일한 상시 프로세스**가 이것이다. 예약을 예약으로
# 감시하면 예약이 안 뜬 날 감시도 안 뜬다.
#
# ## 왜 09:00인가
#
# 장전 회차의 존재 이유가 「개장 전에 본다」이므로, 09:00을 넘긴 장전 점검은 **떴어도 늦은
# 것**이다. 그래서 임계를 예정 시각(08:30)이 아니라 개장에 건다 — 30분 지터로 매일 울리는
# 경보는 곧 안 읽힌다.
_PREMARKET_CHECK_DEADLINE = dtime(9, 0)
# 그날 장전 점검이 남겼어야 할 산출물. 이름 규칙은 `docs/동작점검/README.md`의 규약이고,
# `collect_evidence.py`가 같은 규칙으로 쓴다. **`auto/`가 아니라 루트**다(사람이 쓰는 문서).
_CHECK_DOC_DIR = PROJECT_ROOT / "docs" / "동작점검"
# 2026-08-20 — 점검 산출물이 **하루 한 파일**로 바뀌었다(`mahdi-daily-check` 대원칙 B).
# 장전이 만들고 장중·장후가 append하므로, 장전 시점에 존재하는 이름은 이제 이것 하나다.
_CHECK_DOC_PATTERN = "{date}_마흐디_일일점검.md"
# ## 왜 옛 이름을 함께 받는가 — **전환기의 오보는 방향만 반대일 뿐 같은 결함이다**
#
# 이 상수를 새 이름 **하나로만** 두면, 리포 밖(Claude 앱 예약 3종)의 프롬프트가 아직 옛
# 이름을 지시하는 동안 **매일 09:00에 오보가 울린다.** 그것은 이 경보가 원래 막으려던 것과
# 정확히 같은 실패다 — 08-15~16에 `ALERT_ONLY` 94·113줄이 아무도 안 읽히고 끝난 그 형태다.
#
# **비대칭이 판단 근거다**: 옛 이름을 함께 받아 잃는 것은 「이름이 틀렸다」를 여기서 강제하지
# 못하는 것뿐이고, 그것은 애초에 이 경보의 일이 아니다(이 경보가 묻는 것은 *"오늘 장전 점검이
# 떴는가"*이지 *"이름을 규약대로 지었는가"*가 아니다). 반대로 안 받으면 **경보 자체가 죽는다.**
# 이름 규약의 강제는 스킬·프롬프트·이 파일의 계약 테스트가 맡는다.
#
# **언제 지우는가**: Claude 앱 예약 3종이 새 이름으로 바뀌고, `docs/동작점검/`에 새로 생기는
# `_점검_pre.md`가 **한 거래일도 없는 것**을 확인한 뒤. 그때 이 튜플을 한 원소로 줄인다.
_CHECK_DOC_LEGACY_PATTERNS = ("{date}_점검_pre.md",)
_CHECK_DOC_PATTERNS = (_CHECK_DOC_PATTERN, *_CHECK_DOC_LEGACY_PATTERNS)
# 하루에 한 번만 울린다. 09:00~15:45 사이 매분 울리면 397건이고, 그 소음은 08-15~16에
# `ALERT_ONLY` 94·113줄로 이미 한 번 겪었다 — 그때 아무도 안 읽었다.
_MISSING_CHECK_MARKER = "MISSING_CHECK"
# 하루 1회 제한의 기록처. `.watchdog_state.json`에 얹지 않는 이유는 `liveness.next_state()`가
# **매번 새 dict를 만들어** 세 키만 남기기 때문이다 — 거기 얹은 값은 조용히 사라지고, 그러면
# 경보가 매분 울린다. 08-12 Fix#8이 `.watchdog_last_check.json`을 따로 만든 것과 같은 형태다.
_MISSING_CHECK_STATE = LOG_DIR / ".watchdog_missing_check.json"

# ===== 2026-08-19 — 버려진 `.git/index.lock`을 연다 =====
#
# 0바이트 락이 이틀 연속 남아 다음 git 작업을 전부 막았다(08-18 16:20 · 08-19 12:41, 각각
# 자동 점검 세션이 마지막 산출물을 쓴 그 분). 원인·안전 조건·재현은 `mahdi/git_lock.py`
# 모듈 docstring에 있다 — 요지는 **0바이트 index.lock이 트리 킬의 지문**이라는 것이다.
#
# ## 왜 창 밖에서도 도는가
#
# 락 #1은 **16:20**에 생겼다 — `liveness.WATCH_WINDOW_END`(15:45) 밖이다. 감시 창에 가두면
# 그 락은 영영 안 치워진다. 사람은 장이 끝난 뒤에 커밋하고, 이 결함은 **그때 처음 보인다**
# (08-19에 실제로 4시간 18분을 그렇게 있었다).
#
# ## 왜 `stopped_at`/`holiday` 게이트를 안 쓰는가
#
# 그 둘은 「시장이 없으니 관측을 판정하지 않는다」는 뜻이다. 버려진 락은 시장과 무관하고,
# **사람이 시스템을 꺼 둔 날에도 저장소는 쓴다.** 게이트를 물리면 가장 필요한 날에 안 돈다.
_LOCK_SWEEP_REPO = PROJECT_ROOT
# `ops.watchdog_metrics`가 이 마커로 그 줄을 센다 — 복제본이고, 계약 테스트가 일치를 지킨다
# (`_MISSING_CHECK_MARKER`와 같은 규약: 로직은 모듈에, 스크립트는 얇게).
_LOCK_SWEEP_MARKER = "LOCK_SWEPT"


def _premarket_check_missing(now: datetime) -> bool:
    """반환: 지금이 09:00을 넘겼는데 **오늘 날짜의 장전 점검 산출물이 없는가**.

    입력: 현재 시각. 계산: 파일 존재 확인 **최대 두 번**(디렉터리 순회도 DB 접속도 하지 않는다).
    해석: 상세 근거는 위 `_PREMARKET_CHECK_DEADLINE` 주석. 이 함수는 **판정만** 하고,
         휴장일·의도적 정지 게이트는 호출측이 이미 통과시킨 것을 전제한다(그 둘을 여기서 다시
         보면 같은 사실이 두 곳에 적힌다 — 규약 B).
         **이름 후보가 여럿인 이유**는 `_CHECK_DOC_LEGACY_PATTERNS` 주석에 있다 — 하나라도
         있으면 「장전 점검은 떴다」이고, 그것이 이 경보가 묻는 전부다.
    실패 조건: 없다. 경로를 못 읽으면 「없다」로 본다 — 이 경보의 대가는 오경보 한 줄이고,
         침묵의 대가는 08-18처럼 하루를 통째로 놓치는 것이다. 비대칭이 반대 방향이다.
    """
    if now.time() < _PREMARKET_CHECK_DEADLINE:
        return False
    today = now.date().isoformat()
    try:
        return not any(
            (_CHECK_DOC_DIR / pattern.format(date=today)).exists()
            for pattern in _CHECK_DOC_PATTERNS
        )
    except OSError:
        return True


def _missing_check_already_alerted(today: str) -> bool:
    try:
        return json.loads(_MISSING_CHECK_STATE.read_text(encoding="utf-8")).get("date") == today
    except Exception:  # noqa: BLE001 — 없거나 깨졌으면 「아직 안 울렸다」
        return False


def _alert_missing_premarket_check(now: datetime) -> bool:
    """장전 점검 산출물이 없으면 `watchdog.log` 한 줄 + Slack 1회. 반환: 울렸는가.

    **`_restart()`도 `decide()`도 건드리지 않는다.** 이 경보는 관측 루프의 생사와 무관한
    별개의 축이고(관측은 08-18에 완벽히 돌았다 — 안 뜬 것은 사람의 점검이다), 두 축을 한
    판정에 섞으면 「루프가 죽었다」와 「점검이 안 떴다」의 조치가 뒤섞인다.

    실패 조건: 없다 — 파일 쓰기가 실패하면 다음 분에 한 번 더 울릴 뿐이다. 그 소음이
         「영영 안 울림」보다 낫다(08-18이 사흘째 후자였다).
    """
    today = now.date().isoformat()
    if _missing_check_already_alerted(today):
        return False
    detail = (
        f"오늘({today}) 장전 점검 산출물이 {_PREMARKET_CHECK_DEADLINE:%H:%M}까지 없다 — "
        f"`docs/동작점검/{_CHECK_DOC_PATTERN.format(date=today)}`. "
        "**예약이 안 뜬 것을 예약으로 감시할 수 없으므로** 이 줄이 유일한 신호다 "
        "(08-18: 08:30 예정 → 13:28 발화, 298분)."
    )
    _log(f"[{now:%Y-%m-%d %H:%M:%S}] {_MISSING_CHECK_MARKER} — {detail}")
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        _MISSING_CHECK_STATE.write_text(json.dumps({"date": today}, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    notify.notify_sync(f"장전 점검 미발화 — {detail}", level="WARNING")
    return True


def _log(line: str) -> None:
    """워치독 자신의 로그는 관측 루프 로그와 **분리한다** — 관측 루프가 죽은 구간의 기록이므로
    같은 파일에 쓰면 "그 시간대엔 아무 로그도 없다"는 사실 자체가 흐려진다."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with WATCHDOG_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _read_state() -> dict | None:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        return None


# ===== 2026-08-26 (08-26 §1-5 / P2-2) — **파일이 자기가 무엇을 담는지 말하게 한다** =====
#
# 08-26에 두 회차가 이 파일의 `date`를 「오늘 워치독이 돌았는가」로 읽어 오독했다.
# **문제는 파일이 아니라 읽는 쪽이 그 뜻을 몰랐다는 것이다** — 그날 이 파일은 필요할 때
# 정확히 갱신됐다(14:10 첫 DEGRADED). 「오늘 돌았는가」는 `.watchdog_last_check.json`이 답한다.
#
# **권고 (나)를 유지한다 — 정상일 때도 쓰지 않는다.** 매 회차 쓰면 이 파일의 `date`가 「오늘」이
# 되어 「이상이 있었던 마지막 날」이라는 정보가 통째로 사라진다. 대신 **키 하나로 뜻을 적는다.**
#
# 고도화 3이 정리한 형태의 3번 사례이고, 그 규약(「경고를 내는 코드는 그 경고가 참이기 위한
# 조건을 전부 검사하거나, 검사하지 않는 조건을 문구 안에 적는다」)의 후자 쪽 적용이다.
_STATE_NOTE = (
    "이상(DEGRADED/RESTART)이 있었던 마지막 회차의 상태다. "
    "오늘 워치독이 돌았는지는 .watchdog_last_check.json을 본다"
)


def _write_state(state: dict) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        # `note`를 **먼저** 넣어 호출측이 넘긴 키를 덮지 않게 한다(같은 이름이 오면 그쪽이 이긴다).
        payload = {"note": _STATE_NOTE, **state}
        STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# ===== 2026-08-23 (08-21 §1-11 / §4 Fix#4) — DEGRADED 사건의 시작과 끝 =====
#
# 판정(「연속 몇 분째인가」·「언제 끝났는가」)은 `liveness.track_degraded_episode()`에 있고
# 여기서는 **파일 I/O만** 한다 — 이 스크립트가 얇은 이유와 같다(모듈 docstring 참고).
#
# `.watchdog_state.json`에 얹지 않는 이유는 `_MISSING_CHECK_STATE` 주석에 이미 적혀 있다:
# `liveness.next_state()`가 매번 새 dict를 만들어 세 키만 남기므로 **거기 얹은 값은 조용히
# 사라진다.** 그러면 이 카운터가 영원히 1에서 멈춘다.
_DEGRADED_EPISODE_STATE = liveness.degraded_episode_path(LOG_DIR)


# ===== 2026-09-03 (09-03 §1-9 / 제4부 P2-3) — 회복 이력을 경보 안에 넣는다 =====
#
# 절벽 표본이 넷이 됐고 **회복 시각이 관측된 셋(08-26 · 09-01 · 09-03)이 전부 15:25~15:26**,
# 즉 정규장 마감(15:20) +5~6분에 사람 손 없이 풀렸다. 시작 시각도 지속도 규모도 매번 달랐는데
# 회복 시각만 세 번 같았다. 그 사실이 지금 `docs/동작점검/cliff_episodes.md`와 사람 머릿속에만
# 있고 **경보 문구에는 없다** — 09-03에 이 함수는 DEGRADED를 45회 내면서 한 번도 그 말을 안 했다.
#
# ⛔ **자동 조치가 아니다.** 재기동 임계도 경보 조건도 안 건드린다. 08-26이 증명한 것은
# **자동 재기동을 켜지 않은 것이 옳았다**는 것이다(재기동했다면 저절로 풀렸을 회복을 자기가
# 고친 것으로 오해했을 것이다). 여기서 더하는 것은 사람이 읽을 문장 하나뿐이다.
#
# ⚠ **표본 수를 문구에 박는다.** 「3건 중 3건」이라고 적어야 다섯 번째 표본이 나왔을 때 이
# 문구가 낡았다는 것이 보인다 — 「대체로」라고 쓰면 영원히 안 늙는다. 다섯 번째 표본이 나오면
# `cliff_episodes.md`의 표와 **이 상수를 함께** 고칠 것.
_NO_INGEST_RECOVERY_HINT = (
    "참고: 같은 유형(no_ingest)의 과거 표본 3건(08-26 · 09-01 · 09-03)은 45~85분 만에 "
    "**15:25~15:26**(정규장 마감 +5~6분)에 자연 회복했고 3건 다 우리 쪽 조치가 없었다 — "
    "자동 조치가 아니라 사람이 「지금 재기동할 것인가」를 판단할 근거다"
)

# 참고 문구를 **몇 분째에 붙이는가**. 09-03의 DEGRADED는 45줄이었고, 매 줄에 붙이면 45줄이
# 통째로 길어진다(§5 억제 규약 — 하루 30줄을 넘기면 억제가 안 듣는 것이다).
# **에피소드 첫 분 + 이후 30분마다 한 번**이면 45분 사건에 2줄, 하루 최대 3~4줄이다.
_RECOVERY_HINT_REPEAT_MINUTES = 30


def _recovery_hint_due(episode: dict | None) -> bool:
    """반환: 이번 DEGRADED 줄에 회복 이력 참고 문구를 붙일 차례인가.

    해석: 사건의 **첫 분**과 그 뒤 30분마다 한 번. 상태를 못 읽었으면 붙이지 않는다 —
         모르는 채로 매분 붙이면 억제가 통째로 풀린다.
    """
    minutes = (episode or {}).get("minutes")
    if not isinstance(minutes, int) or minutes < 1:
        return False
    return minutes == 1 or minutes % _RECOVERY_HINT_REPEAT_MINUTES == 0


def _read_degraded_episode() -> dict | None:
    try:
        return json.loads(_DEGRADED_EPISODE_STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — 없거나 깨졌으면 「모른다」이고, 새 에피소드로 시작한다
        return None


def _write_degraded_episode(state: dict | None) -> None:
    try:
        if state is None:
            _DEGRADED_EPISODE_STATE.unlink(missing_ok=True)
            return
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        _DEGRADED_EPISODE_STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001 — 못 써도 판정은 계속돼야 한다(카운터만 1로 되돌아간다)
        pass


def _restart() -> tuple[bool, str]:
    """기동 스크립트를 실행한다. 반환: (성공 여부, 요약).

    ## 표준 출력을 **캡처하지 않는다** (2026-08-12 §2-3 / Fix#1)

    종전에는 `capture_output=True`였다. 그 한 인자가 08-12에 워치독을 **5시간 31분** 세웠다.

    기동 스크립트는 `start "..." cmd /k ...`로 COCKPIT과 관측 루프를 새 창에 띄운다. 캡처를
    켜면 파이썬이 파이프를 만들어 자식(`cmd /c bat`)에 물리는데, 그 핸들이 **손자(새 창의
    프로세스)에게까지 상속된다.** bat이 끝나도 손자가 파이프 쓰기 끝을 쥐고 있으므로 부모는
    EOF를 못 받는다. 더 나쁜 것은 `timeout`조차 상한이 못 된다는 점이다 — `subprocess.run`은
    `TimeoutExpired` 처리에서 자식을 죽인 뒤 `communicate()`를 **다시** 부르고, 그것이 같은
    파이프에서 또 막힌다.

    08-12 실측(같은 PC에서 재현):

        capture_output=True   timeout=8 인데 **350.1초** 만에 반환
        capture_output=False  **0.1초**

    운영에서는 관측 루프가 15:45 종료될 때까지 파이프가 안 닫혀, 워치독이 10:14:01부터
    15:45:02까지 매달려 있었다. 작업 스케줄러가 `MultipleInstances=IgnoreNew`라 그동안 매분
    실행이 전부 무시됐다 — **재기동에 성공한 그 순간부터 장 마감까지 감시가 없었다.**

    ## 캡처를 없애도 잃는 것이 없다

    기동 스크립트는 자기 출력을 전부 `logs/premarket_startup.log`에 적는다. 08-12에 「300초
    안에 끝나지 않음」이 오보임을 증명한 것도 그 로그였다(10:14:02 시작 → 10:14:12 종료).
    워치독이 그 출력을 읽은 적은 한 번도 없다 — `result.stdout`은 어디서도 안 쓰인다.

    `DEVNULL`을 쓰는 이유(상속을 그냥 두지 않고): 상속하면 워치독을 띄운 숨김 콘솔로 bat의
    출력이 흘러들어 간다. 지금은 아무도 안 보지만, 언제 무엇이 그 핸들을 붙들지는 우리가
    통제하지 못한다 — **이 함수가 다시는 남의 수명에 묶이지 않게** 명시적으로 끊는다.
    """
    if not START_SCRIPT.exists():
        return False, f"기동 스크립트를 찾지 못함: {START_SCRIPT}"
    try:
        result = subprocess.run(
            ["cmd", "/c", str(START_SCRIPT)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=_RESTART_TIMEOUT_SECONDS,
            cwd=str(PROJECT_ROOT),
        )
    except subprocess.TimeoutExpired:
        return False, f"기동 스크립트가 {_RESTART_TIMEOUT_SECONDS}초 안에 끝나지 않음"
    except Exception as exc:  # noqa: BLE001 — 어떤 실패든 알림까지는 가야 한다
        return False, f"기동 스크립트 실행 실패: {exc!r}"
    if result.returncode != 0:
        return False, f"기동 스크립트 종료 코드 {result.returncode}"
    return True, "기동 스크립트 실행 완료"


def _recent_ingest_minutes(now: datetime) -> int | None:
    """반환: 직전 `liveness.INGEST_STALE_MINUTES`분 동안 `option_analysis_1m`에 **행이 있던 분 수**.
             못 읽으면 **None("모른다")** — 0이 아니다.

    2026-08-14 Fix#2. 워치독이 「가져오고 있는가」를 보는 유일한 창이다.

    ## 왜 `mahdi.data.db.get_connection()`을 안 쓰는가

    그 헬퍼는 타임아웃 없이 `psycopg.connect(dsn)`을 부른다. 감시자가 감시 대상의 DB에
    무제한으로 매달리는 것이 정확히 규약 D가 금지하는 결합이다 — 여기서만 상한을 걸어 연다.

    ## 왜 `underlying`으로 안 거르는가

    이 함수가 답하는 질문은 「이 시스템이 무엇이든 가져오고 있는가」다. 기초자산을 걸면 그
    상수가 워치독과 수집기 사이에서 조용히 갈라질 수 있고(08-11 `PRIORITY_SERIES_LABEL`이
    그래서 계약 테스트를 얻었다), 그 대가로 얻는 정밀도는 여기서 쓸모가 없다 —
    **어느 북이든 한 행이라도 들어왔으면 수집 경로는 살아 있다.**

    실패 조건: 어떤 예외도 밖으로 내지 않는다. DB가 죽었다고 워치독이 죽으면 안 된다.
    """
    since = now - timedelta(minutes=liveness.INGEST_STALE_MINUTES)
    try:
        import psycopg

        from mahdi.config.settings import get_db_settings

        with psycopg.connect(
            get_db_settings().dsn,
            connect_timeout=_INGEST_QUERY_TIMEOUT_SECONDS,
            options=f"-c statement_timeout={_INGEST_QUERY_TIMEOUT_SECONDS * 1000}",
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(DISTINCT date_trunc('minute', timestamp)) "
                    "FROM option_analysis_1m WHERE timestamp >= %s AND timestamp <= %s",
                    (since, now),
                )
                row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 — 못 읽은 것은 「모른다」다
        _log(f"[{now:%Y-%m-%d %H:%M:%S}] 적재 조회 실패(판정은 박동 축으로만 한다): {exc!r}")
        return None
    return int(row[0]) if row and row[0] is not None else 0


def main() -> None:
    now = db.local_now()
    # 2026-08-19 — **모든 게이트보다 먼저, 감시 창과 무관하게.** 위 `_LOCK_SWEEP_REPO` 주석 참고:
    # 락 #1이 16:20(창 밖)에 생겼고, 사람이 커밋을 시도할 때까지 4시간 18분 조용했다.
    # 조용히 치우지 않는다 — 지운 사실은 로그에 남고 `ops.watchdog_metrics`가 그것을 센다.
    for swept in git_lock.sweep(_LOCK_SWEEP_REPO, now):
        _log(
            f"[{now:%Y-%m-%d %H:%M:%S}] {_LOCK_SWEEP_MARKER} — 버려진 git 락을 열었다: "
            f"{swept['path']} ({swept['size']}바이트 · {swept['age_minutes']}분 방치 · "
            "그 사이 git 프로세스 0개). **세션 teardown이 git을 죽인 흔적이다** — "
            "잦아지면 원인을 다시 볼 것(mahdi/git_lock.py)."
        )
    beat = liveness.read_heartbeat(liveness.heartbeat_path(LOG_DIR))
    state = _read_state()
    # 2026-08-17 — 사람이 일부러 껐는가. **적재 조회보다 먼저 본다**: 정지 중이라면 DB를 열
    # 이유가 없다(규약 D — 감시자가 매분 감시 대상의 DB에 붙는 결합을 하나라도 덜 만든다).
    stopped_at = liveness.intentional_stop_at(
        liveness.intentional_stop_path(LOG_DIR), now, beat
    )
    # 2026-08-17 2차 — 오늘이 등재된 휴장일인가. 워치독은 1분마다 **새 프로세스**로 뜨므로
    # `@lru_cache`가 매번 비어 달력 수정이 즉시 반영된다(관측 루프는 다음 기동까지 옛 값을
    # 쓴다 — 오늘이 휴장일임을 뒤늦게 알았을 때 먼저 멈춰야 하는 쪽이 워치독이다).
    holiday = market_calendar.holiday_name(now, market_calendar.load_holiday_calendar())
    # 적재 감시 창 밖에서는 **DB를 아예 안 연다** — 밤새 매분 커넥션을 만들 이유가 없고,
    # 창 밖의 「적재 0분」은 이상이 아니라 정상이다(`liveness.INGEST_WATCH_*` 주석).
    ingest = (
        _recent_ingest_minutes(now)
        if stopped_at is None and holiday is None and liveness.in_ingest_window(now)
        else None
    )
    # 2026-08-19 Fix#4 — **`decide()`보다 먼저, 그리고 조기 return보다 먼저.** 정상일의 판정은
    # `ACTION_OK`라 아래에서 곧장 return하므로, 이 검사를 뒤에 두면 **평소에는 영영 안 돈다**
    # (그리고 평소가 바로 이 경보가 필요한 날이다 — 08-18은 인프라가 하루 종일 초록이었다).
    #
    # 게이트는 `ingest`와 같은 것을 쓴다: 사람이 껐거나 휴장일이면 점검이 없는 것이 정상이다.
    # `in_watch_window`(07:40~15:45)로 창을 잡는 이유는 밤새 매분 파일을 뒤지지 않기 위함이고,
    # 09:00 하한은 `_premarket_check_missing()`이 자체적으로 건다.
    if stopped_at is None and holiday is None and liveness.in_watch_window(now):
        if _premarket_check_missing(now):
            _alert_missing_premarket_check(now)
    decision = liveness.decide(
        beat, now, state,
        # 기동 스크립트가 도는 중이면 판정을 보류한다 — 안 그러면 기동이 서로를 덮어쓴다.
        starting=liveness.startup_in_progress(liveness.startup_marker_path(LOG_DIR), now),
        ingest_minutes_recent=ingest,
        stopped_at=stopped_at,
        holiday=holiday,
    )

    # 2026-08-12 Fix#8 — **판정했다는 사실 자체를 남긴다.** 로그보다 먼저, 그리고 IDLE에도 쓴다:
    # 08-12에 워치독이 5시간 31분 막혀 있었는데 `watchdog.log`의 마지막 줄이 「RESTART」라
    # 사고 대응 중인 것처럼 보였고, 그 침묵을 아무도 실시간으로 못 봤다. 상세 근거는
    # `liveness.watchdog_check_path` 위 주석. **`_restart()` 앞에 두는 것이 중요하다** —
    # 재기동은 최대 300초가 걸리고, 그 300초는 실제로 "판정하지 않은 시간"이 맞다.
    liveness.write_watchdog_check(
        liveness.watchdog_check_path(LOG_DIR), now,
        action=decision.action, detail=decision.detail,
    )

    stamp = f"[{now:%Y-%m-%d %H:%M:%S}]"

    # 2026-08-23 Fix#4 — **로그 분기보다 앞이다.** OK 경로는 `minute % 10`에만 찍고 곧장
    # return하므로, 종료 줄을 그 아래 두면 **열에 아홉은 사라진다**(08-21의 세 구간 중
    # 14:44·15:14가 정확히 그렇게 사라졌을 것이다). 종료 줄은 주기와 무관하게 그 한 번만 찍는다.
    episode, ongoing_note, closing_note = liveness.track_degraded_episode(
        _read_degraded_episode(), now, decision.action,
    )
    _write_degraded_episode(episode)
    if closing_note:
        _log(f"{stamp} RECOVERED — {closing_note}")

    if decision.action == liveness.ACTION_IDLE:
        # 감시 창 밖의 IDLE은 안 남긴다 — 매분 한 줄이면 하루 1,000줄이다.
        #
        # **의도적 정지는 예외로 남긴다**(2026-08-17). 이 구간은 「감시 창 밖이라 조용한 것」이
        # 아니라 **감시 창 안인데 우리가 감시를 껐다**는 뜻이고, 다음날 로그를 읽는 사람에게
        # 그 둘은 완전히 다른 사실이다. 08-12 Fix#8이 `.watchdog_last_check.json`을 만든 것과
        # 같은 이유이고, 주기도 `OK`와 같이 10분에 한 줄로 맞춘다.
        #
        # **휴장일도 같이 남긴다**(2026-08-17 2차). 달력이 틀려 거래일을 휴장일로 적은 날,
        # 이 줄이 그 오답을 드러내는 유일한 자리다 — 그날은 관측도 알림도 없으므로 다른
        # 신호가 하나도 안 나온다. 감시 창 안에서만 찍히므로 하루 최대 49줄이다.
        if now.minute % 10 == 0 and decision.reason in (
            liveness.REASON_INTENTIONAL_STOP, liveness.REASON_HOLIDAY,
        ):
            _log(f"{stamp} IDLE — {decision.detail}")
        return

    if decision.action == liveness.ACTION_OK:
        # 정상도 기록은 남긴다 — 다음날 "워치독이 돌기는 했는가"를 물을 수 있어야 한다.
        # 다만 10분에 한 번만(1분 주기 x 매번이면 로그가 이 줄로 채워진다).
        if now.minute % 10 == 0:
            _log(f"{stamp} OK — {decision.detail}")
        return

    if decision.action == liveness.ACTION_DEGRADED:
        # **생존 신호 이상이 아니다** — 박동은 정상이고 적재만 끊겼다. 문구를 갈라 두지 않으면
        # 다음날 로그를 읽는 사람이 08-14의 「살아 있는데 비어 있다」를 죽음으로 오독한다.
        # 2026-08-23 Fix#4 — 같은 문구가 08-21에 14줄 반복됐고 「몇 분째인가」를 사람이 세었다.
        message = f"관측 루프 적재 정지({decision.reason}) — {decision.detail}"
        if ongoing_note:
            message += f" · {ongoing_note}"
        # 2026-09-03 P2-3 — **문구는 줄 끝에만 붙인다.** 앞머리(`DEGRADED — 관측 루프 적재
        # 정지(...)`)를 건드리면 `mahdi/ops/watchdog_metrics.py`의 `startswith` 집계가 눈이 먼다.
        if decision.reason == liveness.REASON_NO_INGEST and _recovery_hint_due(episode):
            message += f" · {_NO_INGEST_RECOVERY_HINT}"
    else:
        message = f"관측 루프 생존 신호 이상({decision.reason}) — {decision.detail}"
    _log(f"{stamp} {decision.action.upper()} — {message}")

    if decision.action == liveness.ACTION_RESTART:
        ok, summary = _restart()
        _log(f"{stamp} 재기동 시도: {summary}")
        message += f" · 자동 재기동 {'성공' if ok else '실패'}({summary})"

    if decision.should_alert:
        notify.notify_sync(message, level="CRITICAL")

    _write_state(liveness.next_state(state, now, decision))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        # 워치독이 죽으면 그것을 감시할 것이 없다 — 최소한 자기 로그에는 남긴다.
        _log(f"워치독 자체 실패: {exc!r}")
