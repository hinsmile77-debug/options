"""테스트 전역 픽스처.

2026-07-31(운영점검보고서 2026-07-31 §4 우선순위 5): 모든 REST 폴러가 기동 직후
`_sleep_until_first_wall_tick()`으로 **벽시계 격자의 첫 지점까지** 대기하게 바뀌었다. 이 대기는
"콜드스타트 버스트 방지 + 첫 사이클부터 설계 위상에 앉히기"라는 하나의 관심사이고, 집계·실패
격리·스케줄 밀림 같은 나머지 폴러 테스트와는 직교한다. 그런데 대기 시간이 **실제 벽시계 시각에
좌우되기 때문에** 그대로 두면 `asyncio.sleep`을 세거나 가로채는 기존 테스트 40여 개가 실행 시각에
따라 결과가 달라진다(= 재현 불가능한 테스트가 된다).

그래서 기본값으로 이 대기만 무력화한다. 대기 동작 자체는 아래 두 축으로 별도 검증하므로
사각지대가 생기지 않는다:
  - 순수 함수 `_seconds_until_next_wall_tick()`의 단위 테스트(벽시계를 인자로 주입)
  - `wall_tick_alignment_enabled` 픽스처를 요청해 진짜 구현을 그대로 쓰는 폴러 테스트
"""

from __future__ import annotations

import pytest

import mahdi.main as mahdi_main

_REAL_SLEEP_UNTIL_FIRST_WALL_TICK = mahdi_main._sleep_until_first_wall_tick
_ALIGNMENT_OPT_IN_FIXTURE = "wall_tick_alignment_enabled"


@pytest.fixture
def wall_tick_alignment_enabled():
    """요청하면 아래 autouse 무력화가 비활성화돼 실제 벽시계 정렬 대기가 살아난다(마커 픽스처)."""
    return True


@pytest.fixture(autouse=True)
def _neutralize_wall_tick_alignment(request, monkeypatch):
    """폴러 기동 시 벽시계 정렬 대기를 기본적으로 건너뛴다(모듈 docstring 참고)."""
    if _ALIGNMENT_OPT_IN_FIXTURE in request.fixturenames:
        # 옵트인 테스트는 진짜 구현을 그대로 쓴다 — 모듈 속성이 교체되지 않았음을 명시적으로 보장.
        monkeypatch.setattr(
            mahdi_main, "_sleep_until_first_wall_tick", _REAL_SLEEP_UNTIL_FIRST_WALL_TICK
        )
        return

    async def _skip(interval_seconds: float, phase_offset_seconds: float) -> None:
        return None

    monkeypatch.setattr(mahdi_main, "_sleep_until_first_wall_tick", _skip)
